import json

from datasette.app import Datasette
import pytest


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


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-agent" in installed_plugins


@pytest.mark.asyncio
async def test_system_prompt_documents_display_modes(datasette_instance):
    """The system prompt must steer the model toward the right `display`
    mode on sql_query — otherwise it'll default to `model` everywhere and
    the user never sees rendered tables."""
    from datasette_agent.agent import _build_system_prompt

    prompt = await _build_system_prompt(datasette_instance, {"id": "user"})
    lowered = prompt.lower()
    # All three modes must be named so the model knows the menu.
    assert "display" in lowered
    assert '"model"' in lowered or "`model`" in lowered
    assert '"both"' in lowered or "`both`" in lowered
    assert '"user"' in lowered or "`user`" in lowered
    # And the prompt should mention show-me / table rendering as the
    # trigger for picking a non-default mode.
    assert "show" in lowered or "render" in lowered or "table" in lowered


@pytest.mark.asyncio
async def test_system_prompt_warns_against_repeating_rendered_tables(
    datasette_instance,
):
    """When a sql_query result is rendered to the user via `both` or
    `user`, the model must not also paste the rows back as a markdown
    table — that's redundant noise. The prompt has to call this out
    explicitly, otherwise models default to summarizing what they just
    showed."""
    from datasette_agent.agent import _build_system_prompt

    prompt = await _build_system_prompt(datasette_instance, {"id": "user"})
    lowered = prompt.lower()
    # We don't pin exact wording — just that the prompt contains both
    # the "don't repeat" instruction and a reference to the rendered
    # table so the model can connect the two.
    assert "repeat" in lowered or "restate" in lowered or "duplicate" in lowered
    assert "table" in lowered


@pytest.mark.asyncio
async def test_agent_permission_denied(datasette_instance):
    response = await datasette_instance.client.get("/-/agent")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_agent_index(datasette_instance, cookies):
    response = await datasette_instance.client.get("/-/agent", cookies=cookies)
    assert response.status_code == 200
    assert "Agent" in response.text


@pytest.mark.asyncio
async def test_create_conversation(datasette_instance, cookies):
    response = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert "conversation_id" in data
    assert len(data["conversation_id"]) == 26


@pytest.mark.asyncio
async def test_conversation_page(datasette_instance, cookies):
    # Create a conversation first
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Load conversation page
    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert "Chat" in response.text


@pytest.mark.asyncio
async def test_conversation_page_shows_chat_form_for_user_chat(
    datasette_instance, cookies
):
    """A normal user-owned conversation must render the chat input + send
    button so the user can keep typing."""
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="chat-form"' in response.text
    assert 'id="message-input"' in response.text
    assert 'id="send-btn"' in response.text


@pytest.mark.asyncio
async def test_conversation_page_hides_chat_form_for_background_agent(
    datasette_instance, cookies
):
    """A conversation owned by a background agent must not render the chat
    form — the user can't type to an autonomous agent."""
    db = datasette_instance.get_internal_database()
    from datasette_agent.schema import ensure_tables

    await ensure_tables(db)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conversation_id = "01BGCONVAAAAAAAAAAAAAAAAAA"
    agent_id = "01BGAGENTAAAAAAAAAAAAAAAAA"
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", "Background: x", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, "user", "g", "running", now, now],
    )

    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="chat-form"' not in response.text
    assert 'id="message-input"' not in response.text
    assert 'id="send-btn"' not in response.text


async def _insert_background_conversation(
    datasette_instance,
    conversation_id,
    agent_id,
    status="running",
    actor_id="user",
    n_messages=0,
):
    from datasette_agent.schema import ensure_tables
    from datetime import datetime, timezone

    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    # Trigger Datasette startup (and our reconcile hook) first so the row
    # we're about to insert doesn't get flipped to error.
    await datasette_instance.invoke_startup()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, actor_id, "Background: x", now, now],
    )
    await db.execute_write(
        "INSERT INTO agent_background_agents "
        "(id, conversation_id, actor_id, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [agent_id, conversation_id, actor_id, "g", status, now, now],
    )
    for i in range(n_messages):
        await db.execute_write(
            "INSERT INTO agent_messages "
            "(conversation_id, role, message_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                conversation_id,
                "user" if i == 0 else "assistant",
                json.dumps(
                    {
                        "role": "user" if i == 0 else "assistant",
                        "parts": [{"type": "text", "text": f"m{i}"}],
                    }
                ),
                now,
            ],
        )


