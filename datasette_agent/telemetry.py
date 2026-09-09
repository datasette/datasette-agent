"""OpenTelemetry integration for datasette-agent.

Depends on ``opentelemetry-api`` only, like Datasette core: this module
never creates a ``TracerProvider`` or ``MeterProvider``, never configures
an exporter, and must never import ``opentelemetry.sdk`` (a test imports
the package in a fresh subprocess and checks). With no provider installed
every span is a ``NonRecordingSpan`` and every instrument is a no-op -
turning telemetry on is the operator's move, exactly as with core.

The tracer and meter live under their own ``datasette_agent``
instrumentation scope, versioned with the plugin, never core's
``datasette`` scope - a consumer filters and versions the two libraries
independently. Context propagation is via contextvars, so parenting works
across scopes: this plugin's spans nest inside core's request span and
core's ``db.query`` spans nest inside this plugin's tool spans.

Instruments are module-level: OpenTelemetry's ``_ProxyMeter`` forwards to a
provider installed later. The ``ProxyTracer`` resolves and **caches** a
concrete tracer on first use, so the test harness installs its provider in
a session-scoped autouse fixture before any span is created; under an
operator's provider (``opentelemetry-instrument``, a viewer plugin) the
provider exists before this module is imported.

For root-with-link spans (a background agent caused by a request but not
contained by it) use ``datasette.telemetry.linked_root_span_kwargs()``;
there is deliberately no local helper.
"""

import asyncio
import json
import time
from contextlib import contextmanager

import llm

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import __version__
from .telemetry_registry import (
    BACKGROUND_ID,
    BACKGROUND_ITERATION,
    BACKGROUND_ITERATION_SPAN,
    BACKGROUND_ITERATIONS,
    BACKGROUND_MAX_ITERATIONS,
    BACKGROUND_SPAWNED_FROM_CONVERSATION,
    M_BACKGROUND_ITERATIONS,
    EXECUTE_TOOL,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_TYPE,
    M_TOOL_DURATION,
    M_TOOL_OUTPUT_TRUNCATED,
    SQL_DISPLAY,
    TOOL_OUTCOME,
    TOOL_OUTPUT_BYTES,
    TOOL_PLUGIN,
    TOOL_SUSPENDED_ON,
    CHAIN_INDEX,
    CHAIN_STEPS,
    CHAT,
    DATABASES,
    ERROR_TYPE,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_TOKEN_TYPE,
    GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    HISTORY_MESSAGES,
    INVOKE_AGENT,
    M_CHAIN_STEPS,
    M_OPERATION_DURATION,
    M_TIME_TO_FIRST_TOKEN,
    M_TOKEN_USAGE,
    M_TURN_DURATION,
    M_TURNS_ACTIVE,
    MODE,
    NOTIFICATIONS_DRAINED,
    OUTCOME,
    PROMPT_CHARS,
    STREAMING,
    SYSTEM_PROMPT,
    TOOL_CALLS,
    TOOL_CALLS_REQUESTED,
)

# No schema URL. Core pins 1.29.0 for its own db.* spellings, but the GenAI
# conventions this plugin emits moved out of the main semantic-conventions
# repository after 1.44 and are versioned separately, so there is no
# schema file that accurately describes exactly these spellings. The
# plugin telemetry docs' advice applies: a wrong schema URL is worse than
# none. The spellings themselves are the current ones (gen_ai.provider.name
# from 1.37, gen_ai.usage.cache_read.input_tokens from 1.40).
tracer = otel_trace.get_tracer("datasette_agent", __version__)
meter = otel_metrics.get_meter("datasette_agent", __version__)


def clamp(value, allowed, default):
    """Clamp an attribute value to its registry enum.

    Enum attributes declare a closed ``values=`` set in the registry and the
    conformance helpers enforce membership; this is the call-site half of
    that promise - anything unexpected becomes ``default`` instead of
    minting a new metric series.
    """
    return value if value in allowed else default


# --- Instruments ----------------------------------------------------------
#
# Each instrument passes the SDK a short plain-text description; the
# registry entry carries the longer Markdown documentation for the
# generated reference. Unit and buckets always come off the registry entry
# so the meter.create_*() call cannot drift from it (the kit's
# assert_metrics_conform checks kind and unit against the registry).


def _histogram(entry, description):
    return meter.create_histogram(
        entry,
        unit=entry.unit,
        description=description,
        explicit_bucket_boundaries_advisory=entry.buckets,
    )


def _counter(entry, description):
    return meter.create_counter(entry, unit=entry.unit, description=description)


