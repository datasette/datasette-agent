import json
from datetime import datetime, timedelta, timezone

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
        tool_name="run_in_browser",
        arguments={"script": "1 + 1"},
        tool_call_id="call_1",
        supports_browser_tasks=True,
    )
    defaults.update(kwargs)
    return ToolContext(**defaults)


async def get_tasks(datasette):
    db = datasette.get_internal_database()
    return [
        dict(r)
        for r in (
            await db.execute("SELECT * FROM agent_browser_tasks ORDER BY created_at")
        ).rows
    ]


async def complete_task_in_db(datasette, task_id, envelope):
    """Simulate a browser completing a task, without the HTTP endpoint."""
    db = datasette.get_internal_database()
    stored = dict(envelope)
    stored.setdefault("outcome", "completed")
    await db.execute_write(
        "UPDATE agent_browser_tasks SET status = 'completed', result_json = ? "
        "WHERE id = ?",
        [json.dumps(stored), task_id],
    )


async def backdate_task(datasette, task_id, seconds):
    db = datasette.get_internal_database()
    created = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    await db.execute_write(
        "UPDATE agent_browser_tasks SET created_at = ? WHERE id = ?",
        [created, task_id],
    )


# ---- context.browser_task() unit behavior ----


@pytest.mark.asyncio
async def test_browser_task_records_pending_and_raises(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task(
            "<script>run()</script>",
            payload={"token": "secret-token"},
            label="Running debug script",
            timeout_ms=5000,
        )

    task = exc_info.value.task
    assert task["conversation_id"] == "01TESTCONVERSATION0000000"
    assert task["tool_name"] == "run_in_browser"
    assert task["label"] == "Running debug script"
    assert task["html"] == "<script>run()</script>"
    assert task["timeout_ms"] == 5000
    assert task["task_index"] == 0
    # The payload is delivered through the one-shot claim, never on the
    # task dict that travels to the frontend.
    assert "payload" not in task
    assert "payload_json" not in task

    rows = await get_tasks(datasette_instance)
    assert len(rows) == 1
    assert rows[0]["id"] == task["id"]
    assert rows[0]["status"] == "pending"
    assert rows[0]["task_index"] == 0
    assert json.loads(rows[0]["payload_json"]) == {"token": "secret-token"}
    assert rows[0]["timeout_ms"] == 5000
    assert rows[0]["created_at"]


@pytest.mark.asyncio
async def test_browser_task_re_raise_does_not_duplicate_row(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending):
        await context.browser_task("<p>work</p>", timeout_ms=5000)

    again = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await again.browser_task("<p>work</p>", timeout_ms=5000)

    rows = await get_tasks(datasette_instance)
    assert len(rows) == 1
    assert exc_info.value.task["id"] == rows[0]["id"]


@pytest.mark.asyncio
async def test_browser_task_replay_returns_result(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>work</p>")
    await complete_task_in_db(
        datasette_instance,
        exc_info.value.task["id"],
        {"ok": True, "result": {"answer": 2}},
    )

    replay_context = await make_context(datasette_instance)
    outcome = await replay_context.browser_task("<p>work</p>")
    assert outcome == {"ok": True, "result": {"answer": 2}, "outcome": "completed"}
    # No new task row was created
    assert len(await get_tasks(datasette_instance)) == 1


@pytest.mark.asyncio
async def test_browser_task_sequential_tasks_replay_in_order(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>first</p>")
    await complete_task_in_db(
        datasette_instance, exc_info.value.task["id"], {"ok": True, "result": 1}
    )

    context2 = await make_context(datasette_instance)
    first = await context2.browser_task("<p>first</p>")
    assert first["result"] == 1
    with pytest.raises(BrowserTaskPending) as exc_info2:
        await context2.browser_task("<p>second</p>")
    assert exc_info2.value.task["task_index"] == 1
    await complete_task_in_db(
        datasette_instance, exc_info2.value.task["id"], {"ok": True, "result": 2}
    )

    context3 = await make_context(datasette_instance)
    assert (await context3.browser_task("<p>first</p>"))["result"] == 1
    assert (await context3.browser_task("<p>second</p>"))["result"] == 2


@pytest.mark.asyncio
async def test_consumed_tasks_do_not_replay(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>work</p>")
    await complete_task_in_db(
        datasette_instance, exc_info.value.task["id"], {"ok": True, "result": 1}
    )

    done_context = await make_context(datasette_instance)
    assert (await done_context.browser_task("<p>work</p>"))["ok"] is True
    await done_context.mark_browser_tasks_consumed()

    rows = await get_tasks(datasette_instance)
    assert rows[0]["status"] == "consumed"

    # A later identical tool call must run fresh, not replay the stale result
    later_context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending):
        await later_context.browser_task("<p>work</p>")


@pytest.mark.asyncio
async def test_different_call_keys_do_not_share_results(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>work</p>")
    await complete_task_in_db(
        datasette_instance, exc_info.value.task["id"], {"ok": True, "result": 1}
    )

    other_call = await make_context(datasette_instance, tool_call_id="call_2")
    with pytest.raises(BrowserTaskPending):
        await other_call.browser_task("<p>work</p>")


@pytest.mark.asyncio
async def test_browser_task_unsupported_context(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTasksNotSupported

    context = await make_context(datasette_instance, supports_browser_tasks=False)
    assert context.supports_browser_tasks is False
    with pytest.raises(BrowserTasksNotSupported):
        await context.browser_task("<p>work</p>")
    # No orphaned task row
    assert await get_tasks(datasette_instance) == []


@pytest.mark.asyncio
async def test_browser_task_callback(datasette_instance):
    captured = {}

    def callback(task):
        captured.update(task)
        return {"ok": True, "result": "from-callback"}

    context = await make_context(
        datasette_instance,
        supports_browser_tasks=False,
        browser_task_callback=callback,
    )
    outcome = await context.browser_task(
        "<p>work</p>", payload={"x": 1}, label="Working", timeout_ms=5000
    )
    assert outcome == {"ok": True, "result": "from-callback"}
    assert captured["tool_name"] == "run_in_browser"
    assert captured["html"] == "<p>work</p>"
    assert captured["payload"] == {"x": 1}
    assert captured["label"] == "Working"
    assert captured["timeout_ms"] == 5000
    # The callback satisfied the task synchronously - no row persisted
    assert await get_tasks(datasette_instance) == []


@pytest.mark.asyncio
async def test_browser_task_async_callback(datasette_instance):
    async def callback(task):
        return {"ok": False, "error": {"message": "nope"}}

    context = await make_context(datasette_instance, browser_task_callback=callback)
    outcome = await context.browser_task("<p>work</p>")
    assert outcome == {"ok": False, "error": {"message": "nope"}}


@pytest.mark.asyncio
async def test_browser_task_timeout_is_capped(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending, MAX_TIMEOUT_MS

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>work</p>", timeout_ms=99_999_999)
    assert exc_info.value.task["timeout_ms"] == MAX_TIMEOUT_MS
    rows = await get_tasks(datasette_instance)
    assert rows[0]["timeout_ms"] == MAX_TIMEOUT_MS


@pytest.mark.asyncio
async def test_browser_task_overdue_pending_expires_on_reexecute(datasette_instance):
    from datasette_agent.browser_tasks import BrowserTaskPending

    context = await make_context(datasette_instance)
    with pytest.raises(BrowserTaskPending) as exc_info:
        await context.browser_task("<p>work</p>", timeout_ms=1000)
    await backdate_task(datasette_instance, exc_info.value.task["id"], seconds=60)

    replay_context = await make_context(datasette_instance)
    outcome = await replay_context.browser_task("<p>work</p>", timeout_ms=1000)
    assert outcome["ok"] is False
    assert outcome["outcome"] == "expired"
    assert "error" in outcome

    rows = await get_tasks(datasette_instance)
    assert rows[0]["status"] == "expired"


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
def browser_tool_plugin():
    from datasette import hookimpl
    from datasette.plugins import pm
    from datasette_agent.tools import AgentTool

    class BrowserToolPlugin:
        __name__ = "BrowserToolPlugin"

        @hookimpl
        def register_agent_tools(self, datasette):
            async def run_in_browser(datasette, actor, context, script):
                outcome = await context.browser_task(
                    "<div id='harness'><script>execute()</script></div>",
                    payload={"script": script, "secret": "payload-secret"},
                    label="Running {} in your browser".format(script),
                    timeout_ms=5000,
                )
                return json.dumps(outcome)

            async def no_tasks(datasette, actor, path):
                return json.dumps({"looked_at": path})

            return [
                AgentTool(
                    name="run_in_browser",
                    description="Run a script in the user's browser",
                    input_schema={
                        "type": "object",
                        "properties": {"script": {"type": "string"}},
                        "required": ["script"],
                    },
                    fn=run_in_browser,
                ),
                AgentTool(
                    name="no_tasks",
                    description="Look at files",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    fn=no_tasks,
                ),
            ]

    plugin = BrowserToolPlugin()
    pm.register(plugin, name="BrowserToolPlugin")
    try:
        yield plugin
    finally:
        pm.unregister(name="BrowserToolPlugin")


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


async def suspend_on_browser_task(datasette, cookies, script="1 + 1"):
    conversation_id = await start_conversation(datasette, cookies)
    events = await send_message(
        datasette,
        cookies,
        conversation_id,
        json.dumps(
            {
                "tool_calls": [
                    {"name": "run_in_browser", "arguments": {"script": script}}
                ]
            }
        ),
    )
    task = [e for e in events if e["event"] == "browser_task"][0]["data"]
    return conversation_id, task


async def claim_via_api(datasette, cookies, conversation_id, task_id):
    return await datasette.client.post(
        "/-/agent/{}/task/{}/claim".format(conversation_id, task_id),
        content=json.dumps({}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )


async def complete_via_api(datasette, cookies, conversation_id, task_id, envelope):
    return await datasette.client.post(
        "/-/agent/{}/task/{}/complete".format(conversation_id, task_id),
        content=json.dumps(envelope),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )


async def cancel_via_api(datasette, cookies, conversation_id, task_id):
    return await datasette.client.post(
        "/-/agent/{}/task/{}/cancel".format(conversation_id, task_id),
        content=json.dumps({}),
        headers={"Content-Type": "application/json"},
        cookies=cookies,
    )


@pytest.mark.asyncio
async def test_browser_task_suspends_turn(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)

    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {
                "tool_calls": [
                    {"name": "run_in_browser", "arguments": {"script": "1 + 1"}}
                ]
            }
        ),
    )
    types = [e["event"] for e in events]

    task_events = [e for e in events if e["event"] == "browser_task"]
    assert len(task_events) == 1
    task = task_events[0]["data"]
    assert task["tool_name"] == "run_in_browser"
    assert task["label"] == "Running 1 + 1 in your browser"
    assert task["html"] == "<div id='harness'><script>execute()</script></div>"
    assert task["timeout_ms"] == 5000
    assert "id" in task
    # Secrets ride the one-shot claim, never the SSE event
    assert "payload-secret" not in json.dumps(task)
    assert "tool_result" not in types
    assert {"event": "done", "data": {"browser_task_pending": True}} in events
    assert "error" not in types

    # DB state: task row pending, no tool_result row for the suspended call
    db = ds.get_internal_database()
    t_rows = (await db.execute("SELECT * FROM agent_browser_tasks")).rows
    assert len(t_rows) == 1
    assert t_rows[0]["status"] == "pending"
    assert t_rows[0]["id"] == task["id"]

    messages = (
        await db.execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    assert "tool" not in [m["role"] for m in messages]


@pytest.mark.asyncio
async def test_sibling_tool_results_survive_suspension(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    events = await send_message(
        ds,
        cookies,
        conversation_id,
        json.dumps(
            {
                "tool_calls": [
                    {"name": "run_in_browser", "arguments": {"script": "1 + 1"}},
                    {"name": "no_tasks", "arguments": {"path": "/etc"}},
                ]
            }
        ),
    )
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "no_tasks"
    assert [e for e in events if e["event"] == "browser_task"]


# ---- claim endpoint ----


@pytest.mark.asyncio
async def test_claim_returns_payload_exactly_once(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    response = await claim_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["task"]["id"] == task["id"]
    assert data["task"]["payload"] == {"script": "1 + 1", "secret": "payload-secret"}
    assert data["task"]["timeout_ms"] == 5000

    db = ds.get_internal_database()
    row = (
        await db.execute("SELECT * FROM agent_browser_tasks WHERE id = ?", [task["id"]])
    ).first()
    assert row["status"] == "running"
    assert row["claimed_at"]

    # Second claim (history replay, duplicate tab) stands down
    response = await claim_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert data["state"] == "running"
    assert "task" not in data


@pytest.mark.asyncio
async def test_claim_permissions(datasette_instance, cookies, browser_tool_plugin):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    other_cookies = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    response = await claim_via_api(ds, other_cookies, conversation_id, task["id"])
    assert response.status_code == 403

    response = await claim_via_api(
        ds, cookies, conversation_id, "01UNKNOWNTASK0000000000000"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_claim_past_deadline_expires(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await backdate_task(ds, task["id"], seconds=60)

    response = await claim_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert data["state"] == "expired"

    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT status FROM agent_browser_tasks WHERE id = ?", [task["id"]]
        )
    ).first()
    assert row["status"] == "expired"


# ---- complete endpoint + resume ----


@pytest.mark.asyncio
async def test_complete_resumes_tool_and_continues_chain(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await claim_via_api(ds, cookies, conversation_id, task["id"])

    response = await complete_via_api(
        ds,
        cookies,
        conversation_id,
        task["id"],
        {"ok": True, "result": {"answer": 2}},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    events = _parse_sse(response.text)
    types = [e["event"] for e in events]

    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "run_in_browser"
    assert json.loads(tool_results[0]["data"]["output"]) == {
        "ok": True,
        "result": {"answer": 2},
        "outcome": "completed",
    }
    assert types[-1] == "done"
    assert events[-1]["data"] == {}
    assert "error" not in types

    db = ds.get_internal_database()
    row = (
        await db.execute("SELECT * FROM agent_browser_tasks WHERE id = ?", [task["id"]])
    ).first()
    # The tool call completed, so its tasks were marked consumed
    assert row["status"] == "consumed"
    assert json.loads(row["result_json"]) == {
        "ok": True,
        "result": {"answer": 2},
        "outcome": "completed",
    }
    assert row["completed_by"] == "user"
    assert row["completed_at"]

    # Chain continued: assistant message follows the tool result
    messages = (
        await db.execute(
            "SELECT role FROM agent_messages " "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    roles = [m["role"] for m in messages]
    assert "tool" in roles
    assert roles[-1] == "assistant"


@pytest.mark.asyncio
async def test_complete_error_envelope_reaches_tool(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await claim_via_api(ds, cookies, conversation_id, task["id"])

    response = await complete_via_api(
        ds,
        cookies,
        conversation_id,
        task["id"],
        {"ok": False, "error": {"message": "script threw"}},
    )
    events = _parse_sse(response.text)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    output = json.loads(tool_results[0]["data"]["output"])
    assert output["ok"] is False
    assert output["error"] == {"message": "script threw"}
    assert output["outcome"] == "completed"


@pytest.mark.asyncio
async def test_complete_without_claim_allowed(
    datasette_instance, cookies, browser_tool_plugin
):
    # Hosts that skip claiming (callback executors) complete from pending
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert json.loads(tool_results[0]["data"]["output"])["result"] == 1


@pytest.mark.asyncio
async def test_complete_first_write_wins(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    assert response.status_code == 200

    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 2}
    )
    assert response.status_code == 400

    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT result_json FROM agent_browser_tasks WHERE id = ?", [task["id"]]
        )
    ).first()
    assert json.loads(row["result_json"])["result"] == 1


@pytest.mark.asyncio
async def test_complete_validates_body(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    # Body must be a JSON object with a boolean "ok"
    response = await complete_via_api(ds, cookies, conversation_id, task["id"], [1, 2])
    assert response.status_code == 400
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"result": 1}
    )
    assert response.status_code == 400

    # The task is still completable after the rejected posts
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_complete_oversized_result_rejected(
    datasette_instance, cookies, browser_tool_plugin
):
    from datasette_agent.browser_tasks import MAX_RESULT_BYTES

    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    big = "x" * (MAX_RESULT_BYTES + 1)
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": big}
    )
    assert response.status_code == 400
    data = response.json()
    assert data["ok"] is False
    assert "error" in data

    # Task unchanged - the page can post a trimmed result
    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": "trimmed"}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_complete_past_deadline_expires_and_rejects(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await backdate_task(ds, task["id"], seconds=60)

    response = await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    assert response.status_code == 400
    assert response.json()["state"] == "expired"

    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT status FROM agent_browser_tasks WHERE id = ?", [task["id"]]
        )
    ).first()
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_complete_permissions(datasette_instance, cookies, browser_tool_plugin):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    other_cookies = {"ds_actor": ds.client.actor_cookie({"id": "other"})}
    response = await complete_via_api(
        ds, other_cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    assert response.status_code == 403

    response = await complete_via_api(
        ds, cookies, conversation_id, "01UNKNOWNTASK0000000000000", {"ok": True}
    )
    assert response.status_code == 404


# ---- cancel endpoint ----


@pytest.mark.asyncio
async def test_cancel_resumes_with_cancelled_envelope(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    response = await cancel_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    events = _parse_sse(response.text)
    tool_results = [e for e in events if e["event"] == "tool_result"]
    output = json.loads(tool_results[0]["data"]["output"])
    assert output["ok"] is False
    assert output["outcome"] == "cancelled"
    assert output["error"]["message"] == "Cancelled by the user"


@pytest.mark.asyncio
async def test_cancel_running_task(datasette_instance, cookies, browser_tool_plugin):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await claim_via_api(ds, cookies, conversation_id, task["id"])

    response = await cancel_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 200
    events = _parse_sse(response.text)
    output = json.loads(
        [e for e in events if e["event"] == "tool_result"][0]["data"]["output"]
    )
    assert output["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_task_rejected(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )
    response = await cancel_via_api(ds, cookies, conversation_id, task["id"])
    assert response.status_code == 400


# ---- conversation page ----


@pytest.mark.asyncio
async def test_conversation_page_embeds_pending_tasks(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="pending-tasks-data"' in response.text
    assert task["id"] in response.text
    # The payload never appears in the page - it rides the one-shot claim
    assert "payload-secret" not in response.text


@pytest.mark.asyncio
async def test_conversation_page_without_pending_tasks(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id = await start_conversation(ds, cookies)
    await send_message(ds, cookies, conversation_id, "hello")

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="pending-tasks-data"' not in response.text


@pytest.mark.asyncio
async def test_completed_task_html_not_replayed_on_page(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await complete_via_api(
        ds, cookies, conversation_id, task["id"], {"ok": True, "result": 1}
    )

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert 'id="pending-tasks-data"' not in response.text
    # The stored task html is never rendered again once the task has
    # left pending
    assert "<script>execute()</script>" not in response.text


@pytest.mark.asyncio
async def test_conversation_page_converts_overdue_tasks(
    datasette_instance, cookies, browser_tool_plugin
):
    ds = datasette_instance
    conversation_id, task = await suspend_on_browser_task(ds, cookies)
    await backdate_task(ds, task["id"], seconds=60)

    response = await ds.client.get(
        "/-/agent/{}".format(conversation_id), cookies=cookies
    )
    assert response.status_code == 200
    assert 'id="pending-tasks-data"' not in response.text

    db = ds.get_internal_database()
    row = (
        await db.execute(
            "SELECT status FROM agent_browser_tasks WHERE id = ?", [task["id"]]
        )
    ).first()
    assert row["status"] == "expired"


# ---- context injection defaults ----


@pytest.mark.asyncio
async def test_context_defaults_to_no_browser_task_support(datasette_instance):
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
    assert captured["context"].supports_browser_tasks is False
