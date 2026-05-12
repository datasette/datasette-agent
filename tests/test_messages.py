"""Tests for datasette_agent.messages helpers (persistence + strip rules)."""

import json

from datasette_agent.messages import strip_internal_keys


def test_strip_internal_keys_removes_underscore_prefixed():
    """Any top-level key starting with _ is treated as a side-channel for
    user-visible rendering / export and must not flow back to the model."""
    raw = json.dumps({"columns": ["a"], "rows": [[1]], "_html": "<table></table>"})
    stripped = json.loads(strip_internal_keys(raw))
    assert stripped == {"columns": ["a"], "rows": [[1]]}


def test_strip_internal_keys_removes_arbitrary_underscore_keys():
    """The rule is structural — not an enumerated list of names. Any key
    starting with _ goes, so future tools can stash extra hidden fields
    (e.g. _rows, _meta) without amending the strip code."""
    raw = json.dumps(
        {
            "row_count": 47,
            "_html": "<table></table>",
            "_rows": [[1, 2], [3, 4]],
            "_meta": {"shape": "table"},
        }
    )
    stripped = json.loads(strip_internal_keys(raw))
    assert stripped == {"row_count": 47}


def test_strip_internal_keys_preserves_non_underscore_keys():
    """Public keys must be left alone — including ones the legacy strip
    used to remove like 'sql'. Plugins that want a key hidden must opt in
    by prefixing it with _."""
    raw = json.dumps({"columns": ["a"], "rows": [[1]], "sql": "select * from t"})
    stripped = json.loads(strip_internal_keys(raw))
    assert stripped == {
        "columns": ["a"],
        "rows": [[1]],
        "sql": "select * from t",
    }


def test_strip_internal_keys_returns_original_when_nothing_to_strip():
    """If no underscore keys exist, return the input unchanged so we don't
    waste work re-serializing."""
    raw = json.dumps({"columns": ["a"], "rows": [[1]]})
    assert strip_internal_keys(raw) == raw


def test_strip_internal_keys_non_json_passthrough():
    """Non-JSON strings and empty input are passed through verbatim."""
    assert strip_internal_keys("") == ""
    assert strip_internal_keys("plain text") == "plain text"


def test_strip_internal_keys_non_dict_passthrough():
    """JSON that doesn't decode to a dict (lists, scalars) is passed through."""
    arr = json.dumps([{"_html": "x"}, {"_html": "y"}])
    assert strip_internal_keys(arr) == arr
