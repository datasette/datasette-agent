from pluggy import HookspecMarker

hookspec = HookspecMarker("datasette")


@hookspec
def register_agent_tools(datasette):
    "Return a list of AgentTool instances, or an awaitable function returning that list"


@hookspec
def register_agent_client_tools(datasette):
    "Return AgentClientTool instances for tools that run in the browser"