def _up_down_counter(entry, description):
    return meter.create_up_down_counter(entry, unit=entry.unit, description=description)


# --- Instruments ----------------------------------------------------------

token_usage = _histogram(M_TOKEN_USAGE, "Tokens per model response, by token type")
operation_duration = _histogram(
    M_OPERATION_DURATION, "Duration of one model response, streaming window included"
)
time_to_first_token = _histogram(
    M_TIME_TO_FIRST_TOKEN, "Seconds until the first streamed chunk of a model response"
)
turn_duration = _histogram(
    M_TURN_DURATION, "Duration of one agent turn, by mode and outcome"
)
chain_steps = _histogram(M_CHAIN_STEPS, "Model round-trips per agent turn")
turns_active = _up_down_counter(M_TURNS_ACTIVE, "Agent turns in flight, by mode")
tool_duration = _histogram(M_TOOL_DURATION, "Duration of one tool call, by outcome")
background_iterations = _histogram(
    M_BACKGROUND_ITERATIONS, "Loop passes per background agent run, by outcome"
)
tool_output_truncated = _counter(
    M_TOOL_OUTPUT_TRUNCATED, "Tool outputs cut down before the model saw them"
)


# --- Providers ------------------------------------------------------------

# Semconv well-known provider names for llm plugins whose package name
# differs from the semconv spelling. Anything else reports the plugin's
# name (llm_foo -> foo): still bounded by what is installed.
_PROVIDER_ALIASES = {
    "gemini": "gcp.gemini",
    "vertex": "gcp.vertex_ai",
    "mistral": "mistral_ai",
    "grok": "x_ai",
    "bedrock": "aws.bedrock",
    "azure": "azure.ai.openai",
    "watsonx": "ibm.watsonx.ai",
}
UNKNOWN_MODEL = "unknown"


def provider_name_for(model):
    """``gen_ai.provider.name`` for an llm model (or datasette-llm's wrapper
    around one), derived from the module that implements it. Never
    consults the model id, which is user-configured text."""
    inner = getattr(model, "_model", model)
    module = type(inner).__module__ or ""
    if module.startswith("llm.default_plugins.openai"):
        return "openai"
    top = module.split(".", 1)[0]
    name = top[4:] if top.startswith("llm_") else top
    name = name.replace("-", "_")
    return _PROVIDER_ALIASES.get(name, name) or "unknown"


def _model_id(model):
    return getattr(model, "model_id", None) or UNKNOWN_MODEL


# --- Turns ----------------------------------------------------------------


class TurnRecorder:
    """What one ``invoke_agent`` span learns as the turn progresses.

    Handed out by ``turn_span()``; call sites report the model once it is
    resolved, the history size, each chain step and completed tool call,
    and the outcome. ``fail()`` classifies an exception the call site
    caught itself (``run_agent`` turns exceptions into an SSE event rather
    than letting them propagate).
    """

    def __init__(self, span, mode):
        self.span = span
        self.mode = mode
        self.model_id = UNKNOWN_MODEL
        self.outcome = "done"
        self.chain_steps = 0
        self.tool_calls = 0
        self.error_type = None
        self.iterations = None

    def set_background(self, agent_id, *, max_iterations, spawned_from_conversation):
        "Mark the turn as a background run (the whole run is one turn)."
        if self.span.is_recording():
            self.span.set_attribute(BACKGROUND_ID, agent_id)
            self.span.set_attribute(BACKGROUND_MAX_ITERATIONS, max_iterations)
            self.span.set_attribute(
                BACKGROUND_SPAWNED_FROM_CONVERSATION, bool(spawned_from_conversation)
            )

    def set_iterations(self, iterations):
        self.iterations = iterations

    def set_model(self, model):
        self.model_id = _model_id(model)

    def set_history(self, messages):
        if self.span.is_recording():
            self.span.set_attribute(HISTORY_MESSAGES, messages)

    def set_notifications_drained(self, count):
        if self.span.is_recording():
            self.span.set_attribute(NOTIFICATIONS_DRAINED, count)

    def step(self):
        self.chain_steps += 1

    def tool_call(self):
        self.tool_calls += 1

    def set_outcome(self, outcome):
        self.outcome = outcome

    def fail(self, exception):
        "Classify an exception that ended the turn."
        if isinstance(exception, ValueError) and str(exception).startswith(
            "Chain limit of"
        ):
            # llm raises a bare ValueError here (a typed exception is a
            # pending upstream ask); the message prefix is its contract.
            self.outcome = "chain_limit"
        elif isinstance(exception, asyncio.CancelledError):
            self.outcome = "cancelled"
        else:
            self.outcome = "error"
        self.error_type = type(exception).__qualname__