@pytest.mark.asyncio
async def test_conversation_poll_returns_status_and_count(datasette_instance, cookies):
    """The poll endpoint is the heartbeat the conversation page uses to
    decide when to reload — it must report current background-agent
    status and message count."""
    await _insert_background_conversation(
        datasette_instance,
        "01POLLCONVAAAAAAAAAAAAAAAA",
        "01POLLAGENTAAAAAAAAAAAAAAA",
        status="running",
        n_messages=3,
    )

    response = await datasette_instance.client.get(
        "/-/agent/01POLLCONVAAAAAAAAAAAAAAAA/poll",
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_status"] == "running"
    assert data["message_count"] == 3


@pytest.mark.asyncio
async def test_conversation_poll_user_chat_has_null_agent_status(
    datasette_instance, cookies
):
    """For a normal user chat (no linked background agent), agent_status
    is null so the client knows there's nothing to poll for."""
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}/poll",
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_status"] is None
    assert data["message_count"] >= 0


@pytest.mark.asyncio
async def test_conversation_poll_requires_permission(datasette_instance):
    await _insert_background_conversation(
        datasette_instance,
        "01POLLAUTHCONVAAAAAAAAAAAA",
        "01POLLAUTHAGENTAAAAAAAAAAA",
    )
    response = await datasette_instance.client.get(
        "/-/agent/01POLLAUTHCONVAAAAAAAAAAAA/poll"
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_conversation_poll_not_found(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/01POLLNOPECONVAAAAAAAAAAAA/poll",
        cookies=cookies,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_background_conversation_page_includes_poll_url(
    datasette_instance, cookies
):
    """The conversation page for a background agent must expose the poll
    URL as a data attribute so the client knows to live-refresh."""
    await _insert_background_conversation(
        datasette_instance,
        "01POLLPAGECONVAAAAAAAAAAAA",
        "01POLLPAGEAGENTAAAAAAAAAAA",
        status="running",
    )

    response = await datasette_instance.client.get(
        "/-/agent/01POLLPAGECONVAAAAAAAAAAAA",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "/-/agent/01POLLPAGECONVAAAAAAAAAAAA/poll" in response.text
    assert "data-poll-url=" in response.text


@pytest.mark.asyncio
async def test_user_conversation_page_does_not_include_poll_url(
    datasette_instance, cookies
):
    """User-chat conversation pages stream via SSE on send — they should
    NOT advertise a poll URL since there is no autonomous loop to track."""
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "data-poll-url=" not in response.text


@pytest.mark.asyncio
async def test_conversation_not_found(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/01234567890123456789012345", cookies=cookies
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_conversation_actor_scoping(datasette_instance, cookies):
    ds = datasette_instance

    # Create conversation as "user"
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Same user can access it
    response = await ds.client.get(f"/-/agent/{conversation_id}", cookies=cookies)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stream_endpoint(datasette_instance, cookies):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message to the stream endpoint
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "What databases are available?"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Parse SSE events
    events = _parse_sse(response.text)
    event_types = [e["event"] for e in events]

    # Should have at least some text chunks and a done event
    assert "done" in event_types


@pytest.mark.asyncio
async def test_stream_endpoint_headers(datasette_instance, cookies):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message to the stream endpoint
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    # Content-Encoding: none is needed for SSE streaming on Fly.io
    assert response.headers.get("content-encoding") == "none"
    assert response.headers.get("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_messages_persisted(datasette_instance, cookies):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "Hi there"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )

    # Check messages were saved (one row per MessageDict)
    db = ds.get_internal_database()
    messages = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    assert len(messages) >= 2  # At least user + assistant
    assert messages[0]["role"] == "user"
    first = json.loads(messages[0]["message_json"])
    user_text = "".join(p["text"] for p in first["parts"] if p.get("type") == "text")
    assert user_text == "Hi there"


@pytest.mark.asyncio
async def test_reasoning_streams_and_persists(tmp_path):
    """Reasoning tokens from the model should stream via SSE as
    'reasoning_chunk' events and persist as role='reasoning' rows."""
    ds = Datasette(
        memory=True,
        metadata={
            "plugins": {
                "datasette-llm": {
                    "default_model": {
                        "model": "echo",
                        "options": {"thinking": True},
                    }
                }
            }
        },
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}

    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hi"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "Explain it"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200

    events = _parse_sse(response.text)
    reasoning = [e for e in events if e["event"] == "reasoning_chunk"]
    assert reasoning, "expected at least one reasoning_chunk SSE event"
    # echo with thinking=True emits two reasoning chunks + one text chunk
    joined = "".join(e["data"]["content"] for e in reasoning)
    assert "First I consider" in joined
    assert "then I decide" in joined

    db = ds.get_internal_database()
    rows = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    # Reasoning lives as a ReasoningPart inside an assistant MessageDict
    reasoning_text = ""
    for row in rows:
        if row["role"] != "assistant":
            continue
        msg = json.loads(row["message_json"])
        for part in msg.get("parts", []):
            if part.get("type") == "reasoning":
                reasoning_text += part.get("text", "")
    assert "First I consider" in reasoning_text
    assert "then I decide" in reasoning_text


@pytest.mark.asyncio
async def test_conversation_title_auto_set(datasette_instance, cookies):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "What tables exist?"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )

    # Check title was set
    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT title FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    assert row["title"] == "What tables exist?"


@pytest.mark.asyncio
async def test_agent_index_shows_conversations(datasette_instance, cookies):
    ds = datasette_instance

    # Create a conversation and send a message to set title
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "Test conversation"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )

    # Check index page
    response = await ds.client.get("/-/agent", cookies=cookies)
    assert response.status_code == 200
    assert "Test conversation" in response.text


@pytest.mark.asyncio
async def test_default_tools_registered(datasette_instance):
    from datasette_agent.tools import get_agent_tools

    tools = await get_agent_tools(datasette_instance)
    tool_names = {t.name for t in tools}
    assert "list_databases_and_tables" in tool_names
    assert "describe_table" in tool_names
    assert "sql_query" in tool_names


@pytest.mark.asyncio
async def test_describe_table_handles_foreign_keys(datasette_instance):
    from datasette_agent.sql_tools import _describe_table

    db = datasette_instance.add_memory_database("describe_fk_test_db")
    await db.execute_write("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    await db.execute_write(
        "CREATE TABLE repos (id INTEGER PRIMARY KEY, owner INTEGER REFERENCES users(id), name TEXT)"
    )
    await datasette_instance.client.get("/-/plugins.json")

    result = json.loads(
        await _describe_table(
            datasette_instance,
            {"id": "user"},
            "describe_fk_test_db",
            "repos",
        )
    )

    assert result["foreign_keys"] == [
        {"column": "owner", "other_table": "users", "other_column": "id"}
    ]


@pytest.mark.asyncio
async def test_describe_table_tool(tmp_path):
    import sqlite3

    from datasette_agent.sql_tools import _describe_table

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id))"
    )
    conn.close()

    ds = Datasette([db_path])
    await ds.invoke_startup()
    result = json.loads(await _describe_table(ds, {"id": "test"}, "test", "books"))
    assert result["table"] == "books"
    assert any(c["name"] == "title" for c in result["columns"])
    assert len(result["foreign_keys"]) == 1
    assert result["foreign_keys"][0]["column"] == "author_id"
    assert result["foreign_keys"][0]["other_table"] == "authors"

    bad_db = json.loads(await _describe_table(ds, {"id": "test"}, "nope", "books"))
    assert bad_db == {
        "error": "Database 'nope' not found",
        "available_databases": ["test"],
    }

    bad_table = json.loads(await _describe_table(ds, {"id": "test"}, "test", "nope"))
    assert bad_table["error"] == "Table 'nope' not found in database 'test'"
    assert set(bad_table["available_tables"]) == {"authors", "books"}


