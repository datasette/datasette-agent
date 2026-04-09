import json
from datetime import datetime, timezone

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


async def _build_conversation_history(db, conversation_id):
    messages = (
        await db.execute(
            "SELECT role, content, tool_name, tool_arguments, tool_output "
            "FROM datasette_agent_messages WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    if not messages:
        return ""
    parts = ["\n\nPrevious conversation:"]
    for msg in messages:
        role = msg["role"]
        if role == "user":
            parts.append(f"\nUser: {msg['content']}")
        elif role == "assistant":
            parts.append(f"\nAssistant: {msg['content']}")
        elif role == "tool_call":
            parts.append(f"\n[Tool call: {msg['tool_name']}({msg['tool_arguments']})]")
        elif role == "tool_result":
            output = msg["tool_output"] or ""
            # Strip keys the LLM shouldn't see (e.g. _html) before truncating
            try:
                parsed = json.loads(output)
                if isinstance(parsed, dict):
                    stripped = {
                        k: v for k, v in parsed.items() if k not in ("_html", "sql")
                    }
                    if stripped != parsed:
                        output = json.dumps(stripped)
            except (json.JSONDecodeError, TypeError):
                pass
            if len(output) > 500:
                output = output[:500] + "..."
            parts.append(f"\n[Tool result: {output}]")
    return "\n".join(parts)


async def _save_message(
    db,
    conversation_id,
    role,
    content=None,
    tool_name=None,
    tool_arguments=None,
    tool_output=None,
):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO datasette_agent_messages "
        "(conversation_id, role, content, tool_name, tool_arguments, tool_output, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [conversation_id, role, content, tool_name, tool_arguments, tool_output, now],
    )
    await db.execute_write(
        "UPDATE datasette_agent_conversations SET updated_at = ? WHERE id = ?",
        [now, conversation_id],
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

    # Build system prompt with conversation history
    system_prompt = await _build_system_prompt(datasette, actor)
    history = await _build_conversation_history(db, conversation_id)
    if history:
        system_prompt += history

    # Get model
    llm_instance = LLM(datasette)
    model = await llm_instance.model(purpose="agent", actor=actor)

    # Update conversation model_id
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE datasette_agent_conversations SET model_id = ?, updated_at = ? WHERE id = ?",
        [model.model_id, now, conversation_id],
    )

    # Tool call callbacks for SSE streaming
    async def before_call(tool, tool_call):
        await _send_sse(
            writer,
            "tool_call",
            {
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        )
        await _save_message(
            db,
            conversation_id,
            "tool_call",
            tool_name=tool_call.name,
            tool_arguments=json.dumps(tool_call.arguments),
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
        )
        # Strip keys the LLM shouldn't see (e.g. _html, sql) so it
        # doesn't parrot back raw HTML or talk about the SQL query
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                stripped = {
                    k: v for k, v in parsed.items() if k not in ("_html", "sql")
                }
                if stripped != parsed:
                    tool_result.output = json.dumps(stripped)
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        # Run the chain
        chain_response = model.chain(
            user_message,
            system=system_prompt,
            tools=llm_tools,
            stream=True,
            before_call=before_call,
            after_call=after_call,
        )

        async for response in chain_response.responses():
            chunks = []
            async for chunk in response:
                chunks.append(chunk)
                await _send_sse(writer, "text_chunk", {"content": chunk})

            full_text = "".join(chunks)
            if full_text.strip():
                await _save_message(db, conversation_id, "assistant", content=full_text)

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