@contextmanager
def turn_span(*, mode, conversation_id, **span_kwargs):
    """``invoke_agent datasette-agent`` around one turn, plus
    ``turns.active``, ``turn.duration`` and ``chain.steps``.

    Yields a ``TurnRecorder``. Every exit path stamps the outcome and
    counts on the span and records the histograms; only ``error``,
    ``chain_limit`` and ``max_iterations`` set span status ``ERROR`` - a
    suspension or a cancellation is not a failure of the agent.

    ``span_kwargs`` go to ``start_as_current_span``: a background run
    passes ``datasette.telemetry.linked_root_span_kwargs()`` to become a
    root with a link instead of a child of the request that started it.
    """
    mode = clamp(mode, MODE.values, "chat")
    started = time.perf_counter()
    turns_active.add(1, {MODE: mode})
    # record_exception / set_status_on_exception are off: the outcome
    # classification below decides what is an error, and a CancelledError
    # (a BaseException the SDK would not handle anyway) is not one.
    with tracer.start_as_current_span(
        INVOKE_AGENT,
        record_exception=False,
        set_status_on_exception=False,
        **span_kwargs,
    ) as span:
        recorder = TurnRecorder(span, mode)
        if span.is_recording():
            span.set_attribute(GEN_AI_OPERATION_NAME, "invoke_agent")
            span.set_attribute(GEN_AI_AGENT_NAME, "datasette-agent")
            span.set_attribute(GEN_AI_CONVERSATION_ID, conversation_id)
            span.set_attribute(MODE, mode)
        try:
            yield recorder
        except BaseException as exception:
            recorder.fail(exception)
            raise
        finally:
            outcome = clamp(recorder.outcome, OUTCOME.values, "error")
            if span.is_recording():
                span.set_attribute(GEN_AI_REQUEST_MODEL, recorder.model_id)
                span.set_attribute(OUTCOME, outcome)
                span.set_attribute(CHAIN_STEPS, recorder.chain_steps)
                span.set_attribute(TOOL_CALLS, recorder.tool_calls)
                if recorder.error_type is not None:
                    span.set_attribute(ERROR_TYPE, recorder.error_type)
                if recorder.iterations is not None:
                    span.set_attribute(BACKGROUND_ITERATIONS, recorder.iterations)
                if outcome in ("error", "chain_limit", "max_iterations"):
                    span.set_status(Status(StatusCode.ERROR))
            if recorder.iterations is not None:
                background_iterations.record(
                    recorder.iterations, {MODE: mode, OUTCOME: outcome}
                )
            elapsed = time.perf_counter() - started
            attributes = {
                MODE: mode,
                OUTCOME: outcome,
                GEN_AI_REQUEST_MODEL: recorder.model_id,
            }
            if recorder.error_type is not None:
                attributes[ERROR_TYPE] = recorder.error_type
            turn_duration.record(elapsed, attributes)
            chain_steps.record(
                recorder.chain_steps,
                {MODE: mode, GEN_AI_REQUEST_MODEL: recorder.model_id},
            )
            turns_active.add(-1, {MODE: mode})


@contextmanager
def background_iteration_span(iteration):
    "``datasette_agent.background.iteration`` around one loop pass."
    with tracer.start_as_current_span(BACKGROUND_ITERATION_SPAN) as span:
        if span.is_recording():
            span.set_attribute(BACKGROUND_ITERATION, iteration)
        yield


# --- Model calls ----------------------------------------------------------


def _cache_tokens(details):
    """Normalise provider-specific prompt-cache counts out of
    ``Usage.details``: Anthropic's two flat keys, OpenAI's nested
    ``prompt_tokens_details.cached_tokens``. Unknown keys are dropped."""
    if not isinstance(details, dict):
        return None, None
    read = details.get("cache_read_input_tokens")
    creation = details.get("cache_creation_input_tokens")
    nested = details.get("prompt_tokens_details")
    if read is None and isinstance(nested, dict):
        read = nested.get("cached_tokens")
    return (
        read if isinstance(read, int) and not isinstance(read, bool) else None,
        creation
        if isinstance(creation, int) and not isinstance(creation, bool)
        else None,
    )


async def _maybe_await(accessor):
    """Call an llm response accessor (``usage``, ``tool_calls``) that is a
    coroutine method on ``AsyncResponse`` and a plain method on
    ``Response``, tolerating duck-typed responses that lack it."""
    if accessor is None:
        return None
    result = accessor()
    if asyncio.iscoroutine(result):
        result = await result
    return result


