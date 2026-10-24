"""Model discovery for the agent.

datasette-llm owns the policy for which models exist, which have keys
and which an administrator has allowed for the ``agent`` purpose. This
module narrows that list to models the agent can actually drive - ones
that support tool calling - and resolves the default the UI should
preselect.
"""

from datasette_llm import LLM, ModelNotFoundError

AGENT_PURPOSE = "agent"


class AgentModelUnavailable(Exception):
    """The model an agent turn would run on is not on offer to the actor."""


async def _default_model_id(llm, actor):
    """The datasette-llm default for the agent purpose, or None when no
    default is configured. A configured default that cannot be resolved
    raises."""
    try:
        model = await llm.model(purpose=AGENT_PURPOSE, actor=actor)
    except ModelNotFoundError:
        return None
    return model.model_id


async def get_agent_models(datasette, actor):
    """Return ``(model_ids, default_model_id)`` for the agent purpose.

    ``model_ids`` lists the models this actor may start a conversation
    with, in the order datasette-llm ranks them: the configured default
    first, then any purpose-specific allowlist in config order, then
    everything else. Models that cannot call tools are excluded - the
    agent is nothing but tool calls, so offering one would only produce
    an error on the first turn.

    ``default_model_id`` is the model datasette-llm picks when no
    explicit model is given, or None when nothing is configured or the
    configured default cannot be resolved. It is informational: a broken
    default surfaces as an error on the first turn, exactly as before.
    """
    llm = LLM(datasette)
    models = await llm.models(actor=actor, purpose=AGENT_PURPOSE)
    model_ids = [m.model_id for m in models if getattr(m, "supports_tools", False)]
    try:
        default_model_id = await _default_model_id(llm, actor)
    except Exception:
        default_model_id = None
    return model_ids, default_model_id


async def resolve_agent_model_id(datasette, actor, model_id=None):
    """Return the id of the model an agent turn should run on.

    ``model_id`` is the model a conversation is pinned to, if any.
    Without one the datasette-llm default is used or, when no default
    is configured, the first model on offer - the one the new-chat
    picker preselects - so background agents and explorer reports work
    without a default too. A configured default is never silently
    swapped for another model.

    Raises AgentModelUnavailable unless the result is on offer to this
    actor right now, so a pinned model that has since been blocked,
    dropped from the allowlist or lost its key stops being used.
    """
    model_ids, _ = await get_agent_models(datasette, actor)
    pinned = model_id is not None
    if model_id is None:
        model_id = await _default_model_id(LLM(datasette), actor)
    if model_id is None:
        if not model_ids:
            raise AgentModelUnavailable("No models are available for the agent")
        model_id = model_ids[0]
    if model_id not in model_ids:
        if pinned:
            raise AgentModelUnavailable(
                "Model {} is no longer available - start a new chat to use "
                "a different model".format(model_id)
            )
        raise AgentModelUnavailable("Model not available: {}".format(model_id))
    return model_id
