import json

import pytest
from datasette.app import Datasette


@pytest.fixture
def datasette_instance(tmp_path):
    return Datasette(
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
            }
        },
        internal=str(tmp_path / "internal.db"),
    )


async def make_context(datasette, **kwargs):
    from datasette_agent.questions import ToolContext
    from datasette_agent.schema import ensure_tables

    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    defaults = dict(
        datasette=datasette,
        actor={"id": "user"},
        conversation_id="01TESTCONVERSATION0000000",
        tool_name="edit_files",
        arguments={"path": "/tmp"},
        tool_call_id="call_1",
        supports_questions=True,
    )
    defaults.update(kwargs)
    return ToolContext(**defaults)


async def get_questions(datasette):
    db = datasette.get_internal_database()
    return [
        dict(r)
        for r in (
            await db.execute("SELECT * FROM agent_questions ORDER BY created_at")
        ).rows
    ]


async def answer_question(datasette, question_id, answer):
    db = datasette.get_internal_database()
    await db.execute_write(
        "UPDATE agent_questions SET status = 'answered', answer_json = ? WHERE id = ?",
        [json.dumps(answer), question_id],
    )


@pytest.mark.asyncio
async def test_ask_user_boolean_records_question_and_raises(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Is it OK to edit files in /tmp?")

    question = exc_info.value.question
    assert question["question_type"] == "boolean"
    assert question["prompt"] == "Is it OK to edit files in /tmp?"
    assert question["conversation_id"] == "01TESTCONVERSATION0000000"
    assert question["tool_name"] == "edit_files"

    rows = await get_questions(datasette_instance)
    assert len(rows) == 1
    assert rows[0]["id"] == question["id"]
    assert rows[0]["status"] == "pending"
    assert rows[0]["question_type"] == "boolean"
    assert rows[0]["ask_index"] == 0


@pytest.mark.asyncio
async def test_ask_user_choice_records_options(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Which mode?", options=["dry-run", "apply"])

    question = exc_info.value.question
    assert question["question_type"] == "choice"
    assert question["options"] == ["dry-run", "apply"]
    rows = await get_questions(datasette_instance)
    assert json.loads(rows[0]["options_json"]) == ["dry-run", "apply"]


@pytest.mark.asyncio
async def test_ask_user_free_text(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Describe the change", free_text=True)
    assert exc_info.value.question["question_type"] == "text"


@pytest.mark.asyncio
async def test_ask_user_replay_returns_answer(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Is it OK?")
    await answer_question(datasette_instance, exc_info.value.question["id"], True)

    # A fresh context for the same tool call replays the stored answer
    replay_context = await make_context(datasette_instance)
    answer = await replay_context.ask_user("Is it OK?")
    assert answer is True
    # No new question row was created
    assert len(await get_questions(datasette_instance)) == 1


@pytest.mark.asyncio
async def test_ask_user_sequential_questions_replay_in_order(datasette_instance):
    from datasette_agent.questions import QuestionPending

    # First execution: q0 asked
    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("First?")
    await answer_question(datasette_instance, exc_info.value.question["id"], True)

    # Second execution: q0 replays, q1 raises
    context2 = await make_context(datasette_instance)
    assert await context2.ask_user("First?") is True
    with pytest.raises(QuestionPending) as exc_info2:
        await context2.ask_user("Second?", options=["a", "b"])
    assert exc_info2.value.question["ask_index"] == 1
    await answer_question(datasette_instance, exc_info2.value.question["id"], "b")

    # Third execution: both replay
    context3 = await make_context(datasette_instance)
    assert await context3.ask_user("First?") is True
    assert await context3.ask_user("Second?", options=["a", "b"]) == "b"


@pytest.mark.asyncio
async def test_consumed_answers_do_not_replay(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Is it OK?")
    await answer_question(datasette_instance, exc_info.value.question["id"], True)

    done_context = await make_context(datasette_instance)
    assert await done_context.ask_user("Is it OK?") is True
    await done_context.mark_questions_consumed()

    # A later identical tool call must re-ask, not replay the stale answer
    later_context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending):
        await later_context.ask_user("Is it OK?")


@pytest.mark.asyncio
async def test_different_call_keys_do_not_share_answers(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Is it OK?")
    await answer_question(datasette_instance, exc_info.value.question["id"], True)

    other_call = await make_context(datasette_instance, tool_call_id="call_2")
    with pytest.raises(QuestionPending):
        await other_call.ask_user("Is it OK?")


@pytest.mark.asyncio
async def test_call_key_falls_back_to_arguments_hash(datasette_instance):
    from datasette_agent.questions import QuestionPending

    # No tool_call_id (e.g. echo model): key derives from tool name + args
    context = await make_context(datasette_instance, tool_call_id=None)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Is it OK?")
    await answer_question(datasette_instance, exc_info.value.question["id"], True)

    same_args = await make_context(datasette_instance, tool_call_id=None)
    assert await same_args.ask_user("Is it OK?") is True

    different_args = await make_context(
        datasette_instance, tool_call_id=None, arguments={"path": "/etc"}
    )
    with pytest.raises(QuestionPending):
        await different_args.ask_user("Is it OK?")


@pytest.mark.asyncio
async def test_ask_user_unsupported_context(datasette_instance):
    from datasette_agent.questions import QuestionsNotSupported

    context = await make_context(datasette_instance, supports_questions=False)
    with pytest.raises(QuestionsNotSupported):
        await context.ask_user("Is it OK?")
    # No orphaned question row
    assert await get_questions(datasette_instance) == []


@pytest.mark.asyncio
async def test_ask_user_auto_approve_boolean(datasette_instance):
    context = await make_context(
        datasette_instance, supports_questions=False, auto_approve=True
    )
    assert await context.ask_user("Is it OK?") is True
    assert await get_questions(datasette_instance) == []


@pytest.mark.asyncio
async def test_ask_user_auto_approve_does_not_answer_choices(datasette_instance):
    from datasette_agent.questions import QuestionsNotSupported

    context = await make_context(
        datasette_instance, supports_questions=False, auto_approve=True
    )
    with pytest.raises(QuestionsNotSupported):
        await context.ask_user("Which mode?", options=["dry-run", "apply"])


@pytest.mark.asyncio
async def test_ask_user_text_displays_as_escaped_html(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Review this?", text="<b>plain text</b>")

    assert exc_info.value.question["html"] == "<pre>&lt;b&gt;plain text&lt;/b&gt;</pre>"


@pytest.mark.asyncio
async def test_ask_user_options_and_free_text_are_exclusive(datasette_instance):
    context = await make_context(datasette_instance)
    with pytest.raises(ValueError):
        await context.ask_user("Pick", options=["a"], free_text=True)


# ---- context injection via make_llm_tools ----


@pytest.mark.asyncio
async def test_context_injected_into_tools_that_declare_it(datasette_instance):
    import llm
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import AgentTool, make_llm_tools

    await datasette_instance.invoke_startup()
    await ensure_tables(datasette_instance.get_internal_database())

    captured = {}

    async def with_context(datasette, actor, context, path):
        captured["context"] = context
        return json.dumps({"path": path})

    async def without_context(datasette, actor, path):
        captured["plain_kwargs"] = {"path": path}
        return json.dumps({"path": path})

    tools = [
        AgentTool(
            name="with_context",
            description="t",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            fn=with_context,
        ),
        AgentTool(
            name="without_context",
            description="t",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            fn=without_context,
        ),
    ]
    llm_tools = make_llm_tools(
        tools,
        datasette_instance,
        {"id": "user"},
        conversation_id="01TESTCONVERSATION0000000",
        supports_questions=True,
    )
    model = llm.get_async_model("echo")
    chain = model.chain(
        json.dumps(
            {
                "tool_calls": [
                    {"name": "with_context", "arguments": {"path": "/tmp"}},
                    {"name": "without_context", "arguments": {"path": "/etc"}},
                ]
            }
        ),
        tools=llm_tools,
    )
    await chain.text()

    context = captured["context"]
    assert context.tool_name == "with_context"
    assert context.arguments == {"path": "/tmp"}
    assert context.conversation_id == "01TESTCONVERSATION0000000"
    assert context.actor == {"id": "user"}
    assert context.supports_questions is True
    # The tool without a context parameter is invoked exactly as before
    assert captured["plain_kwargs"] == {"path": "/etc"}


@pytest.mark.asyncio
async def test_context_defaults_to_no_question_support(datasette_instance):
    import llm
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import AgentTool, make_llm_tools

    await datasette_instance.invoke_startup()
    await ensure_tables(datasette_instance.get_internal_database())

    captured = {}

    async def with_context(datasette, actor, context):
        captured["context"] = context
        return "ok"

    llm_tools = make_llm_tools(
        [
            AgentTool(
                name="with_context",
                description="t",
                input_schema={"type": "object", "properties": {}},
                fn=with_context,
            )
        ],
        datasette_instance,
        {"id": "user"},
        conversation_id="01TESTCONVERSATION0000000",
    )
    model = llm.get_async_model("echo")
    chain = model.chain(
        json.dumps({"tool_calls": [{"name": "with_context"}]}),
        tools=llm_tools,
    )
    await chain.text()
    assert captured["context"].supports_questions is False


# ---- chain suspension via the stream endpoint ----


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
def ask_tool_plugin():
    from datasette import hookimpl
    from datasette.plugins import pm
    from datasette_agent.tools import AgentTool

    class AskToolPlugin:
        __name__ = "AskToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def approve_edit(datasette, actor, context, path):
                ok = await context.ask_user(
                    "Is it OK to edit files in {}?".format(path)
                )
                return json.dumps({"approved": ok, "path": path})

            async def no_questions(datasette, actor, path):
                return json.dumps({"looked_at": path})

            async def approve_html(datasette, actor, context, path):
                ok = await context.ask_user(
                    "Run this command?", html="<pre>rm -rf {}</pre>".format(path)
                )
                return json.dumps({"approved": ok})

            async def multi_step(datasette, actor, context, path):
                ok = await context.ask_user("Approve {}?".format(path))
                if not ok:
                    return json.dumps({"cancelled": True})
                mode = await context.ask_user(
                    "Which mode?", options=["dry-run", "apply"]
                )
                note = await context.ask_user("Any notes?", free_text=True)
                return json.dumps({"path": path, "mode": mode, "note": note})

            return [
                AgentTool(
                    name="approve_edit",
                    description="Edit files (asks first)",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=approve_edit,
                ),
                AgentTool(
                    name="no_questions",
                    description="Look at files",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=no_questions,
                ),
                AgentTool(
                    name="approve_html",
                    description="Run a command (asks with html)",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=approve_html,
                ),
                AgentTool(
                    name="multi_step",
                    description="Edit files (asks three questions)",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=multi_step,
                ),
            ]

    plugin = AskToolPlugin()
    pm.register(plugin, name="AskToolPlugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="AskToolPlugin")


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
    return resp.json()["conversation_id"]


async def send_message(datasette, cookies, conversation_id, message):
    response = await datasette.client.post(
        "/-/agent/{}/stream".format(conversation_id),
        content=json.dumps({"message": message}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    assert response.status_code == 200
    return _parse_sse(response.text)


@pytest.mark.asyncio
async def test_ask_user_suspends_turn(datasette_instance, cookies, ask_tool_plugin):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {"tool_calls": [{"name": "approve_edit", "arguments": {"path": "/tmp"}}]}
        ),
    )
    types = [e["event"] for e in events]

    # The question reached the stream, the turn ended, and no tool_result
    # was sent for the suspended call
    question_events = [e for e in events if e["event"] == "question"]
    assert len(question_events) == 1
    question = question_events[0]["data"]
    assert question["question_type"] == "boolean"
    assert question["prompt"] == "Is it OK to edit files in /tmp?"
    assert question["tool_name"] == "approve_edit"
    assert "id" in question
    assert "tool_result" not in types
    assert {"event": "done", "data": {"question_pending": True}} in events
    assert "error" not in types

    # DB state: question row pending, assistant tool_call persisted,
    # no tool_result row for the suspended call
    db = ds.get_internal_database()
    q_rows = (await db.execute("SELECT * FROM agent_questions")).rows
    assert len(q_rows) == 1
    assert q_rows[0]["status"] == "pending"
    assert q_rows[0]["id"] == question["id"]

    messages = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    roles = [m["role"] for m in messages]
    assert "tool" not in roles
    assistant_jsons = [
        json.loads(m["message_json"]) for m in messages if m["role"] == "assistant"
    ]
    tool_call_parts = [
        p for m in assistant_jsons for p in m["parts"] if p["type"] == "tool_call"
    ]
    assert any(p["name"] == "approve_edit" for p in tool_call_parts)


@pytest.mark.asyncio
async def test_sibling_tool_results_survive_suspension(
    datasette_instance, cookies, ask_tool_plugin
):
    """When one tool call suspends, results from other tool calls in the
    same response must still be persisted and streamed."""
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {
                "tool_calls": [
                    {"name": "approve_edit", "arguments": {"path": "/tmp"}},
                    {"name": "no_questions", "arguments": {"path": "/etc"}},
                ]
            }
        ),
    )
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "no_questions"
    assert [e for e in events if e["event"] == "question"]

    db = ds.get_internal_database()
    tool_messages = (
        await db.execute(
            "SELECT message_json FROM agent_messages "
            "WHERE conversation_id = ? AND role = 'tool'",
            [conversation_id],
        )
    ).rows
    names = [
        p["name"]
        for m in tool_messages
        for p in json.loads(m["message_json"])["parts"]
        if p["type"] == "tool_result"
    ]
    assert names == ["no_questions"]


# ---- answer endpoint + resume ----


async def answer_via_api(datasette, cookies, conversation_id, question_id, answer):
    response = await datasette.client.post(
        "/-/agent/{}/question/{}".format(conversation_id, question_id),
        content=json.dumps({"answer": answer}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )
    return response


async def suspend_on_approve_edit(datasette, cookies):
    conversation_id = await start_conversation(datasette, cookies)
    events = await send_message(
        datasette,
        cookies,
        conversation_id,
        json.dumps(
            {"tool_calls": [{"name": "approve_edit", "arguments": {"path": "/tmp"}}]}
        ),
    )
    question = [e for e in events if e["event"] == "question"][0]["data"]
    return conversation_id, question


@pytest.mark.asyncio
async def test_answer_resumes_tool_and_continues_chain(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id, question = await suspend_on_approve_edit(ds, cookies)

    response = await answer_via_api(ds, cookies, conversation_id, question["id"], True)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    events = _parse_sse(response.text)
    types = [e["event"] for e in events]

    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "approve_edit"
    assert json.loads(tool_results[0]["data"]["output"]) == {
        "approved": True,
        "path": "/tmp",
    }
    assert types[-1] == "done"
    assert events[-1]["data"] == {}
    assert "error" not in types

    db = ds.get_internal_database()
    # Question is consumed, answer recorded with who answered it
    q_row = (
        await db.execute("SELECT * FROM agent_questions WHERE id = ?", [question["id"]])
    ).first()
    assert q_row["status"] == "consumed"
    assert json.loads(q_row["answer_json"]) is True
    assert q_row["answered_by"] == "user"
    assert q_row["answered_at"]

    # Message chain: ... assistant(tool_call), tool(result), assistant
    messages = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    roles = [m["role"] for m in messages]
    assert "tool" in roles
    # The chain continued: an assistant message follows the tool result
    assert roles.index("tool") < len(roles) - 1
    assert roles[-1] == "assistant"


@pytest.mark.asyncio
async def test_answer_false_reaches_tool(datasette_instance, cookies, ask_tool_plugin):
    ds = datasette_instance
    conversation_id, question = await suspend_on_approve_edit(ds, cookies)
    response = await answer_via_api(ds, cookies, conversation_id, question["id"], False)
    events = _parse_sse(response.text)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert json.loads(tool_results[0]["data"]["output"])["approved"] is False


@pytest.mark.asyncio
async def test_multi_question_tool_asks_again_on_resume(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {"tool_calls": [{"name": "multi_step", "arguments": {"path": "/tmp"}}]}
        ),
    )
    q0 = [e for e in events if e["event"] == "question"][0]["data"]
    assert q0["question_type"] == "boolean"

    # First answer: tool re-executes, asks the choice question
    response = await answer_via_api(ds, cookies, conversation_id, q0["id"], True)
    events = _parse_sse(response.text)
    questions = [e for e in events if e["event"] == "question"]
    assert len(questions) == 1
    q1 = questions[0]["data"]
    assert q1["question_type"] == "choice"
    assert q1["options"] == ["dry-run", "apply"]
    assert {"event": "done", "data": {"question_pending": True}} in events
    assert not [e for e in events if e["event"] == "tool_result"]

    # Second answer: tool asks the free text question
    response = await answer_via_api(ds, cookies, conversation_id, q1["id"], "apply")
    events = _parse_sse(response.text)
    q2 = [e for e in events if e["event"] == "question"][0]["data"]
    assert q2["question_type"] == "text"

    # Final answer: tool completes with all three answers replayed
    response = await answer_via_api(
        ds, cookies, conversation_id, q2["id"], "deploy notes"
    )
    events = _parse_sse(response.text)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert json.loads(tool_results[0]["data"]["output"]) == {
        "path": "/tmp",
        "mode": "apply",
        "note": "deploy notes",
    }
    assert events[-1] == {"event": "done", "data": {}}


@pytest.mark.asyncio
async def test_answer_validation(datasette_instance, cookies, ask_tool_plugin):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {"tool_calls": [{"name": "multi_step", "arguments": {"path": "/tmp"}}]}
        ),
    )
    q0 = [e for e in events if e["event"] == "question"][0]["data"]

    # Boolean question rejects non-boolean answers
    response = await answer_via_api(ds, cookies, conversation_id, q0["id"], "yes")
    assert response.status_code == 400

    # Get to the choice question
    response = await answer_via_api(ds, cookies, conversation_id, q0["id"], True)
    q1 = [e for e in _parse_sse(response.text) if e["event"] == "question"][0]["data"]

    # Choice question rejects answers not in options
    response = await answer_via_api(ds, cookies, conversation_id, q1["id"], "nope")
    assert response.status_code == 400
    # Text answer to a choice question also rejected
    response = await answer_via_api(ds, cookies, conversation_id, q1["id"], True)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_answer_permissions(datasette_instance, cookies, ask_tool_plugin):
    ds = datasette_instance
    conversation_id, question = await suspend_on_approve_edit(ds, cookies)

    # Another actor cannot answer
    other_cookies = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    response = await answer_via_api(
        ds, other_cookies, conversation_id, question["id"], True
    )
    assert response.status_code == 403

    # Unknown question 404s
    response = await answer_via_api(
        ds, cookies, conversation_id, "01UNKNOWNQUESTION000000000", True
    )
    assert response.status_code == 404

    # Answering twice fails: the first answer resolves the question
    response = await answer_via_api(ds, cookies, conversation_id, question["id"], True)
    assert response.status_code == 200
    response = await answer_via_api(ds, cookies, conversation_id, question["id"], True)
    assert response.status_code == 400


# ---- conversation page renders pending questions ----


@pytest.mark.asyncio
async def test_conversation_page_shows_pending_question(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id, question = await suspend_on_approve_edit(ds, cookies)

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="pending-question-data"' in response.text
    assert "Is it OK to edit files in /tmp?" in response.text
    assert question["id"] in response.text


@pytest.mark.asyncio
async def test_conversation_page_without_pending_question(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "hello")

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="pending-question-data"' not in response.text


@pytest.mark.asyncio
async def test_answered_question_disappears_from_page(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id, question = await suspend_on_approve_edit(ds, cookies)
    await answer_via_api(ds, cookies, conversation_id, question["id"], True)

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert 'id="pending-question-data"' not in response.text


# ---- ask_user html= parameter ----


@pytest.mark.asyncio
async def test_ask_user_html_stored_and_raised(datasette_instance):
    from datasette_agent.questions import QuestionPending

    context = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info:
        await context.ask_user("Save this query?", html="<pre>select 1 + 1</pre>")
    question = exc_info.value.question
    assert question["html"] == "<pre>select 1 + 1</pre>"
    rows = await get_questions(datasette_instance)
    assert rows[0]["html"] == "<pre>select 1 + 1</pre>"

    # Re-raise for the still-pending question keeps the html
    again = await make_context(datasette_instance)
    with pytest.raises(QuestionPending) as exc_info2:
        await again.ask_user("Save this query?", html="<pre>select 1 + 1</pre>")
    assert exc_info2.value.question["html"] == "<pre>select 1 + 1</pre>"


@pytest.mark.asyncio
async def test_question_html_reaches_sse_and_page(
    datasette_instance, cookies, ask_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {"tool_calls": [{"name": "approve_html", "arguments": {"path": "/tmp"}}]}
        ),
    )
    question = [e for e in events if e["event"] == "question"][0]["data"]
    assert question["html"] == "<pre>rm -rf /tmp</pre>"

    page = await ds.client.get("/-/agent/{}".format(conversation_id), cookies=cookies)
    # The < is unicode-escaped inside the embedded JSON
    assert "\\u003cpre>rm -rf /tmp\\u003c/pre>" in page.text
