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

ATTRIBUTES = ()


# --- Spans ----------------------------------------------------------------

SPANS = ()


# --- Metrics --------------------------------------------------------------

METRICS = ()
