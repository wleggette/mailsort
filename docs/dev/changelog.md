# Development Changelog

Running log of significant changes, decisions, and rationale. Reverse
chronological — newest entries first.

---

## 2026-04-03 — Fix: system test age gate timing + Docker intercept

**What changed:**
- **fix:** Age gate test no longer relies on a fixture email loaded at setup
  time (whose `received_at` was either always-too-new or already-old-enough
  by the time the age gate phase ran). Instead, `phase_age_gate` injects a
  fresh email with `received_at=now` right before step 1, guaranteeing it's
  too_new initially and eligible after the min_age wait.
- **fix:** E4 fixture email (`BofA too new`) reverted to `now + 5min` —
  always too_new, used only by dry-run and too-new-blocked verification.
- **fix:** `verify_age_gate` now matches "age gate" in subject (the freshly
  injected email) instead of "too new" (the always-blocked fixture).
- **fix:** `verify_too_new_blocked` now checks both the E4 fixture and the
  age-gate email are blocked at step 1.

**Root causes identified:**
1. Running Docker container (`mailsort`) intercepted all CLI commands,
   writing run records to the container's DB instead of `data/test.db`.
2. Stale test emails from prior runs corrupted bootstrap coherence.
3. E4's `received_at = now + 5min` could never pass the age gate within
   the test's 65-second wait window.

**Files:** `generate_inbox_emails.py`, `run_system_test.py`,
`verify_results.py`

---

## 2026-04-03 — Feat: auto-downgrade to dry run on read-only token

**What changed:**
- **feat:** `run_classification_pass` now checks `jmap.is_read_only` before
  executing a live run. If the JMAP token lacks write permissions,
  the run is automatically downgraded to dry-run mode instead of
  failing with `move_failed` errors on every email.
- **feat:** New `RunResult` dataclass returned by `run_classification_pass`
  carries `run_id`, effective `dry_run` flag, and `read_only_downgrade`
  indicator.
- **feat:** CLI displays `[DRY RUN — read-only token]` in the summary
  when auto-downgraded. Scheduler logs a warning with the run ID.
- **schema:** M10 migration adds `dry_run BOOLEAN NOT NULL DEFAULT 0` to
  `runs` table. `start_run` stores the effective dry-run mode.
- **fix:** `reconcile_stale_runs` now only abandons live runs (`dry_run=0`).
  Dry-run rows are left alone since they don't hold a lock.
- **ui:** Dashboard shows blue "dry run" badge instead of green "completed"
  for dry-run runs (both explicit and auto-downgraded).
- **ui:** Settings page Fastmail card now shows Account ID, Capabilities,
  Contacts availability, and Permissions (READ-ONLY / READ-WRITE).
  Removed "(read-only)" from page subtitle.
- **test:** 4 auto-downgrade tests (downgrade triggers, writable proceeds,
  explicit dry-run not flagged, record_hits skipped on downgrade).
  All existing tests updated for `RunResult` return type.

---

## 2026-04-02 — Feat: prevent concurrent live runs

**What changed:**
- **feat:** Live runs (`dry_run=False`) now acquire an exclusive `flock` on
  `data/mailsort.run.lock` before proceeding. Lock is acquired early (before
  JMAP setup) so a second instance fails fast. Dry runs, the web UI, and
  CLI read commands are unaffected.
- **feat:** CLI `run` and `dry-run` commands now detect a running `mailsort`
  Docker container and delegate via `docker exec`. This ensures all runs
  happen inside the same Linux kernel where `flock` works, solving the
  Docker Desktop VM boundary problem.
- **fix:** `docker-compose.yml` now sets `stop_grace_period: 180s` to give
  in-flight runs time to finish before Docker force-kills the container during
  `docker compose up --build`.
- **test:** 4 lock tests (acquire, dry-run bypass, release, exception safety).
  6 Docker delegation tests (container detection, exec delegation).
- **docs:** System test plan updated with X18–X20 entries (all deferred to
  unit test).

**Note:** `flock` and SQLite `BEGIN EXCLUSIVE` locks don't propagate across
Docker Desktop's macOS host ↔ Linux VM boundary. Docker delegation solves
this by ensuring all runs execute inside the container.

---

## 2026-04-02 — Fix: learner false-positive skipped-sort detection

