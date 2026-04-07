import json
from datetime import datetime, timezone

from ulid import ULID

from .api import start_background_agent
from .schema import ensure_tables
from .sql_tools import get_default_tools
from .tools import AgentTool


def _make_append_to_report_tool(datasette, report_id):
    """Create an append_to_report AgentTool with a closure over report_id."""

    async def _append_to_report(datasette, actor, markdown_content):
        db = datasette.get_internal_database()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute_write(
            "UPDATE datasette_agent_explorer_reports "
            "SET content = content || ?, updated_at = ? WHERE id = ?",
            [markdown_content + "\n\n", now, report_id],
        )
        return json.dumps({"status": "appended", "report_id": report_id})

    return AgentTool(
        name="append_to_report",
        description=(
            "Append markdown content to the exploration report. "
            "Call this tool each time you have a finding to record. "
            "Each call appends to the existing report. Include section headers, "
            "data summaries, and observations in markdown format."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "markdown_content": {
                    "type": "string",
                    "description": "Markdown content to append to the report",
                },
            },
            "required": ["markdown_content"],
        },
        fn=_append_to_report,
    )


def _build_explorer_goal(database_name, table_name=None, extra_prompt=None):
    """Build the goal prompt for an explorer agent."""
    if table_name:
        goal = (
            f'You are a data exploration agent. Your task is to thoroughly explore the "{table_name}" '
            f'table in the "{database_name}" database and produce a detailed report.\n\n'
            "Follow this process:\n"
            "1. Describe the table schema and understand its columns\n"
            "2. Examine row count, value distributions, date ranges, and basic statistics\n"
            "3. Look for patterns, trends, anomalies, and outliers\n"
            "4. If there are foreign keys, join with related tables to find cross-table insights\n"
            "5. Use append_to_report() to record each finding as you go -- do not wait until the end\n\n"
            "Structure your report with clear markdown sections. Include:\n"
            "- Table structure overview\n"
            "- Key statistics and distributions\n"
            "- Interesting patterns or anomalies\n"
            "- Relationships with other tables\n"
            "- Data quality observations\n\n"
            "When you have thoroughly explored the table, call mark_finished with a brief summary."
        )
    else:
        goal = (
            f'You are a data exploration agent. Your task is to thoroughly explore the "{database_name}" '
            "database and produce a detailed report of interesting findings.\n\n"
            "Follow this process:\n"
            "1. Start by listing all tables and describing their schemas\n"
            "2. For each table, run queries to understand the data: row counts, value distributions, date ranges\n"
            "3. Look for interesting patterns, trends, anomalies, and relationships between tables\n"
            "4. Run analytical queries: aggregations, correlations, outlier detection\n"
            "5. Use append_to_report() to record each finding as you go -- do not wait until the end\n\n"
            "Structure your report with clear markdown sections. Include:\n"
            "- A summary of the database structure\n"
            "- Key statistics for each table\n"
            "- Interesting patterns or anomalies discovered\n"
            "- Relationships between tables\n"
            "- Any data quality issues found\n\n"
            "When you have thoroughly explored the database, call mark_finished with a brief summary."
        )

    if extra_prompt:
        goal += f"\n\nAdditional guidance from the user: {extra_prompt}"

    return goal


async def start_explorer(
    datasette, actor, database_name, table_name=None, extra_prompt=None
):
    """Start an explorer agent. Returns (report_id, agent_id)."""
    db = datasette.get_internal_database()
    await ensure_tables(db)

    report_id = str(ULID())
    actor_id = actor.get("id") if actor else None
    now = datetime.now(timezone.utc).isoformat()

    # Create report record
    await db.execute_write(
        "INSERT INTO datasette_agent_explorer_reports "
        "(id, agent_id, actor_id, database_name, table_name, extra_prompt, content, created_at, updated_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, '', ?, ?)",
        [report_id, actor_id, database_name, table_name, extra_prompt, now, now],
    )

    # Build goal
    goal = _build_explorer_goal(database_name, table_name, extra_prompt)

    # Build tools: standard SQL tools + append_to_report
    tools = get_default_tools()
    tools.append(_make_append_to_report_tool(datasette, report_id))

    # Start background agent
    agent_id = await start_background_agent(
        datasette=datasette,
        actor=actor,
        goal=goal,
        tools=tools,
    )

    # Link agent to report
    await db.execute_write(
        "UPDATE datasette_agent_explorer_reports SET agent_id = ? WHERE id = ?",
        [agent_id, report_id],
    )

    return report_id, agent_id
