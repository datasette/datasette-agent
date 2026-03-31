import json

from datasette.app import Datasette
import pytest


@pytest.fixture
def datasette_instance():
    return Datasette(
        memory=True,
        metadata={
            "plugins": {
                "datasette-llm": {
                    "default_model": "echo",
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-agent" in installed_plugins


@pytest.mark.asyncio
async def test_agent_index(datasette_instance):
    response = await datasette_instance.client.get("/-/agent")
    assert response.status_code == 200
    assert "Agent" in response.text


@pytest.mark.asyncio
async def test_create_conversation(datasette_instance):
    response = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "conversation_id" in data
    assert len(data["conversation_id"]) == 26


@pytest.mark.asyncio
async def test_conversation_page(datasette_instance):
    # Create a conversation first
    resp = await datasette_instance.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]

    # Load conversation page
    response = await datasette_instance.client.get(f"/-/agent/{conversation_id}")
    assert response.status_code == 200
    assert "Chat" in response.text


@pytest.mark.asyncio
async def test_conversation_not_found(datasette_instance):
    response = await datasette_instance.client.get("/-/agent/01234567890123456789012345")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_conversation_actor_scoping(datasette_instance):
    ds = datasette_instance

    # Create conversation as anonymous user
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]

    # Anonymous user can access it
    response = await ds.client.get(f"/-/agent/{conversation_id}")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stream_endpoint(datasette_instance):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message to the stream endpoint
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "What databases are available?"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    # Parse SSE events
    events = _parse_sse(response.text)
    event_types = [e["event"] for e in events]

    # Should have at least some text chunks and a done event
    assert "done" in event_types


@pytest.mark.asyncio
async def test_messages_persisted(datasette_instance):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "Hi there"}),
        headers={"Content-Type": "application/json"},
    )

    # Check messages were saved
    db = ds.get_internal_database()
    messages = (
        await db.execute(
            "SELECT role, content FROM datasette_agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    assert len(messages) >= 2  # At least user + assistant
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hi there"


@pytest.mark.asyncio
async def test_conversation_title_auto_set(datasette_instance):
    ds = datasette_instance

    # Create conversation
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]

    # Send a message
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "What tables exist?"}),
        headers={"Content-Type": "application/json"},
    )

    # Check title was set
    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT title FROM datasette_agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    assert row["title"] == "What tables exist?"


@pytest.mark.asyncio
async def test_agent_index_shows_conversations(datasette_instance):
    ds = datasette_instance

    # Create a conversation and send a message to set title
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    conversation_id = resp.json()["conversation_id"]
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "Test conversation"}),
        headers={"Content-Type": "application/json"},
    )

    # Check index page
    response = await ds.client.get("/-/agent")
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
