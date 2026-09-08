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
from .questions import QuestionsNotSupported
from .schema import ensure_tables
from .tools import filter_tools_for_actor, get_agent_tools, make_llm_tools


async def _ask_user_in_terminal(question):
    body = question.get("text")
    if body is None:
        body = question.get("html")
    if body:
        print()
        print(body)
        print()

    question_type = question["question_type"]
    prompt = question["prompt"]
    try:
        if question_type == "boolean":
            answer = input("{} [y/N] ".format(prompt))
            return answer.strip().lower() in {"y", "yes"}
        if question_type == "choice":
            options = question.get("options") or []
            for i, option in enumerate(options, 1):
                print("  {}. {}".format(i, option))
            while True:
                answer = input("{} ".format(prompt)).strip()
                if answer.isdigit() and 1 <= int(answer) <= len(options):
                    return options[int(answer) - 1]
                if answer in options:
                    return answer
                print("Enter one of: {}".format(", ".join(options)))
        if question_type == "text":
            return input("{} ".format(prompt))
    except (EOFError, KeyboardInterrupt) as ex:
        raise QuestionsNotSupported("No terminal input available") from ex
    raise QuestionsNotSupported("Unsupported question type: {}".format(question_type))


async def run_chat(datasette, initial_prompt=None, actor=None, auto_approve=False):
    await datasette.invoke_startup()
    db = datasette.get_internal_database()
    await ensure_tables(db)

    from ulid import ULID

    conversation_id = str(ULID())
    now = datetime.now(timezone.utc).isoformat()
    actor = actor or {"id": "cli"}
    actor_id = str(actor["id"]) if actor and actor.get("id") is not None else None
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, model_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [conversation_id, actor_id, None, None, now, now],
    )

    agent_tools = await get_agent_tools(datasette)
    agent_tools = await filter_tools_for_actor(datasette, actor, agent_tools)
    # supports_questions stays False: CLI questions are answered directly
    # through the callback rather than persisted for web replay.
    llm_tools = make_llm_tools(
        agent_tools,
        datasette,
        actor,
        conversation_id=conversation_id,
        auto_approve=auto_approve,
        ask_user_callback=_ask_user_in_terminal,
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
