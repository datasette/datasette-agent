"""The pieces of Datasette's plugin telemetry kit this plugin builds on,
whichever Datasette is installed.

Datasette 1.0a41 ships the kit (``datasette.telemetry_registry``,
``datasette.telemetry``, ``datasette.telemetry_testing``) and instruments
core itself. On 1.0a37-1.0a40 the kit is absent, so this module falls back
to minimal stand-ins with the same constructor signatures. The agent's own
spans and metrics only need ``opentelemetry-api``, so they are emitted
either way - an older core just contributes no request or ``db.query``
spans of its own around them.

TODO: once a stable Datasette 1.x release includes the kit, raise the
``datasette>=`` floor to it, delete this module and import straight from
``datasette.telemetry_registry`` / ``datasette.telemetry`` again.
"""

try:
    from datasette.telemetry import linked_root_span_kwargs
    from datasette.telemetry_registry import (
        COUNTER,
        HISTOGRAM,
        UPDOWN_COUNTER,
        Attribute,
        MetricName,
        SpanName,
    )
except ImportError:
    HAS_DATASETTE_TELEMETRY = False
else:
    HAS_DATASETTE_TELEMETRY = True

if not HAS_DATASETTE_TELEMETRY:
    # Datasette < 1.0a41. Trimmed copies of the kit's definitions: the same
    # str subclasses carrying the same metadata, so the registry, the
    # instrumentation and scripts/telemetry-doc.py read them identically.
    from opentelemetry import context as otel_context_api
    from opentelemetry.trace import Link, SpanKind, get_current_span

    COUNTER = "Counter"
    UPDOWN_COUNTER = "UpDownCounter"
    HISTOGRAM = "Histogram"

    class _Entry(str):
        __slots__ = ()

        def __reduce__(self):
            # Copies collapse to a plain str, as in the kit - the SDK's
            # console exporter deepcopies metric attribute keys.
            return (str, (str(self),))

        def __repr__(self):
            return f"{type(self).__name__}({str(self)!r})"

    class Attribute(_Entry):
        __slots__ = ("description", "optional", "values")

        def __new__(cls, name, description, optional=False, values=None):
            self = super().__new__(cls, name)
            self.description = description
            self.optional = optional
            self.values = frozenset(values) if values is not None else None
            return self

    class SpanName(_Entry):
        __slots__ = ("attributes", "description", "dynamic", "kind", "prefix")

        def __new__(
            cls,
            name,
            description,
            attributes=(),
            prefix=False,
            dynamic=False,
            kind=SpanKind.INTERNAL,
        ):
            self = super().__new__(cls, name)
            self.description = description
            self.attributes = tuple(attributes)
            self.prefix = prefix
            self.dynamic = dynamic
            self.kind = kind
            return self

    class MetricName(_Entry):
        __slots__ = ("attributes", "buckets", "description", "kind", "unit")

        def __new__(cls, name, kind, unit, description, attributes=(), buckets=None):
            self = super().__new__(cls, name)
            self.kind = kind
            self.unit = unit
            self.description = description
            self.attributes = tuple(attributes)
            self.buckets = tuple(buckets) if buckets is not None else None
            return self

    def linked_root_span_kwargs(context=None):
        "Start a span as a new trace root linked to the current span."
        cause = get_current_span(context).get_span_context()
        links = [Link(cause)] if cause.is_valid else []
        return {"context": otel_context_api.Context(), "links": links}


__all__ = [
    "COUNTER",
    "HAS_DATASETTE_TELEMETRY",
    "HISTOGRAM",
    "UPDOWN_COUNTER",
    "Attribute",
    "MetricName",
    "SpanName",
    "linked_root_span_kwargs",
]