@pytest.mark.asyncio
async def test_list_databases_and_tables_shape(tmp_path):
    import sqlite3

    from datasette_agent.sql_tools import _list_databases_and_tables

    db_path = str(tmp_path / "alpha.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE one (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE two (id INTEGER PRIMARY KEY)")
    conn.close()

    ds = Datasette([db_path])
    await ds.invoke_startup()
    result = json.loads(await _list_databases_and_tables(ds, {"id": "test"}))
    assert result == {
        "databases": [{"database_name": "alpha", "table_names": ["one", "two"]}]
    }


@pytest.mark.asyncio
async def test_sql_query_unknown_database(tmp_path):
    import sqlite3

    from datasette_agent.sql_tools import _sql_query

    db_path = str(tmp_path / "test.db")
    sqlite3.connect(db_path).close()

    ds = Datasette([db_path])
    await ds.invoke_startup()
    result = json.loads(await _sql_query(ds, {"id": "test"}, "nope", "select 1"))
    assert result == {
        "error": "Database 'nope' not found",
        "available_databases": ["test"],
    }


def test_agent_tools_command():
    from click.testing import CliRunner
    from datasette.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "tools"])
    assert result.exit_code == 0
    # Should list the default tools with descriptions
    assert "list_databases_and_tables" in result.output
    assert "describe_table" in result.output
    assert "sql_query" in result.output
    # Should show plugin grouping
    assert "agent:" in result.output


