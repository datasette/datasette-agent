"""The single source of truth for every OpenTelemetry signal datasette-agent emits.

Built from Datasette's public plugin telemetry kit
(``datasette.telemetry_registry`` - see Datasette's "Telemetry for plugin
authors" docs): ``Attribute`` / ``SpanName`` / ``MetricName`` subclass
``str``, so a registry entry *is* the name handed to OpenTelemetry and a
typo is an ``ImportError`` rather than a silently misnamed signal.

Three things read this module:

1. **The instrumentation** in ``telemetry.py`` and the call sites it serves.
2. **The generated reference** in the README (``scripts/telemetry-doc.py``).
3. **The conformance test** ``tests/test_telemetry_registry.py``, which runs
   a broad workload and asserts both directions with the kit's helpers:
   everything emitted is registered (enum membership included) and
   everything registered is emitted.

Naming: the instrumentation scope is the import package name
(``datasette_agent``). Three spans and two metrics follow the OpenTelemetry
GenAI semantic conventions (``gen_ai.*``, ``invoke_agent`` / ``chat`` /
``execute_tool``) so trace backends render them with their GenAI styling;
everything plugin-specific lives under the ``datasette_agent.*`` prefix.
``error.type`` is core's semconv spelling, reused rather than shadowed.

Privacy: no prompt text, model output, tool arguments, tool output, actor
ids or error messages are ever recorded - counts, sizes and closed enums
instead. Conversation, tool-call and response ids ride on spans only,
never on a metric.
"""

from opentelemetry.trace import SpanKind

from datasette.telemetry_registry import (
    COUNTER,
    HISTOGRAM,
    UPDOWN_COUNTER,
    Attribute,
    MetricName,
    SpanName,
)

__all__ = [
    "ATTRIBUTES",
    "CHAIN_STEP_BUCKETS",
    "COUNTER",
    "GENAI_DURATION_BUCKETS",
    "HISTOGRAM",
    "HUMAN_WAIT_BUCKETS",
    "METRICS",
    "SPANS",
    "TOKEN_BUCKETS",
    "UPDOWN_COUNTER",
]

# --- Histogram bucket boundaries ------------------------------------------
#
# Core's DURATION_BUCKETS (0.1 ms to 10 s) are tuned for in-process SQLite.
# Model calls, tool calls and agent turns are a different regime, so every
# duration here uses its own set. Published in the generated docs: an
# operator writing a histogram_quantile() query needs to know them.

# Model calls, tool calls and turns: the semconv recommendation for
# gen_ai.client.operation.duration, doubling from 10 ms to ~82 s.
GENAI_DURATION_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
)

# Token counts: the semconv recommendation for gen_ai.client.token.usage,
# powers of four from 1 to 64M.
TOKEN_BUCKETS = (
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    67108864,
)

# Human wait times for ask_user() and browser_task(): seconds to a day. A
# question can sit unanswered until the user comes back tomorrow.
HUMAN_WAIT_BUCKETS = (
    1,
    5,
    15,
    30,
    60,
    120,
    300,
    600,
    900,
    1800,
    3600,
    7200,
    14400,
    86400,
)


# --- Attributes -----------------------------------------------------------
#
# gen_ai.* attributes are the OpenTelemetry GenAI semantic conventions,
# reused verbatim so trace backends (Grafana, Honeycomb, Langfuse, Phoenix,
# Jaeger) key their GenAI rendering off them. datasette_agent.* attributes
# are this plugin's own.

