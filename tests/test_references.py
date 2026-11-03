import json
import sqlite3

from datasette.app import Datasette
import pytest

from datasette import hookimpl
from datasette.plugins import pm

from datasette_agent.messages import flatten_for_render, message_dict_text
from datasette_agent.references import (
    REFERENCES_KEY,
    Reference,
    ReferenceType,
    add_reference_urls,
    builtin_id,
    builtin_reference_types,
    normalize_references,
    prepare_references,
    split_builtin_id,
)


@pytest.fixture
def datasette_instance(tmp_path):
    db_path = str(tmp_path / "demo.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, qty INT)")
    conn.execute("CREATE TABLE secrets (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("CREATE VIEW big_items AS SELECT * FROM items WHERE qty > 10")
    conn.close()
    return Datasette(
        [db_path],
        internal=str(tmp_path / "internal.db"),
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {"datasette-agent": {"id": "user"}},
            "databases": {
                "demo": {
                    "tables": {"secrets": {"allow": {"id": "admin"}}},
                    "queries": {
                        "top_items": {
                            "sql": "select * from items where qty > :min",
                            "title": "Top items",
                        },
                        "hidden_sql": {
                            "sql": "select 'shh'",
                            "hide_sql": True,
                        },
                    },
                }
            },
        },
    )


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


@pytest.mark.asyncio
async def test_references_api_requires_permission(datasette_instance):
    response = await datasette_instance.client.get("/-/agent/api/references?q=item")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_references_api_search(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=item", cookies=cookies
    )
    assert response.status_code == 200
    results = response.json()["results"]
    found = [(r["type"], r["id"], r["name"]) for r in results]
    # Exact match first, then prefix/substring matches
    assert found[0] == ("table", "demo/items", "items")
    assert ("table", "demo/big_items", "big_items") in found
    assert ("query", "demo/top_items", "top_items") in found
    assert results[0]["description"] == "Table in demo"


@pytest.mark.asyncio
async def test_references_api_empty_query_lists_everything(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/api/references", cookies=cookies
    )
    types = {r["type"] for r in response.json()["results"]}
    assert types == {"table", "query", "database"}


@pytest.mark.asyncio
async def test_references_api_hides_forbidden_tables(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=secret", cookies=cookies
    )
    assert response.json()["results"] == []


@pytest.mark.asyncio
async def test_references_api_escapes_like_wildcards(datasette_instance, cookies):
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=%25", cookies=cookies
    )
    assert response.json()["results"] == []


@pytest.mark.parametrize(
    "ref,expected",
    (
        ({"type": "table", "id": "demo/items", "name": "items"}, "/prefix/demo/items"),
        (
            {"type": "table", "id": "demo/has~2Fslash", "name": "has/slash"},
            "/prefix/demo/has~2Fslash",
        ),
        (
            {"type": "query", "id": "demo/top_items", "name": "top_items"},
            "/prefix/demo/top_items",
        ),
        ({"type": "database", "id": "demo", "name": "demo"}, "/prefix/demo"),
        ({"type": "table", "id": "gone/items", "name": "items"}, None),
        # Legacy persisted shape
        ({"type": "table", "database": "demo", "name": "items"}, "/prefix/demo/items"),
        ({"type": "database", "database": "demo", "name": "demo"}, "/prefix/demo"),
    ),
)
@pytest.mark.asyncio
async def test_reference_urls_use_datasette_urls(datasette_instance, ref, expected):
    datasette_instance._settings["base_url"] = "/prefix/"
    messages = await add_reference_urls(
        datasette_instance, [{"role": "user", "references": [ref]}]
    )
    assert messages[0]["references"][0]["url"] == expected


def test_builtin_id_round_trip():
    ref_id = builtin_id("my/db", "a/b~c")
    assert "/" in ref_id
    assert split_builtin_id(ref_id) == ("my/db", "a/b~c")
    assert builtin_id("demo", "items") == "demo/items"


