import json

import pytest
from inline_snapshot import snapshot

from datasette_agent.export_markdown import format_conversation_markdown


def _msg(role, content=None, tool_name=None, tool_arguments=None, tool_output=None):
    return {
        "role": role,
        "content": content,
        "tool_name": tool_name,
        "tool_arguments": tool_arguments,
        "tool_output": tool_output,
    }


def test_user_and_assistant_messages():
    messages = [
        _msg("user", content="Hello there"),
        _msg("assistant", content="Hi! How can I help?"),
    ]
    result = format_conversation_markdown("Test Chat", messages)
    assert result == snapshot(
        """\
# Test Chat

## User

Hello there

## Agent

Hi! How can I help?
"""
    )


def test_tool_call_and_result():
    messages = [
        _msg("user", content="Count rows"),
        _msg(
            "tool_call",
            tool_name="sql_query",
            tool_arguments='{"database": "main", "sql": "select count(*) from t"}',
        ),
        _msg(
            "tool_result",
            tool_name="sql_query",
            tool_output='{"columns": ["count(*)"], "rows": [{"count(*)": 42}]}',
        ),
        _msg("assistant", content="There are 42 rows."),
    ]
    result = format_conversation_markdown("Tool Test", messages)
    assert result == snapshot(
        """\
# Tool Test

## User

Count rows

<details>
<summary>Tool call: sql_query</summary>

```json
{
  "database": "main",
  "sql": "select count(*) from t"
}
```

```json
{
  "columns": [
    "count(*)"
  ],
  "rows": [
    {
      "count(*)": 42
    }
  ]
}
```

</details>

## Agent

There are 42 rows.
"""
    )


def test_html_widget_in_tool_result():
    output = json.dumps({"_html": "<div>Pretty</div>", "status": "ok"})
    messages = [
        _msg("tool_call", tool_name="render", tool_arguments="{}"),
        _msg("tool_result", tool_name="render", tool_output=output),
    ]
    result = format_conversation_markdown("HTML Test", messages)
    assert result == snapshot(
        """\
# HTML Test

<details>
<summary>Tool call: render</summary>

```json
{}
```

```json
{
  "status": "ok"
}
```

</details>

<details>
<summary>HTML widget: render</summary>

```html
<div>Pretty</div>
```

</details>
"""
    )


def test_create_artifact():
    args = json.dumps({"type": "html", "content": "<h1>Hello</h1>"})
    output = json.dumps(
        {
            "_html": "<iframe></iframe>",
            "artifact_id": "art-123",
            "status": "created",
        }
    )
    messages = [
        _msg("tool_call", tool_name="create_artifact", tool_arguments=args),
        _msg("tool_result", tool_name="create_artifact", tool_output=output),
    ]
    result = format_conversation_markdown("Artifact Test", messages)
    assert result == snapshot(
        """\
# Artifact Test

<details>
<summary>Tool call: create_artifact</summary>

```json
{
  "type": "html",
  "content": "<h1>Hello</h1>"
}
```

```json
{
  "artifact_id": "art-123",
  "status": "created"
}
```

</details>

<details>
<summary>HTML widget: create_artifact</summary>

```html
<iframe></iframe>
```

</details>

<details>
<summary>Artifact HTML</summary>

```html
<h1>Hello</h1>
```

</details>
"""
    )


def test_concurrent_tool_calls_paired_correctly():
    """Multiple tool calls of the same name should pair with results in order."""
    messages = [
        _msg(
            "tool_call",
            tool_name="describe_table",
            tool_arguments='{"table": "releases"}',
        ),
        _msg(
            "tool_call",
            tool_name="describe_table",
            tool_arguments='{"table": "repos"}',
        ),
        _msg(
            "tool_result",
            tool_name="describe_table",
            tool_output='"releases info"',
        ),
        _msg(
            "tool_result",
            tool_name="describe_table",
            tool_output='"repos info"',
        ),
    ]
    result = format_conversation_markdown("Concurrent", messages)
    # First result should pair with first call (releases), second with second (repos)
    assert '"table": "releases"' in result.split('"releases info"')[0]
    assert '"table": "repos"' in result.split('"repos info"')[0]


def test_tool_result_error_string():
    messages = [
        _msg(
            "tool_call",
            tool_name="describe_table",
            tool_arguments='{"table": "x"}',
        ),
        _msg(
            "tool_result",
            tool_name="describe_table",
            tool_output="Error: table not found",
        ),
    ]
    result = format_conversation_markdown("Error Test", messages)
    assert result == snapshot(
        """\
# Error Test

<details>
<summary>Tool call: describe_table</summary>

```json
{
  "table": "x"
}
```

```json
Error: table not found
```

</details>
"""
    )


def test_empty_assistant_message_skipped():
    messages = [
        _msg("assistant", content=None),
        _msg("assistant", content=""),
        _msg("assistant", content="Real content"),
    ]
    result = format_conversation_markdown("Skip Empty", messages)
    assert result == snapshot(
        """\
# Skip Empty

## Agent

Real content
"""
    )
