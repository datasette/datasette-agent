"""Span shapes for the agent's turns and model calls.

Each test drives a real turn through llm-echo and inspects the finished
spans the kit's in-memory exporter collected. The trace tree a chat turn
is expected to produce:

    POST /-/agent/(?P<conversation_id>...)/stream$     SERVER   (core)
    └── invoke_agent datasette-agent                   INTERNAL (plugin)
        ├── datasette_agent.system_prompt              INTERNAL
        │   └── db.query ...                           CLIENT   (core)
        ├── db.query ...                               CLIENT   (core)
        └── chat echo                                  CLIENT   (plugin)
"""

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest
from datasette import hookimpl
from datasette.app import Datasette
from datasette.plugins import pm
from opentelemetry.trace import SpanKind, StatusCode

from datasette_agent.tools import AgentTool

pytest.importorskip("opentelemetry.sdk")

SCOPE = "datasette_agent"


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


def parse_sse(text):
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


async def start_conversation(ds, cookies):
    response = await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation_id"]


async def send_message(ds, cookies, conversation_id, message):
    response = await ds.client.post(
        "/-/agent/{}/stream".format(conversation_id),
        content=json.dumps({"message": message}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def ours(otel_spans, name=None):
    """Finished spans from this plugin's scope, optionally by name or prefix."""
    spans = [
        span
        for span in otel_spans.get_finished_spans()
        if span.instrumentation_scope and span.instrumentation_scope.name == SCOPE
    ]
    if name is not None:
        spans = [span for span in spans if span.name.startswith(name)]
    return spans


def tool_call_prompt(*calls):
    "An llm-echo prompt that makes the model issue the given tool calls."
    return json.dumps(
        {
            "prompt": "call some tools",
            "tool_calls": [{"name": name, "arguments": args} for name, args in calls],
        }
    )


@pytest.fixture
def ask_tool_plugin():
    class AskToolPlugin:
        __name__ = "AskToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def approve_edit(datasette, actor, context, path):
                ok = await context.ask_user(
                    "Is it OK to edit files in {}?".format(path)
                )
                return json.dumps({"approved": ok, "path": path})

            return [
                AgentTool(
                    name="approve_edit",
                    description="Edit files (asks first)",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=approve_edit,
                )
            ]

    plugin = AskToolPlugin()
    pm.register(plugin, name="AskToolPlugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="AskToolPlugin")


# --- Chat turns -----------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_turn_trace_shape(ds, cookies, otel_spans):
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(ds, cookies, conversation_id, "hello there")
    assert events[-1]["event"] == "done"

    finished = otel_spans.get_finished_spans()
    (request,) = [s for s in finished if s.kind == SpanKind.SERVER]
    assert request.name.startswith("POST ")
    assert "stream" in request.name

    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.name == "invoke_agent datasette-agent"
    assert turn.kind == SpanKind.INTERNAL
    assert turn.parent.span_id == request.context.span_id
    assert turn.status.status_code == StatusCode.UNSET
    attrs = dict(turn.attributes)
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "datasette-agent"
    assert attrs["gen_ai.conversation.id"] == conversation_id
    assert attrs["gen_ai.request.model"] == "echo"
    assert attrs["datasette_agent.mode"] == "chat"
    assert attrs["datasette_agent.outcome"] == "done"
    # This turn's user message is persisted before the history is loaded.
    assert attrs["datasette_agent.history.messages"] == 1
    assert attrs["datasette_agent.chain.steps"] == 1
    assert attrs["datasette_agent.tool_calls"] == 0
    assert "error.type" not in attrs
    assert "datasette_agent.notifications_drained" not in attrs

    (prompt,) = ours(otel_spans, "datasette_agent.system_prompt")
    assert prompt.parent.span_id == turn.context.span_id
    assert prompt.attributes["datasette_agent.databases"] >= 1
    assert prompt.attributes["datasette_agent.prompt.chars"] > 100

    (chat,) = ours(otel_spans, "chat ")
    assert chat.name == "chat echo"
    assert chat.kind == SpanKind.CLIENT
    assert chat.parent.span_id == turn.context.span_id
    chat_attrs = dict(chat.attributes)
    assert chat_attrs["gen_ai.operation.name"] == "chat"
    assert chat_attrs["gen_ai.provider.name"] == "echo"
    assert chat_attrs["gen_ai.request.model"] == "echo"
    assert chat_attrs["datasette_agent.chain.index"] == 0
    assert chat_attrs["datasette_agent.streaming"] is True
    assert chat_attrs["datasette_agent.tool_calls_requested"] == 0
    # llm-echo reports word counts as usage
    assert chat_attrs["gen_ai.usage.input_tokens"] == 2
    assert chat_attrs["gen_ai.usage.output_tokens"] > 0
    assert "gen_ai.response.model" not in chat_attrs
    assert [event.name for event in chat.events] == ["gen_ai.first_token"]

    # Core's db.query spans issued during the turn nest under the turn (or
    # under the system prompt span), never as orphan roots. The view runs
    # a couple of queries before the turn starts; those sit under the
    # request span.
    turn_family = {turn.context.span_id, prompt.context.span_id}
    db_spans = [
        s for s in finished if s.name == "db.query" and s.start_time >= turn.start_time
    ]
    assert len(db_spans) >= 3
    assert all(
        s.parent is not None and s.parent.span_id in turn_family for s in db_spans
    )


@pytest.mark.asyncio
async def test_tool_calls_count_steps_and_tools(ds, cookies, otel_spans):
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(("list_databases_and_tables", {})),
    )
    assert [e["event"] for e in events if e["event"] == "tool_result"]

    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.attributes["datasette_agent.chain.steps"] == 2
    assert turn.attributes["datasette_agent.tool_calls"] == 1
    assert turn.attributes["datasette_agent.outcome"] == "done"

    chats = sorted(ours(otel_spans, "chat "), key=lambda s: s.start_time)
    assert [c.attributes["datasette_agent.chain.index"] for c in chats] == [0, 1]
    assert [c.attributes["datasette_agent.tool_calls_requested"] for c in chats] == [
        1,
        0,
    ]
    assert all(c.parent.span_id == turn.context.span_id for c in chats)


@pytest.mark.asyncio
async def test_history_grows_across_turns(ds, cookies, otel_spans):
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "first")
    otel_spans.clear()
    await send_message(ds, cookies, conversation_id, "second")
    (turn,) = ours(otel_spans, "invoke_agent ")
    # The first turn's user message and assistant response, plus this
    # turn's user message.
    assert turn.attributes["datasette_agent.history.messages"] == 3


@pytest.mark.asyncio
async def test_turn_error_is_reported_and_still_streams_error_event(
    ds, cookies, otel_spans, monkeypatch
):
    from datasette_agent import agent

    async def explode(db, conversation_id):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(agent, "load_messages", explode)
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(ds, cookies, conversation_id, "hello")
    assert events[-1] == {"event": "error", "data": {"message": "history unavailable"}}

    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.status.status_code == StatusCode.ERROR
    attrs = dict(turn.attributes)
    assert attrs["datasette_agent.outcome"] == "error"
    assert attrs["error.type"] == "RuntimeError"
    # Failed before the model was resolved.
    assert attrs["gen_ai.request.model"] == "unknown"
    assert attrs["datasette_agent.chain.steps"] == 0
    # The message never reaches the span.
    assert "history unavailable" not in json.dumps(attrs)
    assert not turn.status.description


def test_chain_limit_is_its_own_outcome(otel_spans):
    from datasette_agent.telemetry import turn_span

    with turn_span(mode="chat", conversation_id="01TEST") as turn:
        turn.fail(ValueError("Chain limit of 5 exceeded."))
    (span,) = ours(otel_spans, "invoke_agent ")
    assert span.attributes["datasette_agent.outcome"] == "chain_limit"
    assert span.attributes["error.type"] == "ValueError"
    assert span.status.status_code == StatusCode.ERROR


def test_cancellation_is_not_an_error(otel_spans):
    from datasette_agent.telemetry import turn_span

    with pytest.raises(asyncio.CancelledError):
        with turn_span(mode="chat", conversation_id="01TEST"):
            raise asyncio.CancelledError()
    (span,) = ours(otel_spans, "invoke_agent ")
    assert span.attributes["datasette_agent.outcome"] == "cancelled"
    assert span.attributes["error.type"] == "CancelledError"
    assert span.status.status_code == StatusCode.UNSET


def test_unknown_mode_is_clamped(otel_spans):
    from datasette_agent.telemetry import turn_span

    with turn_span(mode="nonsense", conversation_id="01TEST"):
        pass
    (span,) = ours(otel_spans, "invoke_agent ")
    assert span.attributes["datasette_agent.mode"] == "chat"


# --- Suspension and resume --------------------------------------------------


@pytest.mark.asyncio
async def test_question_suspends_then_resume_turn(
    ds, cookies, otel_spans, ask_tool_plugin
):
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(("approve_edit", {"path": "/tmp"})),
    )
    (question_event,) = [e for e in events if e["event"] == "question"]
    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.attributes["datasette_agent.mode"] == "chat"
    assert turn.attributes["datasette_agent.outcome"] == "question"
    assert turn.status.status_code == StatusCode.UNSET
    # The paused tool call is not a completed one.
    assert turn.attributes["datasette_agent.tool_calls"] == 0
    assert turn.attributes["datasette_agent.chain.steps"] == 1

    otel_spans.clear()
    response = await ds.client.post(
        "/-/agent/{}/question/{}".format(conversation_id, question_event["data"]["id"]),
        content=json.dumps({"answer": True}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[-1]["event"] == "done"

    (resume,) = ours(otel_spans, "invoke_agent ")
    assert resume.attributes["datasette_agent.mode"] == "resume"
    assert resume.attributes["datasette_agent.outcome"] == "done"
    assert resume.attributes["datasette_agent.tool_calls"] == 1
    # The replayed tool call, then one model response.
    assert resume.attributes["datasette_agent.chain.steps"] == 1
    (request,) = [
        s for s in otel_spans.get_finished_spans() if s.kind == SpanKind.SERVER
    ]
    assert resume.parent.span_id == request.context.span_id


# --- CLI ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_turn(ds, otel_spans, capsys):
    from datasette_agent.cli_chat import run_chat

    await run_chat(ds, initial_prompt="hello from the terminal", actor={"id": "cli"})
    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.attributes["datasette_agent.mode"] == "cli"
    assert turn.attributes["datasette_agent.outcome"] == "done"
    assert turn.attributes["gen_ai.request.model"] == "echo"
    assert turn.attributes["datasette_agent.chain.steps"] == 1
    assert "datasette_agent.history.messages" not in turn.attributes
    (chat,) = ours(otel_spans, "chat ")
    assert chat.parent.span_id == turn.context.span_id
    assert chat.attributes["gen_ai.usage.input_tokens"] == 4
    assert "hello from the terminal" in capsys.readouterr().out


# --- Provider names -------------------------------------------------------------


def test_provider_name_for():
    from datasette_agent.telemetry import provider_name_for

    def model_from(module):
        cls = type("M", (), {"model_id": "x"})
        cls.__module__ = module
        return cls()

    class Wrapped:
        def __init__(self, inner):
            self._model = inner

    assert provider_name_for(model_from("llm_anthropic")) == "anthropic"
    assert provider_name_for(model_from("llm_gemini")) == "gcp.gemini"
    assert provider_name_for(model_from("llm_mistral")) == "mistral_ai"
    assert provider_name_for(model_from("llm_openrouter.models")) == "openrouter"
    assert (
        provider_name_for(model_from("llm.default_plugins.openai_models")) == "openai"
    )
    assert provider_name_for(model_from("llm_echo")) == "echo"
    assert provider_name_for(Wrapped(model_from("llm_anthropic"))) == "anthropic"


def test_cache_tokens_are_normalised():
    from datasette_agent.telemetry import _cache_tokens

    assert _cache_tokens(None) == (None, None)
    assert _cache_tokens({}) == (None, None)
    assert _cache_tokens(
        {"cache_read_input_tokens": 120, "cache_creation_input_tokens": 30}
    ) == (120, 30)
    assert _cache_tokens({"prompt_tokens_details": {"cached_tokens": 64}}) == (
        64,
        None,
    )
    assert _cache_tokens({"cache_read_input_tokens": "12"}) == (None, None)


# --- No provider ------------------------------------------------------------------


def test_turn_runs_unchanged_without_a_provider(tmp_path):
    """With no SDK provider installed every span is a NonRecordingSpan and
    every instrument a no-op: a turn must behave identically. Runs in a
    fresh interpreter because the test session has a provider installed.
    Front-loaded by conftest, like the kit's own subprocess test."""
    script = textwrap.dedent(
        """
        import asyncio, json
        from datasette.app import Datasette
        from datasette_agent.agent import run_agent
        from datasette_agent.schema import ensure_tables
        from opentelemetry import trace

        assert type(trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"

        class Writer:
            def __init__(self):
                self.chunks = []
            async def write(self, text):
                self.chunks.append(text)

        async def main():
            ds = Datasette(
                memory=True,
                metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
                config={"permissions": {"datasette-agent": {"id": "user"}}},
                internal=%r,
            )
            await ds.invoke_startup()
            db = ds.get_internal_database()
            await ensure_tables(db)
            await db.execute_write(
                "INSERT INTO agent_conversations (id, actor_id, created_at, updated_at) "
                "VALUES ('01NOPROVIDERAAAAAAAAAAAAAA', 'user', '', '')"
            )
            writer = Writer()
            await run_agent(ds, {"id": "user"}, "01NOPROVIDERAAAAAAAAAAAAAA", "hello", writer)
            text = "".join(writer.chunks)
            assert "event: text_chunk" in text, text
            assert text.rstrip().endswith("event: done\\ndata: {}"), text
            print("NO_PROVIDER_OK")

        asyncio.run(main())
        """
        % str(tmp_path / "internal.db")
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "NO_PROVIDER_OK" in result.stdout
