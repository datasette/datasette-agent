"""Stopping a running chat turn with POST /-/agent/{id}/stop."""

import asyncio
import json

import llm_echo
import pytest
from datasette import hookimpl
from datasette.app import Datasette
from datasette.plugins import pm

from datasette_agent.tools import AgentTool


@pytest.fixture
def ds(tmp_path):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={"permissions": {"datasette-agent": {"id": ["user", "other"]}}},
        internal=str(tmp_path / "internal.db"),
    )


def _cookies(ds, actor_id="user"):
    return {"ds_actor": ds.client.actor_cookie({"id": actor_id})}


async def _new_conversation(ds):
    response = await ds.client.post(
        "/-/agent/api/conversations",
        content="{}",
        headers={"Content-Type": "application/json"},
        cookies=_cookies(ds),
    )
    return response.json()["conversation_id"]


def _start_turn(ds, conversation_id, message):
    return asyncio.create_task(
        ds.client.post(
            f"/-/agent/{conversation_id}/stream",
            content=json.dumps({"message": message}),
            headers={"Content-Type": "application/json"},
            cookies=_cookies(ds),
            timeout=30,
        )
    )


async def _stop(ds, conversation_id, actor_id="user"):
    return await ds.client.post(
        f"/-/agent/{conversation_id}/stop", content="", cookies=_cookies(ds, actor_id)
    )


def _events(text):
    events = []
    current = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[6:])))
            current = None
    return events


