// Persistent per-page Python (Pyodide) runtime for the execute_python
// agent tool.
//
// Task HTML is hermetic - torn down as soon as its task completes - so
// a Python session that survives across tool calls has to live here,
// in the chat page runtime. The interpreter itself never runs in this
// page: it runs inside a hidden, sandboxed iframe created lazily on
// first use.
//
// Security model
// --------------
// The iframe is created with sandbox="allow-scripts" and WITHOUT
// allow-same-origin, so it gets an opaque origin. That is the whole
// point:
//
// - document.cookie throws inside it - Python code can never read the
//   user's Datasette cookies.
// - fetch() from it is cross-origin and credential-less: requests to
//   this Datasette server carry no cookies and are blocked by CORS
//   anyway. The only sanctioned path to the database is the SQL
//   bridge below, where THIS page (holding the user's credentials)
//   performs the fetch against the read-only /{db}/-/query.json route
//   and posts only the response body back.
// - The frame is display:none and has no allow-top-navigation /
//   allow-popups / allow-modals, so code in it cannot draw UI, open
//   windows or navigate this page. Its own detached DOM is the only
//   DOM it can touch.
//
// Messages from the frame are only trusted when event.source is the
// frame's own contentWindow, and the SQL bridge validates every field
// before building a URL from it.
(() => {
  "use strict";

  const SANDBOX_SRC = "/-/static-plugins/datasette-agent/pyodide-sandbox.js";
  const BOOT_TIMEOUT_MS = 120000;

  let iframe = null;
  let readyPromise = null; // resolves when the sandbox posts "ready"
  let readyResolve = null;
  let readyReject = null;
  let bootInfo = null; // {pyodideVersion, pythonVersion} from "ready"
  let hasExecuted = false; // false until the first successful execute()
  let executionCounter = 0;
  const pendingExecutions = new Map(); // id -> {resolve, reject}
  let queue = Promise.resolve(); // execute() calls are serialized

  // Datasette's tilde-encoding for path segments: [A-Za-z0-9_-] pass
  // through, space becomes "+", every other UTF-8 byte becomes ~HEX.
  function tildeEncode(value) {
    const bytes = new TextEncoder().encode(String(value));
    let out = "";
    for (const b of bytes) {
      const ch = String.fromCharCode(b);
      if (/[A-Za-z0-9_-]/.test(ch)) {
        out += ch;
      } else if (ch === " ") {
        out += "+";
      } else {
        out += "~" + b.toString(16).toUpperCase().padStart(2, "0");
      }
    }
    return out;
  }

  function destroySandbox(reason) {
    if (readyReject) {
      readyReject(new Error(reason || "Python sandbox was shut down"));
    }
    for (const entry of pendingExecutions.values()) {
      entry.reject(new Error(reason || "Python sandbox was shut down"));
    }
    pendingExecutions.clear();
    if (iframe) iframe.remove();
    iframe = null;
    readyPromise = null;
    readyResolve = null;
    readyReject = null;
    bootInfo = null;
    hasExecuted = false;
  }

  function ensureSandbox() {
    if (readyPromise) return readyPromise;
    iframe = document.createElement("iframe");
    // allow-scripts only: no allow-same-origin means an opaque origin -
    // no cookies, no credentialed fetch, no reaching into this page.
    iframe.setAttribute("sandbox", "allow-scripts");
    iframe.style.display = "none";
    iframe.setAttribute("aria-hidden", "true");
    iframe.setAttribute("title", "Python sandbox");
    // srcdoc documents inherit this page's base URL, so the relative
    // static path resolves; the script runs with the opaque origin.
    iframe.srcdoc =
      '<!DOCTYPE html><html><head><meta charset="utf-8">' +
      '<script src="' +
      SANDBOX_SRC +
      '"><\/script></head><body></body></html>';
    readyPromise = new Promise((resolve, reject) => {
      readyResolve = resolve;
      readyReject = reject;
      setTimeout(() => {
        reject(new Error("Timed out loading the Python interpreter"));
      }, BOOT_TIMEOUT_MS);
    });
    // Swallow unhandled rejection warnings for the stored promise; the
    // real await in execute() still observes the rejection.
    readyPromise.catch(() => {});
    document.body.appendChild(iframe);
    return readyPromise;
  }

  // ---- SQL bridge ----
  //
  // The sandbox posts {type: "sql", id, database, sql, params}; this
  // page fetches Datasette's read-only query endpoint with the user's
  // own credentials and posts back {type: "sql-result", id, status,
  // body}. The endpoint enforces SELECT-only server-side, and the
  // user's execute-sql permissions apply because the request carries
  // their cookies. Nothing here lets the sandbox choose an arbitrary
  // URL: the path is built from a validated database name and the
  // querystring from validated named parameters.
  const PARAM_NAME_RE = /^[A-Za-z][A-Za-z0-9_]*$/;

  async function handleSqlRequest(message) {
    const reply = { type: "sql-result", id: message.id };
    try {
      const { database, sql, params } = message;
      if (typeof database !== "string" || !database) {
        throw new Error("database must be a non-empty string");
      }
      if (typeof sql !== "string" || !sql) {
        throw new Error("sql must be a non-empty string");
      }
      const search = new URLSearchParams();
      search.set("sql", sql);
      if (params !== undefined && params !== null) {
        if (typeof params !== "object" || Array.isArray(params)) {
          throw new Error("params must be an object of named parameters");
        }
        for (const [name, value] of Object.entries(params)) {
          if (!PARAM_NAME_RE.test(name) || name === "sql") {
            throw new Error("Invalid SQL parameter name: " + name);
          }
          const type = typeof value;
          if (type !== "string" && type !== "number" && type !== "boolean") {
            throw new Error(
              "SQL parameter " + name + " must be a string, number or boolean"
            );
          }
          search.set(name, String(value));
        }
      }
      const url =
        "/" + tildeEncode(database) + "/-/query.json?" + search.toString();
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      reply.status = response.status;
      reply.body = await response.text();
    } catch (err) {
      reply.error = String((err && err.message) || err);
    }
    if (iframe && iframe.contentWindow) {
      // The sandbox has an opaque origin, so "*" is the only usable
      // targetOrigin - safe here because we post to a direct window
      // reference, not a name or opener.
      iframe.contentWindow.postMessage(reply, "*");
    }
  }

  window.addEventListener("message", (event) => {
    if (!iframe || event.source !== iframe.contentWindow) return;
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "ready") {
      bootInfo = {
        pyodide_version: String(message.pyodideVersion || ""),
        python_version: String(message.pythonVersion || ""),
      };
      if (readyResolve) readyResolve();
    } else if (message.type === "boot-error") {
      const error = new Error(
        "Failed to start the Python interpreter: " + message.error
      );
      if (readyReject) readyReject(error);
      destroySandbox(error.message);
    } else if (message.type === "result") {
      const entry = pendingExecutions.get(message.id);
      if (!entry) return;
      pendingExecutions.delete(message.id);
      if (message.ok) {
        entry.resolve(message.payload);
      } else {
        entry.reject(new Error(String(message.error)));
      }
    } else if (message.type === "sql") {
      handleSqlRequest(message);
    }
  });

  async function executeInSandbox(code, timeoutMs) {
    const budget = Math.max(10000, Number(timeoutMs) || 120000);
    const deadline = Date.now() + budget;
    const freshSession = !hasExecuted;
    let timer = null;
    const timedOut = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(
          new Error(
            "Python execution timed out after " +
              Math.round(budget / 1000) +
              " seconds; the interpreter was restarted and session state was lost"
          )
        );
      }, budget);
    });
    try {
      await Promise.race([ensureSandbox(), timedOut]);
      const remaining = Math.max(1000, deadline - Date.now());
      const id = "exec-" + ++executionCounter;
      const resultPromise = new Promise((resolve, reject) => {
        pendingExecutions.set(id, { resolve, reject });
      });
      iframe.contentWindow.postMessage(
        { type: "execute", id, code: String(code), timeoutMs: remaining },
        "*"
      );
      const payload = await Promise.race([resultPromise, timedOut]);
      hasExecuted = true;
      return Object.assign(
        { fresh_session: freshSession },
        bootInfo || {},
        payload
      );
    } catch (err) {
      // A timed-out interpreter may be wedged in a busy loop - tear it
      // down so the next call starts clean. (If the loop is truly
      // spinning the frame shares this thread and the timer only fires
      // once it yields; the server-side task deadline is the backstop.)
      if (String(err).indexOf("timed out") !== -1) {
        destroySandbox(String((err && err.message) || err));
      }
      throw err;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  // Public API used by execute_python task HTML. Executions are
  // serialized: the interpreter is single-threaded and calls share one
  // global namespace.
  function execute(code, timeoutMs) {
    const run = queue.then(() => executeInSandbox(code, timeoutMs));
    queue = run.then(
      () => {},
      () => {}
    );
    return run;
  }

  window.datasetteAgentPython = { execute };
})();
