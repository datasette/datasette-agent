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
from datasette.app import Datasette
from opentelemetry.trace import SpanKind, StatusCode


pytest.importorskip("opentelemetry.sdk")

SCOPE = "datasette_agent"


@pytest.fixture
def ds(tmp_path):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
                "datasette-agent-background": {"id": "user"},
            }
        },
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
    ds, cookies, otel_spans, telemetry_ask_tools
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
    (suspended_tool,) = ours(otel_spans, "execute_tool ")
    assert turn.attributes["datasette_agent.mode"] == "chat"
    assert turn.attributes["datasette_agent.outcome"] == "question"
    assert "datasette_agent.resumed_from" not in turn.attributes
    # The question row carries the tool span's ids.
    row = (
        await ds.get_internal_database().execute(
            "SELECT trace_id, span_id FROM agent_questions WHERE id = ?",
            [question_event["data"]["id"]],
        )
    ).first()
    assert row["trace_id"] == format(suspended_tool.context.trace_id, "032x")
    assert row["span_id"] == format(suspended_tool.context.span_id, "016x")
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
    assert resume.attributes["datasette_agent.resumed_from"] == "question"
    assert resume.attributes["datasette_agent.tool_calls"] == 1
    # The replayed tool call, then one model response.
    assert resume.attributes["datasette_agent.chain.steps"] == 1
    (request,) = [
        s for s in otel_spans.get_finished_spans() if s.kind == SpanKind.SERVER
    ]
    assert resume.parent.span_id == request.context.span_id
    # The resumed turn links back to the tool span that suspended, whose
    # ids were persisted with the question row...
    (link,) = resume.links
    assert link.context.trace_id == suspended_tool.context.trace_id
    assert link.context.span_id == suspended_tool.context.span_id
    # ...and the re-executed call is marked as a replay.
    (replayed,) = ours(otel_spans, "execute_tool ")
    assert replayed.attributes["datasette_agent.tool.replayed"] is True
    assert replayed.attributes["datasette_agent.tool.outcome"] == "ok"
    assert "datasette_agent.suspension.kind" not in replayed.attributes


# --- Tool calls ---------------------------------------------------------------


async def run_tool_call(ds, cookies, otel_spans, *calls):
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(ds, cookies, conversation_id, tool_call_prompt(*calls))
    return events


@pytest.mark.asyncio
async def test_tool_span_shape(ds, cookies, otel_spans):
    await run_tool_call(
        ds,
        cookies,
        otel_spans,
        ("sql_query", {"database": "_memory", "sql": "select 1", "display": "both"}),
    )
    (turn,) = ours(otel_spans, "invoke_agent ")
    (tool,) = ours(otel_spans, "execute_tool ")
    assert tool.name == "execute_tool sql_query"
    assert tool.kind == SpanKind.INTERNAL
    assert tool.parent.span_id == turn.context.span_id
    assert tool.status.status_code == StatusCode.UNSET
    attrs = dict(tool.attributes)
    assert attrs["gen_ai.operation.name"] == "execute_tool"
    assert attrs["gen_ai.tool.name"] == "sql_query"
    assert attrs["gen_ai.tool.type"] == "function"
    assert attrs["datasette_agent.tool.plugin"] == "agent"
    assert attrs["datasette_agent.tool.outcome"] == "ok"
    assert attrs["datasette_agent.sql.display"] == "both"
    assert attrs["datasette_agent.tool.output.bytes"] > 10
    # llm issues an id for every tool call, and the provider's id (when it
    # has one) is what lands here - never the argument-hash call key.
    assert (
        isinstance(attrs["gen_ai.tool.call.id"], str) and attrs["gen_ai.tool.call.id"]
    )
    assert not attrs["gen_ai.tool.call.id"].startswith("call:")
    assert "error.type" not in attrs
    # The SQL itself is never on the tool span...
    assert "select 1" not in json.dumps(attrs)
    # ...but core's db.query for it nests underneath.
    queries = [
        s
        for s in otel_spans.get_finished_spans()
        if s.name == "db.query"
        and s.parent
        and s.parent.span_id == tool.context.span_id
    ]
    assert any(s.attributes.get("db.query.text") == "select 1" for s in queries)
    # Tool spans and chat spans are siblings under the turn.
    chats = ours(otel_spans, "chat ")
    assert all(c.parent.span_id == turn.context.span_id for c in chats)
    assert chats[0].end_time <= tool.start_time <= chats[1].start_time


