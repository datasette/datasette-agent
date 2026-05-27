"""Tests for run-as-agent + identities list API (phase-07/04)."""

import json

import pytest
from datasette.app import Datasette

from datasette_agent import identities
from datasette_agent.api import (
    start_background_agent_as_identity,
    get_background_agent_status,
)
from datasette_agent.schema import ensure_tables

GOAL_THAT_FINISHES = json.dumps(
    {
        "prompt": "Test goal",
        "tool_calls": [
            {
                "name": "mark_finished",
                "arguments": {"final_message": "All done", "error": None},
            }
        ],
    }
)


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        internal=str(tmp_path / "internal.db"),
    )


async def _setup(ds):
    await ds.invoke_startup()
    await ensure_tables(ds.get_internal_database())


@pytest.mark.asyncio
async def test_run_as_identity_records_provenance(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug="researcher"
    )
    agent_id = await start_background_agent_as_identity(
        ds, "agent:researcher", GOAL_THAT_FINISHES, launched_by="alice"
    )
    # Wait for the background task to settle.
    task = ds._background_agent_tasks.get(agent_id)
    if task is not None:
        await task

    status = await get_background_agent_status(ds, agent_id)
    assert status["identity_id"] == "agent:researcher"
    assert status["launched_by"] == "alice"
    # The run's actor_id is the AGENT, not the launcher.
    assert status["actor_id"] == "agent:researcher"


@pytest.mark.asyncio
async def test_run_as_identity_actor_is_agent_not_launcher(datasette_instance):
    """The actor passed to tools (and thus datasette.allowed) is the agent."""
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug="researcher"
    )

    captured = {}

    # Monkeypatch run_background_agent to capture the actor it's called with.
    import datasette_agent.api as api_module

    real = api_module.run_background_agent

    async def _capture(datasette, actor, agent_id, tools=None):
        captured["actor"] = actor
        return await real(datasette, actor, agent_id, tools=tools)

    api_module.run_background_agent = _capture
    try:
        agent_id = await start_background_agent_as_identity(
            ds, "agent:researcher", GOAL_THAT_FINISHES, launched_by="alice"
        )
        task = ds._background_agent_tasks.get(agent_id)
        if task is not None:
            await task
    finally:
        api_module.run_background_agent = real

    assert captured["actor"]["id"] == "agent:researcher"
    assert captured["actor"]["kind"] == "agent"
    assert captured["actor"]["agent"] is True


@pytest.mark.asyncio
async def test_run_as_identity_unknown_raises(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    with pytest.raises(ValueError):
        await start_background_agent_as_identity(
            ds, "agent:nope", GOAL_THAT_FINISHES, launched_by="alice"
        )


@pytest.mark.asyncio
async def test_run_as_identity_disabled_raises(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug="researcher"
    )
    await identities.disable_identity(ds, "agent:researcher")
    with pytest.raises(ValueError):
        await start_background_agent_as_identity(
            ds, "agent:researcher", GOAL_THAT_FINISHES, launched_by="alice"
        )


# --- identities list API ---


def _actor_cookie(ds, actor_id):
    return {"ds_actor": ds.client.actor_cookie({"id": actor_id})}


@pytest.mark.asyncio
async def test_identities_api_requires_auth(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    response = await ds.client.get("/-/agent/api/identities")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_identities_api_owned_and_shareable(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Alice Private", owner_actor_id="alice", slug="alice-priv"
    )
    await identities.create_identity(
        ds,
        display_name="Bob Shared",
        owner_actor_id="bob",
        slug="bob-shared",
        shareable=True,
    )
    await identities.create_identity(
        ds, display_name="Bob Private", owner_actor_id="bob", slug="bob-priv"
    )

    response = await ds.client.get(
        "/-/agent/api/identities", cookies=_actor_cookie(ds, "alice")
    )
    assert response.status_code == 200
    data = response.json()
    ids = {a["id"] for a in data["results"]}
    # Alice sees her own private + bob's shareable, NOT bob's private.
    assert ids == {"agent:alice-priv", "agent:bob-shared"}
    # Shape: datasette-acl-share consumes "results"; "agents" is also provided.
    assert data["results"] == data["agents"]
    alice_agent = next(a for a in data["results"] if a["id"] == "agent:alice-priv")
    assert alice_agent["kind"] == "agent"
    assert alice_agent["owned_by_me"] is True
    assert alice_agent["avatar_url"].endswith("/-/agent/pic/alice-priv")
    bob_agent = next(a for a in data["results"] if a["id"] == "agent:bob-shared")
    assert bob_agent["owned_by_me"] is False


@pytest.mark.asyncio
async def test_identities_api_query_filter(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug="researcher"
    )
    await identities.create_identity(
        ds, display_name="Summarizer", owner_actor_id="alice", slug="summarizer"
    )
    response = await ds.client.get(
        "/-/agent/api/identities?q=research", cookies=_actor_cookie(ds, "alice")
    )
    data = response.json()
    ids = {a["id"] for a in data["results"]}
    assert ids == {"agent:researcher"}


@pytest.mark.asyncio
async def test_identities_api_excludes_disabled(datasette_instance):
    ds = datasette_instance
    await _setup(ds)
    await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug="researcher"
    )
    await identities.disable_identity(ds, "agent:researcher")
    response = await ds.client.get(
        "/-/agent/api/identities", cookies=_actor_cookie(ds, "alice")
    )
    data = response.json()
    assert data["results"] == []
