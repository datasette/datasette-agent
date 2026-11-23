"""Model discovery for the agent.

datasette-llm owns the policy for which models exist, which have keys
and which an administrator has allowed for the ``agent`` purpose. This
module narrows that list to models the agent can actually drive - ones
that support tool calling - and resolves the default the UI should
preselect.
"""

import llm
from datasette_llm import LLM, ModelNotFoundError

AGENT_PURPOSE = "agent"


class AgentModelUnavailable(Exception):
    """The model a conversation is pinned to can no longer be used."""


def _default_is_configured(datasette):
    config = datasette.plugin_config("datasette-llm") or {}
    purpose_config = (config.get("purposes") or {}).get(AGENT_PURPOSE) or {}
    return bool(purpose_config.get("model") or config.get("default_model"))


async def get_agent_model(datasette, actor, pinned_model_id=None):
    """Return the datasette-llm model an agent turn should run on.

    ``pinned_model_id`` is the model a conversation is pinned to, if it
    has one yet. datasette-llm checks a model requested by ID against
    its allowlist, blocklist and keys, so a pinned model that has since
    been taken away raises AgentModelUnavailable instead of carrying on.

    Without a pinned model this is the datasette-llm default. If no
    default is configured it is the first model on offer, the one the
    new-chat picker preselects, so background agents and
    ``datasette agent chat`` work without a default as the web chat
    does. A default that is configured but unusable is never replaced
    by another model: that raises, as a mistake to be fixed.
    """
    llm_instance = LLM(datasette)
    if pinned_model_id is not None:
        try:
            return await llm_instance.model(
                model_id=pinned_model_id, purpose=AGENT_PURPOSE, actor=actor
            )
        except (ModelNotFoundError, llm.NeedsKeyException) as ex:
            raise AgentModelUnavailable(
                f"This conversation uses the model {pinned_model_id}, which is "
                "no longer available. Start a new chat to use a different model."
            ) from ex
    try:
        return await llm_instance.model(purpose=AGENT_PURPOSE, actor=actor)
    except ModelNotFoundError:
        if _default_is_configured(datasette):
            raise
        model_ids, _ = await get_agent_models(datasette, actor)
        if not model_ids:
            raise
        return await llm_instance.model(
            model_id=model_ids[0], purpose=AGENT_PURPOSE, actor=actor
        )


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
        default_model = await llm.model(purpose=AGENT_PURPOSE, actor=actor)
        default_model_id = default_model.model_id
    except Exception:
        default_model_id = None
    return model_ids, default_model_id
