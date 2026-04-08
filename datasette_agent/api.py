import asyncio
from datetime import datetime, timezone

from ulid import ULID

from .background_agent import run_background_agent
from .schema import ensure_tables
from .tools import get_agent_tools


async def start_background_agent(
    datasette, actor, goal, tools=None, spawned_by_conversation_id=None
):
    """Start a background agent. Returns the agent_id (ULID).

    Args:
        datasette: The Datasette instance
        actor: The actor dict (e.g. {"id": "user"})
        goal: The goal prompt for the agent
        tools: Optional list of AgentTool instances. If None, uses default tools.
        spawned_by_conversation_id: If spawned from a chat, the conversation ID to notify on completion.
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    agent_id = str(ULID())
    conversation_id = str(ULID())
    actor_id = actor.get("id") if actor else None
    now = datetime.now(timezone.utc).isoformat()

    # Create conversation record for message logging
    await db.execute_write(
        "INSERT INTO datasette_agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, actor_id, f"Background: {goal[:100]}", now, now],
    )

    # Create background agent record
    await db.execute_write(
        "INSERT INTO datasette_agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, spawned_by_conversation_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
        [
            agent_id,
            conversation_id,
            actor_id,
            goal,
            spawned_by_conversation_id,
            now,
            now,
        ],
    )

    # Resolve tools if not provided
    if tools is None:
        tools = await get_agent_tools(datasette)

    # Launch as asyncio task
    task = asyncio.get_event_loop().create_task(
        run_background_agent(datasette, actor, agent_id, tools=tools)
    )

    # Store task reference on datasette instance
    if not hasattr(datasette, "_background_agent_tasks"):
        datasette._background_agent_tasks = {}
    datasette._background_agent_tasks[agent_id] = task

    def _cleanup(t):
        if hasattr(datasette, "_background_agent_tasks"):
            datasette._background_agent_tasks.pop(agent_id, None)

    task.add_done_callback(_cleanup)

    return agent_id


async def get_background_agent_status(datasette, agent_id):
    """Get the status of a background agent.

    Returns a dict with: id, status, goal, final_message, error, conversation_id, created_at, updated_at
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    row = (
        await db.execute(
            "SELECT * FROM datasette_agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return None
    return dict(row)
