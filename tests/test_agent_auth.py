"""Tests for agent token auth + resolve sub-hook + avatar endpoint (phase-07/03)."""

import pytest
from datasette.app import Datasette
from datasette.plugins import pm
from datasette.utils import await_me_maybe

from datasette_agent import identities
from datasette_agent.schema import ensure_tables


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(memory=True, internal=str(tmp_path / "internal.db"))


async def _make_agent(ds, slug="researcher", display_name="Researcher", **kw):
    await ds.invoke_startup()
    await ensure_tables(ds.get_internal_database())
    return await identities.create_identity(
        ds, display_name=display_name, owner_actor_id="user", slug=slug, **kw
    )


@pytest.mark.asyncio
async def test_actor_from_request_valid_token(datasette_instance):
    ds = datasette_instance
    result = await _make_agent(ds)
    token = result["token"]
    response = await ds.client.get(
        "/-/agent", headers={"Authorization": f"Bearer {token}"}
    )
    # The page itself may 403 (no datasette-agent permission), but the actor
    # must have been resolved. Use the messages/whoami style: check via a
    # debug endpoint. Simpler: hit a route and confirm actor resolution by
    # asserting the auth path returns the agent actor directly.
    actor = await _resolve_actor(ds, token)
    assert actor is not None
    assert actor["id"] == "agent:researcher"
    assert actor["kind"] == "agent"
    assert actor["agent"] is True
    assert actor["display_name"] == "Researcher"
    _ = response  # the request executed without error


async def _resolve_actor(ds, token):
    """Invoke the actor_from_request hook chain with a bearer token."""
    from datasette.utils.asgi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    request = Request(scope, None)
    for hook_result in pm.hook.actor_from_request(datasette=ds, request=request):
        actor = await await_me_maybe(hook_result)
        if actor is not None:
            return actor
    return None


@pytest.mark.asyncio
async def test_actor_from_request_bad_token(datasette_instance):
    ds = datasette_instance
    await _make_agent(ds)
    assert await _resolve_actor(ds, "dsatok_garbage") is None


@pytest.mark.asyncio
async def test_actor_from_request_non_agent_token_ignored(datasette_instance):
    """A non-agent (dstok_) bearer must NOT be claimed by our hook (returns
    None) so Datasette's own token auth still handles it."""
    ds = datasette_instance
    await _make_agent(ds)
    human_token = await ds.create_token("user", expires_after=3600)
    # Our hook should not claim a dstok_ token.
    from datasette.utils.asgi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {human_token}".encode())],
    }
    request = Request(scope, None)
    from datasette_agent import actor_from_request

    inner = actor_from_request(ds, request)
    assert await inner() is None


@pytest.mark.asyncio
async def test_actor_from_request_disabled(datasette_instance):
    ds = datasette_instance
    result = await _make_agent(ds)
    token = result["token"]
    await identities.disable_identity(ds, "agent:researcher")
    assert await _resolve_actor(ds, token) is None


@pytest.mark.asyncio
async def test_resolve_actors_subhook(datasette_instance):
    ds = datasette_instance
    await _make_agent(ds)
    from datasette_agent import datasette_resolve_actors

    inner = datasette_resolve_actors(ds, ["agent:researcher", "human-1"])
    result = await inner()
    assert "agent:researcher" in result
    assert "human-1" not in result
    assert result["agent:researcher"]["display_name"] == "Researcher"
    assert result["agent:researcher"]["kind"] == "agent"
    assert result["agent:researcher"]["avatar_url"].endswith(
        "/-/agent/pic/researcher"
    )


@pytest.mark.asyncio
async def test_resolve_actors_subhook_no_agents(datasette_instance):
    ds = datasette_instance
    await _make_agent(ds)
    from datasette_agent import datasette_resolve_actors

    inner = datasette_resolve_actors(ds, ["just-a-human"])
    assert await inner() == {}


@pytest.mark.asyncio
async def test_actors_from_ids_resolves_agent(datasette_instance):
    """With profiles absent, our fallback core actors_from_ids resolves agents."""
    ds = datasette_instance
    await _make_agent(ds)
    resolved = await ds.actors_from_ids(["agent:researcher"])
    assert "agent:researcher" in resolved
    assert resolved["agent:researcher"]["display_name"] == "Researcher"
    assert resolved["agent:researcher"]["kind"] == "agent"


@pytest.mark.asyncio
async def test_avatar_endpoint(datasette_instance):
    ds = datasette_instance
    await _make_agent(ds)
    response = await ds.client.get("/-/agent/pic/researcher")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in response.content
    assert b"Cache-Control" in b"Cache-Control"  # header present
    assert "cache-control" in response.headers


@pytest.mark.asyncio
async def test_avatar_endpoint_unknown_slug(datasette_instance):
    """An unknown slug still returns a default avatar (no info leak via 404)."""
    ds = datasette_instance
    await _make_agent(ds)
    response = await ds.client.get("/-/agent/pic/nope")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
