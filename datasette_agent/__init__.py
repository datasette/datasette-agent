from importlib.metadata import PackageNotFoundError, version as metadata_version

from datasette import hookimpl
from datasette.permissions import Action
from datasette.plugins import pm

from . import hookspecs

try:
    __version__ = metadata_version("datasette-agent")
except PackageNotFoundError:
    __version__ = "0+unknown"

pm.add_hookspecs(hookspecs)


@hookimpl
def register_actions():
    return [
        Action(
            name="datasette-agent",
            description="Access the agent chat assistant",
        ),
        Action(
            name="datasette-agent-explore",
            description="Launch and view AI explorer reports on databases and tables",
        ),
        Action(
            name="datasette-agent-background",
            description="Spawn and inspect background agents",
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
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})/poll$",
            views.agent_conversation_poll,
        ),
        (
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})/stream$",
            views.agent_stream,
        ),
        (
            r"^/-/agent/(?P<conversation_id>[A-Za-z0-9]{26})/question/(?P<question_id>[A-Za-z0-9]{26})$",
            views.api_answer_question,
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

    env.filters["agent_pretty_json"] = pretty_json

    def extract_html(value):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed.get("_html", "")
        except (json.JSONDecodeError, TypeError):
            pass
        return ""

    env.filters["agent_extract_html"] = extract_html

    def extract_edit_sql_url(value):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed.get("_edit_sql_url", "")
        except (json.JSONDecodeError, TypeError):
            pass
        return ""

    env.filters["agent_extract_edit_sql_url"] = extract_edit_sql_url

    def format_datetime(value):
        """Render persisted ISO 8601 timestamps as 'YYYY-MM-DD HH:MM:SS' —
        drop microseconds and the tz suffix for compact tabular display.
        Falls back to the original value when parsing fails so legacy or
        malformed entries still render something."""
        if not value:
            return ""
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    env.filters["agent_format_datetime"] = format_datetime


@hookimpl
def menu_links(datasette, actor, request):
    async def inner():
        if not await datasette.allowed(action="datasette-agent", actor=actor):
            return []
        return [
            {
                "href": datasette.urls.path("/-/agent"),
                "label": "Datasette Agent",
            }
        ]

    return inner


@hookimpl
def homepage_actions(datasette, actor, request):
    async def inner():
        if not await datasette.allowed(action="datasette-agent", actor=actor):
            return []
        return [
            {
                "href": datasette.urls.path("/-/agent"),
                "label": "Datasette Agent",
                "description": "Open the agent chat assistant",
            }
        ]

    return inner


def _agent_jump_config_script(datasette):
    """Inline bootstrap consumed by static/agent-jump.js, which reads this
    config lazily once the Datasette JS plugin manager initializes."""
    import json

    config = {
        "createConversationUrl": datasette.urls.path("/-/agent/api/conversations"),
        "conversationBaseUrl": datasette.urls.path("/-/agent/"),
        "pluginVersion": __version__,
    }
    return "window.datasetteAgentJumpConfig = {};".format(json.dumps(config))


@hookimpl
def extra_body_script(datasette, request):
    async def inner():
        if not request or not await datasette.allowed(
            action="datasette-agent", actor=request.actor
        ):
            return None
        return _agent_jump_config_script(datasette)

    return inner


@hookimpl
def extra_js_urls(datasette, request):
    async def inner():
        if not request or not await datasette.allowed(
            action="datasette-agent", actor=request.actor
        ):
            return []
        return [datasette.urls.static_plugins("datasette-agent", "agent-jump.js")]

    return inner


@hookimpl
def database_actions(datasette, actor, database, request):
    from datasette.utils import tilde_encode

    async def inner():
        if not await datasette.allowed(action="datasette-agent-explore", actor=actor):
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
        if not await datasette.allowed(action="datasette-agent-explore", actor=actor):
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
    @click.option(
        "--root",
        "use_root",
        is_flag=True,
        help="Run as Datasette root actor, allowing all permissions",
    )
    @click.option(
        "--yes",
        "auto_approve",
        is_flag=True,
        help="Auto-approve yes/no confirmation prompts",
    )
    @click.option("--unsafe", is_flag=True, help="Equivalent to --root --yes")
    def chat(files, prompt, model_id, use_root, auto_approve, unsafe):
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
        if unsafe:
            use_root = True
            auto_approve = True
        actor = {"id": "root"} if use_root else {"id": "cli"}
        if use_root:
            ds.root_enabled = True
        asyncio.run(
            run_chat(
                ds,
                initial_prompt=prompt,
                actor=actor,
                auto_approve=auto_approve,
            )
        )


@hookimpl
def register_agent_tools(datasette):
    from .background_tools import get_background_tools
    from .query_tools import get_query_tools
    from .sql_tools import get_default_tools

    return get_default_tools() + get_query_tools() + get_background_tools()
