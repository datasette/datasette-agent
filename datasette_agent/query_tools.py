"""Built-in save_query tool: store a query in Datasette's stored queries.

Validation and persistence go through Datasette's own JSON endpoints
(GET /db/-/queries/analyze, POST /db/-/queries/store) called via
datasette.client as the requesting actor, so the rules - name
validation, duplicate detection, is_write analysis, write-permission
enforcement - stay exactly in sync with the web UI.

Saving always requires explicit human approval via context.ask_user(),
with the full SQL displayed to the user.
"""

import html as html_module
import json

from datasette.resources import DatabaseResource

from .tools import AgentTool

SAVE_QUERY_DESCRIPTION = """Save a SQL query as a Datasette stored query so it can be re-run later from its own URL.

The query may be read-only SQL or write SQL (INSERT/UPDATE/DELETE) - Datasette detects which automatically. Named parameters like :name become form fields on the query page.

The user is shown the full SQL and asked to approve before anything is saved. If they decline, do not retry unless they ask you to.

name must be unique within the database; if it is already taken, pick a different name. Saved queries are private to the user by default - pass is_private false to make the query visible to others."""


def _error(message, **extra):
    return json.dumps({"ok": False, "error": message, **extra})


def _approval_html(database, name, title, sql, is_write, is_private):
    rows = [
        ("Database", database),
        ("Name", name),
        ("Title", title or ""),
        ("Type", "write query" if is_write else "read-only query"),
        ("Visibility", "private" if is_private else "visible to others"),
    ]
    details = "".join(
        "<tr><th>{}</th><td>{}</td></tr>".format(
            html_module.escape(label), html_module.escape(str(value))
        )
        for label, value in rows
        if value
    )
    return (
        '<table class="agent-save-query-details">{}</table>'
        "<pre>{}</pre>".format(details, html_module.escape(sql))
    )


async def save_query(
    datasette,
    actor,
    context,
    database,
    name,
    sql,
    title=None,
    description=None,
    is_private=True,
):
    try:
        datasette.get_database(database)
    except KeyError:
        return _error("Database not found: {}".format(database))

    # Pre-check permissions so the user is never asked to approve a save
    # that would be rejected anyway.
    for action in ("execute-sql", "store-query"):
        if not await datasette.allowed(
            action=action, resource=DatabaseResource(database), actor=actor
        ):
            return _error(
                "Permission denied: saving queries requires the {} permission "
                "on database '{}'".format(action, database)
            )

    cookies = {}
    if actor:
        cookies["ds_actor"] = datasette.client.actor_cookie(actor)

    # Analyze first: invalid SQL or operations the actor may not perform
    # come straight back to the model without bothering the user.
    analyze_response = await datasette.client.get(
        "{}/-/queries/analyze".format(datasette.urls.database(database)),
        params={"sql": sql},
        cookies=cookies,
    )
    if analyze_response.status_code != 200:
        return _error(
            "Could not analyze query (HTTP {})".format(analyze_response.status_code)
        )
    analysis = analyze_response.json()
    if analysis.get("analysis_error"):
        return _error(analysis["analysis_error"])
    if analysis.get("save_disabled"):
        return _error(
            "This query cannot be saved - it uses operations you do not have "
            "permission to perform"
        )
    is_write = bool(analysis.get("analysis_is_write"))

    if await datasette.get_query(database, name) is not None:
        return _error(
            'A query named "{}" already exists in database "{}" - '
            "pick a different name".format(name, database)
        )

    approved = await context.ask_user(
        'Save this {} query as "{}" in database "{}"?'.format(
            "write" if is_write else "read-only", name, database
        ),
        html=_approval_html(database, name, title, sql, is_write, is_private),
    )
    if not approved:
        return json.dumps(
            {
                "ok": False,
                "cancelled": True,
                "message": "The user declined to save this query.",
            }
        )

    query_data = {
        "name": name,
        "sql": sql,
        "is_private": bool(is_private),
    }
    if title:
        query_data["title"] = title
    if description:
        query_data["description"] = description
    store_response = await datasette.client.post(
        "{}/-/queries/store".format(datasette.urls.database(database)),
        json={"query": query_data},
        cookies=cookies,
    )
    if store_response.status_code != 201:
        try:
            errors = store_response.json().get("errors") or []
            message = "; ".join(errors) or "HTTP {}".format(
                store_response.status_code
            )
        except (json.JSONDecodeError, ValueError):
            message = "HTTP {}".format(store_response.status_code)
        return _error("Could not save query: {}".format(message))

    saved = store_response.json()["query"]
    url = datasette.urls.table(database, name)
    return json.dumps(
        {
            "ok": True,
            "database": database,
            "name": name,
            "url": url,
            "is_write": saved["is_write"],
            "is_private": saved["is_private"],
            "parameters": saved.get("parameters") or [],
            "_html": '<p>Saved query: <a href="{}">{}</a></p>'.format(
                html_module.escape(url), html_module.escape(name)
            ),
        }
    )


def get_query_tools():
    return [
        AgentTool(
            name="save_query",
            description=SAVE_QUERY_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Database to save the query in",
                    },
                    "name": {
                        "type": "string",
                        "description": "Unique name for the stored query (used in its URL)",
                    },
                    "sql": {
                        "type": "string",
                        "description": "The SQL to save - read-only or write, with optional :named parameters",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional human-readable title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional longer description",
                    },
                    "is_private": {
                        "type": "boolean",
                        "description": "Keep the query private to the user (default true)",
                    },
                },
                "required": ["database", "name", "sql"],
            },
            fn=save_query,
        )
    ]
