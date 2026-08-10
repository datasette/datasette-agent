import html
import json
import time
from collections.abc import Mapping
from urllib.parse import urlencode

from datasette.resources import DatabaseResource, TableResource
from datasette.utils import escape_sqlite, named_parameters, sqlite3
from datasette.utils.asgi import Forbidden
from datasette.write_sql import (
    IgnoreWriteSqlOperation,
    QueryWriteRejected,
    RequireWriteSqlPermissions,
    decision_for_write_sql_operation,
    operation_is_write,
)

from .tools import AgentTool

_DISPLAY_MODES = ("model", "both", "user")
DROP_TABLE_TARGET_TYPES = {"table", "virtual-table"}

_EXECUTE_WRITE_SQL_DESCRIPTION = """
Execute ordered write SQL statements against a database. The tool analyzes each
statement and asks the user for approval before running anything.

The approval shows statements, parameters, permissions, and destructive-operation
warnings. Statements run in order; if one fails, later statements are skipped and
earlier successes are not rolled back. Use sql_query for read-only SQL.
""".strip()


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


def _json_error(message, **extra):
    return json.dumps({"ok": False, "error": message, **extra})


def _derived_write_parameters(sql):
    parameters = []
    seen = set()
    for parameter in named_parameters(sql):
        if parameter.startswith("_"):
            raise ValueError("Magic parameters are not allowed")
        if parameter not in seen:
            parameters.append(parameter)
            seen.add(parameter)
    return parameters


def _normalize_statements(statements):
    if not isinstance(statements, list) or not statements:
        raise ValueError("statements must be a non-empty array")
    normalized = []
    for index, statement in enumerate(statements, 1):
        if isinstance(statement, str):
            sql = statement
            params = {}
        elif isinstance(statement, Mapping):
            sql = statement.get("sql")
            params = statement.get("params") or {}
        else:
            raise ValueError("statement {} must be an object".format(index))
        if not sql or not isinstance(sql, str):
            raise ValueError("statement {} must include SQL".format(index))
        if not isinstance(params, Mapping):
            raise ValueError("statement {} params must be an object".format(index))
        normalized.append(
            {
                "index": index,
                "sql": sql,
                "provided_params": dict(params),
            }
        )
    return normalized


def _display_operations(analysis):
    operations = []
    for operation in analysis.operations:
        decision = decision_for_write_sql_operation(operation)
        if isinstance(decision, IgnoreWriteSqlOperation):
            continue
        operations.append(operation)
    return operations


def _analysis_is_write(analysis):
    return any(operation_is_write(operation) for operation in analysis.operations)


async def _analysis_rows_with_permissions(datasette, analysis, actor):
    rows = []
    is_write = _analysis_is_write(analysis)
    for operation in _display_operations(analysis):
        decision = decision_for_write_sql_operation(operation)
        required_permissions = []
        allowed = None
        if isinstance(decision, RequireWriteSqlPermissions):
            allowed = True
            for permission in decision.permissions:
                required_permissions.append(permission.action)
                if not await datasette.allowed(
                    action=permission.action,
                    resource=permission.resource,
                    actor=actor,
                ):
                    allowed = False
        elif is_write:
            allowed = False
        rows.append(
            {
                "operation": operation.operation,
                "target_type": operation.target_type,
                "database": operation.database,
                "table": operation.table or operation.target,
                "required_permissions": required_permissions,
                "allowed": allowed,
                "source": operation.source,
            }
        )
    return rows


def _is_drop_table_operation(operation):
    return (
        operation.operation == "drop"
        and operation.target_type in DROP_TABLE_TARGET_TYPES
    )


async def _drop_table_impacts(datasette, db, actor, analysis):
    impacts = []
    for operation in _display_operations(analysis):
        if not _is_drop_table_operation(operation):
            continue
        table = operation.table or operation.target
        if not table:
            continue
        impact = {
            "database": operation.database or db.name,
            "table": table,
            "row_count": None,
            "row_count_error": None,
        }
        can_view_table = await datasette.allowed(
            action="view-table",
            resource=TableResource(database=db.name, table=table),
            actor=actor,
        )
        if can_view_table:
            try:
                row = (
                    await db.execute(
                        "select count(*) as count from {}".format(escape_sqlite(table))
                    )
                ).first()
                impact["row_count"] = row["count"]
            except Exception as ex:
                impact["row_count_error"] = str(ex)
        else:
            impact["row_count_error"] = (
                "not shown because you do not have view-table permission"
            )
        impacts.append(impact)
    return impacts


