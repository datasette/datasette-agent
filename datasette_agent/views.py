import asyncio
import json
from datetime import datetime, timezone

from datasette.utils import tilde_decode
from datasette.utils.asgi import AsgiStream, Response
from ulid import ULID

from .agent import run_agent
from .export_markdown import format_conversation_markdown
from .messages import flatten_for_render
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
    return Response.html(
        await datasette.render_template(
            "agent_index.html",
            {"conversations": conversations},
            request=request,
        )
    )


async def api_create_conversation(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = str(ULID())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, _actor_id(request), None, now, now],
    )
    return Response.json({"conversation_id": conversation_id})


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
        return Response.html("Conversation not found", status=404)
    if row["actor_id"] != actor_id:
        return Response.html("Forbidden", status=403)

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
        return Response.html("Conversation not found", status=404)
    if row["actor_id"] != actor_id:
        return Response.html("Forbidden", status=403)

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
        # Register this run so POST .../cancel can interrupt it mid-stream.
        # stream_fn IS the cancellable coroutine, so we track its own task.
        tasks = getattr(datasette, "_chat_agent_tasks", None)
        if tasks is None:
            tasks = datasette._chat_agent_tasks = {}
        tasks[conversation_id] = asyncio.current_task()
        try:
            await run_agent(
                datasette=datasette,
                actor=request.actor,
                conversation_id=conversation_id,
                user_message=user_message,
                writer=writer,
            )
        finally:
            tasks.pop(conversation_id, None)

    return AsgiStream(
        stream_fn,
        headers={
            "Cache-Control": "no-cache",
            "Content-Encoding": "none",
        },
        content_type="text/event-stream",
    )


async def api_cancel_chat(request, datasette):
    """Stop an in-flight chat stream for a conversation.

    Mirrors api_cancel_background_agent: idempotent for conversations with
    no live stream (returns 200 with cancelled=false), so the client can
    safely fire-and-forget when the user hits Stop.
    """
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

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

    task = getattr(datasette, "_chat_agent_tasks", {}).get(conversation_id)
    cancelled = False
    if task is not None and not task.done():
        task.cancel()
        cancelled = True

    return Response.json({"conversation_id": conversation_id, "cancelled": cancelled})


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
        return Response.html("Database not found", status=404)

    # Verify the table exists if specified
    if table_name:
        tables = await target_db.table_names()
        if table_name not in tables:
            return Response.html("Table not found", status=404)

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
        return Response.html("Report not found", status=404)
    if row["actor_id"] != actor_id:
        return Response.html("Forbidden", status=403)

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
