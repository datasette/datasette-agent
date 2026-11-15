"""File attachments on chat messages, stored in datasette-files."""

import base64
import hashlib
import json

from datasette.app import Datasette
import llm
import llm_echo
import pytest

from datasette_agent.attachments import FILE_ID_KEY, FILENAME_KEY
from datasette_agent.export_markdown import format_conversation_markdown
from datasette_agent.messages import (
    flatten_for_render,
    load_messages,
    make_user_message_dict,
)

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)
PNG = base64.b64decode(PNG_BASE64)
ECHO_TYPES = ["image/gif", "image/jpeg", "image/png"]
SOURCE = "agent-attachments"


def make_datasette(tmp_path, agent_config=None, permissions=None):
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    if agent_config is None:
        agent_config = {"attachments_source": SOURCE}
    return Datasette(
        memory=True,
        config={
            "plugins": {
                "datasette-llm": {"default_model": "echo", "models": ["echo"]},
                "datasette-agent": agent_config,
                "datasette-files": {
                    "sources": {
                        SOURCE: {
                            "storage": "filesystem",
                            "config": {"root": str(uploads)},
                            "audience": "collaborators",
                        }
                    }
                },
            },
            "permissions": permissions
            or {"datasette-agent": {"id": ["user", "other"]}},
        },
        internal=str(tmp_path / "internal.db"),
    )


@pytest.fixture
def ds(tmp_path):
    return make_datasette(tmp_path)


def cookies_for(ds, actor_id="user"):
    return {"ds_actor": ds.client.actor_cookie({"id": actor_id})}


async def upload(ds, actor_id="user", content=PNG, content_type="image/png", **kwargs):
    return await ds.client.post(
        "/-/agent/api/attachments",
        files={"file": (kwargs.pop("filename", "shot.png"), content, content_type)},
        data={"model_id": kwargs.pop("model_id", "echo")},
        cookies=kwargs.pop("cookies", None) or cookies_for(ds, actor_id),
    )


async def upload_id(ds, actor_id="user"):
    response = await upload(ds, actor_id)
    assert response.status_code == 200, response.text
    return response.json()["file"]["id"]


async def create_conversation(ds, actor_id="user"):
    response = await ds.client.post(
        "/-/agent/api/conversations", cookies=cookies_for(ds, actor_id)
    )
    return response.json()["conversation_id"]


async def send(ds, conversation_id, message, attachments=None, actor_id="user"):
    body = {"message": message}
    if attachments is not None:
        body["attachments"] = attachments
    return await ds.client.post(
        f"/-/agent/{conversation_id}/stream",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
        cookies=cookies_for(ds, actor_id),
    )


async def stored_messages(ds, conversation_id, role=None):
    rows = (
        await ds.get_internal_database().execute(
            "SELECT role, message_json FROM agent_messages "
            "WHERE conversation_id = ? ORDER BY id",
            [conversation_id],
        )
    ).rows
    return [
        json.loads(row["message_json"])
        for row in rows
        if role is None or row["role"] == role
    ]


async def echoed(ds, conversation_id):
    "What the echo model reported it was given, one dict per response."
    return [
        json.loads(message["parts"][-1]["text"])
        for message in await stored_messages(ds, conversation_id, "assistant")
    ]


@pytest.fixture
def prompts(monkeypatch):
    """The message chain of every prompt the echo model executes - what
    an adapter that builds its request from prompt.messages would send."""
    seen = []
    original = llm_echo._Shared.shared

    def shared(self, prompt, stream, response, conversation):
        seen.append(list(prompt.messages))
        return original(self, prompt, stream, response, conversation)

    monkeypatch.setattr(llm_echo._Shared, "shared", shared)
    return seen


def attachment_parts(messages):
    return [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, llm.parts.AttachmentPart)
    ]


def text_of(messages):
    return "".join(
        part.text
        for message in messages
        for part in message.parts
        if isinstance(part, llm.parts.TextPart)
    )


# Model info


