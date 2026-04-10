# Design Decisions

Log of architectural and design decisions that shaped the current
implementation. Explains WHY the code works the way it does. Reverse
chronological — newest entries first.

---

## 2026-04-08 — Eligibility gates override confidence gate skip_reason

**Context:** When a flagged email had below-threshold LLM confidence, the
confidence gate fired first (`skip_reason="below_threshold"`), and the
eligibility gate only ran when `should_move` was still True. The audit log
showed `below_threshold` instead of `flagged`, hiding the user-intent signal.

**Options considered:**
1. **Keep current order** (confidence gate → eligibility gate only if
   `should_move`). Simpler, but the audit log misleads: a flagged email
   looks like a confidence problem.
2. **Run eligibility gates unconditionally**, overriding any prior
   `skip_reason`. The audit log always reflects the user-intent signal.
3. **Run eligibility gates first**, before the confidence gate. Would prevent
   the confidence gate from running for ineligible emails — but then the audit
   log loses the confidence assessment for flagged/unread emails.

**Decision:** Option 2 — eligibility gates always run and override.

**Rationale:**
- User-intent signals (unread, flagged, too_new) represent observable state
  the user controls. They should be clearly visible in the audit log.
- The confidence gate still runs (classification quality is still assessed),
  but its `skip_reason` is overridden when an eligibility gate also applies.
- Option 3 was rejected because it loses the "what would mailsort do if this
  email were eligible?" information — the same audit visibility principle that
  drove the LLM cache decision.

**Affected files:** `orchestrator.py` (removed `if decision.should_move:` guard
on eligibility gates), `architecture.md` (Step 5 diagram), `classification.md`
(precedence paragraph), `system-test-plan.md` (S11 + X27).

---

## 2026-04-08 — LLM classification cache (cache only, no eligibility gate)

**Context:** Every run makes LLM API calls for emails that sit in the inbox
across multiple cycles — even if the classification hasn't changed. This wastes
tokens and adds latency.

**Options considered:**
1. **Eligibility gate before LLM** — skip LLM calls for unread/flagged/too-new
   emails by classifying them as `source="system"` immediately. Pro: fewer API
   calls. Con: loses audit visibility — ineligible emails show no classification.
2. **LLM cache only** — classify everything (thread → rules → LLM), but cache
   LLM results in the audit log keyed by a classification version hash. Reuse
   cached results on subsequent runs if the version hasn't changed. Pro: full
   audit visibility preserved. Con: first run still makes all LLM calls.
3. **Both** — eligibility gate + cache.

**Decision:** Option 2 — LLM cache only.

**Rationale:** Audit visibility is a core design principle. The eligibility gate
in option 1 means ineligible emails show `source="system"` with no real
classification, preventing analysis of what mailsort *would* do. The cache alone
provides the major cost saving (no repeated calls for the same email) without
sacrificing visibility.

**Key sub-decisions:**
- **Cache key:** `email_id` + `created_at >= classification_version_changed_at`.
  Uses the existing audit_log as storage — no separate cache table.
- **Version hash:** SHA-256 of `folder_descriptions + "\n" + llm_model`. Stored
  in `learner_state`. Timestamp updated only when the hash changes.
- **Pipeline split:** `classify_without_llm()` + `classify_llm()` so the
  orchestrator can interpose the cache check between rules and LLM.
- **`source="system"` fallback:** `build_move_decision` uses `source="system"`
  (not `"llm"`) when classification is None. This makes it clear in the audit
  log that no real classification occurred.
- **`cached` column:** `audit_log.cached BOOLEAN NOT NULL DEFAULT 0` tracks
  cache hits. `MoveDecision.cached` propagates to the writer.

---

## 2026-04-06 — Deduplicate analysis metrics by email_id

**Context:** The analysis page (`/analyze`) and CLI `mailsort analyze` count
`audit_log` rows, not distinct emails. If an email sits in the inbox for
multiple cycles (e.g., skipped as `below_threshold` three times before a rule
is created that moves it), it contributes three rows to the "skipped" total
and three to the LLM confidence histogram. This inflates totals and distorts
the source breakdown, especially for skipped emails.

