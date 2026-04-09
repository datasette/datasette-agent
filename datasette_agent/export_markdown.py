import json
from collections import defaultdict


def _pretty_json(value):
    try:
        return json.dumps(json.loads(value), indent=2)
    except (json.JSONDecodeError, TypeError):
        return value or ""


def format_conversation_markdown(title, messages):
    """Format a conversation as a markdown transcript.

    Args:
        title: Conversation title string
        messages: List of message dicts with keys: role, content, tool_name,
                  tool_arguments, tool_output
    Returns:
        Markdown string
    """
    parts = []
    parts.append(f"# {title}\n")

    # Queue of pending tool_call arguments per tool_name
    pending_calls = defaultdict(list)

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        tool_name = msg["tool_name"]
        tool_arguments = msg["tool_arguments"]
        tool_output = msg["tool_output"]

        if role == "user":
            parts.append(f"## User\n\n{content}\n")

        elif role == "assistant":
            if content:
                parts.append(f"## Agent\n\n{content}\n")

        elif role == "tool_call":
            pending_calls[tool_name].append(tool_arguments)

        elif role == "tool_result":
            # Pop the matching tool_call arguments
            call_args = None
            if pending_calls[tool_name]:
                call_args = pending_calls[tool_name].pop(0)

            # Check for _html in output
            html_content = None
            display_output = tool_output
            try:
                parsed = json.loads(tool_output)
                if isinstance(parsed, dict) and "_html" in parsed:
                    html_content = parsed.pop("_html")
                    display_output = json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass

            # Check for create_artifact HTML content
            artifact_html = None
            if tool_name == "create_artifact" and call_args:
                try:
                    parsed_args = json.loads(call_args)
                    if "content" in parsed_args:
                        artifact_html = parsed_args["content"]
                except (json.JSONDecodeError, TypeError):
                    pass

            # Write the tool call/result pair as collapsed details
            parts.append(f"<details>\n<summary>Tool call: {tool_name}</summary>\n")
            if call_args:
                parts.append(f"```json\n{_pretty_json(call_args)}\n```\n")
            parts.append(f"```json\n{_pretty_json(display_output)}\n```\n")
            parts.append("</details>\n")

            # HTML widget from _html
            if html_content:
                parts.append(f"<details>\n<summary>HTML widget: {tool_name}</summary>\n")
                parts.append(f"```html\n{html_content}\n```\n")
                parts.append("</details>\n")

            # Artifact HTML from create_artifact arguments
            if artifact_html:
                parts.append(f"<details>\n<summary>Artifact HTML</summary>\n")
                parts.append(f"```html\n{artifact_html}\n```\n")
                parts.append("</details>\n")

    return "\n".join(parts)
