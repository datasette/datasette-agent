# datasette-agent

[![PyPI](https://img.shields.io/pypi/v/datasette-agent.svg)](https://pypi.org/project/datasette-agent/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-agent?include_prereleases&label=changelog)](https://github.com/datasette/datasette-agent/releases)
[![Tests](https://github.com/datasette/datasette-agent/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-agent/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-agent/blob/main/LICENSE)

An LLM-powered agent assistant for Datasette

See [Datasette Agent, an extensible AI assistant for Datasette](https://datasette.io/blog/2026/datasette-agent/) for more about this project, including tips on running it on your own machine.

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-agent
```
## Usage

Visit `/-/agent` to start a conversation with the chat assistant.

The agent uses [datasette-llm](https://github.com/datasette/datasette-llm) to call language models. Configure a default model for it before visiting `/-/agent`, for example in `datasette.yml`:

```yaml
plugins:
  datasette-llm:
    default_model: gpt-5.4-mini
```

### Choosing a model

When more than one model is available, the new-chat form on `/-/agent` (and the "Start a new agent chat" section on other Datasette pages) shows a model picker. The choice is made when a conversation starts and is pinned for that conversation's whole life - later messages keep using the same model even if the configured default changes, and the model cannot be switched mid-conversation. Start a new chat to use a different model. The conversation page shows the model it is using next to the title.

The list comes from datasette-llm's model list for the `agent` purpose: models whose API key is available, minus any excluded by datasette-llm's allowlist and blocklist configuration, and minus any that cannot call tools (the agent needs tool calling). The configured default is preselected. To offer a specific set of models for the agent, configure the purpose in `datasette.yml`:

```yaml
plugins:
  datasette-llm:
    purposes:
      agent:
        model: gpt-5.5
        models:
        - gpt-5.5
        - gpt-5.4-mini
        - claude-sonnet-5
```

Here `model` is the default for the agent and `models` is the allowlist offered in the picker. Without a `purposes.agent` section the picker offers every tool-capable model that datasette-llm considers available, with `default_model` preselected.

The same list is available as JSON from `/-/agent/api/models`, and `POST /-/agent/api/conversations` accepts an optional `{"model": "..."}` to pin a model when creating a conversation programmatically. Background agents and explorer reports always use the default model.

The "Explore with AI agent" entries that appear in the database and table action menus launch a background agent that explores the selected database or table and writes a report. Reports live under `/-/agent/explore/`.

Visit `/-/agent/background` to launch background agents directly. Each one is given a goal and runs toward it without further input. The listing includes a Stop button for cancelling agents that are still running.

### Saving queries

The agent has a built-in `save_query` tool that saves SQL it has written as a [Datasette stored query](https://docs.datasette.io/en/latest/sql_queries.html). The query can be read-only or write SQL - Datasette analyzes it to decide which, and named `:parameters` become form fields on the saved query page.

Saving always requires human approval: the agent shows you the full SQL plus the proposed name, database and visibility, and nothing is stored until you click Yes. Validation and persistence run through Datasette's own `/-/queries/analyze` and `/-/queries/store` endpoints as the requesting actor, so the actor needs `execute-sql` and `store-query` on the target database (plus the relevant row permissions for write queries) - the same rules as the query creation web UI. Saved queries default to private.

### SQL parameters

The `sql_query` tool accepts optional named parameters as an array of name/value entries:

```json
{
  "database": "demo",
  "sql": "select * from items where name = :name and qty >= :qty",
  "params": [
    {"name": "name", "value": "apple"},
    {"name": "qty", "value": 3}
  ]
}
```

Parameter names omit the placeholder prefix and must be unique. Values can be strings, numbers, booleans or null. Each statement in `execute_write_sql` accepts the same `params` format. Both tools also accept parameter dictionaries, preserving compatibility with saved write-tool calls.

### Executing write SQL

The agent also has a built-in `execute_write_sql` tool that can run one or more ordered write SQL statements against a mutable database. It analyzes each statement first and asks the user for explicit approval in chat before anything runs.

The approval prompt shows the SQL, parameters, required permissions and destructive-operation warnings. Execution runs through Datasette's own `/-/execute-write` endpoint as the requesting actor, so the actor needs `execute-write-sql` on the target database plus the Datasette write permissions for the operations being performed. Statements run in order; if one fails, later statements are skipped and earlier successes are not rolled back. Use `sql_query` for read-only SQL.

### Permissions

This plugin registers three independent permissions:

- `datasette-agent` — required to use the chat assistant under `/-/agent`.
- `datasette-agent-explore` — required to see the "Explore with AI agent" entries in the database/table action menus and to use the explorer routes under `/-/agent/explore/`.
- `datasette-agent-background` — required to use the `spawn_background_agent` and `check_background_agent` tools from chat, and to access the `/-/agent/background` page and `/-/agent/api/background/*` endpoints. The background-agent endpoints require both `datasette-agent` and `datasette-agent-background`.

The three permissions are independent: an actor may hold any subset. The `--root` user holds all of them.

## Registering additional tools from plugins

Other Datasette plugins can register additional tools for the agent using the `register_agent_tools` plugin hook.

### Defining a tool

Create a Datasette plugin that implements the `register_agent_tools` hook, returning a list of `AgentTool` instances:

```python
from datasette import hookimpl
from datasette_agent.tools import AgentTool


@hookimpl
def register_agent_tools(datasette):
    return [
        AgentTool(
            name="my_tool",
            description="Description of what this tool does, used by the LLM to decide when to call it.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The query to run",
                    },
                    "style": {
                        "type": "string",
                        "enum": ["brief", "detailed"],
                        "description": "Output style",
                    },
                },
                "required": ["query"],
            },
            fn=my_tool_handler,
            # Optional: name a Datasette permission action that gates this tool.
            # required_permission="myplugin-write",
        ),
    ]
```

### Gating a tool with a permission

`AgentTool` accepts an optional `required_permission: str | None` field. When set, the agent harness calls `datasette.allowed(action=required_permission, actor=actor)` for the current actor before sending the tool list to the LLM. If the actor lacks the permission, the tool is filtered out of the list — the model never sees it and cannot call it. There is no runtime "permission denied" branch your `fn` needs to handle.

Your plugin is responsible for registering the action via Datasette's `register_actions` plugin hook:

```python
from datasette import hookimpl
from datasette.permissions import Action
from datasette_agent.tools import AgentTool


@hookimpl
def register_actions():
    return [
        Action(
            name="myplugin-write",
            description="Allow my plugin's write tools",
        ),
    ]


@hookimpl
def register_agent_tools(datasette):
    return [
        AgentTool(
            name="my_write_tool",
            description="Writes things",
            input_schema={"type": "object", "properties": {}},
            fn=my_write_handler,
            required_permission="myplugin-write",
        ),
    ]
```

For a working example see this plugin's own `spawn_background_agent` and `check_background_agent` tools, which use `required_permission="datasette-agent-background"`.

### Tool handler function

Each tool's `fn` must be an async function that accepts `datasette` and `actor` as keyword arguments, plus any parameters defined in `input_schema`. It must return a JSON string:

```python
import json


async def my_tool_handler(datasette, actor, query, style=None):
    # Do work here...
    return json.dumps({
        "result": "Tool output that the LLM will see",
    })
```

To render rich HTML inline in the chat UI, include an `_html` key in the returned JSON. Any top-level key whose name starts with `_` is removed before the tool result is sent to the LLM, so the HTML is shown to the user but not passed back to the model:

```python
return json.dumps({
    "_html": '<div class="my-widget">Rich content here</div>',
    "summary": "Widget rendered successfully",
})
```

### Asking the user questions from a tool

A tool can pause mid-execution and ask the human user a question. Declare a `context` parameter on your handler and call `await context.ask_user(...)`:

```python
import json


async def edit_files(datasette, actor, context, path):
    ok = await context.ask_user(
        "Is it OK to edit files in {}?".format(path)
    )
    if not ok:
        return json.dumps({"cancelled": True})
    mode = await context.ask_user(
        "How should I apply this?", options=["dry-run", "apply"]
    )
    note = await context.ask_user("Any notes?", free_text=True)
    # ... do the work ...
    return json.dumps({"edited": path, "mode": mode, "note": note})
```

Three kinds of question are supported:

- `await context.ask_user("Approve?")` - yes/no, returns a `bool`
- `await context.ask_user("Which?", options=["a", "b"])` - multiple choice, returns the selected `str`
- `await context.ask_user("Describe it", free_text=True)` - freeform, returns a `str`

Pass `html=` to display trusted HTML above the question - use this to show the user exactly what they are approving, for example the full SQL of a query inside a `<pre>` tag. Escape any interpolated content yourself (e.g. with `html.escape()`); the string is rendered as-is in the chat UI.

Pass `text=` to provide a plain-text version for terminal contexts such as `datasette agent chat`. If `text=` is provided without `html=`, the web chat displays the text too, HTML-escaped inside a `<pre>` block. The CLI displays `text=` when available; if only `html=` was provided, it prints that HTML directly.

When `ask_user()` has no answer yet it suspends the agent turn: the question is rendered as a form in the chat UI, and persisted to the internal database so it survives a server restart - the form re-renders when the conversation page is reloaded. Once the user answers, the tool function is **re-executed from the top**; previously answered questions return their stored answers immediately and execution proceeds past the `ask_user()` call. Because of this replay model you should call `ask_user()` *before* performing side effects.

The `context` object also exposes `context.actor`, `context.conversation_id`, `context.tool_name`, `context.arguments` and `context.tool_call_id`.

In contexts with no human watching - for example background agents, or CLI chat when terminal input is unavailable - `ask_user()` raises `QuestionsNotSupported`, which surfaces to the model as a tool error so it can proceed without input. Tools that only declare `datasette` and `actor` are unaffected by all of this.

### Running work in the user's browser from a tool

Some tools need code to run in the user's browser - screenshotting a rendered page, executing JavaScript against a live DOM, measuring layout. **Browser tasks** are the primitive for this: a tool hands the user's browser a unit of work, the agent turn suspends, and the tool receives the result when the browser posts it back - even if that takes minutes, a page reload or a server restart.

Declare a `context` parameter on your handler and call `await context.browser_task(...)`:

```python
import json


async def measure_page(datasette, actor, context, url):
    outcome = await context.browser_task(
        html=BROWSER_HARNESS_HTML,
        payload={"url": url, "token": make_capability_token(url)},
        label="Measuring {} in your browser".format(url),
        timeout_ms=30_000,
    )
    if not outcome["ok"]:
        return json.dumps({"error": outcome["error"], "outcome": outcome["outcome"]})
    return json.dumps({"measurements": outcome["result"]})
```

The four arguments:

- `html` - **trusted, server-authored HTML** rendered into the chat page. Unlike question `html=`, its `<script>` elements execute; this is the sanctioned script-execution path. Never interpolate model output or user input into it unescaped.
- `payload` - optional JSON handed to the executing page **exactly once**, through a one-shot claim (see below). Put per-run secrets here - tokens, capability URLs - never in `html`, because `html` is persisted for audit.
- `label` - human-visible status line shown next to a spinner (falls back to "Working in your browser…").
- `timeout_ms` - server-enforced deadline for the whole task, capped at 10 minutes.

The return value is the envelope the page posted - `{"ok": True, "result": ...}` or `{"ok": False, "error": {...}}` - plus an `"outcome"` key:

- `"completed"` - the page posted a result (which may itself report `ok: False`, e.g. a script error)
- `"expired"` - the deadline passed with no completion (crashed or closed tab)
- `"cancelled"` - the user clicked the **Skip this step** button that renders alongside every task

Failures come back as data, never exceptions, because your tool re-executes from the top on resume and handles them as ordinary results. The same replay model as `ask_user()` applies: when no result exists yet, `browser_task()` suspends the turn; on resume the tool function re-runs from the top and finished tasks return their stored envelopes immediately. Call `browser_task()` **before** performing side effects. Results are size-capped at 512 KB - tools with bigger appetites should store the data themselves and post a summary.

#### Writing the task HTML

Task HTML must claim the task to receive its payload, do its work, then complete. It talks to the runtime through the public `window.datasetteAgent` API - never by fetching endpoints by hand or reaching into the chat page's markup:

```python
BROWSER_HARNESS_HTML = """
<div hidden>
<script type="module">
const taskId = "__DATASETTE_TASK_ID__";
const claimed = await window.datasetteAgent.claimTask(taskId);
if (claimed.ok) {
  // claimed.payload is the payload= object; claimed.timeoutMs the deadline
  try {
    const result = await doTheWork(claimed.payload);
    await window.datasetteAgent.completeTask(taskId, {ok: true, result});
  } catch (err) {
    await window.datasetteAgent.completeTask(taskId, {
      ok: false, error: {message: String(err)},
    });
  }
}
// claimed.ok === false: another tab already claimed it, or the task is
// finished - render nothing, do nothing.
</script>
</div>
"""
```

The literal string `__DATASETTE_TASK_ID__` anywhere in your `html` is substituted with the real task id at render time. (Classic, non-module scripts can alternatively find it structurally via `document.currentScript.closest("[data-task-id]").dataset.taskId` - module scripts should use the placeholder, since `document.currentScript` is `null` inside them.)

The API:

- `claimTask(taskId)` - atomically claims the task and resolves `{ok: true, payload, timeoutMs}` **exactly once per task, ever**. Any later or concurrent claim - a duplicate tab, a reloaded page re-rendering conversation history - resolves `{ok: false, state}` and the caller should stand down. This claim gate is what makes script-bearing HTML safe to re-render: execution happens at most once no matter how many times the markup appears.
- `completeTask(taskId, envelope)` - posts `{ok, result?, error?}`. First write wins. The suspended turn resumes immediately and its events stream into the transcript on the same connection.
- `cancelTask(taskId)` - equivalent to the user clicking Skip.

Once a task leaves the pending state its HTML is torn down and never rendered again - reloading the conversation shows an inert one-line record instead. Keep task HTML hermetic: render, execute, complete, tear down. A tool that needs several rounds of browser work should issue several `browser_task()` calls in sequence.

#### Capability detection and testing

`context.supports_browser_tasks` is `True` in the web chat and `False` for background agents and CLI chat, where calling `browser_task()` raises `BrowserTasksNotSupported` - surfaced to the model as a tool error, like `QuestionsNotSupported`. Check the flag first if your tool has a browserless fallback.

For tests, pass a `browser_task_callback` when constructing the `ToolContext` (or through `make_llm_tools()`): it receives `{tool_name, html, payload, label, timeout_ms}` and returns the envelope directly, synchronously or as a coroutine, without touching the database or any browser. Envelopes returned this way are normalized with `outcome: "completed"` exactly like real completions, so your tool cannot tell the executors apart.

## Rendering custom HTML from tools

Tool plugins can render rich HTML inline in the chat UI by returning a JSON object with an `_html` key. The HTML is rendered directly in the conversation. The remaining keys are returned to the LLM as the tool result, with any key whose name starts with `_` removed first.

Example tool implementation:

```python
import json

async def _render_widget(datasette, actor, database, sql):
    html = (
        '<script src="/-/static-plugins/my-plugin/widget.js" type="module"></script>\n'
        '<my-widget>\n'
        f'<script type="application/json">{json.dumps({"database": database, "sql": sql})}</script>\n'
        '</my-widget>'
    )
    return json.dumps({
        "_html": html,
        "database": database,
        "sql": sql,
        "summary": "Widget rendered successfully",
    })
```

The `_html` value is inserted into the chat as raw HTML, so it can include custom elements, scripts, and styles. The other keys (`database`, `sql`, and `summary` in this example) are what the LLM receives as the tool result.

If your plugin runs SQL and displays the results in HTML, add a link below the rendered output using Datasette Agent's built-in SQL link styling:

```html
<p class="agent-sql-edit-link"><a href="/data/-/query?sql=select+1">View SQL query</a></p>
```

### Example plugins

- [datasette-agent-charts](https://github.com/datasette/datasette-agent-charts) - renders charts from SQL query results using Observable Plot
- [datasette-agent-openai-imagegen](https://github.com/datasette/datasette-agent-openai-imagegen) - generates images using OpenAI's image generation API

## CLI commands

### Interactive chat

Start an interactive chat session with the agent from the command line:

```bash
datasette agent chat mydata.db
```

You can pass multiple database files, use `:memory:` for an in-memory database, specify a model, or send a single prompt:

```bash
datasette agent chat mydata.db -m gpt-5.4-mini
datasette agent chat mydata.db -m gpt-5.4-mini -p "List all tables"
```

Options:

- `-p`, `--prompt` — Send a single prompt and exit (non-interactive mode)
- `-m`, `--model` — LLM model to use
- `--root` — Run as the Datasette root actor, allowing all permissions
- `--yes` — Automatically approve yes/no confirmation prompts
- `--unsafe` — Equivalent to `--root --yes`

By default the CLI runs as an actor called `cli` and respects Datasette permissions. Tools that ask for approval show a terminal prompt; for example, `execute_write_sql` shows a plain-text version of the SQL, parameters, permissions and warnings before asking for confirmation. Use `--yes` to skip yes/no confirmations, `--root` to run with root permissions, or `--unsafe` to do both.

### Listing available tools

To see all registered agent tools, grouped by plugin:

```bash
datasette agent tools
```

Output includes:

```
agent:
  list_databases_and_tables
    List all available databases and their tables
  describe_table
    Get column names, types, and foreign keys for a table
  sql_query
    Execute a read-only SQL query against a database
  execute_write_sql
    Execute ordered write SQL statements against a database
```

Add `--json` for machine-readable output:

```bash
datasette agent tools --json
```

## Observability

datasette-agent instruments every agent turn, model call, tool call, suspension
and background run with [OpenTelemetry](https://opentelemetry.io/), through
`opentelemetry-api` only. The plugin never installs a provider or an exporter:
with no SDK in the process every span is a no-op and every instrument does
nothing, so there is no cost until an operator turns telemetry on - exactly as
with Datasette core. See Datasette's own telemetry documentation for the
operator story; the two easiest routes are the standard
`opentelemetry-instrument` wrapper, or the `datasette-otel-viewer` plugin,
which records this instance's spans and metrics into a SQLite database and
browses them at `/-/otel` with no `OTEL_*` configuration at all.

Three spans follow the OpenTelemetry GenAI semantic conventions (`gen_ai.*`),
so trace backends render them with their GenAI styling. A chat turn that runs
one SQL query produces this tree (core's spans marked):

```
POST /-/agent/(?P<conversation_id>...)/stream$      SERVER   (core)
└── invoke_agent datasette-agent                    INTERNAL
    ├── datasette_agent.system_prompt               INTERNAL
    │   └── db.query ×N                             CLIENT   (core)
    ├── db.query  (load history)                    CLIENT   (core)
    ├── chat claude-sonnet-5                        CLIENT
    ├── execute_tool sql_query                      INTERNAL
    │   └── db.query  SELECT ...                    CLIENT   (core)
    ├── chat claude-sonnet-5                        CLIENT
    └── db.query ×N  (persist messages)             CLIENT   (core)
```

The gap between one `chat` ending and the next starting is tool time; the gap
inside a `chat` before its `gen_ai.first_token` event is provider latency.
`execute_write_sql` posts through `datasette.client`, so its tool span
contains a nested `SERVER` span that core marks `datasette.internal_client:
true` - filter on that to keep request counts honest. A background agent or
explorer run is a root span in its own trace with a link back to the request
that started it; a turn resumed after `ask_user()` or `browser_task()` links
back to the tool span that suspended.

**Privacy.** No signal carries a prompt, a model's output, tool arguments,
tool output, an error message or an actor id. Variability is closed enums,
sizes are byte counts, and conversation, tool-call, question and task ids
appear on spans only, never as a metric dimension. A sentinel-content test
enforces this. Token usage on a public instance is spend data per request -
not a new class of data, but worth knowing the trace backend holds it. Core's
warning about inbound `traceparent` headers applies unchanged: strip them at
the proxy on a public instance.

Every span, attribute and metric below lives in the `datasette_agent`
instrumentation scope and is declared in `datasette_agent/telemetry_registry.py`;
a conformance test holds the code to this reference in both directions, and
`uv run scripts/telemetry-doc.py` regenerates it.

<!-- telemetry-reference:start -->

#### Spans

Span names ending in `{...}` are families: the suffix is the model id or the tool name. Kinds are `INTERNAL` unless noted.

**`invoke_agent datasette-agent`** - One agent turn, following the GenAI `invoke_agent` convention. Starts before the pending-notification drain and the user message insert so every `db.query` of the turn nests under it, and ends in a `finally` so the error path that sends the `error` SSE event still closes it. Kind `INTERNAL`: the agent is this process's own work, not a call out. Status is `ERROR` only for `outcome=error`, `chain_limit` and `max_iterations`; a suspension is `UNSET` because it is the designed way a turn ends. For a background agent or explorer run the span covers the whole run and is a **root span in its own trace** with a link back to whatever span was current when the run was started - the API request, or the spawning turn's `execute_tool spawn_background_agent` - the shape core uses for `block=False` writes: a run outlives its cause, so a link records the causation without asserting containment. The `background.*` attributes appear on those runs only.

- `gen_ai.operation.name` - The GenAI operation: `invoke_agent` for a whole turn, `chat` for one model response, `execute_tool` for one tool call. One of: `chat`, `execute_tool`, `invoke_agent`.
- `gen_ai.agent.name` - Always `datasette-agent`. A fixed string today; a future agent profiles feature could vary it, but it would stay bounded. One of: `datasette-agent`.
- `gen_ai.conversation.id` - The conversation's ULID. Opaque, but a stable key across every turn a person has with the agent, so closer to a session id than a request id. **Spans only, never a metric dimension.** The same ULID is in the request span's `url.path` anyway.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.
- `datasette_agent.mode` - How the turn was started: `chat` (a user message on the stream route), `resume` (continuing a turn suspended on `ask_user()` or `browser_task()`), `cli` (`datasette agent chat`), `background` (a background agent's whole run) or `explorer` (the same, launched by the explorer). One of: `background`, `chat`, `cli`, `explorer`, `resume`.
- `datasette_agent.outcome` - How the turn ended. `done` is a normal end; `question` and `browser_task` mean the turn suspended waiting on a human, which is the designed way a turn ends and **not** an error; `chain_limit` is llm's chain limit tripping; `cancelled` is the asyncio task being cancelled (a client disconnect, or the background-agent cancel endpoint); `error` is anything else raised. Background runs end in `completed`, `max_iterations`, `cancelled` or `error`. Only `error`, `chain_limit` and `max_iterations` set span status `ERROR`. One of: `browser_task`, `cancelled`, `chain_limit`, `completed`, `done`, `error`, `max_iterations`, `question`.
- `datasette_agent.history.messages` *(optional)* - Messages loaded from the conversation's persisted history for this turn, this turn's own user message included - what drives input tokens up over a long conversation. Absent in `cli` mode, where llm holds the history in memory; for a background run, the size at its last iteration.
- `datasette_agent.chain.steps` - Model responses in this turn's chain: 1 for a plain answer, 2 or more when the model called tools and came back for another round.
- `datasette_agent.tool_calls` - Tool calls that ran to completion this turn (a call that suspended the turn is not counted; it re-runs on resume).
- `datasette_agent.notifications_drained` *(optional)* - Background-agent completion notifications prepended to the user's message on this turn. Set only when there were any.
- `datasette_agent.resumed_from` *(optional)* - For `mode=resume`: what the turn is continuing from. The span also carries a link to the `execute_tool` span that suspended, when its trace context was persisted with the row. One of: `browser_task`, `question`.
- `datasette_agent.background.id` *(optional)* - The background agent's ULID. Spans only, never a metric dimension.
- `datasette_agent.background.iterations` *(optional)* - How many loop passes the background run made before it ended.
- `datasette_agent.background.max_iterations` *(optional)* - The iteration cap the run was allowed (`MAX_ITERATIONS`).
- `datasette_agent.background.spawned_from_conversation` *(optional)* - `True` when a chat turn's `spawn_background_agent` tool started the run (its completion is then posted back as a notification), `False` for the HTTP API and the explorer. Never the other conversation's id - the span link carries that.
- `error.type` *(optional)* - Exception class name when the work raised; `CancelledError` when it was cancelled. Never the message. Core's semantic-convention spelling, reused per the plugin telemetry docs. Absent on success, and absent when a tool merely *returned* an error payload - that is an outcome.

**`chat {...}`** *(CLIENT)* - One model response, named `chat {gen_ai.request.model}`. Kind `CLIENT`: the one span here that represents a call to something outside the process, exactly as core's `db.query` is `CLIENT`. Covers exactly the streaming window - llm's response object is lazy and iterating it is what makes the HTTP call - so the gap before the `gen_ai.first_token` event is provider latency and the gap between one `chat` ending and the next starting is tool time. If the operator also installs `opentelemetry-instrumentation-httpx` the provider's HTTP call appears as a child. The prompt, the messages, the streamed text and the reasoning are never recorded.

- `gen_ai.operation.name` - The GenAI operation: `invoke_agent` for a whole turn, `chat` for one model response, `execute_tool` for one tool call. One of: `chat`, `execute_tool`, `invoke_agent`.
- `gen_ai.provider.name` - Which LLM provider served the call, derived from the llm plugin that implements the model: `anthropic`, `openai`, `gcp.gemini`, `echo` (tests)... Semconv well-known spellings where one exists, the plugin's module name otherwise. Bounded by the installed llm plugins.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.
- `gen_ai.response.model` *(optional)* - The provider-reported concrete model (`claude-sonnet-5-20260101`) when llm reports one that differs from the requested id.
- `gen_ai.response.id` *(optional)* - The provider's response id, for support tickets. Present only when the provider returns one. Spans only.
- `gen_ai.usage.input_tokens` *(optional)* - Input tokens the provider billed for this response, when it reports usage.
- `gen_ai.usage.output_tokens` *(optional)* - Output tokens the provider billed for this response, when it reports usage.
- `gen_ai.usage.cache_read.input_tokens` *(optional)* - Input tokens served from the provider's prompt cache, normalised from Anthropic's `cache_read_input_tokens` and OpenAI's `prompt_tokens_details.cached_tokens`. Cache hit rate is the single biggest cost lever with prompt-caching providers.
- `gen_ai.usage.cache_creation.input_tokens` *(optional)* - Input tokens written to the provider's prompt cache (Anthropic's `cache_creation_input_tokens`).
- `datasette_agent.chain.index` - 0-based position of this model response within the turn's chain.
- `datasette_agent.streaming` - Whether the response was streamed (`True` for chat, `False` for background agents).
- `datasette_agent.tool_calls_requested` - Tool calls the model asked for in this response. Zero means the chain ends here.
- `error.type` *(optional)* - Exception class name when the work raised; `CancelledError` when it was cancelled. Never the message. Core's semantic-convention spelling, reused per the plugin telemetry docs. Absent on success, and absent when a tool merely *returned* an error payload - that is an outcome.

**`execute_tool {...}`** - One tool invocation, named `execute_tool {gen_ai.tool.name}`, following the GenAI `execute_tool` convention. Every tool - this plugin's own and any registered through `register_agent_tools` - runs through one code path, so every call gets a span. Core's `db.query` spans issued by the tool nest under it, as does the nested internal `SERVER` span `execute_write_sql` produces by posting through `datasette.client` (core marks that one `datasette.internal_client: true`). Arguments and output are never recorded; a suspension is `outcome=suspended` with the question or task id, never an error. The span ends when the tool raises; the human wait that follows is not a span (it can outlive the process) but the `suspension.wait` histogram, and the resumed turn links back here.

- `gen_ai.operation.name` - The GenAI operation: `invoke_agent` for a whole turn, `chat` for one model response, `execute_tool` for one tool call. One of: `chat`, `execute_tool`, `invoke_agent`.
- `gen_ai.tool.name` - The tool's registered name (`sql_query`, `describe_table`, a plugin's tool...). Bounded: the set of registered tools.
- `gen_ai.tool.call.id` *(optional)* - The provider's id for this tool call, when it issues one. Never the argument-hash fallback the plugin derives for providers that do not. Spans only.
- `gen_ai.tool.type` - Always `function` - every agent tool is a client-side function. One of: `function`.
- `datasette_agent.tool.plugin` - The pluggy name of the plugin whose `register_agent_tools` hook registered the tool (`agent` for this plugin's own tools), or `unknown` for a tool constructed directly. Lets an operator see that the slow tool came from `datasette-foo`. Bounded by installed plugins.
- `datasette_agent.tool.outcome` - How the tool call ended. Most tools return errors *as data* - `{"error": ...}` is a successful call from llm's point of view and a failed one from the operator's - so the returned payload is classified too: `permission_denied` and `not_found` for the two messages this plugin's own tools emit, `error` for any other top-level `error` key or a raised exception, `suspended` when the tool paused the turn on `ask_user()` / `browser_task()`, `ok` otherwise. Only a raised exception sets span status `ERROR`; a returned error payload means the tool did what it was asked and the request to it was wrong. One of: `error`, `not_found`, `ok`, `permission_denied`, `suspended`.
- `datasette_agent.suspension.kind` *(optional)* - What a tool suspended the turn on: an `ask_user()` question or a `browser_task()`. On the `execute_tool` span when `outcome=suspended`; the dimension of the suspension metrics. One of: `browser_task`, `question`.
- `datasette_agent.question.type` *(optional)* - The `ask_user()` question's shape: yes/no, a choice, or free text. Never the prompt or the options. One of: `boolean`, `choice`, `text`.
- `datasette_agent.question.id` *(optional)* - The `agent_questions` row the tool suspended on. Spans only.
- `datasette_agent.task.id` *(optional)* - The `agent_browser_tasks` row the tool suspended on. Spans only.
- `datasette_agent.tool.replayed` *(optional)* - `True` when the tool call consumed a stored answer or browser-task result instead of suspending - the re-execution of a suspended call on resume. Absent on a fresh call.
- `datasette_agent.tool.output.bytes` - Length of the tool's returned string. A size, never the content.
- `datasette_agent.sql.display` *(optional)* - `sql_query` only: the `display` mode the model picked. The distribution is directly actionable - the system prompt is trying to steer it. Rows, truncation and the SQL text are on the nested core `db.query` span; not duplicated here. One of: `both`, `model`, `user`.
- `error.type` *(optional)* - Exception class name when the work raised; `CancelledError` when it was cancelled. Never the message. Core's semantic-convention spelling, reused per the plugin telemetry docs. Absent on success, and absent when a tool merely *returned* an error payload - that is an outcome.

**`datasette_agent.system_prompt`** - Building the system prompt: one permission check and one `table_names()` query per database, on every turn and every background-agent iteration. On an instance with many databases this is a visible slice of turn latency, and this span is the evidence for (or against) caching it.

- `datasette_agent.databases` - Databases the system-prompt builder iterated - each one costs a permission check and a `table_names()` query, on every turn.
- `datasette_agent.prompt.chars` - Length of the built system prompt in characters. A size, never the text.

**`datasette_agent.background.iteration`** - One pass of a background agent's loop, child of the run's `invoke_agent` root. Each pass rebuilds the system prompt, reloads the whole history and runs a chain, so this is where "why did iteration 7 take four minutes" gets answered; `chat` and `execute_tool` spans nest under it.

- `datasette_agent.background.iteration` - 1-based loop pass within the background run.

#### Metrics

**`gen_ai.client.token.usage`** *(Histogram, unit `{token}`)* - Tokens per model response, per token type - the semconv metric. A histogram rather than a counter, as semconv chose, so the p95 of input tokens says whether the context is growing while `sum` still gives total spend.

- `gen_ai.operation.name` - The GenAI operation: `invoke_agent` for a whole turn, `chat` for one model response, `execute_tool` for one tool call. One of: `chat`, `execute_tool`, `invoke_agent`.
- `gen_ai.provider.name` - Which LLM provider served the call, derived from the llm plugin that implements the model: `anthropic`, `openai`, `gcp.gemini`, `echo` (tests)... Semconv well-known spellings where one exists, the plugin's module name otherwise. Bounded by the installed llm plugins.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.
- `gen_ai.token.type` - Which count a `gen_ai.client.token.usage` measurement is. `input` and `output` are the semconv values; `cache_read` and `cache_creation` are recorded additionally when the provider reports prompt-cache usage. One of: `cache_creation`, `cache_read`, `input`, `output`.

Bucket boundaries: 1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864.

**`gen_ai.client.operation.duration`** *(Histogram, unit `s`)* - Duration of one model response, measured across the streaming window - the semconv metric. `error.type` splits failures out of the latency distribution.

- `gen_ai.operation.name` - The GenAI operation: `invoke_agent` for a whole turn, `chat` for one model response, `execute_tool` for one tool call. One of: `chat`, `execute_tool`, `invoke_agent`.
- `gen_ai.provider.name` - Which LLM provider served the call, derived from the llm plugin that implements the model: `anthropic`, `openai`, `gcp.gemini`, `echo` (tests)... Semconv well-known spellings where one exists, the plugin's module name otherwise. Bounded by the installed llm plugins.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.
- `error.type` *(optional)* - Exception class name when the work raised; `CancelledError` when it was cancelled. Never the message. Core's semantic-convention spelling, reused per the plugin telemetry docs. Absent on success, and absent when a tool merely *returned* an error payload - that is an outcome.

Bucket boundaries: 0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92.

**`datasette_agent.chat.time_to_first_token`** *(Histogram, unit `s`)* - Seconds from starting a streamed model response to its first text, reasoning or tool-call chunk - the number a user waiting on a spinner feels. Streamed responses only. The same instant is a `gen_ai.first_token` event on the `chat` span, but a span event does not survive sampling and this does.

- `gen_ai.provider.name` - Which LLM provider served the call, derived from the llm plugin that implements the model: `anthropic`, `openai`, `gcp.gemini`, `echo` (tests)... Semconv well-known spellings where one exists, the plugin's module name otherwise. Bounded by the installed llm plugins.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.

Bucket boundaries: 0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92.

**`datasette_agent.turn.duration`** *(Histogram, unit `s`)* - One measurement per `invoke_agent` span: the whole turn, model calls and tool calls and persistence included. **The top-line instrument**: turns per second, error rate, suspension rate and p50/p95 turn time all come from it, split by mode.

- `datasette_agent.mode` - How the turn was started: `chat` (a user message on the stream route), `resume` (continuing a turn suspended on `ask_user()` or `browser_task()`), `cli` (`datasette agent chat`), `background` (a background agent's whole run) or `explorer` (the same, launched by the explorer). One of: `background`, `chat`, `cli`, `explorer`, `resume`.
- `datasette_agent.outcome` - How the turn ended. `done` is a normal end; `question` and `browser_task` mean the turn suspended waiting on a human, which is the designed way a turn ends and **not** an error; `chain_limit` is llm's chain limit tripping; `cancelled` is the asyncio task being cancelled (a client disconnect, or the background-agent cancel endpoint); `error` is anything else raised. Background runs end in `completed`, `max_iterations`, `cancelled` or `error`. Only `error`, `chain_limit` and `max_iterations` set span status `ERROR`. One of: `browser_task`, `cancelled`, `chain_limit`, `completed`, `done`, `error`, `max_iterations`, `question`.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.
- `error.type` *(optional)* - Exception class name when the work raised; `CancelledError` when it was cancelled. Never the message. Core's semantic-convention spelling, reused per the plugin telemetry docs. Absent on success, and absent when a tool merely *returned* an error payload - that is an outcome.

Bucket boundaries: 0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92.

**`datasette_agent.chain.steps`** *(Histogram, unit `{step}`)* - Model round-trips per turn. A rising median means the model is flailing, tool outputs are being truncated and re-fetched, or the system prompt is not landing; `chain_limit` outcomes on the turn histogram are the extreme of the same signal.

- `datasette_agent.mode` - How the turn was started: `chat` (a user message on the stream route), `resume` (continuing a turn suspended on `ask_user()` or `browser_task()`), `cli` (`datasette agent chat`), `background` (a background agent's whole run) or `explorer` (the same, launched by the explorer). One of: `background`, `chat`, `cli`, `explorer`, `resume`.
- `gen_ai.request.model` - The llm model id the call was made with (`claude-sonnet-5`, `echo`). On an `invoke_agent` span it is the conversation's pinned model, or `unknown` when the turn failed before a model was resolved. Bounded by the models an operator has configured.

Bucket boundaries: 1, 2, 3, 4, 6, 8, 10, 15, 20.

**`datasette_agent.turns.active`** *(UpDownCounter, unit `{turn}`)* - Turns in flight right now, by mode. Each streaming turn holds an SSE connection and a provider stream open, so this is the capacity number; `mode=background` / `explorer` is the number of background agents running.

- `datasette_agent.mode` - How the turn was started: `chat` (a user message on the stream route), `resume` (continuing a turn suspended on `ask_user()` or `browser_task()`), `cli` (`datasette agent chat`), `background` (a background agent's whole run) or `explorer` (the same, launched by the explorer). One of: `background`, `chat`, `cli`, `explorer`, `resume`.

**`datasette_agent.tool.duration`** *(Histogram, unit `s`)* - One measurement per `execute_tool` span. Per-tool call count and error rate derive from its count, so there is no separate counter. A `suspended` duration is the time until the tool raised - short, and not the human wait.

- `gen_ai.tool.name` - The tool's registered name (`sql_query`, `describe_table`, a plugin's tool...). Bounded: the set of registered tools.
- `datasette_agent.tool.plugin` - The pluggy name of the plugin whose `register_agent_tools` hook registered the tool (`agent` for this plugin's own tools), or `unknown` for a tool constructed directly. Lets an operator see that the slow tool came from `datasette-foo`. Bounded by installed plugins.
- `datasette_agent.tool.outcome` - How the tool call ended. Most tools return errors *as data* - `{"error": ...}` is a successful call from llm's point of view and a failed one from the operator's - so the returned payload is classified too: `permission_denied` and `not_found` for the two messages this plugin's own tools emit, `error` for any other top-level `error` key or a raised exception, `suspended` when the tool paused the turn on `ask_user()` / `browser_task()`, `ok` otherwise. Only a raised exception sets span status `ERROR`; a returned error payload means the tool did what it was asked and the request to it was wrong. One of: `error`, `not_found`, `ok`, `permission_denied`, `suspended`.

Bucket boundaries: 0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92.

**`datasette_agent.tool.output.truncated`** *(Counter, unit `{call}`)* - Tool outputs cut down before the model saw them. A high rate for `sql_query` means the model is over-fetching, or the model-visible output limit is wrong for the workload. A counter rather than a dimension on the duration histogram: truncation is an event to alert on, not something to split latency by.

- `gen_ai.tool.name` - The tool's registered name (`sql_query`, `describe_table`, a plugin's tool...). Bounded: the set of registered tools.

**`datasette_agent.background.iterations`** *(Histogram, unit `{iteration}`)* - Loop passes per background run, by outcome - how close the agents run to `MAX_ITERATIONS`. The whole run's duration is on `datasette_agent.turn.duration` with `mode=background`; iterations are deliberately not mixed into that histogram.

- `datasette_agent.mode` - How the turn was started: `chat` (a user message on the stream route), `resume` (continuing a turn suspended on `ask_user()` or `browser_task()`), `cli` (`datasette agent chat`), `background` (a background agent's whole run) or `explorer` (the same, launched by the explorer). One of: `background`, `chat`, `cli`, `explorer`, `resume`.
- `datasette_agent.outcome` - How the turn ended. `done` is a normal end; `question` and `browser_task` mean the turn suspended waiting on a human, which is the designed way a turn ends and **not** an error; `chain_limit` is llm's chain limit tripping; `cancelled` is the asyncio task being cancelled (a client disconnect, or the background-agent cancel endpoint); `error` is anything else raised. Background runs end in `completed`, `max_iterations`, `cancelled` or `error`. Only `error`, `chain_limit` and `max_iterations` set span status `ERROR`. One of: `browser_task`, `cancelled`, `chain_limit`, `completed`, `done`, `error`, `max_iterations`, `question`.

Bucket boundaries: 1, 2, 3, 5, 8, 12, 16, 20, 30, 50.

**`datasette_agent.suspensions`** *(Counter, unit `{suspension}`)* - Turns suspended waiting on a human, by kind and asking tool - counted when the pending row is inserted, not when a suspended call re-raises on resume. How often the agent stops to ask.

- `datasette_agent.suspension.kind` *(optional)* - What a tool suspended the turn on: an `ask_user()` question or a `browser_task()`. On the `execute_tool` span when `outcome=suspended`; the dimension of the suspension metrics. One of: `browser_task`, `question`.
- `gen_ai.tool.name` - The tool's registered name (`sql_query`, `describe_table`, a plugin's tool...). Bounded: the set of registered tools.
- `datasette_agent.question.type` *(optional)* - The `ask_user()` question's shape: yes/no, a choice, or free text. Never the prompt or the options. One of: `boolean`, `choice`, `text`.

**`datasette_agent.suspension.wait`** *(Histogram, unit `s`)* - Seconds from a suspension's row being created to its resolution - how long people take to answer, and how often browser tasks expire. Recorded at the four resolution sites after their guarded UPDATE succeeds, so a lost race is not double-counted.

- `datasette_agent.suspension.kind` *(optional)* - What a tool suspended the turn on: an `ask_user()` question or a `browser_task()`. On the `execute_tool` span when `outcome=suspended`; the dimension of the suspension metrics. One of: `browser_task`, `question`.
- `datasette_agent.suspension.resolution` - How a suspension ended: a question was `answered`; a browser task was `completed` by the page, `cancelled` by the user, or `expired` past its deadline. Expiry is recorded lazily, when someone next looks, so an expired wait is at least the timeout and possibly much more. One of: `answered`, `cancelled`, `completed`, `expired`.

Bucket boundaries: 1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 86400.

<!-- telemetry-reference:end -->

## Development

To set up this plugin locally, first checkout the code. Run the tests like this:
```bash
cd datasette-agent
uv run pytest
```
After changing `datasette_agent/telemetry_registry.py`, regenerate the
telemetry reference above (CI checks it is fresh):
```bash
uv run scripts/telemetry-doc.py
```
To run the development server with a persistent internal database and GPT-5.5 as the model:
```bash
uv run datasette --internal internal.db \
  --root --secret 1 \
  -s plugins.datasette-llm.default_model gpt-5.5
```
Add extra database files to that command to enable the agent to query them.

## Credits

This plugin vendors [streaming-markdown](https://github.com/thetarnav/streaming-markdown) by Damian Tarnawski, MIT licensed.
