# Importing the fixture names registers them: session-scoped autouse
# tracer/meter providers with in-memory synchronous export (silently
# skipped when the SDK is not installed), and a per-test reset that drains
# the exporter and reader. Tests take otel_spans / otel_metrics. See the
# kit's docstrings for the once-per-process/ProxyTracer reasoning.
import asyncio
import json

import pytest
from datasette import hookimpl
from datasette.plugins import pm
from datasette_agent.tools import AgentTool
from datasette.telemetry_testing import (  # noqa: F401
    MetricsCollector,
    otel_metrics,
    otel_meter_provider,
    otel_provider,
    otel_reset,
    otel_spans,
)


def pytest_collection_modifyitems(items):
    # The kit's sdk-isolation check shells out to a fresh interpreter, and
    # its docstring documents a macOS/CPython 3.13 fork+exec crash (SIGBUS)
    # when subprocess-spawning tests run late in a thread-heavy process -
    # so run it first, the way core's conftest front-loads its equivalents.
    subprocess_tests = {
        "test_package_never_imports_the_sdk",
        "test_turn_runs_unchanged_without_a_provider",
    }
    front = [item for item in items if item.name in subprocess_tests]
    for item in front:
        items.insert(0, items.pop(items.index(item)))


# --- Tool plugins the telemetry tests drive through llm-echo ------------------
#
# Named distinctly from the ask_tool_plugin / browser_tool_plugin fixtures
# that test_questions.py and test_browser_tasks.py define locally.


@pytest.fixture
def telemetry_ask_tools():
    class AskToolPlugin:
        __name__ = "AskToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def approve_edit(datasette, actor, context, path):
                ok = await context.ask_user(
                    "Is it OK to edit files in {}?".format(path)
                )
                return json.dumps({"approved": ok, "path": path})

            async def explode(datasette, actor, path):
                raise RuntimeError("tool blew up on {}".format(path))

            async def big_output(datasette, actor):
                return {"rows": "x" * 20000}

            async def hang(datasette, actor):
                await asyncio.Event().wait()

            path_schema = {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
            return [
                AgentTool(
                    name="approve_edit",
                    description="Edit files (asks first)",
                    input_schema=path_schema,
                    fn=approve_edit,
                ),
                AgentTool(
                    name="explode",
                    description="Raises",
                    input_schema=path_schema,
                    fn=explode,
                ),
                AgentTool(
                    name="big_output",
                    description="Returns more than the model may see",
                    input_schema={"type": "object", "properties": {}},
                    fn=big_output,
                ),
                AgentTool(
                    name="hang",
                    description="Never returns",
                    input_schema={"type": "object", "properties": {}},
                    fn=hang,
                ),
            ]

    plugin = AskToolPlugin()
    pm.register(plugin, name="AskToolPlugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="AskToolPlugin")


@pytest.fixture
def telemetry_browser_tools():
    class BrowserToolPlugin:
        __name__ = "BrowserToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def run_in_browser(datasette, actor, context, script):
                outcome = await context.browser_task(
                    "<div id='harness'><script>execute()</script></div>",
                    payload={"script": script, "secret": "payload-secret"},
                    label="Running {} in your browser".format(script),
                    timeout_ms=5000,
                )
                return json.dumps(outcome)

            return [
                AgentTool(
                    name="run_in_browser",
                    description="Run a script in the user's browser",
                    input_schema={
                        "type": "object",
                        "properties": {"script": {"type": "string"}},
                        "required": ["script"],
                    },
                    fn=run_in_browser,
                )
            ]

    plugin = BrowserToolPlugin()
    pm.register(plugin, name="BrowserToolPlugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="BrowserToolPlugin")
