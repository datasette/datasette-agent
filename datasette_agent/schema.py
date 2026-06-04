SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_identities (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT,
    avatar_icon TEXT,
    avatar_color TEXT,
    owner_actor_id TEXT,
    model_id TEXT,
    system_prompt TEXT,
    token_hash TEXT,
    shareable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_conversations (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    title TEXT,
    model_id TEXT,
    identity_id TEXT REFERENCES agent_identities(id),
    launched_by TEXT,
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
    identity_id TEXT REFERENCES agent_identities(id),
    launched_by TEXT,
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


# Columns added to pre-existing run tables. CREATE TABLE IF NOT EXISTS won't
# add new columns to a table that already exists, so we ALTER them in
# idempotently for databases created before these columns existed.
_RUN_PROVENANCE_COLUMNS = {
    "agent_conversations": [
        ("identity_id", "TEXT REFERENCES agent_identities(id)"),
        ("launched_by", "TEXT"),
    ],
    "agent_background_agents": [
        ("identity_id", "TEXT REFERENCES agent_identities(id)"),
        ("launched_by", "TEXT"),
    ],
}


async def _existing_columns(db, table):
    rows = (await db.execute(f"PRAGMA table_info({table})")).rows
    return {row["name"] for row in rows}


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
    # Idempotently add provenance columns to tables that may predate them.
    for table, columns in _RUN_PROVENANCE_COLUMNS.items():
        existing = await _existing_columns(db, table)
        for name, coltype in columns:
            if name not in existing:
                await db.execute_write(
                    f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"
                )
