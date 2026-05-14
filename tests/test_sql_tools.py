"""Tests for sql_query's display modes.

sql_query exposes three modes via a `display` parameter:

- model: results returned as JSON to the LLM only. No HTML rendered.
- both:  results returned to the LLM AND an HTML table rendered for the user.
- user:  HTML table rendered for the user; the LLM sees only column names,
         a row count, and the SQL it ran — saving tokens on bulk fetches.

The user-visible HTML rides on the `_html` side channel and full rowsets
that should not flow back to the model sit under `_rows` — both stripped
by strip_internal_keys before history rebuild.
"""

import json

from datasette.app import Datasette
import pytest


@pytest.fixture
def datasette_with_data(tmp_path):
    return Datasette(
        metadata={
            "plugins": {
                "datasette-llm": {
                    "default_model": "echo",
                }
            }
        },
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
                "execute-sql": {"id": "user"},
            }
        },
        internal=str(tmp_path / "internal.db"),
    )


async def _seed_items(ds):
    # Invoke startup so Datasette's default actions (execute-sql etc.) are
    # registered before sql_query's permission check runs.
    await ds.invoke_startup()
    # Datasette's :memory: databases share across instances (cache=shared),
    # so successive tests would accumulate rows. Use explicit IDs +
    # INSERT OR IGNORE so seeding is idempotent.
    db = ds.add_memory_database("data")
    await db.execute_write(
        "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT, qty INTEGER)"
    )
    for row_id, name, qty in [(1, "apple", 3), (2, "pear", 5), (3, "plum", 2)]:
        await db.execute_write(
            "INSERT OR IGNORE INTO items (id, name, qty) VALUES (?, ?, ?)",
            [row_id, name, qty],
        )


def _get_sql_tool():
    from datasette_agent.sql_tools import get_default_tools

    for tool in get_default_tools():
        if tool.name == "sql_query":
            return tool
    raise AssertionError("sql_query tool not found")


@pytest.mark.asyncio
async def test_sql_query_model_mode_default(datasette_with_data):
    """Default (no display arg) returns full rows to the model, no _html."""
    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
    )
    data = json.loads(out)
    assert data["columns"] == ["id", "name", "qty"]
    assert len(data["rows"]) == 3
    assert "_html" not in data
    assert "_rows" not in data


@pytest.mark.asyncio
async def test_sql_query_both_mode_returns_rows_and_html(datasette_with_data):
    """`both` mode gives the model the full rows AND renders an HTML table
    on the _html side channel for the user."""
    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
        display="both",
    )
    data = json.loads(out)
    assert data["columns"] == ["id", "name", "qty"]
    assert len(data["rows"]) == 3
    assert "_html" in data
    assert "<table" in data["_html"]
    # Cell content from the rendered table
    assert "apple" in data["_html"]


@pytest.mark.asyncio
async def test_sql_query_user_mode_hides_rows_from_model(datasette_with_data):
    """`user` mode replaces the bulky `rows` array with a `row_count`
    summary the model can reason over, parks the full rows under `_rows`
    for export, and renders the HTML table for the user."""
    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
        display="user",
    )
    data = json.loads(out)
    assert data["columns"] == ["id", "name", "qty"]
    assert data["row_count"] == 3
    assert "rows" not in data  # model-visible bulk rows are gone
    assert "_html" in data
    assert "_rows" in data
    assert len(data["_rows"]) == 3


@pytest.mark.asyncio
async def test_sql_query_user_mode_strips_to_summary_for_model(
    datasette_with_data,
):
    """After strip_internal_keys (i.e. what the model actually sees in
    history rebuild), `user` mode leaves columns + row_count + truncated +
    sql, but nothing rowset-shaped."""
    from datasette_agent.messages import strip_internal_keys

    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    raw = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
        display="user",
    )
    stripped = json.loads(strip_internal_keys(raw))
    assert "_html" not in stripped
    assert "_rows" not in stripped
    assert "rows" not in stripped
    assert stripped["row_count"] == 3
    assert stripped["columns"] == ["id", "name", "qty"]


@pytest.mark.asyncio
async def test_sql_query_display_appears_in_input_schema():
    """The display parameter must be advertised on the tool's input_schema
    so the model knows it can pick a mode."""
    tool = _get_sql_tool()
    props = tool.input_schema["properties"]
    assert "display" in props
    assert set(props["display"]["enum"]) == {"model", "both", "user"}
    # Display is optional — only database+sql are required.
    assert "display" not in tool.input_schema.get("required", [])


