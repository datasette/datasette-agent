"""Persistence helpers for agent messages and responses.

The schema stores one row per llm.MessageDict (user / assistant / tool).
Assistant rows come from response.to_dict()["messages"]; tool rows are
synthesized in after_call when the agent runs tools we executed.

llm 0.32+ guarantees that to_dict / from_dict round-trip provider_metadata
(Anthropic thinking signatures, Gemini thoughtSignature, etc.), so all of
that is preserved automatically inside message_json.
"""

import json
from datetime import datetime, timezone

import llm

MODEL_TOOL_OUTPUT_LIMIT = 10000
AGENT_OUTPUT_TRUNCATED_KEY = "agent_output_truncated"
TRUNCATED_STRING_SUFFIX = "... (truncated)"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def make_user_message_dict(text):
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


def make_tool_message_dict(name, output, tool_call_id):
    part = {"type": "tool_result", "name": name, "output": output or ""}
    if tool_call_id is not None:
        part["tool_call_id"] = tool_call_id
    return {"role": "tool", "parts": [part]}


def message_dict_text(msg_dict):
    """Concatenate text parts of a MessageDict (used for title / display)."""
    return "".join(
        p.get("text", "") for p in msg_dict.get("parts", []) if p.get("type") == "text"
    )


def strip_internal_keys(output):
    """Remove any top-level _-prefixed keys before sending tool output back
    to the model.

    Keys starting with _ are a side channel for user-only rendering and
    export (e.g. _html for inline HTML, _rows for the full rowset behind
    a summary). Tools opt in by prefixing the key; this function is the
    one place that decides what reaches the LLM.
    """
    if not output:
        return output
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return output
    if not isinstance(parsed, dict):
        return output
    stripped = {k: v for k, v in parsed.items() if not k.startswith("_")}
    if stripped == parsed:
        return output
    return json.dumps(stripped)


def _json_dumps_compact(value):
    return json.dumps(value, separators=(",", ":"))


def _serialized_size(value):
    return len(_json_dumps_compact(value))


