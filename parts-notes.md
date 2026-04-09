# Notes on using the llm "parts" branch

## What we used

We used the `parts` branch of [simonw/llm](https://github.com/simonw/llm/tree/parts) to stream tool call argument tokens to the datasette-agent UI. Specifically:

### StreamEvent dataclass (`llm.parts.StreamEvent`)

```python
@dataclass
class StreamEvent:
    type: str  # "text", "reasoning", "tool_call_name", "tool_call_args", "tool_result"
    chunk: str
    part_index: int
    tool_call_id: Optional[str] = None
    server_executed: bool = False
    tool_name: Optional[str] = None
```

### `AsyncResponse.astream_events()` method

Replaces the basic `async for chunk in response` (which only yields text strings) with a richer stream that includes tool call data:

```python
async for event in response.astream_events():
    if event.type == "text":
        # Same as before - text content streaming
        pass
    elif event.type == "tool_call_args":
        # NEW - raw JSON fragments of tool call arguments as the LLM generates them
        pass
```

### How we integrated it

**In `agent.py` (web SSE streaming):**
```python
async for response in chain_response.responses():
    async for event in response.astream_events():
        if event.type == "text":
            await _send_sse(writer, "text_chunk", {"content": event.chunk})
        elif event.type == "tool_call_args":
            await _send_sse(writer, "tool_call_args_chunk", {"content": event.chunk})
```

**In `cli_chat.py` (terminal):**
```python
async for event in resp.astream_events():
    if event.type == "text":
        print(event.chunk, end="", flush=True)
    elif event.type == "tool_call_args":
        print(f"\033[2m{event.chunk}\033[0m", end="", file=sys.stderr, flush=True)
```

Both use `hasattr(response, 'astream_events')` to fall back to the old `async for chunk in response` pattern when the parts branch isn't installed.

## What worked well

1. **Drop-in replacement for the inner loop**: Switching from `async for chunk in response` to `async for event in response.astream_events()` was straightforward. Text events contain the same data, just wrapped in a StreamEvent.

2. **Callbacks still work**: The `before_call`/`after_call` callbacks on `chain_response` continue to fire between responses. The chain's `responses()` generator calls `execute_tool_calls()` after each response is consumed, so we get both:
   - Fine-grained streaming events (tool_call_args as they arrive)
   - Lifecycle callbacks (before_call with full parsed arguments, after_call with results)

3. **Backward compatibility via plain string wrapping**: If a plugin yields plain strings instead of StreamEvents, `astream_events()` wraps them as `StreamEvent(type="text", ...)`. This means the echo test model (llm-echo) works without any changes - it yields plain strings and they become text events.

4. **The event types make sense**: `tool_call_name`, `tool_call_args`, `text`, `reasoning` - these are the right categories. We only needed `text` and `tool_call_args` for our use case, but the taxonomy is clear.

## Feedback and suggestions for improvement

### 1. Consider adding `tool_call_args` events to `AsyncChainResponse.astream_events()` documentation

The chain-level `astream_events()` iterates through `responses()` which calls `execute_tool_calls()` between responses. This means:
- `tool_call_args` events stream during response generation
- Then there's a gap while tools execute (no events)
- Then the next response's events start streaming

This is correct behavior but the timing relationship between streaming events and callback invocation could be documented more explicitly. A consumer might wonder "when does before_call fire relative to tool_call_args events?" Answer: after all tool_call_args for that response have been yielded.

### 2. No `tool_result` events from `astream_events()`

When iterating `response.astream_events()` or `chain_response.astream_events()`, tool results are NOT yielded as events. They happen inside `execute_tool_calls()` via callbacks. This makes sense architecturally (tool execution is a side effect, not a streaming chunk from the model), but it means a consumer using ONLY `astream_events()` without callbacks would miss tool results entirely.

Consider: should `AsyncChainResponse.astream_events()` optionally yield synthetic `tool_result` events after tool execution? Something like:

```python
async def astream_events(self, include_tool_results=False):
    async for response_item in self.responses():
        async for event in response_item.astream_events():
            yield event
        if include_tool_results:
            for tool_result in response_item.tool_results:
                yield StreamEvent(type="tool_result", chunk=tool_result.output, ...)
```

This would let a consumer build a complete picture from a single iteration, without needing both events AND callbacks.

### 3. `part_index` isn't obviously useful for streaming consumers

The `part_index` field tracks which Part a StreamEvent contributes to. This makes sense for reconstructing `Part` objects after the fact, but for a streaming consumer (like our UI), we never used it. The `type` field was sufficient to route events.

However, `part_index` could be important when the model generates multiple tool calls in one response (parallel tool calls). In that case, you'd get interleaved `tool_call_args` events for different tool calls. We could use `tool_call_id` or `part_index` to demultiplex them. We didn't handle this case yet - it would be good to test.

### 4. `tool_call_name` events could include the full name directly

Currently `tool_call_name` uses `chunk` for the tool name, same as other event types. This is consistent but a dedicated `tool_name` field exists on StreamEvent and it's only set... sometimes? The semantics of `tool_name` vs `chunk` on a `tool_call_name` event could be clearer.

Looking at the OpenAI plugin, `tool_call_name` events set both `chunk` and `tool_name` to the same value. Clarify which one consumers should use.

### 5. Versioning / feature detection

We used `hasattr(response, 'astream_events')` for detection. Once parts merges to main and gets released, it would be nice to have a cleaner way to check, like `llm.HAS_PARTS` or a version check. The hasattr approach works but feels like a workaround.

### 6. The `llm-echo` test model doesn't yield StreamEvents

This means tests that use echo don't exercise the `tool_call_args` code path - only the text wrapping path. It would be useful for `llm-echo` to optionally yield StreamEvents (especially for tool calls) so that downstream projects like datasette-agent can write integration tests for the full event flow.

Maybe `llm-echo` could gain a mode where tool calls also produce `tool_call_name`/`tool_call_args` StreamEvents when streaming.

## Installation

```
pip install 'llm @ git+https://github.com/simonw/llm.git@parts'
```

Commit on parts branch at time of integration: `226d73d` ("Only package llm and llm.*")
