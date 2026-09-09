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

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace

from . import __version__

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
    return meter.create_up_down_counter(
        entry, unit=entry.unit, description=description
    )
