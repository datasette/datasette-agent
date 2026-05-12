"""Tests for the Jinja filters registered by datasette-agent."""

import pytest

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
        "format_datetime",
        "2026-05-12T20:33:41.622326+00:00",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_without_microseconds(datasette_instance):
    out = await _filter(
        datasette_instance,
        "format_datetime",
        "2026-05-12T20:33:41+00:00",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_naive_isoformat(datasette_instance):
    """Some legacy rows may not carry a tz suffix; we still format them."""
    out = await _filter(
        datasette_instance,
        "format_datetime",
        "2026-05-12T20:33:41",
    )
    assert out == "2026-05-12 20:33:41"


@pytest.mark.asyncio
async def test_format_datetime_passthrough_for_garbage(datasette_instance):
    """If input isn't a recognizable ISO datetime, return it unchanged so
    the template still shows *something* rather than blanking out."""
    out = await _filter(datasette_instance, "format_datetime", "not a date")
    assert out == "not a date"


@pytest.mark.asyncio
async def test_format_datetime_passthrough_for_empty(datasette_instance):
    assert await _filter(datasette_instance, "format_datetime", "") == ""
    assert await _filter(datasette_instance, "format_datetime", None) == ""
