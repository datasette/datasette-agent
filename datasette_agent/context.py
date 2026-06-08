import contextvars

current_conversation_id = contextvars.ContextVar(
    "current_conversation_id", default=None
)

# Holds the per-turn async callable used by ask_user() to put a question to
# the connected user and block until they answer. None when there is no
# interactive user attached (background agents, CLI).
current_ask_user = contextvars.ContextVar("current_ask_user", default=None)
