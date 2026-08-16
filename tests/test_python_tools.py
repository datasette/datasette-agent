import ast
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest
from datasette.app import Datasette

STATIC_DIR = Path(__file__).parent.parent / "datasette_agent" / "static"


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


async def make_context(datasette, **kwargs):
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tool_context import ToolContext

    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    defaults = dict(
        datasette=datasette,
        actor={"id": "user"},
        conversation_id="01TESTCONVERSATION0000000",
        tool_name="execute_python",
        arguments={"code": "1 + 1"},
        tool_call_id="call_1",
        supports_browser_tasks=True,
    )
    defaults.update(kwargs)
    return ToolContext(**defaults)


# A 1x1 transparent PNG, valid base64
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


# ---- The execute_python tool ----


@pytest.mark.asyncio
async def test_execute_python_success_payload(datasette_instance):
    from datasette_agent.python_tools import _execute_python

    seen = {}

    def callback(task):
        seen.update(task)
        return {
            "ok": True,
            "result": {
                "ok": True,
                "stdout": "hello\n",
                "stderr": "",
                "result_repr": "42",
                "fresh_session": True,
                "pyodide_version": "314.0.4",
                "python_version": "3.14.0",
                "images": [TINY_PNG_B64, "<script>alert(1)</script>", 123],
                "notes": ["a note"],
            },
        }

    context = await make_context(datasette_instance, browser_task_callback=callback)
    output = json.loads(
        await _execute_python(
            datasette_instance, {"id": "user"}, context, code="print('hello')\n42"
        )
    )
    assert output["ok"] is True
    assert output["stdout"] == "hello\n"
    assert output["result_repr"] == "42"
    assert output["fresh_session"] is True
    assert output["notes"] == ["a note"]
    assert output["pyodide_version"] == "314.0.4"
    # Only the valid base64 image survives, rendered via _html
    assert output["images_shown_to_user"] == 1
    assert output["_html"].count("<img") == 1
    assert TINY_PNG_B64 in output["_html"]
    assert "alert(1)" not in output["_html"]
    # Raw images are never handed to the model
    assert "images" not in output

    # The browser task was issued with the code in the payload
    assert seen["payload"] == {"code": "print('hello')\n42"}
    assert seen["label"] == "Running Python in your browser"
    assert seen["timeout_ms"] == 120_000


@pytest.mark.asyncio
async def test_execute_python_python_error_passes_through(datasette_instance):
    from datasette_agent.python_tools import _execute_python

    def callback(task):
        return {
            "ok": True,
            "result": {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "error": "Traceback (most recent call last):\n...ZeroDivisionError",
                "fresh_session": False,
                "images": [],
            },
        }

    context = await make_context(datasette_instance, browser_task_callback=callback)
    output = json.loads(
        await _execute_python(datasette_instance, {"id": "user"}, context, code="1/0")
    )
    assert output["ok"] is False
    assert "ZeroDivisionError" in output["error"]
    assert output["fresh_session"] is False
    assert "_html" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope,expected_error,expected_outcome",
    (
        (
            {
                "ok": False,
                "error": {"message": "Browser task timed out before completing"},
                "outcome": "expired",
            },
            "Browser task timed out before completing",
            "expired",
        ),
        (
            {
                "ok": False,
                "error": {"message": "Cancelled by the user"},
                "outcome": "cancelled",
            },
            "Cancelled by the user",
            "cancelled",
        ),
    ),
)
async def test_execute_python_infrastructure_failures(
    datasette_instance, envelope, expected_error, expected_outcome
):
    from datasette_agent.python_tools import _execute_python

    context = await make_context(
        datasette_instance, browser_task_callback=lambda task: envelope
    )
    output = json.loads(
        await _execute_python(datasette_instance, {"id": "user"}, context, code="1")
    )
    assert output["ok"] is False
    assert output["error"] == expected_error
    assert output["outcome"] == expected_outcome


@pytest.mark.asyncio
async def test_execute_python_without_browser_support(datasette_instance):
    from datasette_agent.python_tools import _execute_python

    context = await make_context(datasette_instance, supports_browser_tasks=False)
    output = json.loads(
        await _execute_python(datasette_instance, {"id": "user"}, context, code="1")
    )
    assert output["ok"] is False
    assert "connected browser" in output["error"]


