import json
from collections.abc import Mapping

from datasette.resources import DatabaseResource, TableResource

from .tools import AgentTool


def _get_value(item, key):
    if isinstance(item, Mapping):
        return item[key]
    return getattr(item, key)


async def _list_databases_and_tables(datasette, actor):
    result = {}
    for db_name, db in datasette.databases.items():
        if db_name.startswith("_"):
            continue
        if not await datasette.allowed(
            action="view-database",
            resource=DatabaseResource(database=db_name),
            actor=actor,
        ):
            continue
        tables = await db.table_names()
        result[db_name] = tables
    return json.dumps(result)


async def _describe_table(datasette, actor, database: str, table: str):
    if not await datasette.allowed(
        action="view-table",
        resource=TableResource(database=database, table=table),
        actor=actor,
    ):
        return json.dumps({"error": "Permission denied"})
    db = datasette.get_database(database)
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


async def _sql_query(datasette, actor, database: str, sql: str):
    if not await datasette.allowed(
        action="execute-sql",
        resource=DatabaseResource(database=database),
        actor=actor,
    ):
        return json.dumps({"error": "Permission denied"})
    db = datasette.get_database(database)
    try:
        result = await db.execute(sql, truncate=True)
        rows = [dict(row) for row in result.rows]
        output = json.dumps(
            {
                "columns": result.columns,
                "rows": rows,
                "truncated": result.truncated,
            }
        )
        # Truncate large outputs to avoid overwhelming the model
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"
        return output
    except Exception as e:
        return json.dumps({"error": str(e)})


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
                },
                "required": ["database", "sql"],
            },
            fn=_sql_query,
        ),
    ]
