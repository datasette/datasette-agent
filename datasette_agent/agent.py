import json
from datetime import datetime, timezone

import llm
from datasette_llm import LLM

from .context import current_conversation_id
from .schema import ensure_tables
from .tools import get_agent_tools, make_llm_tools


async def _build_system_prompt(datasette, actor):
    parts = [
        "You are a helpful data analysis assistant. "
        "You have access to tools that let you explore and query databases. "
        "Use the tools to answer questions about the data. "
        "The available databases and tables are listed below - do not call "
        "list_databases_and_tables or describe_table if you already have the "
        "information you need from this prompt or the conversation history. "
        "Only describe a table when you need column details you haven't seen yet.\n\n"
        "Your output will be rendered as markdown. "
        "Escape underscores in identifiers like column names and table names "
        "with a backslash (e.g. table\\_name, row\\_count) to prevent "
        "them from being interpreted as italic markers. "
        "This is not necessary inside backtick code spans like `table_name`."
    ]
    # Include available databases in system prompt
    db_info = {}
    for db_name, db in datasette.databases.items():
        if db_name.startswith("_"):
            continue
        tables = await db.table_names()
        if tables:
            db_info[db_name] = tables
    if db_info:
        parts.append("\nAvailable databases and tables:")
        for db_name, tables in db_info.items():
            parts.append(f"\n  {db_name}: {', '.join(tables)}")
    return "\n".join(parts)


def _strip_internal_keys(output):
    """Remove _html and sql keys the LLM shouldn't see from a tool-output JSON blob."""
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            stripped = {k: v for k, v in parsed.items() if k not in ("_html", "sql")}
            if stripped != parsed:
                return json.dumps(stripped)
    except (json.JSONDecodeError, TypeError):
        pass
    return output


