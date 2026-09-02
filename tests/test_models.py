"""Model selection: which models the agent offers, choosing one when a
conversation starts, and that conversation staying pinned to it."""

import json

from datasette.app import Datasette
import pytest

from datasette_agent.models import get_agent_models
from datasette_agent.schema import ensure_tables

# gpt-4 is registered by llm's OpenAI plugin with supports_tools=False,
# so it exercises the "listed by datasette-llm but useless to the
# agent" path. require_keys is off because no OpenAI key exists here.
MULTI_MODEL_CONFIG = {
    "default_model": "echo",
    "require_keys": False,
    "purposes": {"agent": {"models": ["echo", "echo-needs-key", "gpt-4"]}},
}


def _make_datasette(tmp_path, llm_config):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": llm_config}},
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )


@pytest.fixture
def multi_model_datasette(tmp_path):
    return _make_datasette(tmp_path, MULTI_MODEL_CONFIG)


@pytest.fixture
def single_model_datasette(tmp_path):
    # Explicit allowlist: whichever provider keys the machine running
    # the tests happens to have must not leak extra models in.
    return _make_datasette(tmp_path, {"default_model": "echo", "models": ["echo"]})


@pytest.fixture
def cookies(multi_model_datasette):
    return {"ds_actor": multi_model_datasette.client.actor_cookie({"id": "user"})}


async def _create_conversation(ds, cookies, body):
    return await ds.client.post(
        "/-/agent/api/conversations",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )


async def _conversation_row(ds, conversation_id):
    db = ds.get_internal_database()
    return (
        await db.execute(
            "SELECT * FROM agent_conversations WHERE id = ?", [conversation_id]
        )
    ).first()


@pytest.mark.asyncio
async def test_get_agent_models_filters_to_tool_capable_and_orders_default_first(
    multi_model_datasette,
):
    await multi_model_datasette.invoke_startup()
    models, default_model = await get_agent_models(
        multi_model_datasette, {"id": "user"}
    )
    # gpt-4 is allowlisted for the purpose but cannot call tools.
    assert models == ["echo", "echo-needs-key"]
    assert default_model == "echo"


@pytest.mark.asyncio
async def test_get_agent_models_without_default_configured(tmp_path):
    ds = _make_datasette(tmp_path, {"require_keys": False, "models": ["echo"]})
    await ds.invoke_startup()
    models, default_model = await get_agent_models(ds, {"id": "user"})
    assert models == ["echo"]
    assert default_model is None


@pytest.mark.asyncio
async def test_get_agent_models_respects_purpose_blocklist(tmp_path):
    ds = _make_datasette(
        tmp_path,
        {
            "default_model": "echo",
            "require_keys": False,
            "models": ["echo", "echo-needs-key"],
            "purposes": {"agent": {"blocked_models": ["echo-needs-key"]}},
        },
    )
    await ds.invoke_startup()
    models, _ = await get_agent_models(ds, {"id": "user"})
    assert models == ["echo"]


@pytest.mark.asyncio
async def test_models_api(multi_model_datasette, cookies):
    response = await multi_model_datasette.client.get(
        "/-/agent/api/models", cookies=cookies
    )
    assert response.status_code == 200
    assert response.json() == {
        "models": ["echo", "echo-needs-key"],
        "default_model": "echo",
    }


@pytest.mark.asyncio
async def test_models_api_requires_permission(multi_model_datasette):
    response = await multi_model_datasette.client.get("/-/agent/api/models")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_index_renders_model_picker_when_several_models(
    multi_model_datasette, cookies
):
    response = await multi_model_datasette.client.get("/-/agent", cookies=cookies)
    assert response.status_code == 200
    assert '<select id="new-model" name="model">' in response.text
    assert '<option value="echo" selected>echo</option>' in response.text
    assert '<option value="echo-needs-key">echo-needs-key</option>' in response.text
    # Tool-less model never reaches the picker.
    assert 'value="gpt-4"' not in response.text


@pytest.mark.asyncio
async def test_index_pins_single_model_without_a_picker(single_model_datasette):
    cookies = {"ds_actor": single_model_datasette.client.actor_cookie({"id": "user"})}
    response = await single_model_datasette.client.get("/-/agent", cookies=cookies)
    assert response.status_code == 200
    assert "<select" not in response.text
    assert (
        '<input type="hidden" id="new-model" name="model" value="echo">'
        in response.text
    )
    assert "Model: <code>echo</code>" in response.text


@pytest.mark.asyncio
async def test_index_without_any_models_omits_picker(tmp_path):
    # Allow only echo, then block it: the allowlist keeps provider keys
    # on the test machine from adding models, the blocklist empties it.
    ds = _make_datasette(tmp_path, {"models": ["echo"], "blocked_models": ["echo"]})
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    response = await ds.client.get("/-/agent", cookies=cookies)
    assert response.status_code == 200
    assert 'id="new-model"' not in response.text


