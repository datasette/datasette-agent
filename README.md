# datasette-agent

[![PyPI](https://img.shields.io/pypi/v/datasette-agent.svg)](https://pypi.org/project/datasette-agent/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-agent?include_prereleases&label=changelog)](https://github.com/datasette/datasette-agent/releases)
[![Tests](https://github.com/datasette/datasette-agent/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-agent/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-agent/blob/main/LICENSE)

An LLM-powered agent assistant for Datasette

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

The "Explore with AI agent" entries that appear in the database and table action menus launch a background agent that explores the selected database or table and writes a report. Reports live under `/-/agent/explore/`.

Visit `/-/agent/background` to launch background agents directly. Each one is given a goal and runs toward it without further input. The listing includes a Stop button for cancelling agents that are still running.

### Permissions

This plugin registers three independent permissions:

- `datasette-agent` — required to use the chat assistant under `/-/agent`.
- `datasette-agent-explore` — required to see the "Explore with AI agent" entries in the database/table action menus and to use the explorer routes under `/-/agent/explore/`.
- `datasette-agent-background` — required to use the `spawn_background_agent` and `check_background_agent` tools from chat, and to access the `/-/agent/background` page and `/-/agent/api/background/*` endpoints. The background-agent endpoints require both `datasette-agent` and `datasette-agent-background`.

The three permissions are independent: an actor may hold any subset. The `--root` user holds all of them.

## JSON API

The plugin exposes a small JSON API under `/-/agent`. Use normal Datasette authentication for these endpoints, usually by sending the same cookies that the Datasette UI uses. The plugin skips Datasette CSRF checks for paths below `/-/agent/`, so API clients can `POST` JSON directly.

All JSON request bodies should use:

```http
Content-Type: application/json
```

IDs in URLs are 26-character ULIDs. Responses that include timestamps use ISO 8601 strings. Conversation, background-agent, and explorer-report records are scoped to the current Datasette actor: if another actor owns the record the API returns `403 Forbidden`.

Common error responses returned by the route handlers are JSON objects such as:

```json
{"error": "Not found"}
```

Permission failures are handled by Datasette and return `403 Forbidden`.

### Create a conversation

```http
POST /-/agent/api/conversations
```

Permission: `datasette-agent`

Creates an empty chat conversation for the current actor and returns its ID. The request body is not currently inspected; if you have an initial user message, send it afterwards to the stream endpoint.

Response:

```json
{
  "conversation_id": "01JEXAMPLE000000000000000"
}
```

Example:

```bash
curl -X POST http://localhost:8001/-/agent/api/conversations \
  -H 'Content-Type: application/json' \
  --data '{}'
```

### Stream a chat response

```http
POST /-/agent/{conversation_id}/stream
```

Permission: `datasette-agent`

Sends a user message to an existing conversation and streams the assistant response using Server-Sent Events (SSE). This endpoint uses `POST` with a JSON body, so browser clients should use `fetch()` streaming rather than the native `EventSource` API, which only supports `GET`.

Request:

```json
{
  "message": "What tables are in this Datasette instance?"
}
```

Response headers include:

```http
Content-Type: text/event-stream
Cache-Control: no-cache
Content-Encoding: none
```

Each event frame has an `event:` line and a single JSON `data:` line:

```text
event: text_chunk
data: {"content": "There are three tables:"}

event: done
data: {}

```

SSE event types:

- `reasoning_chunk` - optional reasoning text from models that expose it. Data: `{"content": "..."}`
- `text_chunk` - assistant markdown text. Concatenate these in order for the visible assistant message. Data: `{"content": "..."}`
- `tool_call` - emitted before the agent runs a tool. Data: `{"name": "sql_query", "arguments": {...}}`
- `tool_result` - emitted after a tool returns. Data: `{"name": "sql_query", "output": "..."}`. The `output` value is the raw tool-output string, often JSON.
- `done` - successful terminal event. Data: `{}`
- `error` - terminal failure event. Data: `{"message": "..."}`

Clients should treat `done` or `error` as terminal. If the HTTP stream closes before either event, treat the response as interrupted.

Example:

```bash
curl -N -X POST http://localhost:8001/-/agent/01JEXAMPLE000000000000000/stream \
  -H 'Content-Type: application/json' \
  --data '{"message": "List the tables"}'
```

Minimal browser client:

```javascript
const response = await fetch(`/-/agent/${conversationId}/stream`, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({message: "List the tables"}),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
let eventType = null;

while (true) {
  const {done, value} = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, {stream: true});
  const lines = buffer.split("\n");
  buffer = lines.pop();

  for (const line of lines) {
    if (line.startsWith("event: ")) {
      eventType = line.slice(7);
    } else if (line.startsWith("data: ") && eventType) {
      const data = JSON.parse(line.slice(6));
      console.log(eventType, data);
      eventType = null;
    }
  }
}
```

### Poll a conversation

```http
GET /-/agent/{conversation_id}/poll
```

Permission: `datasette-agent`

Returns a lightweight status object for a conversation. This is mainly used by conversation pages linked to background agents, where new messages may appear without the user submitting a chat turn.

Response:

```json
{
  "agent_status": "running",
  "message_count": 3
}
```

`agent_status` is one of `pending`, `running`, `completed`, `error`, or `null` when the conversation is a normal user chat with no linked background agent.

### Start a background agent

```http
POST /-/agent/api/background
```

Permissions: `datasette-agent` and `datasette-agent-background`

Starts an autonomous background agent for the current actor. The agent creates its own conversation and runs until it calls its internal `mark_finished` tool, hits its iteration limit, errors, or is cancelled.

Request:

```json
{
  "goal": "Analyze the data and summarize unusual patterns"
}
```

Response:

```json
{
  "agent_id": "01JEXAMPLE111111111111111",
  "conversation_id": "01JEXAMPLE222222222222222",
  "status": "pending"
}
```

`goal` is required. `status` is usually `pending` or `running` immediately after creation.

### Get background-agent status

```http
GET /-/agent/api/background/{agent_id}
```

Permissions: `datasette-agent` and `datasette-agent-background`

Returns the stored background-agent row for the current actor.

Response:

```json
{
  "id": "01JEXAMPLE111111111111111",
  "conversation_id": "01JEXAMPLE222222222222222",
  "actor_id": "user",
  "goal": "Analyze the data and summarize unusual patterns",
  "status": "completed",
  "final_message": "Analysis complete",
  "error": null,
  "spawned_by_conversation_id": null,
  "created_at": "2026-05-20T12:00:00+00:00",
  "updated_at": "2026-05-20T12:00:05+00:00"
}
```

`status` is one of `pending`, `running`, `completed`, or `error`. Cancelled agents are marked as `error` with `error` set to `"Cancelled by user"`.

### Cancel a background agent

```http
POST /-/agent/api/background/{agent_id}/cancel
```

Permissions: `datasette-agent` and `datasette-agent-background`

Cancels a pending or running background agent. No request body is required.

Response when cancellation changes the agent:

```json
{
  "agent_id": "01JEXAMPLE111111111111111",
  "status": "error",
  "cancelled": true
}
```

Response for an already terminal agent:

```json
{
  "agent_id": "01JEXAMPLE111111111111111",
  "status": "completed",
  "cancelled": false
}
```

### Start an explorer report

```http
POST /-/agent/api/explore
```

Permission: `datasette-agent-explore`

Starts a background exploration report for a database, optionally focused on a single table. The report content is appended over time as the background agent discovers findings.

Request:

```json
{
  "database": "fixtures",
  "table": "facetable",
  "extra_prompt": "Focus on missing values and date ranges"
}
```

Fields:

- `database` - required Datasette database name.
- `table` - optional table name.
- `extra_prompt` - optional additional guidance for the explorer agent.

Response:

```json
{
  "report_id": "01JEXAMPLE333333333333333",
  "agent_id": "01JEXAMPLE444444444444444"
}
```

### Get an explorer report

```http
GET /-/agent/api/explore/{report_id}
```

Permission: `datasette-agent-explore`

Returns the explorer report row, joined to its background-agent status. Poll this endpoint while `agent_status` is `pending` or `running`.

Response:

```json
{
  "id": "01JEXAMPLE333333333333333",
  "agent_id": "01JEXAMPLE444444444444444",
  "actor_id": "user",
  "database_name": "fixtures",
  "table_name": "facetable",
  "extra_prompt": "Focus on missing values and date ranges",
  "content": "## Table structure\n\n...",
  "created_at": "2026-05-20T12:00:00+00:00",
  "updated_at": "2026-05-20T12:00:30+00:00",
  "agent_status": "running",
  "agent_final_message": null,
  "agent_error": null,
  "agent_conversation_id": "01JEXAMPLE555555555555555"
}
```

### Related non-JSON endpoint

```http
GET /-/agent/{conversation_id}/markdown
```

Permission: `datasette-agent`

Downloads the conversation as Markdown. The response is `text/markdown; charset=utf-8` with a `Content-Disposition` attachment filename derived from the conversation title.

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

### Listing available tools

To see all registered agent tools, grouped by plugin:

```bash
datasette agent tools
```

Output:

```
agent:
  list_databases_and_tables
    List all available databases and their tables
  describe_table
    Get column names, types, and foreign keys for a table
  sql_query
    Execute a read-only SQL query against a database
```

Add `--json` for machine-readable output:

```bash
datasette agent tools --json
```

## Development

To set up this plugin locally, first checkout the code. Run the tests like this:
```bash
cd datasette-agent
uv run pytest
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