@pytest.mark.asyncio
async def test_models_api_lists_attachment_types(ds):
    response = await ds.client.get("/-/agent/api/models", cookies=cookies_for(ds))
    assert response.json() == {
        "models": ["echo"],
        "default_model": "echo",
        "attachment_types": {"echo": ECHO_TYPES},
    }


@pytest.mark.asyncio
async def test_models_api_no_attachment_types_unless_configured(tmp_path):
    ds = make_datasette(tmp_path, agent_config={})
    response = await ds.client.get("/-/agent/api/models", cookies=cookies_for(ds))
    assert response.json()["attachment_types"] == {"echo": []}


@pytest.mark.asyncio
async def test_models_api_no_attachment_types_without_datasette_files(ds, monkeypatch):
    monkeypatch.setattr("datasette_agent.attachments.files_api", lambda: None)
    response = await ds.client.get("/-/agent/api/models", cookies=cookies_for(ds))
    assert response.json()["attachment_types"] == {"echo": []}


# Upload endpoint


@pytest.mark.asyncio
async def test_upload_stores_private_file_owned_by_actor(ds):
    from datasette_files import get_file_for_actor

    response = await upload(ds)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    file_id = data["file"]["id"]
    assert file_id.startswith("df-")
    assert data["file"] == {
        "id": file_id,
        "filename": "shot.png",
        "content_type": "image/png",
        "size": len(PNG),
        "url": f"/-/files/{file_id}",
        "thumbnail_url": f"/-/files/{file_id}/thumbnail",
    }

    file = await get_file_for_actor(ds, {"id": "user"}, file_id)
    assert file.owner_id == "user"
    assert file.state == "private"
    assert file.source_slug == SOURCE
    assert await file.read() == PNG
    assert await get_file_for_actor(ds, {"id": "other"}, file_id) is None


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_type(ds):
    response = await upload(
        ds, content=b"hello", content_type="text/plain", filename="notes.txt"
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "text/plain" in response.json()["error"]


@pytest.mark.asyncio
async def test_upload_rejects_file_over_limit(tmp_path):
    ds = make_datasette(
        tmp_path,
        agent_config={"attachments_source": SOURCE, "attachments_max_bytes": 50},
    )
    response = await upload(ds)
    assert len(PNG) > 50
    assert response.status_code == 413
    assert response.json()["ok"] is False
    assert "too large" in response.json()["error"]


@pytest.mark.asyncio
async def test_upload_requires_agent_permission(ds):
    response = await upload(ds, cookies={"x": "logged-out"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_upload_requires_signed_in_actor(tmp_path):
    # Even where logged-out visitors may use the agent
    ds = make_datasette(tmp_path, permissions={"datasette-agent": True})
    response = await upload(ds, cookies={"x": "logged-out"})
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "Sign in to attach files"}


@pytest.mark.asyncio
async def test_upload_when_attachments_not_configured(tmp_path):
    ds = make_datasette(tmp_path, agent_config={})
    response = await upload(ds)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Attachments are not enabled"}


@pytest.mark.asyncio
async def test_upload_without_datasette_files(ds, monkeypatch):
    monkeypatch.setattr("datasette_agent.attachments.files_api", lambda: None)
    response = await upload(ds)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Attachments are not enabled"}


@pytest.mark.asyncio
async def test_upload_rejects_model_the_actor_cannot_use(ds):
    response = await upload(ds, model_id="echo-needs-key")
    assert response.status_code == 400
    assert "echo-needs-key" in response.json()["error"]


@pytest.mark.asyncio
async def test_upload_requires_post_and_a_file(ds):
    response = await ds.client.get("/-/agent/api/attachments", cookies=cookies_for(ds))
    assert response.status_code == 405
    response = await ds.client.post(
        "/-/agent/api/attachments",
        data={"model_id": "echo"},
        cookies=cookies_for(ds),
    )
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "No file provided"}


# Sending


@pytest.mark.asyncio
async def test_attachment_reaches_the_model(ds, prompts):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "What is this?", [file_id])
    assert response.status_code == 200
    assert "event: done" in response.text

    # The echo model reports the attachments it was given
    (echo,) = await echoed(ds, conversation_id)
    assert echo["prompt"] == "What is this?"
    assert echo["attachments"] == [
        {
            "type": "image/png",
            "path": None,
            "url": None,
            "id": hashlib.sha256(PNG).hexdigest(),
        }
    ]

    # The message chain carries the file exactly once
    (messages,) = prompts
    (part,) = attachment_parts(messages)
    assert part.attachment.type == "image/png"
    assert part.attachment.content == PNG