**Options considered:**

1. **COUNT(DISTINCT email_id)** — simple dedup for totals, but ambiguous for
   attribution. Which source, confidence, and outcome do we assign to the
   email? The first? The last? An aggregate?
2. **Keep only the most recent audit row per email_id** — uses a CTE or
   subquery with `ROW_NUMBER() OVER (PARTITION BY email_id ORDER BY
   created_at DESC)` to select the latest decision for each email. The last
   decision reflects the final outcome (moved vs. skipped) and the source that
   ultimately handled it. Attribution is unambiguous.
3. **Do nothing** — the audit log is an event log; multiple rows per email are
   expected. Analysis consumers should understand this. But this makes the
   "emails classified" count misleading to users who read it as "how many
   emails did mailsort process."

**Decision:** Option 2 — deduplicate by taking the most recent audit row per
`email_id` within the analysis window. Applied to: overall totals (classified,
moved, skipped), source breakdown, and LLM confidence distribution.

**Rationale:**
- The analysis page answers "how well is mailsort performing?" The meaningful
  unit is the email, not the classification event. If an email was skipped
  twice then moved on the third cycle, the outcome is "moved" — not
  "2 skipped + 1 moved."
- The most recent row has the final outcome: if mailsort eventually moved
  the email, the earlier skipped rows are superseded. If it was skipped
  every time, the last skip is representative.
- Correction rows (`classification_source='correction'`) are already counted
  separately via `COUNT(DISTINCT email_id)` and are unaffected by this change.
- The audit log itself is not modified — dedup is applied only at query time
  for analysis metrics.

**Implementation:** A CTE `latest_per_email` selects the row with the highest
`a.id` per `email_id` (using `id` instead of `created_at` to avoid ties).
The base filter, source breakdown, and LLM confidence queries all join against
this CTE instead of raw `audit_log`.

**Affected files:** `src/mailsort/web/routes/analyze.py`,
`src/mailsort/main.py` (`_print_analysis`).

---

## 2026-04-05 — Folder description regeneration via `mailsort describe`

**Context:** Folder descriptions are generated once during bootstrap and never updated.
This causes problems when bootstrap descriptions are poor (small sample), folder purpose
evolves, or fallback descriptions persist from LLM unavailability. Since descriptions
feed the LLM classification prompt, stale descriptions degrade classification quality.

**Decision:** Add user-initiated regeneration via CLI (`mailsort describe`) and web UI
(per-folder and bulk buttons on `/folders`).

**Key design choices:**

- **No fallback on regeneration.** Initial generation falls back to "Emails filed under X"
  because something is better than nothing. Regeneration is user-initiated — replacing a
  reasonable LLM description with a generic fallback would be a regression. On LLM failure,
  the old description is preserved and the error is reported.
- **CLI subcommand name: `describe`** (not `regenerate-descriptions`). Short, clear verb
  that parallels existing short names (`run`, `analyze`, `bootstrap`). Supports `--folder`,
  `--pattern`, `--all`, and `--dry-run`.
- **JMAPClient per web request.** The web app doesn't hold a persistent JMAP client.
  Creating one per regeneration request adds ~200ms overhead but avoids lifecycle complexity
  (session expiry, cleanup). Acceptable since regeneration is infrequent.
- **Sample strategy: most recent emails.** `query_folder_emails` already sorts
  `receivedAt DESC`. Recent emails better represent a folder's current purpose, which is
  exactly the motivation for regeneration. Sample size stays at 15 (LLM prompt limit).
- **Manual overrides always skipped with warning.** Folders with
  `folder_description_overrides` in config are never touched by regeneration. A warning
  message tells the user which folders were skipped and why.
- **Sync POST + redirect for web UI.** A single Haiku call takes ~500ms. Even bulk
  regeneration of 20 folders is ~10s. No need for async/HTMX complexity on a maintenance
  action.

