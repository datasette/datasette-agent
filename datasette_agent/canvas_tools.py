"""Built-in show_in_canvas tool: open a Datasette page in the canvas
pane next to the chat.

The tool itself only validates the path and checks it renders for the
actor; the conversation page sees the `_canvas_url` key on the tool
result and points its canvas iframe at it. `_canvas_url` and `_html`
are stripped before the model sees the result.
"""

import html as html_module
import json
from urllib.parse import urlsplit

from datasette.utils import PrefixedUrlString

from .tools import AgentTool

# Path prefixes the canvas refuses to show - the agent's own chat UI
# nested inside its own canvas is never useful.
BLOCKED_PREFIXES = ("/-/agent",)

SHOW_IN_CANVAS_DESCRIPTION = """Open a page from this Datasette instance in the canvas, a pane shown next to the chat.

Use this to show the user a table, a filtered or sorted view of a table, a row page, a SQL query page (/database?sql=...), a stored query, or any other page served by this Datasette instance, including pages added by plugins.

path must be a path on this Datasette instance starting with /, including any query string - for example /mydb/mytable?_sort_desc=created or /mydb?sql=select+... Full URLs to other sites are not allowed.

The user can click around inside the canvas after you open it. Prefer this over describing where to find something."""


def _error(message):
    return json.dumps({"ok": False, "error": message})


def normalize_canvas_path(datasette, path):
    """Return (path, None) for a usable same-site path, or (None, error)."""
    if not isinstance(path, str) or not path.strip():
        return None, "path is required"
    path = path.strip()
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or not path.startswith("/") or "\\" in path:
        return None, (
            "path must be a path on this Datasette instance starting with /, "
            "not a full URL"
        )
    # Accept paths with or without the base_url prefix. The result is a
    # PrefixedUrlString so datasette.client doesn't prefix it again.
    base_url = datasette.setting("base_url") or "/"
    if base_url != "/" and path.startswith(base_url):
        path = PrefixedUrlString(path)
    else:
        path = datasette.urls.path(path)
    local = "/" + path[len(base_url) :]
    if any(
        local == prefix or local.startswith(prefix + "/") for prefix in BLOCKED_PREFIXES
    ):
        return None, "The agent chat pages cannot be shown in the canvas"
    return path, None


async def show_in_canvas(datasette, actor, context, path):
    if not context.supports_browser_tasks:
        return _error("The canvas is only available in interactive chat")
    path, error = normalize_canvas_path(datasette, path)
    if error:
        return _error(error)

    # Render the page as the actor so a typo or a permission problem
    # comes back to the model instead of a broken canvas.
    cookies = {}
    if actor:
        cookies["ds_actor"] = datasette.client.actor_cookie(actor)
    response = await datasette.client.get(path, cookies=cookies)
    if response.status_code >= 400:
        return _error(
            "{} returned HTTP {} - check the path and try again".format(
                path, response.status_code
            )
        )

    link = (
        '<p class="agent-canvas-opened">Opened in canvas: '
        '<a href="{0}" data-agent-canvas-link>{0}</a></p>'
    ).format(html_module.escape(path))
    return json.dumps(
        {
            "ok": True,
            "path": path,
            "message": "The page is now showing in the canvas next to the chat",
            "_canvas_url": path,
            "_html": link,
        }
    )


def get_canvas_tools():
    return [
        AgentTool(
            name="show_in_canvas",
            description=SHOW_IN_CANVAS_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path on this Datasette instance, starting with /, including any query string",
                    },
                },
                "required": ["path"],
            },
            fn=show_in_canvas,
        ),
    ]
