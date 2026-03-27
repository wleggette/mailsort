# Data Models

SQLite schema, Pydantic models, and migration versioning.

## SQLite Schema

### Rules

```sql
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,          -- exact_sender | sender_domain | list_id | subject_regex
    condition_value TEXT NOT NULL,    -- The match value (email, domain, regex, etc.)
    target_folder_path TEXT NOT NULL, -- e.g., "INBOX/Affairs/Banks"
    target_folder_id TEXT,           -- JMAP mailbox ID (resolved at runtime)
    confidence REAL DEFAULT 1.0,     -- 0.0 to 1.0
    hit_count INTEGER DEFAULT 0,     -- Times this rule has matched (live runs only)
    last_hit_at TEXT,                -- ISO timestamp (live runs only)
    source TEXT NOT NULL DEFAULT 'auto'
        CHECK(source IN ('auto','manual','bootstrap','llm_suggested')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active BOOLEAN DEFAULT 1
);

CREATE INDEX idx_rules_type_value ON rules(rule_type, condition_value);
CREATE INDEX idx_rules_active ON rules(active);
CREATE UNIQUE INDEX idx_rules_unique_active
    ON rules(rule_type, condition_value) WHERE active = 1;
```

### Runs

```sql
CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL CHECK(status IN ('running','completed','failed','abandoned')),
    trigger       TEXT NOT NULL DEFAULT 'scheduler',
    emails_seen   INTEGER NOT NULL DEFAULT 0,
    emails_moved  INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
```

### Audit Log

```sql
CREATE TABLE audit_log (
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
    skip_reason             TEXT,           -- below_threshold | below_threshold_known_contact
                                           -- | too_new | unread | flagged | unknown_folder
                                           -- | llm_unavailable | llm_api_error
                                           -- | llm_skip_sender | llm_skip_domain
                                           -- | llm_skip_known_contact | classification_error
    email_received_at       TEXT,            -- original receivedAt from JMAP (added in migration 8)
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (rule_id) REFERENCES rules(id)
);

CREATE INDEX idx_audit_email   ON audit_log(email_id);
CREATE INDEX idx_audit_thread  ON audit_log(thread_id);
CREATE INDEX idx_audit_domain  ON audit_log(from_domain);
CREATE INDEX idx_audit_created ON audit_log(created_at);
CREATE INDEX idx_audit_run     ON audit_log(run_id);
```

### Contacts Cache

```sql
CREATE TABLE contacts (
    email_address TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    relationship TEXT,          -- Optional override from config (e.g. "spouse", "parent")
    fastmail_uid TEXT,          -- ContactCard uid — used to detect updates
    refreshed_at TEXT NOT NULL
);

-- No index needed on email_address — it's the PRIMARY KEY
```

Populated during bootstrap and refreshed daily by the orchestrator via
`refresh_contacts()` in `classifier/features.py`. The refresh:

- Fetches all contacts via JMAP `ContactCard/get`
- Parses name maps (full → given+surname fallback) and email maps
- Merges `known_contact_overrides` from config (adds relationship hints for
  existing Fastmail contacts, and inserts override-only addresses that aren't
  in the address book so the `llm_move_known_contact` threshold applies to them)
- Uses per-contact error isolation (one bad record doesn't block the rest)
- Tracks last refresh time in `learner_state` table (at most once per 24h)
- Gracefully degrades if `urn:ietf:params:jmap:contacts` scope is unavailable

### Folder Descriptions

```sql
CREATE TABLE folder_descriptions (
    folder_path TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    source TEXT NOT NULL,  -- 'auto' (bootstrap-generated) | 'manual' (config override)
    generated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### Inbox Snapshot

Stores the set of email IDs seen in the inbox for each run, used by Category 3
(inbox departure detection) to diff between consecutive runs.

```sql
CREATE TABLE inbox_snapshot (
    email_id    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX idx_snapshot_run ON inbox_snapshot(run_id);
```

Populated by `learner.save_inbox_snapshot()` at the end of each run.
Cleaned up by `learner.cleanup_old_snapshots()` — rows older than 2 days are
removed to prevent unbounded growth.

### Learner State

Key-value store for learner bookkeeping (last folder scan time, live folder
paths for the web UI).

```sql
CREATE TABLE learner_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Known keys:
- `last_folder_scan` — ISO timestamp of the most recent Category 4 daily scan
- `last_contacts_refresh` — ISO timestamp of the most recent contact refresh
- `live_folder_paths` — JSON array of folder paths from the most recent run

### Schema Version

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

---

## Pydantic Models

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class EmailFeatures(BaseModel):
    """Extracted features from an email for classification."""
    email_id: str
    thread_id: str                       # JMAP threadId — used for thread context
    from_address: str
    from_domain: str
    to_addresses: list[str]
    subject: str
    list_id: Optional[str] = None
    list_unsubscribe: Optional[str] = None
    received_at: datetime
    preview: str
    keywords: list[str]            # JMAP keywords ($seen, $flagged, etc.)
    current_mailbox_ids: dict[str, bool]

class Classification(BaseModel):
    """Result of classifying an email."""
    folder_path: str
    folder_id: Optional[str] = None  # Resolved JMAP mailbox ID
    confidence: float
    source: str                      # "thread" | "rule" | "llm" | "manual"
    rule_id: Optional[int] = None
    reasoning: Optional[str] = None

class MoveDecision(BaseModel):
    """Final decision on whether and where to move an email."""
    email_id: str
    features: EmailFeatures
    classification: Classification
    should_move: bool
    skip_reason: Optional[str] = None  # below_threshold | below_threshold_known_contact
                                       # | too_new | unread | flagged | unknown_folder
                                       # | llm_unavailable | llm_skip_* | classification_error
```

---

## Migration Versioning

On startup, `migrations.py` checks the current version and applies any pending
migrations in order. Migrations are never skipped or re-applied.

```python
MIGRATIONS = [
    (1, "create_rules_table"),
    (2, "create_runs_table"),
    (3, "create_audit_log_table"),
    (4, "create_contacts_table"),
    (5, "create_folder_descriptions_table"),
]

def run_migrations():
    current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for version, name in MIGRATIONS:
        if version > current:
            apply_migration(version, name)
            db.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (version,))
            logger.info(f"Applied migration {version}: {name}")
```
