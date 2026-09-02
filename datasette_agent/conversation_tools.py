"""Built-in tools for consulting the user's other conversations.

Two chat-only tools let the agent pull context out of earlier chats
when the user says something like "remember we were talking about X
yesterday":

- ``search_conversations`` finds the user's other conversations whose
  messages match one or more search terms. Every call asks the user to
  approve the terms first, and the result deliberately reveals very
  little: title, id, start/end dates, message count and one short
  snippet per matching term. The per-call approval plus the tiny
  snippets are what stop the model from reconstructing a conversation
  it has not been granted access to through hundreds of searches.

- ``read_conversation`` reads a section of one conversation - user and
  assistant text plus tool calls and results - in pages, so the model
  can load just the parts it needs. The first read of a given
  conversation asks the user for permission; once granted, the grant is
  persisted for the rest of the current chat and further reads of that
  conversation (including its in-conversation search mode, which
  returns much longer context around each match) proceed without
  asking again.

Both tools only ever see conversations owned by the current actor, and
never the conversation they are being called from.
"""

import html as html_module
import json
import re
from datetime import datetime, timezone

from .messages import strip_internal_keys
from .tools import AgentTool

CONVERSATION_TOOL_NAMES = ("search_conversations", "read_conversation")

MAX_SEARCH_TERMS = 5
# Conversations returned per search term - keeps the per-call leak small.
MAX_SEARCH_RESULTS_PER_TERM = 10
# Characters of context either side of a match in search_conversations.
SEARCH_SNIPPET_CONTEXT = 60

# read_conversation paging: messages per call, default and maximum, and
# the character budget for one call's output (kept under the model-facing
# tool output cap so the lossy truncation in messages.py never kicks in).
READ_DEFAULT_LIMIT = 20
READ_MAX_LIMIT = 50
READ_MAX_CHARS = 8000
# Longest single text / tool output included verbatim in a read.
READ_PART_MAX_CHARS = 4000
# In-conversation search: context either side of a match, matches per call.
READ_SNIPPET_CONTEXT = 300
READ_MAX_MATCHES = 10

CONVERSATION_TOOLS_PROMPT = (
    "\n\nThe user may refer to something discussed in an earlier conversation "
    '("remember when we were talking about X yesterday"). Use '
    "search_conversations to find it - the user is asked to approve your "
    "search terms every time, so pick a few good terms per call - then "
    "read_conversation to load the sections you need once the user grants "
    "access to that conversation. Read sections and use its search mode "
    "rather than paging through whole conversations."
)

SEARCH_CONVERSATIONS_DESCRIPTION = """Search the user's other conversations with this assistant for text matching one or more terms.

Use this when the user refers to something discussed in an earlier conversation. Pass up to five search terms; matching is a case-insensitive substring match against user messages, assistant replies, tool calls and tool results. The user is asked to approve the terms before the search runs - every call asks, so choose terms carefully instead of searching repeatedly. If they decline, do not retry unless they ask you to.

Results show only each matching conversation's title, id, start and end dates, message count and a short snippet around the first match for each term. To read a conversation's actual content call read_conversation with its id."""

READ_CONVERSATION_DESCRIPTION = """Read part of one of the user's other conversations with this assistant, including tool calls and tool results.

The first time you read a given conversation the user is asked to grant access; if they decline, do not retry unless they ask you to. Once granted, further reads of that conversation in this chat do not ask again.

Messages are numbered from 1. Pass start and limit to read a page of messages (the output is capped, and next_start tells you where to continue). Alternatively pass search to find every occurrence of a term inside the conversation, with several hundred characters of context around each match and the message_index to read around. Prefer search plus small targeted pages over paging through an entire conversation."""


# --- helpers -------------------------------------------------------------


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _actor_id(actor):
    if actor and actor.get("id") is not None:
        return str(actor["id"])
    return None


def _actor_clause(actor_id):
    if actor_id is None:
        return "actor_id IS NULL", []
    return "actor_id = ?", [actor_id]


