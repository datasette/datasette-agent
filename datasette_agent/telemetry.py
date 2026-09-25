"""OpenTelemetry for datasette-agent: one span per agent turn, API-only.

llm and datasette-llm supply the model call and tool call spans beneath it.
Without an OpenTelemetry SDK installed every span is a no-op.
"""

import asyncio
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

try:
    _version = version("datasette-agent")
except PackageNotFoundError:  # pragma: no cover
    _version = None

tracer = trace.get_tracer("datasette_agent", _version)

AGENT_NAME = "datasette-agent"
SPAN_TURN = "invoke_agent " + AGENT_NAME
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
ERROR_TYPE = "error.type"
# chat | resume
MODE = "datasette_agent.mode"
# completed | question | browser_task | error | cancelled
OUTCOME = "datasette_agent.outcome"
# Cancellation is a designed way for a turn to end: status stays UNSET
_CANCELLED = (asyncio.CancelledError, KeyboardInterrupt)


class _Turn:
    def __init__(self, span):
        self.span = span

    def set_outcome(self, outcome):
        self.span.set_attribute(OUTCOME, outcome)


@contextmanager
def turn_span(mode, conversation_id):
    """Current ``invoke_agent datasette-agent`` span around one agent turn.

    Records no message text, actor ids or error messages: an exception sets
    ``error.type`` to its class name. Cancellation is an outcome, not an error.
    """
    with tracer.start_as_current_span(
        SPAN_TURN,
        kind=SpanKind.INTERNAL,
        attributes={
            GEN_AI_OPERATION_NAME: "invoke_agent",
            GEN_AI_AGENT_NAME: AGENT_NAME,
            GEN_AI_CONVERSATION_ID: conversation_id,
            MODE: mode,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        turn = _Turn(span)
        try:
            yield turn
        except _CANCELLED:
            turn.set_outcome("cancelled")
            raise
        except Exception as ex:
            turn.set_outcome("error")
            span.set_attribute(ERROR_TYPE, type(ex).__qualname__)
            span.set_status(Status(StatusCode.ERROR))
            raise
