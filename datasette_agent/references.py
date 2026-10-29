"""#-references: tag a database, table or stored (canned) query in a
chat message.

The chat composer searches resources through search_references() as
the user types after a ``#``. Picked references travel with the message
to the stream endpoint, where resolve_references() re-checks
permissions and turns them into a schema note the model sees alongside
the user's text. That note is persisted as its own text part, marked
with REFERENCES_KEY so the UI can render the references as pills
instead of showing the schema dump.
"""

from datasette.resources import DatabaseResource, QueryResource, TableResource

REFERENCES_KEY = "agent_references"
REFERENCE_TYPES = ("table", "query", "database")
MAX_REFERENCES = 10
MAX_RESULTS = 20
# Per-reference cap on the schema text sent to the model; a database
# with hundreds of tables would otherwise swamp the context window.
MAX_REFERENCE_CHARS = 20000

_TYPE_ORDER = {t: i for i, t in enumerate(REFERENCE_TYPES)}


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match_rank(name, q):
    """0 exact, 1 prefix, 2 substring, 3 matched elsewhere (title, SQL)."""
    if not q:
        return 0
    name = name.lower()
    q = q.lower()
    if name == q:
        return 0
    if name.startswith(q):
        return 1
    if q in name:
        return 2
    return 3


async def _search_databases(datasette, actor, q):
    page = await datasette.allowed_resources("view-database", actor, limit=1000)
    results = []
    for resource in page.resources:
        name = resource.parent
        if name not in datasette.databases:
            continue
        if q and q.lower() not in name.lower():
            continue
        results.append(
            {
                "type": "database",
                "database": name,
                "name": name,
                "description": "Database",
            }
        )
    return results


async def _search_tables(datasette, actor, q, limit):
    sql, params = await datasette.allowed_resources_sql(
        action="view-table", actor=actor
    )
    params = dict(params)
    params["agent_ref_q"] = "%{}%".format(_like_escape(q))
    params["agent_ref_prefix"] = "{}%".format(_like_escape(q))
    # Over-fetch so hidden tables (FTS shadow tables and the like) can
    # be dropped without coming up short.
    params["agent_ref_limit"] = limit * 3
    rows = (
        await datasette.get_internal_database().execute(
            "SELECT parent, child FROM ({}) "
            "WHERE child LIKE :agent_ref_q ESCAPE '\\' "
            "ORDER BY child LIKE :agent_ref_prefix ESCAPE '\\' DESC, "
            "length(child), child "
            "LIMIT :agent_ref_limit".format(sql),
            params,
        )
    ).rows
    hidden = {}
    views = {}
    results = []
    for row in rows:
        database, table = row["parent"], row["child"]
        if database not in datasette.databases:
            continue
        if database not in hidden:
            db = datasette.get_database(database)
            hidden[database] = set(await db.hidden_table_names())
            views[database] = set(await db.view_names())
        if table in hidden[database]:
            continue
        results.append(
            {
                "type": "table",
                "database": database,
                "name": table,
                "description": "{} in {}".format(
                    "View" if table in views[database] else "Table", database
                ),
            }
        )
        if len(results) >= limit:
            break
    return results


async def _search_queries(datasette, actor, q, limit):
    page = await datasette.list_queries(actor=actor, q=q or None, limit=limit)
    # list_queries also matches on description and SQL, and doesn't
    # escape LIKE wildcards - keep only name and title matches.
    needle = q.lower()
    return [
        {
            "type": "query",
            "database": query.database,
            "name": query.name,
            "description": "Query in {}{}".format(
                query.database, ": {}".format(query.title) if query.title else ""
            ),
        }
        for query in page.queries
        if query.database in datasette.databases
        and (needle in query.name.lower() or needle in (query.title or "").lower())
    ]


async def search_references(datasette, actor, q="", limit=MAX_RESULTS):
    """Databases, tables and stored queries matching q that actor can see."""
    q = (q or "").strip()
    results = (
        await _search_tables(datasette, actor, q, limit)
        + await _search_queries(datasette, actor, q, limit)
        + await _search_databases(datasette, actor, q)
    )
    results.sort(
        key=lambda r: (
            _match_rank(r["name"], q),
            _TYPE_ORDER[r["type"]],
            len(r["name"]),
            r["name"].lower(),
        )
    )
    return results[:limit]


