import asyncio
from datetime import datetime, timezone

from ulid import ULID

from .background_agent import run_background_agent
from .schema import ensure_tables
from .tools import get_agent_tools


async def start_background_agent(
    datasette,
    actor,
    goal,
    tools=None,
    spawned_by_conversation_id=None,
    identity_id=None,
    launched_by=None,
):
    """Start a background agent. Returns the agent_id (ULID).

    Args:
        datasette: The Datasette instance
        actor: The actor dict (e.g. {"id": "user"}). For run-as-agent this is
            the agent identity actor; tool calls are then authorized against
            the agent's grants (least privilege).
        goal: The goal prompt for the agent
        tools: Optional list of AgentTool instances. If None, uses default tools.
        spawned_by_conversation_id: If spawned from a chat, the conversation ID to notify on completion.
        identity_id: Optional agent_identities.id this run acts as (provenance).
        launched_by: Optional actor id of the human who launched the run
            (accountability — recorded even though the agent acts as itself).
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    agent_id = str(ULID())
    conversation_id = str(ULID())
    actor_id = str(actor["id"]) if actor and actor.get("id") is not None else None
    now = datetime.now(timezone.utc).isoformat()

    # Create conversation record for message logging
    await db.execute_write(
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, identity_id, launched_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            conversation_id,
            actor_id,
            f"Background: {goal[:100]}",
            identity_id,
            launched_by,
            now,
            now,
        ],
    )

    # Create background agent record
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, spawned_by_conversation_id, "
        "identity_id, launched_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        [
            agent_id,
            conversation_id,
            actor_id,
            goal,
            spawned_by_conversation_id,
            identity_id,
            launched_by,
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


async def start_background_agent_as_identity(
    datasette, identity_id, goal, *, launched_by, tools=None, spawned_by_conversation_id=None
):
    """Start a background agent running under an agent *identity* actor.

    The job executes as ``agent:<id>`` so every tool call evaluates
    ``datasette.allowed(..., actor=agent_actor)`` against only the agent's
    grants (least privilege), and edits are attributed to the agent. The human
    who launched it is recorded in ``launched_by`` for accountability.

    Raises ValueError if the identity is missing or disabled.
    """
    from .identities import load_identity_actor

    identity_actor = await load_identity_actor(datasette, identity_id)
    if identity_actor is None:
        raise ValueError(f"Unknown or disabled agent identity: {identity_id}")
    return await start_background_agent(
        datasette,
        actor=identity_actor,
        goal=goal,
        tools=tools,
        spawned_by_conversation_id=spawned_by_conversation_id,
        identity_id=identity_id,
        launched_by=launched_by,
    )


async def reconcile_running_agents(datasette):
    """Mark any agent rows left in pending/running as error.

    Background agent tasks live only in process memory (in
    datasette._background_agent_tasks), so a Datasette restart abandons
    every in-flight run. Without this pass, those rows stay marked
    running forever and the explorer report page spins indefinitely.
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE agent_background_agents "
        "SET status = 'error', error = ?, updated_at = ? "
        "WHERE status IN ('pending', 'running')",
        ["Interrupted by Datasette restart", now],
    )


async def get_background_agent_status(datasette, agent_id):
    """Get the status of a background agent.

    Returns a dict with: id, status, goal, final_message, error, conversation_id, created_at, updated_at
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    row = (
        await db.execute(
            "SELECT * FROM agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return None
    return dict(row)
