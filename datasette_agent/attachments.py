"""File attachments on chat messages.

Attached files live in datasette-files, private to the user who attached
them. A persisted user message only references each file, by ID, on an
attachment part; the bytes are read back through datasette-files - with
its permission check, for the actor running the turn - each time the
history is loaded for the model.

datasette-files is an optional dependency: without it, or without the
``attachments_source`` plugin setting, attachments are switched off.
"""

import llm

# Extra keys on a persisted attachment part. llm ignores them.
FILE_ID_KEY = "agent_file_id"
FILENAME_KEY = "agent_filename"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 10
NOT_ENABLED_MESSAGE = "Attachments are not enabled"


class AttachmentError(Exception):
    """An attachment request that cannot be honored. The message is shown
    to the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def files_api():
    """The datasette_files module, or None if it is not installed or is
    too old to have the API attachments need."""
    try:
        import datasette_files
    except ImportError:
        return None
    if not all(
        hasattr(datasette_files, name) for name in ("create_file", "get_file_for_actor")
    ):
        return None
    return datasette_files


def _plugin_config(datasette):
    return datasette.plugin_config("datasette-agent") or {}


def attachments_source(datasette):
    """Slug of the datasette-files source attachments are stored in, or
    None if attachments are switched off."""
    source = _plugin_config(datasette).get("attachments_source")
    if not source or files_api() is None:
        return None
    return source


def attachments_max_bytes(datasette):
    value = _plugin_config(datasette).get("attachments_max_bytes")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def model_attachment_types(datasette, model_id):
    """Sorted MIME types this model accepts as attachments. Empty if it
    accepts none, or if attachments are switched off."""
    if not model_id or attachments_source(datasette) is None:
        return []
    try:
        model = llm.get_async_model(model_id)
    except llm.UnknownModelError:
        return []
    return sorted(getattr(model, "attachment_types", None) or [])


def format_bytes(size):
    if size >= 1024 * 1024:
        return "{:.1f} MB".format(size / (1024 * 1024)).replace(".0 MB", " MB")
    if size >= 1024:
        return "{:.0f} KB".format(size / 1024)
    return "{} bytes".format(size)


def unsupported_type_message(filename, content_type, model_id):
    return "{} is {}, which {} does not accept as an attachment".format(
        filename, content_type or "an unknown type", model_id
    )


def too_large_message(datasette):
    return "File is too large: the limit is {}".format(
        format_bytes(attachments_max_bytes(datasette))
    )


def file_url(datasette, file_id):
    return datasette.urls.path("/-/files/{}".format(file_id))


def thumbnail_url(datasette, file_id):
    return datasette.urls.path("/-/files/{}/thumbnail".format(file_id))


def file_to_json(datasette, file):
    return {
        "id": file.id,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
        "url": file_url(datasette, file.id),
        "thumbnail_url": thumbnail_url(datasette, file.id),
    }


def make_attachment_part(file):
    """The part persisted on a user message for an attached file. It
    names the file and never carries its bytes."""
    return {
        "type": "attachment",
        "attachment": {"type": file.content_type},
        FILE_ID_KEY: file.id,
        FILENAME_KEY: file.filename,
    }


async def store_attachment(datasette, actor_id, filename, content, content_type):
    """Store an uploaded attachment as a private file owned by the actor."""
    source = attachments_source(datasette)
    if source is None:
        raise AttachmentError(NOT_ENABLED_MESSAGE)
    try:
        return await files_api().create_file(
            datasette,
            source=source,
            filename=filename,
            content=content,
            content_type=content_type,
            owner_id=actor_id,
            state="private",
        )
    except ValueError as e:
        # The configured source does not exist or cannot take uploads
        raise AttachmentError("Attachments are misconfigured: {}".format(e), status=500)


async def resolve_attachments(datasette, actor, model_id, file_ids):
    """Check the files a message wants to attach and return their parts.

    Any file the actor can view may be attached - not only ones uploaded
    through the chat - provided the conversation's model accepts its
    type. Raises AttachmentError if any of them cannot be.
    """
    if not isinstance(file_ids, list) or not all(
        isinstance(file_id, str) for file_id in file_ids
    ):
        raise AttachmentError("attachments must be a list of file IDs")
    if not file_ids:
        return []
    if len(file_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise AttachmentError(
            "A message can have at most {} attachments".format(
                MAX_ATTACHMENTS_PER_MESSAGE
            )
        )
    if attachments_source(datasette) is None:
        raise AttachmentError(NOT_ENABLED_MESSAGE)
    types = model_attachment_types(datasette, model_id)
    if not types:
        raise AttachmentError("{} does not accept attachments".format(model_id))

    max_bytes = attachments_max_bytes(datasette)
    parts = []
    for file_id in file_ids:
        file = await files_api().get_file_for_actor(datasette, actor, file_id)
        if file is None:
            # Missing and not-viewable look the same, so IDs can't be probed
            raise AttachmentError("Attachment not found: {}".format(file_id))
        if file.content_type not in types:
            raise AttachmentError(
                unsupported_type_message(file.filename, file.content_type, model_id)
            )
        if not file.size:
            raise AttachmentError("{} is empty".format(file.filename))
        if file.size > max_bytes:
            raise AttachmentError(
                "{} is too large to attach: the limit is {}".format(
                    file.filename, format_bytes(max_bytes)
                )
            )
        parts.append(make_attachment_part(file))
    return parts


def unavailable_note(part):
    # Adapters join a message's text parts with no separator, so start
    # the note on its own paragraph.
    return "\n\n[Attachment {} is no longer available]".format(
        part.get(FILENAME_KEY) or part.get(FILE_ID_KEY)
    )


async def load_attachment_part(datasette, actor, part):
    """Turn a persisted attachment part into the llm part to send to the
    model: the file's bytes if this actor can still view the file,
    otherwise a note saying it is gone."""
    file = None
    api = files_api() if datasette is not None else None
    if api is not None:
        file = await api.get_file_for_actor(datasette, actor, part[FILE_ID_KEY])
    if file is None:
        return llm.parts.TextPart(text=unavailable_note(part))
    return llm.parts.AttachmentPart(
        attachment=llm.Attachment(
            type=(part.get("attachment") or {}).get("type") or file.content_type,
            content=await file.read(),
        )
    )


def add_attachment_urls(datasette, messages, absolute=None):
    """Add url and thumbnail_url to each attachment on flattened render
    messages. absolute, if given, turns a path into a full URL."""
    for message in messages:
        for attachment in message.get("attachments") or []:
            url = file_url(datasette, attachment["file_id"])
            thumbnail = thumbnail_url(datasette, attachment["file_id"])
            if absolute is not None:
                url = absolute(url)
                thumbnail = absolute(thumbnail)
            attachment["url"] = url
            attachment["thumbnail_url"] = thumbnail
    return messages
