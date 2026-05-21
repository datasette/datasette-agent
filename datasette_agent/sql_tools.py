import html
import json
from collections.abc import Mapping
from urllib.parse import urlencode

from datasette.resources import DatabaseResource, TableResource

from .tools import AgentTool

_DISPLAY_MODES = ("model", "both", "user")


def _render_rows_html(columns, rows, truncated):
    """Render a SQL result rowset as a small HTML <table>.

    All column and cell values are HTML-escaped; cells are rendered with
    their str() value. The output is dropped into the chat under a
    .agent-rich-result wrapper by the existing _html side channel.
    """
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body_rows = []
    for row in rows:
        if isinstance(row, Mapping):
            cells = [row.get(c) for c in columns]
        else:
            cells = list(row)
        body_rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in cells) + "</tr>"
        )
    body = "".join(body_rows)
    foot = ""
    if truncated:
        foot = (
            f'<tfoot><tr><td colspan="{len(columns)}">'
            "Results truncated</td></tr></tfoot>"
        )
    table = (
        '<div class="agent-sql-result-scroll">'
        '<table class="agent-sql-result">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody>"
        f"{foot}"
        "</table>"
        "</div>"
    )
    return table


def _get_value(item, key):
    if isinstance(item, Mapping):
        return item[key]
    return getattr(item, key)


async def _list_databases_and_tables(datasette, actor):
    databases = []
    for db_name, db in datasette.databases.items():
        if not await datasette.allowed(
            action="execute-sql",
            resource=DatabaseResource(database=db_name),
            actor=actor,
        ):
            continue
        databases.append(
            {"database_name": db_name, "table_names": await db.table_names()}
        )
    return json.dumps({"databases": databases})


async def _describe_table(datasette, actor, database: str, table: str):
    if not await datasette.allowed(
        action="view-table",
        resource=TableResource(database=database, table=table),
        actor=actor,
    ):
        return json.dumps({"error": "Permission denied"})
    if database not in datasette.databases:
        available = list(datasette.databases)
        return json.dumps(
            {
                "error": f"Database '{database}' not found",
                "available_databases": available,
            }
        )
    db = datasette.get_database(database)
    table_names = await db.table_names()
    if table not in table_names:
        return json.dumps(
            {
                "error": f"Table '{table}' not found in database '{database}'",
                "available_tables": table_names,
            }
        )
    columns = await db.table_column_details(table)
    foreign_keys = await db.foreign_keys_for_table(table)
    return json.dumps(
        {
            "database": database,
            "table": table,
            "columns": [
                {"name": col.name, "type": col.type, "notnull": col.notnull}
                for col in columns
            ],
            "foreign_keys": [
                {
                    "column": _get_value(fk, "column"),
                    "other_table": _get_value(fk, "other_table"),
                    "other_column": _get_value(fk, "other_column"),
                }
                for fk in foreign_keys
            ],
        }
    )


async def _sql_query(datasette, actor, database: str, sql: str, display: str = "model"):
    if not await datasette.allowed(
        action="execute-sql",
        resource=DatabaseResource(database=database),
        actor=actor,
    ):
        return json.dumps({"error": "Permission denied"})
    if database not in datasette.databases:
        available = list(datasette.databases)
        return json.dumps(
            {
                "error": f"Database '{database}' not found",
                "available_databases": available,
            }
        )
    if display not in _DISPLAY_MODES:
        display = "model"
    db = datasette.get_database(database)
    query_path = datasette.urls.database(database) + "/-/query"
    edit_sql_url = f"{query_path}?{urlencode({'sql': sql})}"
    try:
        result = await db.execute(sql, truncate=True)
        rows = [dict(row) for row in result.rows]

        if display == "user":
            # Hide bulk rows from the model: surface a summary the model
            # can reason over (columns + row_count) and stash the actual
            # rows under _rows for export.
            payload = {
                "columns": result.columns,
                "row_count": len(rows),
                "truncated": result.truncated,
                "_html": _render_rows_html(result.columns, rows, result.truncated),
                "_rows": rows,
                "_edit_sql_url": edit_sql_url,
            }
        elif display == "both":
            payload = {
                "columns": result.columns,
                "rows": rows,
                "truncated": result.truncated,
                "_html": _render_rows_html(result.columns, rows, result.truncated),
                "_edit_sql_url": edit_sql_url,
            }
        else:  # "model" — unchanged default
            payload = {
                "columns": result.columns,
                "rows": rows,
                "truncated": result.truncated,
                "_edit_sql_url": edit_sql_url,
            }

        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": str(e), "_edit_sql_url": edit_sql_url})


def get_default_tools():
    return [
        AgentTool(
            name="list_databases_and_tables",
            description="List all available databases and their tables",
            input_schema={"type": "object", "properties": {}},
            fn=_list_databases_and_tables,
        ),
        AgentTool(
            name="describe_table",
            description="Get column names, types, and foreign keys for a table",
            input_schema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "The database name",
                    },
                    "table": {
                        "type": "string",
                        "description": "The table name",
                    },
                },
                "required": ["database", "table"],
            },
            fn=_describe_table,
        ),
        AgentTool(
            name="sql_query",
            description="Execute a read-only SQL query against a database. Results are limited to the first 1,000 rows.",
            input_schema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "The database name",
                    },
                    "sql": {
                        "type": "string",
                        "description": "The SQL query to execute",
                    },
                    "display": {
                        "type": "string",
                        "enum": list(_DISPLAY_MODES),
                        "default": "model",
                        "description": (
                            "Where results go. 'model' (default) returns "
                            "rows to you alone — pick this when you need "
                            "to reason over the data and won't show it to "
                            "the user verbatim. 'both' returns rows to you "
                            "AND renders an HTML table the user sees — pick "
                            "this when you'll comment on the data you're "
                            "answering with. 'user' renders the table for "
                            "the user but only returns columns + row_count "
                            "to you — pick this for 'show me…' style "
                            "queries where you don't need to read the rows."
                        ),
                    },
                },
                "required": ["database", "sql"],
            },
            fn=_sql_query,
        ),
    ]
