"""Metric values for turns and model calls, via the kit's collector."""

import json

import pytest
from datasette.app import Datasette

pytest.importorskip("opentelemetry.sdk")

from test_telemetry import (  # noqa: E402
    GOAL_TOOL_THEN_FINISH,
    send_message,
    start_conversation,
    suspend_on_browser_task,
    task_post,
    tool_call_prompt,
    wait_for_agent,
)


@pytest.fixture
def ds(tmp_path):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )


@pytest.fixture
def cookies(ds):
    return {"ds_actor": ds.client.actor_cookie({"id": "user"})}


CHAT_ATTRS = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "echo",
    "gen_ai.request.model": "echo",
}


@pytest.mark.asyncio
async def test_chat_turn_metrics(ds, cookies, otel_metrics):
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "hello there")
    otel_metrics.collect()

    tokens_in = otel_metrics.point(
        "gen_ai.client.token.usage", {**CHAT_ATTRS, "gen_ai.token.type": "input"}
    )
    assert tokens_in.count == 1
    assert tokens_in.sum == 2
    tokens_out = otel_metrics.point(
        "gen_ai.client.token.usage", {**CHAT_ATTRS, "gen_ai.token.type": "output"}
    )
    assert tokens_out.sum > 0
    # No cache counts from echo.
    assert not otel_metrics.points(
        "gen_ai.client.token.usage", {"gen_ai.token.type": "cache_read"}
    )

    duration = otel_metrics.point("gen_ai.client.operation.duration", CHAT_ATTRS)
    assert duration.count == 1
    assert "error.type" not in dict(duration.attributes)

    ttft = otel_metrics.point(
        "datasette_agent.chat.time_to_first_token",
        {"gen_ai.provider.name": "echo", "gen_ai.request.model": "echo"},
    )
    assert ttft.count == 1
    assert ttft.sum <= duration.sum

    turn = otel_metrics.point(
        "datasette_agent.turn.duration",
        {
            "datasette_agent.mode": "chat",
            "datasette_agent.outcome": "done",
            "gen_ai.request.model": "echo",
        },
    )
    assert turn.count == 1
    assert turn.sum >= duration.sum

    steps = otel_metrics.point(
        "datasette_agent.chain.steps",
        {"datasette_agent.mode": "chat", "gen_ai.request.model": "echo"},
    )
    assert steps.count == 1 and steps.sum == 1

    # +1 at the start, -1 at the end: nothing in flight once the turn ends.
    active = otel_metrics.point(
        "datasette_agent.turns.active", {"datasette_agent.mode": "chat"}
    )
    assert active.value == 0


@pytest.mark.asyncio
async def test_tool_calling_turn_records_one_measurement_per_response(
    ds, cookies, otel_metrics
):
    conversation_id = await start_conversation(ds, cookies)
    await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(("list_databases_and_tables", {})),
    )
    otel_metrics.collect()
    assert otel_metrics.point("gen_ai.client.operation.duration", CHAT_ATTRS).count == 2
    steps = otel_metrics.point("datasette_agent.chain.steps")
    assert steps.count == 1 and steps.sum == 2


@pytest.mark.asyncio
async def test_failed_turn_carries_error_type(ds, cookies, otel_metrics, monkeypatch):
    from datasette_agent import agent

    async def explode(db, conversation_id):
        raise RuntimeError("nope")

    monkeypatch.setattr(agent, "load_messages", explode)
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "hello")
    otel_metrics.collect()
    turn = otel_metrics.point(
        "datasette_agent.turn.duration", {"datasette_agent.outcome": "error"}
    )
    assert turn.attributes["error.type"] == "RuntimeError"
    assert turn.attributes["gen_ai.request.model"] == "unknown"
    assert "nope" not in json.dumps(dict(turn.attributes))
    assert not otel_metrics.points("gen_ai.client.operation.duration")


def test_active_turns_counter_moves_with_the_turn(otel_metrics):
    from datasette_agent.telemetry import turn_span

    with turn_span(mode="cli", conversation_id="01TEST"):
        otel_metrics.collect()
        inside = otel_metrics.point(
            "datasette_agent.turns.active", {"datasette_agent.mode": "cli"}
        )
        assert inside.value == 1
    # The kit's reader reports sums cumulatively: back to zero after -1.
    otel_metrics.collect()
    after = otel_metrics.point(
        "datasette_agent.turns.active", {"datasette_agent.mode": "cli"}
    )
    assert after.value == 0


