import json

import pytest
import pytest_asyncio
from datasette.app import Datasette


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


@pytest.fixture
def datasette_instance(tmp_path):
    ds = Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={"permissions": {"datasette-agent": {"id": "user"}}},
        internal=str(tmp_path / "internal.db"),
    )
    ds.add_memory_database("data")
    return ds


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


async def start_conversation(datasette, cookies):
    resp = await datasette.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


async def add_messages(datasette, conversation_id, messages, title=None):
    """Persist a scripted transcript directly. Each entry is
    ("user"|"assistant", text) or ("tool_call", name, args) or
    ("tool_result", name, output)."""
    from datasette_agent.messages import insert_message

    db = datasette.get_internal_database()
    for entry in messages:
        kind = entry[0]
        if kind in ("user", "assistant"):
            msg = {"role": kind, "parts": [{"type": "text", "text": entry[1]}]}
        elif kind == "tool_call":
            msg = {
                "role": "assistant",
                "parts": [
                    {"type": "tool_call", "name": entry[1], "arguments": entry[2]}
                ],
            }
        elif kind == "tool_result":
            msg = {
                "role": "tool",
                "parts": [
                    {"type": "tool_result", "name": entry[1], "output": entry[2]}
                ],
            }
        else:
            raise ValueError(kind)
        await insert_message(db, conversation_id, msg)
    if title is not None:
        await db.execute_write(
            "UPDATE agent_conversations SET title = ? WHERE id = ?",
            [title, conversation_id],
        )