@pytest.mark.asyncio
async def test_tool_error_payloads_are_outcomes_not_errors(ds, cookies, otel_spans):
    await run_tool_call(
        ds,
        cookies,
        otel_spans,
        ("sql_query", {"database": "nope", "sql": "select 1"}),
        ("sql_query", {"database": "_memory", "sql": "select * from SENTINEL_missing"}),
        ("describe_table", {"database": "_memory", "table": "absent"}),
    )
    tools = sorted(ours(otel_spans, "execute_tool "), key=lambda s: s.start_time)
    outcomes = [t.attributes["datasette_agent.tool.outcome"] for t in tools]
    assert outcomes == ["not_found", "error", "not_found"]
    for tool in tools:
        assert tool.status.status_code == StatusCode.UNSET
        assert "error.type" not in tool.attributes
        assert "SENTINEL_missing" not in json.dumps(dict(tool.attributes))


@pytest.mark.asyncio
async def test_tool_permission_denied_outcome(tmp_path, otel_spans):
    ds = Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {"datasette-agent": {"id": "user"}},
            "databases": {
                "_memory": {"permissions": {"execute-sql": {"id": "someone_else"}}}
            },
        },
        internal=str(tmp_path / "internal.db"),
    )
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    await run_tool_call(
        ds,
        cookies,
        otel_spans,
        ("sql_query", {"database": "_memory", "sql": "select 1"}),
    )
    (tool,) = ours(otel_spans, "execute_tool ")
    assert tool.attributes["datasette_agent.tool.outcome"] == "permission_denied"
    assert tool.status.status_code == StatusCode.UNSET


@pytest.mark.asyncio
async def test_raising_tool_is_an_error(ds, cookies, otel_spans, telemetry_ask_tools):
    events = await run_tool_call(
        ds, cookies, otel_spans, ("explode", {"path": "/SENTINEL-path"})
    )
    # llm turns the exception into an error tool result; the turn goes on.
    assert events[-1]["event"] == "done"
    (tool,) = ours(otel_spans, "execute_tool ")
    assert tool.name == "execute_tool explode"
    attrs = dict(tool.attributes)
    assert attrs["datasette_agent.tool.plugin"] == "AskToolPlugin"
    assert attrs["datasette_agent.tool.outcome"] == "error"
    assert attrs["error.type"] == "RuntimeError"
    assert tool.status.status_code == StatusCode.ERROR
    assert "SENTINEL-path" not in json.dumps(attrs)
    assert not tool.status.description
    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.attributes["datasette_agent.outcome"] == "done"


@pytest.mark.asyncio
async def test_suspending_tool_span(ds, cookies, otel_spans, telemetry_ask_tools):
    await run_tool_call(ds, cookies, otel_spans, ("approve_edit", {"path": "/tmp"}))
    (tool,) = ours(otel_spans, "execute_tool ")
    attrs = dict(tool.attributes)
    assert attrs["datasette_agent.tool.outcome"] == "suspended"
    assert attrs["datasette_agent.suspension.kind"] == "question"
    assert attrs["datasette_agent.question.type"] == "boolean"
    assert len(attrs["datasette_agent.question.id"]) == 26
    assert "datasette_agent.task.id" not in attrs
    assert "datasette_agent.tool.output.bytes" not in attrs
    assert tool.status.status_code == StatusCode.UNSET
    assert "error.type" not in attrs


@pytest.mark.asyncio
async def test_tools_learn_their_plugin(ds, telemetry_ask_tools):
    from datasette_agent.tools import get_agent_tools

    tools = await get_agent_tools(ds)
    by_name = {tool.name: tool.plugin_name for tool in tools}
    assert by_name["sql_query"] == "agent"
    assert by_name["approve_edit"] == "AskToolPlugin"
    # Order is what pluggy would have produced: last registered first.
    assert list(by_name)[0] == "approve_edit"


def test_classify_tool_payload():
    from datasette_agent.telemetry import classify_tool_payload

    assert classify_tool_payload("") == "ok"
    assert classify_tool_payload("plain text") == "ok"
    assert classify_tool_payload('{"rows": []}') == "ok"
    assert (
        classify_tool_payload('{"error": "Permission denied"}') == "permission_denied"
    )
    assert (
        classify_tool_payload(
            '{"ok": false, "error": "Permission denied: need execute-write-sql"}'
        )
        == "permission_denied"
    )
    assert classify_tool_payload('{"error": "Database \'x\' not found"}') == "not_found"
    assert (
        classify_tool_payload("{\"error\": \"Table 't' not found in database 'x'\"}")
        == "not_found"
    )
    assert classify_tool_payload('{"error": "no such table: t"}') == "error"
    assert classify_tool_payload('{"error": {"code": 1}}') == "error"
    assert classify_tool_payload('{"error"') == "ok"
    assert classify_tool_payload('["error"]') == "ok"


# --- Browser tasks ----------------------------------------------------------------


async def suspend_on_browser_task(ds, cookies, otel_spans):
    conversation_id = await start_conversation(ds, cookies)
    otel_spans.clear()
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(("run_in_browser", {"script": "1 + 1"})),
    )
    (task,) = [e["data"] for e in events if e["event"] == "browser_task"]
    return conversation_id, task


