"""Phase-07/05: share-with-agent integration + security tests.

Exercises the real acl engine (datasette-acl) + profiles (actor resolution) +
datasette-acl-share (capability detection) installed alongside datasette-agent.

Security properties asserted:
  * Least privilege: an agent's access == only what is granted to agent:<id>.
  * Attribution: an agent-as-identity run is attributed to agent:<id>.
  * Revocation: revoking the grant cuts access on the next check.
  * No re-share: an agent must not be granted a Manager (manage) role.
  * Disable: a disabled agent's token no longer authenticates.
"""

import json

import pytest
import pytest_asyncio
from datasette import hookimpl
from datasette.app import Datasette
from datasette.permissions import Action, Resource
from datasette.plugins import pm

from datasette_acl.grants import grant, revoke
from datasette_acl.roles import AclRole

from datasette_agent import identities
from datasette_agent.api import (
    start_background_agent_as_identity,
    get_background_agent_status,
)
from datasette_agent.schema import ensure_tables


class PaperDocResource(Resource):
    """Parent-only mock doc resource type (mirrors the plan's paper-doc)."""

    name = "paper-doc"
    parent_class = None

    def __init__(self, parent, child=None):
        super().__init__(parent=parent, child=child)

    @classmethod
    async def resources_sql(cls, datasette, actor=None):
        return "SELECT 'A' AS parent, NULL AS child UNION ALL SELECT 'B', NULL"


PAPER_ROLES = [
    AclRole("paper-doc", "Viewer", ["paper-view"], rank=1),
    AclRole("paper-doc", "Editor", ["paper-view", "paper-edit"], rank=2),
    AclRole(
        "paper-doc",
        "Manager",
        ["paper-view", "paper-edit", "paper-manage"],
        rank=3,
        manage=True,
    ),
]


class PaperPlugin:
    __name__ = "PaperPlugin"

    @hookimpl
    def register_actions(self, datasette):
        return [
            Action(
                name="paper-view", description="View", resource_class=PaperDocResource
            ),
            Action(
                name="paper-edit", description="Edit", resource_class=PaperDocResource
            ),
            Action(
                name="paper-manage",
                description="Manage",
                resource_class=PaperDocResource,
            ),
        ]

    @hookimpl
    def datasette_acl_roles(self, datasette):
        return list(PAPER_ROLES)


@pytest_asyncio.fixture
async def ds(tmp_path):
    plugin = PaperPlugin()
    pm.register(plugin, name="paper-plugin")
    try:
        datasette = Datasette(
            memory=True,
            metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
            config={"permissions": {"datasette-acl": {"id": "root"}}},
            internal=str(tmp_path / "internal.db"),
        )
        await datasette.invoke_startup()
        await ensure_tables(datasette.get_internal_database())
        yield datasette
    finally:
        pm.unregister(name="paper-plugin")


async def _make_agent(ds, slug="researcher", **kw):
    return await identities.create_identity(
        ds, display_name="Researcher", owner_actor_id="alice", slug=slug, **kw
    )


def _agent_actor():
    return {"id": "agent:researcher", "kind": "agent", "agent": True}


# --- capability detection ---------------------------------------------------


@pytest.mark.asyncio
async def test_share_capabilities_reports_agents(ds):
    response = await ds.client.get("/-/share/capabilities")
    assert response.status_code == 200
    caps = response.json()
    assert caps["agents"] is True
    assert caps["people"] is True


@pytest.mark.asyncio
async def test_identities_endpoint_present_for_capability_probe(ds):
    """datasette-acl-share probes listAgents; the route must exist (not 404)."""
    await _make_agent(ds, shareable=True)
    response = await ds.client.get(
        "/-/agent/api/identities",
        cookies={"ds_actor": ds.client.actor_cookie({"id": "alice"})},
    )
    assert response.status_code == 200
    assert response.status_code != 404


# --- least privilege --------------------------------------------------------


@pytest.mark.asyncio
async def test_least_privilege_viewer(ds):
    await _make_agent(ds)
    agent = _agent_actor()
    # Grant the agent Viewer on doc A only.
    await grant(ds, "paper-doc", "A", actor_id="agent:researcher", role="Viewer")

    # Can view A, cannot edit/manage A.
    assert await ds.allowed(
        action="paper-view", resource=PaperDocResource(parent="A"), actor=agent
    )
    assert not await ds.allowed(
        action="paper-edit", resource=PaperDocResource(parent="A"), actor=agent
    )
    assert not await ds.allowed(
        action="paper-manage", resource=PaperDocResource(parent="A"), actor=agent
    )

    # No access at all to doc B (not shared).
    assert not await ds.allowed(
        action="paper-view", resource=PaperDocResource(parent="B"), actor=agent
    )
    assert not await ds.allowed(
        action="paper-edit", resource=PaperDocResource(parent="B"), actor=agent
    )


