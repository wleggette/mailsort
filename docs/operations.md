# Operations & Deployment

Docker deployment, operational concerns, error handling, and monitoring.

## Docker

### Dockerfile

Because this project uses a `src/` layout, the package source must be copied
into the image before `pip install .` is executed. Otherwise the package may
not be importable at runtime.

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy package source and metadata — src/ must be present before pip install
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

EXPOSE 8025 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8025/health')" || exit 1

CMD ["mailsort", "start"]
```

The `start` command runs the scheduler, health check (port 8025), and web UI
(port 8080) in a single process. See `docs/dev/decisions.md` "Embed web UI in
scheduler process" for rationale.

### docker-compose.yml

```yaml
services:
  mailsort:
    build: .
    container_name: mailsort
    restart: unless-stopped
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    ports:
      - "8080:8080"
    environment:
      - FASTMAIL_API_TOKEN=${FASTMAIL_API_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - TZ=America/Chicago
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### .env file (not committed)

```
FASTMAIL_API_TOKEN=fmu1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Concurrent Run Protection

APScheduler may fire a new run while a previous one is still in progress (e.g.,
if a run takes longer than the scheduler interval). Two simultaneous runs would
process the same inbox emails and write conflicting audit records.

Two layers of protection:

1. **APScheduler `max_instances=1`** — prevents the scheduler from launching a
   second instance of the job while one is already running.
2. **`runs` table lock** — before starting work, insert a row into `runs` with
   `status='running'`. If a row with `status='running'` already exists, skip.

```python
def acquire_run_lock(run_id: str) -> bool:
    """Attempt to acquire the run lock by inserting a new run. Returns False if already running."""
    existing = db.execute(
        "SELECT run_id FROM runs WHERE status = 'running'"
    ).fetchone()
    if existing:
        logger.warning(f"Run {existing['run_id']} still in progress, skipping")
        return False
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger) VALUES (?, datetime('now'), 'running', 'scheduler')",
        (run_id,)
    )
    return True

def finish_run(run_id: str, status: str = "completed", error_summary: str = None):
    db.execute(
        "UPDATE runs SET status=?, finished_at=datetime('now'), error_summary=? WHERE run_id=?",
        (status, error_summary, run_id)
    )
```

On startup, reconcile stale runs — any row with `status='running'` from a
previous process is marked `abandoned`:

```python
def reconcile_stale_runs():
    """Mark interrupted runs as abandoned on startup."""
    db.execute("UPDATE runs SET status='abandoned' WHERE status='running'")
```

---

## Error Handling Design

Every I/O boundary (JMAP API, Anthropic API, SQLite) is wrapped so that
failures are logged, partial progress is preserved, and one bad email never
kills the entire batch. Four principles govern error handling:

### 1. Guaranteed audit logging

Classification decisions are always written to `audit_log`, even when the
JMAP move call crashes. The orchestrator wraps the move step in `try/except`
and writes audit rows in a `finally` block:

```python
outcomes: dict[str, bool] = {}
try:
    if planned and not dry_run:
        outcomes = jmap.move_emails(moves)
except Exception:
    logger.exception("JMAP move_emails failed — decisions will still be logged")
finally:
    # Always log, even on move crash. When outcomes is empty,
    # planned decisions are recorded as moved=False — accurate
    # since nothing was confirmed moved.
    audit.log_decisions(run_id, decisions, outcomes)
```

The outer `run_classification_pass` also catches any exception from the full
run body and calls `finish_run(status="failed")`, so the `runs` table always
reflects what happened.

### 2. Per-email isolation

A classification failure for one email must not prevent the remaining emails
from being processed. The orchestrator wraps each `pipeline.classify()` call:

```python
for features in eligible:
    try:
        classification, skip_reason = pipeline.classify(features)
    except Exception:
        logger.exception("Classification failed for %s, skipping", features.email_id)
        classification, skip_reason = None, "classification_error"
```

Within the pipeline itself, the thread context DB lookup and JMAP fallback
are each individually wrapped so failures degrade to the next classification
tier (rules, then LLM) rather than crashing.

### 3. Defensive audit writes

`AuditWriter.finish_run()` is called from exception handlers and must never
mask the original error. It catches and logs its own DB failures internally:

```python
def finish_run(self, run_id, *, status, ...):
    try:
        self._db.execute("UPDATE runs SET status=? ...", ...)
        self._db.commit()
    except Exception:
        logger.exception("Failed to write finish_run for %s", run_id)
```

`AuditWriter.log_decisions()` uses per-row isolation — if one insert fails
(e.g., constraint violation), remaining rows are still written:

```python
for d in decisions:
    try:
        self.log_decision(run_id, d, moved=moved)
        logged += 1
    except Exception:
        logger.exception("Failed to log audit row for %s", d.email_id)
```

### 4. Graceful degradation across tiers

Each I/O tier degrades independently without blocking the others:

| Tier | Failure mode | Behavior |
|------|-------------|----------|
| **JMAP query/fetch** | Network error, HTTP 5xx | Run marked `failed`, exception propagated to caller |
| **Thread context DB** | SQLite error | Logged, returns `None` — falls through to rule engine |
| **Thread context JMAP** | `Thread/get` or `Email/get` failure | Logged, returns `None` — falls through to rule engine |
| **Rule engine** | Bad regex in `subject_regex` rule | Logged per-rule, continues to next rule |
| **LLM (Anthropic)** | API timeout, rate limit, parse error | Returns `Classification(confidence=0.0, reasoning="api_error")` — email is skipped, not crashed |
| **JMAP move** | `Email/set` network error | Logged, `outcomes` stays empty, all decisions logged as `moved=False` |
| **Audit DB** | Insert/commit failure | Logged per-row, remaining rows still attempted |

Anthropic API failures are handled at the LLM classifier level — `classify()`
catches all exceptions and returns a safe default. Since rules don't need the
LLM, rule-matched emails can still be moved in the same run. Only emails that
require LLM classification are skipped.

---

## Deleted Folder Handling

If a Fastmail folder is renamed or deleted, rules pointing to it will have a
stale `target_folder_path` that no longer exists in the mailbox tree.
`RuleEngine.reconcile_folders()` (`classifier/rules.py`) compares active rules
against the live mailbox tree and deactivates any with a missing target.

**When it runs:**

- **Every classification pass** — the orchestrator calls `reconcile_folders`
  at the start of `_execute_run`, before the learning step, so stale rules
  never match during classification.
- **Every bootstrap** — called before rule creation and coverage calculation,
  ensuring deleted-folder evidence is excluded from both.

**What it affects:**

- **Rule deactivation:** active rules targeting a deleted folder are set to
  `active=0`. They are retained (not deleted) so they appear in the rules UI
  for review. If the folder was renamed, the rule can be manually updated and
  re-activated.
- **Bootstrap rule creation:** `_create_rules_from_evidence` filters out
  audit_log evidence pointing to folders not in the live tree, preventing
  rules from being created for deleted folders.
- **Bootstrap coverage:** `_calculate_coverage` excludes deleted-folder
  evidence from both the matched count and the total, so coverage percentage
  reflects only reachable folders.
- **Classification fallback:** if a rule somehow matches but the folder ID
  can't be resolved (e.g., folder deleted between reconciliation and
  classification), the email gets `skip_reason = "unknown_folder"` and is
  not moved.

---

## Database Migration Versioning

Migrations are tracked via a `schema_version` table. On startup, `migrations.py`
checks the current version and applies any pending migrations in order.
Migrations are never skipped or re-applied. See [design/data-models.md](design/data-models.md)
for full schema details.