@pytest.mark.asyncio
async def test_tool_metrics(ds, cookies, otel_metrics, telemetry_ask_tools):
    conversation_id = await start_conversation(ds, cookies)
    await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(
            ("sql_query", {"database": "_memory", "sql": "select 1"}),
            ("sql_query", {"database": "nope", "sql": "select 1"}),
            ("big_output", {}),
        ),
    )
    otel_metrics.collect()
    ok = otel_metrics.point(
        "datasette_agent.tool.duration",
        {
            "gen_ai.tool.name": "sql_query",
            "datasette_agent.tool.plugin": "agent",
            "datasette_agent.tool.outcome": "ok",
        },
    )
    assert ok.count == 1
    not_found = otel_metrics.point(
        "datasette_agent.tool.duration",
        {"gen_ai.tool.name": "sql_query", "datasette_agent.tool.outcome": "not_found"},
    )
    assert not_found.count == 1
    truncated = otel_metrics.point(
        "datasette_agent.tool.output.truncated", {"gen_ai.tool.name": "big_output"}
    )
    assert truncated.value == 1
    assert not otel_metrics.points(
        "datasette_agent.tool.output.truncated", {"gen_ai.tool.name": "sql_query"}
    )


@pytest.mark.asyncio
async def test_background_run_metrics(ds, otel_metrics):
    from datasette_agent.api import start_background_agent

    await ds.invoke_startup()
    agent_id = await start_background_agent(
        datasette=ds, actor={"id": "user"}, goal=GOAL_TOOL_THEN_FINISH
    )
    await wait_for_agent(ds, agent_id)
    otel_metrics.collect()
    iterations = otel_metrics.point(
        "datasette_agent.background.iterations",
        {"datasette_agent.mode": "background", "datasette_agent.outcome": "completed"},
    )
    assert iterations.count == 1 and iterations.sum == 1
    turn = otel_metrics.point(
        "datasette_agent.turn.duration", {"datasette_agent.mode": "background"}
    )
    assert turn.count == 1
    assert (
        otel_metrics.point(
            "datasette_agent.turns.active", {"datasette_agent.mode": "background"}
        ).value
        == 0
    )
    # The two model responses of the one iteration.
    assert otel_metrics.point("gen_ai.client.operation.duration", CHAT_ATTRS).count == 2
    assert not otel_metrics.points("datasette_agent.chat.time_to_first_token")


@pytest.mark.asyncio
async def test_question_suspension_metrics(
    ds, cookies, otel_metrics, telemetry_ask_tools
):
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(
        ds, cookies, conversation_id, tool_call_prompt(("approve_edit", {"path": "/x"}))
    )
    (question,) = [e["data"] for e in events if e["event"] == "question"]
    otel_metrics.collect()
    suspended = otel_metrics.point(
        "datasette_agent.suspensions",
        {
            "datasette_agent.suspension.kind": "question",
            "gen_ai.tool.name": "approve_edit",
            "datasette_agent.question.type": "boolean",
        },
    )
    assert suspended.value == 1
    assert not otel_metrics.points("datasette_agent.suspension.wait")

    response = await ds.client.post(
        "/-/agent/{}/question/{}".format(conversation_id, question["id"]),
        content=json.dumps({"answer": True}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    otel_metrics.collect()
    wait = otel_metrics.point(
        "datasette_agent.suspension.wait",
        {
            "datasette_agent.suspension.kind": "question",
            "datasette_agent.suspension.resolution": "answered",
        },
    )
    assert wait.count == 1 and 0 <= wait.sum < 5
    # The replayed call on resume is not a new suspension: nothing new
    # since the previous (delta) collect.
    assert not otel_metrics.points("datasette_agent.suspensions")


@pytest.mark.asyncio
async def test_browser_task_wait_by_resolution(
    ds, cookies, otel_metrics, otel_spans, telemetry_browser_tools
):
    from datasette_agent.browser_tasks import expire_task

    conversation_id, task = await suspend_on_browser_task(ds, cookies, otel_spans)
    response = await task_post(
        ds, cookies, conversation_id, task["id"], "complete", {"ok": True}
    )
    assert response.status_code == 200, response.text
    conversation_id, cancelled = await suspend_on_browser_task(ds, cookies, otel_spans)
    response = await task_post(ds, cookies, conversation_id, cancelled["id"], "cancel")
    assert response.status_code == 200, response.text
    _, expired = await suspend_on_browser_task(ds, cookies, otel_spans)
    assert await expire_task(ds.get_internal_database(), expired["id"]) is True
    # Second expiry of the same row is a no-op and records nothing.
    assert await expire_task(ds.get_internal_database(), expired["id"]) is False

    otel_metrics.collect()
    for resolution in ("completed", "cancelled", "expired"):
        point = otel_metrics.point(
            "datasette_agent.suspension.wait",
            {
                "datasette_agent.suspension.kind": "browser_task",
                "datasette_agent.suspension.resolution": resolution,
            },
        )
        assert point.count == 1, resolution
    assert (
        otel_metrics.point(
            "datasette_agent.suspensions",
            {"datasette_agent.suspension.kind": "browser_task"},
        ).value
        == 3
    )
