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
            r"^/-/agent/api/background/(?P<agent_id>[A-Za-z0-9]{26})/cancel$",
            views.api_cancel_background_agent,
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
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})/markdown$",
            views.agent_conversation_markdown,
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
def startup(datasette):
    from .api import reconcile_running_agents

    async def inner():
        await reconcile_running_agents(datasette)

    return inner


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
def register_commands(cli):
    import asyncio
    import click
    import json

    @cli.group()
    def agent():
        "Commands for the datasette-agent plugin"
        pass

    @agent.command()
    @click.option("--json", "output_json", is_flag=True, help="Output as JSON")
    def tools(output_json):
        "List available agent tools"
        from datasette.app import Datasette
        from .tools import get_agent_tools_by_plugin

        ds = Datasette(memory=True)
        grouped = asyncio.run(get_agent_tools_by_plugin(ds))

        if output_json:
            click.echo(
                json.dumps(
                    [
                        {
                            "name": t.name,
                            "description": t.description,
                            "input_schema": t.input_schema,
                            "plugin": plugin_name,
                        }
                        for plugin_name, plugin_tools in grouped.items()
                        for t in plugin_tools
                    ],
                    indent=2,
                )
            )
        else:
            for plugin_name, plugin_tools in grouped.items():
                click.echo(f"{plugin_name}:")
                for tool in plugin_tools:
                    click.echo(f"  {tool.name}")
                    click.echo(f"    {tool.description}")
                click.echo()

    @agent.command()
    @click.argument("files", nargs=-1, type=click.Path(exists=False))
    @click.option("-p", "--prompt", help="Initial prompt to send")
    @click.option("-m", "--model", "model_id", help="LLM model to use")
    def chat(files, prompt, model_id):
        "Interactive chat session with the agent"
        from datasette.app import Datasette
        from .cli_chat import run_chat

        db_files = []
        memory = False
        for f in files:
            if f == ":memory:":
                memory = True
            else:
                db_files.append(f)

        kwargs = {}
        if memory or not db_files:
            kwargs["memory"] = True

        metadata = {}
        if model_id:
            metadata = {"plugins": {"datasette-llm": {"default_model": model_id}}}

        ds = Datasette(db_files, metadata=metadata, **kwargs)
        asyncio.run(run_chat(ds, initial_prompt=prompt))


@hookimpl
def register_agent_tools(datasette):
    from .background_tools import get_background_tools
    from .sql_tools import get_default_tools

    return get_default_tools() + get_background_tools()
