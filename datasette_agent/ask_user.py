"""Blocking ask-the-user helper for agent tools.

A tool implementation can call::

    from datasette_agent.ask_user import ask_user

    choice = await ask_user(
        "Which environment should I deploy to?",
        ["production", "staging"],
    )

to pause the agent turn and present the user with a multiple-choice
question. The call blocks until the user picks one of the offered options,
then returns the chosen label as a string.

This works by holding the chat SSE stream open while we wait: the question
is emitted as a ``user_question`` event, an ``asyncio.Future`` is parked in
a per-Datasette registry, and the answer endpoint resolves it once the user
clicks an option. Because there is no connected user in non-interactive
contexts (background agents, the CLI), ``ask_user`` raises
``AskUserUnavailable`` there.
"""

import asyncio

from ulid import ULID

from .context import current_ask_user


class AskUserUnavailable(RuntimeError):
    "Raised when ask_user is called outside an interactive chat turn."


def normalize_options(options):
    """Coerce an options list into ``[{"label": ..., "description"?: ...}]``.

    Accepts plain strings or ``{"label", "description"}`` dicts. Raises
    ``ValueError`` for anything else or an empty list.
    """
    normalized = []
    for option in options:
        if isinstance(option, str):
            normalized.append({"label": option})
        elif isinstance(option, dict):
            if "label" not in option:
                raise ValueError("Each option dict must have a 'label' key")
            item = {"label": str(option["label"])}
            description = option.get("description")
            if description:
                item["description"] = str(description)
            normalized.append(item)
        else:
            raise ValueError(
                "Options must be strings or {'label', 'description'} dicts"
            )
    if not normalized:
        raise ValueError("ask_user requires at least one option")
    return normalized


async def ask_user(question, options):
    """Ask the user a multiple-choice question and block until they answer.

    ``question`` is the prompt text. ``options`` is a list of choice labels
    (strings) or ``{"label", "description"}`` dicts. Returns the label of
    the option the user selected.

    Raises ``AskUserUnavailable`` when called outside an interactive chat
    turn, and ``ValueError`` for a malformed ``options`` list.
    """
    ask = current_ask_user.get()
    if ask is None:
        raise AskUserUnavailable(
            "ask_user is only available inside an interactive chat turn"
        )
    return await ask(str(question), normalize_options(options))


def pending_questions(datasette):
    """Return the per-Datasette registry of unanswered questions.

    Maps question_id -> {future, conversation_id, actor_id, labels}.
    """
    registry = getattr(datasette, "_agent_pending_questions", None)
    if registry is None:
        registry = {}
        datasette._agent_pending_questions = registry
    return registry


def make_ask_user(datasette, conversation_id, actor_id, send_question):
    """Build the per-turn ask_user callable bound to one open stream.

    ``send_question(payload)`` is an async function that delivers the
    question to the connected client (in practice, an SSE ``user_question``
    event). The returned callable matches the signature expected by the
    ``current_ask_user`` contextvar.
    """
    registry = pending_questions(datasette)

    async def _ask(question, options):
        question_id = str(ULID())
        future = asyncio.get_running_loop().create_future()
        registry[question_id] = {
            "future": future,
            "conversation_id": conversation_id,
            "actor_id": actor_id,
            "labels": [option["label"] for option in options],
        }
        await send_question(
            {"id": question_id, "question": question, "options": options}
        )
        try:
            return await future
        finally:
            # Whether answered, cancelled, or the connection dropped, never
            # leave a stale entry behind for the answer endpoint to hit.
            registry.pop(question_id, None)

    return _ask


def resolve_question(datasette, conversation_id, actor_id, question_id, answer):
    """Resolve a pending question with the user's selected answer.

    Returns ``(ok, error_message)``. Validates that the question exists,
    belongs to this conversation and actor, and that ``answer`` is one of
    the offered option labels.
    """
    registry = pending_questions(datasette)
    entry = registry.get(question_id)
    if entry is None:
        return False, "Question not found or already answered"
    if entry["conversation_id"] != conversation_id:
        return False, "Question does not belong to this conversation"
    if entry["actor_id"] != actor_id:
        return False, "Forbidden"
    if answer not in entry["labels"]:
        return False, "Answer is not one of the offered options"
    future = entry["future"]
    if future.done():
        return False, "Question already answered"
    future.set_result(answer)
    return True, None
