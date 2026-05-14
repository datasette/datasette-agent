from dataclasses import dataclass
from typing import Callable

import llm as llm_library
from datasette.utils import await_me_maybe
from datasette.plugins import pm


@dataclass
class AgentTool:
    name: str
    description: str
    input_schema: dict
    fn: Callable  # async fn(datasette, actor, **tool_params) -> str
    required_permission: str | None = None


async def get_agent_tools(datasette):
    tools = []
    for result in pm.hook.register_agent_tools(datasette=datasette):
        result = await await_me_maybe(result)
        if result:
            tools.extend(result)
    return tools


async def get_agent_tools_by_plugin(datasette):
    """Return dict mapping plugin name to list of AgentTool instances."""
    grouped = {}
    for impl in pm.hook.register_agent_tools.get_hookimpls():
        result = await await_me_maybe(impl.function(datasette=datasette))
        if result:
            grouped[impl.plugin_name] = list(result)
    return grouped


async def filter_tools_for_actor(datasette, actor, tools):
    """Drop tools whose `required_permission` the actor lacks."""
    out = []
    for tool in tools:
        if tool.required_permission and not await datasette.allowed(
            action=tool.required_permission, actor=actor
        ):
            continue
        out.append(tool)
    return out


def make_llm_tools(agent_tools, datasette, actor):
    """Convert AgentTool instances to llm.Tool instances with context bound."""
    llm_tools = []
    for agent_tool in agent_tools:

        async def _impl(_agent_tool=agent_tool, **kwargs):
            return await _agent_tool.fn(datasette=datasette, actor=actor, **kwargs)

        llm_tools.append(
            llm_library.Tool(
                name=agent_tool.name,
                description=agent_tool.description,
                input_schema=agent_tool.input_schema,
                implementation=_impl,
            )
        )
    return llm_tools
