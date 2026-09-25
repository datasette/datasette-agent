import asyncio
import json

import pytest
from datasette.app import Datasette
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from test_questions import (  # noqa: F401
    ask_tool_plugin,
    answer_via_api,
    send_message,
    start_conversation,
)

TURN = "invoke_agent datasette-agent"


@pytest.fixture(scope="session")
def _exporter():
    # set_tracer_provider() only works once per process
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_exporter):
    _exporter.clear()
    yield _exporter.get_finished_spans
    _exporter.clear()


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


def turns(finished):
    return [s for s in finished if s.name == TURN]


def descendants(finished, root):
    ids, found = {root.context.span_id}, []
    for span in sorted(finished, key=lambda s: s.start_time):
        if span.parent is not None and span.parent.span_id in ids:
            ids.add(span.context.span_id)
            found.append(span)
    return found


@pytest.mark.asyncio
async def test_chat_turn_span(ds, cookies, spans):
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "secret message text")
    finished = spans()
    [turn] = turns(finished)
    assert dict(turn.attributes) == {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "datasette-agent",
        "gen_ai.conversation.id": conversation_id,
        "datasette_agent.mode": "chat",
        "datasette_agent.outcome": "completed",
    }
    assert turn.kind == trace.SpanKind.INTERNAL
    assert turn.status.status_code == StatusCode.UNSET
    names = [s.name for s in descendants(finished, turn)]
    assert "datasette_llm.prompt" in names
    assert "chat echo" in names
    # Privacy: no span records the user's message text
    for span in finished:
        for value in span.attributes.values():
            assert "secret message" not in str(value)


@pytest.mark.asyncio
async def test_tool_call_nests_under_turn(ds, cookies, spans):
    conversation_id = await start_conversation(ds, cookies)
    message = {"tool_calls": [{"name": "list_databases_and_tables", "arguments": {}}]}
    await send_message(ds, cookies, conversation_id, json.dumps(message))
    finished = spans()
    [turn] = turns(finished)
    names = [s.name for s in descendants(finished, turn)]
    assert "execute_tool list_databases_and_tables" in names


@pytest.mark.asyncio
async def test_question_then_resume(ds, cookies, spans, ask_tool_plugin):  # noqa: F811
    conversation_id = await start_conversation(ds, cookies)
    message = {"tool_calls": [{"name": "approve_edit", "arguments": {"path": "/x"}}]}
    events = await send_message(ds, cookies, conversation_id, json.dumps(message))
    question = [e for e in events if e["event"] == "question"][0]["data"]
    [chat] = turns(spans())
    assert chat.attributes["datasette_agent.outcome"] == "question"
    assert chat.status.status_code == StatusCode.UNSET

    response = await answer_via_api(ds, cookies, conversation_id, question["id"], True)
    assert response.status_code == 200
    resume = [t for t in turns(spans()) if t is not chat]
    assert [t.attributes["datasette_agent.mode"] for t in resume] == ["resume"]
    assert resume[0].attributes["datasette_agent.outcome"] == "completed"
    assert resume[0].attributes["gen_ai.conversation.id"] == conversation_id


@pytest.mark.asyncio
async def test_model_error(ds, cookies, spans, monkeypatch):
    import llm_echo

    async def boom(self, *args, **kwargs):
        raise RuntimeError("secret error detail")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_echo.EchoAsync, "execute", boom)
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(ds, cookies, conversation_id, "hi")
    assert [e for e in events if e["event"] == "error"]
    [turn] = turns(spans())
    assert turn.attributes["datasette_agent.outcome"] == "error"
    assert turn.attributes["error.type"] == "RuntimeError"
    assert turn.status.status_code == StatusCode.ERROR
    assert not turn.status.description
    assert not turn.events


@pytest.mark.asyncio
async def test_writer_failure_mid_chain_restores_context(ds, spans):
    # A client disconnect abandons the chain's responses() generator without
    # aclose(). The turn span still ends as an error and restores the context.
    # The abandoned datasette_llm.prompt / chat spans are an upstream issue:
    # they outlive the turn and log "Failed to detach context" when collected.
    from datasette_agent.agent import run_agent
    from datasette_agent.schema import ensure_tables

    class FailingWriter:
        async def write(self, data):
            if data.startswith("event: text_chunk"):
                raise ConnectionError()

    await ds.invoke_startup()
    db = ds.get_internal_database()
    await ensure_tables(db)
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at)"
        " VALUES ('c1', 'user', '', '', '')"
    )
    with trace.get_tracer("test").start_as_current_span("outer") as outer:
        await run_agent(ds, {"id": "user"}, "c1", "hi", FailingWriter())
        assert trace.get_current_span() is outer
    [turn] = turns(spans())
    assert turn.parent.span_id == outer.get_span_context().span_id
    assert turn.attributes["datasette_agent.outcome"] == "error"
    assert turn.attributes["error.type"] == "ConnectionError"


@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt])
def test_cancelled_turn_is_not_an_error(spans, exc):
    from datasette_agent.telemetry import turn_span

    with pytest.raises(exc):
        with turn_span("chat", "c1"):
            raise exc()
    [turn] = turns(spans())
    assert turn.attributes["datasette_agent.outcome"] == "cancelled"
    assert "error.type" not in turn.attributes
    assert turn.status.status_code == StatusCode.UNSET
