SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_modeler_models (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_modeler_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL REFERENCES agent_modeler_models(id),
    revision INTEGER NOT NULL,
    doc_json TEXT NOT NULL,
    operations_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_modeler_revisions_model
    ON agent_modeler_revisions(model_id, revision);
"""


async def ensure_tables(db):
    await db.execute_write_script(SCHEMA_SQL)
