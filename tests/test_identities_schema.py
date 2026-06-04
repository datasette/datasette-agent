"""Tests for the agent_identities table + run provenance columns (phase-07/01)."""

import pytest
from datasette.app import Datasette

from datasette_agent.schema import ensure_tables


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(memory=True, internal=str(tmp_path / "internal.db"))


async def _table_columns(db, table):
    rows = (await db.execute(f"PRAGMA table_info({table})")).rows
    return {row["name"] for row in rows}


async def _tables(db):
    rows = (
        await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    ).rows
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_ensure_tables_creates_agent_identities(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    assert "agent_identities" in await _tables(db)
    cols = await _table_columns(db, "agent_identities")
    expected = {
        "id",
        "slug",
        "display_name",
        "description",
        "avatar_icon",
        "avatar_color",
        "owner_actor_id",
        "model_id",
        "system_prompt",
        "token_hash",
        "shareable",
        "created_at",
        "updated_at",
        "disabled",
    }
    assert expected <= cols


@pytest.mark.asyncio
async def test_run_provenance_columns_added(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    for table in ("agent_conversations", "agent_background_agents"):
        cols = await _table_columns(db, table)
        assert "identity_id" in cols
        assert "launched_by" in cols


@pytest.mark.asyncio
async def test_ensure_tables_idempotent(datasette_instance):
    """Running ensure_tables twice must not error and must not duplicate columns."""
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    cols_first = await _table_columns(db, "agent_background_agents")
    # Run again — must be a no-op (ALTER ADD COLUMN would error if re-run).
    await ensure_tables(db)
    cols_second = await _table_columns(db, "agent_background_agents")
    assert cols_first == cols_second


@pytest.mark.asyncio
async def test_provenance_columns_added_to_legacy_table(datasette_instance):
    """Simulate a pre-existing run table without the new columns, then upgrade."""
    db = datasette_instance.get_internal_database()
    # Create a legacy agent_background_agents table lacking the new columns.
    await db.execute_write(
        """
        CREATE TABLE agent_background_agents (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            actor_id TEXT,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            final_message TEXT,
            error TEXT,
            spawned_by_conversation_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cols_before = await _table_columns(db, "agent_background_agents")
    assert "identity_id" not in cols_before
    assert "launched_by" not in cols_before

    await ensure_tables(db)

    cols_after = await _table_columns(db, "agent_background_agents")
    assert "identity_id" in cols_after
    assert "launched_by" in cols_after


@pytest.mark.asyncio
async def test_insert_identity_roundtrip(datasette_instance):
    db = datasette_instance.get_internal_database()
    await ensure_tables(db)
    await db.execute_write(
        """
        INSERT INTO agent_identities
            (id, slug, display_name, owner_actor_id, shareable,
             created_at, updated_at, disabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "agent:researcher",
            "researcher",
            "Researcher",
            "user",
            1,
            "2026-05-26T00:00:00+00:00",
            "2026-05-26T00:00:00+00:00",
            0,
        ],
    )
    row = (
        await db.execute(
            "SELECT * FROM agent_identities WHERE id = ?", ["agent:researcher"]
        )
    ).first()
    assert row["id"] == "agent:researcher"
    assert row["slug"] == "researcher"
    assert row["display_name"] == "Researcher"
    assert row["owner_actor_id"] == "user"
    assert row["shareable"] == 1
    assert row["disabled"] == 0