@pytest.mark.asyncio
async def test_create_conversation_stores_chosen_model(multi_model_datasette, cookies):
    response = await _create_conversation(
        multi_model_datasette, cookies, {"message": "hi", "model": "echo-needs-key"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["model_id"] == "echo-needs-key"
    row = await _conversation_row(multi_model_datasette, data["conversation_id"])
    assert row["model_id"] == "echo-needs-key"


@pytest.mark.asyncio
async def test_create_conversation_without_model_leaves_it_unset(
    multi_model_datasette, cookies
):
    response = await _create_conversation(
        multi_model_datasette, cookies, {"message": "hi"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["model_id"] is None
    row = await _conversation_row(multi_model_datasette, data["conversation_id"])
    assert row["model_id"] is None


@pytest.mark.asyncio
async def test_create_conversation_accepts_empty_body(multi_model_datasette, cookies):
    response = await multi_model_datasette.client.post(
        "/-/agent/api/conversations", cookies=cookies
    )
    assert response.status_code == 200
    assert response.json()["model_id"] is None


@pytest.mark.parametrize(
    "model,expected_error",
    [
        ("gpt-4", "Model not available: gpt-4"),
        ("no-such-model", "Model not available: no-such-model"),
        ("", "model must be a non-empty string"),
        (42, "model must be a non-empty string"),
    ],
)
@pytest.mark.asyncio
async def test_create_conversation_rejects_unavailable_model(
    multi_model_datasette, cookies, model, expected_error
):
    response = await _create_conversation(
        multi_model_datasette, cookies, {"message": "hi", "model": model}
    )
    assert response.status_code == 400
    assert response.json() == {"error": expected_error}
    db = multi_model_datasette.get_internal_database()
    await ensure_tables(db)
    count = (await db.execute("SELECT count(*) AS c FROM agent_conversations")).first()
    assert count["c"] == 0


@pytest.mark.asyncio
async def test_create_conversation_rejects_invalid_json(multi_model_datasette, cookies):
    response = await multi_model_datasette.client.post(
        "/-/agent/api/conversations",
        content="not json",
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid JSON body"}


@pytest.mark.asyncio
async def test_chat_runs_on_the_chosen_model(
    multi_model_datasette, cookies, monkeypatch
):
    """End to end: a conversation created with a non-default model must
    prompt that model, not the default. echo-needs-key proves it by
    echoing the key it was given, which the default echo model never
    sees."""
    monkeypatch.setenv("LLM_ECHO_NEEDS_KEY_KEY", "sekrit")
    ds = multi_model_datasette
    resp = await _create_conversation(
        ds, cookies, {"message": "hi", "model": "echo-needs-key"}
    )
    conversation_id = resp.json()["conversation_id"]

    response = await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "hello there"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    assert '\\"key\\": \\"sekrit\\"' in response.text

    db = ds.get_internal_database()
    responses = (
        await db.execute(
            "SELECT model_id FROM agent_responses WHERE conversation_id = ?",
            [conversation_id],
        )
    ).rows
    assert responses
    assert {r["model_id"] for r in responses} == {"echo-needs-key"}
    row = await _conversation_row(ds, conversation_id)
    assert row["model_id"] == "echo-needs-key"


@pytest.mark.asyncio
async def test_chat_without_chosen_model_pins_the_default(
    multi_model_datasette, cookies
):
    ds = multi_model_datasette
    resp = await _create_conversation(ds, cookies, {"message": "hi"})
    conversation_id = resp.json()["conversation_id"]
    assert (await _conversation_row(ds, conversation_id))["model_id"] is None

    await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert (await _conversation_row(ds, conversation_id))["model_id"] == "echo"


@pytest.mark.asyncio
async def test_every_turn_reuses_the_pinned_model(tmp_path, monkeypatch):
    """Once a conversation has a model_id, later turns must ask
    datasette-llm for exactly that model rather than re-resolving the
    default - that is what makes the choice stick when the configured
    default changes underneath a long-running conversation."""
    from llm.parts import StreamEvent

    from datasette_agent.agent import run_agent

    ds = Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )
    await ds.invoke_startup()
    db = ds.get_internal_database()
    await ensure_tables(db)
    conversation_id = "01PINNEDMODELAAAAAAAAAAAAA"
    now = "2026-09-02T00:00:00+00:00"
    await db.execute_write(
        "INSERT INTO agent_conversations "
        "(id, actor_id, title, model_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [conversation_id, "user", None, "pinned-model", now, now],
    )

    requested = []

    class FakeResponse:
        async def astream_events(self):
            yield StreamEvent(type="text", chunk="ok")

        def to_dict(self):
            return {
                "model": "pinned-model",
                "id": "fake-response",
                "prompt": {},
                "messages": [
                    {"role": "assistant", "parts": [{"type": "text", "text": "ok"}]}
                ],
            }

    class FakeChain:
        async def responses(self):
            yield FakeResponse()

    class FakeModel:
        model_id = "pinned-model"

        def chain(self, *args, **kwargs):
            return FakeChain()

    class FakeLLM:
        def __init__(self, datasette):
            pass

        async def model(self, model_id=None, purpose=None, actor=None):
            requested.append((model_id, purpose))
            return FakeModel()

    class Writer:
        def __init__(self):
            self.chunks = []

        async def write(self, chunk):
            self.chunks.append(chunk)

    monkeypatch.setattr("datasette_agent.agent.LLM", FakeLLM)

    for message in ("first", "second"):
        await run_agent(ds, {"id": "user"}, conversation_id, message, Writer())

    assert requested == [("pinned-model", "agent"), ("pinned-model", "agent")]


@pytest.mark.asyncio
async def test_conversation_page_shows_pinned_model(multi_model_datasette, cookies):
    resp = await _create_conversation(
        multi_model_datasette, cookies, {"message": "hi", "model": "echo-needs-key"}
    )
    conversation_id = resp.json()["conversation_id"]
    response = await multi_model_datasette.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert (
        '<span class="agent-model-badge" title="Model used for this conversation">'
        "echo-needs-key</span>"
    ) in response.text


@pytest.mark.asyncio
async def test_conversation_page_has_no_badge_before_model_resolved(
    multi_model_datasette, cookies
):
    resp = await _create_conversation(multi_model_datasette, cookies, {"message": "hi"})
    conversation_id = resp.json()["conversation_id"]
    response = await multi_model_datasette.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies
    )
    assert response.status_code == 200
    assert "agent-model-badge" not in response.text