@pytest.mark.asyncio
async def test_references_api_results_include_url_and_label(
    datasette_instance, cookies
):
    response = await datasette_instance.client.get(
        "/-/agent/api/references", cookies=cookies
    )
    results = {(r["type"], r["name"]): r for r in response.json()["results"]}
    assert results[("table", "items")]["url"] == "/demo/items"
    assert results[("table", "items")]["label"] == "table"
    assert results[("table", "items")]["id"] == "demo/items"
    assert results[("query", "top_items")]["url"] == "/demo/top_items"
    assert results[("database", "demo")]["url"] == "/demo"
    assert results[("database", "demo")]["label"] == "db"


def test_normalize_references():
    types = {t.type: t for t in builtin_reference_types()}
    assert normalize_references("nope", types) == []
    assert normalize_references(
        [
            {"type": "table", "id": "demo/items", "name": "items"},
            {"type": "table", "id": "demo/items", "name": "items"},
            # Legacy shape is upgraded; same id so it's a duplicate
            {"type": "table", "database": "demo", "name": "items"},
            {"type": "bogus", "id": "x", "name": "items"},
            {"type": "table", "id": "", "name": "items"},
            {"type": "table", "id": "demo/x", "name": 5},
            {"type": "table", "id": "demo/x", "name": "x" * 1000},
            {
                "type": "query",
                "id": "demo/top_items",
                "name": "top_items",
                "description": "d" * 1000,
                "url": "javascript:alert(1)",
            },
            "string",
        ],
        types,
    ) == [
        Reference(type="table", id="demo/items", name="items"),
        Reference(
            type="query", id="demo/top_items", name="top_items", description="d" * 500
        ),
    ]


@pytest.mark.asyncio
async def test_prepare_references(datasette_instance):
    await datasette_instance.invoke_startup()
    references, note = await prepare_references(
        datasette_instance,
        {"id": "user"},
        [
            {
                "type": "table",
                "id": "demo/items",
                "name": "items",
                "description": "Table in demo",
            },
            {"type": "query", "id": "demo/top_items", "name": "top_items"},
            {"type": "query", "id": "demo/hidden_sql", "name": "hidden_sql"},
            {"type": "table", "id": "demo/big_items", "name": "big_items"},
            {"type": "database", "id": "demo", "name": "demo"},
            # Kept as pointers, but with no context: forbidden, missing
            {"type": "table", "id": "demo/secrets", "name": "secrets"},
            {"type": "table", "id": "demo/missing", "name": "missing"},
            {"type": "table", "id": "nope/items", "name": "nope_items"},
            {"type": "bogus", "id": "x", "name": "x"},
        ],
    )
    assert references[:3] == [
        {
            "type": "table",
            "id": "demo/items",
            "name": "items",
            "description": "Table in demo",
        },
        {
            "type": "query",
            "id": "demo/top_items",
            "name": "top_items",
            "description": "",
        },
        {
            "type": "query",
            "id": "demo/hidden_sql",
            "name": "hidden_sql",
            "description": "",
        },
    ]
    assert [r["name"] for r in references][3:] == [
        "big_items",
        "demo",
        "secrets",
        "missing",
        "nope_items",
    ]
    assert "tagged the following items" in note
    # Table schema is indented under its reference line
    assert (
        "- #items (table id: demo/items): Table in demo\n"
        "  CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, qty INT)"
    ) in note
    assert "  CREATE VIEW big_items AS SELECT * FROM items WHERE qty > 10" in note
    # Query details
    assert (
        "- #top_items (query id: demo/top_items)\n"
        "  Read-only query\n"
        "  Title: Top items\n"
        "  Parameters: :min\n"
        "  SQL:\n"
        "  select * from items where qty > :min"
    ) in note
    assert "select 'shh'" not in note
    assert "(The SQL for this query is hidden.)" in note
    # Database: names of visible tables and views, not their schemas
    assert (
        "- #demo (database id: demo)\n" "  Tables: items\n" "  Views: big_items"
    ) in note
    # Forbidden or missing tables get a bare pointer, no schema
    assert "- #secrets (table id: demo/secrets)\n\n" in note
    assert "CREATE TABLE secrets" not in note
    assert "- #missing (table id: demo/missing)\n\n" in note
    assert "describe_table" in note
    assert "describe_query" in note
    # Shared instruction lines appear once
    assert note.count("~2F") == 1


