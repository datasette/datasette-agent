"""Metric values for turns and model calls, via the kit's collector."""

import json

import pytest
from datasette.app import Datasette

pytest.importorskip("opentelemetry.sdk")

from test_telemetry import (  # noqa: E402
    ask_tool_plugin,  # noqa: F401  (fixture)
    send_message,
    start_conversation,
    tool_call_prompt,
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
async def test_tool_metrics(ds, cookies, otel_metrics, ask_tool_plugin):  # noqa: F811
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
