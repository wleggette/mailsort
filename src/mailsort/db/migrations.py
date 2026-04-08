"""Database schema creation and versioned migrations.

Each migration is a (version, name, sql) tuple. Migrations are applied in
order and never re-applied. The schema_version table tracks which have run.
"""

from __future__ import annotations

import logging

from mailsort.db.database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration SQL
# ---------------------------------------------------------------------------

_M1_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_M2_RULES = """
CREATE TABLE IF NOT EXISTS rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type           TEXT NOT NULL
                            CHECK(rule_type IN ('exact_sender','sender_domain','list_id','subject_regex')),
    condition_value     TEXT NOT NULL,
    target_folder_path  TEXT NOT NULL,
    target_folder_id    TEXT,
    confidence          REAL NOT NULL DEFAULT 0.90,
    hit_count           INTEGER NOT NULL DEFAULT 0,
    last_hit_at         TEXT,
    source              TEXT NOT NULL DEFAULT 'auto'
                            CHECK(source IN ('auto','manual','bootstrap','llm_suggested')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    active              BOOLEAN NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rules_type_value ON rules(rule_type, condition_value);
CREATE INDEX IF NOT EXISTS idx_rules_active     ON rules(active);
"""

_M3_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL
                      CHECK(status IN ('running','completed','failed','abandoned')),
    trigger       TEXT NOT NULL DEFAULT 'scheduler',
    emails_seen   INTEGER NOT NULL DEFAULT 0,
    emails_moved  INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
"""

_M4_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT,
    email_id                TEXT NOT NULL,
    thread_id               TEXT,
    from_address            TEXT,
    from_domain             TEXT,
    subject                 TEXT,
    list_id                 TEXT,
    source_folder           TEXT NOT NULL DEFAULT 'INBOX',
    target_folder           TEXT NOT NULL,
    confidence              REAL NOT NULL,
    classification_source   TEXT NOT NULL
                                CHECK(classification_source IN ('thread','rule','llm','manual')),
    rule_id                 INTEGER,
    llm_reasoning           TEXT,
    moved                   BOOLEAN NOT NULL,
    skip_reason             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (rule_id) REFERENCES rules(id)
);

CREATE INDEX IF NOT EXISTS idx_audit_email   ON audit_log(email_id);
CREATE INDEX IF NOT EXISTS idx_audit_thread  ON audit_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_audit_domain  ON audit_log(from_domain);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_run     ON audit_log(run_id);
"""

_M5_CONTACTS = """
CREATE TABLE IF NOT EXISTS contacts (
    email_address TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    relationship  TEXT,
    fastmail_uid  TEXT,
    refreshed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email_address);
"""

