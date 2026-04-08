import json
from datetime import datetime, timezone

from datasette_llm import LLM

from .agent import _build_conversation_history, _build_system_prompt, _save_message
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

    # Load agent record
    row = (
        await db.execute(
            "SELECT * FROM datasette_agent_background_agents WHERE id = ?",
            [agent_id],
        )
    ).first()
    if row is None:
        return

    goal = row["goal"]
    conversation_id = row["conversation_id"]
    spawned_by_conversation_id = row["spawned_by_conversation_id"]

    # Set status to running
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE datasette_agent_background_agents SET status = 'running', updated_at = ? WHERE id = ?",
        [now, agent_id],
    )

    # Build tools list
    if tools is None:
        tools = await get_agent_tools(datasette)
    # Filter out spawn/check tools to prevent recursion
    tools = [
        t
        for t in tools
        if t.name not in ("spawn_background_agent", "check_background_agent")
    ]

    # Add mark_finished tool
    finished_state = {"called": False, "message": None, "error": None}
    mark_finished_tool = _make_mark_finished_tool(finished_state)
    tools.append(mark_finished_tool)

    try:
        # Save initial goal as user message
        await _save_message(db, conversation_id, "user", content=goal)

        # Get model
        llm_instance = LLM(datasette)
        model = await llm_instance.model(purpose="agent", actor=actor)

        # Update conversation model_id
        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE datasette_agent_conversations SET model_id = ?, updated_at = ? WHERE id = ?",
            [model.model_id, now, conversation_id],
        )

        iteration = 0
        while not finished_state["called"] and iteration < MAX_ITERATIONS:
            iteration += 1

            # Build system prompt with conversation history
            system_prompt = await _build_system_prompt(datasette, actor)
            system_prompt += (
                f"\n\nYour goal: {goal}\n\n"
                "You MUST call the mark_finished tool when you have completed your goal. "
                "Do not stop working until you call mark_finished."
            )
            history = await _build_conversation_history(db, conversation_id)
            if history:
                system_prompt += history

            # Determine the prompt for this iteration
            if iteration == 1:
                prompt_text = goal
            else:
                # Re-prompt: keep going
                keep_going_msg = (
                    f"Keep going. Reminder: your goal is: {goal}. "
                    "You must call mark_finished() when done."
                )
                await _save_message(db, conversation_id, "user", content=keep_going_msg)
                prompt_text = keep_going_msg

            # Tool callbacks - DB only, no SSE
            async def before_call(tool, tool_call):
                await _save_message(
                    db,
                    conversation_id,
                    "tool_call",
                    tool_name=tool_call.name,
                    tool_arguments=json.dumps(tool_call.arguments),
                )

            async def after_call(tool, tool_call, tool_result):
                output = tool_result.output if tool_result.output else ""
                await _save_message(
                    db,
                    conversation_id,
                    "tool_result",
                    tool_name=tool_result.name,
                    tool_output=output,
                )
                # Strip keys the LLM shouldn't see
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

            llm_tools = make_llm_tools(tools, datasette, actor)

            chain_response = model.chain(
                prompt_text,
                system=system_prompt,
                tools=llm_tools,
                stream=False,
                before_call=before_call,
                after_call=after_call,
            )

            async for response in chain_response.responses():
                chunks = []
                async for chunk in response:
                    chunks.append(chunk)

                full_text = "".join(chunks)
                if full_text.strip():
                    await _save_message(
                        db, conversation_id, "assistant", content=full_text
                    )

        # Determine final status
        if finished_state["called"]:
            final_message = finished_state["message"]
            error = finished_state["error"]
            status = "error" if error else "completed"
        else:
            # Hit max iterations
            final_message = None
            error = f"Agent hit maximum iteration limit ({MAX_ITERATIONS})"
            status = "error"

        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE datasette_agent_background_agents "
            "SET status = ?, final_message = ?, error = ?, updated_at = ? "
            "WHERE id = ?",
            [status, final_message, error, now, agent_id],
        )

        # Create notification for spawning conversation
        if spawned_by_conversation_id:
            notification_content = (
                f"[Background agent {agent_id} {status}] "
                f"Goal: {goal}. "
                f"Result: {final_message or error or 'No message'}"
            )
            await db.execute_write(
                "INSERT INTO datasette_agent_pending_notifications "
                "(conversation_id, content, created_at) VALUES (?, ?, ?)",
                [spawned_by_conversation_id, notification_content, now],
            )

    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE datasette_agent_background_agents "
            "SET status = 'error', error = ?, updated_at = ? WHERE id = ?",
            [str(e), now, agent_id],
        )