@pytest.mark.asyncio
async def test_stored_message_references_file_without_its_bytes(ds):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])

    (user_message,) = await stored_messages(ds, conversation_id, "user")
    assert user_message["parts"] == [
        {"type": "text", "text": "What is this?"},
        {
            "type": "attachment",
            "attachment": {"type": "image/png"},
            FILE_ID_KEY: file_id,
            FILENAME_KEY: "shot.png",
        },
    ]

    rows = (
        await ds.get_internal_database().execute(
            "SELECT message_json FROM agent_messages WHERE conversation_id = ?",
            [conversation_id],
        )
    ).rows
    assert len(rows) == 2
    for row in rows:
        assert PNG_BASE64 not in row["message_json"]
        assert PNG_BASE64[:40] not in row["message_json"]


@pytest.mark.asyncio
async def test_another_actors_private_file_is_rejected(ds):
    file_id = await upload_id(ds, "other")
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "What is this?", [file_id])
    assert response.status_code == 400
    assert response.json() == {"error": f"Attachment not found: {file_id}"}
    assert await stored_messages(ds, conversation_id) == []


@pytest.mark.asyncio
async def test_missing_file_is_rejected(ds):
    conversation_id = await create_conversation(ds)
    file_id = "df-" + "0" * 26
    response = await send(ds, conversation_id, "What is this?", [file_id])
    assert response.status_code == 400
    assert response.json() == {"error": f"Attachment not found: {file_id}"}
    assert await stored_messages(ds, conversation_id) == []


@pytest.mark.asyncio
async def test_existing_file_of_supported_type_can_be_attached(ds):
    "Any file the actor can view may be attached, not only chat uploads."
    from datasette_files import create_file

    await ds.invoke_startup()
    file = await create_file(
        ds,
        source=SOURCE,
        filename="existing.png",
        content=PNG,
        content_type="image/png",
        owner_id="user",
        state="private",
    )
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "And this?", [file.id])
    assert response.status_code == 200
    (echo,) = await echoed(ds, conversation_id)
    assert [a["type"] for a in echo["attachments"]] == ["image/png"]


@pytest.mark.asyncio
async def test_file_of_unsupported_type_is_rejected(ds):
    from datasette_files import create_file

    await ds.invoke_startup()
    file = await create_file(
        ds,
        source=SOURCE,
        filename="notes.txt",
        content=b"hello",
        content_type="text/plain",
        owner_id="user",
        state="private",
    )
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "Read this", [file.id])
    assert response.status_code == 400
    assert "text/plain" in response.json()["error"]
    assert "notes.txt" in response.json()["error"]
    assert await stored_messages(ds, conversation_id) == []


@pytest.mark.asyncio
async def test_attachments_rejected_when_not_configured(tmp_path):
    ds = make_datasette(tmp_path, agent_config={})
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "Hi", ["df-" + "0" * 26])
    assert response.status_code == 400
    assert response.json() == {"error": "Attachments are not enabled"}
    assert await stored_messages(ds, conversation_id) == []


@pytest.mark.parametrize("attachments", ["df-abc", [1], [{"id": "df-abc"}], ["x"] * 11])
@pytest.mark.asyncio
async def test_malformed_attachments_are_rejected(ds, attachments):
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "Hi", attachments)
    assert response.status_code == 400
    assert await stored_messages(ds, conversation_id) == []