def _error(message, **extra):
    return json.dumps({"ok": False, "error": message, **extra})


def _format_datetime(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value


def like_pattern(text):
    """Build a LIKE pattern (used with ESCAPE '\\') matching text anywhere."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def json_like_pattern(term):
    """LIKE pattern matching term inside a json.dumps()-encoded string.

    agent_messages.message_json is written with json.dumps(), which
    escapes quotes, backslashes and every non-ASCII character. Encoding
    the term the same way means the pattern matches the stored form.
    """
    return like_pattern(json.dumps(term)[1:-1])


def normalize_terms(terms):
    """Validate the terms argument; returns a list or raises ValueError."""
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list):
        raise ValueError("terms must be an array of strings")
    cleaned = []
    for term in terms:
        if not isinstance(term, str):
            raise ValueError("terms must be an array of strings")
        term = term.strip()
        if term and term not in cleaned:
            cleaned.append(term)
    if not cleaned:
        raise ValueError("Provide at least one non-empty search term")
    if len(cleaned) > MAX_SEARCH_TERMS:
        raise ValueError("At most {} search terms per call".format(MAX_SEARCH_TERMS))
    return cleaned


def _term_pattern(term):
    return re.compile(re.escape(term), re.IGNORECASE)


def make_snippet(text, match_start, match_end, context):
    """Return text around [match_start, match_end) with whitespace collapsed."""
    start = max(0, match_start - context)
    end = min(len(text), match_end + context)
    snippet = " ".join(text[start:end].split())
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _message_parts(msg_dict):
    """The parts of a persisted MessageDict that the tools expose.

    Reasoning parts are skipped; tool results have their user-only
    _-prefixed keys (e.g. _html) stripped exactly as the model saw them.
    """
    parts = []
    for part in msg_dict.get("parts", []):
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"type": "text", "text": part.get("text") or ""})
        elif ptype == "tool_call":
            parts.append(
                {
                    "type": "tool_call",
                    "name": part.get("name") or "",
                    "arguments": part.get("arguments") or {},
                }
            )
        elif ptype == "tool_result":
            parts.append(
                {
                    "type": "tool_result",
                    "name": part.get("name") or "",
                    "output": strip_internal_keys(part.get("output") or "") or "",
                }
            )
    return parts


def _part_text(part):
    if part["type"] == "text":
        return part["text"]
    if part["type"] == "tool_call":
        return "{} {}".format(part["name"], json.dumps(part["arguments"]))
    return part["output"]


async def load_conversation_messages(db, conversation_id):
    """All messages of a conversation, numbered from 1 in stored order."""
    rows = (
        await db.execute(
            "SELECT role, message_json, created_at FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    messages = []
    for index, row in enumerate(rows, 1):
        try:
            msg_dict = json.loads(row["message_json"])
        except (json.JSONDecodeError, TypeError):
            msg_dict = {}
        messages.append(
            {
                "index": index,
                "role": row["role"],
                "created_at": row["created_at"],
                "parts": _message_parts(msg_dict),
            }
        )
    return messages


def _conversation_summary(row, message_count):
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "started_at": row["created_at"],
        "last_message_at": row["updated_at"],
        "message_count": message_count,
    }


async def _owned_conversation(db, actor_id, conversation_id):
    """The conversation row if it exists and belongs to the actor, else None."""
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    row = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?", [conversation_id]
        )
    ).first()
    if row is None or row["actor_id"] != actor_id:
        return None
    return row


# --- search_conversations ---------------------------------------------------


def _first_match(messages, title, pattern):
    """(count, snippet) for pattern across a conversation's messages."""
    count = 0
    snippet = None
    for message in messages:
        for part in message["parts"]:
            text = _part_text(part)
            for match in pattern.finditer(text):
                count += 1
                if snippet is None:
                    snippet = make_snippet(
                        text, match.start(), match.end(), SEARCH_SNIPPET_CONTEXT
                    )
    if snippet is None and title:
        match = pattern.search(title)
        if match:
            count += 1
            snippet = make_snippet(
                title, match.start(), match.end(), SEARCH_SNIPPET_CONTEXT
            )
    return count, snippet


async def _search_term(db, actor_id, exclude_id, term):
    """Conversations matching one term, newest first, capped per term.

    Returns (results, more) where each result is (row, message_count,
    count, snippet) and more is True when the cap was hit.
    """
    clause, params = _actor_clause(actor_id)
    candidates = (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE {} AND id != ? AND ("
            "title LIKE ? ESCAPE '\\' OR id IN ("
            "SELECT conversation_id FROM agent_messages "
            "WHERE message_json LIKE ? ESCAPE '\\')) "
            "ORDER BY updated_at DESC, id DESC".format(clause),
            params + [exclude_id or "", like_pattern(term), json_like_pattern(term)],
        )
    ).rows
    pattern = _term_pattern(term)
    results = []
    for row in candidates:
        messages = await load_conversation_messages(db, row["id"])
        count, snippet = _first_match(messages, row["title"], pattern)
        if not count:
            # LIKE matched JSON structure rather than message text.
            continue
        if len(results) >= MAX_SEARCH_RESULTS_PER_TERM:
            return results, True
        results.append((row, len(messages), count, snippet))
    return results, False


def _search_results_html(datasette, conversations):
    if not conversations:
        return "<p>No matching conversations.</p>"
    items = []
    for conversation in conversations:
        url = datasette.urls.path("/-/agent/{}".format(conversation["id"]))
        matches = "".join(
            "<li><code>{}</code> ({}): {}</li>".format(
                html_module.escape(match["term"]),
                match["count"],
                html_module.escape(match["snippet"] or ""),
            )
            for match in conversation["matches"]
        )
        items.append(
            '<li><a href="{}">{}</a> <small>{} to {}, {} messages</small>'
            "<ul>{}</ul></li>".format(
                html_module.escape(url),
                html_module.escape(conversation["title"] or conversation["id"]),
                html_module.escape(_format_datetime(conversation["started_at"])),
                html_module.escape(_format_datetime(conversation["last_message_at"])),
                conversation["message_count"],
                matches,
            )
        )
    return '<ul class="agent-conversation-search">{}</ul>'.format("".join(items))


async def search_conversations(datasette, actor, context, terms):
    try:
        terms = normalize_terms(terms)
    except ValueError as ex:
        return _error(str(ex))

    approved = await context.ask_user(
        "Search your other conversations for these terms?",
        html="<ul>{}</ul>".format(
            "".join(
                "<li><code>{}</code></li>".format(html_module.escape(term))
                for term in terms
            )
        ),
        text="\n".join("- {}".format(term) for term in terms),
    )
    if not approved:
        return json.dumps(
            {
                "ok": False,
                "cancelled": True,
                "message": "The user declined this search.",
            }
        )

    db = datasette.get_internal_database()
    actor_id = _actor_id(actor)
    by_id = {}
    truncated_terms = []
    for term in terms:
        results, more = await _search_term(db, actor_id, context.conversation_id, term)
        if more:
            truncated_terms.append(term)
        for row, message_count, count, snippet in results:
            entry = by_id.get(row["id"])
            if entry is None:
                entry = _conversation_summary(row, message_count)
                entry["matches"] = []
                by_id[row["id"]] = entry
            entry["matches"].append({"term": term, "count": count, "snippet": snippet})

    conversations = sorted(
        by_id.values(), key=lambda c: (c["last_message_at"], c["id"]), reverse=True
    )
    result = {
        "ok": True,
        "terms": terms,
        "conversations": conversations,
        "_html": _search_results_html(datasette, conversations),
    }
    if truncated_terms:
        result["truncated_terms"] = truncated_terms
        result["note"] = (
            "Only the {} most recent matching conversations are shown for: {}. "
            "Use more specific terms to narrow the search.".format(
                MAX_SEARCH_RESULTS_PER_TERM, ", ".join(truncated_terms)
            )
        )
    return json.dumps(result)


# --- read_conversation ------------------------------------------------------


async def has_conversation_grant(db, conversation_id, granted_conversation_id):
    if not conversation_id:
        return False
    row = (
        await db.execute(
            "SELECT 1 FROM agent_conversation_grants "
            "WHERE conversation_id = ? AND granted_conversation_id = ?",
            [conversation_id, granted_conversation_id],
        )
    ).first()
    return row is not None


async def record_conversation_grant(
    db, conversation_id, granted_conversation_id, actor_id
):
    if not conversation_id:
        return
    await db.execute_write(
        "INSERT OR IGNORE INTO agent_conversation_grants "
        "(conversation_id, granted_conversation_id, actor_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        [conversation_id, granted_conversation_id, actor_id, _utc_now()],
    )


def _grant_html(datasette, summary):
    url = datasette.urls.path("/-/agent/{}".format(summary["id"]))
    rows = [
        ("Title", summary["title"] or "(untitled)"),
        ("Started", _format_datetime(summary["started_at"])),
        ("Last message", _format_datetime(summary["last_message_at"])),
        ("Messages", str(summary["message_count"])),
    ]
    details = "".join(
        "<tr><th>{}</th><td>{}</td></tr>".format(
            html_module.escape(label), html_module.escape(value)
        )
        for label, value in rows
    )
    details += '<tr><th>Link</th><td><a href="{0}">{0}</a></td></tr>'.format(
        html_module.escape(url)
    )
    return (
        '<table class="agent-read-conversation-details">{}</table>'
        "<p>The agent will be able to read every message in this conversation, "
        "including tool calls and results, for the rest of this chat.</p>"
    ).format(details)


def _grant_text(summary):
    return (
        "Title: {}\nID: {}\nStarted: {}\nLast message: {}\nMessages: {}\n\n"
        "The agent will be able to read every message in this conversation, "
        "including tool calls and results, for the rest of this chat."
    ).format(
        summary["title"] or "(untitled)",
        summary["id"],
        _format_datetime(summary["started_at"]),
        _format_datetime(summary["last_message_at"]),
        summary["message_count"],
    )


def _truncate_text(text, limit=READ_PART_MAX_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated, {} characters total)".format(len(text))


def _render_part_for_read(part):
    if part["type"] == "text":
        return {"type": "text", "text": _truncate_text(part["text"])}
    if part["type"] == "tool_call":
        rendered = {"type": "tool_call", "name": part["name"]}
        arguments_json = json.dumps(part["arguments"])
        if len(arguments_json) <= READ_PART_MAX_CHARS:
            rendered["arguments"] = part["arguments"]
        else:
            rendered["arguments_json"] = _truncate_text(arguments_json)
        return rendered
    return {
        "type": "tool_result",
        "name": part["name"],
        "output": _truncate_text(part["output"]),
    }


def _read_section(messages, start, limit):
    requested = messages[start - 1 : start - 1 + limit]
    selected = []
    total = 0
    for message in requested:
        rendered = {
            "index": message["index"],
            "role": message["role"],
            "created_at": message["created_at"],
            "parts": [_render_part_for_read(p) for p in message["parts"]],
        }
        size = len(json.dumps(rendered))
        if selected and total + size > READ_MAX_CHARS:
            break
        selected.append(rendered)
        total += size
    end = start + len(selected) - 1 if selected else start - 1
    result = {
        "start": start,
        "end": end,
        "messages": selected,
        "next_start": end + 1 if end < len(messages) else None,
    }
    if len(selected) < len(requested):
        result["note"] = (
            "Output budget reached after {} of the {} requested messages - "
            "call again with start={} to continue.".format(
                len(selected), len(requested), end + 1
            )
        )
    return result


def _search_section(messages, term):
    pattern = _term_pattern(term)
    matches = []
    count = 0
    for message in messages:
        for part in message["parts"]:
            text = _part_text(part)
            for match in pattern.finditer(text):
                count += 1
                if len(matches) < READ_MAX_MATCHES:
                    matches.append(
                        {
                            "message_index": message["index"],
                            "role": message["role"],
                            "part": part["type"],
                            "snippet": make_snippet(
                                text, match.start(), match.end(), READ_SNIPPET_CONTEXT
                            ),
                        }
                    )
    result = {"search": term, "match_count": count, "matches": matches}
    if count > len(matches):
        result["note"] = (
            "Showing the first {} of {} matches - use read_conversation with "
            "start set to a message_index to read around later ones, or search "
            "for a more specific term.".format(len(matches), count)
        )
    return result


def _coerce_int(value, name, default, minimum, maximum):
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be an integer".format(name))
    if value < minimum:
        raise ValueError("{} must be at least {}".format(name, minimum))
    if maximum is not None and value > maximum:
        raise ValueError("{} must be at most {}".format(name, maximum))
    return value


async def read_conversation(
    datasette, actor, context, conversation_id, start=None, limit=None, search=None
):
    try:
        start = _coerce_int(start, "start", 1, 1, None)
        limit = _coerce_int(limit, "limit", READ_DEFAULT_LIMIT, 1, READ_MAX_LIMIT)
    except ValueError as ex:
        return _error(str(ex))
    if search is not None and (not isinstance(search, str) or not search.strip()):
        return _error("search must be a non-empty string")

    if context.conversation_id and conversation_id == context.conversation_id:
        return _error(
            "That is the current conversation - its messages are already in "
            "your context."
        )

    db = datasette.get_internal_database()
    actor_id = _actor_id(actor)
    row = await _owned_conversation(db, actor_id, conversation_id)
    if row is None:
        return _error("Conversation not found: {}".format(conversation_id))

    messages = await load_conversation_messages(db, conversation_id)
    summary = _conversation_summary(row, len(messages))

    if not await has_conversation_grant(db, context.conversation_id, conversation_id):
        approved = await context.ask_user(
            'Allow the agent to read the conversation "{}"?'.format(
                summary["title"] or conversation_id
            ),
            html=_grant_html(datasette, summary),
            text=_grant_text(summary),
        )
        if not approved:
            return json.dumps(
                {
                    "ok": False,
                    "cancelled": True,
                    "message": "The user declined access to this conversation.",
                }
            )
        await record_conversation_grant(
            db, context.conversation_id, conversation_id, actor_id
        )

    result = {"ok": True, "conversation": summary}
    if search is not None:
        result.update(_search_section(messages, search.strip()))
    else:
        if messages and start > len(messages):
            return _error(
                "start is beyond the last message - this conversation has {} "
                "messages".format(len(messages))
            )
        result.update(_read_section(messages, start, limit))
    return json.dumps(result)


def get_conversation_tools():
    return [
        AgentTool(
            name="search_conversations",
            description=SEARCH_CONVERSATIONS_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_SEARCH_TERMS,
                        "description": (
                            "Up to {} search terms; each is matched as a "
                            "case-insensitive substring".format(MAX_SEARCH_TERMS)
                        ),
                    },
                },
                "required": ["terms"],
            },
            fn=search_conversations,
        ),
        AgentTool(
            name="read_conversation",
            description=READ_CONVERSATION_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "ID of the conversation to read, from search_conversations",
                    },
                    "start": {
                        "type": "integer",
                        "description": "Index of the first message to read, starting at 1 (default 1)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to read (default {}, max {})".format(
                            READ_DEFAULT_LIMIT, READ_MAX_LIMIT
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Instead of reading a page of messages, find every "
                            "occurrence of this term in the conversation with "
                            "context around each match"
                        ),
                    },
                },
                "required": ["conversation_id"],
            },
            fn=read_conversation,
        ),
    ]
