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
import json
from datetime import datetime, timezone

from ulid import ULID


class QuestionPending(Exception):
    """Raised by ask_user to suspend the current turn.

    The llm library converts tool exceptions into error ToolResults but
    preserves the original on tool_result.exception - the agent loop
    detects this one there and suspends instead of continuing the chain.
    """

    def __init__(self, question):
        self.question = question
        super().__init__(question["prompt"])


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


def find_pending_tool_calls(message_rows):
    """Given agent_messages rows (dicts with role + message_json, in id
    order), return the tool calls from the LAST assistant message that have
    no tool_result yet: [{name, arguments, tool_call_id}, ...].

    A suspended turn always halts at its final assistant message - any
    earlier tool calls already have their results persisted.
    """
    last_assistant_index = None
    tool_calls = []
    for i, row in enumerate(message_rows):
        if row["role"] != "assistant":
            continue
        msg = json.loads(row["message_json"])
        calls = [p for p in msg.get("parts", []) if p.get("type") == "tool_call"]
        if calls:
            last_assistant_index = i
            tool_calls = calls
    if last_assistant_index is None:
        return []

    results = []
    for row in message_rows[last_assistant_index + 1 :]:
        if row["role"] != "tool":
            continue
        msg = json.loads(row["message_json"])
        results.extend(
            p for p in msg.get("parts", []) if p.get("type") == "tool_result"
        )

    pending = []
    unmatched_result_names = [
        r.get("name") for r in results if not r.get("tool_call_id")
    ]
    result_ids = {r.get("tool_call_id") for r in results if r.get("tool_call_id")}
    for call in tool_calls:
        call_id = call.get("tool_call_id")
        if call_id:
            if call_id in result_ids:
                continue
        else:
            # No id (some providers): match by name, consuming one
            # result per call so duplicate calls pair up FIFO.
            if call.get("name") in unmatched_result_names:
                unmatched_result_names.remove(call.get("name"))
                continue
        pending.append(
            {
                "name": call.get("name"),
                "arguments": call.get("arguments") or {},
                "tool_call_id": call_id,
            }
        )
    return pending


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
    ):
        self.datasette = datasette
        self.actor = actor
        self.conversation_id = conversation_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.tool_call_id = tool_call_id
        self.supports_questions = supports_questions
        self.call_key = call_key_for(tool_name, arguments, tool_call_id)
        self._ask_index = 0

    async def ask_user(self, prompt, *, options=None, free_text=False, html=None):
        """Ask the user a question; returns their answer.

        - no kwargs: yes/no question, returns bool
        - options=[...]: multiple choice, returns the selected option
        - free_text=True: freeform question, returns str

        html= is optional trusted HTML rendered above the question in
        the UI - use it to show the user exactly what they are
        approving (escape any interpolated content yourself).

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
