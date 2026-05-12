import asyncio
import json

from datasette.app import Datasette
import pytest

from datasette_agent.schema import ensure_tables

# The echo model treats JSON prompts with "tool_calls" specially - it will
# invoke those tools. We use this to make the background agent call mark_finished.
GOAL_THAT_FINISHES = json.dumps(
    {
        "prompt": "Test goal",
        "tool_calls": [
            {
                "name": "mark_finished",
                "arguments": {"final_message": "All done", "error": None},
            }
        ],
    }
)

SIMPLE_GOAL = "List all tables in the database"

# Echo emits a list_databases_and_tables tool call AND mark_finished in the
# same response. The chain framework runs both tools; mark_finished sets the
# done flag so the outer loop exits. This exercises the case where an
# assistant message with tool_calls must be followed in the agent_messages
# table by the matching tool_result rows BEFORE any later assistant rows —
# otherwise the next turn rebuilds messages= in an order OpenAI rejects.
GOAL_TOOL_THEN_FINISH = json.dumps(
    {
        "prompt": "Test goal",
        "tool_calls": [
            {"name": "list_databases_and_tables", "arguments": {}},
            {
                "name": "mark_finished",
                "arguments": {"final_message": "done", "error": None},
            },
        ],
    }
)


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(
        memory=True,
        metadata={
            "plugins": {
                "datasette-llm": {
                    "default_model": "echo",
                }
            }
        },
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
            }
        },
        internal=str(tmp_path / "internal.db"),
    )


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


# --- Schema tests ---


@pytest.mark.asyncio
async def test_background_agents_table_created(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    tables = await db.table_names()
    assert "agent_background_agents" in tables
    assert "agent_pending_notifications" in tables


# --- Python API tests ---


@pytest.mark.asyncio
async def test_start_background_agent(datasette_instance):
    from datasette_agent.api import start_background_agent

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal="List all tables in the database",
    )
    assert isinstance(agent_id, str)
    assert len(agent_id) == 26  # ULID length


@pytest.mark.asyncio
async def test_get_background_agent_status(datasette_instance):
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal="Test goal",
    )
    status = await get_background_agent_status(datasette_instance, agent_id)
    assert status["id"] == agent_id
    assert status["goal"] == "Test goal"
    assert status["status"] in ("pending", "running", "completed", "error")
    assert "conversation_id" in status


@pytest.mark.asyncio
async def test_background_agent_creates_conversation(datasette_instance):
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal="Test goal",
    )
    status = await get_background_agent_status(datasette_instance, agent_id)
    db = datasette_instance.get_internal_database()
    row = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?",
            [status["conversation_id"]],
        )
    ).first()
    assert row is not None
    assert row["actor_id"] == "user"


@pytest.mark.asyncio
async def test_background_agent_writes_messages_to_db(datasette_instance):
    """Background agent should persist every step to the messages table."""
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal=GOAL_THAT_FINISHES,
    )
    # Wait for the agent to finish
    for _ in range(20):
        await asyncio.sleep(0.2)
        status = await get_background_agent_status(datasette_instance, agent_id)
        if status["status"] in ("completed", "error"):
            break

    db = datasette_instance.get_internal_database()
    messages = (
        await db.execute(
            "SELECT role FROM agent_messages WHERE conversation_id = ? ORDER BY id",
            [status["conversation_id"]],
        )
    ).rows
    # Should have at least one message (the initial user/system goal message)
    assert len(messages) >= 1


