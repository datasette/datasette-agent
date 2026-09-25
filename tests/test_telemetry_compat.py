"""telemetry_compat's fallback for Datasette releases without the kit.

Runs against whatever Datasette is installed, and forces the fallback by
hiding the kit's modules, so both halves are exercised on 1.0a41+.
"""

import copy
import importlib
import subprocess
import sys
import textwrap

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

import datasette_agent.telemetry_compat


@pytest.fixture
def fallback(monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "datasette.telemetry", None)
    monkeypatch.setitem(sys.modules, "datasette.telemetry_registry", None)
    module = importlib.reload(datasette_agent.telemetry_compat)
    yield module
    monkeypatch.undo()
    importlib.reload(datasette_agent.telemetry_compat)


def test_detects_installed_datasette():
    try:
        import datasette.telemetry_registry  # noqa: F401
    except ImportError:
        expected = False
    else:
        expected = True
    assert datasette_agent.telemetry_compat.HAS_DATASETTE_TELEMETRY is expected


def test_fallback_entries_are_strings_with_metadata(fallback):
    assert fallback.HAS_DATASETTE_TELEMETRY is False
    attribute = fallback.Attribute("x.mode", "A mode.", values=("a", "b"))
    span = fallback.SpanName("chat ", "A chat.", attributes=[attribute], prefix=True)
    metric = fallback.MetricName(
        "x.duration", fallback.HISTOGRAM, "s", "How long.", buckets=[1, 2]
    )
    assert attribute == "x.mode" and attribute.values == {"a", "b"}
    assert not attribute.optional
    assert span == "chat " and span.attributes == (attribute,) and span.prefix
    assert span.kind is SpanKind.INTERNAL
    assert metric.kind == "Histogram" and metric.buckets == (1, 2)
    # Copies collapse to plain str, as the kit's entries do.
    assert type(copy.deepcopy(attribute)) is str


def test_fallback_linked_root_span_kwargs(fallback):
    assert fallback.linked_root_span_kwargs()["links"] == []
    cause = SpanContext(
        trace_id=1, span_id=2, is_remote=False, trace_flags=TraceFlags(1)
    )
    with trace.use_span(NonRecordingSpan(cause)):
        kwargs = fallback.linked_root_span_kwargs()
    (link,) = kwargs["links"]
    assert link.context == cause
    assert not trace.get_current_span(kwargs["context"]).get_span_context().is_valid


def test_plugin_imports_without_the_kit():
    """The whole plugin loads with the kit hidden, as on Datasette < 1.0a41.
    A fresh interpreter, since the registry is built at import time.
    Front-loaded by conftest, like the other subprocess tests."""
    script = textwrap.dedent(
        """
        import sys
        import datasette.app  # core itself may import the kit
        sys.modules["datasette.telemetry"] = None
        sys.modules["datasette.telemetry_registry"] = None
        import datasette_agent, datasette_agent.background_agent
        from datasette_agent import telemetry_compat, telemetry_registry
        assert telemetry_compat.HAS_DATASETTE_TELEMETRY is False
        assert type(telemetry_registry.SPANS[0]).__module__ == (
            "datasette_agent.telemetry_compat"
        )
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True)
