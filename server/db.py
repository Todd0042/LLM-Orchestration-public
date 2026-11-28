"""Versioned SQLite schema migrations layered on top of config.init_db's base tables.

Every replicated table gets a local `seq` column bumped by triggers on insert/update, so
peers can ask "give me everything you changed after seq N" without trusting wall clocks.
"""

from __future__ import annotations

import sqlite3

_SEQ_COUNTER = """CREATE TABLE IF NOT EXISTS seq_counter (id INTEGER PRIMARY KEY CHECK (id = 1), v INTEGER NOT NULL);
INSERT OR IGNORE INTO seq_counter (id, v) VALUES (1, 0);"""


def _seq_triggers(table: str) -> str:
    return f"""
ALTER TABLE {table} ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_{table}_seq ON {table}(seq);
CREATE TRIGGER IF NOT EXISTS {table}_seq_ins AFTER INSERT ON {table} BEGIN
  UPDATE seq_counter SET v = v + 1 WHERE id = 1;
  UPDATE {table} SET seq = (SELECT v FROM seq_counter WHERE id = 1) WHERE rowid = NEW.rowid;
END;
CREATE TRIGGER IF NOT EXISTS {table}_seq_upd AFTER UPDATE ON {table} WHEN NEW.seq = OLD.seq BEGIN
  UPDATE seq_counter SET v = v + 1 WHERE id = 1;
  UPDATE {table} SET seq = (SELECT v FROM seq_counter WHERE id = 1) WHERE rowid = NEW.rowid;
END;
UPDATE {table} SET seq = rowid;
UPDATE seq_counter SET v = MAX(v, (SELECT COALESCE(MAX(seq), 0) FROM {table})) WHERE id = 1;
"""


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
ALTER TABLE desktop_intermission_queue ADD COLUMN target_node TEXT NOT NULL DEFAULT 'desktop';
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  node_id TEXT NOT NULL,
  zone TEXT NOT NULL,
  client TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
""",
    ),
    (
        2,
        _SEQ_COUNTER
        + _seq_triggers("chats")
        + _seq_triggers("messages")
        + _seq_triggers("artifacts")
        + """
CREATE TABLE IF NOT EXISTS sync_state (
  peer_id TEXT NOT NULL,
  stream TEXT NOT NULL,
  last_seq INTEGER NOT NULL DEFAULT 0,
  synced_at REAL,
  PRIMARY KEY (peer_id, stream)
);
CREATE TABLE IF NOT EXISTS vault_sync_state (
  peer_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (peer_id, rel_path)
);
""",
    ),
    (
        3,
        """
CREATE TABLE IF NOT EXISTS repo_activity (
  repo_key TEXT PRIMARY KEY,
  repo_name TEXT NOT NULL,
  node_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  since REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_handoffs (
  id TEXT PRIMARY KEY,
  repo_name TEXT NOT NULL,
  from_node TEXT NOT NULL,
  to_node TEXT NOT NULL,
  status TEXT NOT NULL,
  wip_ref TEXT NOT NULL DEFAULT '',
  wip_sha TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
"""
        + _seq_triggers("repo_activity"),
    ),
    (
        4,
        """
CREATE TABLE IF NOT EXISTS service_state (
  service_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  restarts_json TEXT NOT NULL DEFAULT '[]',
  next_action_at REAL NOT NULL DEFAULT 0,
  last_detail TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  PRIMARY KEY (service_id, node_id)
);
CREATE TABLE IF NOT EXISTS service_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  node_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_service_events_ts ON service_events(ts);
""",
    ),
    (
        5,
        """
ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
""",
    ),
    (
        6,
        """
CREATE TABLE IF NOT EXISTS passkeys (
  id TEXT PRIMARY KEY,
  public_key TEXT NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  last_used_at REAL NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_sessions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  last_seen REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
"""
        + _seq_triggers("passkeys")
        + _seq_triggers("device_sessions"),
    ),
    (
        7,
        """
CREATE TABLE IF NOT EXISTS orch_cards (
  id TEXT PRIMARY KEY,
  repo TEXT NOT NULL UNIQUE,
  workdir TEXT NOT NULL,
  holder TEXT NOT NULL,
  claude_session TEXT NOT NULL,
  hook_secret TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
""",
    ),
]


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(v) AS v FROM schema_version").fetchone()
    current = int(row["v"] or 0) if row else 0
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript("BEGIN;" + script + f"\nINSERT INTO schema_version (v) VALUES ({version});\nCOMMIT;")
        current = version
    return current
