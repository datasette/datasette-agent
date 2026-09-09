#!/usr/bin/env python3
"""Render README.md's OpenTelemetry reference from the telemetry registry.

Rewrites the block between the telemetry-reference markers so the docs
cannot drift from the code; `--check` exits non-zero if the file would
change (run in CI). The registry entries carry everything this needs, so
this is a small renderer, not a copy of core's cog-into-RST generator.

Usage: uv run scripts/telemetry-doc.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasette_agent import telemetry_registry as reg  # noqa: E402

README = Path(__file__).resolve().parent.parent / "README.md"
START = "<!-- telemetry-reference:start -->"
END = "<!-- telemetry-reference:end -->"


def _md(text: str) -> str:
    "Registry descriptions use RST double-backtick literals; fold to Markdown."
    return " ".join(text.replace("``", "`").split())


def _attribute_line(attribute) -> str:
    parts = [f"- `{attribute}`"]
    if attribute.optional:
        parts.append("*(optional)*")
    parts.append("-")
    parts.append(_md(attribute.description))
    if attribute.values is not None:
        values = ", ".join(f"`{value}`" for value in sorted(attribute.values))
        parts.append(f"One of: {values}.")
    return " ".join(parts)


def _span_heading(span) -> str:
    if span.prefix:
        return f"`{span}{{...}}`"
    return f"`{span}`"


def render() -> str:
    lines: list[str] = []
    lines.append("#### Spans")
    lines.append("")
    lines.append(
        "Span names ending in `{...}` are families: the suffix is the model id "
        "or the tool name. Kinds are `INTERNAL` unless noted."
    )
    lines.append("")
    for span in reg.SPANS:
        kind = f" *({span.kind.name})*" if span.kind.name != "INTERNAL" else ""
        lines.append(f"**{_span_heading(span)}**{kind} - {_md(span.description)}")
        lines.append("")
        if span.attributes:
            for attribute in span.attributes:
                lines.append(_attribute_line(attribute))
            lines.append("")
    lines.append("#### Metrics")
    lines.append("")
    for metric in reg.METRICS:
        lines.append(
            f"**`{metric}`** *({metric.kind}, unit `{metric.unit}`)* - "
            f"{_md(metric.description)}"
        )
        lines.append("")
        if metric.attributes:
            for attribute in metric.attributes:
                lines.append(_attribute_line(attribute))
            lines.append("")
        if metric.buckets is not None:
            buckets = ", ".join(str(boundary) for boundary in metric.buckets)
            lines.append(f"Bucket boundaries: {buckets}.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    check = "--check" in sys.argv
    text = README.read_text()
    if START not in text or END not in text:
        print(f"README.md is missing the {START} / {END} markers", file=sys.stderr)
        return 1
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{START}\n\n{render()}\n{END}{tail}"
    if updated == text:
        return 0
    if check:
        print(
            "README.md telemetry reference is stale; run "
            "`uv run scripts/telemetry-doc.py`",
            file=sys.stderr,
        )
        return 1
    README.write_text(updated)
    print("Rewrote README.md telemetry reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