async def _analyze_write_statement(datasette, db, actor, statement):
    sql = statement["sql"]
    provided_params = statement["provided_params"]
    try:
        parameter_names = _derived_write_parameters(sql)
    except ValueError as ex:
        raise ValueError("statement {}: {}".format(statement["index"], ex)) from ex
    extra_params = set(provided_params) - set(parameter_names)
    if extra_params:
        raise ValueError(
            "statement {} unknown parameters: {}".format(
                statement["index"], ", ".join(sorted(extra_params))
            )
        )
    params = {name: provided_params.get(name, "") for name in parameter_names}
    try:
        analysis = await db.analyze_sql(sql, params)
    except sqlite3.DatabaseError as ex:
        raise ValueError(
            "statement {} could not be analyzed: {}".format(statement["index"], ex)
        ) from ex
    if not _analysis_is_write(analysis):
        raise ValueError(
            "statement {} is read-only SQL; use sql_query instead".format(
                statement["index"]
            )
        )
    try:
        await datasette.ensure_query_write_permissions(
            db.name, sql, actor=actor, analysis=analysis
        )
    except QueryWriteRejected as ex:
        raise PermissionError("statement {}: {}".format(statement["index"], ex)) from ex
    except Forbidden as ex:
        raise PermissionError("statement {}: {}".format(statement["index"], ex)) from ex
    return {
        **statement,
        "params": params,
        "parameter_names": parameter_names,
        "analysis": analysis,
        "analysis_rows": await _analysis_rows_with_permissions(
            datasette, analysis, actor
        ),
        "drop_table_impacts": await _drop_table_impacts(datasette, db, actor, analysis),
    }


def _render_params(params):
    if not params:
        return ""
    rows = "".join(
        "<tr><th>{}</th><td>{}</td></tr>".format(
            html.escape(str(key)), html.escape(str(value))
        )
        for key, value in params.items()
    )
    return '<table class="agent-write-sql-params"><tbody>{}</tbody></table>'.format(
        rows
    )


def _render_analysis_rows(rows):
    if not rows:
        return ""

    def _permissions_html(permissions):
        if not permissions:
            return ""
        return "".join(
            '<code class="agent-write-sql-permission">{}</code>'.format(
                html.escape(permission)
            )
            for permission in permissions
        )

    body = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(row["operation"])),
            html.escape(str(row["database"] or "")),
            html.escape(str(row["table"] or "")),
            _permissions_html(row["required_permissions"]),
        )
        for row in rows
    )
    return (
        '<div class="agent-write-sql-table-scroll">'
        '<table class="agent-write-sql-analysis">'
        "<thead><tr><th>Operation</th><th>Database</th><th>Table</th>"
        "<th>Required permissions</th></tr></thead>"
        "<tbody>{}</tbody></table>"
        "</div>".format(body)
    )