async def call_tool(datasette, cookies, conversation_id, name, arguments):
    response = await datasette.client.post(
        "/-/agent/{}/stream".format(conversation_id),
        content=json.dumps(
            {
                "message": json.dumps(
                    {"tool_calls": [{"name": name, "arguments": arguments}]}
                )
            }
        ),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    return _parse_sse(response.text)


async def answer(datasette, cookies, conversation_id, question_id, value):
    response = await datasette.client.post(
        "/-/agent/{}/question/{}".format(conversation_id, question_id),
        content=json.dumps({"answer": value}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    return _parse_sse(response.text)


def questions(events):
    return [e["data"] for e in events if e["event"] == "question"]


def tool_result(events):
    results = [e for e in events if e["event"] == "tool_result"]
    assert len(results) == 1
    return json.loads(results[0]["data"]["output"])


async def question_count(datasette):
    db = datasette.get_internal_database()
    return (await db.execute("SELECT count(*) AS c FROM agent_questions")).first()["c"]


SECRET = "the launch codes are 4-8-15-16-23-42"

OLD_TRANSCRIPT = [
    ("user", "Let's talk about pelicans in Half Moon Bay."),
    ("assistant", "Pelicans are wonderful. " + SECRET + " but that is unrelated."),
    ("tool_call", "sql_query", {"database": "data", "sql": "select * from birds"}),
    (
        "tool_result",
        "sql_query",
        json.dumps({"rows": [{"name": "brown pelican"}], "_html": "<b>hidden</b>"}),
    ),
    ("user", "Also tell me about Café Rouge, the restaurant with 100% ratings."),
    ("assistant", "Café Rouge is a fine place for pelican watching."),
]


@pytest_asyncio.fixture
async def old_conversation(datasette_instance, cookies):
    conversation_id = await start_conversation(datasette_instance, cookies)
    await add_messages(
        datasette_instance,
        conversation_id,
        OLD_TRANSCRIPT,
        title="Pelicans in Half Moon Bay",
    )
    return conversation_id


# --- registration and availability -------------------------------------


@pytest.mark.asyncio
async def test_conversation_tools_registered(datasette_instance):
    from datasette_agent.tools import get_agent_tools

    tools = await get_agent_tools(datasette_instance)
    names = {t.name for t in tools}
    assert "search_conversations" in names
    assert "read_conversation" in names


@pytest.mark.asyncio
async def test_conversation_tools_excluded_from_background_agents(
    datasette_instance, monkeypatch
):
    """Background agents have nobody watching to approve a search or a
    read, so the tools must not be offered to them at all."""
    from datasette_agent import background_agent
    from datasette_agent.api import start_background_agent

    seen = []
    original = background_agent.make_llm_tools

    def recording_make_llm_tools(tools, *args, **kwargs):
        seen.append({t.name for t in tools})
        return original(tools, *args, **kwargs)

    monkeypatch.setattr(background_agent, "make_llm_tools", recording_make_llm_tools)
    await datasette_instance.invoke_startup()
    agent_id = await start_background_agent(
        datasette_instance,
        {"id": "user"},
        json.dumps(
            {
                "tool_calls": [
                    {"name": "mark_finished", "arguments": {"final_message": "ok"}}
                ]
            }
        ),
    )
    await datasette_instance._background_agent_tasks[agent_id]
    assert seen
    for names in seen:
        assert "search_conversations" not in names
        assert "read_conversation" not in names
        assert "sql_query" in names


@pytest.mark.asyncio
async def test_chat_system_prompt_mentions_conversation_tools(
    datasette_instance, cookies
):
    """The chat system prompt should tell the model the tools exist;
    the shared base prompt (also used by background agents) must not."""
    from datasette_agent.agent import _build_system_prompt

    await datasette_instance.invoke_startup()
    base = await _build_system_prompt(datasette_instance, {"id": "user"})
    assert "search_conversations" not in base

    conversation_id = await start_conversation(datasette_instance, cookies)
    await datasette_instance.client.post(
        "/-/agent/{}/stream".format(conversation_id),
        content=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    db = datasette_instance.get_internal_database()
    row = (
        await db.execute(
            "SELECT system_prompt FROM agent_responses WHERE conversation_id = ?",
            [conversation_id],
        )
    ).first()
    assert "search_conversations" in row["system_prompt"]
    assert "read_conversation" in row["system_prompt"]


# --- search_conversations ----------------------------------------------


@pytest.mark.asyncio
async def test_search_asks_for_approval_and_returns_snippets(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "search_conversations",
        {"terms": ["pelican", "nothing-here"]},
    )
    qs = questions(events)
    assert len(qs) == 1
    question = qs[0]
    assert question["question_type"] == "boolean"
    assert question["tool_name"] == "search_conversations"
    assert "<code>pelican</code>" in question["html"]
    assert "<code>nothing-here</code>" in question["html"]
    # Nothing ran before approval
    assert not [e for e in events if e["event"] == "tool_result"]

    events = await answer(ds, cookies, conversation_id, question["id"], True)
    result = tool_result(events)
    assert result["ok"] is True
    assert result["terms"] == ["pelican", "nothing-here"]
    assert len(result["conversations"]) == 1
    found = result["conversations"][0]
    assert found["id"] == old_conversation
    assert found["title"] == "Pelicans in Half Moon Bay"
    assert found["message_count"] == len(OLD_TRANSCRIPT)
    assert found["started_at"]
    assert found["last_message_at"] >= found["started_at"]
    assert set(found) == {
        "id",
        "title",
        "started_at",
        "last_message_at",
        "message_count",
        "matches",
    }
    # One snippet for the term that matched, none for the other
    assert [m["term"] for m in found["matches"]] == ["pelican"]
    match = found["matches"][0]
    assert match["count"] >= 3
    assert "pelican" in match["snippet"].lower()
    # A snippet is short and does not leak the rest of the conversation
    assert len(match["snippet"]) <= 2 * 60 + len("pelican") + 6
    assert SECRET not in json.dumps(result)
    # The rendered HTML links to the conversation
    assert "/-/agent/{}".format(old_conversation) in result["_html"]


@pytest.mark.asyncio
async def test_search_matches_tool_calls_and_results_but_not_hidden_html(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    async def search(term):
        events = await call_tool(
            ds, cookies, conversation_id, "search_conversations", {"terms": [term]}
        )
        events = await answer(
            ds, cookies, conversation_id, questions(events)[0]["id"], True
        )
        return tool_result(events)

    # tool_call arguments
    result = await search("from birds")
    assert [c["id"] for c in result["conversations"]] == [old_conversation]
    # tool_result output
    result = await search("brown pelican")
    assert [c["id"] for c in result["conversations"]] == [old_conversation]
    # _html is a user-only side channel - the model never saw it, so it
    # must not be searchable either
    result = await search("hidden")
    assert result["conversations"] == []


@pytest.mark.asyncio
async def test_search_handles_non_ascii_and_like_wildcards(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    async def search(term):
        events = await call_tool(
            ds, cookies, conversation_id, "search_conversations", {"terms": [term]}
        )
        events = await answer(
            ds, cookies, conversation_id, questions(events)[0]["id"], True
        )
        return tool_result(events)

    # message_json stores non-ASCII as \uXXXX escapes
    result = await search("café rouge")
    assert [c["id"] for c in result["conversations"]] == [old_conversation]
    assert "Café Rouge" in result["conversations"][0]["matches"][0]["snippet"]
    # % and _ are LIKE wildcards and must be matched literally
    result = await search("100% ratings")
    assert [c["id"] for c in result["conversations"]] == [old_conversation]
    result = await search("100_ ratings")
    assert result["conversations"] == []


@pytest.mark.asyncio
async def test_search_excludes_current_and_other_actors_conversations(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    other_cookies = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    # Grant the other actor chat access for this test only
    ds.config["permissions"]["datasette-agent"] = {"id": ["user", "other"]}
    other_conversation = await start_conversation(ds, other_cookies)
    await add_messages(ds, other_conversation, [("user", "My private pelican notes")])

    conversation_id = await start_conversation(ds, cookies)
    await add_messages(ds, conversation_id, [("user", "pelican pelican pelican")])

    events = await call_tool(
        ds, cookies, conversation_id, "search_conversations", {"terms": ["pelican"]}
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], True
    )
    result = tool_result(events)
    ids = [c["id"] for c in result["conversations"]]
    assert old_conversation in ids
    assert conversation_id not in ids
    assert other_conversation not in ids


@pytest.mark.asyncio
async def test_search_declined(datasette_instance, cookies, old_conversation):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, conversation_id, "search_conversations", {"terms": ["pelican"]}
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], False
    )
    result = tool_result(events)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "conversations" not in result


@pytest.mark.asyncio
async def test_search_asks_every_time(datasette_instance, cookies, old_conversation):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    for _ in range(2):
        events = await call_tool(
            ds, cookies, conversation_id, "search_conversations", {"terms": ["pelican"]}
        )
        assert len(questions(events)) == 1
        events = await answer(
            ds, cookies, conversation_id, questions(events)[0]["id"], True
        )
        assert tool_result(events)["ok"] is True
    assert await question_count(ds) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terms,message",
    (
        (["a", "b", "c", "d", "e", "f"], "At most 5"),
        ([], "at least one"),
        (["   "], "at least one"),
        ([1, 2], "array of strings"),
        ({"term": "a"}, "array of strings"),
    ),
)
async def test_search_rejects_bad_terms_without_asking(
    datasette_instance, cookies, terms, message
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, conversation_id, "search_conversations", {"terms": terms}
    )
    assert not questions(events)
    result = tool_result(events)
    assert result["ok"] is False
    assert message in result["error"]
    # No question row was ever created
    assert await question_count(ds) == 0


@pytest.mark.asyncio
async def test_search_caps_results_per_term(datasette_instance, cookies):
    from datasette_agent.conversation_tools import MAX_SEARCH_RESULTS_PER_TERM

    ds = datasette_instance
    for i in range(MAX_SEARCH_RESULTS_PER_TERM + 2):
        cid = await start_conversation(ds, cookies)
        await add_messages(ds, cid, [("user", "walrus number {}".format(i))])
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, conversation_id, "search_conversations", {"terms": ["walrus"]}
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], True
    )
    result = tool_result(events)
    assert len(result["conversations"]) == MAX_SEARCH_RESULTS_PER_TERM
    assert result["truncated_terms"] == ["walrus"]
    assert "walrus" in result["note"]


# --- read_conversation -------------------------------------------------


@pytest.mark.asyncio
async def test_read_requires_grant_then_reads_sections(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "limit": 2},
    )
    qs = questions(events)
    assert len(qs) == 1
    question = qs[0]
    assert question["question_type"] == "boolean"
    assert question["tool_name"] == "read_conversation"
    assert "Pelicans in Half Moon Bay" in question["prompt"]
    assert "Pelicans in Half Moon Bay" in question["html"]
    assert "/-/agent/{}".format(old_conversation) in question["html"]
    assert str(len(OLD_TRANSCRIPT)) in question["html"]
    assert not [e for e in events if e["event"] == "tool_result"]

    events = await answer(ds, cookies, conversation_id, question["id"], True)
    result = tool_result(events)
    assert result["ok"] is True
    assert result["conversation"]["id"] == old_conversation
    assert result["conversation"]["message_count"] == len(OLD_TRANSCRIPT)
    assert result["start"] == 1
    assert result["end"] == 2
    assert result["next_start"] == 3
    assert [m["index"] for m in result["messages"]] == [1, 2]
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["parts"] == [
        {"type": "text", "text": OLD_TRANSCRIPT[0][1]}
    ]
    assert SECRET in result["messages"][1]["parts"][0]["text"]
    for message in result["messages"]:
        assert message["created_at"]

    # The grant is persisted: a second read of the same conversation with
    # different arguments does not ask again.
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "start": 3, "limit": 2},
    )
    assert not questions(events)
    result = tool_result(events)
    assert result["ok"] is True
    assert [m["index"] for m in result["messages"]] == [3, 4]
    call, call_result = result["messages"]
    assert call["role"] == "assistant"
    assert call["parts"] == [
        {
            "type": "tool_call",
            "name": "sql_query",
            "arguments": {"database": "data", "sql": "select * from birds"},
        }
    ]
    assert call_result["role"] == "tool"
    assert call_result["parts"][0]["type"] == "tool_result"
    assert call_result["parts"][0]["name"] == "sql_query"
    output = json.loads(call_result["parts"][0]["output"])
    assert output == {"rows": [{"name": "brown pelican"}]}
    assert "_html" not in output
    assert result["next_start"] == 5

    # Reading the tail reports there is nothing further
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "start": 5},
    )
    result = tool_result(events)
    assert [m["index"] for m in result["messages"]] == [5, 6]
    assert result["next_start"] is None
    assert await question_count(ds) == 1


