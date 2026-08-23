import json
from datetime import datetime, timezone

import llm
from datasette_llm import LLM

from .agent import _build_system_prompt
from .messages import (
    insert_message,
    insert_response,
    load_messages,
    make_tool_message_dict,
    make_user_message_dict,
    prepare_tool_output_for_model,
)
from .schema import ensure_tables
from .tools import AgentTool, get_agent_tools, make_llm_tools

MAX_ITERATIONS = 50


def _make_mark_finished_tool(finished_state):
    """Create the mark_finished AgentTool with a closure over finished_state."""

    async def _mark_finished(datasette, actor, final_message, error=None):
        finished_state["called"] = True
        finished_state["message"] = final_message
        finished_state["error"] = error
        return json.dumps({"status": "finished"})

    return AgentTool(
        name="mark_finished",
        description=(
            "Call this tool when you have completed your goal. "
            "Pass a final_message summarizing what you accomplished. "
            "If you got stuck, pass an error message in the error parameter."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "final_message": {
                    "type": "string",
                    "description": "Summary of what was accomplished",
                },
                "error": {
                    "type": "string",
                    "description": "Error message if the agent got stuck",
                },
            },
            "required": ["final_message"],
        },
        fn=_mark_finished,
    )


async def run_background_agent(datasette, actor, agent_id, tools=None):
    """Run a background agent to completion. Designed to be launched as an asyncio.Task."""
    db = datasette.get_internal_database()
    await ensure_tables(db)

    row = (
        await db.execute(
            "SELECT * FROM agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return

    goal = row["goal"]
    conversation_id = row["conversation_id"]
    spawned_by_conversation_id = row["spawned_by_conversation_id"]

    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE agent_background_agents SET status = 'running', updated_at = ? WHERE id = ?",
        [now, agent_id],
    )

    if tools is None:
        tools = await get_agent_tools(datasette)
    tools = [
        t
        for t in tools
        if t.name not in ("spawn_background_agent", "check_background_agent")
    ]

    finished_state = {"called": False, "message": None, "error": None}
    mark_finished_tool = _make_mark_finished_tool(finished_state)
    tools.append(mark_finished_tool)

    try:
        # Save initial goal as the opening user message.
        await insert_message(db, conversation_id, make_user_message_dict(goal))

        llm_instance = LLM(datasette)
        model = await llm_instance.model(purpose="agent", actor=actor)

        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE agent_conversations SET model_id = ?, updated_at = ? WHERE id = ?",
            [model.model_id, now, conversation_id],
        )

        iteration = 0
        while not finished_state["called"] and iteration < MAX_ITERATIONS:
            iteration += 1

            system_prompt = await _build_system_prompt(datasette, actor)
            system_prompt += (
                f"\n\nYour goal: {goal}\n\n"
                "You MUST call the mark_finished tool when you have completed your goal. "
                "Do not stop working until you call mark_finished."
            )

            if iteration == 1:
                current_user_text = goal
            else:
                current_user_text = (
                    f"Keep going. Reminder: your goal is: {goal}. "
                    "You must call mark_finished() when done."
                )
                await insert_message(
                    db,
                    conversation_id,
                    make_user_message_dict(current_user_text),
                )

            chain_messages = await load_messages(db, conversation_id)

            pending_tool_messages = []

            async def before_call(tool, tool_call):
                return None

            async def after_call(tool, tool_call, tool_result):
                output = tool_result.output or ""
                pending_tool_messages.append(
                    make_tool_message_dict(
                        tool_result.name, output, tool_result.tool_call_id
                    )
                )
                tool_result.output = prepare_tool_output_for_model(output)

            # supports_questions stays False: nobody is watching a
            # background agent to answer, so ask_user() tells the tool
            # (via QuestionsNotSupported -> error result) to proceed
            # without input rather than suspending forever.
            llm_tools = make_llm_tools(
                tools, datasette, actor, conversation_id=conversation_id
            )

            chain_response = model.chain(
                current_user_text,
                messages=[llm.system(system_prompt), *chain_messages],
                system=system_prompt,
                tools=llm_tools,
                stream=False,
                before_call=before_call,
                after_call=after_call,
            )

            async for response in chain_response.responses():
                # Flush tool results from the PRIOR response before
                # persisting this one (after_call fires between yields).
                for tool_msg in pending_tool_messages:
                    await insert_message(db, conversation_id, tool_msg)
                pending_tool_messages.clear()

                async for _chunk in response:
                    pass
                await insert_response(db, conversation_id, response)

            # Final flush: catch tool results from the last response.
            for tool_msg in pending_tool_messages:
                await insert_message(db, conversation_id, tool_msg)
            pending_tool_messages.clear()

        if finished_state["called"]:
            final_message = finished_state["message"]
            error = finished_state["error"]
            status = "error" if error else "completed"
        else:
            final_message = None
            error = f"Agent hit maximum iteration limit ({MAX_ITERATIONS})"
            status = "error"

        now = datetime.now(timezone.utc).isoformat()
        # Insert the notification BEFORE flipping status, so observers that
        # poll status and then read notifications cannot see status =
        # 'completed' before the notification row is committed.
        if spawned_by_conversation_id:
            notification_content = (
                f"[Background agent {agent_id} {status}] "
                f"Goal: {goal}. "
                f"Result: {final_message or error or 'No message'}"
            )
            await db.execute_write(
                "INSERT INTO agent_pending_notifications "
                "(conversation_id, content, created_at) VALUES (?, ?, ?)",
                [spawned_by_conversation_id, notification_content, now],
            )

        # Guard with status='running' so a natural-completion write loses
        # to a cancel write that already flipped the row to 'error'.
        await db.execute_write(
            "UPDATE agent_background_agents "
            "SET status = ?, final_message = ?, error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running'",
            [status, final_message, error, now, agent_id],
        )

    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE agent_background_agents "
            "SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
            [str(e), now, agent_id],
        )