def _truncate_string(value, string_prefix_length=None):
    if not value:
        return value, False
    if string_prefix_length is not None:
        if len(value) <= string_prefix_length:
            return value, False
        return value[:string_prefix_length] + TRUNCATED_STRING_SUFFIX, True
    if len(value) <= len(TRUNCATED_STRING_SUFFIX):
        return "", True
    keep = max(0, (len(value) // 2) - len(TRUNCATED_STRING_SUFFIX))
    return value[:keep] + TRUNCATED_STRING_SUFFIX, True


def _shrink_json_value(value, string_prefix_length=None):
    """Return (shrunken_value, changed) for one deterministic shrink step."""
    if isinstance(value, str):
        return _truncate_string(value, string_prefix_length)

    if isinstance(value, list):
        if len(value) > 1:
            return value[: max(1, len(value) // 2)], True
        if len(value) == 1:
            item, changed = _shrink_json_value(value[0], string_prefix_length)
            if changed:
                return [item], True
            return [], True
        return value, False

    if isinstance(value, dict):
        candidates = [
            (key, item)
            for key, item in value.items()
            if key != AGENT_OUTPUT_TRUNCATED_KEY
        ]
        for key, _ in sorted(
            candidates, key=lambda item: _serialized_size(item[1]), reverse=True
        ):
            item, changed = _shrink_json_value(value[key], string_prefix_length)
            if changed:
                shrunk = dict(value)
                shrunk[key] = item
                return shrunk, True
        if candidates:
            key, _ = max(candidates, key=lambda item: _serialized_size(item[1]))
            shrunk = dict(value)
            del shrunk[key]
            return shrunk, True
        return value, False

    return value, False


def _truncate_json_to_length(value, max_length):
    if not isinstance(value, dict):
        return value

    truncated = dict(value)
    truncated[AGENT_OUTPUT_TRUNCATED_KEY] = True

    while _serialized_size(truncated) > max_length:
        truncated, changed = _shrink_json_value(truncated)
        if not changed:
            minimal = {AGENT_OUTPUT_TRUNCATED_KEY: True}
            if _serialized_size(minimal) <= max_length:
                return minimal
            return truncated
    return truncated


def prepare_tool_output_for_model(output, max_length=MODEL_TOOL_OUTPUT_LIMIT):
    """Strip user/export-only keys and cap model-visible JSON safely.

    Tool outputs may carry rich user-facing content under top-level keys such
    as _html. The original output is persisted and streamed to the browser, but
    the LLM should see only the public keys. If those public keys are still too
    large, recursively shrink the parsed JSON until the serialized form fits,
    while keeping it valid JSON and adding agent_output_truncated=true.
    """
    stripped = strip_internal_keys(output)
    if not stripped:
        return stripped
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return stripped
    if not isinstance(parsed, dict):
        return stripped

    compact = _json_dumps_compact(parsed)
    if len(compact) <= max_length:
        return stripped if len(stripped) <= max_length else compact

    truncated = _truncate_json_to_length(parsed, max_length)
    return _json_dumps_compact(truncated)


async def insert_message(db, conversation_id, msg_dict, response_id=None):
    """Persist one MessageDict and bump the conversation's updated_at."""
    now = _utc_now()
    await db.execute_write(
        "INSERT INTO agent_messages (conversation_id, role, message_json, response_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            conversation_id,
            msg_dict.get("role", ""),
            json.dumps(msg_dict),
            response_id,
            now,
        ],
    )
    await db.execute_write(
        "UPDATE agent_conversations SET updated_at = ? WHERE id = ?",
        [now, conversation_id],
    )


async def insert_response(db, conversation_id, response):
    """Insert one agent_responses audit row + every assistant MessageDict
    from response.messages. Returns the agent_responses.id (used as
    response_id on subsequent tool-result rows).
    """
    data = response.to_dict()
    prompt = data.get("prompt") or {}
    usage = data.get("usage")
    options = prompt.get("options")
    cursor = await db.execute_write(
        "INSERT INTO agent_responses (conversation_id, model_id, llm_response_id, "
        "usage_json, options_json, system_prompt, datetime_utc, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            conversation_id,
            data.get("model"),
            data.get("id"),
            json.dumps(usage) if usage else None,
            json.dumps(options) if options else None,
            prompt.get("system"),
            data.get("datetime_utc"),
            _utc_now(),
        ],
    )
    response_pk = cursor.lastrowid
    for msg_dict in data.get("messages", []):
        await insert_message(db, conversation_id, msg_dict, response_id=response_pk)
    return response_pk


def flatten_for_render(rows):
    """Flatten agent_messages rows into the legacy per-Part dict shape the
    template + markdown export consume. Each row is a MessageDict; one
    Part becomes one output dict with keys: role, content, tool_name,
    tool_arguments, tool_output.

    role on output is one of: user, assistant, reasoning, tool_call,
    tool_result — matching what the template branches on.
    """
    out = []
    for row in rows:
        try:
            msg = json.loads(row["message_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        msg_role = msg.get("role", "")
        for part in msg.get("parts", []):
            ptype = part.get("type")
            if ptype == "text":
                out.append(
                    {
                        "role": msg_role,
                        "content": part.get("text", ""),
                        "tool_name": None,
                        "tool_arguments": None,
                        "tool_output": None,
                    }
                )
            elif ptype == "reasoning":
                out.append(
                    {
                        "role": "reasoning",
                        "content": part.get("text", ""),
                        "tool_name": None,
                        "tool_arguments": None,
                        "tool_output": None,
                    }
                )
            elif ptype == "tool_call":
                out.append(
                    {
                        "role": "tool_call",
                        "content": None,
                        "tool_name": part.get("name", ""),
                        "tool_arguments": json.dumps(part.get("arguments", {})),
                        "tool_output": None,
                    }
                )
            elif ptype == "tool_result":
                out.append(
                    {
                        "role": "tool_result",
                        "content": None,
                        "tool_name": part.get("name", ""),
                        "tool_arguments": None,
                        "tool_output": part.get("output", ""),
                    }
                )
    return out


async def load_messages(db, conversation_id):
    """Return the full prior message chain as llm.Message objects.

    Tool-result outputs are stripped of _html / _rows and capped here so the
    model never sees user/export-only bulk data — but the persisted row keeps
    the original payload so UI rendering / export still has access to the rich
    form.
    """
    rows = (
        await db.execute(
            "SELECT message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    messages = []
    for row in rows:
        msg_dict = json.loads(row["message_json"])
        if msg_dict.get("role") == "tool":
            for part in msg_dict.get("parts", []):
                if part.get("type") == "tool_result":
                    part["output"] = prepare_tool_output_for_model(
                        part.get("output", "")
                    )
        messages.append(llm.Message.from_dict(msg_dict))
    return messages