@pytest.mark.asyncio
async def test_execute_python_timeout_clamped(datasette_instance):
    from datasette_agent.python_tools import MAX_TIMEOUT_SECONDS, _execute_python

    seen = {}

    def callback(task):
        seen.update(task)
        return {"ok": True, "result": {"ok": True, "stdout": "", "stderr": ""}}

    context = await make_context(datasette_instance, browser_task_callback=callback)
    await _execute_python(
        datasette_instance, {"id": "user"}, context, code="1", timeout_seconds=99999
    )
    assert seen["timeout_ms"] == MAX_TIMEOUT_SECONDS * 1000

    await _execute_python(
        datasette_instance, {"id": "user"}, context, code="1", timeout_seconds=1
    )
    assert seen["timeout_ms"] == 10_000

    await _execute_python(
        datasette_instance,
        {"id": "user"},
        context,
        code="1",
        timeout_seconds="not-a-number",
    )
    assert seen["timeout_ms"] == 120_000


@pytest.mark.asyncio
async def test_execute_python_registered_as_agent_tool(datasette_instance):
    from datasette_agent.tools import get_agent_tools

    await datasette_instance.invoke_startup()
    tools = await get_agent_tools(datasette_instance)
    by_name = {tool.name: tool for tool in tools}
    assert "execute_python" in by_name
    assert "code" in by_name["execute_python"].input_schema["properties"]


def test_task_html_uses_runtime_and_placeholder():
    from datasette_agent.browser_tasks import TASK_ID_PLACEHOLDER
    from datasette_agent.python_tools import _TASK_HTML

    assert TASK_ID_PLACEHOLDER in _TASK_HTML
    assert "window.datasetteAgent.claimTask" in _TASK_HTML
    assert "window.datasetteAgentPython" in _TASK_HTML
    assert "completeTask" in _TASK_HTML


def test_conversation_template_loads_pyodide_runtime():
    template = (
        Path(__file__).parent.parent
        / "datasette_agent"
        / "templates"
        / "agent_conversation.html"
    ).read_text()
    assert "pyodide-runtime.js" in template


# ---- The embedded Python harness (extracted from pyodide-sandbox.js) ----


def extract_harness():
    source = (STATIC_DIR / "pyodide-sandbox.js").read_text()
    match = re.search(r"const HARNESS = `(.*?)`;", source, re.DOTALL)
    assert match, "HARNESS template literal not found"
    raw = match.group(1)
    # Template-literal hazards: a stray backtick or ${ would corrupt or
    # interpolate the Python source.
    assert "${" not in raw
    # Apply JS escape interpretation (only \\ sequences are used)
    return raw.replace("\\\\", "\\")


async def _fake_eval_code_async(code, globals=None, filename="<exec>"):
    """Local stand-in for pyodide.code.eval_code_async: execute
    statements, return the value of a trailing expression, allow
    top-level await - compiled under the caller-provided filename
    like pyodide."""
    flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    module = ast.parse(code, filename=filename)
    last_expr = None
    if module.body and isinstance(module.body[-1], ast.Expr):
        last_expr = ast.Expression(module.body.pop().value)
        ast.copy_location(last_expr, last_expr.body)
    if module.body:
        outcome = eval(compile(module, filename, "exec", flags=flags), globals)
        if inspect.iscoroutine(outcome):
            await outcome
    if last_expr is not None:
        ast.fix_missing_locations(last_expr)
        outcome = eval(compile(last_expr, filename, "eval", flags=flags), globals)
        if inspect.iscoroutine(outcome):
            outcome = await outcome
        return outcome
    return None


