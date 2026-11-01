"""#-references: tag a database, table, stored query - or anything a
plugin registers - in a chat message.

Reference types come from the ``register_agent_reference_types`` plugin
hook, which datasette-agent implements itself for tables, queries and
databases. Each type supplies a search function, used by the chat
composer's autocomplete as the user types after a ``#``, and
optionally a function returning the URL for a reference's pill.

Picked references travel with the message to the stream endpoint. They
are pointers, not content: the server checks their shape and that their
type is registered, then adds a note listing them (plus each type's
instructions) to the user turn. Nothing about the referenced item is
looked up at that point - the model uses its tools to read the item,
and those tools enforce permissions. The note is persisted as its own
text part, marked with REFERENCES_KEY, so the UI can render the
references as pills instead of showing the note.

URLs are never taken from the client or persisted: they are computed
by the reference's type whenever references are rendered.
"""

import dataclasses
from dataclasses import dataclass
from typing import Callable

from datasette.plugins import pm
from datasette.utils import await_me_maybe, tilde_decode, tilde_encode

REFERENCES_KEY = "agent_references"
MAX_REFERENCES = 10
MAX_RESULTS = 20
MAX_ID_CHARS = 1000
MAX_NAME_CHARS = 500
MAX_DESCRIPTION_CHARS = 500


@dataclass
class Reference:
    """Something the user can tag with #name in a chat message."""

    type: str
    # Opaque to datasette-agent; meaningful to the plugin that
    # registered the type (and to the tools the model uses on it).
    id: str
    # What goes after the "#" and on the pill.
    name: str
    # Shown in the autocomplete and included in the note for the model.
    description: str = ""


@dataclass
class ReferenceType:
    type: str
    # Short label shown on pills and in the autocomplete, e.g. "table".
    label: str
    # async (datasette, actor, q, limit) -> list[Reference]. Must only
    # return items the actor is allowed to see.
    search: Callable
    # (datasette, Reference) -> str | None
    url: Callable | None = None
    # Added once to the model's note when this type is referenced -
    # typically which tool to call with the reference's id.
    instructions: str = ""
    # Tie-break between equally good matches of different types.
    order: int = 100


async def get_reference_types(datasette):
    """Registered reference types, keyed by type."""
    types = {}
    for result in pm.hook.register_agent_reference_types(datasette=datasette):
        result = await await_me_maybe(result)
        for reference_type in result or []:
            types.setdefault(reference_type.type, reference_type)
    return types


def _match_rank(name, q):
    """0 exact, 1 prefix, 2 substring, 3 matched elsewhere."""
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


def _reference_url(datasette, reference_type, ref):
    if reference_type is None or reference_type.url is None:
        return None
    try:
        return reference_type.url(datasette, ref)
    except Exception:
        return None


def _to_json(datasette, types, ref):
    reference_type = types.get(ref.type)
    data = dataclasses.asdict(ref)
    data["label"] = reference_type.label if reference_type else ref.type
    data["url"] = _reference_url(datasette, reference_type, ref)
    return data


async def search_references(datasette, actor, q="", limit=MAX_RESULTS):
    """Search every registered reference type for items matching q
    that actor can see. Returns JSON-ready dicts."""
    q = (q or "").strip()
    types = await get_reference_types(datasette)
    results = []
    for reference_type in types.values():
        try:
            found = await reference_type.search(datasette, actor, q, limit)
        except Exception:
            # One broken plugin shouldn't break autocomplete.
            continue
        results.extend(ref for ref in found or [] if isinstance(ref, Reference))
    results.sort(
        key=lambda r: (
            _match_rank(r.name, q),
            types[r.type].order if r.type in types else 1000,
            len(r.name),
            r.name.lower(),
        )
    )
    return [_to_json(datasette, types, ref) for ref in results[:limit]]


def _legacy_id(ref):
    """References persisted before reference types were pluggable
    were {type, database, name}; derive their id."""
    database = ref.get("database")
    if not isinstance(database, str):
        return None
    if ref.get("type") == "database":
        return database
    if ref.get("type") in ("table", "query") and isinstance(ref.get("name"), str):
        return builtin_id(database, ref["name"])
    return None


def coerce_reference(ref):
    """A Reference from a client-supplied or persisted dict, or None
    if it's malformed."""
    if not isinstance(ref, dict):
        return None
    ref_type = ref.get("type")
    ref_id = ref.get("id")
    if ref_id is None:
        ref_id = _legacy_id(ref)
    name = ref.get("name")
    description = ref.get("description")
    if not isinstance(description, str):
        description = ""
    if not all(isinstance(v, str) and v for v in (ref_type, ref_id, name)):
        return None
    if len(ref_id) > MAX_ID_CHARS or len(name) > MAX_NAME_CHARS:
        return None
    return Reference(
        type=ref_type,
        id=ref_id,
        name=name,
        description=description[:MAX_DESCRIPTION_CHARS],
    )