def _approval_html(database, analyzed_statements):
    drop_impacts = [
        impact
        for statement in analyzed_statements
        for impact in statement["drop_table_impacts"]
    ]
    is_danger = bool(drop_impacts)
    panel_style = (
        "border: 3px solid #b00020; background: #fff1f1; color: #330006;"
        if is_danger
        else "border: 1px solid #c9d1d9; background: #f6f8fa; color: #24292f;"
    )
    heading = (
        "DANGER: this batch will drop table{}".format(
            "" if len(drop_impacts) == 1 else "s"
        )
        if is_danger
        else "Confirm write SQL batch"
    )
    drop_html = ""
    if drop_impacts:
        items = []
        for impact in drop_impacts:
            if impact["row_count"] is not None:
                count = "{} row{}".format(
                    impact["row_count"], "" if impact["row_count"] == 1 else "s"
                )
            else:
                count = impact["row_count_error"] or "row count unavailable"
            items.append(
                "<li><strong>{}</strong>.{} will be dropped: {}</li>".format(
                    html.escape(str(impact["database"])),
                    html.escape(str(impact["table"])),
                    html.escape(str(count)),
                )
            )
        drop_html = "<ul>{}</ul>".format("".join(items))

    statement_items = []
    for statement in analyzed_statements:
        params_html = _render_params(statement["params"])
        analysis_html = _render_analysis_rows(statement["analysis_rows"])
        statement_items.append(
            "<li><p><strong>Statement {}</strong></p><pre>{}</pre>{}{}</li>".format(
                statement["index"],
                html.escape(statement["sql"]),
                params_html,
                analysis_html,
            )
        )
    return (
        '<section class="agent-write-sql-confirmation{}" style="{} padding: 1rem; '
        'border-radius: 6px;">'
        "<h2>{}</h2>"
        "<p>Database: <strong>{}</strong></p>"
        "{}"
        "<p>Statements execute in order. If one statement fails, later statements "
        "will not be executed.</p>"
        "<ol>{}</ol>"
        "</section>"
    ).format(
        " agent-write-sql-danger" if is_danger else "",
        panel_style,
        html.escape(heading),
        html.escape(database),
        drop_html,
        "".join(statement_items),
    )


def _approval_text(database, analyzed_statements):
    lines = ["Database: {}".format(database)]
    drop_impacts = [
        impact
        for statement in analyzed_statements
        for impact in statement["drop_table_impacts"]
    ]
    if drop_impacts:
        lines.append("")
        lines.append(
            "DANGER: this batch will drop table{}".format(
                "" if len(drop_impacts) == 1 else "s"
            )
        )
        for impact in drop_impacts:
            if impact["row_count"] is not None:
                count = "{} row{}".format(
                    impact["row_count"], "" if impact["row_count"] == 1 else "s"
                )
            else:
                count = impact["row_count_error"] or "row count unavailable"
            lines.append(
                "- {}.{} will be dropped: {}".format(
                    impact["database"], impact["table"], count
                )
            )

    lines.append("")
    lines.append(
        "Statements execute in order. If one statement fails, later statements "
        "will not be executed."
    )
    for statement in analyzed_statements:
        lines.append("")
        lines.append("Statement {}:".format(statement["index"]))
        lines.append(statement["sql"])
        if statement["params"]:
            lines.append("Parameters:")
            for key, value in statement["params"].items():
                lines.append("- {}: {}".format(key, value))
        if statement["analysis_rows"]:
            lines.append("Operations:")
            for row in statement["analysis_rows"]:
                permissions = ", ".join(row["required_permissions"]) or "none"
                lines.append(
                    "- {} {}.{} (permissions: {})".format(
                        row["operation"],
                        row["database"] or database,
                        row["table"] or "",
                        permissions,
                    )
                )
    return "\n".join(lines)


async def _post_execute_write(datasette, actor, database, sql, params):
    payload = {"sql": sql, "params": params}
    path = datasette.urls.database(database) + "/-/execute-write"
    try:
        return await datasette.client.post(path, actor=actor, json=payload)
    except TypeError as ex:
        if "actor" not in str(ex):
            raise
        cookies = {}
        if actor:
            cookies["ds_actor"] = datasette.client.actor_cookie(actor)
        return await datasette.client.post(path, json=payload, cookies=cookies)


def _response_json(response):
    try:
        return response.json()
    except TypeError:
        return response.json


