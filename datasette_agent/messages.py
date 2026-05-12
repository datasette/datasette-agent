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

    Tool-result outputs are stripped of _html / sql keys here so the model
    never sees those — but the persisted row keeps the original payload so
    UI rendering / export still has access to the rich form.
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
                    part["output"] = strip_internal_keys(part.get("output", ""))
        messages.append(llm.Message.from_dict(msg_dict))
    return messages
