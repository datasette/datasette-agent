# Importing the fixture names registers them: session-scoped autouse
# tracer/meter providers with in-memory synchronous export (silently
# skipped when the SDK is not installed), and a per-test reset that drains
# the exporter and reader. Tests take otel_spans / otel_metrics. See the
# kit's docstrings for the once-per-process/ProxyTracer reasoning.
from datasette.telemetry_testing import (  # noqa: F401
    MetricsCollector,
    otel_metrics,
    otel_meter_provider,
    otel_provider,
    otel_reset,
    otel_spans,
)


def pytest_collection_modifyitems(items):
    # The kit's sdk-isolation check shells out to a fresh interpreter, and
    # its docstring documents a macOS/CPython 3.13 fork+exec crash (SIGBUS)
    # when subprocess-spawning tests run late in a thread-heavy process -
    # so run it first, the way core's conftest front-loads its equivalents.
    subprocess_tests = {
        "test_package_never_imports_the_sdk",
        "test_turn_runs_unchanged_without_a_provider",
    }
    front = [item for item in items if item.name in subprocess_tests]
    for item in front:
        items.insert(0, items.pop(items.index(item)))
