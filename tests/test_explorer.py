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
def datasette_instance(tmp_path):
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
                "datasette-agent-explore": {"id": "user"},
            }
        },
        internal=str(tmp_path / "internal.db"),
    )
    # Add a test database with a table
    return ds


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


async def _setup_test_db(datasette_instance):
    """Create a test database with some data."""
    db = datasette_instance.add_memory_database("test_db")
    await db.execute_write(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)"
    )
    await db.execute_write("INSERT OR IGNORE INTO users VALUES (1, 'Alice', 30)")
    await db.execute_write("INSERT OR IGNORE INTO users VALUES (2, 'Bob', 25)")
    return db


# --- Schema tests ---


@pytest.mark.asyncio
async def test_explorer_reports_table_created(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    tables = await db.table_names()
    assert "agent_explorer_reports" in tables


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
            "SELECT * FROM agent_explorer_reports WHERE id = ?",
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
            "SELECT * FROM agent_explorer_reports WHERE id = ?",
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
            "SELECT * FROM agent_explorer_reports WHERE id = ?",
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
        "INSERT INTO agent_explorer_reports "
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
            "SELECT content FROM agent_explorer_reports WHERE id = ?",
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
            "SELECT content FROM agent_explorer_reports WHERE id = ?",
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
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, error, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            agent_id,
            conversation_id,
            "user",
            "Explore test_db",
            "error",
            error,
            now,
            now,
        ],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports "
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
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "Explore test_db", "completed", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports "
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
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: test", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "Explore test_db", "completed", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports "
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


@pytest.mark.asyncio
async def test_database_explorer_includes_table_reports(datasette_instance, cookies):
    """The database-level explorer page should include reports for specific tables too."""
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    # Create a database-level report
    db_conv_id = "01ARZ3NDEKTSV4RRFFQ69G5FC1"
    db_agent_id = "01ARZ3NDEKTSV4RRFFQ69G5FC2"
    db_report_id = "01ARZ3NDEKTSV4RRFFQ69G5FC3"
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [db_conv_id, "user", "Background: db explore", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents (id, conversation_id, actor_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [db_agent_id, db_conv_id, "user", "Explore test_db", "completed", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports (id, agent_id, actor_id, database_name, table_name, content, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        [db_report_id, db_agent_id, "user", "test_db", "DB findings", now, now],
    )

    # Create a table-level report
    tbl_conv_id = "01ARZ3NDEKTSV4RRFFQ69G5FC4"
    tbl_agent_id = "01ARZ3NDEKTSV4RRFFQ69G5FC5"
    tbl_report_id = "01ARZ3NDEKTSV4RRFFQ69G5FC6"
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        [tbl_conv_id, "user", "Background: table explore", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents (id, conversation_id, actor_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            tbl_agent_id,
            tbl_conv_id,
            "user",
            "Explore users table",
            "completed",
            now,
            now,
        ],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports (id, agent_id, actor_id, database_name, table_name, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            tbl_report_id,
            tbl_agent_id,
            "user",
            "test_db",
            "users",
            "Table findings",
            now,
            now,
        ],
    )

    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    # Both reports should appear
    assert f"/-/agent/explore/report/{db_report_id}" in response.text
    assert f"/-/agent/explore/report/{tbl_report_id}" in response.text
    # Table-scoped report should show table name
    assert "users" in response.text


# --- Live refresh of the explorer listing page ---


async def _insert_report(
    db,
    conversation_id,
    agent_id,
    report_id,
    status,
    *,
    final_message=None,
    error=None,
    content="",
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: x", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, final_message, error, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            agent_id,
            conversation_id,
            "user",
            "Explore test_db",
            status,
            final_message,
            error,
            now,
            now,
        ],
    )
    await db.execute_write(
        "INSERT INTO agent_explorer_reports "
        "(id, agent_id, actor_id, database_name, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [report_id, agent_id, "user", "test_db", content, now, now],
    )


@pytest.mark.asyncio
async def test_explorer_listing_marks_running_rows_for_polling(
    datasette_instance, cookies
):
    """A running report on the listing page must be tagged so the page can
    target it for live refresh."""
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    await _insert_report(
        db,
        "01ARZ3NDEKTSV4RRFFQ69POLL1",
        "01ARZ3NDEKTSV4RRFFQ69POLL2",
        "01ARZ3NDEKTSV4RRFFQ69POLL3",
        "running",
    )

    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    # The running row's <div> carries a data attribute reflecting its live
    # status so JS can locate it without re-templating.
    assert '<div class="explorer-report"' in response.text
    assert 'data-agent-status="running"' in response.text
    # The page polls the per-report status endpoint
    assert "/-/agent/api/explore/" in response.text


@pytest.mark.asyncio
async def test_explorer_listing_does_not_poll_terminal_rows(
    datasette_instance, cookies
):
    """Completed and errored reports should not be marked for polling; the
    listing JS should only target running/pending."""
    await _setup_test_db(datasette_instance)
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)

    await _insert_report(
        db,
        "01ARZ3NDEKTSV4RRFFQ69DONE01",
        "01ARZ3NDEKTSV4RRFFQ69DONE02",
        "01ARZ3NDEKTSV4RRFFQ69DONE03",
        "completed",
        final_message="all good",
    )

    response = await datasette_instance.client.get(
        "/-/agent/explore/test_db",
        cookies=cookies,
    )
    assert response.status_code == 200
    # The actual report <div> must reflect the terminal status; no row
    # marked running/pending means the polling loop has nothing to do.
    import re

    div_attrs = re.findall(r'<div class="explorer-report"[^>]*>', response.text)
    assert div_attrs
    for attrs in div_attrs:
        assert 'data-agent-status="completed"' in attrs
        assert 'data-agent-status="running"' not in attrs
        assert 'data-agent-status="pending"' not in attrs


# --- Independent permission for explorer ---


def _chat_only(tmp_path):
    """Datasette where the actor has datasette-agent but NOT
    datasette-agent-explore. Used to check the explorer routes are now
    independently gated."""
    return Datasette(
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
                # Note: no datasette-agent-explore grant.
            }
        },
        internal=str(tmp_path / "internal.db"),
    )


