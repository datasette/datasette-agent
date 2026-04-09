import json

from .context import current_conversation_id
from .tools import AgentTool


def get_background_tools():
    """Return AgentTool instances for spawning and checking background agents."""

    async def _spawn_background_agent(datasette, actor, goal):
        from .api import start_background_agent

        conversation_id = current_conversation_id.get()
        agent_id = await start_background_agent(
            datasette=datasette,
            actor=actor,
            goal=goal,
            spawned_by_conversation_id=conversation_id,
        )
        from .api import get_background_agent_status

        status = await get_background_agent_status(datasette, agent_id)
        return json.dumps(
            {
                "agent_id": agent_id,
                "conversation_id": status["conversation_id"],
                "status": status["status"],
            }
        )

    async def _check_background_agent(datasette, actor, agent_id):
        from .api import get_background_agent_status

        status = await get_background_agent_status(datasette, agent_id)
        if status is None:
            return json.dumps({"error": "Agent not found"})
        return json.dumps(status)

    return [
        AgentTool(
            name="spawn_background_agent",
            description=(
                "Start a background agent to work on a goal autonomously. "
                "Returns immediately with an agent_id you can use to check status later. "
                "The background agent will work independently until it completes its goal."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The goal for the background agent to accomplish",
                    },
                },
                "required": ["goal"],
            },
            fn=_spawn_background_agent,
        ),
        AgentTool(
            name="check_background_agent",
            description=(
                "Check the status of a background agent. "
                "Returns the agent's current status, goal, and result if completed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The ID of the background agent to check",
                    },
                },
                "required": ["agent_id"],
            },
            fn=_check_background_agent,
        ),
    ]