@pytest.mark.asyncio
async def test_read_search_mode_returns_long_context(
    datasette_instance, cookies, old_conversation
):
    from datasette_agent.conversation_tools import READ_SNIPPET_CONTEXT

    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    # Search mode also asks for the grant first
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "search": "launch codes"},
    )
    question = questions(events)[0]
    events = await answer(ds, cookies, conversation_id, question["id"], True)
    result = tool_result(events)
    assert result["ok"] is True
    assert result["search"] == "launch codes"
    assert result["match_count"] == 1
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["message_index"] == 2
    assert match["role"] == "assistant"
    assert match["part"] == "text"
    # Longer context than search_conversations: the whole short message
    assert match["snippet"] == OLD_TRANSCRIPT[1][1]
    assert READ_SNIPPET_CONTEXT > 60

    # Once granted, searching again does not ask
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "search": "PELICAN"},
    )
    assert not questions(events)
    result = tool_result(events)
    assert result["match_count"] >= 3
    assert {m["part"] for m in result["matches"]} >= {"text", "tool_result"}


@pytest.mark.asyncio
async def test_read_search_mode_caps_matches(datasette_instance, cookies):
    from datasette_agent.conversation_tools import READ_MAX_MATCHES

    ds = datasette_instance
    old = await start_conversation(ds, cookies)
    await add_messages(
        ds, old, [("user", "otter " * (READ_MAX_MATCHES + 5))], title="Otters"
    )
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old, "search": "otter"},
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], True
    )
    result = tool_result(events)
    assert result["match_count"] == READ_MAX_MATCHES + 5
    assert len(result["matches"]) == READ_MAX_MATCHES
    assert "note" in result


