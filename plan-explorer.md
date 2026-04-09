# Explorer Agent Feature

## Context

We want an "explorer agent" that can be triggered from the database or table actions menus in Datasette. It runs in the background using the existing background agent system, explores the data looking for interesting trends and patterns, and produces a markdown report by appending findings as it goes via an `append_to_report` tool.

## Schema Changes (`schema.py`)

Add one new table:

```sql
CREATE TABLE IF NOT EXISTS datasette_agent_explorer_reports (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES datasette_agent_background_agents(id),
    actor_id TEXT,
    database_name TEXT NOT NULL,
    table_name TEXT,
    extra_prompt TEXT,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- `agent_id` links to the background agent driving this exploration
- `table_name` is NULL for database-level explorations
- `content` accumulates markdown via `append_to_report` (SQLite `content || ?` concatenation)
- Status is derived from the linked background agent, not duplicated

## New File: `explorer.py`

### `_make_append_to_report_tool(datasette, report_id)` 
Factory (like `_make_mark_finished_tool` in `background_agent.py`) returning an `AgentTool`:
- `append_to_report(markdown_content: str)` - appends to the report's `content` column
- Uses `UPDATE ... SET content = content || ? ...`

### `_build_explorer_goal(database_name, table_name, extra_prompt)`
Builds the goal prompt. Two variants:

**Database-level**: Instructs agent to list all tables, examine schemas, run analytical queries, look for patterns/anomalies/relationships, and use `append_to_report()` for each finding.

**Table-level**: Instructs agent to focus on the specific table but join with related tables when useful. Same analytical approach.

Both include the optional `extra_prompt` as "Additional guidance from the user: ..." if provided.

### `start_explorer(datasette, actor, database_name, table_name=None, extra_prompt=None)`
Orchestration function:
1. Create report record in `datasette_agent_explorer_reports` (agent_id=NULL initially)
2. Build goal prompt via `_build_explorer_goal`
3. Build tools: standard SQL tools from `get_default_tools()` + `append_to_report`
4. Call `start_background_agent(datasette, actor, goal, tools=tools)` - `mark_finished` is added automatically by the background agent runner
5. Update report record with agent_id
6. Return `(report_id, agent_id)`

## Action Hooks (`__init__.py`)

### `database_actions(datasette, actor, database, request)`
Returns `[{"href": "/-/agent/explore/{database}", "label": "Explore with AI agent", "description": "..."}]` (async inner function pattern for permission check).

### `table_actions(datasette, actor, database, table, request)`  
Returns `[{"href": "/-/agent/explore/{database}/{table}", "label": "Explore with AI agent", "description": "..."}]`.

## Routes (`__init__.py`)

Added before the conversation_id catch-all:
```python
(r"^/-/agent/explore/(?P<database>[^/]+)$", views.explorer_page),
(r"^/-/agent/explore/(?P<database>[^/]+)/(?P<table>[^/]+)$", views.explorer_page),
(r"^/-/agent/api/explore$", views.api_start_explorer),
(r"^/-/agent/api/explore/(?P<report_id>[A-Za-z0-9]{26})$", views.api_explorer_report),
```

## Views (`views.py`)

### `explorer_page` - GET `/-/agent/explore/<database>[/<table>]`
- Verifies database (and table) exist
- Queries reports joined with background_agents to get status
- Renders `agent_explorer.html`

### `api_start_explorer` - POST `/-/agent/api/explore`
- Accepts `{"database": str, "table": str|null, "extra_prompt": str|null}`
- Calls `start_explorer()`, returns `{"report_id": ..., "agent_id": ...}`

### `api_explorer_report` - GET `/-/agent/api/explore/<report_id>`
- Returns report JSON with agent status (for polling)

## Template: `agent_explorer.html`

- Extends `base.html`, links to `agent.css`
- Form with optional textarea for extra prompt + "Start exploration" button
- Lists reports with status badge, timestamp, and rendered markdown content
- JS: form submission POSTs to `/api/explore`, reloads page
- JS: polls running reports via `/api/explore/<report_id>` and updates content

## Files to Modify/Create

| File | Change |
|------|--------|
| `schema.py` | Add `datasette_agent_explorer_reports` table |
| `explorer.py` (new) | `start_explorer()`, `_make_append_to_report_tool()`, `_build_explorer_goal()` |
| `views.py` | Add `explorer_page`, `api_start_explorer`, `api_explorer_report` |
| `__init__.py` | Add `database_actions`, `table_actions` hooks; register 4 new routes |
| `templates/agent_explorer.html` (new) | Explorer page UI |
| `tests/test_explorer.py` (new) | Tests |

## Implementation Order (TDD)

1. Write failing tests
2. Schema changes
3. `explorer.py` - core logic
4. Views + routes
5. Action hooks
6. Template
7. Make all tests pass

## Verification

- Test: explorer reports table is created
- Test: `start_explorer` creates report and agent records
- Test: `append_to_report` tool appends content
- Test: explorer page loads for database and table
- Test: API endpoints work (create, status)
- Test: database_actions and table_actions return links
- Test: permission checks on explorer pages