GEN_AI_OPERATION_NAME = Attribute(
    "gen_ai.operation.name",
    "The GenAI operation: ``invoke_agent`` for a whole turn, ``chat`` for "
    "one model response, ``execute_tool`` for one tool call.",
    values={"invoke_agent", "chat", "execute_tool"},
)
GEN_AI_AGENT_NAME = Attribute(
    "gen_ai.agent.name",
    "Always ``datasette-agent``. A fixed string today; a future agent "
    "profiles feature could vary it, but it would stay bounded.",
    values={"datasette-agent"},
)
GEN_AI_CONVERSATION_ID = Attribute(
    "gen_ai.conversation.id",
    "The conversation's ULID. Opaque, but a stable key across every turn a "
    "person has with the agent, so closer to a session id than a request "
    "id. **Spans only, never a metric dimension.** The same ULID is in the "
    "request span's ``url.path`` anyway.",
)
GEN_AI_PROVIDER_NAME = Attribute(
    "gen_ai.provider.name",
    "Which LLM provider served the call, derived from the llm plugin that "
    "implements the model: ``anthropic``, ``openai``, ``gcp.gemini``, "
    "``echo`` (tests)... Semconv well-known spellings where one exists, the "
    "plugin's module name otherwise. Bounded by the installed llm plugins.",
)
GEN_AI_REQUEST_MODEL = Attribute(
    "gen_ai.request.model",
    "The llm model id the call was made with (``claude-sonnet-5``, ``echo``). "
    "On an ``invoke_agent`` span it is the conversation's pinned model, or "
    "``unknown`` when the turn failed before a model was resolved. Bounded "
    "by the models an operator has configured.",
)
GEN_AI_RESPONSE_MODEL = Attribute(
    "gen_ai.response.model",
    "The provider-reported concrete model (``claude-sonnet-5-20260101``) "
    "when llm reports one that differs from the requested id.",
    optional=True,
)
GEN_AI_RESPONSE_ID = Attribute(
    "gen_ai.response.id",
    "The provider's response id, for support tickets. Present only when the "
    "provider returns one. Spans only.",
    optional=True,
)
GEN_AI_USAGE_INPUT_TOKENS = Attribute(
    "gen_ai.usage.input_tokens",
    "Input tokens the provider billed for this response, when it reports usage.",
    optional=True,
)
GEN_AI_USAGE_OUTPUT_TOKENS = Attribute(
    "gen_ai.usage.output_tokens",
    "Output tokens the provider billed for this response, when it reports usage.",
    optional=True,
)
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = Attribute(
    "gen_ai.usage.cache_read.input_tokens",
    "Input tokens served from the provider's prompt cache, normalised from "
    "Anthropic's ``cache_read_input_tokens`` and OpenAI's "
    "``prompt_tokens_details.cached_tokens``. Cache hit rate is the single "
    "biggest cost lever with prompt-caching providers.",
    optional=True,
)
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = Attribute(
    "gen_ai.usage.cache_creation.input_tokens",
    "Input tokens written to the provider's prompt cache (Anthropic's "
    "``cache_creation_input_tokens``).",
    optional=True,
)
GEN_AI_TOKEN_TYPE = Attribute(
    "gen_ai.token.type",
    "Which count a ``gen_ai.client.token.usage`` measurement is. ``input`` "
    "and ``output`` are the semconv values; ``cache_read`` and "
    "``cache_creation`` are recorded additionally when the provider reports "
    "prompt-cache usage.",
    values={"input", "output", "cache_read", "cache_creation"},
)
MODE = Attribute(
    "datasette_agent.mode",
    "How the turn was started: ``chat`` (a user message on the stream "
    "route), ``resume`` (continuing a turn suspended on ``ask_user()`` or "
    "``browser_task()``), ``cli`` (``datasette agent chat``), ``background`` "
    "(a background agent's whole run) or ``explorer`` (the same, launched "
    "by the explorer).",
    values={"chat", "resume", "cli", "background", "explorer"},
)
OUTCOME = Attribute(
    "datasette_agent.outcome",
    "How the turn ended. ``done`` is a normal end; ``question`` and "
    "``browser_task`` mean the turn suspended waiting on a human, which is "
    "the designed way a turn ends and **not** an error; ``chain_limit`` is "
    "llm's chain limit tripping; ``cancelled`` is the asyncio task being "
    "cancelled (a client disconnect, or the background-agent cancel "
    "endpoint); ``error`` is anything else raised. Background runs end in "
    "``completed``, ``max_iterations``, ``cancelled`` or ``error``. Only "
    "``error`` and ``chain_limit`` set span status ``ERROR``.",
    values={
        "done",
        "question",
        "browser_task",
        "chain_limit",
        "cancelled",
        "error",
        "completed",
        "max_iterations",
    },
)
HISTORY_MESSAGES = Attribute(
    "datasette_agent.history.messages",
    "Messages loaded from the conversation's persisted history for this "
    "turn, this turn's own user message included - what drives input "
    "tokens up over a long conversation. Absent in ``cli`` mode, where llm "
    "holds the history in memory.",
    optional=True,
)
CHAIN_STEPS = Attribute(
    "datasette_agent.chain.steps",
    "Model responses in this turn's chain: 1 for a plain answer, 2 or more "
    "when the model called tools and came back for another round.",
)
TOOL_CALLS = Attribute(
    "datasette_agent.tool_calls",
    "Tool calls that ran to completion this turn (a call that suspended the "
    "turn is not counted; it re-runs on resume).",
)
NOTIFICATIONS_DRAINED = Attribute(
    "datasette_agent.notifications_drained",
    "Background-agent completion notifications prepended to the user's "
    "message on this turn. Set only when there were any.",
    optional=True,
)
CHAIN_INDEX = Attribute(
    "datasette_agent.chain.index",
    "0-based position of this model response within the turn's chain.",
)
STREAMING = Attribute(
    "datasette_agent.streaming",
    "Whether the response was streamed (``True`` for chat, ``False`` for "
    "background agents).",
)
TOOL_CALLS_REQUESTED = Attribute(
    "datasette_agent.tool_calls_requested",
    "Tool calls the model asked for in this response. Zero means the chain ends here.",
)
DATABASES = Attribute(
    "datasette_agent.databases",
    "Databases the system-prompt builder iterated - each one costs a "
    "permission check and a ``table_names()`` query, on every turn.",
)
PROMPT_CHARS = Attribute(
    "datasette_agent.prompt.chars",
    "Length of the built system prompt in characters. A size, never the text.",
)
ERROR_TYPE = Attribute(
    "error.type",
    "Exception class name when the work raised; ``CancelledError`` when it "
    "was cancelled. Never the message. Core's semantic-convention spelling, "
    "reused per the plugin telemetry docs. Absent on success, and absent "
    "when a tool merely *returned* an error payload - that is an outcome.",
    optional=True,
)

