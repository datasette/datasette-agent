import json
import sys
from datetime import datetime, timezone

from datasette_llm import LLM

from .messages import (
    insert_message,
    insert_response,
    make_tool_message_dict,
    make_user_message_dict,
    prepare_tool_output_for_model,
)
from .schema import ensure_tables
from .tools import filter_tools_for_actor, get_agent_tools, make_llm_tools


async def run_chat(datasette, initial_prompt=None):
    await datasette.invoke_startup()
    db = datasette.get_internal_database()
    await ensure_tables(db)

    from ulid import ULID

    conversation_id = str(ULID())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, model_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [conversation_id, "cli", None, None, now, now],
    )

    actor = {"id": "cli"}

    agent_tools = await get_agent_tools(datasette)
    agent_tools = await filter_tools_for_actor(datasette, actor, agent_tools)
    # supports_questions stays False: the CLI has no answer UI, so
    # ask_user() raises QuestionsNotSupported instead of suspending.
    llm_tools = make_llm_tools(
        agent_tools, datasette, actor, conversation_id=conversation_id
    )

    llm_instance = LLM(datasette)
    model = await llm_instance.model(purpose="agent", actor=actor)

    from .agent import _build_system_prompt

    system_prompt = await _build_system_prompt(datasette, actor)

    pending_tool_messages = []

    async def before_call(tool, tool_call):
        args_str = json.dumps(tool_call.arguments, indent=2)
        print(f"\n--- Tool: {tool_call.name} ---")
        print(args_str)
        sys.stdout.flush()

    async def after_call(tool, tool_call, tool_result):
        output = tool_result.output or ""
        display = output if len(output) <= 500 else output[:500] + "..."
        print("--- Result ---")
        print(display)
        print("---")
        sys.stdout.flush()
        pending_tool_messages.append(
            make_tool_message_dict(tool_result.name, output, tool_result.tool_call_id)
        )
        tool_result.output = prepare_tool_output_for_model(output)

    conversation = model.conversation()

    first = True
    one_shot = initial_prompt is not None
    while True:
        if first and initial_prompt:
            user_message = initial_prompt
        else:
            try:
                user_message = input("\n> " if not first else "> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not user_message.strip():
                break
        first = False

        await insert_message(db, conversation_id, make_user_message_dict(user_message))

        chain_response = conversation.chain(
            user_message,
            system=system_prompt,
            stream=True,
            tools=llm_tools,
            before_call=before_call,
            after_call=after_call,
        )

        print()
        async for resp in chain_response.responses():
            async for chunk in resp:
                print(chunk, end="", flush=True)
            response_pk = await insert_response(db, conversation_id, resp)
            for tool_msg in pending_tool_messages:
                await insert_message(
                    db, conversation_id, tool_msg, response_id=response_pk
                )
            pending_tool_messages.clear()
        print()

        if one_shot:
            break
