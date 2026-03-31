SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS datasette_agent_conversations (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    title TEXT,
    model_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasette_agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES datasette_agent_conversations(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_arguments TEXT,
    tool_output TEXT,
    created_at TEXT NOT NULL
);
"""


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
