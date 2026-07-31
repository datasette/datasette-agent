"""Browser tasks: tool-initiated units of work executed by the user's
connected browser.

Tools call ``await context.browser_task(...)`` to hand the browser a
unit of work: trusted HTML rendered into the chat page (scripts run),
plus a JSON payload delivered exactly once through an atomic claim.
The turn suspends with the same PauseChain semantics as ask_user();
when the page posts a result (or the task expires or the user skips
it) the suspended chain resumes and the tool call re-executes,
replaying the stored result envelope.

The agent_browser_tasks table is the durable state, keyed on
(conversation_id, call_key, task_index) exactly like agent_questions.
At-most-once execution is enforced twice over: only pending tasks ever
render their HTML, and the pending -> running claim transition is an
atomic compare-and-set that succeeds exactly once.
"""

import json
from datetime import datetime, timezone

import llm

# Server-enforced ceiling on timeout_ms - a task cannot stay claimable
# for longer than this.
MAX_TIMEOUT_MS = 600_000  # 10 minutes

# result_json is size-capped on write; oversized completions are
# rejected with a structured error so the page can post a trimmed
# result.
MAX_RESULT_BYTES = 512 * 1024


class BrowserTaskPending(llm.PauseChain):
    """Raised by browser_task to suspend the current turn.

    As an llm.PauseChain subclass it propagates cleanly out of the
    chain: concurrent sibling tool calls run to completion first, no
    provider call is made with a placeholder result, and the agent
    loop catches it to end the turn. Resuming the chain re-executes
    the pending call, replaying finished tasks from the
    agent_browser_tasks table.
    """

    def __init__(self, task):
        super().__init__(task.get("label") or "Working in your browser")
        self.task = task


class BrowserTasksNotSupported(Exception):
    """browser_task is unavailable (e.g. background agents, CLI chat)."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def task_row_to_dict(row):
    """Shape an agent_browser_tasks row for the frontend: the SSE event
    and the pending-task embed on the conversation page.

    Deliberately excludes payload_json - per-run secrets are only ever
    handed out through the one-shot claim endpoint, never embedded in
    anything that gets rendered or persisted as markup.
    """
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "call_key": row["call_key"],
        "task_index": row["task_index"],
        "tool_name": row["tool_name"],
        "label": row["label"],
        "html": row["html"],
        "timeout_ms": row["timeout_ms"],
        "status": row["status"],
    }


def expired_envelope():
    return {
        "ok": False,
        "error": {"message": "Browser task timed out before completing"},
        "outcome": "expired",
    }


def cancelled_envelope():
    return {
        "ok": False,
        "error": {"message": "Cancelled by the user"},
        "outcome": "cancelled",
    }


def task_deadline(row):
    """Absolute deadline for a task row, as an aware datetime."""
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.timestamp() + row["timeout_ms"] / 1000


def task_is_overdue(row):
    return datetime.now(timezone.utc).timestamp() > task_deadline(row)


async def expire_task(db, task_id):
    """Convert a pending/running task to expired. Returns True if this
    call performed the conversion (CAS: first writer wins)."""
    result = await db.execute_write(
        "UPDATE agent_browser_tasks SET status = 'expired', result_json = ?, "
        "completed_at = ? WHERE id = ? AND status IN ('pending', 'running')",
        [json.dumps(expired_envelope()), _utc_now(), task_id],
    )
    return result.rowcount == 1


async def expire_overdue_tasks(db, conversation_id):
    """Lazy expiry sweep for the conversation view: convert any
    pending/running tasks whose deadline has passed."""
    rows = (
        await db.execute(
            "SELECT id, created_at, timeout_ms FROM agent_browser_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'running')",
            [conversation_id],
        )
    ).rows
    for row in rows:
        if task_is_overdue(row):
            await expire_task(db, row["id"])


async def claim_task(db, task_id, actor_id):
    """Atomic pending -> running transition; the heart of at-most-once.

    Returns (task_row, None) on a successful claim, or (None, state)
    where state is the reason the claim was refused. History replay,
    duplicate tabs, and re-renders all hit "already claimed" here and
    do nothing.
    """
    row = (
        await db.execute("SELECT * FROM agent_browser_tasks WHERE id = ?", [task_id])
    ).first()
    if row is None:
        return None, "not_found"
    if row["status"] in ("pending", "running") and task_is_overdue(row):
        await expire_task(db, task_id)
        return None, "expired"
    if row["status"] != "pending":
        return None, row["status"]
    result = await db.execute_write(
        "UPDATE agent_browser_tasks SET status = 'running', claimed_at = ?, "
        "completed_by = ? WHERE id = ? AND status = 'pending'",
        [_utc_now(), actor_id, task_id],
    )
    if result.rowcount != 1:
        # Lost the race to another tab: report the current state.
        current = (
            await db.execute(
                "SELECT status FROM agent_browser_tasks WHERE id = ?", [task_id]
            )
        ).first()
        return None, current["status"] if current else "not_found"
    claimed = (
        await db.execute("SELECT * FROM agent_browser_tasks WHERE id = ?", [task_id])
    ).first()
    return claimed, None


async def complete_task(db, task_id, envelope, actor_id):
    """Store a completion envelope: running -> completed (or pending ->
    completed, for hosts that skip claiming). First write wins.

    Returns None on success, else the state that blocked completion.
    """
    row = (
        await db.execute("SELECT * FROM agent_browser_tasks WHERE id = ?", [task_id])
    ).first()
    if row is None:
        return "not_found"
    if row["status"] in ("pending", "running") and task_is_overdue(row):
        await expire_task(db, task_id)
        return "expired"
    stored = dict(envelope)
    stored["outcome"] = "completed"
    result = await db.execute_write(
        "UPDATE agent_browser_tasks SET status = 'completed', result_json = ?, "
        "completed_at = ?, completed_by = ? "
        "WHERE id = ? AND status IN ('pending', 'running')",
        [json.dumps(stored), _utc_now(), actor_id, task_id],
    )
    if result.rowcount != 1:
        current = (
            await db.execute(
                "SELECT status FROM agent_browser_tasks WHERE id = ?", [task_id]
            )
        ).first()
        return current["status"] if current else "not_found"
    return None


async def cancel_task(db, task_id, actor_id):
    """User-initiated skip: pending/running -> cancelled.

    Returns None on success, else the state that blocked cancellation.
    """
    result = await db.execute_write(
        "UPDATE agent_browser_tasks SET status = 'cancelled', result_json = ?, "
        "completed_at = ?, completed_by = ? "
        "WHERE id = ? AND status IN ('pending', 'running')",
        [json.dumps(cancelled_envelope()), _utc_now(), actor_id, task_id],
    )
    if result.rowcount == 1:
        return None
    current = (
        await db.execute(
            "SELECT status FROM agent_browser_tasks WHERE id = ?", [task_id]
        )
    ).first()
    return current["status"] if current else "not_found"