async def _messages(ds, conversation_id):
    rows = (
        await ds.get_internal_database().execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    return [(row["role"], json.loads(row["message_json"])) for row in rows]


@pytest.fixture
def model_calls(monkeypatch):
    """Record each call to the echo model. A call whose prompt is "hang"
    streams some text and then waits until the test releases it."""
    calls = []
    release = asyncio.Event()
    streaming = asyncio.Event()
    original = llm_echo.EchoAsync.execute

    async def execute(self, prompt, stream, response, conversation=None):
        calls.append(prompt.prompt)
        if prompt.prompt == "hang":
            yield "partial answer"
            streaming.set()
            await release.wait()
            yield " that never finishes"
            return
        async for chunk in original(self, prompt, stream, response, conversation):
            yield chunk

    monkeypatch.setattr(llm_echo.EchoAsync, "execute", execute)
    return {"calls": calls, "release": release, "streaming": streaming}


@pytest.fixture
def slow_tool():
    """A slow_tool that waits until the test releases it."""
    state = {"started": asyncio.Event(), "release": asyncio.Event(), "ran": False}

    class SlowToolPlugin:
        __name__ = "SlowToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def slow(datasette, actor):
                state["started"].set()
                await state["release"].wait()
                state["ran"] = True
                return json.dumps({"finished": True})

            return [
                AgentTool(
                    name="slow_tool",
                    description="A tool that takes a while",
                    input_schema={"type": "object", "properties": {}},
                    fn=slow,
                )
            ]

    pm.register(SlowToolPlugin(), name="test-slow-tool")
    try:
        yield state
    finally:
        pm.unregister(name="test-slow-tool")


@pytest.mark.asyncio
async def test_stop_while_model_is_streaming(ds, model_calls):
    conversation_id = await _new_conversation(ds)
    turn = _start_turn(ds, conversation_id, "hang")
    await asyncio.wait_for(model_calls["streaming"].wait(), 5)

    stop = await _stop(ds, conversation_id)
    assert stop.status_code == 200
    assert stop.json() == {"ok": True, "stopping": True}

    response = await asyncio.wait_for(turn, 5)
    events = _events(response.text)
    names = [name for name, _ in events]
    assert "error" not in names
    assert names[-2:] == ["stopped", "done"]
    assert events[-1][1] == {"stopped": True}

    # The text the user already saw is kept; the rest never arrives
    messages = await _messages(ds, conversation_id)
    assert [role for role, _ in messages] == ["user", "assistant"]
    assert messages[1][1]["parts"] == [{"type": "text", "text": "partial answer"}]

    # The conversation carries on normally afterwards
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "hello again"}),
        headers={"Content-Type": "application/json"},
        cookies=_cookies(ds),
    )
    names = [name for name, _ in _events(response.text)]
    assert "error" not in names and names[-1] == "done"
    assert [role for role, _ in await _messages(ds, conversation_id)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_stop_lets_a_running_tool_finish(ds, model_calls, slow_tool):
    conversation_id = await _new_conversation(ds)
    turn = _start_turn(
        ds,
        conversation_id,
        json.dumps({"prompt": "go", "tool_calls": [{"name": "slow_tool"}]}),
    )
    await asyncio.wait_for(slow_tool["started"].wait(), 5)

    stop = await _stop(ds, conversation_id)
    assert stop.json() == {"ok": True, "stopping": True}
    # The tool is not interrupted
    await asyncio.sleep(0.1)
    assert not turn.done()
    slow_tool["release"].set()

    response = await asyncio.wait_for(turn, 5)
    names = [name for name, _ in _events(response.text)]
    assert "error" not in names
    assert names[-2:] == ["stopped", "done"]
    assert slow_tool["ran"]
    # No model call after the tool: the turn stopped there
    assert len(model_calls["calls"]) == 1

    # The tool call has its result, so the history is complete and
    # nothing is left for auto-resume to re-run
    messages = await _messages(ds, conversation_id)
    assert [role for role, _ in messages] == ["user", "assistant", "tool"]
    tool_parts = messages[2][1]["parts"]
    assert json.loads(tool_parts[0]["output"]) == {"finished": True}

    response = await ds.client.get(f"/-/agent/{conversation_id}", cookies=_cookies(ds))
    assert "data-needs-resume" not in response.text


@pytest.mark.asyncio
async def test_stop_with_no_running_turn(ds):
    conversation_id = await _new_conversation(ds)
    stop = await _stop(ds, conversation_id)
    assert stop.status_code == 200
    assert stop.json() == {"ok": True, "stopping": False}


@pytest.mark.asyncio
async def test_stop_only_by_owner(ds, model_calls):
    conversation_id = await _new_conversation(ds)
    turn = _start_turn(ds, conversation_id, "hang")
    await asyncio.wait_for(model_calls["streaming"].wait(), 5)
    try:
        stop = await _stop(ds, conversation_id, actor_id="other")
        assert stop.status_code == 403
        missing = await _stop(ds, "0" * 26)
        assert missing.status_code == 404
        anonymous = await ds.client.post(f"/-/agent/{conversation_id}/stop")
        assert anonymous.status_code == 403
        assert not turn.done()
    finally:
        await _stop(ds, conversation_id)
        await asyncio.wait_for(turn, 5)


@pytest.mark.asyncio
async def test_stop_requires_post(ds):
    conversation_id = await _new_conversation(ds)
    response = await ds.client.get(
        f"/-/agent/{conversation_id}/stop", cookies=_cookies(ds)
    )
    assert response.status_code == 405


@pytest.mark.asyncio
async def test_second_turn_rejected_while_running(ds, model_calls):
    conversation_id = await _new_conversation(ds)
    turn = _start_turn(ds, conversation_id, "hang")
    await asyncio.wait_for(model_calls["streaming"].wait(), 5)
    try:
        second = await ds.client.post(
            f"/-/agent/{conversation_id}/stream",
            content=json.dumps({"message": "keep going"}),
            headers={"Content-Type": "application/json"},
            cookies=_cookies(ds),
        )
        assert second.status_code == 409
        assert "still working" in second.json()["error"]
    finally:
        await _stop(ds, conversation_id)
        await asyncio.wait_for(turn, 5)
    # The rejected message was never saved
    roles = [role for role, _ in await _messages(ds, conversation_id)]
    assert roles == ["user", "assistant"]
