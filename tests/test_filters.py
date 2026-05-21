"""Tests for the Jinja filters registered by datasette-agent."""

import pytest
import json

from datasette.app import Datasette


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(
        memory=True,
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )


async def _filter(datasette, name, value):
    await datasette.invoke_startup()
    env = datasette.get_jinja_environment()
    return env.filters[name](value)


@pytest.mark.asyncio
async def test_format_datetime_strips_microseconds_and_timezone(
    datasette_instance,
):
    """Persisted timestamps are full ISO 8601 with microseconds + tz; the
    UI only needs YYYY-MM-DD HH:MM:SS so tables stay compact."""
    out = await _filter(
        datasette_instance,
        "agent_format_datetime",
        "2026-05-12T20:33:41.622326+00:00",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_without_microseconds(datasette_instance):
    out = await _filter(
        datasette_instance,
        "agent_format_datetime",
        "2026-05-12T20:33:41+00:00",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_naive_isoformat(datasette_instance):
    """Some legacy rows may not carry a tz suffix; we still format them."""
    out = await _filter(
        datasette_instance,
        "agent_format_datetime",
        "2026-05-12T20:33:41",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_passthrough_for_garbage(datasette_instance):
    """If input isn't a recognizable ISO datetime, return it unchanged so
    the template still shows *something* rather than blanking out."""
    out = await _filter(datasette_instance, "agent_format_datetime", "not a date")
    assert out == "not a date"


@pytest.mark.asyncio
async def test_format_datetime_passthrough_for_empty(datasette_instance):
    assert await _filter(datasette_instance, "agent_format_datetime", "") == ""
    assert await _filter(datasette_instance, "agent_format_datetime", None) == ""


@pytest.mark.asyncio
async def test_extract_edit_sql_url_for_non_html_result(datasette_instance):
    out = await _filter(
        datasette_instance,
        "agent_extract_edit_sql_url",
        json.dumps(
            {
                "columns": ["a"],
                "rows": [{"a": 1}],
                "_edit_sql_url": "/data/-/query?sql=select+a",
            }
        ),
    )
    assert out == "/data/-/query?sql=select+a"


@pytest.mark.asyncio
async def test_extract_edit_sql_url_for_html_result(datasette_instance):
    out = await _filter(
        datasette_instance,
        "agent_extract_edit_sql_url",
        json.dumps(
            {
                "_html": "<table></table>",
                "_edit_sql_url": "/data/-/query?sql=select+a",
            }
        ),
    )
    assert out == "/data/-/query?sql=select+a"
