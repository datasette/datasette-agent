# Background Agent System

## Context

Currently, datasette-agent only supports interactive chat sessions via SSE streaming in the browser. We want agents that can run autonomously in the background - given a goal and tools, they work until done. This enables: programmatic agent invocation by other plugins, chat agents delegating long-running tasks to sub-agents, and a UI for users to kick off background work.

## Design Decisions

- **No recursion**: Background agents cannot spawn sub-agents. They get data tools + `mark_finished` only.
- **Notification via prefix**: When a background agent completes, the notification is prepended to the next user message in the spawning chat conversation.
- **Separate UI page**: Background agents get their own page at `/-/agent/background`.
- **Reuse existing tables**: Background agent messages are stored in `datasette_agent_messages` via their own `conversation_id`, so the existing conversation view can display their logs.

## Schema Changes (`schema.py`)

Add two new tables:

```sql
CREATE TABLE IF NOT EXISTS datasette_agent_background_agents (
    id TEXT PRIMARY KEY,                    -- ULID
    conversation_id TEXT NOT NULL REFERENCES datasette_agent_conversations(id),
    actor_id TEXT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, running, completed, error
    final_message TEXT,
    error TEXT,
    spawned_by_conversation_id TEXT,         -- chat conversation that spawned this, if any
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasette_agent_pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,           -- the chat conversation to notify
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

## New Files

### 1. `background_agent.py` - Core runner

**`async def run_background_agent(datasette, actor, agent_id)`**

- Loaded as an `asyncio.Task` (no SSE writer needed)
- Sets status to `running` in DB
- Builds a system prompt that prominently states the goal and instructs the agent to call `mark_finished` when done
- **Outer "keep going" loop**:
  1. Build conversation history from DB (reuses `_build_conversation_history` from `agent.py`)
  2. Call `model.chain()` with tools including `mark_finished`
  3. Iterate responses, save all messages to DB via `_save_message`
  4. After chain completes, check if `mark_finished` was called (via a closure flag)
  5. If NOT called: insert a synthetic user message "Keep going. Reminder: your goal is: {goal}. You must call mark_finished() when done." and loop
  6. If called: break
  7. Safety limit of 50 iterations to prevent runaway agents
- On completion: update status to `completed` or `error`, store `final_message`/`error`
- If `spawned_by_conversation_id` is set: insert a row into `datasette_agent_pending_notifications`
- `mark_finished` tool is created dynamically per-agent using a mutable dict closure:
  ```python
  finished_state = {"called": False, "message": None, "error": None}
  ```

### 2. `api.py` - Python API for plugins

```python
async def start_background_agent(
    datasette, actor, goal, tools=None, spawned_by_conversation_id=None
) -> str:
    """Start a background agent. Returns the agent_id (ULID)."""
```

- Creates a conversation record in `datasette_agent_conversations`
- Creates a record in `datasette_agent_background_agents` with status `pending`
- If `tools` is None, uses `get_agent_tools(datasette)` (the standard plugin hook)
- Always adds `mark_finished` tool (never includes spawn/check tools - no recursion)
- Creates `asyncio.Task` via `asyncio.get_event_loop().create_task()`
- Stores task reference on `datasette._background_agent_tasks` dict (keyed by agent_id)
- Adds a `task.add_done_callback` to clean up the dict entry

```python
async def get_background_agent_status(datasette, agent_id) -> dict:
    """Returns {id, status, goal, final_message, error, conversation_id, created_at, updated_at}"""
```

- Queries `datasette_agent_background_agents` table

### 3. `background_tools.py` - Chat agent tools

Two `AgentTool` instances for the chat agent:

**`spawn_background_agent`**
- Input: `{"goal": str}`
- Uses `contextvars.ContextVar` to read the current `conversation_id` (set by `run_agent`)
- Calls `start_background_agent(datasette, actor, goal, spawned_by_conversation_id=current_conversation_id)`
- Returns immediately: `{"agent_id": "...", "conversation_id": "...", "status": "pending"}`

**`check_background_agent`**
- Input: `{"agent_id": str}`
- Calls `get_background_agent_status(datasette, agent_id)`
- Returns the full status dict as JSON

### 4. `context.py` - ContextVar for conversation_id

```python
import contextvars
current_conversation_id = contextvars.ContextVar('current_conversation_id', default=None)
```

## Modified Files

### `agent.py`
1. Import and set `current_conversation_id` ContextVar before `model.chain()` call
2. Before building the system prompt, check `datasette_agent_pending_notifications` for this conversation_id
3. If notifications exist: prepend them to `user_message`, delete the notification rows
4. Extract shared helpers (`_build_system_prompt`, `_build_conversation_history`, `_save_message`) - these are already module-level functions, so `background_agent.py` can import them directly

### `schema.py`
- Add the two new CREATE TABLE statements to `SCHEMA_SQL`

### `__init__.py`
1. Register new routes for background agent UI and API
2. Register `spawn_background_agent` and `check_background_agent` in `register_agent_tools`

### `views.py`
Add three new view functions:

- **`agent_background_index(request, datasette)`** - GET `/-/agent/background`
  - Lists background agents for current actor from `datasette_agent_background_agents`
  - Renders `agent_background.html` template with creation form

- **`api_create_background_agent(request, datasette)`** - POST `/-/agent/api/background`
  - Accepts `{"goal": str}`
  - Calls `start_background_agent()` from the Python API
  - Returns `{"agent_id": ..., "conversation_id": ...}`

- **`api_background_agent_status(request, datasette)`** - GET `/-/agent/api/background/{agent_id}`
  - Returns status JSON

### `templates/agent_background.html` (new)
- Form with textarea for goal + submit button
- Table listing background agents: id, goal (truncated), status, created_at
- Each row links to `/-/agent/{conversation_id}` to view full logs
- Status shown as a badge (pending/running/completed/error)

## Route Summary

New routes added to `register_routes()`:
```python
(r"^/-/agent/background$", views.agent_background_index),
(r"^/-/agent/api/background$", views.api_create_background_agent),
(r"^/-/agent/api/background/(?P<agent_id>[A-Za-z0-9]{26})$", views.api_background_agent_status),
```

## Implementation Order

1. Schema changes (`schema.py`) + context module (`context.py`)
2. Background agent runner (`background_agent.py`)
3. Python API (`api.py`)
4. Background tools for chat agent (`background_tools.py`)
5. Modify `agent.py` - set ContextVar, handle pending notifications
6. New views + routes (`views.py`, `__init__.py`)
7. New template (`agent_background.html`)
8. Register tools and routes in `__init__.py`
9. Tests

## Verification

1. **Unit test**: Start a background agent via `start_background_agent()`, verify it creates DB records, runs, and completes with `mark_finished`
2. **Unit test**: Verify "keep going" re-prompting when LLM stops without `mark_finished`
3. **Unit test**: Verify `spawn_background_agent` / `check_background_agent` tools work from chat context
4. **Unit test**: Verify pending notification is prepended to next chat message
5. **Integration test**: Hit the API endpoints (POST create, GET status)
6. **Manual**: Visit `/-/agent/background`, create a background agent, watch its conversation logs
