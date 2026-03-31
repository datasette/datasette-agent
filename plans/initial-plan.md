# datasette-agent Implementation Plan

## Context

Build a Datasette plugin that adds a plugin-extensible LLM chatbot agent at `/-/agent`. The agent uses datasette-llm for model access and defines a `register_agent_tools` plugin hook so other plugins can add tools. Ships with a default SQL query tool. Conversations are persisted with ULID IDs, scoped to actors, and streamed via SSE.

## Prerequisites: datasette-llm changes

**File**: `/Users/simon/dev/scratch/datasette-llm/datasette_llm/__init__.py`

`WrappedConversation` only has `.prompt()`. We need `.chain()` for tool-calling loops.

Add `WrappedConversation.chain()` that:
1. Invokes `llm_prompt_context` hooks once with the initial prompt
2. Delegates to the underlying `self._conversation.chain(prompt_text, key=self._key, **kwargs)`
3. Returns the `AsyncChainResponse` (which handles the tool loop internally)

Also add `WrappedAsyncModel.chain()` that creates a `WrappedConversation` and calls `.chain()` on it.

## File Structure

```
datasette_agent/
    __init__.py          # Hook registrations, plugin entry point
    hookspecs.py         # register_agent_tools hookspec
    tools.py             # AgentTool dataclass, tool-collection helper
    schema.py            # DB schema, ensure_tables()
    agent.py             # Core agent loop (chain + SSE streaming)
    views.py             # Route handlers
    sql_tools.py         # Default SQL tools (list DBs, describe table, query)
    static/
        agent.js         # Frontend SSE client + DOM updates
        agent.css        # Chat UI styling
    templates/
        agent_index.html         # Conversation list + new chat
        agent_conversation.html  # Chat UI
```

## Step 1: Update datasette-llm

Add `chain()` to `WrappedConversation` (after existing `prompt()` at line 260):

```python
def chain(self, prompt_text: str, **kwargs):
    """Execute a conversation chain with tool support, wrapped by hooks."""
    # Same hook setup as prompt()
    # But returns AsyncChainResponse directly
    return self._conversation.chain(prompt_text, key=self._key, **kwargs)
```

Note: Hook wrapping for chain is tricky since the chain runs asynchronously across multiple responses. For v1, pass through to the underlying conversation's `chain()` without hook wrapping — the hooks are primarily for budgeting/auditing and we can add chain-level hook support later.

Add `chain()` to `WrappedAsyncModel` similarly.

## Step 2: Plugin hook and AgentTool class

**`datasette_agent/hookspecs.py`**:
```python
@hookspec
def register_agent_tools(datasette):
    "Return a list of AgentTool instances"
```

Register with `pm.add_hookspecs(hookspecs)` in `__init__.py`.

**`datasette_agent/tools.py`** — `AgentTool` dataclass:
```python
@dataclass
class AgentTool:
    name: str
    description: str
    input_schema: dict          # JSON Schema for parameters
    fn: Callable                # async fn(datasette, actor, **tool_params) -> str
```

Helper `get_agent_tools(datasette)` collects tools from all plugins via the hook.

Helper `make_llm_tools(agent_tools, datasette, actor)` converts `AgentTool` list into `llm.Tool` list by creating closures that capture context.

## Step 3: Database schema

**`datasette_agent/schema.py`** — two tables in `datasette.get_internal_database()`:

```sql
CREATE TABLE IF NOT EXISTS datasette_agent_conversations (
    id TEXT PRIMARY KEY,        -- ULID
    actor_id TEXT,              -- nullable for anonymous
    title TEXT,
    model_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasette_agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES datasette_agent_conversations(id),
    role TEXT NOT NULL,          -- 'user', 'assistant', 'tool_call', 'tool_result'
    content TEXT,               -- text for user/assistant
    tool_name TEXT,             -- for tool_call/tool_result
    tool_arguments TEXT,        -- JSON, for tool_call
    tool_output TEXT,           -- for tool_result
    created_at TEXT NOT NULL
);
```

`ensure_tables(db)` function runs CREATE IF NOT EXISTS, called from views.

## Step 4: Routes and views

**`datasette_agent/views.py`** with routes registered in `__init__.py`:

| Route | Method | Handler | Purpose |
|-------|--------|---------|---------|
| `/-/agent` | GET | `agent_index` | List conversations, new chat button |
| `/-/agent/api/conversations` | POST | `api_create_conversation` | Create conversation, return `{id}` |
| `/-/agent/<id>` | GET | `agent_conversation` | Render chat UI with history |
| `/-/agent/<id>/stream` | POST | `agent_stream` | Accept message, return SSE stream |

