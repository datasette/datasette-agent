"""Model discovery for the agent.

datasette-llm owns the policy for which models exist, which have keys
and which an administrator has allowed for the ``agent`` purpose. This
module narrows that list to models the agent can actually drive - ones
that support tool calling - and resolves the default the UI should
preselect.
"""

from datasette_llm import LLM

AGENT_PURPOSE = "agent"


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
