import json
from datetime import datetime, timezone

from datasette import Forbidden, NotFound, Response
from datasette.utils import tilde_decode
from datasette.utils.asgi import AsgiStream
from ulid import ULID

from .agent import resume_agent, run_agent
from .browser_tasks import (
    MAX_RESULT_BYTES,
    cancel_task,
    claim_task,
    complete_task,
    expire_overdue_tasks,
    task_row_to_dict,
)
from .export_markdown import format_conversation_markdown
from .messages import flatten_for_render
from .models import get_agent_models
from .questions import question_row_to_dict
from .schema import ensure_tables


def _actor_id(request):
    if request.actor:
        actor_id = request.actor.get("id")
        if actor_id is not None:
            return str(actor_id)
    return None


async def agent_index(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    actor_id = _actor_id(request)
    if actor_id:
        conversations = (
            await db.execute(
                "SELECT id, title, created_at, updated_at FROM agent_conversations "
                "WHERE actor_id = ? ORDER BY updated_at DESC",
                [actor_id],
            )
        ).rows
    else:
        conversations = (
            await db.execute(
                "SELECT id, title, created_at, updated_at FROM agent_conversations "
                "WHERE actor_id IS NULL ORDER BY updated_at DESC",
            )
        ).rows
    models, default_model = await get_agent_models(datasette, request.actor)
    return Response.html(
        await datasette.render_template(
            "agent_index.html",
            {
                "conversations": conversations,
                "models": models,
                "default_model": default_model,
            },
            request=request,
        )
    )


async def api_models(request, datasette):
    """List the models this actor can start an agent conversation with.

    Backs the model picker in the jump section rendered on every
    Datasette page - that markup is built client-side, so it fetches
    the list lazily instead of every page carrying it inline.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    models, default_model = await get_agent_models(datasette, request.actor)
    return Response.json({"models": models, "default_model": default_model})


async def api_create_conversation(request, datasette):
    """Create an empty conversation, optionally pinned to a model.

    ``{"model": "..."}`` in the JSON body picks the model for the whole
    conversation; it must be one of the ids from ``api_models`` for this
    actor. Omit it to use the datasette-llm default, resolved on the
    first turn. The model cannot be changed once the conversation
    exists.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    body = await request.post_body()
    data = {}
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return Response.json({"error": "Invalid JSON body"}, status=400)
        if not isinstance(data, dict):
            return Response.json({"error": "Body must be a JSON object"}, status=400)

    model_id = data.get("model")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id:
            return Response.json(
                {"error": "model must be a non-empty string"}, status=400
            )
        available, _ = await get_agent_models(datasette, request.actor)
        if model_id not in available:
            return Response.json(
                {"error": "Model not available: {}".format(model_id)}, status=400
            )

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = str(ULID())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, model_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [conversation_id, _actor_id(request), None, model_id, now, now],
    )
    return Response.json({"conversation_id": conversation_id, "model_id": model_id})


