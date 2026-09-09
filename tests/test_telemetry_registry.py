"""Registry <-> reality conformance for datasette-agent's telemetry.

Static half: the registry is well-formed. Dynamic half: one broad workload,
a single ``collect()``, then the kit's four conformance assertions plus the
sentinel-content privacy walk.
"""

import json

import pytest

from datasette_agent.telemetry_registry import (
    ATTRIBUTES,
    HISTOGRAM,
    METRICS,
    SPANS,
)

SCOPE = "datasette_agent"


# --- Static half (no SDK needed) -----------------------------------------


def test_package_never_imports_the_sdk():
    # Front-loaded by conftest's pytest_collection_modifyitems - the
    # helper's docstring documents a macOS/CPython 3.13 fork+exec crash
    # when subprocess-spawning tests run late in a thread-heavy process.
    from datasette.telemetry_testing import assert_package_never_imports_sdk

    assert_package_never_imports_sdk("datasette_agent")


def test_registry_has_no_duplicate_names():
    for group in (SPANS, METRICS, ATTRIBUTES):
        names = [str(entry) for entry in group]
        assert len(names) == len(set(names)), f"duplicates in {names}"


def test_registry_entries_are_documented():
    for entry in (*SPANS, *METRICS, *ATTRIBUTES):
        assert entry.description and entry.description.strip(), (
            f"{entry!r} has no description"
        )


def test_every_histogram_declares_buckets():
    for metric in METRICS:
        if metric.kind == HISTOGRAM:
            assert metric.buckets, f"{metric!r} declares no buckets"
        else:
            assert metric.buckets is None, f"{metric!r} is not a histogram"


def test_entries_are_usable_as_plain_strings():
    for entry in (*SPANS, *METRICS, *ATTRIBUTES):
        assert isinstance(entry, str)
        assert entry == str(entry)


def test_every_name_is_prefixed():
    # The kit docs' naming rule: signals live under a prefix the plugin
    # owns, never bare datasette.*. The gen_ai.* names are the GenAI
    # semantic conventions, deliberately shared so backends render them;
    # error.type is core's semconv spelling, deliberately reused.
    for entry in (*SPANS, *METRICS):
        name = str(entry)
        assert (
            name.startswith("datasette_agent.")
            or name.startswith("gen_ai.")
            or name.startswith(("invoke_agent ", "chat ", "execute_tool "))
        ), entry
    for attribute in ATTRIBUTES:
        name = str(attribute)
        assert (
            name.startswith("datasette_agent.")
            or name.startswith("gen_ai.")
            or name == "error.type"
        ), attribute


def test_span_attributes_are_registered_attributes():
    registered = set(ATTRIBUTES)
    for entry in (*SPANS, *METRICS):
        for attribute in entry.attributes:
            assert attribute in registered, f"{entry!r} uses unlisted {attribute!r}"


# --- Dynamic half ---------------------------------------------------------

pytest.importorskip("opentelemetry.sdk")

from datasette.app import Datasette  # noqa: E402
from datasette.telemetry_testing import (  # noqa: E402
    assert_metrics_conform,
    assert_metrics_covered,
    assert_no_forbidden_values,
    assert_spans_conform,
    assert_spans_covered,
)

from test_telemetry import (  # noqa: E402
    parse_sse,
    send_message,
    start_background_via_api,
    start_conversation,
    tool_call_prompt,
    wait_for_agent,
)

# Sentinels planted in the workload. The user's text and the model's
# output (echo repeats the prompt) must never reach any signal, in any
# scope. The SQL sentinel reaches core's db.query span by core's own
# documented design (db.query.text), so that one is checked against this
# plugin's scope only.
SENTINEL_USER_TEXT = "SENTINEL-user-text-7f3a"
SENTINEL_SQL = "SENTINEL_sql_9b1c"


