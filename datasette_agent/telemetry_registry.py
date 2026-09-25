"""The single source of truth for every OpenTelemetry signal datasette-agent emits.

Built from Datasette's public plugin telemetry kit
(``datasette.telemetry_registry``, via ``telemetry_compat`` so it still
imports on Datasette releases that predate the kit - see Datasette's
"Telemetry for plugin authors" docs): ``Attribute`` / ``SpanName`` / ``MetricName`` subclass
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

from .telemetry_compat import (
    COUNTER,
    HISTOGRAM,
    UPDOWN_COUNTER,
    Attribute,
    MetricName,
    SpanName,
)

__all__ = [
    "ATTRIBUTES",
    "BACKGROUND_ITERATION_BUCKETS",
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
    "``error``, ``chain_limit`` and ``max_iterations`` set span status "
    "``ERROR``.",
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
    "holds the history in memory; for a background run, the size at its "
    "last iteration.",
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
GEN_AI_TOOL_NAME = Attribute(
    "gen_ai.tool.name",
    "The tool's registered name (``sql_query``, ``describe_table``, a "
    "plugin's tool...). Bounded: the set of registered tools.",
)
GEN_AI_TOOL_CALL_ID = Attribute(
    "gen_ai.tool.call.id",
    "The provider's id for this tool call, when it issues one. Never the "
    "argument-hash fallback the plugin derives for providers that do not. "
    "Spans only.",
    optional=True,
)
GEN_AI_TOOL_TYPE = Attribute(
    "gen_ai.tool.type",
    "Always ``function`` - every agent tool is a client-side function.",
    values={"function"},
)
TOOL_PLUGIN = Attribute(
    "datasette_agent.tool.plugin",
    "The pluggy name of the plugin whose ``register_agent_tools`` hook "
    "registered the tool (``agent`` for this plugin's own tools), or "
    "``unknown`` for a tool constructed directly. Lets an operator see "
    "that the slow tool came from ``datasette-foo``. Bounded by installed "
    "plugins.",
)
TOOL_OUTCOME = Attribute(
    "datasette_agent.tool.outcome",
    "How the tool call ended. Most tools return errors *as data* - "
    '``{"error": ...}`` is a successful call from llm\'s point of view '
    "and a failed one from the operator's - so the returned payload is "
    "classified too: ``permission_denied`` and ``not_found`` for the two "
    "messages this plugin's own tools emit, ``error`` for any other "
    "top-level ``error`` key or a raised exception, ``suspended`` when the "
    "tool paused the turn on ``ask_user()`` / ``browser_task()``, ``ok`` "
    "otherwise. Only a raised exception sets span status ``ERROR``; a "
    "returned error payload means the tool did what it was asked and the "
    "request to it was wrong.",
    values={"ok", "error", "suspended", "permission_denied", "not_found"},
)
SUSPENSION_KIND = Attribute(
    "datasette_agent.suspension.kind",
    "What a tool suspended the turn on: an ``ask_user()`` question or a "
    "``browser_task()``. On the ``execute_tool`` span when "
    "``outcome=suspended``; the dimension of the suspension metrics.",
    values={"question", "browser_task"},
    optional=True,
)
SUSPENSION_RESOLUTION = Attribute(
    "datasette_agent.suspension.resolution",
    "How a suspension ended: a question was ``answered``; a browser task "
    "was ``completed`` by the page, ``cancelled`` by the user, or "
    "``expired`` past its deadline. Expiry is recorded lazily, when "
    "someone next looks, so an expired wait is at least the timeout and "
    "possibly much more.",
    values={"answered", "completed", "cancelled", "expired"},
)
QUESTION_TYPE = Attribute(
    "datasette_agent.question.type",
    "The ``ask_user()`` question's shape: yes/no, a choice, or free text. "
    "Never the prompt or the options.",
    values={"boolean", "choice", "text"},
    optional=True,
)
QUESTION_ID = Attribute(
    "datasette_agent.question.id",
    "The ``agent_questions`` row the tool suspended on. Spans only.",
    optional=True,
)
TASK_ID = Attribute(
    "datasette_agent.task.id",
    "The ``agent_browser_tasks`` row the tool suspended on. Spans only.",
    optional=True,
)
TOOL_REPLAYED = Attribute(
    "datasette_agent.tool.replayed",
    "``True`` when the tool call consumed a stored answer or browser-task "
    "result instead of suspending - the re-execution of a suspended call "
    "on resume. Absent on a fresh call.",
    optional=True,
)
RESUMED_FROM = Attribute(
    "datasette_agent.resumed_from",
    "For ``mode=resume``: what the turn is continuing from. The span also "
    "carries a link to the ``execute_tool`` span that suspended, when its "
    "trace context was persisted with the row.",
    values={"question", "browser_task"},
    optional=True,
)
TOOL_OUTPUT_BYTES = Attribute(
    "datasette_agent.tool.output.bytes",
    "Length of the tool's returned string. A size, never the content.",
)
SQL_DISPLAY = Attribute(
    "datasette_agent.sql.display",
    "``sql_query`` only: the ``display`` mode the model picked. The "
    "distribution is directly actionable - the system prompt is trying to "
    "steer it. Rows, truncation and the SQL text are on the nested core "
    "``db.query`` span; not duplicated here.",
    values={"model", "both", "user"},
    optional=True,
)
BACKGROUND_ID = Attribute(
    "datasette_agent.background.id",
    "The background agent's ULID. Spans only, never a metric dimension.",
    optional=True,
)
BACKGROUND_ITERATIONS = Attribute(
    "datasette_agent.background.iterations",
    "How many loop passes the background run made before it ended.",
    optional=True,
)
BACKGROUND_MAX_ITERATIONS = Attribute(
    "datasette_agent.background.max_iterations",
    "The iteration cap the run was allowed (``MAX_ITERATIONS``).",
    optional=True,
)
BACKGROUND_SPAWNED_FROM_CONVERSATION = Attribute(
    "datasette_agent.background.spawned_from_conversation",
    "``True`` when a chat turn's ``spawn_background_agent`` tool started "
    "the run (its completion is then posted back as a notification), "
    "``False`` for the HTTP API and the explorer. Never the other "
    "conversation's id - the span link carries that.",
    optional=True,
)
BACKGROUND_ITERATION = Attribute(
    "datasette_agent.background.iteration",
    "1-based loop pass within the background run.",
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
    BACKGROUND_ID,
    BACKGROUND_ITERATIONS,
    BACKGROUND_MAX_ITERATIONS,
    BACKGROUND_SPAWNED_FROM_CONVERSATION,
    BACKGROUND_ITERATION,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_TYPE,
    TOOL_PLUGIN,
    TOOL_OUTCOME,
    SUSPENSION_KIND,
    SUSPENSION_RESOLUTION,
    QUESTION_TYPE,
    QUESTION_ID,
    TASK_ID,
    TOOL_REPLAYED,
    RESUMED_FROM,
    TOOL_OUTPUT_BYTES,
    SQL_DISPLAY,
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
    "work, not a call out. Status is ``ERROR`` only for ``outcome=error``, "
    "``chain_limit`` and ``max_iterations``; a suspension is ``UNSET`` "
    "because it is the designed way a turn ends. For a background agent "
    "or explorer run the span covers the whole run and is a **root span "
    "in its own trace** with a link back to whatever span was current "
    "when the run was started - the API request, or the spawning turn's "
    "``execute_tool spawn_background_agent`` - the shape core uses for "
    "``block=False`` writes: a run outlives its cause, so a link records "
    "the causation without asserting containment. The ``background.*`` "
    "attributes appear on those runs only.",
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
        RESUMED_FROM,
        BACKGROUND_ID,
        BACKGROUND_ITERATIONS,
        BACKGROUND_MAX_ITERATIONS,
        BACKGROUND_SPAWNED_FROM_CONVERSATION,
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

EXECUTE_TOOL = SpanName(
    "execute_tool ",
    "One tool invocation, named ``execute_tool {gen_ai.tool.name}``, "
    "following the GenAI ``execute_tool`` convention. Every tool - this "
    "plugin's own and any registered through ``register_agent_tools`` - "
    "runs through one code path, so every call gets a span. Core's "
    "``db.query`` spans issued by the tool nest under it, as does the "
    "nested internal ``SERVER`` span ``execute_write_sql`` produces by "
    "posting through ``datasette.client`` (core marks that one "
    "``datasette.internal_client: true``). Arguments and output are never "
    "recorded; a suspension is ``outcome=suspended`` with the question or "
    "task id, never an error. The span ends when the tool raises; the "
    "human wait that follows is not a span (it can outlive the process) "
    "but the ``suspension.wait`` histogram, and the resumed turn links "
    "back here.",
    (
        GEN_AI_OPERATION_NAME,
        GEN_AI_TOOL_NAME,
        GEN_AI_TOOL_CALL_ID,
        GEN_AI_TOOL_TYPE,
        TOOL_PLUGIN,
        TOOL_OUTCOME,
        SUSPENSION_KIND,
        QUESTION_TYPE,
        QUESTION_ID,
        TASK_ID,
        TOOL_REPLAYED,
        TOOL_OUTPUT_BYTES,
        SQL_DISPLAY,
        ERROR_TYPE,
    ),
    prefix=True,
)

BACKGROUND_ITERATION_SPAN = SpanName(
    "datasette_agent.background.iteration",
    "One pass of a background agent's loop, child of the run's "
    "``invoke_agent`` root. Each pass rebuilds the system prompt, reloads "
    'the whole history and runs a chain, so this is where "why did '
    'iteration 7 take four minutes" gets answered; ``chat`` and '
    "``execute_tool`` spans nest under it.",
    (BACKGROUND_ITERATION,),
)

SPANS = (INVOKE_AGENT, CHAT, EXECUTE_TOOL, SYSTEM_PROMPT, BACKGROUND_ITERATION_SPAN)


# --- Metrics --------------------------------------------------------------
#
# Every duration is in seconds on the bucket sets above. Nothing here
# carries a conversation id, a tool-call id or any other unbounded value;
# the dimensions are closed enums or bounded-by-installed-code names
# (models, providers).

CHAIN_STEP_BUCKETS = (1, 2, 3, 4, 6, 8, 10, 15, 20)
BACKGROUND_ITERATION_BUCKETS = (1, 2, 3, 5, 8, 12, 16, 20, 30, 50)

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
    "connection and a provider stream open, so this is the capacity number; "
    "``mode=background`` / ``explorer`` is the number of background agents "
    "running.",
    (MODE,),
)
M_SUSPENSIONS = MetricName(
    "datasette_agent.suspensions",
    COUNTER,
    "{suspension}",
    "Turns suspended waiting on a human, by kind and asking tool - "
    "counted when the pending row is inserted, not when a suspended call "
    "re-raises on resume. How often the agent stops to ask.",
    (SUSPENSION_KIND, GEN_AI_TOOL_NAME, QUESTION_TYPE),
)
M_SUSPENSION_WAIT = MetricName(
    "datasette_agent.suspension.wait",
    HISTOGRAM,
    "s",
    "Seconds from a suspension's row being created to its resolution - "
    "how long people take to answer, and how often browser tasks expire. "
    "Recorded at the four resolution sites after their guarded UPDATE "
    "succeeds, so a lost race is not double-counted.",
    (SUSPENSION_KIND, SUSPENSION_RESOLUTION),
    buckets=HUMAN_WAIT_BUCKETS,
)
M_BACKGROUND_ITERATIONS = MetricName(
    "datasette_agent.background.iterations",
    HISTOGRAM,
    "{iteration}",
    "Loop passes per background run, by outcome - how close the agents run "
    "to ``MAX_ITERATIONS``. The whole run's duration is on "
    "``datasette_agent.turn.duration`` with ``mode=background``; "
    "iterations are deliberately not mixed into that histogram.",
    (MODE, OUTCOME),
    buckets=BACKGROUND_ITERATION_BUCKETS,
)

M_TOOL_DURATION = MetricName(
    "datasette_agent.tool.duration",
    HISTOGRAM,
    "s",
    "One measurement per ``execute_tool`` span. Per-tool call count and "
    "error rate derive from its count, so there is no separate counter. "
    "A ``suspended`` duration is the time until the tool raised - short, "
    "and not the human wait.",
    (GEN_AI_TOOL_NAME, TOOL_PLUGIN, TOOL_OUTCOME),
    buckets=GENAI_DURATION_BUCKETS,
)
M_TOOL_OUTPUT_TRUNCATED = MetricName(
    "datasette_agent.tool.output.truncated",
    COUNTER,
    "{call}",
    "Tool outputs cut down before the model saw them. A high rate for "
    "``sql_query`` means the model is over-fetching, or the model-visible "
    "output limit is wrong for the workload. A counter rather than a "
    "dimension on the duration histogram: truncation is an event to alert "
    "on, not something to split latency by.",
    (GEN_AI_TOOL_NAME,),
)

METRICS = (
    M_TOKEN_USAGE,
    M_OPERATION_DURATION,
    M_TIME_TO_FIRST_TOKEN,
    M_TURN_DURATION,
    M_CHAIN_STEPS,
    M_TURNS_ACTIVE,
    M_TOOL_DURATION,
    M_TOOL_OUTPUT_TRUNCATED,
    M_BACKGROUND_ITERATIONS,
    M_SUSPENSIONS,
    M_SUSPENSION_WAIT,
)
