import json
import os

import httpx
from datasette.utils.asgi import Response

from .tools import get_agent_tools


async def _build_realtime_system_prompt(datasette, actor):
    parts = [
        "You are a helpful data analysis assistant. "
        "You have access to tools that let you explore and query databases. "
        "Use the tools to answer questions about the data. "
        "Always start by listing databases and tables, then describe relevant tables "
        "before writing queries."
    ]
    db_info = {}
    for db_name, db in datasette.databases.items():
        if db_name.startswith("_"):
            continue
        tables = await db.table_names()
        if tables:
            db_info[db_name] = tables
    if db_info:
        parts.append("\nAvailable databases and tables:")
        for db_name, tables in db_info.items():
            parts.append(f"\n  {db_name}: {', '.join(tables)}")
    return "\n".join(parts)


async def realtime_page(request, datasette):
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    return Response.html(
        await datasette.render_template(
            "agent_realtime.html",
            {},
            request=request,
        )
    )


async def realtime_token(request, datasette):
    """Create an ephemeral token for the OpenAI Realtime API."""
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return Response.json(
            {"error": "OPENAI_API_KEY environment variable not set"}, status=500
        )

    # Build tool definitions from registered plugins
    agent_tools = await get_agent_tools(datasette)
    tools = [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        }
        for t in agent_tools
    ]

    instructions = await _build_realtime_system_prompt(datasette, actor=request.actor)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-realtime-preview",
                "modalities": ["text"],
                "instructions": instructions,
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {"type": "server_vad"},
                "tools": tools,
                "tool_choice": "auto",
            },
        )

    if resp.status_code != 200:
        return Response.json(
            {"error": f"OpenAI API error: {resp.text}"}, status=502
        )

    data = resp.json()
    return Response.json(data)


async def realtime_tool_call(request, datasette):
    """Execute a tool server-side and return the result."""
    await datasette.ensure_permission(action="datasette-agent", actor=request.actor)
    if request.method != "POST":
        return Response.json({"error": "POST required"}, status=405)

    body = await request.post_body()
    data = json.loads(body)
    tool_name = data.get("name")
    arguments = data.get("arguments", {})

    agent_tools = await get_agent_tools(datasette)
    tool = None
    for t in agent_tools:
        if t.name == tool_name:
            tool = t
            break

    if not tool:
        return Response.json({"error": f"Unknown tool: {tool_name}"}, status=404)

    try:
        result = await tool.fn(datasette=datasette, actor=request.actor, **arguments)
        return Response.json({"output": result})
    except Exception as e:
        return Response.json({"output": json.dumps({"error": str(e)})})
