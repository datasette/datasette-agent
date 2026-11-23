"""Per-invocation context for agent tools.

Tools that declare a ``context`` parameter receive a ToolContext,
constructed fresh for every tool call. It is the tool-facing surface
for the two suspend/resume primitives:

- ``await context.ask_user(...)`` - human-in-the-loop questions,
  backed by the agent_questions table (see questions.py).
- ``await context.browser_task(...)`` - units of work executed by the
  user's connected browser, backed by the agent_browser_tasks table
  (see browser_tasks.py).

Both either replay a previously stored result (when the tool call is
being re-executed after resume) or record a pending row and raise an
llm.PauseChain subclass to suspend the turn. The tables are the
durable state: a suspended conversation resumes after a server restart
because everything is keyed on (conversation_id, call_key, index).

call_key identifies one tool call across re-executions: the provider's
tool_call_id when available, otherwise a hash of the tool name and
arguments (identical concurrent calls would then share results, which
is acceptable - they would ask identical questions or issue identical
tasks).
"""

import hashlib
import html as html_module
import inspect
import json
from datetime import datetime, timezone

from ulid import ULID

from .browser_tasks import (
    MAX_TIMEOUT_MS,
    BrowserTaskPending,
    BrowserTasksNotSupported,
    expire_task,
    task_is_overdue,
    task_row_to_dict,
)
from .questions import QuestionPending, QuestionsNotSupported, question_row_to_dict


def call_key_for(tool_name, arguments, tool_call_id):
    if tool_call_id:
        return "id:{}".format(tool_call_id)
    args_hash = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=repr).encode("utf-8")
    ).hexdigest()[:16]
    return "call:{}:{}".format(tool_name, args_hash)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


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
        supports_browser_tasks=True,
        browser_task_callback=None,
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
        self.supports_browser_tasks = supports_browser_tasks
        self.browser_task_callback = browser_task_callback
        self.call_key = call_key_for(tool_name, arguments, tool_call_id)
        self._ask_index = 0
        self._task_index = 0

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

    async def browser_task(self, html, *, payload=None, label=None, timeout_ms=60_000):
        """Hand the user's browser a unit of work; returns its result.

        html is trusted server-authored HTML rendered into the chat
        page - its scripts run. payload is JSON delivered to the
        executing page exactly once, through the one-shot claim; put
        per-run secrets there, never in html. The return value is the
        envelope the page posted - {"ok": True, "result": ...} or
        {"ok": False, "error": {...}} - with an "outcome" key of
        "completed", "expired" or "cancelled". Failures come back as
        data, not exceptions.

        Raises BrowserTaskPending if the result is not yet available.
        The code before this call re-runs when the tool call is
        re-executed after the task finishes, so run it before
        performing side effects.
        """
        timeout_ms = max(1, min(int(timeout_ms), MAX_TIMEOUT_MS))

        if self.browser_task_callback is not None:
            result = self.browser_task_callback(
                {
                    "tool_name": self.tool_name,
                    "html": html,
                    "payload": payload,
                    "label": label,
                    "timeout_ms": timeout_ms,
                }
            )
            if inspect.isawaitable(result):
                result = await result
            # Normalize exactly like the HTTP complete path merges in
            # outcome: "completed" - tools cannot tell executors apart.
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("outcome", "completed")
            return result

        if not self.supports_browser_tasks:
            raise BrowserTasksNotSupported(
                "browser_task() is not available in this context - proceed "
                "without it, or report that a connected browser is required"
            )

        task_index = self._task_index
        self._task_index += 1

        db = self.datasette.get_internal_database()
        # Consumed rows are audit records of earlier identical calls -
        # skip them (so a later identical call runs fresh) but never
        # delete them. The latest non-consumed row is this call's task.
        existing = (
            await db.execute(
                "SELECT * FROM agent_browser_tasks "
                "WHERE conversation_id = ? AND call_key = ? AND task_index = ? "
                "AND status != 'consumed' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                [self.conversation_id, self.call_key, task_index],
            )
        ).first()
        if existing is not None:
            status = existing["status"]
            if status in ("pending", "running") and task_is_overdue(existing):
                # Lazy expiry: the deadline passed with no completion
                # (crashed or closed tab). Convert and fall through to
                # the terminal-replay path.
                await expire_task(db, existing["id"])
                existing = (
                    await db.execute(
                        "SELECT * FROM agent_browser_tasks WHERE id = ?",
                        [existing["id"]],
                    )
                ).first()
                status = existing["status"]
            if status in ("completed", "expired", "cancelled"):
                return json.loads(existing["result_json"])
            # Re-raising for an already-pending task (e.g. a second
            # suspended tool call re-executed on resume) must not
            # insert a duplicate row.
            raise BrowserTaskPending(task_row_to_dict(existing))

        task_id = str(ULID())
        await db.execute_write(
            "INSERT INTO agent_browser_tasks "
            "(id, conversation_id, call_key, task_index, tool_name, label, "
            "html, payload_json, timeout_ms, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            [
                task_id,
                self.conversation_id,
                self.call_key,
                task_index,
                self.tool_name,
                label,
                html,
                json.dumps(payload) if payload is not None else None,
                timeout_ms,
                _utc_now(),
            ],
        )
        row = (
            await db.execute(
                "SELECT * FROM agent_browser_tasks WHERE id = ?", [task_id]
            )
        ).first()
        raise BrowserTaskPending(task_row_to_dict(row))

    async def mark_browser_tasks_consumed(self):
        """Call once the tool call has completed: its finished tasks
        must not replay for a later identical call."""
        db = self.datasette.get_internal_database()
        await db.execute_write(
            "UPDATE agent_browser_tasks SET status = 'consumed' "
            "WHERE conversation_id = ? AND call_key = ? "
            "AND status IN ('completed', 'expired', 'cancelled')",
            [self.conversation_id, self.call_key],
        )
