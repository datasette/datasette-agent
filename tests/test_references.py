import json
import sqlite3

from datasette.app import Datasette
import pytest

from datasette_agent.messages import flatten_for_render, message_dict_text
from datasette_agent.references import (
    REFERENCES_KEY,
    normalize_references,
    resolve_references,
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
    found = [(r["type"], r["database"], r["name"]) for r in results]
    # Exact match first, then prefix/substring matches
    assert found[0] == ("table", "demo", "items")
    assert ("table", "demo", "big_items") in found
    assert ("query", "demo", "top_items") in found
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


def test_normalize_references():
    assert normalize_references("nope") == []
    assert normalize_references(
        [
            {"type": "table", "database": "demo", "name": "items"},
            {"type": "table", "database": "demo", "name": "items"},
            {"type": "bogus", "database": "demo", "name": "items"},
            {"type": "table", "database": "", "name": "items"},
            {"type": "table", "database": "demo", "name": 5},
            "string",
        ]
    ) == [{"type": "table", "database": "demo", "name": "items"}]


@pytest.mark.asyncio
async def test_resolve_references(datasette_instance):
    await datasette_instance.invoke_startup()
    references, context = await resolve_references(
        datasette_instance,
        {"id": "user"},
        [
            {"type": "table", "database": "demo", "name": "items"},
            {"type": "table", "database": "demo", "name": "big_items"},
            {"type": "query", "database": "demo", "name": "top_items"},
            {"type": "query", "database": "demo", "name": "hidden_sql"},
            {"type": "database", "database": "demo", "name": "demo"},
            # Dropped: forbidden, missing, unknown database
            {"type": "table", "database": "demo", "name": "secrets"},
            {"type": "table", "database": "demo", "name": "missing"},
            {"type": "table", "database": "nope", "name": "items"},
        ],
    )
    assert [r["name"] for r in references] == [
        "items",
        "big_items",
        "top_items",
        "hidden_sql",
        "demo",
    ]
    assert "tagged the following resources" in context
    assert (
        'The table "items" in database "demo":\n'
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, qty INT)"
    ) in context
    assert 'The view "big_items"' in context
    assert "Title: Top items" in context
    assert "select * from items where qty > :min" in context
    assert "select 'shh'" not in context
    assert "(The SQL for this query is hidden.)" in context
    assert 'The database "demo" contains:' in context
    # The database summary respects table permissions too
    assert "secrets" not in context


@pytest.mark.asyncio
async def test_resolve_references_nothing_resolves(datasette_instance):
    await datasette_instance.invoke_startup()
    assert await resolve_references(
        datasette_instance,
        {"id": "user"},
        [{"type": "table", "database": "demo", "name": "secrets"}],
    ) == ([], None)


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
                    {"type": "table", "database": "demo", "name": "items"},
                    {"type": "table", "database": "demo", "name": "secrets"},
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
        {"type": "table", "database": "demo", "name": "items"}
    ]
    assert "CREATE TABLE items" in context_part["text"]
    assert "secrets" not in context_part["text"]
    # The schema note isn't part of the user-visible text
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
        'data-references=\'[{"database": "demo", "name": "items", '
        '"type": "table"}]\''
    ) in page.text
    assert "CREATE TABLE items" not in page.text

    # Markdown export mentions the tag but not the schema dump
    markdown = await ds.client.get(
        f"/-/agent/{conversation_id}/markdown", cookies=cookies
    )
    assert "Tagged: `items` (table in demo)" in markdown.text
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
