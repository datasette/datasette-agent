# datasette-agent-charts Implementation Plan

## Context

datasette-agent has a plugin hook (`register_agent_tools`) that lets plugins add tools the LLM can call. Tool results currently render as plain JSON in `<pre>` tags. We need to:
1. Add rich HTML rendering support to datasette-agent (generic `_html` convention)
2. Build datasette-agent-charts: a web component + two chart tools using Observable Plot

## Changes to datasette-agent

### 1. `datasette_agent/__init__.py` — add `extract_html` Jinja2 filter

Add alongside the existing `pretty_json` filter in `prepare_jinja2_environment`:

```python
def extract_html(value):
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed.get("_html", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return ""

env.filters["extract_html"] = extract_html
```

### 2. `datasette_agent/templates/agent_conversation.html` — render `_html` on page reload

Replace the `tool_result` block (lines 31-35) to check for `_html` content and render it with `| safe`, keeping the raw JSON in a collapsed `<details>` for debugging.

### 3. `datasette_agent/static/agent.js` — detect `_html` in streaming tool results

Modify `appendToolResult()` to:
- Parse the output as JSON
- If it has `_html`, render that HTML in a `<div class="agent-rich-result">`
- Clone any `<script>` elements so they execute (innerHTML doesn't run scripts)
- Still show raw JSON in a collapsed `<details>` below

## Changes to datasette-agent-charts

### 4. `datasette_agent_charts/static/datasette-chart.js` — new web component

`<datasette-chart>` custom element that:
- Loads Observable Plot from `https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6/+esm` (self-contained, includes d3)
- Reads config from a `<script type="application/json">` child
- Supports 4 mark types: `barY`, `line`, `dot`, `areaY`
- Config shape: `{type, data, x, y, color?, title?, xLabel?, yLabel?, width?, height?}`
- Idempotent `customElements.define` registration

### 5. `datasette_agent_charts/__init__.py` — two chart tools

**`render_chart`** — takes inline data + chart config, returns `{_html: "...", chart_type, row_count}`
- For when the LLM already has data from a previous `sql_query` call

**`render_chart_from_query`** — takes database, SQL, chart config, executes query server-side, returns `{_html: "...", chart_type, row_count, columns}`
- Saves a round-trip; data never passes through the LLM
- Checks `execute-sql` permission (same pattern as `sql_tools.py`)

Both tools' `_html` includes:
```html
<script src="/-/static-plugins/datasette-agent-charts/datasette-chart.js" type="module"></script>
<datasette-chart>
  <script type="application/json">{"type":"barY","data":[...],...}</script>
</datasette-chart>
```

The `<script type="module">` is cached by the browser (fetched once regardless of how many charts are rendered). The web component self-registers idempotently.

### 6. `pyproject.toml` — add `datasette-agent` dependency

## Implementation Order

1. datasette-agent: `__init__.py` (add filter)
2. datasette-agent: `agent_conversation.html` (template change)
3. datasette-agent: `agent.js` (rich result rendering)
4. datasette-agent-charts: `static/datasette-chart.js` (web component)
5. datasette-agent-charts: `__init__.py` (chart tools)
6. datasette-agent-charts: `pyproject.toml` (dependency)

## Verification

- Install both plugins in dev mode, run datasette with a test database
- Start an agent conversation, ask it to chart some data
- Verify: chart renders inline in chat, collapsed details shows raw JSON
- Reload the page — chart should re-render from stored tool result
- Test both tools: `render_chart` (inline data) and `render_chart_from_query` (SQL)