@pytest.mark.asyncio
async def test_tool_results_inserted_before_next_assistant(datasette_instance):
    """An assistant message with tool_calls must be followed by the matching
    tool_result message(s) in agent_messages BEFORE any subsequent assistant
    message. If they land out of order, OpenAI rejects the next turn with
    'tool_calls must be followed by tool messages'."""
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal=GOAL_TOOL_THEN_FINISH,
    )
    for _ in range(20):
        await asyncio.sleep(0.2)
        status = await get_background_agent_status(datasette_instance, agent_id)
        if status["status"] in ("completed", "error"):
            break

    db = datasette_instance.get_internal_database()
    rows = (
        await db.execute(
            "SELECT id, role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [status["conversation_id"]],
        )
    ).rows

    def parts(row):
        return json.loads(row["message_json"]).get("parts", [])

    def n_tool_calls(row):
        return sum(1 for p in parts(row) if p.get("type") == "tool_call")

    def n_tool_results(row):
        return sum(1 for p in parts(row) if p.get("type") == "tool_result")

    # Walk the rows. For every assistant row with N tool_calls, the very
    # next rows must be tool messages whose total tool_result count == N,
    # before any further assistant row.
    for i, row in enumerate(rows):
        if row["role"] != "assistant":
            continue
        n_calls = n_tool_calls(row)
        if n_calls == 0:
            continue
        results_seen = 0
        for follow in rows[i + 1 :]:
            if follow["role"] == "tool":
                results_seen += n_tool_results(follow)
            else:
                break
        assert results_seen == n_calls, (
            f"Assistant row id={row['id']} emitted {n_calls} tool_call(s); "
            f"expected {n_calls} tool_result(s) in immediately following rows "
            f"but found {results_seen}. Row order: "
            f"{[(r['id'], r['role']) for r in rows]}"
        )


@pytest.mark.asyncio
async def test_background_agent_completes(datasette_instance):
    """Agent should eventually reach completed status when mark_finished is called."""
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal=GOAL_THAT_FINISHES,
    )
    # Wait for agent to finish
    for _ in range(20):
        await asyncio.sleep(0.2)
        status = await get_background_agent_status(datasette_instance, agent_id)
        if status["status"] in ("completed", "error"):
            break
    assert status["status"] == "completed"
    assert status["final_message"] == "All done"


# --- Background tools for chat agent ---


@pytest.mark.asyncio
async def test_background_tools_registered(datasette_instance):
    from datasette_agent.tools import get_agent_tools

    tools = await get_agent_tools(datasette_instance)
    tool_names = {t.name for t in tools}
    assert "spawn_background_agent" in tool_names
    assert "check_background_agent" in tool_names


# --- Context var ---


@pytest.mark.asyncio
async def test_context_var_exists():
    from datasette_agent.context import current_conversation_id

    assert current_conversation_id.get() is None


# --- Pending notifications ---


@pytest.mark.asyncio
async def test_pending_notification_created_on_completion(datasette_instance):
    """When a background agent spawned by a chat completes, it should create a notification."""
    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    agent_id = await start_background_agent(
        datasette=datasette_instance,
        actor={"id": "user"},
        goal=GOAL_THAT_FINISHES,
        spawned_by_conversation_id="FAKE_CONVERSATION_ID_12345",
    )
    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(0.2)
        status = await get_background_agent_status(datasette_instance, agent_id)
        if status["status"] in ("completed", "error"):
            break

    notifications = (
        await db.execute(
            "SELECT * FROM agent_pending_notifications WHERE conversation_id = ?",
            ["FAKE_CONVERSATION_ID_12345"],
        )
    ).rows
    assert len(notifications) >= 1


# --- API endpoints ---


