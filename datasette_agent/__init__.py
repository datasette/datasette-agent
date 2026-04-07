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

    return [
        (r"^/-/agent$", views.agent_index),
        (r"^/-/agent/api/conversations$", views.api_create_conversation),
        (r"^/-/agent/background$", views.agent_background_index),
        (r"^/-/agent/api/background$", views.api_create_background_agent),
        (
            r"^/-/agent/api/background/(?P<agent_id>[A-Za-z0-9]{26})$",
            views.api_background_agent_status,
        ),
        (
            r"^/-/agent/explore/report/(?P<report_id>[A-Za-z0-9]{26})$",
            views.explorer_report_page,
        ),
        (
            r"^/-/agent/explore/(?P<database>[^/]+)/(?P<table>[^/]+)$",
            views.explorer_page,
        ),
        (r"^/-/agent/explore/(?P<database>[^/]+)$", views.explorer_page),
        (r"^/-/agent/api/explore$", views.api_start_explorer),
        (
            r"^/-/agent/api/explore/(?P<report_id>[A-Za-z0-9]{26})$",
            views.api_explorer_report,
        ),
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
def database_actions(datasette, actor, database, request):
    from datasette.utils import tilde_encode

    async def inner():
        if not await datasette.allowed(action="datasette-agent", actor=actor):
            return []
        return [
            {
                "href": datasette.urls.path(
                    f"/-/agent/explore/{tilde_encode(database)}"
                ),
                "label": "Explore with AI agent",
                "description": "Launch an AI agent to explore this database and generate a report",
            }
        ]

    return inner


@hookimpl
def table_actions(datasette, actor, database, table, request):
    from datasette.utils import tilde_encode

    async def inner():
        if not await datasette.allowed(action="datasette-agent", actor=actor):
            return []
        return [
            {
                "href": datasette.urls.path(
                    f"/-/agent/explore/{tilde_encode(database)}/{tilde_encode(table)}"
                ),
                "label": "Explore with AI agent",
                "description": "Launch an AI agent to explore this table and generate a report",
            }
        ]

    return inner


@hookimpl
def register_agent_tools(datasette):
    from .background_tools import get_background_tools
    from .sql_tools import get_default_tools

    return get_default_tools() + get_background_tools()
