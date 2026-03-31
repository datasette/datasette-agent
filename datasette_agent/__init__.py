from datasette import hookimpl
from datasette.plugins import pm

from . import hookspecs

pm.add_hookspecs(hookspecs)


@hookimpl
def register_routes():
    from . import views

    return [
        (r"^/-/agent$", views.agent_index),
        (r"^/-/agent/api/conversations$", views.api_create_conversation),
        (
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})$",
            views.agent_conversation,
        ),
        (
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})/stream$",
            views.agent_stream,
        ),
    ]


@hookimpl
def skip_csrf(scope):
    return scope["path"].startswith("/-/agent/")


@hookimpl
def register_llm_purposes(datasette):
    from datasette_llm import Purpose

    return [
        Purpose(
            name="agent",
            description="Agent chat assistant for exploring and querying data",
        )
    ]


@hookimpl
def register_agent_tools(datasette):
    from .sql_tools import get_default_tools

    return get_default_tools()
