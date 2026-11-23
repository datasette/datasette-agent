from pluggy import HookspecMarker

hookspec = HookspecMarker("datasette")


@hookspec
def register_agent_tools(datasette):
    "Return a list of AgentTool instances, or an awaitable function returning that list"


@hookspec
def register_agent_reference_types(datasette):
    "Return a list of ReferenceType instances, or an awaitable function returning that list"
