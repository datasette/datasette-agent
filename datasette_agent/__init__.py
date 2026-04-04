from datasette import hookimpl
from datasette.permissions import Action
from datasette.plugins import pm

from . import hookspecs

pm.add_hookspecs(hookspecs)


@hookimpl
def register_actions():
    return [
        Action(
            name="datasette-agent",
            description="Access the agent chat assistant",
        ),
    ]


@hookimpl
def register_routes():
    from . import views
    from . import realtime_views

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
        (r"^/-/agent-realtime$", realtime_views.realtime_page),
        (r"^/-/agent-realtime/token$", realtime_views.realtime_token),
        (r"^/-/agent-realtime/tool-call$", realtime_views.realtime_tool_call),
    ]


@hookimpl
def skip_csrf(scope):
    return scope["path"].startswith("/-/agent/") or scope["path"].startswith(
        "/-/agent-realtime/"
    )


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
def prepare_jinja2_environment(env, datasette):
    import json

    def pretty_json(value):
        try:
            return json.dumps(json.loads(value), indent=2)
        except (json.JSONDecodeError, TypeError):
            return value

    env.filters["pretty_json"] = pretty_json

    def extract_html(value):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed.get("_html", "")
        except (json.JSONDecodeError, TypeError):
            pass
        return ""

    env.filters["extract_html"] = extract_html


@hookimpl
def register_agent_tools(datasette):
    from .sql_tools import get_default_tools

    return get_default_tools()
