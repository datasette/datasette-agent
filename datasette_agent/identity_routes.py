"""HTTP routes for agent identities: avatar pic + identities list API.

The avatar endpoint mirrors datasette-user-profiles' generated-SVG approach
(``/-/profile/pic/<id>``) so agents render with a consistent style in the share
dialog and anywhere ``actors_from_ids`` is used.
"""

import re

from datasette.utils.asgi import Response

from .identities import get_identity_by_slug, list_identities

# A simple robot glyph (Bootstrap Icons "robot", MIT) drawn inside a circle.
# Inner SVG paths only; we wrap them with a colored circle.
_ROBOT_INNER = (
    '<path d="M6 12.5a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 0 1h-3a.5.5 0 0 1-.5-.5M3 '
    "8.062C3 6.76 4.235 5.765 5.53 5.886a26.6 26.6 0 0 0 4.94 0C11.765 5.765 13 "
    "6.76 13 8.062v1.157a.93.93 0 0 1-.765.935c-.845.147-2.34.346-4.235.346s-3.39"
    "-.2-4.235-.346A.93.93 0 0 1 3 9.219zm4.542-.827a.25.25 0 0 0-.217.068l-.92.9"
    "2a25 25 0 0 1-1.871-.183.25.25 0 0 0-.068.495c.55.076 1.232.149 2.02.193a.25"
    ".25 0 0 0 .189-.071l.754-.736.847 1.71a.25.25 0 0 0 .404.062l.932-.97a25 25 "
    "0 0 0 1.922-.188.25.25 0 0 0-.068-.495c-.538.074-1.207.145-1.98.189a.25.25 0"
    " 0 0-.166.076l-.754.785-.842-1.7a.25.25 0 0 0-.182-.135Z\"/>"
    '<path d="M8.5 1.866a1 1 0 1 0-1 0V3h-2A4.5 4.5 0 0 0 1 7.5V8a1 1 0 0 0-1 '
    "1v2a1 1 0 0 0 1 1v1a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-1a1 1 0 0 0 1-1V9a1 1 0 "
    "0 0-1-1v-.5A4.5 4.5 0 0 0 10.5 3h-2zM14 7.5V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1"
    'V7.5A3.5 3.5 0 0 1 5.5 4h5A3.5 3.5 0 0 1 14 7.5"/>'
)

_DEFAULT_COLOR = "#1e66f5"


def _generate_agent_svg(color: str, size: int = 96) -> str:
    if not re.match(r"^#[0-9a-fA-F]{6}$", color or ""):
        color = _DEFAULT_COLOR
    icon_size = size * 0.5625
    offset = (size - icon_size) / 2
    scale = icon_size / 16
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<circle cx="{size // 2}" cy="{size // 2}" r="{size // 2}" fill="{color}"/>'
        f'<g transform="translate({offset:.1f},{offset:.1f}) scale({scale:.4f})" fill="white">'
        f"{_ROBOT_INNER}"
        f"</g></svg>"
    )


async def agent_pic(request, datasette):
    """GET /-/agent/pic/<slug> — generated SVG avatar for an agent identity."""
    slug = request.url_vars["slug"]
    row = await get_identity_by_slug(datasette, slug)
    color = row["avatar_color"] if row and row.get("avatar_color") else _DEFAULT_COLOR
    svg = _generate_agent_svg(color)
    return Response(
        body=svg,
        content_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=3600, stale-while-revalidate=86400",
        },
    )


def _agent_actor_dict(datasette, row, *, current_actor_id=None):
    """Render an identity row as a datasette-acl-share Actor dict."""
    out = {
        "id": row["id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "avatar_url": datasette.urls.path(f"/-/agent/pic/{row['slug']}"),
        "kind": "agent",
    }
    if current_actor_id is not None:
        out["owned_by_me"] = row.get("owner_actor_id") == current_actor_id
    return out


async def api_identities(request, datasette):
    """GET /-/agent/api/identities?q= — agents the current actor may share with.

    v1 scope (plan §6): owned_by_me OR shareable=1. Requires an authenticated
    actor. Returns both ``results`` (consumed by datasette-acl-share's listAgents)
    and ``agents`` (the shape named in the task file) for compatibility.
    """
    actor = request.actor
    if not actor or actor.get("id") is None:
        return Response.json({"error": "Authentication required"}, status=403)
    current_actor_id = str(actor["id"])

    q = (request.args.get("q") or "").strip().lower()

    owned = await list_identities(
        datasette, owner=current_actor_id, include_disabled=False
    )
    shareable = await list_identities(
        datasette, shareable=True, include_disabled=False
    )
    # Union, de-duplicated by id, preserving display_name ordering.
    seen = {}
    for row in owned + shareable:
        seen[row["id"]] = row
    rows = sorted(seen.values(), key=lambda r: (r["display_name"] or "").lower())

    if q:
        rows = [
            r
            for r in rows
            if q in (r["display_name"] or "").lower()
            or q in (r["slug"] or "").lower()
        ]

    actors = [
        _agent_actor_dict(datasette, r, current_actor_id=current_actor_id)
        for r in rows
    ]
    return Response.json({"results": actors, "agents": actors})