def test_agent_tools_command_descriptions():
    from click.testing import CliRunner
    from datasette.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "tools"])
    assert result.exit_code == 0
    # Should include tool descriptions
    assert "Execute a read-only SQL query" in result.output


def test_agent_tools_command_json():
    from click.testing import CliRunner
    from datasette.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "tools", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    tool_names = {t["name"] for t in data}
    assert "list_databases_and_tables" in tool_names
    assert "describe_table" in tool_names
    assert "sql_query" in tool_names
    # Each tool should have name, description, input_schema, plugin
    for tool in data:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert "plugin" in tool
    # Check plugin name
    assert all(t["plugin"] == "agent" for t in data)


def test_chat_command_with_prompt(tmp_path):
    import sqlite3

    from click.testing import CliRunner
    from datasette.cli import cli

    # Create a test database
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dogs (name TEXT, age INTEGER)")
    conn.execute("INSERT INTO dogs VALUES ('Cleo', 5)")
    conn.close()

    runner = CliRunner()
    # -p sends one prompt, then we send empty input to exit the loop
    result = runner.invoke(cli, ["agent", "chat", db_path, "-m", "echo", "-p", "hello"])
    assert result.exit_code == 0
    # echo model echoes back the prompt, so "hello" should appear in output
    assert "hello" in result.output


def test_chat_command_interactive(tmp_path):
    from click.testing import CliRunner
    from datasette.cli import cli

    runner = CliRunner()
    # Type a message then empty line to quit
    result = runner.invoke(
        cli, ["agent", "chat", ":memory:", "-m", "echo"], input="hi there\n\n"
    )
    assert result.exit_code == 0
    # echo model echoes the input
    assert "hi there" in result.output


def test_chat_command_shows_tool_calls(tmp_path):
    import sqlite3

    from click.testing import CliRunner
    from datasette.cli import cli

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dogs (name TEXT)")
    conn.close()

    runner = CliRunner()
    result = runner.invoke(
        cli, ["agent", "chat", db_path, "-m", "echo", "-p", "list tables"]
    )
    assert result.exit_code == 0


def _parse_sse(text):
    """Parse SSE text into a list of {event, data} dicts."""
    events = []
    current_event = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: ") and current_event:
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append({"event": current_event, "data": data})
            current_event = None
    return events
