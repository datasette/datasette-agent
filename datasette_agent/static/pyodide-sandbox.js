// Runs INSIDE the sandboxed iframe created by pyodide-runtime.js -
// an opaque-origin document with no cookies, no credentialed fetch
// and no access to the chat page. It boots Pyodide from the CDN,
// installs a small Python harness, and services "execute" messages
// from the parent page.
//
// The only channel back to Datasette is the SQL bridge: Python's
// execute_sql() posts {type: "sql"} to the parent, which performs a
// credentialed fetch against the read-only /{db}/-/query.json route
// and posts the response body back. Nothing in this frame ever sees
// a cookie or a credential.
(() => {
  "use strict";

  const PYODIDE_VERSION = "314.0.4";
  const CDN_BASE =
    "https://cdn.jsdelivr.net/pyodide/v" + PYODIDE_VERSION + "/full/";

  // The Python harness. Kept free of backtick, ${ and </script
  // sequences so it can live in this template literal.
  const HARNESS = `
import base64
import contextlib
import io
import json
import os
import sys
import traceback

# matplotlib must render off-screen: the Agg backend writes PNG bytes
# instead of expecting a visible canvas. Set before it is imported.
os.environ.setdefault("MPLBACKEND", "AGG")

_MAX_OUTPUT_CHARS = 20000
_MAX_REPR_CHARS = 5000
_MAX_FIGURES = 6
_MAX_IMAGE_BUDGET = 350000  # total base64 chars across all figures

# The persistent namespace user code runs in - survives across
# execute_python calls for the lifetime of this interpreter.
_agent_globals = {"__name__": "__main__"}


def _truncate(text, limit):
    if len(text) > limit:
        return text[:limit] + "\\n... [truncated, {} characters total]".format(
            len(text)
        )
    return text


async def execute_sql(database, sql, params=None):
    """Run a read-only SQL query against a Datasette database.

    Returns a list of row dicts. params is an optional dict of named
    parameters referenced as :name placeholders in the SQL. Results
    are truncated at the server's maximum returned rows (a warning is
    printed to stderr when that happens).
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise TypeError("params must be a dict of named parameter values")
    response = json.loads(
        await _datasette_agent_sql_bridge(
            str(database), str(sql), json.dumps(params)
        )
    )
    try:
        data = json.loads(response["body"])
    except Exception:
        data = None
    if response["status"] != 200 or not isinstance(data, dict) or not data.get("ok"):
        message = None
        if isinstance(data, dict):
            message = data.get("error")
        if not message:
            message = (response.get("body") or "")[:500]
        raise RuntimeError(
            "SQL query failed (HTTP {}): {}".format(response["status"], message)
        )
    rows = data.get("rows") or []
    if data.get("truncated"):
        print(
            "Warning: SQL results were truncated at {} rows".format(len(rows)),
            file=sys.stderr,
        )
    return rows


_agent_globals["execute_sql"] = execute_sql


def _collect_images(notes):
    if "matplotlib" not in sys.modules:
        return []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return []
    images = []
    budget = _MAX_IMAGE_BUDGET
    fignums = plt.get_fignums()
    if len(fignums) > _MAX_FIGURES:
        notes.append(
            "Only the first {} of {} matplotlib figures were captured".format(
                _MAX_FIGURES, len(fignums)
            )
        )
    for num in fignums[:_MAX_FIGURES]:
        try:
            figure = plt.figure(num)
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", bbox_inches="tight")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception as ex:
            notes.append("Could not render figure {}: {}".format(num, ex))
            continue
        if len(encoded) > budget:
            notes.append(
                "Figure {} was dropped: too large for the result envelope".format(
                    num
                )
            )
            continue
        budget -= len(encoded)
        images.append(encoded)
    plt.close("all")
    return images


def _format_error():
    lines = traceback.format_exception(*sys.exc_info())
    # Drop harness frames so the traceback starts at the user's code,
    # compiled under the distinct filename "<session>" (the harness
    # itself runs as "<exec>", so that name would be ambiguous).
    for index, line in enumerate(lines):
        if line.lstrip().startswith('File "<session>"'):
            return "Traceback (most recent call last):\\n" + "".join(lines[index:])
    return "".join(lines)


async def _agent_run(code):
    from pyodide.code import eval_code_async

    stdout = io.StringIO()
    stderr = io.StringIO()
    payload = {"ok": True}
    notes = []
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = await eval_code_async(
                code, globals=_agent_globals, filename="<session>"
            )
        if result is not None:
            payload["result_repr"] = _truncate(repr(result), _MAX_REPR_CHARS)
    except BaseException:
        payload["ok"] = False
        payload["error"] = _truncate(_format_error(), _MAX_OUTPUT_CHARS)
    payload["stdout"] = _truncate(stdout.getvalue(), _MAX_OUTPUT_CHARS)
    payload["stderr"] = _truncate(stderr.getvalue(), _MAX_OUTPUT_CHARS)
    payload["images"] = _collect_images(notes)
    if notes:
        payload["notes"] = notes
    return json.dumps(payload)
`;

  let pyodide = null;
  let runFn = null;
  let sqlCounter = 0;
  const pendingSql = new Map();

  // Called from Python (execute_sql). Resolves with a JSON string of
  // {status, body} once the parent posts the response back.
  function sqlBridge(database, sql, paramsJson) {
    return new Promise((resolve, reject) => {
      const id = "sql-" + ++sqlCounter;
      pendingSql.set(id, { resolve, reject });
      window.parent.postMessage(
        {
          type: "sql",
          id,
          database: String(database),
          sql: String(sql),
          params: JSON.parse(paramsJson),
        },
        "*"
      );
    });
  }

  async function runExecution(message) {
    try {
      if (!runFn) {
        throw new Error("Python interpreter is not ready");
      }
      const code = String(message.code);
      try {
        // Fetch any bundled packages (pandas, numpy, matplotlib,
        // micropip, ...) the code imports before running it.
        await pyodide.loadPackagesFromImports(code);
      } catch (err) {
        // Fall through: the Python import will fail with a clearer
        // error message than the loader's.
      }
      const payloadJson = await runFn(code);
      window.parent.postMessage(
        {
          type: "result",
          id: message.id,
          ok: true,
          payload: JSON.parse(payloadJson),
        },
        "*"
      );
    } catch (err) {
      window.parent.postMessage(
        {
          type: "result",
          id: message.id,
          ok: false,
          error: String((err && err.message) || err),
        },
        "*"
      );
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "sql-result") {
      const entry = pendingSql.get(message.id);
      if (!entry) return;
      pendingSql.delete(message.id);
      if (message.error !== undefined) {
        entry.reject(new Error(String(message.error)));
      } else {
        entry.resolve(
          JSON.stringify({ status: message.status, body: message.body })
        );
      }
    } else if (message.type === "execute") {
      runExecution(message);
    }
  });

  async function boot() {
    try {
      pyodide = await loadPyodide({ indexURL: CDN_BASE });
      pyodide.globals.set("_datasette_agent_sql_bridge", sqlBridge);
      await pyodide.runPythonAsync(HARNESS);
      runFn = pyodide.globals.get("_agent_run");
      const pythonVersion = pyodide.runPython(
        "__import__('sys').version.split()[0]"
      );
      window.parent.postMessage(
        {
          type: "ready",
          pyodideVersion: pyodide.version,
          pythonVersion: pythonVersion,
        },
        "*"
      );
    } catch (err) {
      window.parent.postMessage(
        {
          type: "boot-error",
          error: String((err && err.message) || err),
        },
        "*"
      );
    }
  }

  const script = document.createElement("script");
  script.src = CDN_BASE + "pyodide.js";
  script.onload = boot;
  script.onerror = () => {
    window.parent.postMessage(
      { type: "boot-error", error: "Failed to load Pyodide from " + CDN_BASE },
      "*"
    );
  };
  document.head.appendChild(script);
})();