@pytest.mark.asyncio
async def test_api_create_background_agent(datasette_instance, cookies):
    response = await datasette_instance.client.post(
        "/-/agent/api/background",
        content=json.dumps({"goal": "Analyze the data"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    assert "conversation_id" in data


@pytest.mark.asyncio
async def test_api_background_agent_status(datasette_instance, cookies):
    # Create one first
    resp = await datasette_instance.client.post(
        "/-/agent/api/background",
        content=json.dumps({"goal": "Analyze the data"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    agent_id = resp.json()["agent_id"]

    response = await datasette_instance.client.get(
        f"/-/agent/api/background/{agent_id}",
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["goal"] == "Analyze the data"
    assert "status" in data


@pytest.mark.asyncio
async def test_background_index_page(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/background",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "Background" in response.text


@pytest.mark.asyncio
async def test_background_index_requires_permission(datasette_instance):
    response = await datasette_instance.client.get("/-/agent/background")
    assert response.status_code == 403


# --- Notification prefix in chat agent ---


@pytest.mark.asyncio
async def test_notification_prepended_to_user_message(datasette_instance, cookies):
    """When pending notifications exist, they should be prepended to the next chat message."""
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    # Create a conversation
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Insert a fake pending notification
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_pending_notifications (conversation_id, content, created_at) "
        "VALUES (?, ?, ?)",
        [conversation_id, "[Background agent ABC completed] Result: done", now],
    )

    # Send a message - the notification should be consumed
    await datasette_instance.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "What tables exist?"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )

    # Notification should be deleted
    remaining = (
        await db.execute(
            "SELECT * FROM agent_pending_notifications WHERE conversation_id = ?",
            [conversation_id],
        )
    ).rows
    assert len(remaining) == 0

    # The first user message saved should contain the notification prefix
    messages = (
        await db.execute(
            "SELECT message_json FROM agent_messages "
            "WHERE conversation_id = ? AND role = 'user' ORDER BY id",
            [conversation_id],
        )
    ).rows
    first = json.loads(messages[0]["message_json"])
    user_text = "".join(p["text"] for p in first["parts"] if p.get("type") == "text")
    assert "[Background agent ABC completed]" in user_text


# --- Reconcile orphaned running rows on startup ---


@pytest.mark.asyncio
async def test_reconcile_marks_running_rows_as_error(datasette_instance):
    """Rows left in pending/running by a previous process must be marked
    error on startup — otherwise the row sits as `running` forever and the
    explorer report page spins indefinitely."""
    from datasette_agent.api import reconcile_running_agents

    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    rows = [
        ("01ORPHAN_RUNNING_AAAAAAAAA0", "running"),
        ("01ORPHAN_PENDING_AAAAAAAAA0", "pending"),
        ("01DONE_COMPLETED_AAAAAAAAA0", "completed"),
        ("01DONE_ERROR_AAAAAAAAAAAAA0", "error"),
    ]
    for agent_id, status in rows:
        await db.execute_write(
            "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [f"conv_{agent_id}", "user", "x", now, now],
        )
        await db.execute_write(
            "INSERT INTO agent_background_agents "
            "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [agent_id, f"conv_{agent_id}", "user", "g", status, now, now],
        )

    await reconcile_running_agents(datasette_instance)

    statuses = {
        row["id"]: (row["status"], row["error"])
        for row in (
            await db.execute("SELECT id, status, error FROM agent_background_agents")
        ).rows
    }
    assert statuses["01ORPHAN_RUNNING_AAAAAAAAA0"][0] == "error"
    assert "restart" in (statuses["01ORPHAN_RUNNING_AAAAAAAAA0"][1] or "").lower()
    assert statuses["01ORPHAN_PENDING_AAAAAAAAA0"][0] == "error"
    assert "restart" in (statuses["01ORPHAN_PENDING_AAAAAAAAA0"][1] or "").lower()
    # Already-terminal rows are untouched
    assert statuses["01DONE_COMPLETED_AAAAAAAAA0"] == ("completed", None)
    assert statuses["01DONE_ERROR_AAAAAAAAAAAAA0"][0] == "error"


@pytest.mark.asyncio
async def test_reconcile_runs_on_datasette_startup(datasette_instance, cookies):
    """The reconcile pass must be wired into Datasette's startup hook so a
    process restart automatically cleans up orphans without the caller
    having to remember."""
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ["conv_startup_orphan", "user", "x", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            "01STARTUP_ORPHAN_AAAAAAAAA0",
            "conv_startup_orphan",
            "user",
            "g",
            "running",
            now,
            now,
        ],
    )

    # Hitting any URL triggers Datasette.invoke_startup() once.
    await datasette_instance.client.get("/-/agent/background", cookies=cookies)

    row = (
        await db.execute(
            "SELECT status, error FROM agent_background_agents WHERE id = ?",
            ["01STARTUP_ORPHAN_AAAAAAAAA0"],
        )
    ).first()
    assert row["status"] == "error"
    assert "restart" in (row["error"] or "").lower()