async def agent_conversation(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    actor_id = _actor_id(request)

    # Verify ownership
    row = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if row is None:
        raise NotFound("Conversation not found")
    if row["actor_id"] != actor_id:
        raise Forbidden("Forbidden")

    messages = (
        await db.execute(
            "SELECT * FROM agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows

    background_agent = (
        await db.execute(
            "SELECT id, status FROM agent_background_agents WHERE conversation_id = ?",
            [conversation_id],
        )
    ).first()

    pending_question = (
        await db.execute(
            "SELECT * FROM agent_questions "
            "WHERE conversation_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            [conversation_id],
        )
    ).first()
    pending_question_json = None
    if pending_question is not None:
        # < escaped so the JSON is safe inside a <script> block
        pending_question_json = json.dumps(
            question_row_to_dict(pending_question)
        ).replace("<", "\\u003c")

    # Lazy expiry: convert overdue browser tasks before deciding what
    # is still pending.
    await expire_overdue_tasks(db, conversation_id)
    pending_tasks = (
        await db.execute(
            "SELECT * FROM agent_browser_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'running') "
            "ORDER BY created_at",
            [conversation_id],
        )
    ).rows
    pending_tasks_json = None
    if pending_tasks:
        pending_tasks_json = json.dumps(
            [task_row_to_dict(t) for t in pending_tasks]
        ).replace("<", "\\u003c")

    # A task that expired while no page was open leaves the turn
    # suspended with nothing to complete or cancel; flag the page so
    # the frontend auto-resumes instead of stranding the conversation.
    needs_resume = False
    if not pending_tasks and pending_question is None:
        needs_resume = (await _conversation_resume_state(db, conversation_id)) is None

    return Response.html(
        await datasette.render_template(
            "agent_conversation.html",
            {
                "conversation": dict(row),
                "messages": flatten_for_render(messages),
                "conversation_id": conversation_id,
                "background_agent": (
                    dict(background_agent) if background_agent else None
                ),
                "pending_question_json": pending_question_json,
                "pending_tasks_json": pending_tasks_json,
                "needs_resume": needs_resume,
            },
            request=request,
        )
    )


async def agent_conversation_poll(request, datasette):
    """Lightweight heartbeat for the conversation page.

    Returns the live status of the linked background agent (null for user
    chats) plus the current message count, so the client can decide
    when new content has arrived and reload.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)

    conversation_id = request.url_vars["conversation_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT actor_id FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    background_agent = (
        await db.execute(
            "SELECT status FROM agent_background_agents WHERE conversation_id = ?",
            [conversation_id],
        )
    ).first()
    count_row = (
        await db.execute(
            "SELECT count(*) as c FROM agent_messages WHERE conversation_id = ?",
            [conversation_id],
        )
    ).first()

    return Response.json(
        {
            "agent_status": background_agent["status"] if background_agent else None,
            "message_count": count_row["c"] if count_row else 0,
        }
    )


async def agent_conversation_markdown(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if row is None:
        raise NotFound("Conversation not found")
    if row["actor_id"] != actor_id:
        raise Forbidden("Forbidden")

    messages = (
        await db.execute(
            "SELECT * FROM agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows

    title = row["title"] or "Chat"
    markdown = format_conversation_markdown(title, flatten_for_render(messages))

    # Sanitize title for filename
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[
        :60
    ].strip()
    filename = f"{safe_title}.md" if safe_title else "chat.md"

    return Response(
        body=markdown,
        content_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def agent_stream(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    actor_id = _actor_id(request)

    # Verify ownership
    row = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    body = await request.post_body()
    data = json.loads(body)
    user_message = data.get("message", "")

    async def stream_fn(writer):
        await run_agent(
            datasette=datasette,
            actor=request.actor,
            conversation_id=conversation_id,
            user_message=user_message,
            writer=writer,
        )

    return AsgiStream(
        stream_fn,
        headers={
            "Cache-Control": "no-cache",
            "Content-Encoding": "none",
        },
        content_type="text/event-stream",
    )


async def api_answer_question(request, datasette):
    """Record the user's answer to a pending ask_user() question, then
    resume the suspended conversation, streaming SSE like agent_stream."""
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    question_id = request.url_vars["question_id"]
    actor_id = _actor_id(request)

    conversation = (
        await db.execute(
            "SELECT actor_id FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if conversation is None:
        return Response.json({"error": "Not found"}, status=404)
    if conversation["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    question = (
        await db.execute(
            "SELECT * FROM agent_questions WHERE id = ? AND conversation_id = ?",
            [question_id, conversation_id],
        )
    ).first()
    if question is None:
        return Response.json({"error": "Question not found"}, status=404)
    if question["status"] != "pending":
        return Response.json({"error": "Question is not pending"}, status=400)

    body = await request.post_body()
    try:
        answer = json.loads(body).get("answer")
    except json.JSONDecodeError:
        return Response.json({"error": "Invalid JSON body"}, status=400)

    question_type = question["question_type"]
    if question_type == "boolean":
        if not isinstance(answer, bool):
            return Response.json({"error": "Answer must be true or false"}, status=400)
    elif question_type == "choice":
        options = json.loads(question["options_json"] or "[]")
        if not isinstance(answer, str) or answer not in options:
            return Response.json(
                {"error": "Answer must be one of: {}".format(", ".join(options))},
                status=400,
            )
    else:
        if not isinstance(answer, str):
            return Response.json({"error": "Answer must be a string"}, status=400)

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE agent_questions SET status = 'answered', answer_json = ?, "
        "answered_by = ?, answered_at = ? WHERE id = ? AND status = 'pending'",
        [json.dumps(answer), actor_id, now, question_id],
    )

    async def stream_fn(writer):
        await resume_agent(
            datasette=datasette,
            actor=request.actor,
            conversation_id=conversation_id,
            writer=writer,
        )

    return AsgiStream(
        stream_fn,
        headers={
            "Cache-Control": "no-cache",
            "Content-Encoding": "none",
        },
        content_type="text/event-stream",
    )


async def _history_suspended(db, conversation_id):
    """True when the persisted history ends in an assistant message
    with tool calls and no tool results after it - the shape a turn
    suspended on ask_user() or browser_task() leaves behind."""
    row = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            [conversation_id],
        )
    ).first()
    if row is None or row["role"] != "assistant":
        return False
    try:
        parts = json.loads(row["message_json"]).get("parts", [])
    except (json.JSONDecodeError, AttributeError):
        return False
    return any(p.get("type") == "tool_call" for p in parts)


async def _conversation_resume_state(db, conversation_id):
    """Classify whether a suspended conversation is safe to resume with
    no completion in hand.

    Returns None when it is: the history is suspended, no browser task
    is still live, no question awaits an answer, and at least one
    finished (non-consumed) task exists for the re-executed tool call
    to replay - evidence the suspension came from a browser task
    rather than a turn actively streaming in another tab. Otherwise
    returns the blocking state.
    """
    live = (
        await db.execute(
            "SELECT count(*) AS c FROM agent_browser_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'running')",
            [conversation_id],
        )
    ).first()
    if live["c"]:
        return "pending"
    question = (
        await db.execute(
            "SELECT count(*) AS c FROM agent_questions "
            "WHERE conversation_id = ? AND status = 'pending'",
            [conversation_id],
        )
    ).first()
    if question["c"]:
        return "question_pending"
    finished = (
        await db.execute(
            "SELECT count(*) AS c FROM agent_browser_tasks "
            "WHERE conversation_id = ? "
            "AND status IN ('completed', 'expired', 'cancelled')",
            [conversation_id],
        )
    ).first()
    if not finished["c"]:
        return "not_suspended"
    if not await _history_suspended(db, conversation_id):
        return "not_suspended"
    return None


async def _task_request_checks(request, datasette):
    """Shared preamble for the browser-task endpoints: POST only,
    conversation ownership, task lookup. Returns (db, task_row,
    error_response) - exactly one of task_row/error_response is set.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return None, None, Response.json({"error": "POST required"}, status=405)

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    task_id = request.url_vars["task_id"]
    actor_id = _actor_id(request)

    conversation = (
        await db.execute(
            "SELECT actor_id FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if conversation is None:
        return None, None, Response.json({"error": "Not found"}, status=404)
    if conversation["actor_id"] != actor_id:
        return None, None, Response.json({"error": "Forbidden"}, status=403)

    task = (
        await db.execute(
            "SELECT * FROM agent_browser_tasks WHERE id = ? AND conversation_id = ?",
            [task_id, conversation_id],
        )
    ).first()
    if task is None:
        return None, None, Response.json({"error": "Task not found"}, status=404)
    return db, task, None


def _resume_stream(request, datasette, conversation_id):
    """Stream the resumed turn back on this response, exactly as the
    question-answer endpoint does - the completing tab is the tab
    watching the conversation, so it receives the resumed turn's
    events on the same connection.
    """

    async def stream_fn(writer):
        await resume_agent(
            datasette=datasette,
            actor=request.actor,
            conversation_id=conversation_id,
            writer=writer,
        )

    return AsgiStream(
        stream_fn,
        headers={
            "Cache-Control": "no-cache",
            "Content-Encoding": "none",
        },
        content_type="text/event-stream",
    )


async def api_claim_task(request, datasette):
    """Atomically claim a pending browser task: pending -> running.

    The claim succeeds exactly once and is the only way the payload is
    ever handed out - history replay, duplicate tabs, and re-renders
    all hit "already claimed" and do nothing.
    """
    db, task, error = await _task_request_checks(request, datasette)
    if error is not None:
        return error

    claimed, state = await claim_task(db, task["id"], _actor_id(request))
    if claimed is None:
        return Response.json({"ok": False, "state": state})
    return Response.json(
        {
            "ok": True,
            "task": {
                "id": claimed["id"],
                "payload": (
                    json.loads(claimed["payload_json"])
                    if claimed["payload_json"]
                    else None
                ),
                "timeout_ms": claimed["timeout_ms"],
            },
        }
    )


async def api_complete_task(request, datasette):
    """Store the result envelope a browser task posted, then resume the
    suspended conversation, streaming SSE like the answer endpoint.

    Completing from 'pending' - without claiming first - is allowed by
    design, for hosts that skip claiming (callback executors, future
    headless runners). It is safe because the endpoint is actor-bound
    and the payload is still only obtainable through the one-shot
    claim.
    """
    db, task, error = await _task_request_checks(request, datasette)
    if error is not None:
        return error

    body = await request.post_body()
    if len(body) > MAX_RESULT_BYTES:
        return Response.json(
            {
                "ok": False,
                "error": {
                    "code": "result_too_large",
                    "message": "Result exceeds {} bytes - post a trimmed result".format(
                        MAX_RESULT_BYTES
                    ),
                },
            },
            status=400,
        )
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        return Response.json({"error": "Invalid JSON body"}, status=400)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
        return Response.json(
            {"error": 'Body must be a JSON object with a boolean "ok"'}, status=400
        )

    state = await complete_task(db, task["id"], envelope, _actor_id(request))
    if state is not None:
        return Response.json({"ok": False, "state": state}, status=400)

    return _resume_stream(request, datasette, task["conversation_id"])


async def api_cancel_task(request, datasette):
    """User-initiated skip: pending/running -> cancelled, then resume.
    The escape hatch that stops a hung task bricking the conversation -
    the tool resumes with a failure result."""
    db, task, error = await _task_request_checks(request, datasette)
    if error is not None:
        return error

    state = await cancel_task(db, task["id"], _actor_id(request))
    if state is not None:
        return Response.json({"ok": False, "state": state}, status=400)

    return _resume_stream(request, datasette, task["conversation_id"])


async def api_resume_conversation(request, datasette):
    """Resume a turn suspended with no live pending work - e.g. a
    browser task that expired while no page was open, leaving nothing
    to complete or cancel.

    Runs the lazy-expiry sweep first, then refuses unless the
    conversation is verifiably stuck: 409 while a task or question is
    still genuinely pending (the frontend deadline watcher re-arms on
    this - it can never steal a claim by poking here), 400 when the
    history is not suspended. On success streams the resumed turn as
    SSE like the answer endpoint.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = request.url_vars["conversation_id"]
    actor_id = _actor_id(request)

    conversation = (
        await db.execute(
            "SELECT actor_id FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if conversation is None:
        return Response.json({"error": "Not found"}, status=404)
    if conversation["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    await expire_overdue_tasks(db, conversation_id)
    state = await _conversation_resume_state(db, conversation_id)
    if state in ("pending", "question_pending"):
        return Response.json({"ok": False, "state": state}, status=409)
    if state is not None:
        return Response.json({"ok": False, "state": state}, status=400)

    return _resume_stream(request, datasette, conversation_id)


async def agent_background_index(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    await datasette.ensure_permission(
        action="datasette-agent-background", actor=request.actor
    )
    db = datasette.get_internal_database()
    await ensure_tables(db)
    actor_id = _actor_id(request)

    if actor_id:
        agents = (
            await db.execute(
                "SELECT * FROM agent_background_agents "
                "WHERE actor_id = ? ORDER BY created_at DESC",
                [actor_id],
            )
        ).rows
    else:
        agents = (
            await db.execute(
                "SELECT * FROM agent_background_agents "
                "WHERE actor_id IS NULL ORDER BY created_at DESC",
            )
        ).rows

    return Response.html(
        await datasette.render_template(
            "agent_background.html",
            {"agents": [dict(a) for a in agents]},
            request=request,
        )
    )


async def api_create_background_agent(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    await datasette.ensure_permission(
        action="datasette-agent-background", actor=request.actor
    )
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    from .api import get_background_agent_status, start_background_agent

    body = await request.post_body()
    data = json.loads(body)
    goal = data.get("goal", "")
    if not goal:
        return Response.json({"error": "goal is required"}, status=400)

    actor = request.actor
    agent_id = await start_background_agent(
        datasette=datasette,
        actor=actor,
        goal=goal,
    )
    status = await get_background_agent_status(datasette, agent_id)
    return Response.json(
        {
            "agent_id": agent_id,
            "conversation_id": status["conversation_id"],
            "status": status["status"],
        }
    )


async def api_background_agent_status(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    await datasette.ensure_permission(
        action="datasette-agent-background", actor=request.actor
    )
    db = datasette.get_internal_database()
    await ensure_tables(db)

    agent_id = request.url_vars["agent_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT * FROM agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    return Response.json(dict(row))


async def api_cancel_background_agent(request, datasette):
    """Cancel a pending/running background agent.

    Idempotent for already-terminal rows: returns 200 with cancelled=false
    so the caller can refresh and see the existing result.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    await datasette.ensure_permission(
        action="datasette-agent-background", actor=request.actor
    )
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    db = datasette.get_internal_database()
    await ensure_tables(db)

    agent_id = request.url_vars["agent_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT actor_id, status FROM agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    if row["status"] in ("completed", "error"):
        return Response.json(
            {"agent_id": agent_id, "status": row["status"], "cancelled": False}
        )

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE agent_background_agents "
        "SET status = 'error', error = ?, updated_at = ? "
        "WHERE id = ? AND status IN ('pending', 'running')",
        ["Cancelled by user", now, agent_id],
    )

    task = getattr(datasette, "_background_agent_tasks", {}).get(agent_id)
    if task is not None and not task.done():
        task.cancel()

    return Response.json({"agent_id": agent_id, "status": "error", "cancelled": True})


async def explorer_page(request, datasette):
    await datasette.ensure_permission(
        action="datasette-agent-explore", actor=request.actor
    )
    db = datasette.get_internal_database()
    await ensure_tables(db)

    database_name = tilde_decode(request.url_vars["database"])
    table_name = request.url_vars.get("table")
    if table_name:
        table_name = tilde_decode(table_name)
    actor_id = _actor_id(request)

    # Verify the database exists
    try:
        target_db = datasette.get_database(database_name)
    except KeyError:
        raise NotFound("Database not found")

    # Verify the table exists if specified
    if table_name:
        tables = await target_db.table_names()
        if table_name not in tables:
            raise NotFound("Table not found")

    # Fetch reports for this database/table
    if table_name:
        reports = (
            await db.execute(
                "SELECT r.*, a.status as agent_status, "
                "a.final_message as agent_final_message, "
                "a.error as agent_error, "
                "a.conversation_id as agent_conversation_id "
                "FROM agent_explorer_reports r "
                "LEFT JOIN agent_background_agents a ON r.agent_id = a.id "
                "WHERE r.database_name = ? AND r.table_name = ? AND r.actor_id = ? "
                "ORDER BY r.created_at DESC",
                [database_name, table_name, actor_id],
            )
        ).rows
    else:
        # Database-level page: show all reports for this database
        # (both database-wide and table-specific)
        reports = (
            await db.execute(
                "SELECT r.*, a.status as agent_status, "
                "a.final_message as agent_final_message, "
                "a.error as agent_error, "
                "a.conversation_id as agent_conversation_id "
                "FROM agent_explorer_reports r "
                "LEFT JOIN agent_background_agents a ON r.agent_id = a.id "
                "WHERE r.database_name = ? AND r.actor_id = ? "
                "ORDER BY r.created_at DESC",
                [database_name, actor_id],
            )
        ).rows

    return Response.html(
        await datasette.render_template(
            "agent_explorer.html",
            {
                "database_name": database_name,
                "table_name": table_name,
                "reports": [dict(r) for r in reports],
            },
            request=request,
        )
    )


async def explorer_report_page(request, datasette):
    await datasette.ensure_permission(
        action="datasette-agent-explore", actor=request.actor
    )
    db = datasette.get_internal_database()
    await ensure_tables(db)

    report_id = request.url_vars["report_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT r.*, a.status as agent_status, a.final_message as agent_final_message, "
            "a.error as agent_error, a.conversation_id as agent_conversation_id "
            "FROM agent_explorer_reports r "
            "LEFT JOIN agent_background_agents a ON r.agent_id = a.id "
            "WHERE r.id = ?",
            [report_id],
        )
    ).first()
    if row is None:
        raise NotFound("Report not found")
    if row["actor_id"] != actor_id:
        raise Forbidden("Forbidden")

    return Response.html(
        await datasette.render_template(
            "agent_explorer_report.html",
            {"report": dict(row)},
            request=request,
        )
    )


async def api_start_explorer(request, datasette):
    await datasette.ensure_permission(
        action="datasette-agent-explore", actor=request.actor
    )
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    body = await request.post_body()
    data = json.loads(body)
    database_name = data.get("database")
    table_name = data.get("table")
    extra_prompt = data.get("extra_prompt") or None

    if not database_name:
        return Response.json({"error": "database is required"}, status=400)

    from .explorer import start_explorer

    report_id, agent_id = await start_explorer(
        datasette=datasette,
        actor=request.actor,
        database_name=database_name,
        table_name=table_name,
        extra_prompt=extra_prompt,
    )
    return Response.json({"report_id": report_id, "agent_id": agent_id})


async def api_explorer_report(request, datasette):
    await datasette.ensure_permission(
        action="datasette-agent-explore", actor=request.actor
    )
    db = datasette.get_internal_database()
    await ensure_tables(db)

    report_id = request.url_vars["report_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT r.*, a.status as agent_status, a.final_message as agent_final_message, "
            "a.error as agent_error, a.conversation_id as agent_conversation_id "
            "FROM agent_explorer_reports r "
            "LEFT JOIN agent_background_agents a ON r.agent_id = a.id "
            "WHERE r.id = ?",
            [report_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    return Response.json(dict(row))