def _load_provider_metadata(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


async def _build_conversation_messages(db, conversation_id):
    """Return prior conversation turns as a list of llm.Message objects.

    Assistant / reasoning / tool_call rows for one turn are coalesced into
    a single assistant Message carrying TextPart, ReasoningPart, and
    ToolCallParts in row order. tool_result rows become a tool-role
    Message. provider_metadata (e.g. Anthropic thinking signatures,
    Gemini thoughtSignature) is rehydrated onto each Part so the model
    sees the exact chain it sent last turn.
    """
    rows = (
        await db.execute(
            "SELECT role, content, tool_name, tool_arguments, tool_output, "
            "tool_call_id, provider_metadata, reasoning_redacted, "
            "reasoning_token_count FROM datasette_agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows

    messages = []
    pending_assistant_parts = None
    pending_tool_parts = None

    def flush():
        nonlocal pending_assistant_parts, pending_tool_parts
        if pending_assistant_parts is not None:
            messages.append(llm.Message(role="assistant", parts=pending_assistant_parts))
            pending_assistant_parts = None
        if pending_tool_parts is not None:
            messages.append(llm.Message(role="tool", parts=pending_tool_parts))
            pending_tool_parts = None

    for row in rows:
        role = row["role"]
        pm = _load_provider_metadata(row["provider_metadata"])

        if role == "user":
            flush()
            if row["content"]:
                messages.append(llm.user(row["content"]))
        elif role == "assistant":
            if pending_tool_parts is not None:
                flush()
            if pending_assistant_parts is None:
                pending_assistant_parts = []
            if row["content"]:
                pending_assistant_parts.append(
                    llm.TextPart(text=row["content"], provider_metadata=pm)
                )
        elif role == "reasoning":
            if pending_tool_parts is not None:
                flush()
            if pending_assistant_parts is None:
                pending_assistant_parts = []
            pending_assistant_parts.append(
                llm.ReasoningPart(
                    text=row["content"] or "",
                    redacted=bool(row["reasoning_redacted"]),
                    token_count=row["reasoning_token_count"],
                    provider_metadata=pm,
                )
            )
        elif role == "tool_call":
            if pending_tool_parts is not None:
                flush()
            if pending_assistant_parts is None:
                pending_assistant_parts = []
            try:
                arguments = json.loads(row["tool_arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            pending_assistant_parts.append(
                llm.ToolCallPart(
                    name=row["tool_name"] or "",
                    arguments=arguments,
                    tool_call_id=row["tool_call_id"],
                    provider_metadata=pm,
                )
            )
        elif role == "tool_result":
            if pending_assistant_parts is not None:
                flush()
            if pending_tool_parts is None:
                pending_tool_parts = []
            pending_tool_parts.append(
                llm.ToolResultPart(
                    name=row["tool_name"] or "",
                    output=_strip_internal_keys(row["tool_output"] or ""),
                    tool_call_id=row["tool_call_id"],
                    provider_metadata=pm,
                )
            )

    flush()
    return messages


async def _save_message(
    db,
    conversation_id,
    role,
    content=None,
    tool_name=None,
    tool_arguments=None,
    tool_output=None,
    tool_call_id=None,
    provider_metadata=None,
    reasoning_redacted=None,
    reasoning_token_count=None,
):
    now = datetime.now(timezone.utc).isoformat()
    pm_json = (
        json.dumps(provider_metadata) if provider_metadata is not None else None
    )
    await db.execute_write(
        "INSERT INTO datasette_agent_messages "
        "(conversation_id, role, content, tool_name, tool_arguments, tool_output, "
        "tool_call_id, provider_metadata, reasoning_redacted, reasoning_token_count, "
        "created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            conversation_id,
            role,
            content,
            tool_name,
            tool_arguments,
            tool_output,
            tool_call_id,
            pm_json,
            reasoning_redacted,
            reasoning_token_count,
            now,
        ],
    )
    await db.execute_write(
        "UPDATE datasette_agent_conversations SET updated_at = ? WHERE id = ?",
        [now, conversation_id],
    )


async def _save_response_parts(db, conversation_id, response):
    """Persist the assistant side of one chain response as Part rows.

    Walks response.messages (the structured assistant output) and writes
    one row per Part in the order they were emitted. Captures
    provider_metadata (Anthropic thinking signatures, Gemini
    thoughtSignature, OpenAI encrypted_content) so extended-thinking and
    signed tool-call chains round-trip across turns.
    """
    for message in response.messages:
        if message.role != "assistant":
            continue
        for part in message.parts:
            if isinstance(part, llm.TextPart):
                if part.text:
                    await _save_message(
                        db,
                        conversation_id,
                        "assistant",
                        content=part.text,
                        provider_metadata=part.provider_metadata,
                    )
            elif isinstance(part, llm.ReasoningPart):
                await _save_message(
                    db,
                    conversation_id,
                    "reasoning",
                    content=part.text or "",
                    provider_metadata=part.provider_metadata,
                    reasoning_redacted=1 if part.redacted else 0,
                    reasoning_token_count=part.token_count,
                )
            elif isinstance(part, llm.ToolCallPart):
                await _save_message(
                    db,
                    conversation_id,
                    "tool_call",
                    tool_name=part.name,
                    tool_arguments=json.dumps(part.arguments),
                    tool_call_id=part.tool_call_id,
                    provider_metadata=part.provider_metadata,
                )


async def _send_sse(writer, event, data):
    await writer.write(f"event: {event}\ndata: {json.dumps(data)}\n\n")


async def run_agent(datasette, actor, conversation_id, user_message, writer):
    db = datasette.get_internal_database()
    await ensure_tables(db)

    # Set context var so tools can access conversation_id
    current_conversation_id.set(conversation_id)

    # Check for pending notifications and prepend to user message
    notifications = (
        await db.execute(
            "SELECT id, content FROM datasette_agent_pending_notifications "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    if notifications:
        prefix_parts = [row["content"] for row in notifications]
        notification_ids = [row["id"] for row in notifications]
        for nid in notification_ids:
            await db.execute_write(
                "DELETE FROM datasette_agent_pending_notifications WHERE id = ?",
                [nid],
            )
        user_message = "\n".join(prefix_parts) + "\n\n" + user_message

    # Save user message
    await _save_message(db, conversation_id, "user", content=user_message)

    # Collect and convert tools
    agent_tools = await get_agent_tools(datasette)
    llm_tools = make_llm_tools(agent_tools, datasette, actor)

    # System prompt stays focused on the agent's role; prior turns flow
    # through as structured messages= below, not pasted into the system.
    system_prompt = await _build_system_prompt(datasette, actor)
    prior_messages = await _build_conversation_messages(db, conversation_id)

    # Get model
    llm_instance = LLM(datasette)
    model = await llm_instance.model(purpose="agent", actor=actor)

    # Update conversation model_id
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE datasette_agent_conversations SET model_id = ?, updated_at = ? WHERE id = ?",
        [model.model_id, now, conversation_id],
    )

    # Tool call callbacks for SSE streaming. Assistant-side rows
    # (text / reasoning / tool_call) are persisted from response.messages
    # in the outer loop below, so this callback only fires SSE.
    async def before_call(tool, tool_call):
        await _send_sse(
            writer,
            "tool_call",
            {
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        )

    async def after_call(tool, tool_call, tool_result):
        output = tool_result.output if tool_result.output else ""
        await _send_sse(
            writer,
            "tool_result",
            {
                "name": tool_result.name,
                "output": output,
            },
        )
        await _save_message(
            db,
            conversation_id,
            "tool_result",
            tool_name=tool_result.name,
            tool_output=output,
            tool_call_id=tool_result.tool_call_id,
        )
        # Strip keys the LLM shouldn't see (e.g. _html, sql) so it
        # doesn't parrot back raw HTML or talk about the SQL query
        tool_result.output = _strip_internal_keys(output)

    try:
        # prior_messages already includes the just-saved user turn, so
        # it's the full chain to send to the model. user_message is also
        # passed as prompt_text — real model adapters read prompt.messages
        # (the full chain), but legacy adapters like llm-echo still read
        # prompt.prompt, so we supply both.
        chain_response = model.chain(
            user_message,
            messages=prior_messages,
            system=system_prompt,
            tools=llm_tools,
            stream=True,
            before_call=before_call,
            after_call=after_call,
        )

        async for response in chain_response.responses():
            async for event in response.astream_events():
                if event.type == "text":
                    await _send_sse(writer, "text_chunk", {"content": event.chunk})
                elif event.type == "reasoning":
                    await _send_sse(
                        writer, "reasoning_chunk", {"content": event.chunk}
                    )
                # Other event types (tool_call_name, tool_call_args,
                # tool_result) are surfaced via before_call / after_call
                # once the chain framework invokes them.
            # response.messages is now populated with the assembled Parts
            # (text + reasoning + tool calls) including provider_metadata.
            # Persist them here so signatures round-trip on the next turn.
            await _save_response_parts(db, conversation_id, response)

        # Auto-generate title from first message if not set
        row = (
            await db.execute(
                "SELECT title FROM datasette_agent_conversations WHERE id = ?",
                [conversation_id],
            )
        ).first()
        if row and not row["title"]:
            # Use the first user message as a simple title
            title = user_message[:100]
            if len(user_message) > 100:
                title += "..."
            await db.execute_write(
                "UPDATE datasette_agent_conversations SET title = ? WHERE id = ?",
                [title, conversation_id],
            )

        await _send_sse(writer, "done", {})

    except Exception as e:
        await _send_sse(writer, "error", {"message": str(e)})
