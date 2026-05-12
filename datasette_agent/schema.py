SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_conversations (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    title TEXT,
    model_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    model_id TEXT,
    llm_response_id TEXT,
    usage_json TEXT,
    options_json TEXT,
    system_prompt TEXT,
    datetime_utc TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    role TEXT NOT NULL,
    message_json TEXT NOT NULL,
    response_id INTEGER REFERENCES agent_responses(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_conversation
    ON agent_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_background_agents (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    actor_id TEXT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    final_message TEXT,
    error TEXT,
    spawned_by_conversation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_pending_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_explorer_reports (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agent_background_agents(id),
    actor_id TEXT,
    database_name TEXT NOT NULL,
    table_name TEXT,
    extra_prompt TEXT,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