@pytest.fixture
def harness_namespace(monkeypatch):
    """Execute the harness in a namespace with a fake pyodide module and
    a recording SQL bridge."""
    pyodide_module = types.ModuleType("pyodide")
    code_module = types.ModuleType("pyodide.code")
    code_module.eval_code_async = _fake_eval_code_async
    pyodide_module.code = code_module
    monkeypatch.setitem(sys.modules, "pyodide", pyodide_module)
    monkeypatch.setitem(sys.modules, "pyodide.code", code_module)

    namespace = {}
    exec(extract_harness(), namespace)

    bridge_calls = []
    bridge_responses = []

    async def bridge(database, sql, params_json):
        bridge_calls.append(
            {"database": database, "sql": sql, "params": json.loads(params_json)}
        )
        return json.dumps(bridge_responses.pop(0))

    namespace["_datasette_agent_sql_bridge"] = bridge
    namespace["_bridge_calls"] = bridge_calls
    namespace["_bridge_responses"] = bridge_responses
    return namespace


@pytest.mark.asyncio
async def test_harness_runs_code_and_captures_output(harness_namespace):
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("print('hi')\nx = 2\nx + 40"))
    assert result["ok"] is True
    assert result["stdout"] == "hi\n"
    assert result["stderr"] == ""
    assert result["result_repr"] == "42"
    assert result["images"] == []

    # State persists into the next run
    result2 = json.loads(await run("x * 2"))
    assert result2["result_repr"] == "4"


@pytest.mark.asyncio
async def test_harness_formats_user_tracebacks(harness_namespace):
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("def boom():\n    return 1 / 0\nboom()"))
    assert result["ok"] is False
    assert "ZeroDivisionError" in result["error"]
    assert '<session>' in result["error"]
    # Harness frames are trimmed from the traceback
    assert "eval_code_async" not in result["error"]
    assert "_agent_run" not in result["error"]


@pytest.mark.asyncio
async def test_harness_truncates_large_output(harness_namespace):
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("print('x' * 50000)"))
    assert len(result["stdout"]) < 25000
    assert "truncated" in result["stdout"]


@pytest.mark.asyncio
async def test_harness_execute_sql_success(harness_namespace):
    harness_namespace["_bridge_responses"].append(
        {
            "status": 200,
            "body": json.dumps(
                {"ok": True, "rows": [{"n": 1}, {"n": 2}], "truncated": False}
            ),
        }
    )
    run = harness_namespace["_agent_run"]
    result = json.loads(
        await run(
            "rows = await execute_sql('data', 'select n from t where n > :min',"
            " {'min': 0})\nlen(rows)"
        )
    )
    assert result["ok"] is True
    assert result["result_repr"] == "2"
    assert harness_namespace["_bridge_calls"] == [
        {
            "database": "data",
            "sql": "select n from t where n > :min",
            "params": {"min": 0},
        }
    ]


@pytest.mark.asyncio
async def test_harness_execute_sql_truncation_warns(harness_namespace):
    harness_namespace["_bridge_responses"].append(
        {
            "status": 200,
            "body": json.dumps({"ok": True, "rows": [{"n": 1}], "truncated": True}),
        }
    )
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("await execute_sql('data', 'select n from t')"))
    assert result["ok"] is True
    assert "truncated" in result["stderr"]


@pytest.mark.asyncio
async def test_harness_execute_sql_error_raises(harness_namespace):
    harness_namespace["_bridge_responses"].append(
        {
            "status": 400,
            "body": json.dumps(
                {"ok": False, "error": "Statement must be a SELECT"}
            ),
        }
    )
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("await execute_sql('data', 'update t set n = 1')"))
    assert result["ok"] is False
    assert "Statement must be a SELECT" in result["error"]
    assert "HTTP 400" in result["error"]


@pytest.mark.asyncio
async def test_harness_execute_sql_rejects_bad_params(harness_namespace):
    run = harness_namespace["_agent_run"]
    result = json.loads(await run("await execute_sql('data', 'select 1', [1, 2])"))
    assert result["ok"] is False
    assert "params must be a dict" in result["error"]


# ---- images_html validation ----


def test_images_html_only_renders_clean_base64():
    from datasette_agent.python_tools import images_html

    tags = images_html(
        [
            TINY_PNG_B64,
            'x" onerror="alert(1)',
            "not base64!!",
            None,
            "A" * 600_000,
        ]
    )
    assert len(tags) == 1
    assert TINY_PNG_B64 in tags[0]
    assert "onerror" not in "".join(tags)
