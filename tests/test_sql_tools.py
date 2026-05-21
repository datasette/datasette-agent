"""Tests for sql_query's display modes.

sql_query exposes three modes via a `display` parameter:

- model: results returned as JSON to the LLM only. No HTML rendered.
- both:  results returned to the LLM AND an HTML table rendered for the user.
- user:  HTML table rendered for the user; the LLM sees only column names,
         a row count, and the SQL it ran — saving tokens on bulk fetches.

The user-visible HTML rides on the `_html` side channel and full rowsets
that should not flow back to the model sit under `_rows` — both stripped
by prepare_tool_output_for_model before history rebuild.
"""

import json
from urllib.parse import urlencode

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


def _expected_sql_edit_url(ds, database, sql):
    return ds.urls.database(database) + "/-/query?" + urlencode({"sql": sql})


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
    assert data["_edit_sql_url"] == _expected_sql_edit_url(
        datasette_with_data, "data", "select * from items order by id"
    )


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
    expected_url = _expected_sql_edit_url(
        datasette_with_data, "data", "select * from items order by id"
    )
    assert data["_edit_sql_url"] == expected_url
    assert "<table" in data["_html"]
    assert '<div class="agent-sql-result-scroll">' in data["_html"]
    # Cell content from the rendered table
    assert "apple" in data["_html"]
    assert "View SQL query" not in data["_html"]
    assert expected_url not in data["_html"]


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
    expected_url = _expected_sql_edit_url(
        datasette_with_data, "data", "select * from items order by id"
    )
    assert data["_edit_sql_url"] == expected_url
    assert len(data["_rows"]) == 3
    assert '<div class="agent-sql-result-scroll">' in data["_html"]
    assert "View SQL query" not in data["_html"]
    assert expected_url not in data["_html"]


@pytest.mark.asyncio
async def test_sql_query_user_mode_preserves_large_user_visible_payload(
    datasette_with_data,
):
    """The tool must return valid full JSON even when _html/_rows are large.

    The browser extracts _html from this original output. Model-facing
    truncation happens later, after user-visible side-channel keys are removed.
    """
    await datasette_with_data.invoke_startup()
    db = datasette_with_data.add_memory_database("wide")
    columns_sql = ", ".join(f"c{i} TEXT" for i in range(36))
    await db.execute_write(f"CREATE TABLE wide_items ({columns_sql})")
    column_names = [f"c{i}" for i in range(36)]
    placeholders = ", ".join("?" for _ in column_names)
    insert_sql = (
        f"INSERT INTO wide_items ({', '.join(column_names)}) "
        f"VALUES ({placeholders})"
    )
    for i in range(10):
        await db.execute_write(
            insert_sql,
            [f"value {i}-{j} " + ("x" * 20) for j in range(36)],
        )

    tool = _get_sql_tool()
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="wide",
        sql="select * from wide_items",
        display="user",
    )
    assert len(out) > 10000
    data = json.loads(out)
    assert data["row_count"] == 10
    assert len(data["_rows"]) == 10
    assert "<table" in data["_html"]


@pytest.mark.asyncio
async def test_sql_query_user_mode_strips_to_summary_for_model(
    datasette_with_data,
):
    """After prepare_tool_output_for_model (i.e. what the model actually
    sees in history rebuild), `user` mode leaves columns + row_count +
    truncated, but nothing rowset-shaped."""
    from datasette_agent.messages import prepare_tool_output_for_model

    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    raw = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql="select * from items order by id",
        display="user",
    )
    stripped = json.loads(prepare_tool_output_for_model(raw))
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
    assert data["_edit_sql_url"] == _expected_sql_edit_url(
        datasette_with_data, "data", "select * from items order by id"
    )


@pytest.mark.asyncio
async def test_sql_query_error_includes_edit_sql_url(datasette_with_data):
    await _seed_items(datasette_with_data)
    tool = _get_sql_tool()
    sql = "select * from nope"
    out = await tool.fn(
        datasette=datasette_with_data,
        actor={"id": "user"},
        database="data",
        sql=sql,
    )
    data = json.loads(out)
    assert "error" in data
    assert data["_edit_sql_url"] == _expected_sql_edit_url(
        datasette_with_data, "data", sql
    )


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
    If the actor has execute-sql permission they show up in the listing
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
async def test_list_databases_hides_dbs_without_execute_sql(tmp_path):
    """A database the actor cannot execute-sql against must not appear in
    the listing — otherwise the model will happily suggest queries that
    will then be rejected at execution time."""
    from datasette_agent.sql_tools import _list_databases_and_tables

    ds = Datasette(
        memory=True,
        config={
            "databases": {
                "private": {"permissions": {"execute-sql": {"id": "someone_else"}}}
            }
        },
        internal=str(tmp_path / "internal.db"),
    )
    public = ds.add_memory_database("public")
    private = ds.add_memory_database("private")
    await public.execute_write("CREATE TABLE IF NOT EXISTS p (id INTEGER PRIMARY KEY)")
    await private.execute_write("CREATE TABLE IF NOT EXISTS s (id INTEGER PRIMARY KEY)")
    await ds.invoke_startup()

    result = json.loads(await _list_databases_and_tables(ds, {"id": "user"}))
    names = {d["database_name"] for d in result["databases"]}
    assert "public" in names
    assert "private" not in names