@pytest.mark.asyncio
async def test_prepare_references_nothing_valid(datasette_instance):
    assert await prepare_references(
        datasette_instance, {"id": "user"}, [{"type": "bogus", "id": "x", "name": "x"}]
    ) == ([], None)


class NotesPlugin:
    """Registers a "note" reference type, as a plugin like
    datasette-apps would."""

    NOTES = {"n1": "Shopping list", "n2": "Secret plans"}

    # Plain (non-async) functions work too.
    def context(self, datasette, actor, ref):
        if ref.id == "boom":
            raise ValueError("broken context")
        if ref.id == "huge":
            return "x" * 30000
        if ref.id == "n2" and (actor or {}).get("id") != "admin":
            return None
        if ref.id in self.NOTES:
            return "Body of " + self.NOTES[ref.id]
        return None

    @hookimpl
    def register_agent_reference_types(self, datasette):
        async def search(datasette, actor, q, limit):
            if q == "explode":
                raise ValueError("broken plugin")
            return [
                Reference(type="note", id=note_id, name=name.replace(" ", "_"))
                for note_id, name in self.NOTES.items()
                # Only the owner sees the secret one
                if (note_id != "n2" or (actor or {}).get("id") == "admin")
                and q.lower() in name.lower()
            ]

        return [
            ReferenceType(
                type="note",
                label="note",
                search=search,
                url=lambda datasette, ref: datasette.urls.path("/-/notes/" + ref.id),
                instructions="Use read_note with the id of a tagged note.",
                context=self.context,
            )
        ]


@pytest.fixture
def notes_plugin():
    plugin = NotesPlugin()
    pm.register(plugin, name="test-notes-plugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="test-notes-plugin")


@pytest.mark.asyncio
async def test_plugin_reference_type(datasette_instance, cookies, notes_plugin):
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=shop", cookies=cookies
    )
    assert response.json()["results"] == [
        {
            "type": "note",
            "id": "n1",
            "name": "Shopping_list",
            "description": "",
            "label": "note",
            "url": "/-/notes/n1",
        }
    ]
    # Permission filtering is the plugin's search function's job
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=secret", cookies=cookies
    )
    assert response.json()["results"] == []

    references, note = await prepare_references(
        datasette_instance,
        {"id": "user"},
        [{"type": "note", "id": "n1", "name": "Shopping_list"}],
    )
    assert references == [
        {"type": "note", "id": "n1", "name": "Shopping_list", "description": ""}
    ]
    assert "- #Shopping_list (note id: n1)\n  Body of Shopping list" in note
    assert "Use read_note with the id of a tagged note." in note