class ChatRecorder:
    """What one ``chat`` span learns: the first-token instant and, once
    the response is forced, its usage and tool calls."""

    def __init__(self, span, started, attributes, streaming):
        self.span = span
        self.started = started
        self.attributes = attributes
        self.streaming = streaming
        self.first_token_at = None

    def first_token(self):
        "Call on every streamed chunk; only the first one counts."
        if self.first_token_at is not None:
            return
        self.first_token_at = time.perf_counter()
        self.span.add_event("gen_ai.first_token")
        if self.streaming:
            time_to_first_token.record(
                self.first_token_at - self.started,
                {
                    GEN_AI_PROVIDER_NAME: self.attributes[GEN_AI_PROVIDER_NAME],
                    GEN_AI_REQUEST_MODEL: self.attributes[GEN_AI_REQUEST_MODEL],
                },
            )

    async def finish(self, response):
        """Read usage and tool calls off a completed llm ``AsyncResponse``.
        Call after the response has been fully iterated - the accessors
        force it otherwise, which would block the stream."""
        usage = await _maybe_await(getattr(response, "usage", None))
        tool_calls = await _maybe_await(getattr(response, "tool_calls", None))
        if self.span.is_recording():
            self.span.set_attribute(TOOL_CALLS_REQUESTED, len(tool_calls or ()))
            resolved = getattr(response, "resolved_model", None)
            if resolved and resolved != self.attributes[GEN_AI_REQUEST_MODEL]:
                self.span.set_attribute(GEN_AI_RESPONSE_MODEL, resolved)
            response_json = getattr(response, "response_json", None)
            response_id = (
                response_json.get("id") if isinstance(response_json, dict) else None
            )
            if isinstance(response_id, str) and 0 < len(response_id) <= 128:
                self.span.set_attribute(GEN_AI_RESPONSE_ID, response_id)
        cache_read, cache_creation = _cache_tokens(getattr(usage, "details", None))
        counts = (
            (GEN_AI_USAGE_INPUT_TOKENS, "input", getattr(usage, "input", None)),
            (GEN_AI_USAGE_OUTPUT_TOKENS, "output", getattr(usage, "output", None)),
            (GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, "cache_read", cache_read),
            (
                GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
                "cache_creation",
                cache_creation,
            ),
        )
        for attribute, token_type, count in counts:
            if count is None:
                continue
            if self.span.is_recording():
                self.span.set_attribute(attribute, count)
            token_usage.record(
                count, {**self.attributes, GEN_AI_TOKEN_TYPE: token_type}
            )


@contextmanager
def chat_span(model, *, chain_index, streaming):
    """``chat {model}`` around one model response - wrap the iteration of
    llm's lazy response object, which is what makes the HTTP call - plus
    ``gen_ai.client.operation.duration``. Yields a ``ChatRecorder``: call
    ``first_token()`` on each streamed chunk and ``await finish(response)``
    after the loop."""
    model_id = _model_id(model)
    attributes = {
        GEN_AI_OPERATION_NAME: "chat",
        GEN_AI_PROVIDER_NAME: provider_name_for(model),
        GEN_AI_REQUEST_MODEL: model_id,
    }
    started = time.perf_counter()
    error_type = None
    with tracer.start_as_current_span(
        f"{CHAT}{model_id}", kind=SpanKind.CLIENT
    ) as span:
        if span.is_recording():
            span.set_attributes(attributes)
            span.set_attribute(CHAIN_INDEX, chain_index)
            span.set_attribute(STREAMING, streaming)
        try:
            yield ChatRecorder(span, started, attributes, streaming)
        except BaseException as exception:
            # A failed model call is an error, unlike a failed turn. The
            # SDK records Exception subclasses itself; CancelledError is a
            # BaseException it ignores, and the trace should still show
            # the call did not finish.
            error_type = type(exception).__qualname__
            if span.is_recording():
                span.set_attribute(ERROR_TYPE, error_type)
                span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            duration_attributes = dict(attributes)
            if error_type is not None:
                duration_attributes[ERROR_TYPE] = error_type
            operation_duration.record(
                time.perf_counter() - started, duration_attributes
            )


# --- System prompt --------------------------------------------------------


class SystemPromptRecorder:
    def __init__(self, span):
        self.span = span

    def finish(self, *, databases, prompt_chars):
        if self.span.is_recording():
            self.span.set_attribute(DATABASES, databases)
            self.span.set_attribute(PROMPT_CHARS, prompt_chars)