All routes check a custom `agent` permission on the actor. Conversation views verify `actor_id` matches `request.actor["id"]`.

## Step 5: Agent loop

**`datasette_agent/agent.py`** — `run_agent()` async function:

1. Save user message to DB
2. Collect tools via `get_agent_tools(datasette)`, convert to `llm.Tool` list with actor context
3. Build system prompt with list of permitted databases and their tables
4. Get model via `LLM(datasette).model(purpose="agent", actor=actor)`
5. Reconstruct conversation: load prior messages from DB, include as formatted history in the system prompt (stateless approach — no in-memory conversation caching for v1)
6. Call `wrapped_model.chain(user_message, system=system_prompt, tools=llm_tools, stream=True)`
7. Iterate `async for response in chain_response.responses()`:
   - Stream text chunks via SSE: `async for chunk in response`
   - Use `after_call` callback on chain() to emit `tool_call` and `tool_result` SSE events and persist them to DB
   - After each response completes, save assistant message to DB
8. Send `done` SSE event

**SSE event format**:
```
event: text_chunk
data: {"content": "partial text"}

event: tool_call
data: {"name": "sql_query", "arguments": {...}}

event: tool_result
data: {"name": "sql_query", "output": "..."}

event: done
data: {}
```

**Streaming implementation**: Use `AsgiStream` from `datasette.utils.asgi` with `content_type="text/event-stream"`. The POST to `/stream` both receives the message and returns the SSE stream (avoids race conditions vs separate POST+GET).

## Step 6: Default SQL tools

**`datasette_agent/sql_tools.py`** — three `AgentTool` instances:

1. **`list_databases_and_tables`**: Lists all databases and their tables that the actor can access. Filters by `view-database` permission.

2. **`describe_table`**: Parameters: `database`, `table`. Returns column names, types, foreign keys. Checks `view-table` permission.

3. **`sql_query`**: Parameters: `database`, `sql`. Executes read-only SQL via `db.execute()`. Checks `execute-sql` permission. Returns JSON with `rows`, `columns`, `truncated`. Truncates output to ~10K chars to avoid overwhelming the model.

Registered via `register_agent_tools` hook in `__init__.py`.

## Step 7: Frontend

**`agent_conversation.html`**: Chat UI with message history (rendered server-side), input box at bottom. JavaScript handles sending messages and streaming.

**`agent.js`**:
- POST message to `/-/agent/{id}/stream` via `fetch()`
- Read response body as stream, parse SSE events
- Append text chunks to assistant message bubble
- Show tool calls/results in collapsible sections
- Uses `fetch()` + `response.body.getReader()` (not `EventSource`, which only supports GET)

**`agent_index.html`**: Simple list of conversations with titles, timestamps, and "New chat" button.

**`agent.css`**: Minimal chat styling — message bubbles, input area, tool call display.

## Step 8: Tests

**Using llm-echo** for deterministic testing. Add to dev dependencies.

**`tests/conftest.py`**: Fixtures providing a Datasette instance with echo model configured.

**Test cases**:
- Plugin installation (existing test)
- Route accessibility and permission checks
- Conversation CRUD (create, list, load)
- Actor scoping (can't see other actor's conversations)
- SQL tool execution (use llm-echo's `tool_calls` JSON to trigger tool calls)
- Tool results persistence in DB
- SSE stream format validation
- Custom tool registration via hook

## Step 9: pyproject.toml updates

```toml
dependencies = [
    "datasette",
    "datasette-llm",
    "python-ulid",
]

[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
    "llm-echo>=0.3",
]
```

## Implementation order

1. datasette-llm: add `chain()` to `WrappedConversation` and `WrappedAsyncModel`
2. `hookspecs.py`, `tools.py` — plugin hook and AgentTool class
3. `schema.py` — DB tables
4. `sql_tools.py` — default tools
5. `__init__.py` — wire up hooks and routes
6. `views.py` — route handlers
7. `agent.py` — core agent loop with SSE streaming
8. Templates and static files — chat UI
9. Tests

## Verification

1. `uv run pytest` — all tests pass
2. Manual: `uv run datasette --memory` → navigate to `/-/agent` → start conversation → ask "what tables are available?" → verify tool calls and streaming response
3. Verify conversation persists across page reloads
4. Verify actor scoping (different actors can't see each other's conversations)