@pytest.mark.asyncio
async def test_broken_plugin_search_does_not_break_others(
    datasette_instance, cookies, notes_plugin
):
    response = await datasette_instance.client.get(
        "/-/agent/api/references?q=explode", cookies=cookies
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


@pytest.mark.asyncio
async def test_stream_with_references(datasette_instance, cookies):
    ds = datasette_instance
    resp = await ds.client.post(
        "/-/agent/api/conversations",
        content="{}",
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    conversation_id = resp.json()["conversation_id"]
    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps(
            {
                "message": "How many rows in #items?",
                "references": [
                    # A client-supplied url is ignored, never persisted
                    {
                        "type": "table",
                        "id": "demo/items",
                        "name": "items",
                        "description": "Table in demo",
                        "url": "https://example.com/",
                    },
                    {"type": "nope", "id": "x", "name": "secrets"},
                ],
            }
        ),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )

    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT message_json FROM agent_messages "
            "WHERE conversation_id = ? AND role = 'user'",
            [conversation_id],
        )
    ).first()
    msg = json.loads(row["message_json"])
    text_part, context_part = msg["parts"]
    assert text_part == {"type": "text", "text": "How many rows in #items?"}
    assert context_part[REFERENCES_KEY] == [
        {
            "type": "table",
            "id": "demo/items",
            "name": "items",
            "description": "Table in demo",
        }
    ]
    assert "#items (table id: demo/items)" in context_part["text"]
    assert "CREATE TABLE items" in context_part["text"]
    assert "secrets" not in context_part["text"]
    # The note isn't part of the user-visible text
    assert message_dict_text(msg) == "How many rows in #items?"

    # The title comes from the user's text, not the schema note
    title = (
        await db.execute(
            "SELECT title FROM agent_conversations WHERE id = ?", [conversation_id]
        )
    ).first()["title"]
    assert title == "How many rows in #items?"

    # Rendered page shows the user text with the references attached
    page = await ds.client.get(f"/-/agent/{conversation_id}", cookies=cookies)
    assert (
        'data-references=\'[{"description": "Table in demo", "id": "demo/items", '
        '"label": "table", "name": "items", "type": "table", "url": "/demo/items"}]\''
    ) in page.text
    assert "table id: demo/items" not in page.text
    assert "CREATE TABLE items" not in page.text

    # Markdown export mentions the tag but not the note
    markdown = await ds.client.get(
        f"/-/agent/{conversation_id}/markdown", cookies=cookies
    )
    assert "Tagged: `items` (Table in demo)" in markdown.text
    assert "table id: demo/items" not in markdown.text
    assert "CREATE TABLE items" not in markdown.text


def test_flatten_attaches_references_to_user_text():
    rows = [
        {
            "message_json": json.dumps(
                {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": "Look at #items"},
                        {
                            "type": "text",
                            "text": "schema...",
                            REFERENCES_KEY: [
                                {"type": "table", "database": "d", "name": "items"}
                            ],
                        },
                    ],
                }
            )
        }
    ]
    flattened = flatten_for_render(rows)
    assert len(flattened) == 1
    assert flattened[0]["content"] == "Look at #items"
    assert flattened[0]["references"] == [
        {"type": "table", "database": "d", "name": "items"}
    ]


@pytest.mark.asyncio
async def test_describe_query_tool(datasette_instance):
    from datasette_agent.query_tools import describe_query

    await datasette_instance.invoke_startup()
    actor = {"id": "user"}
    result = json.loads(
        await describe_query(datasette_instance, actor, "demo", "top_items")
    )
    assert result["ok"]
    assert result["title"] == "Top items"
    assert result["sql"] == "select * from items where qty > :min"
    assert result["parameters"] == ["min"]
    hidden = json.loads(
        await describe_query(datasette_instance, actor, "demo", "hidden_sql")
    )
    assert hidden["sql"] is None
    assert hidden["sql_hidden"] is True
    missing = json.loads(
        await describe_query(datasette_instance, actor, "demo", "nope")
    )
    assert not missing["ok"]


@pytest.mark.asyncio
async def test_describe_table_tool_handles_views(datasette_instance):
    from datasette_agent.sql_tools import _describe_table

    await datasette_instance.invoke_startup()
    result = json.loads(
        await _describe_table(datasette_instance, {"id": "user"}, "demo", "big_items")
    )
    assert [c["name"] for c in result["columns"]] == ["id", "name", "qty"]


@pytest.mark.asyncio
async def test_plugin_reference_context(datasette_instance, notes_plugin):
    def refs(*ids):
        return [{"type": "note", "id": i, "name": "note_" + i} for i in ids]

    _, note = await prepare_references(
        datasette_instance, {"id": "user"}, refs("n2", "boom", "huge")
    )
    # The context function decides who sees what
    assert "Secret plans" not in note
    # An exception just means no context; the message still goes
    assert "- #note_boom (note id: boom)\n\n" in note
    # Context is truncated
    assert "  " + "x" * 20000 + "\n  ... (truncated)" in note
    assert "x" * 20001 not in note

    _, note = await prepare_references(datasette_instance, {"id": "admin"}, refs("n2"))
    assert "  Body of Secret plans" in note