**What changed:**
- **fix:** `_detect_skipped_sorts` no longer misidentifies emails that mailsort
  moved in a later run as user manual sorts. The SQL query now excludes emails
  with any non-manual `moved=1` audit entry (`classification_source != 'manual'`).
  Previously, a dry-run `moved=0` entry followed by a live-run move would cause
  the learner to create a spurious `manual` audit row — which then blocked real
  correction detection via dedup. The exclusion is scoped to non-manual entries
  so that a user's own move-and-return-to-inbox cycle doesn't permanently block
  re-detection.
- **fix:** `_detect_skipped_sorts` now deduplicates against existing `manual`
  audit rows (matching `_detect_correction_sorts` behavior). Previously it would
  re-create the same manual entry every run.
- **test:** Added unit tests for learner edge cases: mailsort-moved exclusion,
  user move-and-return re-detection, and dedup of duplicate manual rows
  (`test_learner.py`: L2a, L2b, L2c).
- **test:** Added unit tests for in-flight race windows: email vanishes between
  query and fetch (X15), partial move success (X16), email absent from move
  response (X17) (`test_orchestrator.py`).
- **docs:** System test plan updated with L2a–L2c and X15–X17 entries (all
  deferred to unit test with rationale).

---

## 2026-04-02 — Fix: move_failed status + scheduler double-runs

**What changed:**
- **fix:** JMAP move failures (e.g. read-only token) now set
  `skip_reason='move_failed'` on affected audit entries and finish the run with
  `status='error'`. Previously these showed as "dry run" in the UI and the run
  completed as `status='completed'` — masking the real problem.
- **fix:** Removed duplicate initial run on scheduler startup. The manual
  `_scheduled_run()` call before `scheduler.start()` caused APScheduler to fire
  a second run immediately (its default `next_run_time=now` caught up). Now
  APScheduler handles the immediate first run on its own.
- **feat:** Dashboard "Recent Runs" table now has an "Errors" column showing
  the count of `move_failed` entries per run, with red highlighting.
- **feat:** Dashboard and audit list show distinct "error" status badge (red)
  for runs where moves failed.
- **schema:** Migration M9 rebuilds the `runs` table to add `'error'` to the
  status CHECK constraint.

---

## 2026-03-21 — PRD refinement & List-Unsubscribe analysis

**What changed:**
- Fleshed out PRD: added user stories (set-and-forget deployment, verify
  accuracy), expanded in-scope (inbox visibility/threshold analysis), added
  12 out-of-scope items with rationale.
- Ran data analysis on `List-Unsubscribe` header prevalence across 2,628
  emails. Found only 1.7% true gap — not worth implementing. Full findings
  captured in `docs/dev/design-ideas.md`.
- Created `docs/dev/design-ideas.md` for capturing explored-but-deferred
  feature ideas with enough context to revisit later.
- Created `scripts/analyze_list_unsubscribe.py` for rerunning the analysis.
- Synced all docs: updated cross-references in open-questions.md, cleaned up
  implemented strikethroughs, added design-ideas.md to README/architecture/
  windsurf rules.

---

## 2026-03-21 — Documentation restructure

**What changed:** Migrated monolithic `ARCHITECTURE.md` (2365 lines) into a
structured `docs/` directory.

**New structure:**
```
docs/
  prd.md                    — Product requirements, goals, scope
  architecture.md           — High-level diagrams and sequences
  design/
    jmap-integration.md     — JMAP auth, methods, mailbox tree
    classification.md       — Pipeline, rules, LLM, confidence gate
    learning.md             — Manual sort detection, auto-rules, bootstrap
    audit.md                — Audit log, run lifecycle, reporting
    data-models.md          — DB schema, Pydantic models, migrations
    web-ui.md               — Web UI plan and implementation checklist
  configuration.md          — Config reference
  operations.md             — Docker, deployment, error handling
  planning/
    phases.md               — Dev phases checklist
    open-questions.md       — Future work
    system-test-plan.md     — E2E test plan
  dev/
    changelog.md            — This file
    scratch.md              — Working notes
```

**Why:** The monolithic file made it hard to find and update specific sections.
Each design doc now maps to a code package (`jmap/`, `classifier/`, `audit/`,
etc.) and can evolve independently.
