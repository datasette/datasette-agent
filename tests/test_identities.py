"""Tests for agent identity CRUD + token mint/verify (phase-07/02)."""

import pytest
from datasette.app import Datasette

from datasette_agent import identities
from datasette_agent.schema import ensure_tables


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(memory=True, internal=str(tmp_path / "internal.db"))


async def _setup(ds):
    # Signed-token minting/verification needs the token handler registered.
    await ds.invoke_startup()
    await ensure_tables(ds.get_internal_database())


@pytest.mark.asyncio
async def test_create_identity_returns_token_and_stores_hash(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    result = await identities.create_identity(
        ds,
        display_name="Researcher",
        owner_actor_id="user",
        slug="researcher",
        shareable=True,
    )
    assert result["id"] == "agent:researcher"
    assert result["slug"] == "researcher"
    assert result["display_name"] == "Researcher"
    assert result["shareable"] == 1
    token = result["token"]
    assert token and token.startswith("dsatok_")

    # Plaintext token must NOT be stored anywhere in the row.
    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT * FROM agent_identities WHERE id = ?", ["agent:researcher"]
        )
    ).first()
    assert token not in dict(row).values()
    assert row["token_hash"] is not None
    assert row["token_hash"] != token


@pytest.mark.asyncio
async def test_id_enforces_agent_prefix(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    # No slug => generated; still agent:-prefixed.
    result = await identities.create_identity(
        ds, display_name="Auto", owner_actor_id="user"
    )
    assert result["id"].startswith("agent:")


@pytest.mark.asyncio
async def test_verify_token_roundtrip(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    result = await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="user", slug="researcher"
    )
    token = result["token"]
    verified = await identities.verify_token(ds, token)
    assert verified is not None
    assert verified["id"] == "agent:researcher"


@pytest.mark.asyncio
async def test_verify_token_rejects_bad_token(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    assert await identities.verify_token(ds, None) is None
    assert await identities.verify_token(ds, "garbage") is None
    assert await identities.verify_token(ds, "dstok_notreal") is None


@pytest.mark.asyncio
async def test_verify_token_rejects_non_agent_actor(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    # A valid signed token for a *human* actor must not authenticate as an agent.
    human_token = await ds.create_token("user", expires_after=3600)
    assert await identities.verify_token(ds, human_token) is None


@pytest.mark.asyncio
async def test_verify_token_rejects_disabled(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    result = await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="user", slug="researcher"
    )
    token = result["token"]
    await identities.disable_identity(ds, "agent:researcher")
    assert await identities.verify_token(ds, token) is None
    # Re-enable -> works again.
    await identities.disable_identity(ds, "agent:researcher", disabled=False)
    assert await identities.verify_token(ds, token) is not None


@pytest.mark.asyncio
async def test_rotate_token_invalidates_old(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    result = await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="user", slug="researcher"
    )
    old_token = result["token"]
    new_token = await identities.rotate_token(ds, "agent:researcher")
    assert new_token != old_token
    # Old token no longer matches stored hash -> rejected.
    assert await identities.verify_token(ds, old_token) is None
    # New token works.
    assert await identities.verify_token(ds, new_token) is not None


@pytest.mark.asyncio
async def test_list_identities_filters(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Mine Private", owner_actor_id="alice", slug="mine-priv"
    )
    await identities.create_identity(
        ds,
        display_name="Mine Shared",
        owner_actor_id="alice",
        slug="mine-shared",
        shareable=True,
    )
    await identities.create_identity(
        ds, display_name="Bob's", owner_actor_id="bob", slug="bobs"
    )

    alice_all = await identities.list_identities(ds, owner="alice")
    assert {r["id"] for r in alice_all} == {"agent:mine-priv", "agent:mine-shared"}

    shareable = await identities.list_identities(ds, shareable=True)
    assert {r["id"] for r in shareable} == {"agent:mine-shared"}


@pytest.mark.asyncio
async def test_load_identity_actor(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="user", slug="researcher"
    )
    actor = await identities.load_identity_actor(ds, "agent:researcher")
    assert actor == {
        "id": "agent:researcher",
        "display_name": "Researcher",
        "kind": "agent",
        "agent": True,
    }
    # Disabled identity -> no actor.
    await identities.disable_identity(ds, "agent:researcher")
    assert await identities.load_identity_actor(ds, "agent:researcher") is None
    # Missing identity -> None.
    assert await identities.load_identity_actor(ds, "agent:nope") is None


@pytest.mark.asyncio
async def test_update_identity(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Old", owner_actor_id="user", slug="researcher"
    )
    await identities.update_identity(
        ds, "agent:researcher", display_name="New", shareable=True
    )
    row = await identities.get_identity(ds, "agent:researcher")
    assert row["display_name"] == "New"
    assert row["shareable"] == 1