ATTRIBUTES = (
    GEN_AI_OPERATION_NAME,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
    GEN_AI_TOKEN_TYPE,
    MODE,
    OUTCOME,
    HISTORY_MESSAGES,
    CHAIN_STEPS,
    TOOL_CALLS,
    NOTIFICATIONS_DRAINED,
    CHAIN_INDEX,
    STREAMING,
    TOOL_CALLS_REQUESTED,
    DATABASES,
    PROMPT_CHARS,
    ERROR_TYPE,
)


# --- Spans ----------------------------------------------------------------
#
# Span families (``chat {model}``) are registered as their fixed prefix
# INCLUDING the trailing space, with prefix=True; the kit's span_for()
# matches them by prefix, exact names first. The families here share no
# prefix with each other, so the kit's first-match rule never comes into
# play - keep it that way.

INVOKE_AGENT = SpanName(
    "invoke_agent datasette-agent",
    "One agent turn, following the GenAI ``invoke_agent`` convention. "
    "Starts before the pending-notification drain and the user message "
    "insert so every ``db.query`` of the turn nests under it, and ends in "
    "a ``finally`` so the error path that sends the ``error`` SSE event "
    "still closes it. Kind ``INTERNAL``: the agent is this process's own "
    "work, not a call out. Status is ``ERROR`` only for ``outcome=error`` "
    "and ``chain_limit``; a suspension is ``UNSET`` because it is the "
    "designed way a turn ends.",
    (
        GEN_AI_OPERATION_NAME,
        GEN_AI_AGENT_NAME,
        GEN_AI_CONVERSATION_ID,
        GEN_AI_REQUEST_MODEL,
        MODE,
        OUTCOME,
        HISTORY_MESSAGES,
        CHAIN_STEPS,
        TOOL_CALLS,
        NOTIFICATIONS_DRAINED,
        ERROR_TYPE,
    ),
)
CHAT = SpanName(
    "chat ",
    "One model response, named ``chat {gen_ai.request.model}``. Kind "
    "``CLIENT``: the one span here that represents a call to something "
    "outside the process, exactly as core's ``db.query`` is ``CLIENT``. "
    "Covers exactly the streaming window - llm's response object is lazy "
    "and iterating it is what makes the HTTP call - so the gap before the "
    "``gen_ai.first_token`` event is provider latency and the gap between "
    "one ``chat`` ending and the next starting is tool time. If the "
    "operator also installs ``opentelemetry-instrumentation-httpx`` the "
    "provider's HTTP call appears as a child. The prompt, the messages, "
    "the streamed text and the reasoning are never recorded.",
    (
        GEN_AI_OPERATION_NAME,
        GEN_AI_PROVIDER_NAME,
        GEN_AI_REQUEST_MODEL,
        GEN_AI_RESPONSE_MODEL,
        GEN_AI_RESPONSE_ID,
        GEN_AI_USAGE_INPUT_TOKENS,
        GEN_AI_USAGE_OUTPUT_TOKENS,
        GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
        GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
        CHAIN_INDEX,
        STREAMING,
        TOOL_CALLS_REQUESTED,
        ERROR_TYPE,
    ),
    prefix=True,
    kind=SpanKind.CLIENT,
)
SYSTEM_PROMPT = SpanName(
    "datasette_agent.system_prompt",
    "Building the system prompt: one permission check and one "
    "``table_names()`` query per database, on every turn and every "
    "background-agent iteration. On an instance with many databases this "
    "is a visible slice of turn latency, and this span is the evidence for "
    "(or against) caching it.",
    (DATABASES, PROMPT_CHARS),
)