@pytest.mark.asyncio
async def test_message_without_attachments_is_unchanged(ds, prompts):
    conversation_id = await create_conversation(ds)
    response = await send(ds, conversation_id, "Hi", [])
    assert response.status_code == 200
    (user_message,) = await stored_messages(ds, conversation_id, "user")
    assert user_message["parts"] == [{"type": "text", "text": "Hi"}]
    assert attachment_parts(prompts[0]) == []


# History


@pytest.mark.asyncio
async def test_history_is_hydrated_for_later_turns(ds, prompts):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])
    await send(ds, conversation_id, "Are you sure?")

    # Not a new attachment on the second turn...
    assert (await echoed(ds, conversation_id))[1]["attachments"] == []
    # ...but still in the history the model is sent, exactly once
    second = prompts[1]
    (part,) = attachment_parts(second)
    assert part.attachment.type == "image/png"
    assert part.attachment.content == PNG
    assert second[1].role == "user"
    assert part in second[1].parts


@pytest.mark.asyncio
async def test_load_messages_hydrates_for_actor_who_can_view(ds):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])
    db = ds.get_internal_database()

    messages = await load_messages(
        db, conversation_id, datasette=ds, actor={"id": "user"}
    )
    (part,) = attachment_parts(messages)
    assert part.attachment.content == PNG

    # Never for an actor who cannot view the file
    for kwargs in (
        {"datasette": ds, "actor": {"id": "other"}},
        {"datasette": ds, "actor": None},
        {},
    ):
        messages = await load_messages(db, conversation_id, **kwargs)
        assert attachment_parts(messages) == []
        assert "[Attachment shot.png is no longer available]" in text_of(messages)


@pytest.mark.asyncio
async def test_deleted_attachment_falls_back_to_a_note(ds, prompts):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])
    await ds.get_internal_database().execute_write(
        "DELETE FROM datasette_files WHERE id = ?", [file_id]
    )

    response = await send(ds, conversation_id, "Are you sure?")
    assert response.status_code == 200
    assert "event: done" in response.text
    assert "event: error" not in response.text
    assert len(await stored_messages(ds, conversation_id, "assistant")) == 2
    second = prompts[1]
    assert attachment_parts(second) == []
    assert "[Attachment shot.png is no longer available]" in text_of(second)


@pytest.mark.asyncio
async def test_history_survives_datasette_files_being_removed(ds, prompts, monkeypatch):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])
    monkeypatch.setattr("datasette_agent.attachments.files_api", lambda: None)

    response = await send(ds, conversation_id, "Are you sure?")
    assert "event: done" in response.text
    assert attachment_parts(prompts[1]) == []
    assert "[Attachment shot.png is no longer available]" in text_of(prompts[1])


# Rendering


def test_make_user_message_dict_with_attachments():
    part = {
        "type": "attachment",
        "attachment": {"type": "image/png"},
        FILE_ID_KEY: "df-abc",
        FILENAME_KEY: "shot.png",
    }
    message = make_user_message_dict(
        "Look",
        references=[{"type": "table", "id": "data/t", "name": "t"}],
        references_context="Tagged: t",
        attachments=[part],
    )
    assert [p["type"] for p in message["parts"]] == ["text", "attachment", "text"]
    assert message["parts"][1] == part


def test_flatten_for_render_surfaces_attachments():
    message = {
        "role": "user",
        "parts": [
            {"type": "text", "text": "Look"},
            {
                "type": "attachment",
                "attachment": {"type": "image/png"},
                FILE_ID_KEY: "df-abc",
                FILENAME_KEY: "shot.png",
            },
            {
                "type": "attachment",
                "attachment": {"type": "image/jpeg"},
                FILE_ID_KEY: "df-def",
                FILENAME_KEY: "photo.jpg",
            },
        ],
    }
    (rendered,) = flatten_for_render([{"message_json": json.dumps(message)}])
    assert rendered["role"] == "user"
    assert rendered["content"] == "Look"
    assert rendered["attachments"] == [
        {"file_id": "df-abc", "filename": "shot.png", "content_type": "image/png"},
        {"file_id": "df-def", "filename": "photo.jpg", "content_type": "image/jpeg"},
    ]