**Alternatives considered:**
- *Flag on bootstrap (`--regenerate-descriptions`)*: Rejected — overloads bootstrap
  semantics. Regeneration is destructive (overwrites); bootstrap is additive.
- *Shared JMAPClient on app.state*: Rejected for now — adds lifecycle management
  complexity for no practical benefit given regeneration frequency.
- *Configurable sample size*: Rejected — 15 is the existing LLM prompt size, keeping
  token cost minimal. Not worth a config knob for a rarely-used maintenance action.

---

## 2026-04-05 — Computed confidence model replaces static penalties

**Context:** Rules had static confidence set at creation, modified only by one-way
penalties (−0.15 per correction, −0.10/cycle for staleness). This created three problems:
(1) every rule was deactivated after a single correction (one-strike problem), (2) rules
that drifted in coherence kept firing because stored confidence never reflected live state,
(3) stale rules below the confidence gate entered a dead zone (can't fire → can't get a
hit → can't reset staleness → stuck forever).

**Decision:** Replace with a computed model:
`confidence = max(0, base × coherence × staleness − net_corrections × penalty)`.
Recomputed each cycle from live audit_log state. Bidirectional — confidence recovers
when conditions improve.

**Key design choices:**

- **Per-correction penalty (0.05) is volume-independent.** 3 corrections stops any rule
  regardless of evidence count. Avoids the dilution problem where high-volume rules absorb
  corrections through coherence alone.
- **Net corrections = corrections_against − confirming_sorts.** User sorting back cancels
  corrections 1:1. Corrections age out of the 30-day window automatically.
- **Corrections recorded as `classification_source='correction'`** with `rule_id` set to
  the firing rule. Only the rule that actually fired gets a correction; coherence handles
  cascade to broader rules naturally.
- **`last_relevant_at` replaces `last_hit_at`** for staleness. Tracks most recent email
  matching the rule's condition sorted to the target folder by any method (rule, LLM,
  manual). Fixes the dead zone: LLM or user sorts reset staleness even when the rule
  can't fire.
- **Deactivation at 0.50** (not at `rule_move`). Between 0.50 and 0.85, the rule stays
  `active=1` but doesn't fire — the confidence gate filters it. If conditions improve,
  it recovers without deactivation/reactivation.
- **Reactivation over duplication.** `find_rule_any_status` + `reactivate_rule` ensures
  one row per type+condition. No duplicate rules when evidence re-accumulates.
- **Manual rules exempt.** `source='manual'` rules are not recomputed.

**Alternatives considered:**

1. **Static confidence with one-way penalties (original).** Confidence set once, reduced by
   penalties. Never increases. Dead zones, one-strike kills, no recovery without re-creation.
2. **Confidence as one-way cap.** `min(current, max_from_coherence)`. Same dead-zone problem.
3. **Four-factor multiplicative.** `base × coherence × staleness × correction_ratio`. The
   ratio factor is diluted by volume — 1 correction / 200 emails = 0.995 factor. Invisible.
4. **Coherence-only (no separate correction signal).** 3 corrections / 200 emails = 98.5%
   coherence — rule keeps firing. Unacceptable response time to user feedback.
5. **Ratio-based correction penalty.** Same dilution as option 4.
6. **Check coherence at classification time.** DB query per rule per email. Couples
   classification latency to audit_log size.
7. **Event-driven re-evaluation.** Over-engineered for email sorting's 5-min cycle.

**Affected files:** `config.py`, `db/migrations.py` (M11), `audit/learner.py`,
`classifier/rules.py`, `orchestrator.py`, `main.py`, `web/routes/rules.py`,
`web/templates/rules/detail.html`, `config.yaml.example`.

---

## 2026-04-03 — Auto-downgrade to dry run on read-only token

**Context:** Users may configure a read-only JMAP API token (intentionally or
by accident). Without detection, every email in a live run would attempt a
move, fail with `ReadOnlyTokenError`, and be logged as `move_failed` — noisy
and wasteful.