def _success_html(database, results):
    messages = "".join(
        "<li>{}</li>".format(html.escape(result.get("message") or "Query executed"))
        for result in results
    )
    return (
        "<p>Executed {} write SQL statement{} against <strong>{}</strong>.</p>"
        "<ol>{}</ol>"
    ).format(
        len(results),
        "" if len(results) == 1 else "s",
        html.escape(database),
        messages,
    )


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
    start = time.perf_counter()
    try:
        result = await db.execute(sql, truncate=True)
        query_ms = round((time.perf_counter() - start) * 1000, 2)
        rows = [dict(row) for row in result.rows]

        if display == "user":
            # Hide bulk rows from the model: surface a summary the model
            # can reason over (columns + row_count) and stash the actual
            # rows under _rows for export.
            payload = {
                "columns": result.columns,
                "row_count": len(rows),
                "truncated": result.truncated,
                "query_ms": query_ms,
                "_html": _render_rows_html(result.columns, rows, result.truncated),
                "_rows": rows,
                "_edit_sql_url": edit_sql_url,
            }
        elif display == "both":
            payload = {
                "columns": result.columns,
                "rows": rows,
                "truncated": result.truncated,
                "query_ms": query_ms,
                "_html": _render_rows_html(result.columns, rows, result.truncated),
                "_edit_sql_url": edit_sql_url,
            }
        else:  # "model" — unchanged default
            payload = {
                "columns": result.columns,
                "rows": rows,
                "truncated": result.truncated,
                "query_ms": query_ms,
                "_edit_sql_url": edit_sql_url,
            }

        return json.dumps(payload)
    except Exception as e:
        query_ms = round((time.perf_counter() - start) * 1000, 2)
        return json.dumps(
            {"error": str(e), "query_ms": query_ms, "_edit_sql_url": edit_sql_url}
        )


async def _execute_write_sql(datasette, actor, context, database: str, statements):
    if database not in datasette.databases:
        return _json_error(
            "Database '{}' not found".format(database),
            available_databases=list(datasette.databases),
        )
    db = datasette.get_database(database)
    if not db.is_mutable:
        return _json_error("Database '{}' is immutable".format(database))
    if not await datasette.allowed(
        action="execute-write-sql",
        resource=DatabaseResource(database=database),
        actor=actor,
    ):
        return _json_error(
            "Permission denied: need execute-write-sql on database '{}'".format(
                database
            )
        )
    try:
        normalized_statements = _normalize_statements(statements)
    except ValueError as ex:
        return _json_error(str(ex))

    analyzed_statements = []
    try:
        for statement in normalized_statements:
            analyzed_statements.append(
                await _analyze_write_statement(datasette, db, actor, statement)
            )
    except (ValueError, PermissionError) as ex:
        return _json_error(str(ex))

    approved = await context.ask_user(
        "Execute {} write SQL statement{} against database '{}'?".format(
            len(analyzed_statements),
            "" if len(analyzed_statements) == 1 else "s",
            database,
        ),
        html=_approval_html(database, analyzed_statements),
        text=_approval_text(database, analyzed_statements),
    )
    if not approved:
        return json.dumps(
            {
                "ok": False,
                "cancelled": True,
                "message": "The user declined to execute these SQL statements.",
            }
        )

    results = []
    for statement in analyzed_statements:
        response = await _post_execute_write(
            datasette,
            actor,
            database,
            statement["sql"],
            statement["params"],
        )
        data = _response_json(response)
        if response.status_code != 200 or not data.get("ok"):
            errors = data.get("errors") or data.get("error") or []
            if isinstance(errors, str):
                errors = [errors]
            message = "; ".join(errors) or "HTTP {}".format(response.status_code)
            return json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "failed_statement": statement["index"],
                    "executed_count": len(results),
                    "results": results,
                    "partial": bool(results),
                }
            )
        results.append(data)

    return json.dumps(
        {
            "ok": True,
            "database": database,
            "statements_executed": len(results),
            "results": results,
            "_html": _success_html(database, results),
        }
    )


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
        AgentTool(
            name="execute_write_sql",
            description=_EXECUTE_WRITE_SQL_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "The database name",
                    },
                    "statements": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "Ordered write SQL statements to execute after one user approval"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "sql": {
                                    "type": "string",
                                    "description": "A single writable SQL statement",
                                },
                                "params": {
                                    "type": "object",
                                    "description": (
                                        "Optional named parameter values for this statement"
                                    ),
                                    "additionalProperties": True,
                                },
                            },
                            "required": ["sql"],
                        },
                    },
                },
                "required": ["database", "statements"],
            },
            fn=_execute_write_sql,
        ),
    ]