def normalize_references(references, types):
    """Validate the shape of client-supplied references, dropping
    malformed entries, unregistered types and duplicates."""
    if not isinstance(references, list):
        return []
    normalized = []
    seen = set()
    for item in references:
        ref = coerce_reference(item)
        if ref is None or ref.type not in types:
            continue
        key = (ref.type, ref.id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(ref)
        if len(normalized) >= MAX_REFERENCES:
            break
    return normalized


def references_note(references, types):
    """The note the model sees alongside the user's text."""
    lines = [
        "[Note: the user tagged the following items in their message "
        "using #name. Use your tools to look up anything you need "
        "about them.]",
        "",
    ]
    for ref in references:
        line = "- #{} ({} id: {})".format(ref.name, ref.type, ref.id)
        if ref.description:
            line += ": " + ref.description
        lines.append(line)
    # One copy of each instruction line, even when types share some.
    instructions = {}
    for ref_type in dict.fromkeys(ref.type for ref in references):
        reference_type = types.get(ref_type)
        if reference_type and reference_type.instructions:
            for line in reference_type.instructions.splitlines():
                if line.strip():
                    instructions[line] = None
    if instructions:
        lines.append("")
        lines.extend(instructions)
    return "\n".join(lines)


async def prepare_references(datasette, references):
    """Turn client-supplied references into (references, note) where
    references are JSON-ready dicts to persist. note is None when no
    valid references were supplied."""
    types = await get_reference_types(datasette)
    refs = normalize_references(references, types)
    if not refs:
        return [], None
    return [dataclasses.asdict(ref) for ref in refs], references_note(refs, types)


async def add_reference_urls(datasette, messages):
    """Add url and label to each reference on flattened render
    messages, upgrading legacy persisted references as it goes."""
    types = await get_reference_types(datasette)
    for message in messages:
        if not message.get("references"):
            continue
        rendered = []
        for item in message["references"]:
            ref = coerce_reference(item)
            if ref is not None:
                rendered.append(_to_json(datasette, types, ref))
        message["references"] = rendered
    return messages


# Built-in reference types: tables (and views), stored queries and
# databases. Table and query ids are "database/name", each part
# tilde-encoded so a "/" in either name stays unambiguous.


def builtin_id(database, name):
    return "{}/{}".format(tilde_encode(database), tilde_encode(name))


def split_builtin_id(ref_id):
    database, sep, name = ref_id.partition("/")
    if not sep:
        return None, None
    return tilde_decode(database), tilde_decode(name)


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _search_databases(datasette, actor, q, limit):
    page = await datasette.allowed_resources("view-database", actor, limit=1000)
    results = []
    for resource in page.resources:
        name = resource.parent
        if name not in datasette.databases:
            continue
        if q and q.lower() not in name.lower():
            continue
        results.append(
            Reference(type="database", id=name, name=name, description="Database")
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
            Reference(
                type="table",
                id=builtin_id(database, table),
                name=table,
                description="{} in {}".format(
                    "View" if table in views[database] else "Table", database
                ),
            )
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
        Reference(
            type="query",
            id=builtin_id(query.database, query.name),
            name=query.name,
            description="Query in {}{}".format(
                query.database, ": {}".format(query.title) if query.title else ""
            ),
        )
        for query in page.queries
        if query.database in datasette.databases
        and (needle in query.name.lower() or needle in (query.title or "").lower())
    ]


def _database_url(datasette, ref):
    if ref.id not in datasette.databases:
        return None
    return datasette.urls.database(ref.id)


def _table_url(datasette, ref):
    database, table = split_builtin_id(ref.id)
    if database not in datasette.databases:
        return None
    return datasette.urls.table(database, table)


def _query_url(datasette, ref):
    database, query = split_builtin_id(ref.id)
    if database not in datasette.databases:
        return None
    return datasette.urls.query(database, query)


_TILDE_NOTE = (
    'Table and query ids are "database/name", with any "/" in either '
    "part written as ~2F."
)


def builtin_reference_types():
    return [
        ReferenceType(
            type="table",
            label="table",
            search=_search_tables,
            url=_table_url,
            instructions=(
                "For a tagged table or view, call describe_table with the "
                "database and table from its id.\n" + _TILDE_NOTE
            ),
            order=0,
        ),
        ReferenceType(
            type="query",
            label="query",
            search=_search_queries,
            url=_query_url,
            instructions=(
                "For a tagged stored query, call describe_query with the "
                "database and query name from its id.\n" + _TILDE_NOTE
            ),
            order=1,
        ),
        ReferenceType(
            type="database",
            label="db",
            search=_search_databases,
            url=_database_url,
            instructions=(
                "A tagged database's id is the database name; call "
                "list_databases_and_tables to see its tables."
            ),
            order=2,
        ),
    ]
