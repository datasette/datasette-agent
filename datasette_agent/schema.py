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

CREATE TABLE IF NOT EXISTS agent_questions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    call_key TEXT NOT NULL,
    ask_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    question_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    options_json TEXT,
    html TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    answer_json TEXT,
    answered_by TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    trace_id TEXT,
    span_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_questions_conversation
    ON agent_questions(conversation_id, status);

CREATE TABLE IF NOT EXISTS agent_browser_tasks (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    call_key TEXT NOT NULL,
    task_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    label TEXT,
    html TEXT NOT NULL,
    payload_json TEXT,
    timeout_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    completed_at TEXT,
    completed_by TEXT,
    trace_id TEXT,
    span_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_browser_tasks_conversation
    ON agent_browser_tasks(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_browser_tasks_call
    ON agent_browser_tasks(conversation_id, call_key, task_index);

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


# Columns added after their tables first shipped; CREATE TABLE IF NOT
# EXISTS won't add them to existing databases. trace_id / span_id are the
# OpenTelemetry ids of the execute_tool span a suspension was raised
# from, as W3C lowercase hex - NULL when no tracing provider was
# installed - so the resumed turn can link back to it.
_MIGRATIONS = {
    "agent_questions": ("html", "trace_id", "span_id"),
    "agent_browser_tasks": ("trace_id", "span_id"),
}


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
    for table, columns in _MIGRATIONS.items():
        existing = {
            row["name"]
            for row in (await db.execute(f"PRAGMA table_info({table})")).rows
        }
        for column in columns:
            if column not in existing:
                await db.execute_write(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