**Decision:** Check `jmap.is_read_only` at the start of
`run_classification_pass`. If the token is read-only and `dry_run=False`,
automatically switch to dry-run mode. Return a `RunResult` dataclass so
callers can display the downgrade to the user.

**Options considered:**
1. **Fail fast with an error** — rejects the run entirely. But the
   classification output (audit log, rule hits) is still valuable for
   debugging and setup. Losing it would be wasteful.
2. **Warn and proceed with live mode** — every email hits `move_failed`.
   Generates noise in audit log and error summaries.
3. ✅ **Auto-downgrade to dry run** — preserves all classification output,
   avoids move errors, and clearly signals the read-only state to the user.
   No data loss, no wasted JMAP calls.

**Scope:** The check runs once per `run_classification_pass` call, before any
audit or classification work. Explicit `dry_run=True` is never flagged as a
downgrade (`read_only_downgrade=False`).

---

## 2026-04-02 — Exclusive lock for live runs

**Context:** During `docker compose up --build -d`, two container instances
overlapped for ~90 seconds. Both ran live classification passes against the
same SQLite database, producing duplicate runs and conflicting audit entries.
SQLite WAL mode prevented corruption, but the application logic broke.

**Decision:** Two-layer approach:

1. **`fcntl.flock`** on `data/mailsort.run.lock` — true mutual exclusion
   within a single kernel. Auto-releases on crash/SIGKILL. Callers acquire
   the lock early (before JMAP setup) and release in `finally`.
2. **CLI Docker delegation** — when the local CLI detects a running `mailsort`
   Docker container, it delegates the command via `docker exec` instead of
   running locally. This ensures all runs happen inside the same Linux kernel
   where `flock` works.

**Options considered:**
1. **Lock in `Database.connect()`** — too broad, blocks web UI and dry runs
2. **Lock in each caller without shared helper** — duplicates logic, easy to
   miss a call site
3. **PID file** — stale after crashes, needs cleanup logic
4. **`fcntl.flock` alone** — auto-releases on exit, non-blocking. Works
   perfectly within a single kernel, but `flock` locks are per-kernel and
   don't propagate across Docker Desktop's VM boundary (macOS host ↔ Linux
   container).
5. **SQLite `BEGIN EXCLUSIVE` on a dedicated lock database** — SQLite's file
   locking also uses per-kernel `fcntl` locks under the hood; same Docker
   Desktop VM boundary problem.
6. **Data-level check on the `runs` table** — queries `status='running'` rows.
   Works across Docker, but if a run crashes without `finish_run`, the stale
   row blocks all runs for up to 30 minutes (the reconciliation threshold).
   Operational hazard.
7. ✅ **`flock` + CLI Docker delegation** — `flock` provides true mutual
   exclusion; Docker delegation ensures all runs share a kernel. No stale
   locks, no TOCTOU race, no crash lockout.

**Scope rule:** Only live runs (`dry_run=False`) acquire the lock.
`dry_run=True` always proceeds. `reconcile_stale_runs` unconditionally
abandons all `status='running'` rows — with `flock`, any such row from a
prior process is genuinely stale.

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
2. Filter through `_already_corrected_email_ids()` (now `_already_handled_email_ids()`)
   before JMAP fetch — matches the dedup pattern already used in
   `_detect_correction_sorts`.

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
classification logging), but should the `hit_count` column be updated?
(Originally also applied to `last_hit_at`, now renamed to `last_relevant_at`
and updated by `compute_rule_confidence()` rather than rule hit recording.)

**Decision:** No — dry runs skip hit recording.

**Rationale:**
- `hit_count` is used in the web UI to show rule activity.
- `last_relevant_at` is updated by `compute_rule_confidence()` from audit_log
  data, not by rule hit recording. Dry runs still run learning/confidence
  computation, so `last_relevant_at` is updated regardless.
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
