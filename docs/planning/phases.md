# Development Phases

## Phase 1: Foundation ✅
- [x] Project scaffolding (`pyproject.toml`, `src/` layout, hatchling build)
- [x] Config loading with pydantic (`config.py`, `config.yaml`, env var secrets)
- [x] JMAP client: session discovery, auth, method calls (`jmap/client.py`)
- [x] Mailbox tree discovery and path resolution (`jmap/mailbox_tree.py`)
- [x] Email querying with eligibility filters (`query_inbox_emails`)
- [x] SQLite database setup with versioned migrations (`db/database.py`, `db/migrations.py`)

## Phase 2: Classification ✅
- [x] Feature extractor (`classifier/features.py`)
- [x] Rule engine: CRUD, specificity-ordered matching (`classifier/rules.py`)
- [x] LLM classifier with structured prompt + privacy gate (`classifier/llm.py`)
- [x] Classification pipeline: thread context → rules → LLM (`classifier/pipeline.py`)
- [x] Confidence gate logic (`mover/mover.py`)

## Phase 3: Moving & Logging ✅
- [x] Email mover: batch `Email/set` via `move_emails` (`jmap/client.py`)
- [x] Audit log writer with run lifecycle (`audit/writer.py`)
- [x] Run orchestrator: full classify → move → log pass (`orchestrator.py`)
- [x] Dry-run mode: `mailsort dry-run` CLI command (`main.py`)
- [x] Error handling: per-email isolation, guaranteed audit logging, defensive
      DB writes (see [operations.md](../operations.md) Error Handling Design)
- [x] Undo via keyword tagging: `$mailsort-moved` keyword added to moved emails
      via JMAP patch in `Email/set` (`jmap/client.py`)

## Phase 4: Learning ✅
- [x] Bootstrap: scan existing folders → seed rules + folder descriptions
      (`bootstrap.py`, `mailsort bootstrap` CLI command)
- [x] Manual sort detection — four categories (`audit/learner.py`):
  - [x] Category 1: skipped emails the user moved out of inbox
  - [x] Category 2: mailsort-moved emails the user relocated
  - [x] Category 3: inbox departures via snapshot diff (emails sorted before
        mailsort processed them) — `inbox_snapshot` table, migration 7
  - [x] Category 4: daily folder scan for emails with no audit_log record
        (`learner_state` table tracks last-scan time)
- [x] Auto-rule generation from repeated patterns: all eligible rule types
      created independently — list_id, sender_domain (with coherence check),
      exact_sender — classification-time priority decides which fires
      (`audit/learner.py`)
- [x] Computed confidence model: `confidence = max(0, base × coherence × staleness − net_corrections × penalty)`.
      Base from `BaseConfidenceConfig` per rule type, coherence from 30-day window,
      staleness from `last_relevant_at` (365d threshold), corrections net of confirming
      sorts. Deactivation at 0.50. All thresholds configurable. (`audit/learner.py`)
- [x] Feedback loop: corrections recorded as `classification_source='correction'` with
      `rule_id`. Computed confidence model handles penalty via `compute_rule_confidence()`.
      Dedup via `_already_handled_email_ids` (allows re-correction after new rule move).
- [x] Reactivation over duplication: `find_rule_any_status` + `reactivate_rule` prevents
      duplicate rule rows when evidence re-accumulates for an inactive rule.
- [x] Schema: `last_hit_at` → `last_relevant_at` (migration 11), `hit_count` retained
      for display only.

## Phase 5: Scheduling & Deployment ✅
- [x] APScheduler integration: `BlockingScheduler` with `max_instances=1`,
      runs on configurable interval (`scheduler.py`, `mailsort start` CLI)
- [x] Dockerfile and docker-compose (`Dockerfile`, `docker-compose.yml`)
- [x] Graceful shutdown: SIGTERM/SIGINT handlers stop the scheduler cleanly
      (`scheduler.py`)
- [x] Health check endpoint: `GET /health` on configurable port (default 8025),
      returns JSON with last run status, Docker HEALTHCHECK wired up
      (`health.py`, `scheduler.health_check_port` config)

## Phase 6: Observability & Tuning
- [x] Structured logging: JSON or text format via `logging_config.format` config
      toggle (`main.py` `_JSONFormatter`)
- [x] Export-rules: `mailsort export-rules [--inactive]` dumps rules to YAML
      (`main.py`)
- [x] Confidence threshold analysis: `mailsort analyze [--days N]` shows
      classification sources, move outcomes, LLM confidence distribution,
      skipped-then-manually-sorted stats, and recommendations (`main.py`)
- [x] Web UI for reviewing audit log and rules — see Phase 7 / [web-ui.md](../design/web-ui.md)

## Phase 7: Web UI
See [design/web-ui.md](../design/web-ui.md) for detailed implementation checklist.
