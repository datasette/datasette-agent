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

Visit `/-/agent` to start a conversation with the agent.

## Rendering custom HTML from tools

Tool plugins can render rich HTML inline in the chat UI by returning a JSON object with an `_html` key. The HTML will be rendered directly in the conversation, while the remaining keys are passed back to the LLM as the tool result (the `_html` and `sql` keys are stripped before the LLM sees them, so it won't parrot back raw HTML or SQL).

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

The `_html` value is inserted into the chat as raw HTML, so it can include custom elements, scripts, and styles. The remaining keys (`summary` in this example) are what the LLM receives as the tool result to inform its next response.

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
        ),
    ]
```

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

To render rich HTML inline in the chat UI, include an `_html` key in the returned JSON. The HTML will be displayed to the user, while the remaining keys are passed to the LLM as the tool result (the `_html` key is stripped before the LLM sees it):

```python
return json.dumps({
    "_html": '<div class="my-widget">Rich content here</div>',
    "summary": "Widget rendered successfully",
})
```

### Entry point

Register the plugin via `pyproject.toml`:

```toml
[project.entry-points.datasette]
my_plugin = "datasette_my_plugin"
```

### Example plugins

- [datasette-agent-charts](https://github.com/datasette/datasette-agent-charts) - renders charts from SQL query results using Observable Plot
- [datasette-agent-openai-imagegen](https://github.com/datasette/datasette-agent-openai-imagegen) - generates images using OpenAI's image generation API

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-agent
# Confirm the plugin is visible
uv run datasette plugins
```
To run the tests:
```bash
uv run pytest
```

## Credits

This plugin includes [streaming-markdown](https://github.com/thetarnav/streaming-markdown) by Damian Tarnawski, MIT licensed.
