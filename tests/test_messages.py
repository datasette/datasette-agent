"""Tests for datasette_agent.messages helpers (persistence + strip rules)."""

import json

import pytest

from datasette_agent.messages import (
    _shrink_json_value,
    combine_tool_messages_for_render,
    prepare_tool_output_for_model,
    strip_internal_keys,
)


def test_combine_tool_messages_pairs_results_by_id():
    messages = [
        {
            "role": "tool_call",
            "tool_name": "echo",
            "tool_arguments": '{"value": 1}',
            "tool_output": None,
            "tool_call_id": "call_1",
        },
        {
            "role": "tool_call",
            "tool_name": "echo",
            "tool_arguments": '{"value": 2}',
            "tool_output": None,
            "tool_call_id": "call_2",
        },
        {
            "role": "tool_result",
            "tool_name": "echo",
            "tool_arguments": None,
            "tool_output": '"second"',
            "tool_call_id": "call_2",
        },
        {
            "role": "tool_result",
            "tool_name": "echo",
            "tool_arguments": None,
            "tool_output": '"first"',
            "tool_call_id": "call_1",
        },
    ]

    assert combine_tool_messages_for_render(messages) == [
        {
            "role": "tool",
            "tool_name": "echo",
            "tool_arguments": '{"value": 1}',
            "tool_output": '"first"',
            "tool_call_id": "call_1",
        },
        {
            "role": "tool",
            "tool_name": "echo",
            "tool_arguments": '{"value": 2}',
            "tool_output": '"second"',
            "tool_call_id": "call_2",
        },
    ]


def test_combine_tool_messages_pairs_by_name_without_ids():
    messages = [
        {
            "role": "tool_call",
            "tool_name": "echo",
            "tool_arguments": "first",
            "tool_output": None,
        },
        {
            "role": "tool_call",
            "tool_name": "echo",
            "tool_arguments": "second",
            "tool_output": None,
        },
        {
            "role": "tool_result",
            "tool_name": "echo",
            "tool_arguments": None,
            "tool_output": "one",
        },
        {
            "role": "tool_result",
            "tool_name": "echo",
            "tool_arguments": None,
            "tool_output": "two",
        },
    ]

    paired = combine_tool_messages_for_render(messages)
    assert [message["tool_output"] for message in paired] == ["one", "two"]


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


@pytest.mark.parametrize(
    ("value", "expected", "changed"),
    [
        ("abcdef", "abc... (truncated)", True),
        ("abc", "abc", False),
        ("", "", False),
        ([1, 2, 3, 4], [1, 2], True),
        ([1, 2, 3], [1], True),
        ([1], [], True),
        ([], [], False),
        (
            [{"note": "abcdef"}],
            [{"note": "abc... (truncated)"}],
            True,
        ),
        (
            {"rows": [1, 2, 3, 4], "status": "ok"},
            {"rows": [1, 2], "status": "ok"},
            True,
        ),
        (
            {"summary": {"note": "abcdef"}, "status": "ok"},
            {"summary": {"note": "abc... (truncated)"}, "status": "ok"},
            True,
        ),
        (
            {"agent_output_truncated": True, "rows": [1, 2, 3, 4]},
            {"agent_output_truncated": True, "rows": [1, 2]},
            True,
        ),
        ({"agent_output_truncated": True}, {"agent_output_truncated": True}, False),
        (42, 42, False),
        (False, False, False),
        (None, None, False),
    ],
)
def test_shrink_json_value(value, expected, changed):
    assert _shrink_json_value(value, string_prefix_length=3) == (expected, changed)


def test_prepare_tool_output_for_model_keeps_large_html_side_channel_out():
    """Large user-visible HTML should not make model-visible output truncate.

    _html is rendered from the original persisted/SSE payload; the model gets
    the stripped payload, so only model-visible keys should count toward the
    limit.
    """
    raw = json.dumps(
        {
            "columns": ["a"],
            "row_count": 1,
            "truncated": False,
            "_html": "<table>" + ("x" * 5000) + "</table>",
            "_rows": [{"a": "x" * 5000}],
        }
    )
    prepared = prepare_tool_output_for_model(raw, max_length=1000)
    data = json.loads(prepared)
    assert data == {"columns": ["a"], "row_count": 1, "truncated": False}


def test_prepare_tool_output_for_model_recursively_truncates_to_valid_json():
    raw = json.dumps(
        {
            "columns": ["name", "description"],
            "rows": [
                {
                    "name": "one",
                    "description": "first " + ("x" * 500),
                },
                {
                    "name": "two",
                    "description": "second " + ("y" * 500),
                },
                {
                    "name": "three",
                    "description": "third " + ("z" * 500),
                },
            ],
            "truncated": False,
            "_html": "<table>" + ("h" * 5000) + "</table>",
        }
    )
    prepared = prepare_tool_output_for_model(raw, max_length=350)
    assert len(prepared) <= 350
    data = json.loads(prepared)
    assert data["agent_output_truncated"] is True
    assert data["truncated"] is False
    assert "_html" not in data
    assert len(data["rows"]) < 3


def test_prepare_tool_output_for_model_truncates_nested_strings():
    raw = json.dumps(
        {
            "summary": {
                "notes": "important prefix " + ("x" * 1000),
            },
            "status": "ok",
        }
    )
    prepared = prepare_tool_output_for_model(raw, max_length=180)
    assert len(prepared) <= 180
    data = json.loads(prepared)
    assert data["agent_output_truncated"] is True
    assert data["status"] == "ok"
    assert len(data["summary"]["notes"]) < 1000
