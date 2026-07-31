import inspect
import json
from dataclasses import dataclass
from typing import Callable

import llm as llm_library
from datasette.utils import await_me_maybe
from datasette.plugins import pm

from .questions import ToolContext


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


def tool_wants_context(agent_tool):
    return "context" in inspect.signature(agent_tool.fn).parameters


def make_tool_context(
    agent_tool,
    datasette,
    actor,
    *,
    conversation_id,
    arguments,
    tool_call_id=None,
    supports_questions=False,
    auto_approve=False,
    ask_user_callback=None,
    supports_browser_tasks=False,
    browser_task_callback=None,
):
    return ToolContext(
        datasette=datasette,
        actor=actor,
        conversation_id=conversation_id,
        tool_name=agent_tool.name,
        arguments=arguments,
        tool_call_id=tool_call_id,
        supports_questions=supports_questions,
        auto_approve=auto_approve,
        ask_user_callback=ask_user_callback,
        supports_browser_tasks=supports_browser_tasks,
        browser_task_callback=browser_task_callback,
    )


async def execute_agent_tool(
    agent_tool,
    datasette,
    actor,
    *,
    arguments,
    conversation_id=None,
    tool_call_id=None,
    supports_questions=False,
    auto_approve=False,
    ask_user_callback=None,
    supports_browser_tasks=False,
    browser_task_callback=None,
):
    """Execute one AgentTool: a fresh ToolContext per invocation for
    tools that declare `context`, consumed-question and consumed-task
    bookkeeping on success, output coerced to str. QuestionPending and
    BrowserTaskPending propagate to the caller.
    """
    kwargs = dict(arguments)
    if tool_wants_context(agent_tool):
        context = make_tool_context(
            agent_tool,
            datasette,
            actor,
            conversation_id=conversation_id,
            arguments=dict(kwargs),
            tool_call_id=tool_call_id,
            supports_questions=supports_questions,
            auto_approve=auto_approve,
            ask_user_callback=ask_user_callback,
            supports_browser_tasks=supports_browser_tasks,
            browser_task_callback=browser_task_callback,
        )
        result = await agent_tool.fn(
            datasette=datasette, actor=actor, context=context, **kwargs
        )
        # The call completed: its answered questions and finished
        # browser tasks must not replay for a later identical call.
        await context.mark_questions_consumed()
        await context.mark_browser_tasks_consumed()
    else:
        result = await agent_tool.fn(datasette=datasette, actor=actor, **kwargs)
    if result is not None and not isinstance(result, str):
        result = json.dumps(result, default=repr)
    return result


def make_llm_tools(
    agent_tools,
    datasette,
    actor,
    *,
    conversation_id=None,
    supports_questions=False,
    auto_approve=False,
    ask_user_callback=None,
    supports_browser_tasks=False,
    browser_task_callback=None,
):
    """Convert AgentTool instances to llm.Tool instances with context bound.

    Tools whose fn declares a `context` parameter receive a fresh
    ToolContext per invocation - constructed inside the implementation
    because tool calls can execute concurrently. The llm library passes
    the ToolCall object via the reserved llm_tool_call parameter.
    """
    llm_tools = []
    for agent_tool in agent_tools:
        if tool_wants_context(agent_tool):

            async def _impl(_agent_tool=agent_tool, llm_tool_call=None, **kwargs):
                return await execute_agent_tool(
                    _agent_tool,
                    datasette,
                    actor,
                    arguments=kwargs,
                    conversation_id=conversation_id,
                    tool_call_id=(
                        llm_tool_call.tool_call_id if llm_tool_call else None
                    ),
                    supports_questions=supports_questions,
                    auto_approve=auto_approve,
                    ask_user_callback=ask_user_callback,
                    supports_browser_tasks=supports_browser_tasks,
                    browser_task_callback=browser_task_callback,
                )

        else:

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
