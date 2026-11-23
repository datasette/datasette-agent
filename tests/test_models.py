"""Model selection: which models the agent offers, choosing one when a
conversation starts, and that conversation staying pinned to it."""

import json

from datasette.app import Datasette
from datasette_llm import ModelNotFoundError
import pytest

from datasette_agent.models import get_agent_model, get_agent_models
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

    monkeypatch.setattr("datasette_agent.models.LLM", FakeLLM)

    for message in ("first", "second"):
        turn = run_agent(ds, {"id": "user"}, conversation_id, message)
        async for _ in turn.events():
            pass

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


# --- Pinned models that stop being available -------------------------------

PINNED_CONFIG = {
    "default_model": "echo",
    "models": ["echo", "echo-needs-key"],
}


async def _stream(ds, conversation_id, message):
    return await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps({"message": message}),
        headers={"Content-Type": "application/json"},
        cookies={"ds_actor": ds.client.actor_cookie({"id": "user"})},
    )


async def _response_count(ds, conversation_id):
    db = ds.get_internal_database()
    return (
        await db.execute(
            "SELECT count(*) FROM agent_responses WHERE conversation_id = ?",
            [conversation_id],
        )
    ).single_value()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later_config,keep_key",
    (
        # Dropped from the allowlist
        ({"default_model": "echo", "models": ["echo"]}, True),
        # Blocked for the agent purpose
        (
            {
                **PINNED_CONFIG,
                "purposes": {"agent": {"blocked_models": ["echo-needs-key"]}},
            },
            True,
        ),
        # Same configuration, but its API key has gone
        (PINNED_CONFIG, False),
    ),
    ids=("removed-from-allowlist", "blocked", "key-removed"),
)
async def test_pinned_model_no_longer_available_stops_the_conversation(
    tmp_path, monkeypatch, later_config, keep_key
):
    """A conversation keeps its model for life, but not past the point
    where an administrator stops offering that model."""
    monkeypatch.setenv("LLM_ECHO_NEEDS_KEY_KEY", "sekrit")
    ds = _make_datasette(tmp_path, PINNED_CONFIG)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    resp = await _create_conversation(
        ds, cookies, {"message": "hi", "model": "echo-needs-key"}
    )
    conversation_id = resp.json()["conversation_id"]
    first = await _stream(ds, conversation_id, "first")
    assert "event: error" not in first.text
    assert await _response_count(ds, conversation_id) == 1

    # The same instance restarted with the model taken away.
    if not keep_key:
        monkeypatch.delenv("LLM_ECHO_NEEDS_KEY_KEY")
    later = _make_datasette(tmp_path, later_config)
    second = await _stream(later, conversation_id, "second")
    assert "event: error" in second.text
    assert (
        "This conversation uses the model echo-needs-key, which is no longer "
        "available. Start a new chat to use a different model."
    ) in second.text
    # The model was not called again, and the conversation stays pinned.
    assert await _response_count(later, conversation_id) == 1
    assert (await _conversation_row(later, conversation_id))[
        "model_id"
    ] == "echo-needs-key"


# --- Running without a configured default model ----------------------------

# No default_model. LLM's own default is not in the allowlist, so
# datasette-llm has no default to offer either.
NO_DEFAULT_CONFIG = {"models": ["echo"]}


@pytest.mark.asyncio
async def test_get_agent_model_uses_configured_default(multi_model_datasette):
    await multi_model_datasette.invoke_startup()
    model = await get_agent_model(multi_model_datasette, {"id": "user"})
    assert model.model_id == "echo"


@pytest.mark.asyncio
async def test_get_agent_model_without_default_uses_first_on_offer(tmp_path):
    ds = _make_datasette(tmp_path, NO_DEFAULT_CONFIG)
    await ds.invoke_startup()
    model = await get_agent_model(ds, {"id": "user"})
    assert model.model_id == "echo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_config",
    (
        {"default_model": "no-such-model", "models": ["echo"]},
        {"purposes": {"agent": {"model": "no-such-model"}}, "models": ["echo"]},
    ),
    ids=("default_model", "purpose-model"),
)
async def test_get_agent_model_does_not_replace_a_broken_default(tmp_path, llm_config):
    """A configured default that cannot be used is a mistake to report,
    not a reason to quietly run on some other model."""
    ds = _make_datasette(tmp_path, llm_config)
    await ds.invoke_startup()
    with pytest.raises(ModelNotFoundError):
        await get_agent_model(ds, {"id": "user"})


@pytest.mark.asyncio
async def test_get_agent_model_without_any_models(tmp_path):
    ds = _make_datasette(tmp_path, {"models": ["no-such-model"]})
    await ds.invoke_startup()
    with pytest.raises(ModelNotFoundError):
        await get_agent_model(ds, {"id": "user"})


@pytest.mark.asyncio
async def test_chat_without_default_model_pins_first_on_offer(tmp_path):
    ds = _make_datasette(tmp_path, NO_DEFAULT_CONFIG)
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    resp = await _create_conversation(ds, cookies, {"message": "hi"})
    conversation_id = resp.json()["conversation_id"]
    response = await _stream(ds, conversation_id, "hello")
    assert "event: error" not in response.text
    assert (await _conversation_row(ds, conversation_id))["model_id"] == "echo"


@pytest.mark.asyncio
async def test_background_agent_runs_without_default_model(tmp_path):
    import asyncio

    from datasette_agent.api import (
        get_background_agent_status,
        start_background_agent,
    )

    ds = _make_datasette(tmp_path, NO_DEFAULT_CONFIG)
    await ds.invoke_startup()
    goal = json.dumps(
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
    agent_id = await start_background_agent(
        datasette=ds, actor={"id": "user"}, goal=goal
    )
    for _ in range(20):
        await asyncio.sleep(0.2)
        status = await get_background_agent_status(ds, agent_id)
        if status["status"] in ("completed", "error"):
            break
    assert status["error"] is None
    assert status["status"] == "completed"


def test_chat_command_runs_without_default_model():
    from click.testing import CliRunner
    from datasette.cli import cli

    result = CliRunner().invoke(
        cli,
        ["agent", "chat", "-s", "plugins.datasette-llm.models", '["echo"]']
        + ["-p", "hello"],
    )
    assert result.exit_code == 0, result.output
    assert "hello" in result.output


def test_chat_command_does_not_replace_an_unknown_model():
    from click.testing import CliRunner
    from datasette.cli import cli

    result = CliRunner().invoke(
        cli,
        ["agent", "chat", "-s", "plugins.datasette-llm.models", '["echo"]']
        + ["-m", "no-such-model", "-p", "hello"],
    )
    assert result.exit_code != 0
    assert "hello" not in result.output