@contextmanager
def system_prompt_span():
    "``datasette_agent.system_prompt`` around ``_build_system_prompt``."
    with tracer.start_as_current_span(SYSTEM_PROMPT) as span:
        yield SystemPromptRecorder(span)


# --- Tools --------------------------------------------------------------------

UNKNOWN_PLUGIN = "unknown"


def classify_tool_payload(result):
    """The ``datasette_agent.tool.outcome`` for a tool's returned string.

    Tools in this repo report failures as data: ``{"error": "Permission
    denied"}``, ``{"error": "Database 'x' not found"}``, ``{"ok": false,
    "error": ...}``. Matched on prefix, never recorded. Anything that is
    not a JSON object with a top-level ``error`` key is ``ok``.
    """
    if not result or result[0] != "{" or '"error"' not in result:
        return "ok"
    try:
        parsed = json.loads(result)
    except ValueError:
        return "ok"
    if not isinstance(parsed, dict) or "error" not in parsed:
        return "ok"
    message = parsed["error"]
    if isinstance(message, str):
        if message.startswith("Permission denied"):
            return "permission_denied"
        if message.startswith(("Database '", "Table '")) and " not found" in message:
            return "not_found"
    return "error"


class ToolRecorder:
    """What one ``execute_tool`` span learns: the returned output's size
    and classification."""

    def __init__(self, span):
        self.span = span
        self.outcome = "ok"
        self.error_type = None
        self.suspended_on = None

    def output(self, result):
        "Report the tool's returned string (after coercion)."
        if result is None:
            return
        self.outcome = classify_tool_payload(result)
        if self.span.is_recording():
            self.span.set_attribute(TOOL_OUTPUT_BYTES, len(result))

    def _pause(self, exception):
        self.outcome = "suspended"
        # QuestionPending carries .question, BrowserTaskPending .task;
        # matched by attribute so this module does not import either.
        if hasattr(exception, "question"):
            self.suspended_on = "question"
        elif hasattr(exception, "task"):
            self.suspended_on = "browser_task"
        if self.span.is_recording() and self.suspended_on is not None:
            self.span.set_attribute(TOOL_SUSPENDED_ON, self.suspended_on)

    def _fail(self, exception):
        self.outcome = "error"
        self.error_type = type(exception).__qualname__
        if self.span.is_recording():
            self.span.set_attribute(ERROR_TYPE, self.error_type)
            self.span.set_status(Status(StatusCode.ERROR))


@contextmanager
def tool_span(agent_tool, *, tool_call_id=None, arguments=None):
    """``execute_tool {tool}`` around one tool invocation, plus
    ``tool.duration``. Yields a ``ToolRecorder``: call ``output(result)``
    with the coerced return value. A raised ``llm.PauseChain`` is a
    suspension (status ``UNSET``); any other exception is an error."""
    name = agent_tool.name
    plugin = getattr(agent_tool, "plugin_name", None) or UNKNOWN_PLUGIN
    started = time.perf_counter()
    with tracer.start_as_current_span(
        f"{EXECUTE_TOOL}{name}", record_exception=False, set_status_on_exception=False
    ) as span:
        recorder = ToolRecorder(span)
        if span.is_recording():
            span.set_attribute(GEN_AI_OPERATION_NAME, "execute_tool")
            span.set_attribute(GEN_AI_TOOL_NAME, name)
            span.set_attribute(GEN_AI_TOOL_TYPE, "function")
            span.set_attribute(TOOL_PLUGIN, plugin)
            if tool_call_id:
                span.set_attribute(GEN_AI_TOOL_CALL_ID, str(tool_call_id))
            if name == "sql_query" and isinstance(arguments, dict):
                span.set_attribute(
                    SQL_DISPLAY,
                    clamp(
                        arguments.get("display", "model"), SQL_DISPLAY.values, "model"
                    ),
                )
        try:
            yield recorder
        except llm.PauseChain as exception:
            recorder._pause(exception)
            raise
        except BaseException as exception:
            recorder._fail(exception)
            raise
        finally:
            outcome = clamp(recorder.outcome, TOOL_OUTCOME.values, "error")
            if span.is_recording():
                span.set_attribute(TOOL_OUTCOME, outcome)
            tool_duration.record(
                time.perf_counter() - started,
                {GEN_AI_TOOL_NAME: name, TOOL_PLUGIN: plugin, TOOL_OUTCOME: outcome},
            )


def record_tool_output_truncated(tool_name):
    "Count one tool output that ``prepare_tool_output_for_model`` cut down."
    tool_output_truncated.add(1, {GEN_AI_TOOL_NAME: tool_name})
