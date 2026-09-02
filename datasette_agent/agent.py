import json
from datetime import datetime, timezone

from datasette.resources import DatabaseResource
from datasette_llm import LLM

from .context import current_conversation_id
from .messages import (
    insert_message,
    insert_response,
    load_messages,
    make_tool_message_dict,
    make_user_message_dict,
    message_dict_text,
    prepare_tool_output_for_model,
    strip_internal_keys,
)
from .browser_tasks import BrowserTaskPending
from .models import AGENT_PURPOSE
from .questions import QuestionPending
from .schema import ensure_tables
from .tools import filter_tools_for_actor, get_agent_tools, make_llm_tools


async def _build_system_prompt(datasette, actor):
    parts = [
        "You are a helpful data analysis assistant. "
        "You have access to tools that let you explore and query databases. "
        "Use the tools to answer questions about the data. "
        "The available databases and tables are listed below - do not call "
        "list_databases_and_tables or describe_table if you already have the "
        "information you need from this prompt or the conversation history. "
        "Only describe a table when you need column details you haven't seen yet.\n\n"
        "When calling a tool, pass the arguments directly as a flat JSON object matching "
        "the tool's parameters. Always explicitly specify the 'database' parameter for any "
        "database tools (e.g., sql_query, describe_table) based on the available databases "
        "listed below. For sql_query, provide your SQL in the 'sql' parameter. "
        "Do NOT wrap tool arguments inside a 'kwargs' key (e.g., "
        'do not use {"kwargs": {"param": "value"}}).\n\n'
        "Your output will be rendered as markdown. "
        "Escape underscores in identifiers like column names and table names "
        "with a backslash (e.g. table\\_name, row\\_count) to prevent "
        "them from being interpreted as italic markers. "
        "This is not necessary inside backtick code spans like `table_name`.\n\n"
        "When you call sql_query, pick a `display` mode for the result:\n"
        "- `model` (default): rows come back to you only. Pick this when "
        "you need to reason over the numbers but won't be quoting them "
        "back to the user verbatim (e.g. counting rows before a join, "
        "checking a value to decide what to query next).\n"
        "- `both`: rows come back to you AND a rendered table is shown to "
        "the user. Pick this whenever your answer will reference specific "
        "data — the user gets the table inline, you get to comment on it.\n"
        "- `user`: a rendered table is shown to the user; you only see "
        "column names and row_count, not the rows themselves. Pick this "
        'for "show me…", "list the…", or "what are the top…" requests '
        "where the user wants to read the data and you don't need to "
        "summarize the contents. This saves tokens on bulk fetches.\n\n"
        "When you use `both` or `user`, the rendered table IS your answer "
        "for that data — do NOT then repeat the same rows as a markdown "
        "table or bullet list in your text. The user already sees the "
        "table inline. Your text response should add value the table "
        "doesn't: a one-line takeaway, a caveat, a follow-up suggestion, "
        "or just a brief framing sentence. If there's nothing to add, "
        "say nothing."
    ]
    db_info = {}
    for db_name, db in datasette.databases.items():
        if not await datasette.allowed(
            action="execute-sql",
            resource=DatabaseResource(database=db_name),
            actor=actor,
        ):
            continue
        tables = await db.table_names()
        if tables:
            db_info[db_name] = tables
    if db_info:
        parts.append("\nAvailable databases and tables:")
        for db_name, tables in db_info.items():
            parts.append(f"\n  {db_name}: {', '.join(tables)}")
    return "\n".join(parts)


async def _send_sse(writer, event, data):
    await writer.write(f"event: {event}\ndata: {json.dumps(data)}\n\n")


