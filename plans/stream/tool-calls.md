# Stream Tool Call Tokens to UI

## Context

When the LLM decides to call a tool, the user currently sees "Thinking..." and then the full tool call appears all at once. The raw JSON of tool call arguments is actually streaming in token-by-token from the LLM, but that data is consumed silently inside the `llm` library. The goal is to surface those streaming tokens in a subtle 3-line-high grey box so users can see activity happening.

## Key Dependency: llm "parts" branch

The `parts` branch of `simonw/llm` adds `StreamEvent` objects and `astream_events()`:

```python
@dataclass
class StreamEvent:
    type: str  # "text", "reasoning", "tool_call_name", "tool_call_args", ...
    chunk: str
    part_index: int
    tool_call_id: Optional[str] = None
```

- `response.astream_events()` yields `StreamEvent` objects including `tool_call_args` chunks (raw JSON fragments as the LLM generates them)
- The chain's `responses()` still calls `execute_tool_calls(before_call=..., after_call=...)` between responses, so existing callbacks continue to work
- Regular `async for chunk in response` only yields text strings - tool call data is invisible

**Without this branch, there is no way to access streaming tool call JSON** - the current `llm` API only exposes text chunks. The `before_call` callback fires after the full tool call is already parsed.

## Implementation Plan

### 1. Install llm from parts branch

Update `pyproject.toml` to depend on llm from the parts branch (or a minimum version once parts is merged). For now during development:

```
pip install 'llm @ git+https://github.com/simonw/llm.git@parts'
```

### 2. Modify `datasette_agent/agent.py` — server-side streaming

Change the inner response iteration loop (lines 213-221) from:

```python
async for response in chain_response.responses():
    chunks = []
    async for chunk in response:
        chunks.append(chunk)
        await _send_sse(writer, "text_chunk", {"content": chunk})
    full_text = "".join(chunks)
    if full_text.strip():
        await _save_message(db, conversation_id, "assistant", content=full_text)
```

To:

```python
async for response in chain_response.responses():
    chunks = []
    async for event in response.astream_events():
        if event.type == "text":
            chunks.append(event.chunk)
            await _send_sse(writer, "text_chunk", {"content": event.chunk})
        elif event.type == "tool_call_args":
            await _send_sse(writer, "tool_call_args_chunk", {"content": event.chunk})
    full_text = "".join(chunks)
    if full_text.strip():
        await _save_message(db, conversation_id, "assistant", content=full_text)
```

This adds a new SSE event type `tool_call_args_chunk`. The `before_call`/`after_call` callbacks remain unchanged - they still fire between responses for `tool_call` and `tool_result` SSE events.

**Graceful fallback**: Use `hasattr(response, 'astream_events')` to check if the parts branch is available. If not, fall back to the current `async for chunk in response` pattern. This keeps the code working with older llm versions.

### 3. Modify `datasette_agent/static/agent.js` — client-side display

Add a token stream box that appears during tool call generation:

- New function `getOrCreateTokenStream()`: creates/returns a small `<div class="agent-token-stream">` element appended to messages
- Contains a `<pre>` that shows rolling last ~3 lines of raw JSON
- On `tool_call_args_chunk` event: append text to the pre, trim to last 3 lines, scroll
- On `tool_call` event: remove the token stream box (the real tool call UI replaces it)
- Replace the "Thinking..." indicator logic: when `tool_call_args_chunk` arrives, remove the thinking indicator and show the token stream box instead

Key JS changes in the SSE event loop (~line 176-211):
```javascript
} else if (eventType === "tool_call_args_chunk") {
    // Remove thinking indicator, show token stream
    if (thinkingEl.parentNode) thinkingEl.remove();
    const tokenBox = getOrCreateTokenStream();
    appendToTokenStream(tokenBox, data.content);
    scrollToBottom();
} else if (eventType === "tool_call") {
    // Remove token stream box, show real tool call
    removeTokenStream();
    ...
}
```

### 4. Modify `datasette_agent/static/agent.css` — grey box styling

```css
.agent-token-stream {
    align-self: flex-start;
    max-width: 85%;
    height: 3.6em;         /* ~3 lines */
    overflow: hidden;
    background: #f5f5f5;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 0.3rem 0.5rem;
    font-family: monospace;
    font-size: 0.75rem;
    color: #999;
    line-height: 1.2em;
    white-space: pre;
}
```

### 5. Modify `datasette_agent/cli_chat.py` — CLI display

Change the inner loop (lines 112-115) similarly:

```python
async for resp in response.responses():
    async for event in resp.astream_events():
        if event.type == "text":
            print(event.chunk, end="", flush=True)
            full_text_parts.append(event.chunk)
        elif event.type == "tool_call_args":
            # Print dim grey on stderr
            print(f"\033[2m{event.chunk}\033[0m", end="", file=sys.stderr, flush=True)
```

Same `hasattr` fallback for older llm versions.

### Files to modify

1. `datasette_agent/agent.py` — switch to `astream_events()`, emit `tool_call_args_chunk` SSE
2. `datasette_agent/static/agent.js` — handle `tool_call_args_chunk`, create/manage grey box
3. `datasette_agent/static/agent.css` — style `.agent-token-stream`
4. `datasette_agent/cli_chat.py` — switch to `astream_events()`, dim stderr output

### Verification

1. Install llm from parts branch: `pip install 'llm @ git+https://github.com/simonw/llm.git@parts'`
2. Run `datasette agent chat` CLI and ask a question that triggers tool calls (e.g. "list all tables") — should see dim JSON streaming on stderr before the tool call appears
3. Run the web UI and ask the same — should see the grey box with streaming JSON that disappears when the tool call card appears
4. Verify existing tests still pass: `pytest tests/`
5. Verify graceful fallback: install regular llm and confirm everything still works (just without the streaming tokens)