@pytest.mark.asyncio
async def test_conversation_page_shows_attachment_thumbnails(ds):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])

    response = await ds.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies_for(ds)
    )
    assert response.status_code == 200
    html = response.text
    assert 'class="agent-message-attachments"' in html
    assert f'href="/-/files/{file_id}"' in html
    assert f'src="/-/files/{file_id}/thumbnail"' in html
    assert 'alt="shot.png"' in html
    assert PNG_BASE64[:40] not in html

    # datasette-files serves the thumbnail to the owner only
    thumbnail = await ds.client.get(
        f"/-/files/{file_id}/thumbnail", cookies=cookies_for(ds)
    )
    assert thumbnail.status_code == 200
    denied = await ds.client.get(
        f"/-/files/{file_id}/thumbnail", cookies=cookies_for(ds, "other")
    )
    # datasette-files says the file exists but is not shared with them
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_markdown_export_links_attachments(ds):
    file_id = await upload_id(ds)
    conversation_id = await create_conversation(ds)
    await send(ds, conversation_id, "What is this?", [file_id])

    response = await ds.client.get(
        f"/-/agent/{conversation_id}/markdown", cookies=cookies_for(ds)
    )
    assert response.status_code == 200
    assert (
        f"Attachments: [shot.png](http://localhost/-/files/{file_id})" in response.text
    )
    assert PNG_BASE64[:40] not in response.text


def test_format_conversation_markdown_attachments():
    markdown = format_conversation_markdown(
        "Chat",
        [
            {
                "role": "user",
                "content": "Look",
                "tool_name": None,
                "tool_arguments": None,
                "tool_output": None,
                "attachments": [
                    {"filename": "shot.png", "url": "/-/files/df-abc"},
                    {"filename": "a [b].png", "url": "/-/files/df-def"},
                ],
            }
        ],
    )
    assert markdown == (
        "# Chat\n\n## User\n\nLook\n\n"
        "Attachments: [shot.png](/-/files/df-abc), "
        "[a \\[b\\].png](/-/files/df-def)\n"
    )


# Composer


@pytest.mark.asyncio
async def test_conversation_page_composer_offers_attachments(ds):
    conversation_id = await create_conversation(ds)
    response = await ds.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies_for(ds)
    )
    html = response.text
    assert 'id="attach-btn"' in html
    assert 'data-attachment-upload-url="/-/agent/api/attachments"' in html
    assert 'data-attachment-model="echo"' in html
    assert 'data-attachment-types=\'["image/gif", "image/jpeg", "image/png"]\'' in (
        html
    )


@pytest.mark.asyncio
async def test_conversation_page_composer_without_attachments(tmp_path):
    ds = make_datasette(tmp_path, agent_config={})
    conversation_id = await create_conversation(ds)
    response = await ds.client.get(
        f"/-/agent/{conversation_id}", cookies=cookies_for(ds)
    )
    assert response.status_code == 200
    assert 'id="attach-btn"' not in response.text
    assert "data-attachment-upload-url" not in response.text


@pytest.mark.asyncio
async def test_index_page_composer_offers_attachments(ds):
    response = await ds.client.get("/-/agent", cookies=cookies_for(ds))
    html = response.text
    assert 'id="attach-btn"' in html
    assert 'data-attachment-upload-url="/-/agent/api/attachments"' in html
    assert '"echo": ["image/gif", "image/jpeg", "image/png"]' in html


@pytest.mark.asyncio
async def test_index_page_composer_without_attachments(tmp_path):
    ds = make_datasette(tmp_path, agent_config={})
    response = await ds.client.get("/-/agent", cookies=cookies_for(ds))
    assert response.status_code == 200
    assert 'id="attach-btn"' not in response.text
    assert "data-attachment-upload-url" not in response.text
