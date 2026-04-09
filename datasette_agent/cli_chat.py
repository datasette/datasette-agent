import asyncio
import json
import sys
from datetime import datetime, timezone

from datasette_llm import LLM

from .schema import ensure_tables
from .tools import get_agent_tools, make_llm_tools


async def run_chat(datasette, initial_prompt=None):
    await datasette.invoke_startup()
    db = datasette.get_internal_database()
    await ensure_tables(db)

    # Create a conversation
    from ulid import ULID

    conversation_id = str(ULID())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "INSERT INTO datasette_agent_conversations (id, actor_id, title, model_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [conversation_id, "cli", None, None, now, now],
    )

    actor = {"id": "cli"}

    # Collect tools
    agent_tools = await get_agent_tools(datasette)
    llm_tools = make_llm_tools(agent_tools, datasette, actor)

    # Get model
    llm_instance = LLM(datasette)
    model = await llm_instance.model(purpose="agent", actor=actor)

    # Build system prompt
    from .agent import _build_system_prompt

    system_prompt = await _build_system_prompt(datasette, actor)

    async def before_call(tool, tool_call):
        args_str = json.dumps(tool_call.arguments, indent=2)
        print(f"\n--- Tool: {tool_call.name} ---")
        print(args_str)
        sys.stdout.flush()

    async def after_call(tool, tool_call, tool_result):
        output = tool_result.output or ""
        # Truncate long output for display
        display = output
        if len(display) > 500:
            display = display[:500] + "..."
        print(f"--- Result ---")
        print(display)
        print(f"---")
        sys.stdout.flush()
        # Strip _html/sql keys
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

    conversation = model.conversation()

    first = True
    first_message = True
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

        # Save user message
        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "INSERT INTO datasette_agent_messages "
            "(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [conversation_id, "user", user_message, now],
        )

        chain_kwargs = {}
        if first_message:
            chain_kwargs["system"] = system_prompt
            first_message = False

        response = conversation.chain(
            user_message,
            stream=True,
            tools=llm_tools,
            before_call=before_call,
            after_call=after_call,
            **chain_kwargs,
        )

        print()
        full_text_parts = []
        async for resp in response.responses():
            if hasattr(resp, "astream_events"):
                async for event in resp.astream_events():
                    if event.type == "text":
                        print(event.chunk, end="", flush=True)
                        full_text_parts.append(event.chunk)
                    elif event.type == "tool_call_args":
                        print(
                            f"\033[2m{event.chunk}\033[0m",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
            else:
                async for chunk in resp:
                    print(chunk, end="", flush=True)
                    full_text_parts.append(chunk)
        print()

        full_text = "".join(full_text_parts)
        if full_text.strip():
            now = datetime.now(timezone.utc).isoformat()
            await db.execute_write(
                "INSERT INTO datasette_agent_messages "
                "(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [conversation_id, "assistant", full_text, now],
            )

        if one_shot:
            break
