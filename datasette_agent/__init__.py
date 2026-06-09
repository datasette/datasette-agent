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


def _agent_jump_section_script(datasette):
    import json

    create_conversation_url = datasette.urls.path("/-/agent/api/conversations")
    conversation_base_url = datasette.urls.path("/-/agent/")
    script = r"""
(function () {
  const createConversationUrl = __CREATE_CONVERSATION_URL__;
  const conversationBaseUrl = __CONVERSATION_BASE_URL__;
  const pluginVersion = __PLUGIN_VERSION__;
  let registered = false;

  function register(manager) {
    if (registered || !manager || typeof manager.registerPlugin !== "function") {
      return;
    }
    registered = true;
    manager.registerPlugin("datasette-agent", {
      version: pluginVersion,
      makeJumpSections() {
        return [
          {
            id: "datasette-agent-chat",
            render(node) {
              node.innerHTML = `
                <style>
                  .datasette-agent-jump-start {
                    display: flex;
                    flex-direction: column;
                    gap: 0.5rem;
                  }
                  .datasette-agent-jump-start label {
                    color: #111827;
                    font-size: 0.875rem;
                    font-weight: 600;
                  }
                  .datasette-agent-jump-start textarea {
                    border: 1px solid #d1d5db;
                    border-radius: 0.5rem;
                    box-sizing: border-box;
                    color: #111827;
                    font: inherit;
                    line-height: 1.4;
                    min-height: 4.75rem;
                    padding: 0.625rem 0.75rem;
                    resize: vertical;
                    width: 100%;
                  }
                  .datasette-agent-jump-start textarea:focus {
                    border-color: #2563eb;
                    outline: 2px solid rgba(37, 99, 235, 0.2);
                    outline-offset: 0;
                  }
                  .datasette-agent-jump-actions {
                    align-items: center;
                    display: flex;
                    gap: 0.75rem;
                  }
                  .datasette-agent-jump-start button {
                    background: #2563eb;
                    border: 0;
                    border-radius: 0.375rem;
                    color: white;
                    cursor: pointer;
                    font: inherit;
                    font-weight: 600;
                    padding: 0.45rem 0.75rem;
                  }
                  .datasette-agent-jump-start button:disabled {
                    cursor: default;
                    opacity: 0.65;
                  }
                  .datasette-agent-jump-hint,
                  .datasette-agent-jump-error {
                    font-size: 0.75rem;
                    line-height: 1.35;
                    margin: 0;
                  }
                  .datasette-agent-jump-hint {
                    color: #6b7280;
                    margin-top: -0.25rem;
                  }
                  .datasette-agent-jump-error {
                    color: #b91c1c;
                  }
                </style>
                <form class="datasette-agent-jump-start">
                  <label for="datasette-agent-jump-message">Start a new agent chat</label>
                  <textarea id="datasette-agent-jump-message" name="message" placeholder="Ask a question about your data..." rows="3"></textarea>
                  <p class="datasette-agent-jump-hint">Press Enter to start. Shift+Enter adds a new line.</p>
                  <div class="datasette-agent-jump-actions">
                    <button type="submit">Start chat</button>
                  </div>
                  <p class="datasette-agent-jump-error" data-agent-jump-error role="alert"></p>
                </form>`;

              const form = node.querySelector("form");
              const textarea = node.querySelector("textarea");
              const button = node.querySelector("button");
              const error = node.querySelector("[data-agent-jump-error]");

              textarea.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                  event.preventDefault();
                  form.requestSubmit();
                }
              });

              form.addEventListener("submit", async (event) => {
                event.preventDefault();
                const message = textarea.value.trim();
                if (!message) {
                  textarea.focus();
                  return;
                }

                error.textContent = "";
                button.disabled = true;
                button.textContent = "Starting...";

                try {
                  const response = await fetch(createConversationUrl, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({message}),
                  });
                  if (!response.ok) {
                    throw new Error("HTTP " + response.status);
                  }
                  const data = await response.json();
                  if (!data.conversation_id) {
                    throw new Error("Missing conversation ID");
                  }
                  try {
                    sessionStorage.setItem("agent_pending_message", message);
                  } catch (e) {}
                  window.location.assign(conversationBaseUrl + data.conversation_id);
                } catch (e) {
                  button.disabled = false;
                  button.textContent = "Start chat";
                  error.textContent = "Could not start chat";
                }
              });
            },
          },
        ];
      },
    });
  }

  if (window.__DATASETTE__) {
    register(window.__DATASETTE__);
  } else {
    document.addEventListener("datasette_init", function (event) {
      register(event.detail);
    });
  }
})();
"""
    script = script.replace(
        "__CREATE_CONVERSATION_URL__", json.dumps(create_conversation_url)
    )
    script = script.replace(
        "__CONVERSATION_BASE_URL__", json.dumps(conversation_base_url)
    )
    return script.replace("__PLUGIN_VERSION__", json.dumps(__version__))


@hookimpl
def extra_body_script(datasette, request):
    async def inner():
        if not request or not await datasette.allowed(
            action="datasette-agent", actor=request.actor
        ):
            return None
        return _agent_jump_section_script(datasette)

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
