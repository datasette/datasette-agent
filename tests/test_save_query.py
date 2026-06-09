import json

import pytest
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
        metadata={
            "plugins": {
                "datasette-llm": {
                    "default_model": "echo",
                }
            }
        },
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
                "store-query": {"id": "user"},
                "execute-write-sql": {"id": "user"},
                "insert-row": {"id": "user"},
                "update-row": {"id": "user"},
                "delete-row": {"id": "user"},
            }
        },
        internal=str(tmp_path / "internal.db"),
    )
    ds.add_memory_database("data")
    return ds


@pytest.fixture
def cookies(datasette_instance):
    return {"ds_actor": datasette_instance.client.actor_cookie({"id": "user"})}


async def setup_data_table(datasette):
    db = datasette.get_database("data")
    await db.execute_write(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, name TEXT)"
    )


async def start_conversation(datasette, cookies):
    resp = await datasette.client.post(
        "/-/agent/api/conversations",
        content=json.dumps({}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    return resp.json()["conversation_id"]


async def call_save_query(datasette, cookies, conversation_id, arguments):
    response = await datasette.client.post(
        "/-/agent/{}/stream".format(conversation_id),
        content=json.dumps(
            {
                "message": json.dumps(
                    {"tool_calls": [{"name": "save_query", "arguments": arguments}]}
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


@pytest.mark.asyncio
async def test_save_query_tool_is_registered(datasette_instance):
    from datasette_agent.tools import get_agent_tools

    await datasette_instance.invoke_startup()
    tools = await get_agent_tools(datasette_instance)
    names = [tool.name for tool in tools]
    assert "save_query" in names


@pytest.mark.asyncio
async def test_save_query_read_only_flow(datasette_instance, cookies):
    ds = datasette_instance
    await setup_data_table(ds)
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {
            "database": "data",
            "name": "count_notes",
            "sql": "select count(*) as n from notes",
            "title": "Count the notes",
        },
    )
    questions = [e for e in events if e["event"] == "question"]
    assert len(questions) == 1
    question = questions[0]["data"]
    assert question["question_type"] == "boolean"
    assert "count_notes" in question["prompt"]
    assert "read-only" in question["prompt"]
    # The SQL is displayed, escaped, in the html
    assert "select count(*) as n from notes" in question["html"]

    events = await answer(ds, cookies, conversation_id, question["id"], True)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert len(tool_results) == 1
    result = json.loads(tool_results[0]["data"]["output"])
    assert result["ok"] is True
    assert result["name"] == "count_notes"
    assert result["is_write"] is False
    assert result["url"].endswith("/data/count_notes")

    query = await ds.get_query("data", "count_notes")
    assert query is not None
    assert query.sql == "select count(*) as n from notes"
    assert query.title == "Count the notes"
    assert query.owner_id == "user"
    assert query.is_write is False
    assert query.is_private is True
    assert query.source == "user"


@pytest.mark.asyncio
async def test_save_query_declined(datasette_instance, cookies):
    ds = datasette_instance
    await setup_data_table(ds)
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {"database": "data", "name": "nope", "sql": "select 1"},
    )
    question = [e for e in events if e["event"] == "question"][0]["data"]
    events = await answer(ds, cookies, conversation_id, question["id"], False)
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert await ds.get_query("data", "nope") is None


@pytest.mark.asyncio
async def test_save_query_write_query(datasette_instance, cookies):
    ds = datasette_instance
    await setup_data_table(ds)
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {
            "database": "data",
            "name": "add_note",
            "sql": "insert into notes (name) values (:name)",
        },
    )
    question = [e for e in events if e["event"] == "question"][0]["data"]
    assert "write" in question["prompt"]

    events = await answer(ds, cookies, conversation_id, question["id"], True)
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is True
    assert result["is_write"] is True

    query = await ds.get_query("data", "add_note")
    assert query.is_write is True
    assert query.parameters == ["name"]


@pytest.mark.asyncio
async def test_save_query_invalid_sql_skips_question(datasette_instance, cookies):
    ds = datasette_instance
    await setup_data_table(ds)
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {"database": "data", "name": "bad", "sql": "select * frmo nowhere"},
    )
    assert not [e for e in events if e["event"] == "question"]
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is False
    assert result["error"]
    db = ds.get_internal_database()
    assert (await db.execute("SELECT count(*) AS c FROM agent_questions")).first()[
        "c"
    ] == 0


@pytest.mark.asyncio
async def test_save_query_duplicate_name_skips_question(datasette_instance, cookies):
    ds = datasette_instance
    await setup_data_table(ds)
    await ds.invoke_startup()
    await ds.add_query("data", "existing", "select 1", source="user")
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {"database": "data", "name": "existing", "sql": "select 2"},
    )
    assert not [e for e in events if e["event"] == "question"]
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is False
    assert "exists" in result["error"]


@pytest.mark.asyncio
async def test_save_query_requires_store_query_permission(tmp_path):
    ds = Datasette(
        memory=True,
        metadata={"plugins": {"datasette-llm": {"default_model": "echo"}}},
        config={
            "permissions": {
                "datasette-agent": {"id": "user"},
                # no store-query grant
            }
        },
        internal=str(tmp_path / "internal.db"),
    )
    ds.add_memory_database("data")
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "user"})}
    conversation_id = await start_conversation(ds, cookies)

    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {"database": "data", "name": "q", "sql": "select 1"},
    )
    assert not [e for e in events if e["event"] == "question"]
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is False
    assert "store-query" in result["error"]


@pytest.mark.asyncio
async def test_save_query_unknown_database(datasette_instance, cookies):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await call_save_query(
        ds,
        cookies,
        conversation_id,
        {"database": "missing", "name": "q", "sql": "select 1"},
    )
    result = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert result["ok"] is False
    assert "missing" in result["error"]