_M6_FOLDER_DESCRIPTIONS = """
CREATE TABLE IF NOT EXISTS folder_descriptions (
    folder_path  TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    source       TEXT NOT NULL CHECK(source IN ('auto','manual')),
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_M7_INBOX_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS inbox_snapshot (
    email_id   TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_run ON inbox_snapshot(run_id);

CREATE TABLE IF NOT EXISTS learner_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_M8_AUDIT_RECEIVED_AT = """
ALTER TABLE audit_log ADD COLUMN email_received_at TEXT;
"""

_M9_RUNS_ERROR_STATUS = """
-- Rebuild runs table to add 'error' to the status CHECK constraint.
-- SQLite does not support ALTER COLUMN, so we recreate the table.
-- PRAGMA foreign_keys=OFF is required because inbox_snapshot has a FK to runs.
PRAGMA foreign_keys=OFF;
DROP TABLE IF EXISTS runs_new;
CREATE TABLE runs_new (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL
                      CHECK(status IN ('running','completed','failed','abandoned','error')),
    trigger       TEXT NOT NULL DEFAULT 'scheduler',
    emails_seen   INTEGER NOT NULL DEFAULT 0,
    emails_moved  INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
INSERT INTO runs_new SELECT * FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
PRAGMA foreign_keys=ON;
"""

_M10_RUNS_DRY_RUN = """
ALTER TABLE runs ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT 0;
"""

_M11_COMPUTED_CONFIDENCE = """
-- Rename last_hit_at → last_relevant_at on the rules table.
-- SQLite does not support ALTER COLUMN / RENAME COLUMN before 3.25,
-- so we recreate the table.
PRAGMA foreign_keys=OFF;
DROP TABLE IF EXISTS rules_new;
CREATE TABLE rules_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type           TEXT NOT NULL
                            CHECK(rule_type IN ('exact_sender','sender_domain','list_id','subject_regex')),
    condition_value     TEXT NOT NULL,
    target_folder_path  TEXT NOT NULL,
    target_folder_id    TEXT,
    confidence          REAL NOT NULL DEFAULT 0.90,
    hit_count           INTEGER NOT NULL DEFAULT 0,
    last_relevant_at    TEXT,
    source              TEXT NOT NULL DEFAULT 'auto'
                            CHECK(source IN ('auto','manual','bootstrap','llm_suggested')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    active              BOOLEAN NOT NULL DEFAULT 1
);
INSERT INTO rules_new (id, rule_type, condition_value, target_folder_path,
                       target_folder_id, confidence, hit_count, last_relevant_at,
                       source, created_at, updated_at, active)
SELECT id, rule_type, condition_value, target_folder_path,
       target_folder_id, confidence, hit_count, last_hit_at,
       source, created_at, updated_at, active
FROM rules;
DROP TABLE rules;
ALTER TABLE rules_new RENAME TO rules;
CREATE INDEX IF NOT EXISTS idx_rules_type_value ON rules(rule_type, condition_value);
CREATE INDEX IF NOT EXISTS idx_rules_active     ON rules(active);

-- Add 'correction' to audit_log classification_source CHECK.
-- Same table-rebuild technique.
DROP TABLE IF EXISTS audit_log_new;
CREATE TABLE audit_log_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT,
    email_id                TEXT NOT NULL,
    thread_id               TEXT,
    from_address            TEXT,
    from_domain             TEXT,
    subject                 TEXT,
    list_id                 TEXT,
    source_folder           TEXT NOT NULL DEFAULT 'INBOX',
    target_folder           TEXT NOT NULL,
    confidence              REAL NOT NULL,
    classification_source   TEXT NOT NULL
                                CHECK(classification_source IN ('thread','rule','llm','manual','correction')),
    rule_id                 INTEGER,
    llm_reasoning           TEXT,
    moved                   BOOLEAN NOT NULL,
    skip_reason             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    email_received_at       TEXT,

    FOREIGN KEY (rule_id) REFERENCES rules(id)
);
INSERT INTO audit_log_new SELECT * FROM audit_log;
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;
CREATE INDEX IF NOT EXISTS idx_audit_email   ON audit_log(email_id);
CREATE INDEX IF NOT EXISTS idx_audit_thread  ON audit_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_audit_domain  ON audit_log(from_domain);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_run     ON audit_log(run_id);
PRAGMA foreign_keys=ON;
"""

_M12_SYSTEM_SOURCE_AND_CACHE = """
-- Add 'system' to classification_source CHECK and add 'cached' column.
-- 'system' is used by build_move_decision fallback when classification is None.
-- 'cached' tracks whether an LLM result was reused from a prior run.
PRAGMA foreign_keys=OFF;
DROP TABLE IF EXISTS audit_log_new;
CREATE TABLE audit_log_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT,
    email_id                TEXT NOT NULL,
    thread_id               TEXT,
    from_address            TEXT,
    from_domain             TEXT,
    subject                 TEXT,
    list_id                 TEXT,
    source_folder           TEXT NOT NULL DEFAULT 'INBOX',
    target_folder           TEXT NOT NULL,
    confidence              REAL NOT NULL,
    classification_source   TEXT NOT NULL
                                CHECK(classification_source IN ('thread','rule','llm','manual','correction','system')),
    rule_id                 INTEGER,
    llm_reasoning           TEXT,
    moved                   BOOLEAN NOT NULL,
    skip_reason             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    email_received_at       TEXT,
    cached                  BOOLEAN NOT NULL DEFAULT 0,

    FOREIGN KEY (rule_id) REFERENCES rules(id)
);
INSERT INTO audit_log_new (id, run_id, email_id, thread_id, from_address,
                           from_domain, subject, list_id, source_folder,
                           target_folder, confidence, classification_source,
                           rule_id, llm_reasoning, moved, skip_reason,
                           created_at, email_received_at)
SELECT id, run_id, email_id, thread_id, from_address,
       from_domain, subject, list_id, source_folder,
       target_folder, confidence, classification_source,
       rule_id, llm_reasoning, moved, skip_reason,
       created_at, email_received_at
FROM audit_log;
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;
CREATE INDEX IF NOT EXISTS idx_audit_email   ON audit_log(email_id);
CREATE INDEX IF NOT EXISTS idx_audit_thread  ON audit_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_audit_domain  ON audit_log(from_domain);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_run     ON audit_log(run_id);
PRAGMA foreign_keys=ON;
"""

_MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "create_schema_version",      _M1_SCHEMA_VERSION),
    (2, "create_rules",               _M2_RULES),
    (3, "create_runs",                _M3_RUNS),
    (4, "create_audit_log",           _M4_AUDIT_LOG),
    (5, "create_contacts",            _M5_CONTACTS),
    (6, "create_folder_descriptions", _M6_FOLDER_DESCRIPTIONS),
    (7, "create_inbox_snapshot",      _M7_INBOX_SNAPSHOT),
    (8, "add_audit_received_at",      _M8_AUDIT_RECEIVED_AT),
    (9, "add_runs_error_status",      _M9_RUNS_ERROR_STATUS),
    (10, "add_runs_dry_run",           _M10_RUNS_DRY_RUN),
    (11, "computed_confidence",        _M11_COMPUTED_CONFIDENCE),
    (12, "system_source_and_cache",   _M12_SYSTEM_SOURCE_AND_CACHE),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_migrations(db: Database) -> None:
    """Apply all pending migrations in order. Safe to call on every startup."""
    # Bootstrap: schema_version may not exist yet on first run
    db.execute(_M1_SCHEMA_VERSION)
    db.commit()

    current = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    logger.debug("Current schema version: %d", current)

    for version, name, sql in _MIGRATIONS:
        if version <= current:
            continue
        logger.info("Applying migration %d: %s", version, name)
        with db.transaction():
            db.conn.executescript(sql)
            db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
        logger.info("Migration %d applied", version)
