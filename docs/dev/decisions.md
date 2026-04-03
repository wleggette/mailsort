# Design Decisions

Log of architectural and design decisions that shaped the current
implementation. Explains WHY the code works the way it does. Reverse
chronological — newest entries first.

---

## 2026-04-02 — Exclusive file lock for live runs

**Context:** During `docker compose up --build -d`, two container instances
overlapped for ~90 seconds. Both ran live classification passes against the
same SQLite database, producing duplicate runs and conflicting audit entries.
SQLite WAL mode prevented corruption, but the application logic broke.

**Decision:** Live runs acquire an exclusive `fcntl.flock` on
`data/mailsort.run.lock` (derived from `db_path`). Dry runs, the web UI,
and CLI read commands never acquire the lock.

**Options considered:**
1. **Lock in `Database.connect()`** — too broad, blocks web UI and dry runs
2. **Lock in each caller** — duplicates logic across scheduler/CLI
3. **SQLite advisory table** — race-prone between two processes
4. **PID file** — stale after crashes, needs cleanup logic
5. ✅ **`fcntl.flock` in `run_classification_pass`** — single acquisition
   point, auto-releases on exit/crash, non-blocking (`LOCK_NB`)

**Scope rule:** Only `run_classification_pass(dry_run=False)` acquires the
lock. `dry_run=True` always proceeds. This allows read-only observation runs
to overlap with anything.

**Defense-in-depth:** `docker-compose.yml` also sets `stop_grace_period: 180s`
so Docker waits up to 3 minutes for the old container to finish before
starting the new one. The lock is a second layer for abnormal cases.

---

## 2026-04-02 — Fix false-positive skipped-sort detection in learner

**Context:** System tests revealed that `_detect_skipped_sorts` was creating
spurious `classification_source='manual'` audit entries for emails that mailsort
itself had moved. This happened because the SQL query only checked for `moved=0`
entries without excluding emails that had a subsequent `moved=1` entry from a
live run. The false manual entries then poisoned the dedup check in
`_detect_correction_sorts`, preventing real user corrections from being detected
and rule confidence penalties from being applied.

**Root causes:**
1. The query `WHERE moved = 0` matched dry-run entries even when the same email
   had a `moved=1` entry from a later live run.
2. Unlike `_detect_correction_sorts`, `_detect_skipped_sorts` had no dedup check
   against existing manual audit rows, so it re-created the same entry every run.

**Decision:** Two minimal fixes in `_detect_skipped_sorts`:
1. Add `AND email_id NOT IN (SELECT email_id FROM audit_log WHERE moved = 1
   AND classification_source != 'manual')` — excludes emails **mailsort** moved
   but not emails the **user** moved. A blanket `NOT IN (moved = 1)` would also
   exclude the learner's own manual entries, blocking re-detection if the user
   moves an email out, back to inbox, then out again.
2. Filter through `_already_corrected_email_ids()` before JMAP fetch — matches
   the dedup pattern already used in `_detect_correction_sorts`.

**Alternatives considered:**
- Blanket `NOT IN (moved = 1)` — simpler but blocks re-detection after a user's
  move-and-return cycle (manual entries have `moved=1` too).
- Scoping the `NOT IN` subquery to the lookback window — rejected because an
  email moved by mailsort at any time should not be re-detected as a user sort.
- Adding a `source_run_id` column to distinguish which run created the `moved=0`
  entry — over-engineered; the `NOT IN` subquery is simpler and sufficient.

**Affected files:** `src/mailsort/audit/learner.py`, `tests/test_learner.py`

**Status:** Implemented and verified (unit tests + full test suite).

---

## 2026-04-02 — Distinguish "move failed" from "dry run" in audit outcomes

**Context:** When `move_emails` raises `ReadOnlyTokenError` (or any JMAP
error), the orchestrator catches the exception, logs decisions with
`moved=0` and `skip_reason=NULL`, and finishes the run with
`status='completed'`. The UI template's fallthrough logic shows these
entries as "dry run" — misleading, since the intent was to move.

**Options considered:**
1. **Per-entry `skip_reason='move_error'`** — accurate per row, but no
   run-level visibility.
2. **Run-level error indicator only** — banner on dashboard, but individual
   entries still look like dry runs.
