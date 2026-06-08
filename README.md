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

The "Explore with AI agent" entries that appear in the database and table action menus launch a background agent that explores the selected database or table and writes a report. Reports live under `/-/agent/explore/`.

Visit `/-/agent/background` to launch background agents directly. Each one is given a goal and runs toward it without further input. The listing includes a Stop button for cancelling agents that are still running.

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

### Asking the user a question

A tool can pause the agent and ask the user a multiple-choice question, blocking until they pick an option. Call `ask_user(question, options)` from inside your handler:

```python
import json

from datasette_agent.ask_user import ask_user


async def deploy_tool(datasette, actor, project):
    target = await ask_user(
        f"Which environment should I deploy {project} to?",
        [
            {"label": "production", "description": "Live, customer-facing"},
            {"label": "staging", "description": "Safe to break"},
        ],
    )
    # ...deploy to `target`...
    return json.dumps({"deployed_to": target})
```

`options` is a list of choice labels (plain strings) or `{"label", "description"}` dicts. The call returns the `label` string of the option the user selected, which you can use however you like — including as part of the JSON you return to the model.

While the question is pending, the chat UI shows the options as clickable buttons and the agent turn is held open until the user answers. `ask_user` only works inside an interactive chat turn — calling it from a background agent or the CLI raises `datasette_agent.ask_user.AskUserUnavailable`, so guard those code paths if your tool can run in both contexts.

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
