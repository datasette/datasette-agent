"""Human-in-the-loop questions for agent tools.

Tools that declare a ``context`` parameter receive a ToolContext. Calling
``await context.ask_user(...)`` either replays a previously stored answer
(when the tool call is being re-executed after the user answered) or
records a pending question and raises QuestionPending to suspend the turn.

The agent_questions table is the durable state: a suspended conversation
can resume after a server restart because everything ask_user needs is
keyed on (conversation_id, call_key, ask_index).

call_key identifies one tool call across re-executions: the provider's
tool_call_id when available, otherwise a hash of the tool name and
arguments (identical concurrent calls would then share answers, which is
acceptable - they would ask identical questions).
"""

import hashlib
import html as html_module
import inspect
import json
from datetime import datetime, timezone

import llm
from ulid import ULID


class QuestionPending(llm.PauseChain):
    """Raised by ask_user to suspend the current turn.

    As an llm.PauseChain subclass it propagates cleanly out of the
    chain: concurrent sibling tool calls run to completion first, no
    provider call is made with a placeholder result, and the agent
    loop catches it to end the turn. Resuming the chain with the
    persisted history re-executes the pending call, replaying answered
    questions from the agent_questions table.
    """

    def __init__(self, question):
        super().__init__(question["prompt"])
        self.question = question


class QuestionsNotSupported(Exception):
    """ask_user is unavailable (e.g. background agents, CLI chat)."""


def call_key_for(tool_name, arguments, tool_call_id):
    if tool_call_id:
        return "id:{}".format(tool_call_id)
    args_hash = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=repr).encode("utf-8")
    ).hexdigest()[:16]
    return "call:{}:{}".format(tool_name, args_hash)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def question_row_to_dict(row):
    """Shape an agent_questions row like the dict QuestionPending carries."""
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "call_key": row["call_key"],
        "ask_index": row["ask_index"],
        "tool_name": row["tool_name"],
        "question_type": row["question_type"],
        "prompt": row["prompt"],
        "options": json.loads(row["options_json"]) if row["options_json"] else None,
        "html": row["html"],
    }


class ToolContext:
    """Per-invocation context passed to tools that declare ``context``.

    Constructed fresh for every tool call (tool calls can execute
    concurrently) - never shared, no ambient state.
    """

    def __init__(
        self,
        *,
        datasette,
        actor,
        conversation_id,
        tool_name,
        arguments,
        tool_call_id=None,
        supports_questions=True,
        auto_approve=False,
        ask_user_callback=None,
    ):
        self.datasette = datasette
        self.actor = actor
        self.conversation_id = conversation_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.tool_call_id = tool_call_id
        self.supports_questions = supports_questions
        self.auto_approve = auto_approve
        self.ask_user_callback = ask_user_callback
        self.call_key = call_key_for(tool_name, arguments, tool_call_id)
        self._ask_index = 0

    async def ask_user(
        self, prompt, *, options=None, free_text=False, html=None, text=None
    ):
        """Ask the user a question; returns their answer.

        - no kwargs: yes/no question, returns bool
        - options=[...]: multiple choice, returns the selected option
        - free_text=True: freeform question, returns str

        html= is optional trusted HTML rendered above the question in
        the UI - use it to show the user exactly what they are
        approving (escape any interpolated content yourself).
        text= is an optional terminal-friendly version; if html= is not
        provided, text= is escaped and rendered above the web question.

        Raises QuestionPending if the answer is not yet available. The
        code before this call re-runs when the tool call is re-executed
        after the user answers, so ask before performing side effects.
        """
        if options is not None and free_text:
            raise ValueError("options= and free_text=True are mutually exclusive")
        if options is not None:
            question_type = "choice"
            options = list(options)
        elif free_text:
            question_type = "text"
        else:
            question_type = "boolean"

        if self.auto_approve and question_type == "boolean":
            return True

        if html is None and text is not None:
            html = "<pre>{}</pre>".format(html_module.escape(text))

        if self.ask_user_callback is not None:
            result = self.ask_user_callback(
                {
                    "tool_name": self.tool_name,
                    "question_type": question_type,
                    "prompt": prompt,
                    "options": options,
                    "html": html,
                    "text": text,
                }
            )
            if inspect.isawaitable(result):
                result = await result
            return result

        if not self.supports_questions:
            raise QuestionsNotSupported(
                "ask_user() is not available in this context - proceed without "
                "asking, or report that user input is required"
            )

        ask_index = self._ask_index
        self._ask_index += 1

        db = self.datasette.get_internal_database()
        answered = (
            await db.execute(
                "SELECT answer_json FROM agent_questions "
                "WHERE conversation_id = ? AND call_key = ? AND ask_index = ? "
                "AND status = 'answered'",
                [self.conversation_id, self.call_key, ask_index],
            )
        ).first()
        if answered is not None:
            return json.loads(answered["answer_json"])

        # Re-raising for a question that is already pending (e.g. a second
        # suspended tool call re-executed on resume) must not insert a
        # duplicate row.
        existing = (
            await db.execute(
                "SELECT * FROM agent_questions "
                "WHERE conversation_id = ? AND call_key = ? AND ask_index = ? "
                "AND status = 'pending'",
                [self.conversation_id, self.call_key, ask_index],
            )
        ).first()
        if existing is not None:
            raise QuestionPending(question_row_to_dict(existing))

        question = {
            "id": str(ULID()),
            "conversation_id": self.conversation_id,
            "call_key": self.call_key,
            "ask_index": ask_index,
            "tool_name": self.tool_name,
            "question_type": question_type,
            "prompt": prompt,
            "options": options,
            "html": html,
        }
        await db.execute_write(
            "INSERT INTO agent_questions "
            "(id, conversation_id, call_key, ask_index, tool_name, question_type, "
            "prompt, options_json, html, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            [
                question["id"],
                self.conversation_id,
                self.call_key,
                ask_index,
                self.tool_name,
                question_type,
                prompt,
                json.dumps(options) if options is not None else None,
                html,
                _utc_now(),
            ],
        )
        raise QuestionPending(question)

    async def mark_questions_consumed(self):
        """Call once the tool call has completed: its answered questions
        must not replay for a later identical call."""
        db = self.datasette.get_internal_database()
        await db.execute_write(
            "UPDATE agent_questions SET status = 'consumed' "
            "WHERE conversation_id = ? AND call_key = ? AND status = 'answered'",
            [self.conversation_id, self.call_key],
        )
