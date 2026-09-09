"""Registry <-> reality conformance for datasette-agent's telemetry.

Static half: the registry is well-formed. Dynamic half: one broad workload,
a single ``collect()``, then the kit's four conformance assertions plus the
sentinel-content privacy walk.
"""

import pytest

from datasette_agent.telemetry_registry import (
    ATTRIBUTES,
    HISTOGRAM,
    METRICS,
    SPANS,
)

SCOPE = "datasette_agent"


# --- Static half (no SDK needed) -----------------------------------------


def test_package_never_imports_the_sdk():
    # Front-loaded by conftest's pytest_collection_modifyitems - the
    # helper's docstring documents a macOS/CPython 3.13 fork+exec crash
    # when subprocess-spawning tests run late in a thread-heavy process.
    from datasette.telemetry_testing import assert_package_never_imports_sdk

    assert_package_never_imports_sdk("datasette_agent")


def test_registry_has_no_duplicate_names():
    for group in (SPANS, METRICS, ATTRIBUTES):
        names = [str(entry) for entry in group]
        assert len(names) == len(set(names)), f"duplicates in {names}"


def test_registry_entries_are_documented():
    for entry in (*SPANS, *METRICS, *ATTRIBUTES):
        assert entry.description and entry.description.strip(), (
            f"{entry!r} has no description"
        )


def test_every_histogram_declares_buckets():
    for metric in METRICS:
        if metric.kind == HISTOGRAM:
            assert metric.buckets, f"{metric!r} declares no buckets"
        else:
            assert metric.buckets is None, f"{metric!r} is not a histogram"


def test_entries_are_usable_as_plain_strings():
    for entry in (*SPANS, *METRICS, *ATTRIBUTES):
        assert isinstance(entry, str)
        assert entry == str(entry)


def test_every_name_is_prefixed():
    # The kit docs' naming rule: signals live under a prefix the plugin
    # owns, never bare datasette.*. The gen_ai.* names are the GenAI
    # semantic conventions, deliberately shared so backends render them;
    # error.type is core's semconv spelling, deliberately reused.
    for entry in (*SPANS, *METRICS):
        name = str(entry)
        assert (
            name.startswith("datasette_agent.")
            or name.startswith("gen_ai.")
            or name.startswith(("invoke_agent ", "chat ", "execute_tool "))
        ), entry
    for attribute in ATTRIBUTES:
        name = str(attribute)
        assert (
            name.startswith("datasette_agent.")
            or name.startswith("gen_ai.")
            or name == "error.type"
        ), attribute


def test_span_attributes_are_registered_attributes():
    registered = set(ATTRIBUTES)
    for entry in (*SPANS, *METRICS):
        for attribute in entry.attributes:
            assert attribute in registered, f"{entry!r} uses unlisted {attribute!r}"