3. **Both A + B** — per-entry skip_reason AND run-level error status.
4. **Distinct `move_failed` status** — set `skip_reason='move_failed'` on
   entries whose moves were attempted but failed (API error or per-email
   failure), set run status to `'error'` (not `'failed'` — the run itself
   completed, but moves didn't land), and add an "Errors" column to the
   dashboard run table.

**Decision:** Option 4 — per-entry `skip_reason='move_failed'` plus
run-level `status='error'` with an errors count column.

**Rationale:**
- "dry run" should only appear when `dry_run=True` was explicitly passed.
- "move_failed" is a distinct semantic: classification succeeded, move
  didn't. Users need to see this to diagnose token/permission issues.
- Run status `'error'` (vs `'failed'`) distinguishes "moves didn't work"
  from "run crashed before completing" (which stays `'failed'`).
- An errors column in the run table gives at-a-glance visibility without
  needing to drill into individual entries.

**Affected code:** `orchestrator.py` (catch move errors, set skip_reason),
`audit/writer.py` (finish_run with `'error'` status), `dashboard.html`
(errors column, error badge), `audit/list.html` ("move failed" outcome
badge).

---

## 2026-04-02 — Remove manual immediate run from scheduler startup

**Context:** `start_scheduler` adds an APScheduler interval job, then calls
`_scheduled_run()` manually before `scheduler.start()`. APScheduler's
interval trigger defaults `next_run_time` to `now` (time of `add_job`). The
manual call takes ~3 minutes; when `scheduler.start()` fires, APScheduler
sees the first fire time is in the past and immediately executes the job —
producing a duplicate run ~3 minutes after the manual one.

**Options considered:**
1. **Remove manual call, let APScheduler handle it** — APScheduler already
   fires at `next_run_time=now` when `scheduler.start()` is called.
   Simplest fix.
2. **Keep manual call, push APScheduler's first fire to `now + interval`**
   via `next_run_time` parameter. More code, same result.

**Decision:** Option 1, refined — remove the manual `_scheduled_run()` call
(lines 66–70 of scheduler.py) and add `next_run_time=datetime.now(timezone.utc)`
to `add_job()` so APScheduler fires the first run immediately on `start()`.

**Rationale:**
- APScheduler's interval trigger defaults `next_run_time` to `now + interval`,
  NOT `now`. The manual call existed because without it, the first run would be
  delayed by a full interval. But having both created duplicates.
- Explicit `next_run_time=now` gives immediate-on-start behavior with no
  duplicate. APScheduler then schedules subsequent runs at `now + interval`.
- `max_instances=1` already prevents overlap if the first run is slow.

**Affected code:** `scheduler.py` (remove lines 66–70, add `next_run_time`
to `add_job`).

---

## 2026-03-26 — Embed web UI in scheduler process

**Context:** `mailsort start` runs the scheduler + health check in a single
process. `mailsort web` runs the web UI as a separate process. The PRD's
"set-and-forget Docker container" user story requires both to be available
from a single `docker compose up`. Currently this needs either two containers,
a process manager like supervisord, or a shell script that backgrounds one.

**Options considered:**
1. **Two containers** sharing a SQLite volume — clean separation, but complex
   for a single-user tool (compose file, shared DB locking concerns, two
   images to manage).
2. **Process manager (supervisord)** — reliable but adds a dependency and
   config file for something simple.
3. **Background thread** — start Uvicorn in a daemon thread alongside the
   scheduler, same pattern as the existing `start_health_server()` which
   runs an HTTP server in a background thread.

**Decision:** Option 3 — embed the web UI in the scheduler process as a
background daemon thread.

**Rationale:**
- The health check server already demonstrates this pattern works reliably.
- Single process = single container = simple deployment.
- SQLite is accessed from both the scheduler thread and the web thread, but
  SQLite handles concurrent readers fine (WAL mode), and the web UI is
  read-mostly.
- `mailsort web` is preserved as a standalone command for development (run
  the UI without the scheduler).
- Configurable via `scheduler.web_port` (default 8080, set to 0 to disable).

**Affected code:** `scheduler.py` (add `_start_web_server`), `config.py`
(add `web_port`), `Dockerfile` (expose 8080), `docker-compose.yml` (port
mapping).

---

## 2026-03-25 — Auto-detect test account email from JMAP session

**Context:** The system test scripts (`run_system_test.py`, `load_fixtures.py`)
require a `--to-email` address for constructing the `To:` header on fixture
emails injected via JMAP `Email/import`. This is not needed for authentication
(that's the API token) nor for mailsort itself (which only reads existing
emails). Requiring it as a mandatory CLI argument is friction for running tests.

**Options considered:**
1. **Keep `--to-email` required** — explicit, but annoying since the token
   already identifies the account.
2. **Add `FASTMAIL_TEST_EMAIL` env var** — less CLI friction, but another env
   var to manage alongside `FASTMAIL_API_TOKEN`.
3. **Auto-detect from JMAP session** — Fastmail's session response includes
   `accounts[id].name` which is the account email address. Derive it at
   runtime; keep `--to-email` as an optional override.

**Decision:** Option 3 — auto-detect from JMAP session, keep `--to-email` as
optional override.

**Rationale:**
- Zero-config for the common case (one test account, one token).
- `--to-email` still works for edge cases (shared account, non-standard setup).
- The JMAP session is already fetched during setup — no extra API call.
- Fastmail reliably exposes the email as `accounts[].name`. If a provider
  doesn't, the `account_email` property raises a clear error telling the user
  to pass `--to-email` explicitly.

**Affected code:** `tests/system/load_fixtures.py` (`JMAPLoader.account_email`
property, `--to-email` now `default=None`), `tests/system/run_system_test.py`
(`--to-email` now `default=None`, `phase_setup` resolves via
`loader.account_email` fallback).

---

## 2026-03-21 — System test vs unit test boundary for bootstrap scenarios

**Context:** Some bootstrap behaviors (deleted folder evidence filtering,
coverage calculation accuracy, per-contact error isolation) are documented
in the architecture phase cards but not tested in the system test plan.
Should they be system tests or unit/integration tests?

**Decision:** Cover these in unit/integration tests, not in the system
test plan.

**Rationale:** System tests add value when they exercise **real network
I/O, real JMAP behavior, or real LLM non-determinism** — things that
mocks can't faithfully reproduce. These three scenarios are pure logic
that is fully testable with mocks:

- **Deleted folder filtering** — a set membership check
  (`target_folder in live_folders`). The integration test creates two
  folder trees and verifies the rule is deactivated. No JMAP needed.
- **Coverage calculation** — calls `classify()` against a DB. No
  network I/O. The integration test verifies exact match/unmatch counts.
- **Per-contact error isolation** — requires injecting a malformed
  ContactCard, which isn't possible via normal JMAP. The unit test mocks
  a bad record alongside a good one.

A system test for these would add setup complexity (multi-step JMAP
operations, folder creation/deletion) without catching bugs that the
unit tests miss. The system test plan notes these as "covered by
unit/integration tests" with references to the specific test functions.

**Affected docs:** `docs/planning/system-test-plan.md` §3.4,
`docs/architecture.md` bootstrap phase cards (Covered by: field).

---

## 2026-03-21 — Create all eligible rules (not priority-blocks)

**Context:** When a sender qualifies for multiple rule types (e.g., has a
list-id AND enough emails for an exact_sender rule), should we create only
the highest-priority rule, or all eligible rules?

**Options considered:**
1. **Priority blocks** — create list_id rule, skip exact_sender since list_id
   covers it. Simpler rule table, but if the list_id rule decays or gets
   deactivated, the sender has no fallback.
2. **Create all eligible** — create both list_id and exact_sender independently.
   Classification-time priority (list_id > exact_sender > sender_domain)
   determines which fires. More rules in the table, but fallback resilience.

**Decision:** Option 2 — create all eligible rules independently.

**Rationale:**
- If a broader rule is deactivated (confidence decay, correction penalty),
  the narrower rule still covers the sender.
- The rule detail UI shows all evidence-backed rules, giving full visibility.
- No behavioral difference at classification time — priority still resolves.
- Small cost: slightly more rules in the DB, slightly more DB queries during
  `maybe_create_rule`. Negligible.

**Affected code:** `audit/learner.py` (`maybe_create_rule` returns `list[int]`),
`bootstrap.py` (`_create_rules_from_evidence` removed dedup).

---

## 2026-03-21 — Dry runs do not record rule hit_count

**Context:** During `mailsort dry-run`, rules still match emails (for
classification logging), but should the `hit_count` and `last_hit_at`
columns be updated?

**Decision:** No — dry runs skip hit recording.

**Rationale:**
- `hit_count` is used in the web UI to show rule activity.
- `last_hit_at` is used by confidence decay (rules not hit in 90+ days lose
  confidence). A dry run resetting this clock would prevent legitimate decay.
- Dry runs should be read-only from the rules perspective — observe but don't
  mutate.

**Affected code:** `classifier/rules.py` (`record_hits` parameter on
`RuleEngine`), `orchestrator.py` (passes `record_hits=not dry_run`).

---

## 2026-03-21 — Bootstrap rule source is 'auto', not 'bootstrap'

**Context:** Bootstrap creates rules by delegating to the same
`learner.maybe_create_rule` used during live learning. Should bootstrap
rules have a distinct `source` value?

**Decision:** No — bootstrap rules use `source='auto'`, same as live-learned
rules.

**Rationale:** Bootstrap uses the exact same coherence checks and thresholds
as live learning. There's no behavioral difference. A separate source value
would add complexity without value — the `runs` table already distinguishes
bootstrap runs via `trigger='bootstrap'`.
