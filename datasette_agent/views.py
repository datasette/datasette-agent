import json
from datetime import datetime, timezone

from datasette.utils.asgi import AsgiStream, Response
from ulid import ULID

from .agent import run_agent
from .schema import ensure_tables


def _actor_id(request):
    if request.actor:
        return request.actor.get("id")
    return None


async def agent_index(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    actor_id = _actor_id(request)
    if actor_id:
        conversations = (
            await db.execute(
                "SELECT id, title, created_at, updated_at FROM datasette_agent_conversations "
                "WHERE actor_id = ? ORDER BY updated_at DESC",
                [actor_id],
            )
        ).rows
    else:
        conversations = (
            await db.execute(
                "SELECT id, title, created_at, updated_at FROM datasette_agent_conversations "
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
        "INSERT INTO datasette_agent_conversations (id, actor_id, title, created_at, updated_at) "
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
            "SELECT * FROM datasette_agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    if row is None:
        return Response.html("Conversation not found", status=404)
    if row["actor_id"] != actor_id:
        return Response.html("Forbidden", status=403)

    messages = (
        await db.execute(
            "SELECT * FROM datasette_agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows

    return Response.html(
        await datasette.render_template(
            "agent_conversation.html",
            {
                "conversation": dict(row),
                "messages": [dict(m) for m in messages],
                "conversation_id": conversation_id,
            },
            request=request,
        )
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
            "SELECT * FROM datasette_agent_conversations WHERE id = ?",
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
        headers={"Cache-Control": "no-cache"},
        content_type="text/event-stream",
    )


async def agent_background_index(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    db = datasette.get_internal_database()
    await ensure_tables(db)
    actor_id = _actor_id(request)

    if actor_id:
        agents = (
            await db.execute(
                "SELECT * FROM datasette_agent_background_agents "
                "WHERE actor_id = ? ORDER BY created_at DESC",
                [actor_id],
            )
        ).rows
    else:
        agents = (
            await db.execute(
                "SELECT * FROM datasette_agent_background_agents "
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
    db = datasette.get_internal_database()
    await ensure_tables(db)

    agent_id = request.url_vars["agent_id"]
    actor_id = _actor_id(request)

    row = (
        await db.execute(
            "SELECT * FROM datasette_agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return Response.json({"error": "Not found"}, status=404)
    if row["actor_id"] != actor_id:
        return Response.json({"error": "Forbidden"}, status=403)

    return Response.json(dict(row))
