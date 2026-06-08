import asyncio
import json

import pytest
from datasette.app import Datasette
from llm.parts import StreamEvent

from datasette_agent.agent import run_agent
from datasette_agent.ask_user import (
    AskUserUnavailable,
    ask_user,
    normalize_options,
    pending_questions,
    resolve_question,
)
from datasette_agent.schema import ensure_tables


def _parse_sse(text):
    events = []
    current_event = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: ") and current_event:
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append({"event": current_event, "data": data})
            current_event = None
    return events


class Writer:
    def __init__(self):
        self.chunks = []

    async def write(self, chunk):
        self.chunks.append(chunk)


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


async def _make_conversation(ds, conversation_id, actor_id="user"):
    db = ds.get_internal_database()
    await ensure_tables(db)
    now = "2026-05-19T00:00:00+00:00"
    await db.execute_write(
        "INSERT INTO agent_conversations (id, actor_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [conversation_id, actor_id, None, now, now],
    )


def test_normalize_options_accepts_strings_and_dicts():
    assert normalize_options(["a", "b"]) == [{"label": "a"}, {"label": "b"}]
    assert normalize_options(
        [{"label": "a", "description": "first"}, {"label": "b"}]
    ) == [{"label": "a", "description": "first"}, {"label": "b"}]


def test_normalize_options_rejects_bad_input():
    with pytest.raises(ValueError):
        normalize_options([])
    with pytest.raises(ValueError):
        normalize_options([{"description": "no label"}])
    with pytest.raises(ValueError):
        normalize_options([123])


@pytest.mark.asyncio
async def test_ask_user_raises_without_interactive_context():
    """Outside a chat turn (no current_ask_user set) ask_user must refuse
    rather than hang — this is the background-agent / CLI path."""
    with pytest.raises(AskUserUnavailable):
        await ask_user("Pick one", ["a", "b"])


def _fake_llm_asking(question, options, answer_holder):
    """Build a FakeLLM whose single response calls ask_user mid-stream."""

    class FakeResponse:
        async def astream_events(self):
            answer = await ask_user(question, options)
            answer_holder["answer"] = answer
            yield StreamEvent(type="text", chunk="You picked " + answer)

        def to_dict(self):
            return {
                "model": "fake",
                "id": "fake-response",
                "prompt": {},
                "messages": [
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "ok"}],
                    }
                ],
            }

    class FakeChain:
        async def responses(self):
            yield FakeResponse()

    class FakeModel:
        model_id = "fake"

        def chain(self, *args, **kwargs):
            return FakeChain()

    class FakeLLM:
        def __init__(self, datasette):
            pass

        async def model(self, purpose, actor):
            return FakeModel()

    return FakeLLM


@pytest.mark.asyncio
async def test_ask_user_blocks_until_answered(datasette_instance, monkeypatch):
    """The full round-trip: a tool calls ask_user, the turn parks on the
    question, and only resumes once the answer is resolved."""
    ds = datasette_instance
    await ds.invoke_startup()
    conversation_id = "01ASKUSERCONVAAAAAAAAAAAAA"
    await _make_conversation(ds, conversation_id)

    answer_holder = {}
    monkeypatch.setattr(
        "datasette_agent.agent.LLM",
        _fake_llm_asking(
            "Which fruit?",
            ["Apple", {"label": "Banana", "description": "yellow"}],
            answer_holder,
        ),
    )

    writer = Writer()
    run_task = asyncio.create_task(
        run_agent(ds, {"id": "user"}, conversation_id, "hello", writer)
    )

    # Wait for the question to be emitted, but don't let the turn finish.
    question = None
    for _ in range(200):
        events = _parse_sse("".join(writer.chunks))
        matches = [e for e in events if e["event"] == "user_question"]
        if matches:
            question = matches[0]["data"]
            break
        await asyncio.sleep(0.01)

    assert question is not None, "expected a user_question SSE event"
    assert question["question"] == "Which fruit?"
    assert question["options"] == [
        {"label": "Apple"},
        {"label": "Banana", "description": "yellow"},
    ]
    # The turn must still be running — it's blocked on the answer.
    assert not run_task.done()
    # And the question is registered as pending.
    assert question["id"] in pending_questions(ds)

    ok, error = resolve_question(ds, conversation_id, "user", question["id"], "Banana")
    assert ok
    assert error is None

    await asyncio.wait_for(run_task, timeout=5)
    assert answer_holder["answer"] == "Banana"

    events = _parse_sse("".join(writer.chunks))
    text = "".join(
        e["data"]["content"] for e in events if e["event"] == "text_chunk"
    )
    assert "You picked Banana" in text
    # The pending entry is cleaned up once answered.
    assert question["id"] not in pending_questions(ds)


def test_resolve_question_validates():
    ds = Datasette(memory=True)
    registry = pending_questions(ds)
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    registry["q1"] = {
        "future": future,
        "conversation_id": "conv",
        "actor_id": "user",
        "labels": ["Apple", "Banana"],
    }

    # Unknown question
    assert resolve_question(ds, "conv", "user", "missing", "Apple") == (
        False,
        "Question not found or already answered",
    )
    # Wrong conversation
    assert resolve_question(ds, "other", "user", "q1", "Apple") == (
        False,
        "Question does not belong to this conversation",
    )
    # Wrong actor
    assert resolve_question(ds, "conv", "someone", "q1", "Apple") == (
        False,
        "Forbidden",
    )
    # Not one of the offered options
    assert resolve_question(ds, "conv", "user", "q1", "Cherry") == (
        False,
        "Answer is not one of the offered options",
    )
    # Valid
    assert resolve_question(ds, "conv", "user", "q1", "Apple") == (True, None)
    assert future.result() == "Apple"
    loop.close()


@pytest.mark.asyncio
async def test_answer_endpoint_requires_permission(datasette_instance):
    response = await datasette_instance.client.post(
        "/-/agent/01ANSWERCONVAAAAAAAAAAAAAA/answer",
        content=json.dumps({"question_id": "x", "answer": "y"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_answer_endpoint_not_found(datasette_instance, cookies):
    response = await datasette_instance.client.post(
        "/-/agent/01ANSWERNOPECONVAAAAAAAAAA/answer",
        content=json.dumps({"question_id": "x", "answer": "y"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_answer_endpoint_wrong_actor_forbidden(datasette_instance, cookies):
    ds = datasette_instance
    conversation_id = "01ANSWEROWNERCONVAAAAAAAAA"
    await _make_conversation(ds, conversation_id, actor_id="user")
    other = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    # "other" has datasette-agent only if permission allows — default config
    # only grants it to id=user, so this 403s at the permission gate.
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/answer",
        content=json.dumps({"question_id": "x", "answer": "y"}),
        headers={"Content-Type": "application/json"},
        cookies=other,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_answer_endpoint_missing_fields(datasette_instance, cookies):
    ds = datasette_instance
    conversation_id = "01ANSWERMISSINGCONVAAAAAAA"
    await _make_conversation(ds, conversation_id)
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/answer",
        content=json.dumps({"question_id": "x"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_answer_endpoint_unknown_question(datasette_instance, cookies):
    ds = datasette_instance
    conversation_id = "01ANSWERUNKNOWNCONVAAAAAAA"
    await _make_conversation(ds, conversation_id)
    response = await ds.client.post(
        f"/-/agent/{conversation_id}/answer",
        content=json.dumps({"question_id": "nope", "answer": "y"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Question not found or already answered"