def _explore_only(tmp_path):
    """Inverse: has explorer permission but not chat."""
    return Datasette(
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {
                "datasette-agent-explore": {"id": "user"},
            }
        },
        internal=str(tmp_path / "internal.db"),
    )


@pytest.mark.asyncio
async def test_explorer_routes_require_explore_permission(tmp_path):
    """A user with only datasette-agent (no -explore) must get 403 from
    every explorer route — listing page, report detail, start API,
    status API."""
    ds = _chat_only(tmp_path)
    await _setup_test_db(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    r1 = await ds.client.get("/-/agent/explore/test_db", cookies=cookies)
    assert r1.status_code == 403

    r2 = await ds.client.get(
        "/-/agent/explore/report/01ARZ3NDEKTSV4RRFFQ69G5FAV",
        cookies=cookies,
    )
    assert r2.status_code == 403

    r3 = await ds.client.post(
        "/-/agent/api/explore",
        content=json.dumps({"database": "test_db"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert r3.status_code == 403

    r4 = await ds.client.get(
        "/-/agent/api/explore/01ARZ3NDEKTSV4RRFFQ69G5FAV",
        cookies=cookies,
    )
    assert r4.status_code == 403


@pytest.mark.asyncio
async def test_database_actions_hidden_without_explore_permission(tmp_path):
    """The 'Explore with AI agent' database action menu item must only
    appear when the actor holds datasette-agent-explore."""
    ds = _chat_only(tmp_path)
    await _setup_test_db(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    response = await ds.client.get("/test_db", cookies=cookies)
    assert response.status_code == 200
    assert "Explore with AI agent" not in response.text


@pytest.mark.asyncio
async def test_table_actions_hidden_without_explore_permission(tmp_path):
    """Same for the per-table action menu — only datasette-agent-explore
    surfaces the Explore link."""
    ds = _chat_only(tmp_path)
    await _setup_test_db(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    response = await ds.client.get("/test_db/users", cookies=cookies)
    assert response.status_code == 200
    assert "Explore with AI agent" not in response.text


@pytest.mark.asyncio
async def test_chat_only_user_can_still_use_chat(tmp_path):
    """The two permissions are independent: a user with chat-only access
    keeps full access to /-/agent even though explorer routes are now
    locked down."""
    ds = _chat_only(tmp_path)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    chat = await ds.client.get("/-/agent", cookies=cookies)
    assert chat.status_code == 200

    explore = await ds.client.get("/-/agent/explore/test_db", cookies=cookies)
    assert explore.status_code == 403


@pytest.mark.asyncio
async def test_explore_only_user_can_use_explorer(tmp_path):
    """Inverse of the above: a user with only datasette-agent-explore can
    hit the explorer routes but is locked out of /-/agent."""
    ds = _explore_only(tmp_path)
    await _setup_test_db(ds)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    explore = await ds.client.get("/-/agent/explore/test_db", cookies=cookies)
    assert explore.status_code == 200

    chat = await ds.client.get("/-/agent", cookies=cookies)
    assert chat.status_code == 403
