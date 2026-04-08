import contextvars

current_conversation_id = contextvars.ContextVar(
    "current_conversation_id", default=None
)