@pytest.mark.asyncio
async def test_sql_query_invalid_display_falls_back_to_model(
    datasette_with_data,
):
    """An unknown display value should not crash — fall back to model mode
    so a confused caller still gets a useful answer."""
    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
        display="nonsense",
    )
    data = json.loads(out)
    assert "rows" in data
    assert "_html" not in data


@pytest.mark.asyncio
async def test_render_rows_html_table_shape():
    """The helper produces a thead with column names and a tbody with the
    rows; truncation is signalled in a tfoot caption."""
    from datasette_agent.sql_tools import _render_rows_html

    html = _render_rows_html(
        columns=["a", "b"], rows=[[1, "x"], [2, "y"]], truncated=False
    )
    assert "<table" in html
    assert "<thead" in html and "<th>a</th>" in html and "<th>b</th>" in html
    assert "<tbody" in html
    assert "<td>1</td>" in html and "<td>x</td>" in html
    # Untruncated => no truncated marker
    assert "truncated" not in html.lower()


@pytest.mark.asyncio
async def test_render_rows_html_truncated_marker():
    from datasette_agent.sql_tools import _render_rows_html

    html = _render_rows_html(columns=["a"], rows=[[1]], truncated=True)
    assert "truncated" in html.lower()


@pytest.mark.asyncio
async def test_render_rows_html_escapes_html_in_cells():
    """Cell values may contain user-controlled strings; the helper must
    not let them break out of <td>."""
    from datasette_agent.sql_tools import _render_rows_html

    html = _render_rows_html(
        columns=["x"], rows=[["<script>alert(1)</script>"]], truncated=False
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.asyncio
async def test_list_databases_includes_underscore_db_when_permitted(tmp_path):
    """Databases whose name starts with `_` are not hidden from the agent.
    If the actor has view-database permission they show up in the listing
    alongside everything else — Datasette's permission system is the only
    gatekeeper."""
    from datasette_agent.sql_tools import _list_databases_and_tables

    ds = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    db = ds.add_memory_database("_custom")
    await db.execute_write("CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY)")
    await ds.invoke_startup()

    result = json.loads(await _list_databases_and_tables(ds, {"id": "user"}))
    names = {d["database_name"] for d in result["databases"]}
    assert "_custom" in names


@pytest.mark.asyncio
async def test_sql_query_unknown_database_lists_underscore_dbs(tmp_path):
    """When sql_query reports an unknown-database error, the
    `available_databases` list must include any underscore-prefixed
    databases the actor can execute-sql against."""
    from datasette_agent.sql_tools import _sql_query

    ds = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    db = ds.add_memory_database("_custom")
    await db.execute_write("CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY)")
    await ds.invoke_startup()

    result = json.loads(
        await _sql_query(ds, {"id": "user"}, "nope", "select 1")
    )
    assert result["error"] == "Database 'nope' not found"
    assert "_custom" in result["available_databases"]


@pytest.mark.asyncio
async def test_describe_table_unknown_database_lists_underscore_dbs(tmp_path):
    """The describe_table unknown-database error path must also include
    underscore databases in `available_databases`."""
    from datasette_agent.sql_tools import _describe_table

    ds = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    db = ds.add_memory_database("_custom")
    await db.execute_write("CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY)")
    await ds.invoke_startup()

    result = json.loads(
        await _describe_table(ds, {"id": "user"}, "nope", "foo")
    )
    assert result["error"] == "Database 'nope' not found"
    assert "_custom" in result["available_databases"]


@pytest.mark.asyncio
async def test_sql_query_executes_against_underscore_database(tmp_path):
    """A user with execute-sql permission must actually be able to run a
    query against an underscore-prefixed database — the filter previously
    blocked listing but the query path itself was fine; we still want a
    direct test that the end-to-end happy path works."""
    from datasette_agent.sql_tools import _sql_query

    ds = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    db = ds.add_memory_database("_custom")
    await db.execute_write("CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY)")
    await db.execute_write(
        "INSERT OR IGNORE INTO foo (id) VALUES (1), (2)"
    )
    await ds.invoke_startup()

    result = json.loads(
        await _sql_query(ds, {"id": "user"}, "_custom", "select count(*) as n from foo")
    )
    assert result["rows"] == [{"n": 2}]
