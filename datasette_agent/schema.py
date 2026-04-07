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

CREATE TABLE IF NOT EXISTS datasette_agent_background_agents (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES datasette_agent_conversations(id),
    actor_id TEXT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    final_message TEXT,
    error TEXT,
    spawned_by_conversation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasette_agent_pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
