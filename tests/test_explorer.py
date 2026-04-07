import asyncio
import json

from datasette.app import Datasette
import pytest

from datasette_agent.schema import ensure_tables


# Echo model JSON that makes the agent call append_to_report then mark_finished.
# The echo model processes tool_calls from JSON prompts sequentially.
EXPLORER_GOAL_THAT_FINISHES = json.dumps(
    {
        "prompt": "Explore database",
        "tool_calls": [
            {
                "name": "append_to_report",
                "arguments": {"markdown_content": "## Findings\n\nFound 3 tables."},
            },
            {
                "name": "mark_finished",
                "arguments": {"final_message": "Exploration complete"},
            },
        ],
    }
)


@pytest.fixture
def datasette_instance():
    ds = Datasette(
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
    )
    # Add a test database with a table
    return ds


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


async def _setup_test_db(datasette_instance):
    """Create a test database with some data."""
    db = datasette_instance.add_memory_database("test_db")
    await db.execute_write("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    await db.execute_write("INSERT OR IGNORE INTO users VALUES (1, 'Alice', 30)")
    await db.execute_write("INSERT OR IGNORE INTO users VALUES (2, 'Bob', 25)")
    return db


# --- Schema tests ---


@pytest.mark.asyncio
async def test_explorer_reports_table_created(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    tables = await db.table_names()
    assert "datasette_agent_explorer_reports" in tables


# --- Explorer core logic tests ---


@pytest.mark.asyncio
async def test_start_explorer_creates_records(datasette_instance):
    from datasette_agent.explorer import start_explorer

    await _setup_test_db(datasette_instance)
    report_id, agent_id = await start_explorer(
        datasette=datasette_instance,
        actor={"id": "user"},
        database_name="test_db",
    )
    assert len(report_id) == 26
    assert len(agent_id) == 26

    db = datasette_instance.get_internal_database()
    report = (
        await db.execute(
            "SELECT * FROM datasette_agent_explorer_reports WHERE id = ?",
            [report_id],
        )
    ).first()
    assert report is not None
    assert report["database_name"] == "test_db"
    assert report["table_name"] is None
    assert report["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_start_explorer_with_table(datasette_instance):
    from datasette_agent.explorer import start_explorer

    await _setup_test_db(datasette_instance)
    report_id, agent_id = await start_explorer(
        datasette=datasette_instance,
        actor={"id": "user"},
        database_name="test_db",
        table_name="users",
    )

    db = datasette_instance.get_internal_database()
    report = (
        await db.execute(
            "SELECT * FROM datasette_agent_explorer_reports WHERE id = ?",
            [report_id],
        )
    ).first()
    assert report["table_name"] == "users"


@pytest.mark.asyncio
async def test_start_explorer_with_extra_prompt(datasette_instance):
    from datasette_agent.explorer import start_explorer

    await _setup_test_db(datasette_instance)
    report_id, agent_id = await start_explorer(
        datasette=datasette_instance,
        actor={"id": "user"},
        database_name="test_db",
        extra_prompt="Focus on age distributions",
    )

    db = datasette_instance.get_internal_database()
    report = (
        await db.execute(
            "SELECT * FROM datasette_agent_explorer_reports WHERE id = ?",
            [report_id],
        )
    ).first()
    assert report["extra_prompt"] == "Focus on age distributions"


# --- append_to_report tool tests ---


@pytest.mark.asyncio
async def test_append_to_report_tool(datasette_instance):
    from datasette_agent.explorer import _make_append_to_report_tool

    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    # Create a report record directly
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    report_id = "01TEST_REPORT_ID_AAAAAAAA"
    await db.execute_write(
        "INSERT INTO datasette_agent_explorer_reports "
        "(id, actor_id, database_name, content, created_at, updated_at) "
        "VALUES (?, 'user', 'test_db', '', ?, ?)",
        [report_id, now, now],
    )

    tool = _make_append_to_report_tool(datasette_instance, report_id)
    assert tool.name == "append_to_report"

    # Call the tool
    result = await tool.fn(
        datasette=datasette_instance,
        actor={"id": "user"},
        markdown_content="## Section 1\n\nSome findings.",
    )
    result_data = json.loads(result)
    assert result_data["status"] == "appended"

    # Check content was appended
    row = (
        await db.execute(
            "SELECT content FROM datasette_agent_explorer_reports WHERE id = ?",
            [report_id],
        )
    ).first()
    assert "## Section 1" in row["content"]

    # Append more
    await tool.fn(
        datasette=datasette_instance,
        actor={"id": "user"},
        markdown_content="## Section 2\n\nMore findings.",
    )
    row = (
        await db.execute(
            "SELECT content FROM datasette_agent_explorer_reports WHERE id = ?",
            [report_id],
        )
    ).first()
    assert "## Section 1" in row["content"]
    assert "## Section 2" in row["content"]


# --- Explorer page view tests ---


@pytest.mark.asyncio
async def test_explorer_page_database(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "Explore" in response.text
    assert "test_db" in response.text


@pytest.mark.asyncio
async def test_explorer_page_table(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db/users",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "users" in response.text


@pytest.mark.asyncio
async def test_explorer_page_nonexistent_database(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/explore/nonexistent",
        cookies=cookies,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_explorer_page_nonexistent_table(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db/nonexistent",
        cookies=cookies,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_explorer_page_requires_permission(datasette_instance):
    response = await datasette_instance.client.get("/-/agent/explore/test_db")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_explorer_page_shows_background_agent_error(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conversation_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    agent_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    report_id = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
    error = "No model_id provided and no default_model configured."

    await db.execute_write(
        "INSERT INTO datasette_agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, error, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "Explore test_db", "error", error, now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_explorer_reports "
        "(id, agent_id, actor_id, database_name, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, '', ?, ?)",
        [report_id, agent_id, "user", "test_db", now, now],
    )

    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )

    assert response.status_code == 200
    assert error in response.text
    # Listing page links to the report detail page, not directly to conversation
    assert f"/-/agent/explore/report/{report_id}" in response.text
    assert "No findings recorded yet" not in response.text


# --- API endpoint tests ---


@pytest.mark.asyncio
async def test_api_start_explorer(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.post(
        "/-/agent/api/explore",
        content=json.dumps({"database": "test_db"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert "report_id" in data
    assert "agent_id" in data


@pytest.mark.asyncio
async def test_api_start_explorer_with_table(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.post(
        "/-/agent/api/explore",
        content=json.dumps({"database": "test_db", "table": "users"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert "report_id" in data


@pytest.mark.asyncio
async def test_api_explorer_report_status(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    # Create an explorer
    resp = await datasette_instance.client.post(
        "/-/agent/api/explore",
        content=json.dumps({"database": "test_db"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    report_id = resp.json()["report_id"]

    # Get report status
    response = await datasette_instance.client.get(
        f"/-/agent/api/explore/{report_id}",
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["database_name"] == "test_db"
    assert "content" in data


# --- Action hooks tests ---


@pytest.mark.asyncio
async def test_database_actions_includes_explore(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    # Load the database page and check for the action link
    response = await datasette_instance.client.get(
        "/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "Explore with AI agent" in response.text


@pytest.mark.asyncio
async def test_table_actions_includes_explore(datasette_instance, cookies):
    await _setup_test_db(datasette_instance)
    response = await datasette_instance.client.get(
        "/test_db/users",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "Explore with AI agent" in response.text


# --- Report detail page tests ---


@pytest.mark.asyncio
async def test_report_detail_page_loads(datasette_instance, cookies):
    """The report detail page should render with smd.js for markdown."""
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conversation_id = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
    agent_id = "01ARZ3NDEKTSV4RRFFQ69G5FA2"
    report_id = "01ARZ3NDEKTSV4RRFFQ69G5FA3"

    await db.execute_write(
        "INSERT INTO datasette_agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "Explore test_db", "completed", now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_explorer_reports "
        "(id, agent_id, actor_id, database_name, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [report_id, agent_id, "user", "test_db", "## Findings\n\nSome data.", now, now],
    )

    response = await datasette_instance.client.get(
        f"/-/agent/explore/report/{report_id}",
        cookies=cookies,
    )
    assert response.status_code == 200
    # Should include smd.js for markdown rendering
    assert "smd.js" in response.text
    # Should contain the raw report content for JS to render
    assert "## Findings" in response.text


@pytest.mark.asyncio
async def test_report_detail_page_requires_permission(datasette_instance):
    response = await datasette_instance.client.get(
        "/-/agent/explore/report/01ARZ3NDEKTSV4RRFFQ69G5FA3"
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_report_detail_page_not_found(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/explore/report/01ARZ3NDEKTSV4RRFFQ69G5FA9",
        cookies=cookies,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_explorer_listing_links_to_report_page(datasette_instance, cookies):
    """The explorer listing page should link to report detail pages, not show inline content."""
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conversation_id = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
    agent_id = "01ARZ3NDEKTSV4RRFFQ69G5FB2"
    report_id = "01ARZ3NDEKTSV4RRFFQ69G5FB3"

    await db.execute_write(
        "INSERT INTO datasette_agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "Explore test_db", "completed", now, now],
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_explorer_reports "
        "(id, agent_id, actor_id, database_name, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [report_id, agent_id, "user", "test_db", "## Some content", now, now],
    )

    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    # Should link to the report detail page
    assert f"/-/agent/explore/report/{report_id}" in response.text
    # Should NOT contain the raw markdown content inline
    assert "## Some content" not in response.text