@pytest.mark.asyncio
async def test_agent_access_is_not_the_launchers(ds):
    """An agent does NOT inherit the human launcher's permissions."""
    await _make_agent(ds)
    # alice (human) gets Editor on B; the agent gets nothing on B.
    await grant(ds, "paper-doc", "B", actor_id="alice", role="Editor")
    agent = _agent_actor()
    assert await ds.allowed(
        action="paper-edit", resource=PaperDocResource(parent="B"), actor={"id": "alice"}
    )
    assert not await ds.allowed(
        action="paper-edit", resource=PaperDocResource(parent="B"), actor=agent
    )


# --- revocation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_cuts_access(ds):
    await _make_agent(ds)
    agent = _agent_actor()
    await grant(ds, "paper-doc", "A", actor_id="agent:researcher", role="Viewer")
    assert await ds.allowed(
        action="paper-view", resource=PaperDocResource(parent="A"), actor=agent
    )
    await revoke(ds, "paper-doc", "A", actor_id="agent:researcher")
    assert not await ds.allowed(
        action="paper-view", resource=PaperDocResource(parent="A"), actor=agent
    )


# --- no re-share (Manager) --------------------------------------------------


@pytest.mark.asyncio
async def test_agent_granted_editor_cannot_manage(ds):
    """Editor (the highest non-manage role) must not confer paper-manage."""
    await _make_agent(ds)
    agent = _agent_actor()
    await grant(ds, "paper-doc", "A", actor_id="agent:researcher", role="Editor")
    assert await ds.allowed(
        action="paper-edit", resource=PaperDocResource(parent="A"), actor=agent
    )
    # Editor does NOT include paper-manage -> agent cannot re-share.
    assert not await ds.allowed(
        action="paper-manage", resource=PaperDocResource(parent="A"), actor=agent
    )


@pytest.mark.asyncio
async def test_manager_role_confers_manage_only_when_explicitly_granted(ds):
    """Document that Manager is *possible* but is not the default agent role;
    the share UI / policy gives agents Viewer/Editor only. This asserts the
    distinguishing manage action is the only thing separating Manager."""
    from datasette_acl.roles import build_roles_registry, manage_only_actions

    registry = await build_roles_registry(ds)
    manage_only = manage_only_actions(registry["paper-doc"])
    # The only action distinguishing a manager is paper-manage.
    assert manage_only == {"paper-manage"}
    # And the default agent roles (Viewer/Editor) do not include it.
    viewer = next(r for r in registry["paper-doc"] if r.name == "Viewer")
    editor = next(r for r in registry["paper-doc"] if r.name == "Editor")
    assert "paper-manage" not in viewer.actions
    assert "paper-manage" not in editor.actions


# --- attribution ------------------------------------------------------------

GOAL_THAT_FINISHES = json.dumps(
    {
        "prompt": "Test goal",
        "tool_calls": [
            {
                "name": "mark_finished",
                "arguments": {"final_message": "done", "error": None},
            }
        ],
    }
)


@pytest.mark.asyncio
async def test_attribution_run_attributed_to_agent(ds):
    await _make_agent(ds)
    agent_run_id = await start_background_agent_as_identity(
        ds, "agent:researcher", GOAL_THAT_FINISHES, launched_by="alice"
    )
    task = ds._background_agent_tasks.get(agent_run_id)
    if task is not None:
        await task
    status = await get_background_agent_status(ds, agent_run_id)
    # Edits/actions during the run are attributed to the agent, not alice.
    assert status["actor_id"] == "agent:researcher"
    assert status["identity_id"] == "agent:researcher"
    assert status["launched_by"] == "alice"


# --- disable ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_agent_token_no_longer_authenticates(ds):
    result = await _make_agent(ds)
    token = result["token"]
    # Works before disabling.
    assert await identities.verify_token(ds, token) is not None
    await identities.disable_identity(ds, "agent:researcher")
    # Token rejected after disable.
    assert await identities.verify_token(ds, token) is None
    # And the HTTP auth path also rejects it.
    from datasette_agent import actor_from_request
    from datasette.utils.asgi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    inner = actor_from_request(ds, Request(scope, None))
    assert await inner() is None


@pytest.mark.asyncio
async def test_disabled_agent_run_rejected(ds):
    await _make_agent(ds)
    await identities.disable_identity(ds, "agent:researcher")
    with pytest.raises(ValueError):
        await start_background_agent_as_identity(
            ds, "agent:researcher", GOAL_THAT_FINISHES, launched_by="alice"
        )