async def task_post(ds, cookies, conversation_id, task_id, action, body=None):
    return await ds.client.post(
        "/-/agent/{}/task/{}/{}".format(conversation_id, task_id, action),
        content=json.dumps(body or {}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )


@pytest.mark.asyncio
async def test_browser_task_suspends_and_completes(
    ds, cookies, otel_spans, telemetry_browser_tools
):
    conversation_id, task = await suspend_on_browser_task(ds, cookies, otel_spans)
    (tool,) = ours(otel_spans, "execute_tool ")
    attrs = dict(tool.attributes)
    assert attrs["datasette_agent.tool.outcome"] == "suspended"
    assert attrs["datasette_agent.suspension.kind"] == "browser_task"
    assert attrs["datasette_agent.task.id"] == task["id"]
    assert "datasette_agent.question.type" not in attrs
    (turn,) = ours(otel_spans, "invoke_agent ")
    assert turn.attributes["datasette_agent.outcome"] == "browser_task"

    otel_spans.clear()
    claim = await task_post(ds, cookies, conversation_id, task["id"], "claim")
    assert claim.status_code == 200, claim.text
    complete = await task_post(
        ds,
        cookies,
        conversation_id,
        task["id"],
        "complete",
        {"ok": True, "result": 2},
    )
    assert complete.status_code == 200, complete.text
    assert parse_sse(complete.text)[-1]["event"] == "done"

    (resume,) = ours(otel_spans, "invoke_agent ")
    assert resume.attributes["datasette_agent.mode"] == "resume"
    assert resume.attributes["datasette_agent.resumed_from"] == "browser_task"
    (link,) = resume.links
    assert link.context.span_id == tool.context.span_id
    (replayed,) = ours(otel_spans, "execute_tool ")
    assert replayed.attributes["datasette_agent.tool.replayed"] is True


@pytest.mark.asyncio
async def test_browser_task_cancel_resumes_with_link(
    ds, cookies, otel_spans, telemetry_browser_tools
):
    conversation_id, task = await suspend_on_browser_task(ds, cookies, otel_spans)
    (tool,) = ours(otel_spans, "execute_tool ")
    otel_spans.clear()
    cancel = await task_post(ds, cookies, conversation_id, task["id"], "cancel")
    assert cancel.status_code == 200, cancel.text
    (resume,) = ours(otel_spans, "invoke_agent ")
    assert resume.attributes["datasette_agent.resumed_from"] == "browser_task"
    (link,) = resume.links
    assert link.context.span_id == tool.context.span_id


def test_link_kwargs_rejects_bad_ids():
    from datasette_agent.telemetry import link_kwargs

    assert link_kwargs(None, None) == {}
    assert link_kwargs("", "abc") == {}
    assert link_kwargs("not-hex", "0123456789abcdef") == {}
    assert link_kwargs("0" * 32, "0" * 16) == {}
    (link,) = link_kwargs("1" * 32, "2" * 16)["links"]
    assert link.context.is_remote is True
    assert format(link.context.trace_id, "032x") == "1" * 32


def test_current_span_ids_without_a_recording_span():
    from datasette_agent.telemetry import current_span_ids

    assert current_span_ids() == (None, None)


@pytest.mark.asyncio
async def test_schema_migration_adds_trace_columns(tmp_path):
    from datasette_agent.schema import SCHEMA_SQL, ensure_tables

    ds = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    db = ds.get_internal_database()
    # A database from before the columns existed.
    old_sql = SCHEMA_SQL.replace(",\n    trace_id TEXT,\n    span_id TEXT\n", "\n")
    assert "trace_id" not in old_sql
    await db.execute_write_script(old_sql)
    await ensure_tables(db)
    for table in ("agent_questions", "agent_browser_tasks"):
        columns = {
            row["name"]
            for row in (await db.execute(f"PRAGMA table_info({table})")).rows
        }
        assert {"trace_id", "span_id"} <= columns, table
    # Idempotent.
    await ensure_tables(db)


# --- Background agents ----------------------------------------------------------

GOAL_TOOL_THEN_FINISH = tool_call_prompt(
    ("list_databases_and_tables", {}),
    ("mark_finished", {"final_message": "done"}),
)


async def start_background_via_api(ds, cookies, goal):
    response = await ds.client.post(
        "/-/agent/api/background",
        content=json.dumps({"goal": goal}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    return response.json()["agent_id"]


async def wait_for_agent(ds, agent_id):
    task = getattr(ds, "_background_agent_tasks", {}).get(agent_id)
    if task is not None:
        await asyncio.wait([task], timeout=10)


@pytest.mark.asyncio
async def test_background_run_is_a_linked_root(ds, cookies, otel_spans):
    agent_id = await start_background_via_api(ds, cookies, GOAL_TOOL_THEN_FINISH)
    await wait_for_agent(ds, agent_id)

    finished = otel_spans.get_finished_spans()
    (request,) = [s for s in finished if s.kind == SpanKind.SERVER]
    (root,) = ours(otel_spans, "invoke_agent ")
    # Its own trace, linked back to the request that spawned it.
    assert root.parent is None
    assert root.context.trace_id != request.context.trace_id
    (link,) = root.links
    assert link.context.trace_id == request.context.trace_id
    assert link.context.span_id == request.context.span_id
    assert root.status.status_code == StatusCode.UNSET
    attrs = dict(root.attributes)
    assert attrs["datasette_agent.mode"] == "background"
    assert attrs["datasette_agent.outcome"] == "completed"
    assert attrs["datasette_agent.background.id"] == agent_id
    assert attrs["datasette_agent.background.iterations"] == 1
    assert attrs["datasette_agent.background.max_iterations"] == 50
    assert attrs["datasette_agent.background.spawned_from_conversation"] is False
    assert attrs["datasette_agent.chain.steps"] == 2
    assert attrs["datasette_agent.tool_calls"] == 2
    assert attrs["gen_ai.request.model"] == "echo"

    (iteration,) = ours(otel_spans, "datasette_agent.background.iteration")
    assert iteration.parent.span_id == root.context.span_id
    assert iteration.attributes["datasette_agent.background.iteration"] == 1

    chats = ours(otel_spans, "chat ")
    assert len(chats) == 2
    assert all(c.parent.span_id == iteration.context.span_id for c in chats)
    assert all(c.attributes["datasette_agent.streaming"] is False for c in chats)
    assert all("gen_ai.first_token" not in [e.name for e in c.events] for c in chats)

    tools = {t.name: t for t in ours(otel_spans, "execute_tool ")}
    assert set(tools) == {
        "execute_tool list_databases_and_tables",
        "execute_tool mark_finished",
    }
    assert all(t.parent.span_id == iteration.context.span_id for t in tools.values())
    # mark_finished is built per run, not registered by a plugin.
    assert (
        tools["execute_tool mark_finished"].attributes["datasette_agent.tool.plugin"]
        == "unknown"
    )
    (prompt,) = ours(otel_spans, "datasette_agent.system_prompt")
    assert prompt.parent.span_id == iteration.context.span_id


@pytest.mark.asyncio
async def test_explorer_run_hits_iteration_limit(ds, otel_spans, monkeypatch):
    from datasette_agent import background_agent
    from datasette_agent.explorer import start_explorer

    # The explorer's goal is prose, which echo answers without ever calling
    # mark_finished - so the run ends at the cap.
    monkeypatch.setattr(background_agent, "MAX_ITERATIONS", 2)
    await ds.invoke_startup()
    report_id, agent_id = await start_explorer(ds, {"id": "user"}, "_memory")
    await wait_for_agent(ds, agent_id)

    (root,) = ours(otel_spans, "invoke_agent ")
    assert root.parent is None
    assert root.links == ()  # started outside any span
    attrs = dict(root.attributes)
    assert attrs["datasette_agent.mode"] == "explorer"
    assert attrs["datasette_agent.outcome"] == "max_iterations"
    assert attrs["datasette_agent.background.iterations"] == 2
    assert attrs["datasette_agent.background.max_iterations"] == 2
    assert root.status.status_code == StatusCode.ERROR
    assert "error.type" not in attrs
    iterations = sorted(
        ours(otel_spans, "datasette_agent.background.iteration"),
        key=lambda s: s.start_time,
    )
    assert [
        i.attributes["datasette_agent.background.iteration"] for i in iterations
    ] == [
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_cancelled_background_run(ds, cookies, otel_spans, telemetry_ask_tools):
    agent_id = await start_background_via_api(
        ds, cookies, tool_call_prompt(("hang", {}))
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if any(s.name == "chat echo" for s in otel_spans.get_finished_spans()):
            break
    response = await ds.client.post(
        f"/-/agent/api/background/{agent_id}/cancel", cookies=cookies
    )
    assert response.json()["cancelled"] is True
    await wait_for_agent(ds, agent_id)

    (root,) = ours(otel_spans, "invoke_agent ")
    attrs = dict(root.attributes)
    assert attrs["datasette_agent.outcome"] == "cancelled"
    assert attrs["error.type"] == "CancelledError"
    assert attrs["datasette_agent.background.iterations"] == 1
    assert root.status.status_code == StatusCode.UNSET
    # The tool that was running when the cancel landed did not finish.
    (tool,) = ours(otel_spans, "execute_tool ")
    assert tool.name == "execute_tool hang"
    assert tool.attributes["datasette_agent.tool.outcome"] == "error"
    assert tool.attributes["error.type"] == "CancelledError"


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