async def exercise(tmp_path):
    """One broad workload touching every registered span and metric: a
    chat turn that calls a tool, a turn that suspends on a question and
    its resume, a CLI turn, and a background agent run."""
    from datasette_agent.cli_chat import run_chat

    ds = Datasette(
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
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, SENTINEL_USER_TEXT)
    await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(
            (
                "sql_query",
                {"database": "_memory", "sql": f"select 1 as {SENTINEL_SQL}"},
            )
        ),
    )
    # A tool whose output gets truncated, one that raises, and a SQL error
    # whose message would echo the SQL.
    await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(
            ("big_output", {}),
            ("explode", {"path": SENTINEL_USER_TEXT}),
            (
                "sql_query",
                {"database": "_memory", "sql": f"select * from {SENTINEL_SQL}"},
            ),
        ),
    )
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        tool_call_prompt(("approve_edit", {"path": SENTINEL_USER_TEXT})),
    )
    (question,) = [e for e in events if e["event"] == "question"]
    response = await ds.client.post(
        "/-/agent/{}/question/{}".format(conversation_id, question["data"]["id"]),
        content=json.dumps({"answer": True}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert parse_sse(response.text)[-1]["event"] == "done"
    await run_chat(ds, initial_prompt=SENTINEL_USER_TEXT, actor={"id": "cli"})
    goal = json.dumps(
        {
            "prompt": SENTINEL_USER_TEXT,
            "tool_calls": [
                {"name": "list_databases_and_tables", "arguments": {}},
                {"name": "mark_finished", "arguments": {"final_message": "done"}},
            ],
        }
    )
    agent_id = await start_background_via_api(ds, cookies, goal)
    await wait_for_agent(ds, agent_id)
    return ds


@pytest.mark.asyncio
async def test_conformance(
    tmp_path, otel_spans, otel_metrics, telemetry_ask_tools, capsys
):
    ds = await exercise(tmp_path)
    finished = otel_spans.get_finished_spans()
    # One collect() only: the reader is delta-temporality, so anything an
    # earlier collect() drained is invisible to the coverage assertions.
    otel_metrics.collect()
    assert_spans_conform(SPANS, finished, scope_name=SCOPE)
    assert_spans_covered(SPANS, finished, scope_name=SCOPE)
    assert_metrics_conform(METRICS, otel_metrics, scope_name=SCOPE)
    assert_metrics_covered(METRICS, otel_metrics, scope_name=SCOPE)
    # Privacy walk: user text and model output across every scope...
    assert_no_forbidden_values(
        {SENTINEL_USER_TEXT}, finished_spans=finished, collector=otel_metrics
    )
    # ...and the SQL text within this plugin's scope (core records it on
    # db.query by design).
    assert_no_forbidden_values(
        {SENTINEL_SQL},
        finished_spans=finished,
        collector=otel_metrics,
        scope_name=SCOPE,
    )
    del ds


# --- Wire names pinned as literals ----------------------------------------
#
# A rename is a dashboard-breaking decision to take here, deliberately,
# not a line to re-derive. If one of these fails, either revert the rename
# or update the literal AND the generated README reference in the same
# commit.

EXPECTED_SPANS = {
    "invoke_agent datasette-agent",
    "chat ",
    "execute_tool ",
    "datasette_agent.system_prompt",
    "datasette_agent.background.iteration",
}

EXPECTED_METRICS = {
    "gen_ai.client.token.usage",
    "gen_ai.client.operation.duration",
    "datasette_agent.chat.time_to_first_token",
    "datasette_agent.turn.duration",
    "datasette_agent.chain.steps",
    "datasette_agent.turns.active",
    "datasette_agent.tool.duration",
    "datasette_agent.tool.output.truncated",
    "datasette_agent.background.iterations",
    "datasette_agent.suspensions",
    "datasette_agent.suspension.wait",
}

EXPECTED_ATTRIBUTES = {
    "gen_ai.operation.name",
    "gen_ai.agent.name",
    "gen_ai.conversation.id",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.response.id",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.token.type",
    "datasette_agent.mode",
    "datasette_agent.outcome",
    "datasette_agent.history.messages",
    "datasette_agent.chain.steps",
    "datasette_agent.tool_calls",
    "datasette_agent.notifications_drained",
    "datasette_agent.chain.index",
    "datasette_agent.streaming",
    "datasette_agent.tool_calls_requested",
    "datasette_agent.databases",
    "datasette_agent.prompt.chars",
    "datasette_agent.background.id",
    "datasette_agent.background.iterations",
    "datasette_agent.background.max_iterations",
    "datasette_agent.background.spawned_from_conversation",
    "datasette_agent.background.iteration",
    "gen_ai.tool.name",
    "gen_ai.tool.call.id",
    "gen_ai.tool.type",
    "datasette_agent.tool.plugin",
    "datasette_agent.tool.outcome",
    "datasette_agent.suspension.kind",
    "datasette_agent.suspension.resolution",
    "datasette_agent.question.type",
    "datasette_agent.question.id",
    "datasette_agent.task.id",
    "datasette_agent.tool.replayed",
    "datasette_agent.resumed_from",
    "datasette_agent.tool.output.bytes",
    "datasette_agent.sql.display",
    "error.type",
}


def test_wire_names_are_pinned():
    assert {str(s) for s in SPANS} == EXPECTED_SPANS
    assert {str(m) for m in METRICS} == EXPECTED_METRICS
    assert {str(a) for a in ATTRIBUTES} == EXPECTED_ATTRIBUTES


# Metric dimensions must be bounded. The kit enforces declared values=
# enums; these are the attributes that are bounded by installed code
# (models, providers, exception classes) rather than by an enum here.
OPEN_BUT_BOUNDED = {
    "gen_ai.request.model",
    "gen_ai.provider.name",
    "gen_ai.tool.name",
    "datasette_agent.tool.plugin",
    "error.type",
}


def test_metric_dimensions_are_bounded():
    for metric in METRICS:
        for attribute in metric.attributes:
            assert attribute.values is not None or str(attribute) in OPEN_BUT_BOUNDED, (
                f"{metric}: {attribute} is neither an enum nor allowlisted"
            )


def test_generated_docs_are_fresh():
    """README.md's telemetry reference matches the registry - the pytest
    twin of the `scripts/telemetry-doc.py --check` CI gate."""
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "telemetry_doc", root / "scripts" / "telemetry-doc.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = (root / "README.md").read_text()
    _, rest = text.split(module.START, 1)
    checked_in, _ = rest.split(module.END, 1)
    assert checked_in == "\n\n" + module.render() + "\n", (
        "README.md telemetry reference is stale - run `uv run scripts/telemetry-doc.py`"
    )