SPANS = (INVOKE_AGENT, CHAT, SYSTEM_PROMPT)


# --- Metrics --------------------------------------------------------------
#
# Every duration is in seconds on the bucket sets above. Nothing here
# carries a conversation id, a tool-call id or any other unbounded value;
# the dimensions are closed enums or bounded-by-installed-code names
# (models, providers).

CHAIN_STEP_BUCKETS = (1, 2, 3, 4, 6, 8, 10, 15, 20)

M_TOKEN_USAGE = MetricName(
    "gen_ai.client.token.usage",
    HISTOGRAM,
    "{token}",
    "Tokens per model response, per token type - the semconv metric. A "
    "histogram rather than a counter, as semconv chose, so the p95 of "
    "input tokens says whether the context is growing while ``sum`` still "
    "gives total spend.",
    (
        GEN_AI_OPERATION_NAME,
        GEN_AI_PROVIDER_NAME,
        GEN_AI_REQUEST_MODEL,
        GEN_AI_TOKEN_TYPE,
    ),
    buckets=TOKEN_BUCKETS,
)
M_OPERATION_DURATION = MetricName(
    "gen_ai.client.operation.duration",
    HISTOGRAM,
    "s",
    "Duration of one model response, measured across the streaming "
    "window - the semconv metric. ``error.type`` splits failures out of "
    "the latency distribution.",
    (GEN_AI_OPERATION_NAME, GEN_AI_PROVIDER_NAME, GEN_AI_REQUEST_MODEL, ERROR_TYPE),
    buckets=GENAI_DURATION_BUCKETS,
)
M_TIME_TO_FIRST_TOKEN = MetricName(
    "datasette_agent.chat.time_to_first_token",
    HISTOGRAM,
    "s",
    "Seconds from starting a streamed model response to its first text, "
    "reasoning or tool-call chunk - the number a user waiting on a "
    "spinner feels. Streamed responses only. The same instant is a "
    "``gen_ai.first_token`` event on the ``chat`` span, but a span event "
    "does not survive sampling and this does.",
    (GEN_AI_PROVIDER_NAME, GEN_AI_REQUEST_MODEL),
    buckets=GENAI_DURATION_BUCKETS,
)
M_TURN_DURATION = MetricName(
    "datasette_agent.turn.duration",
    HISTOGRAM,
    "s",
    "One measurement per ``invoke_agent`` span: the whole turn, model "
    "calls and tool calls and persistence included. **The top-line "
    "instrument**: turns per second, error rate, suspension rate and "
    "p50/p95 turn time all come from it, split by mode.",
    (MODE, OUTCOME, GEN_AI_REQUEST_MODEL, ERROR_TYPE),
    buckets=GENAI_DURATION_BUCKETS,
)
M_CHAIN_STEPS = MetricName(
    "datasette_agent.chain.steps",
    HISTOGRAM,
    "{step}",
    "Model round-trips per turn. A rising median means the model is "
    "flailing, tool outputs are being truncated and re-fetched, or the "
    "system prompt is not landing; ``chain_limit`` outcomes on the turn "
    "histogram are the extreme of the same signal.",
    (MODE, GEN_AI_REQUEST_MODEL),
    buckets=CHAIN_STEP_BUCKETS,
)
M_TURNS_ACTIVE = MetricName(
    "datasette_agent.turns.active",
    UPDOWN_COUNTER,
    "{turn}",
    "Turns in flight right now, by mode. Each streaming turn holds an SSE "
    "connection and a provider stream open, so this is the capacity number.",
    (MODE,),
)

METRICS = (
    M_TOKEN_USAGE,
    M_OPERATION_DURATION,
    M_TIME_TO_FIRST_TOKEN,
    M_TURN_DURATION,
    M_CHAIN_STEPS,
    M_TURNS_ACTIVE,
)