async def _run_chain(datasette, actor, conversation_id, writer, prompt_text):
    """Run one model.chain() over the conversation's persisted history,
    streaming SSE events and persisting messages.

    Returns ("question", question_dict) if a tool suspended on
    ask_user(), ("browser_task", task_dict) if it suspended on
    browser_task() (both are llm.PauseChain, so they propagate here
    from the chain), else None. The caller sends the final
    done/question/browser_task events. If the history ends in
    unresolved tool calls - a turn that previously suspended - the
    chain re-executes them through the normal tool machinery before
    calling the model.

    prompt_text is passed through to chain() for adapters that read
    prompt.prompt directly (e.g. echo); the persisted history passed as
    messages= is authoritative and must already contain the
    corresponding user row.
    """
    db = datasette.get_internal_database()

    agent_tools = await get_agent_tools(datasette)
    agent_tools = await filter_tools_for_actor(datasette, actor, agent_tools)
    llm_tools = make_llm_tools(
        agent_tools,
        datasette,
        actor,
        conversation_id=conversation_id,
        supports_questions=True,
        supports_browser_tasks=True,
    )

    system_prompt = await _build_system_prompt(datasette, actor)
    prior_messages = await load_messages(db, conversation_id)

    # A conversation is pinned to one model for its whole life. The
    # model is chosen when the conversation is created (or, for
    # conversations created without an explicit choice, resolved from
    # the datasette-llm default on the first turn) and written back to
    # the row so every later turn reuses it - even if the configured
    # default changes in between.
    conversation_row = (
        await db.execute(
            "SELECT model_id FROM agent_conversations WHERE id = ?",
            [conversation_id],
        )
    ).first()
    stored_model_id = conversation_row["model_id"] if conversation_row else None

    llm_instance = LLM(datasette)
    model = await llm_instance.model(
        model_id=stored_model_id, purpose=AGENT_PURPOSE, actor=actor
    )

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE agent_conversations SET model_id = ?, updated_at = ? WHERE id = ?",
        [model.model_id, now, conversation_id],
    )

    # Tool results from this turn's chain. Buffered so we insert them
    # AFTER the assistant message that called them — preserving logical
    # message order in agent_messages.
    pending_tool_messages = []
    streamed_tool_calls = {}

    async def flush_tool_messages():
        for tool_msg in pending_tool_messages:
            await insert_message(db, conversation_id, tool_msg)
        pending_tool_messages.clear()

    def tool_call_stream_id(obj):
        tool_call_id = getattr(obj, "tool_call_id", None)
        if tool_call_id is not None:
            return f"id:{tool_call_id}"
        part_index = getattr(obj, "part_index", None)
        if part_index is not None:
            return f"part:{part_index}"
        return None

    def matching_streamed_tool_call_id(tool_call, require_unfinalized=True):
        stream_id = tool_call_stream_id(tool_call)
        if stream_id is not None:
            return stream_id
        matches = [
            key
            for key, state in streamed_tool_calls.items()
            if state.get("name") == tool_call.name
            and (not require_unfinalized or not state.get("finalized"))
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def before_call(tool, tool_call):
        stream_id = matching_streamed_tool_call_id(tool_call)
        if stream_id in streamed_tool_calls:
            streamed_tool_calls[stream_id]["finalized"] = True
        await _send_sse(
            writer,
            "tool_call",
            {
                "id": stream_id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "streamed": bool(stream_id and stream_id in streamed_tool_calls),
            },
        )

    async def after_call(tool, tool_call, tool_result):
        output = tool_result.output or ""
        stream_id = tool_call_stream_id(tool_result) or matching_streamed_tool_call_id(
            tool_call, require_unfinalized=False
        )
        await _send_sse(
            writer,
            "tool_result",
            {
                "id": stream_id,
                "name": tool_result.name,
                "output": output,
            },
        )
        # Persist the original output (with _html / sql) for UI + export.
        pending_tool_messages.append(
            make_tool_message_dict(tool_result.name, output, tool_result.tool_call_id)
        )
        # Strip user-only keys and safely cap JSON before the model sees it.
        tool_result.output = prepare_tool_output_for_model(output)

    chain_response = model.chain(
        prompt_text,
        messages=prior_messages,
        system=system_prompt,
        tools=llm_tools,
        stream=True,
        before_call=before_call,
        after_call=after_call,
    )

    try:
        async for response in chain_response.responses():
            # Flush tool results from the PRIOR response before persisting
            # this one. after_call fires between chain.responses() yields,
            # so by the time we receive response N+1, pending_tool_messages
            # holds the results from response N's tool calls. They must
            # land between response N's assistant row and response N+1's
            # assistant row — otherwise the next turn rebuilds messages= in
            # an order OpenAI rejects ("tool_calls must be followed by
            # tool messages").
            await flush_tool_messages()

            async for event in response.astream_events():
                if event.type == "text":
                    await _send_sse(writer, "text_chunk", {"content": event.chunk})
                elif event.type == "reasoning" and event.chunk:
                    await _send_sse(writer, "reasoning_chunk", {"content": event.chunk})
                elif event.type == "tool_call_name":
                    stream_id = tool_call_stream_id(event)
                    if stream_id is None:
                        stream_id = f"streamed:{len(streamed_tool_calls)}"
                    state = streamed_tool_calls.setdefault(
                        stream_id, {"name": "", "args": ""}
                    )
                    state["name"] += event.chunk
                    if len(state["name"]) == len(event.chunk):
                        await _send_sse(
                            writer,
                            "tool_call_start",
                            {"id": stream_id, "name": state["name"]},
                        )
                elif event.type == "tool_call_args":
                    stream_id = tool_call_stream_id(event)
                    if stream_id is None:
                        stream_id = f"streamed:{len(streamed_tool_calls)}"
                    state = streamed_tool_calls.setdefault(
                        stream_id, {"name": "", "args": ""}
                    )
                    state["args"] += event.chunk
                    await _send_sse(
                        writer,
                        "tool_call_args_chunk",
                        {"id": stream_id, "chunk": event.chunk},
                    )
                # tool_result events are surfaced via after_call once the
                # chain framework invokes the local tool.

            await insert_response(db, conversation_id, response)

    except QuestionPending as ex:
        # A tool is waiting on ask_user(). PauseChain semantics
        # guarantee sibling tool calls completed (their results are
        # buffered via after_call) and no provider call was made with
        # the unfinished turn. No tool_result row is persisted for the
        # paused call - that missing row is what marks it pending, so
        # resuming the chain re-executes it.
        await flush_tool_messages()
        return ("question", ex.question)

    except BrowserTaskPending as ex:
        # A tool is waiting on browser_task() - same PauseChain
        # semantics as QuestionPending above.
        await flush_tool_messages()
        return ("browser_task", ex.task)

    # Final flush: tool results from the last response in the chain
    # (chain_limit hit or terminal tool call) would otherwise be lost.
    await flush_tool_messages()

    return None


async def _finish_turn(writer, pending):
    if pending is not None:
        event, data = pending
        await _send_sse(writer, event, data)
        await _send_sse(writer, "done", {"{}_pending".format(event): True})
    else:
        await _send_sse(writer, "done", {})


async def run_agent(datasette, actor, conversation_id, user_message, writer):
    db = datasette.get_internal_database()
    await ensure_tables(db)

    current_conversation_id.set(conversation_id)

    # Drain any pending notifications and prepend their text to the user
    # turn so the model sees them on this turn.
    notifications = (
        await db.execute(
            "SELECT id, content FROM agent_pending_notifications "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    if notifications:
        prefix_parts = [row["content"] for row in notifications]
        for nid in [row["id"] for row in notifications]:
            await db.execute_write(
                "DELETE FROM agent_pending_notifications WHERE id = ?",
                [nid],
            )
        user_message = "\n".join(prefix_parts) + "\n\n" + user_message

    try:
        # Persist the user turn as a MessageDict.
        await insert_message(db, conversation_id, make_user_message_dict(user_message))

        pending = await _run_chain(
            datasette, actor, conversation_id, writer, user_message
        )

        # Auto-set title from the first user message if not yet set.
        row = (
            await db.execute(
                "SELECT title FROM agent_conversations WHERE id = ?",
                [conversation_id],
            )
        ).first()
        if row and not row["title"]:
            title = user_message[:100]
            if len(user_message) > 100:
                title += "..."
            await db.execute_write(
                "UPDATE agent_conversations SET title = ? WHERE id = ?",
                [title, conversation_id],
            )

        await _finish_turn(writer, pending)

    except Exception as e:
        await _send_sse(writer, "error", {"message": str(e)})


async def resume_agent(datasette, actor, conversation_id, writer):
    """Resume a conversation suspended on ask_user() or browser_task().

    The persisted history ends in an assistant message with unresolved
    tool calls, so the llm chain re-executes them through the normal
    tool machinery before calling the model; answered questions and
    finished browser tasks replay from their tables instead of
    suspending again. May suspend again if a tool asks a further
    question or issues a further task.
    """
    db = datasette.get_internal_database()
    await ensure_tables(db)

    current_conversation_id.set(conversation_id)

    try:
        pending = await _run_chain(
            datasette, actor, conversation_id, writer, prompt_text=None
        )
        await _finish_turn(writer, pending)
    except Exception as e:
        await _send_sse(writer, "error", {"message": str(e)})


# Re-exports for background_agent.py / cli_chat.py compatibility.
__all__ = [
    "_build_system_prompt",
    "insert_message",
    "insert_response",
    "load_messages",
    "make_tool_message_dict",
    "make_user_message_dict",
    "message_dict_text",
    "prepare_tool_output_for_model",
    "run_agent",
    "strip_internal_keys",
]