@pytest.mark.asyncio
async def test_read_declined_records_no_grant(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation},
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], False
    )
    result = tool_result(events)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "messages" not in result
    db = ds.get_internal_database()
    assert (
        await db.execute("SELECT count(*) AS c FROM agent_conversation_grants")
    ).first()["c"] == 0

    # Asking again (different arguments) prompts the user again
    events = await call_tool(
        ds,
        cookies,
        conversation_id,
        "read_conversation",
        {"conversation_id": old_conversation, "start": 2},
    )
    assert len(questions(events)) == 1


@pytest.mark.asyncio
async def test_read_grant_is_scoped_to_the_granting_conversation(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    first = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, first, "read_conversation", {"conversation_id": old_conversation}
    )
    events = await answer(ds, cookies, first, questions(events)[0]["id"], True)
    assert tool_result(events)["ok"] is True

    second = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, second, "read_conversation", {"conversation_id": old_conversation}
    )
    assert len(questions(events)) == 1


@pytest.mark.asyncio
async def test_read_rejects_current_missing_and_foreign_conversations(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    other_cookies = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    ds.config["permissions"]["datasette-agent"] = {"id": ["user", "other"]}
    other_conversation = await start_conversation(ds, other_cookies)
    await add_messages(ds, other_conversation, [("user", "private")])

    conversation_id = await start_conversation(ds, cookies)

    async def attempt(target):
        events = await call_tool(
            ds,
            cookies,
            conversation_id,
            "read_conversation",
            {"conversation_id": target},
        )
        assert not questions(events)
        return tool_result(events)

    result = await attempt(conversation_id)
    assert result["ok"] is False
    assert "current conversation" in result["error"]

    result = await attempt("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert result["ok"] is False
    assert "not found" in result["error"]

    # Another actor's conversation looks exactly like a missing one
    result = await attempt(other_conversation)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert await question_count(ds) == 0


@pytest.mark.asyncio
async def test_read_validates_paging_arguments(
    datasette_instance, cookies, old_conversation
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    async def attempt(arguments):
        arguments = {"conversation_id": old_conversation, **arguments}
        events = await call_tool(
            ds, cookies, conversation_id, "read_conversation", arguments
        )
        return events

    for bad in (
        {"start": 0},
        {"limit": 0},
        {"limit": 999},
        {"start": "x"},
        {"search": ""},
    ):
        events = await attempt(bad)
        assert not questions(events)
        assert tool_result(events)["ok"] is False

    # start past the end is an error, reported after the grant
    events = await attempt({"start": 50})
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], True
    )
    result = tool_result(events)
    assert result["ok"] is False
    assert "beyond the last message" in result["error"]

    # Integer-valued strings are accepted
    events = await attempt({"start": "2", "limit": "1"})
    result = tool_result(events)
    assert result["ok"] is True
    assert [m["index"] for m in result["messages"]] == [2]


@pytest.mark.asyncio
async def test_read_respects_output_budget(datasette_instance, cookies):
    from datasette_agent.conversation_tools import (
        READ_MAX_CHARS,
        READ_PART_MAX_CHARS,
    )
    from datasette_agent.messages import MODEL_TOOL_OUTPUT_LIMIT

    ds = datasette_instance
    old = await start_conversation(ds, cookies)
    big = "x" * (READ_PART_MAX_CHARS + 500)
    await add_messages(
        ds,
        old,
        [("user", big), ("assistant", big), ("user", big), ("assistant", "short")],
        title="Big",
    )
    conversation_id = await start_conversation(ds, cookies)
    events = await call_tool(
        ds, cookies, conversation_id, "read_conversation", {"conversation_id": old}
    )
    events = await answer(
        ds, cookies, conversation_id, questions(events)[0]["id"], True
    )
    raw = [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    result = json.loads(raw)
    assert result["ok"] is True
    # Long parts are truncated with a marker, and the page stops before
    # the budget so the model-facing cap never has to mangle the JSON
    first_text = result["messages"][0]["parts"][0]["text"]
    assert first_text.startswith("x" * READ_PART_MAX_CHARS)
    assert "truncated" in first_text
    assert len(result["messages"]) < 4
    assert result["next_start"] == result["end"] + 1
    assert "note" in result
    assert len(raw) <= READ_MAX_CHARS + 500
    assert len(raw) <= MODEL_TOOL_OUTPUT_LIMIT


# --- helpers -----------------------------------------------------------


def test_json_like_pattern_matches_stored_json():
    from datasette_agent.conversation_tools import json_like_pattern, like_pattern

    assert like_pattern("50% off_now\\") == "%50\\% off\\_now\\\\%"
    # Non-ASCII is stored escaped by json.dumps, so the pattern is too
    assert json_like_pattern("café") == "%caf\\\\u00e9%"
    assert json_like_pattern('say "hi"') == '%say \\\\"hi\\\\"%'


def test_make_snippet():
    from datasette_agent.conversation_tools import make_snippet

    text = "aaaa\n\n  bbbb MATCH cccc\tdddd"
    start = text.index("MATCH")
    assert make_snippet(text, start, start + 5, 5) == "...bbbb MATCH cccc..."
    assert make_snippet(text, start, start + 5, 100) == "aaaa bbbb MATCH cccc dddd"


def test_normalize_terms():
    from datasette_agent.conversation_tools import normalize_terms

    assert normalize_terms(["  a ", "b", "a", ""]) == ["a", "b"]
    assert normalize_terms("solo") == ["solo"]
    with pytest.raises(ValueError):
        normalize_terms(["a", "b", "c", "d", "e", "f"])
    with pytest.raises(ValueError):
        normalize_terms({"term": "a"})
