import asyncio
import json
from datetime import datetime, timezone

from datasette.app import Datasette
import pytest


def _parse_sse(message):
    event = None
    data = None
    for line in message.splitlines():
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


class Writer:
    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_browser_client_tool_runner_waits_for_posted_result():
    from datasette_agent.client_tools import (
        BrowserClientToolRunner,
        resolve_client_tool_call,
    )
    from datasette_agent.tools import AgentClientTool

    writer = Writer()
    tool = AgentClientTool(
        name="browser_echo",
        description="Echo in the browser",
        input_schema={"type": "object", "properties": {}},
        module_url="/echo.js",
        timeout=1,
    )
    task = asyncio.create_task(
        BrowserClientToolRunner(writer, "01CLIENTTOOLAAAAAAAAAAAAA", {"id": "user"}).run(
            tool, {"text": "hello"}
        )
    )

    for _ in range(20):
        if writer.messages:
            break
        await asyncio.sleep(0.01)

    assert len(writer.messages) == 1
    event, data = _parse_sse(writer.messages[0])
    assert event == "client_tool_call"
    assert data["name"] == "browser_echo"
    assert data["arguments"] == {"text": "hello"}
    assert data["result_url"].endswith(f"/client-tool-result/{data['id']}")

    resolved = resolve_client_tool_call(
        "01CLIENTTOOLAAAAAAAAAAAAA",
        data["id"],
        {"id": "user"},
        {"nonce": data["nonce"], "ok": True, "result": {"answer": 42}},
    )
    assert resolved is True
    assert json.loads(await task) == {"answer": 42}


@pytest.mark.asyncio
async def test_browser_client_tool_runner_returns_error_on_timeout():
    from datasette_agent.client_tools import BrowserClientToolRunner
    from datasette_agent.tools import AgentClientTool

    writer = Writer()
    tool = AgentClientTool(
        name="slow_browser_tool",
        description="Slow browser tool",
        input_schema={"type": "object", "properties": {}},
        module_url="/slow.js",
        timeout=0.01,
    )
    output = await BrowserClientToolRunner(
        writer, "01CLIENTTOOLBBBBBBBBBBBBB", {"id": "user"}
    ).run(tool, {})
    assert "Timed out waiting for browser tool slow_browser_tool" in json.loads(output)[
        "error"
    ]


@pytest.mark.asyncio
async def test_resolve_client_tool_call_rejects_wrong_actor():
    from datasette_agent.client_tools import (
        BrowserClientToolRunner,
        resolve_client_tool_call,
    )
    from datasette_agent.tools import AgentClientTool

    writer = Writer()
    tool = AgentClientTool(
        name="browser_echo",
        description="Echo in the browser",
        input_schema={"type": "object", "properties": {}},
        module_url="/echo.js",
        timeout=1,
    )
    task = asyncio.create_task(
        BrowserClientToolRunner(writer, "01CLIENTTOOLCCCCCCCCCCCCC", {"id": "alice"}).run(
            tool, {}
        )
    )
    for _ in range(20):
        if writer.messages:
            break
        await asyncio.sleep(0.01)
    _, data = _parse_sse(writer.messages[0])

    with pytest.raises(PermissionError):
        resolve_client_tool_call(
            "01CLIENTTOOLCCCCCCCCCCCCC",
            data["id"],
            {"id": "bob"},
            {"nonce": data["nonce"], "ok": True, "result": {}},
        )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_local_file_client_tools_registered():
    from datasette_agent.tools import get_agent_client_tools

    datasette = Datasette(memory=True)
    tools = await get_agent_client_tools(datasette)
    by_name = {tool.name: tool for tool in tools}
    assert {
        "local_files_status",
        "local_files_list",
        "local_files_read_text",
        "local_files_search",
    }.issubset(by_name)
    assert by_name["local_files_read_text"].module_url.endswith("/local-files.js")
    assert by_name["local_files_read_text"].timeout == 120.0


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


async def _create_conversation(datasette):
    from datasette_agent.schema import ensure_tables

    db = datasette.get_internal_database()
    await ensure_tables(db)
    conversation_id = "01CLIENTCHATCONVAAAAAAAAAA"
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, "user", None, now, now],
    )
    return conversation_id


@pytest.mark.asyncio
async def test_conversation_page_loads_local_files_module(datasette_instance, cookies):
    conversation_id = await _create_conversation(datasette_instance)
    response = await datasette_instance.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert "/-/static-plugins/datasette-agent/local-files.js" in response.text


@pytest.mark.asyncio
async def test_client_tool_result_route_resolves_pending_call(
    datasette_instance, cookies
):
    from datasette_agent.client_tools import BrowserClientToolRunner
    from datasette_agent.tools import AgentClientTool

    conversation_id = await _create_conversation(datasette_instance)
    writer = Writer()
    tool = AgentClientTool(
        name="browser_echo",
        description="Echo in the browser",
        input_schema={"type": "object", "properties": {}},
        module_url="/echo.js",
        timeout=1,
    )
    task = asyncio.create_task(
        BrowserClientToolRunner(writer, conversation_id, {"id": "user"}).run(
            tool, {"text": "hi"}
        )
    )
    for _ in range(20):
        if writer.messages:
            break
        await asyncio.sleep(0.01)
    _, data = _parse_sse(writer.messages[0])

    response = await datasette_instance.client.post(
        f"/-/agent/{conversation_id}/client-tool-result/{data['id']}",
        content=json.dumps(
            {"nonce": data["nonce"], "ok": True, "result": {"text": "hi"}}
        ),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert json.loads(await task) == {"text": "hi"}


@pytest.mark.asyncio
async def test_client_tool_result_route_ignores_late_or_duplicate_result(
    datasette_instance, cookies
):
    conversation_id = await _create_conversation(datasette_instance)

    response = await datasette_instance.client.post(
        f"/-/agent/{conversation_id}/client-tool-result/stale-call-id",
        content=json.dumps({"nonce": "stale", "ok": True, "result": {"text": "late"}}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "ignored": True,
        "error": "Client tool call no longer pending",
    }