def _truncate(text):
    if len(text) <= MAX_REFERENCE_CHARS:
        return text
    return text[:MAX_REFERENCE_CHARS] + "\n... (truncated)"


async def _object_sql(db, name):
    row = (
        await db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            [name],
        )
    ).first()
    return row["sql"] if row and row["sql"] else None


async def _describe_table_reference(datasette, actor, database, name):
    if not await datasette.allowed(
        action="view-table",
        resource=TableResource(database=database, table=name),
        actor=actor,
    ):
        return None
    db = datasette.get_database(database)
    is_view = name in await db.view_names()
    if not is_view and name not in await db.table_names():
        return None
    schema = await _object_sql(db, name)
    return 'The {} "{}" in database "{}":\n{}'.format(
        "view" if is_view else "table", name, database, schema or "(no schema)"
    )


async def _describe_database_reference(datasette, actor, database):
    if not await datasette.allowed(
        action="view-database",
        resource=DatabaseResource(database=database),
        actor=actor,
    ):
        return None
    db = datasette.get_database(database)
    hidden = set(await db.hidden_table_names())
    schemas = []
    for name in list(await db.table_names()) + list(await db.view_names()):
        if name in hidden:
            continue
        if not await datasette.allowed(
            action="view-table",
            resource=TableResource(database=database, table=name),
            actor=actor,
        ):
            continue
        schema = await _object_sql(db, name)
        if schema:
            schemas.append(schema + ";")
    lines = ['The database "{}" contains:'.format(database)]
    lines.append("\n".join(schemas) if schemas else "(no tables)")
    queries = await datasette.list_queries(database, actor=actor, limit=50)
    if queries.queries:
        lines.append(
            "Stored queries in this database: {}".format(
                ", ".join(q.name for q in queries.queries)
            )
        )
    return "\n".join(lines)


async def _describe_query_reference(datasette, actor, database, name):
    if not await datasette.allowed(
        action="view-query",
        resource=QueryResource(database=database, query=name),
        actor=actor,
    ):
        return None
    query = await datasette.get_query(database, name)
    if query is None:
        return None
    lines = [
        'The stored {} query "{}" in database "{}":'.format(
            "write" if query.is_write else "read-only", name, database
        )
    ]
    if query.title:
        lines.append("Title: {}".format(query.title))
    if query.description:
        lines.append("Description: {}".format(query.description))
    if query.parameters:
        lines.append(
            "Parameters: {}".format(", ".join(":" + p for p in query.parameters))
        )
    if query.hide_sql:
        lines.append("(The SQL for this query is hidden.)")
    else:
        lines.append("SQL:\n{}".format(query.sql))
    return "\n".join(lines)


def normalize_references(references):
    """Validate the shape of client-supplied references, dropping
    malformed entries and duplicates. Permissions are checked later."""
    if not isinstance(references, list):
        return []
    normalized = []
    seen = set()
    for ref in references:
        if not isinstance(ref, dict):
            continue
        ref_type = ref.get("type")
        database = ref.get("database")
        name = ref.get("name")
        if ref_type not in REFERENCE_TYPES:
            continue
        if not isinstance(database, str) or not isinstance(name, str):
            continue
        if not database or not name:
            continue
        key = (ref_type, database, name)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"type": ref_type, "database": database, "name": name})
        if len(normalized) >= MAX_REFERENCES:
            break
    return normalized


async def resolve_references(datasette, actor, references):
    """Turn client-supplied references into (references, context_text).

    References the actor cannot see, or that no longer exist, are
    dropped. context_text is None when nothing resolved.
    """
    resolved = []
    sections = []
    for ref in normalize_references(references):
        if ref["database"] not in datasette.databases:
            continue
        if ref["type"] == "table":
            text = await _describe_table_reference(
                datasette, actor, ref["database"], ref["name"]
            )
        elif ref["type"] == "query":
            text = await _describe_query_reference(
                datasette, actor, ref["database"], ref["name"]
            )
        else:
            if ref["name"] != ref["database"]:
                continue
            text = await _describe_database_reference(datasette, actor, ref["database"])
        if text is None:
            continue
        resolved.append(ref)
        sections.append(_truncate(text))
    if not resolved:
        return [], None
    context = (
        "[Note: the user tagged the following resources in their message "
        "using #name. Their schemas are included automatically below, so "
        "you do not need to look them up with list_databases_and_tables "
        "or describe_table.]\n\n" + "\n\n".join(sections)
    )
    return resolved, context
