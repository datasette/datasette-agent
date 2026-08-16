"""The execute_python tool: run Python in a Pyodide interpreter in the
user's browser.

The heavy lifting happens client-side. This module's task HTML is a
thin shim that claims the browser task and hands the code to the
persistent per-page runtime (static/pyodide-runtime.js), which lazily
boots Pyodide 314.0.4 from the jsDelivr CDN inside a sandboxed,
opaque-origin iframe (static/pyodide-sandbox.js). The interpreter
persists across tool calls for the lifetime of the chat page, giving
each conversation a durable Python session; a page reload starts a
fresh one, reported back via fresh_session.

Security model (enforced client-side, documented here because this is
the module people will read first):

- The iframe is sandbox="allow-scripts" WITHOUT allow-same-origin, so
  Python code runs with an opaque origin: document.cookie throws, and
  fetch() carries no credentials and cannot reach this Datasette
  server at all (no CORS headers). It can only touch its own hidden,
  detached DOM.
- Database access goes exclusively through the awaitable Python
  function execute_sql(database, sql, params=None), bridged over
  postMessage to the chat page, which fetches the read-only
  /{database}/-/query.json route with the user's own cookies - so the
  user's execute-sql permissions and Datasette's SELECT-only
  validation apply to every query.
"""

import json
import re

from .browser_tasks import MAX_TIMEOUT_MS, BrowserTasksNotSupported
from .tools import AgentTool

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = (MAX_TIMEOUT_MS // 1000) - 30  # margin under the task cap

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MAX_IMAGE_CHARS = 500_000

# Result keys forwarded from the browser harness to the model.
_RESULT_KEYS = (
    "ok",
    "stdout",
    "stderr",
    "result_repr",
    "error",
    "notes",
    "fresh_session",
    "pyodide_version",
    "python_version",
)

_EXECUTE_PYTHON_DESCRIPTION = """
Execute Python code in a sandboxed Pyodide (CPython on WebAssembly)
interpreter running in the user's browser. Use it for data analysis and
cleaning tasks that SQL alone cannot handle.

- The interpreter persists across calls: variables, functions and imports
  defined in one call are available in later calls. If a result reports
  fresh_session: true, a new interpreter was started (for example after a
  page reload) and previous state is gone - re-run any setup you need.
- An awaitable built-in `await execute_sql(database, sql, params=None)`
  runs a read-only SELECT through the Datasette JSON API with the user's
  permissions and returns a list of row dicts. Named :param placeholders
  are filled from the params dict. Results truncate at the server row limit
  (default 1000) with a warning on stderr. Prefer SQL for filtering and
  aggregation; use Python for what SQL can't do.
- Top-level await is allowed. You receive the repr of the final expression
  (result_repr) plus captured stdout and stderr, each size-capped.
- Scientific packages bundled with Pyodide (pandas, numpy, scipy,
  matplotlib, scikit-learn and more) load automatically on import - the
  first import downloads them from a CDN. Pure-Python packages can be
  installed with: import micropip; await micropip.install("name").
- matplotlib figures are captured as PNG images and shown to the user
  automatically; you see images_shown_to_user instead of the pixels. Do
  not call plt.show().
- The sandbox has no cookies and no network path to Datasette other than
  execute_sql. It shares the page's thread: avoid unbounded loops, since a
  runaway computation can only be stopped by the task deadline.
- The first call in a session may be slow while the interpreter (about
  10 MB) downloads.
""".strip()

# Claims the task, feeds the code to the persistent page runtime, and
# posts back whatever it returns. Python-level failures (exceptions in
# the user code) still complete with ok: true - the harness result
# carries its own ok flag plus the traceback, which the model should
# see as ordinary output. Envelope ok: false is reserved for
# infrastructure failures: runtime missing, boot failure, timeout.
_TASK_HTML = """
<div hidden>
<script type="module">
const taskId = "__DATASETTE_TASK_ID__";
const claimed = await window.datasetteAgent.claimTask(taskId);
if (claimed.ok) {
  try {
    const runtime = window.datasetteAgentPython;
    if (!runtime) {
      throw new Error(
        "The Python runtime is not loaded in this page - " +
        "reload the conversation and try again"
      );
    }
    const budget = Math.max(10000, claimed.timeoutMs - 5000);
    const result = await runtime.execute(claimed.payload.code, budget);
    await window.datasetteAgent.completeTask(taskId, {ok: true, result});
  } catch (err) {
    await window.datasetteAgent.completeTask(taskId, {
      ok: false,
      error: {message: String((err && err.message) || err)},
    });
  }
}
</script>
</div>
"""


def images_html(images):
    """Render harness-captured figures as <img> tags for the _html side
    channel. The strings crossed the trust boundary from the browser,
    so only clean base64 is ever interpolated into markup."""
    tags = []
    for encoded in images:
        if not isinstance(encoded, str):
            continue
        if len(encoded) > _MAX_IMAGE_CHARS or not _BASE64_RE.match(encoded):
            continue
        tags.append(
            '<img class="agent-python-figure" alt="Figure generated by Python" '
            'style="max-width: 100%;" src="data:image/png;base64,{}">'.format(encoded)
        )
    return tags


def _clamp_timeout_seconds(timeout_seconds):
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    return max(10, min(timeout_seconds, MAX_TIMEOUT_SECONDS))


async def _execute_python(
    datasette, actor, context, code, timeout_seconds=DEFAULT_TIMEOUT_SECONDS
):
    timeout_ms = _clamp_timeout_seconds(timeout_seconds) * 1000
    try:
        outcome = await context.browser_task(
            html=_TASK_HTML,
            payload={"code": code},
            label="Running Python in your browser",
            timeout_ms=timeout_ms,
        )
    except BrowserTasksNotSupported:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "execute_python is only available in the web chat with a "
                    "connected browser"
                ),
            }
        )
    if not isinstance(outcome, dict):
        return json.dumps({"ok": False, "error": "Malformed browser task result"})
    if not outcome.get("ok"):
        error = outcome.get("error")
        message = None
        if isinstance(error, dict):
            message = error.get("message")
        elif error:
            message = str(error)
        return json.dumps(
            {
                "ok": False,
                "error": message or "Browser task failed",
                "outcome": outcome.get("outcome"),
            }
        )
    result = outcome.get("result")
    if not isinstance(result, dict):
        return json.dumps(
            {"ok": False, "error": "Malformed result from the browser runtime"}
        )
    payload = {key: result[key] for key in _RESULT_KEYS if key in result}
    payload.setdefault("ok", False)
    images = result.get("images")
    tags = images_html(images) if isinstance(images, list) else []
    if tags:
        payload["images_shown_to_user"] = len(tags)
        payload["_html"] = "".join(tags)
    return json.dumps(payload)


def get_python_tools():
    return [
        AgentTool(
            name="execute_python",
            description=_EXECUTE_PYTHON_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute in the session",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": DEFAULT_TIMEOUT_SECONDS,
                        "description": (
                            "Give up after this many seconds (10-{}). Increase "
                            "for long computations or large package downloads."
                        ).format(MAX_TIMEOUT_SECONDS),
                    },
                },
                "required": ["code"],
            },
            fn=_execute_python,
        )
    ]
