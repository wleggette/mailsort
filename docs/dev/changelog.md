# Development Changelog

Running log of significant changes, decisions, and rationale. Reverse
chronological — newest entries first.

---

## 2026-04-27 — Feat: Analysis page redesign with action-oriented cards

**What changed:**
- **fix:** Skipped-then-sorted query deduplicated by email_id. Emails
  classified across N cycles now produce 1 result, not N. Previously
  reported 1,534 rows for 41 actual emails (~37× inflation).
- **feat:** Analysis page replaced flat "Skipped Emails You Later Sorted"
  table with 5 action-oriented cards:
  - **Folder Description Gaps** — wrong-folder emails grouped by destination,
    with "Review description" links to /folders.
  - **Known Contact Sorting** — per-contact breakdown of sorting mechanisms,
    folder coherence, and threshold-blocked emails.
  - **Learning Effectiveness** — auto rule counts, hit counts, recently
    created rules with link-through to /rules.
  - **Eligibility-Gated Emails** — flagged/unread/too_new breakdown.
  - **LLM Accuracy Summary** — tree structure + 3 precision metrics
    (system effectiveness, move precision, threshold precision) with
    adaptive color thresholds.
- **feat:** Rules page: added `created_days` filter parameter and `Created`
  column (sortable). Enables link from analysis page "N rules created in
  last Xd" to `/rules?filter=all&created_days=30`.
- **feat:** Folders page: added `?highlight=` query param support. Scrolls
  to and highlights specified folders (yellow background). Used by "Review
  description" and "Compare descriptions" links on analysis page.
- **feat:** New config field `classification.min_known_contact_skips`
  (default 3) — minimum threshold-blocked emails before showing a known
  contact card on /analyze.
- **test:** First web UI test file (`tests/test_web_analyze.py`) — 15 tests
  covering query-level dedup, card building, metric calculations, and
  route-level rendering.

---

## 2026-04-08 — Fix: Eligibility gates override confidence gate skip_reason

**What changed:**
- **fix:** Eligibility gates (unread, flagged, too_new) now run unconditionally,
  overriding confidence-gate `skip_reason`. A flagged email with below-threshold
  LLM confidence shows `skip_reason="flagged"` in the audit log, not
  `"below_threshold"`. User-intent signals always take precedence.
- **fix:** LLM cache timestamp format mismatch — `_update_classification_version`
  used ISO `T` separator but `audit_log.created_at` uses SQLite's space-separated
  format. Cache lookups always missed. Fixed to space-separated format.

**Files:** `orchestrator.py`, `architecture.md`, `classification.md`,
`system-test-plan.md`, `verify_results.py`, `test_orchestrator.py`

---

## 2026-04-08 — Feat: LLM classification cache + system source

**What changed:**
- **feat:** LLM classification cache avoids redundant API calls for emails that
  stay in the inbox across runs. On each run, a classification version (SHA-256
  of folder descriptions + LLM model) is computed. If a prior LLM audit row
  exists for the same email after the version timestamp, its result is reused.
- **feat:** `build_move_decision` fallback uses `source="system"` (was `"llm"`)
  when classification is None (LLM unavailable/error/gated).
- **feat:** `audit_log.cached` column tracks cache hits (migration 12). The
  `MoveDecision.cached` flag propagates through to the audit writer.
- **feat:** Pipeline split: `classify_without_llm()` (thread + rules) and
  `classify_llm()` (LLM only), with the orchestrator interposing the cache check.
- **refactor:** Dedup CTEs in analyze.py and main.py now exclude `'system'` rows
  alongside `'manual'`.
- **ui:** Amber badge for `system` source in audit list/detail and rules detail;
  system added to source filter dropdown.

**Files:** `migrations.py`, `pipeline.py`, `orchestrator.py`, `mover.py`,
`models.py`, `writer.py`, `analyze.py`, `main.py`, `audit/list.html`,
`audit/detail.html`, `rules/detail.html`, `analyze.html`

---

## 2026-04-06 — Fix: deduplicate analysis metrics by email_id

**What changed:**
- **fix:** Analysis page and CLI `mailsort analyze` counted audit_log rows, not
  distinct emails. An email skipped across 3 cycles counted as 3 skipped. Now
  uses a CTE to keep only the most recent audit row per `email_id`, so each
  email counts once with its final outcome (moved vs. skipped) and final source.
- Affects: overall totals, source breakdown, LLM confidence distribution.
- Corrections count was already `COUNT(DISTINCT email_id)` — unaffected.

**Files modified:**
- `src/mailsort/web/routes/analyze.py` — CTE dedup for web
- `src/mailsort/main.py` — CTE dedup for CLI `_print_analysis`
- `tests/test_observability.py` — 2 new tests (dedup + dry-run exclusion)
- `docs/dev/decisions.md` — design decision documented
- `docs/design/web-ui.md` — note dedup behavior

---

## 2026-04-06 — Fix: reconcile CLI analyze + docs with web page fixes

**What changed:**
- **fix:** CLI `mailsort analyze` had identical bugs to the web analysis page:
  no dry-run exclusion, corrections query used `'manual'` instead of
  `'correction'`, skipped-then-sorted and rule corrections queries same issue.
  All four fixes ported from web route to CLI.
- **fix:** `docs/design/audit.md` described a "from inbox" / "from other"
  correction split that was never implemented. Updated to document actual
  behavior: single correction count from `classification_source='correction'`.
- **fix:** `docs/design/classification.md` said thread corrections are logged
  as `'manual'` — clarified that executed moves get `'correction'`, skipped
  moves get `'manual'`.

**Files modified:**
- `src/mailsort/main.py` — 4 query fixes in `_print_analysis`
- `docs/design/audit.md` — correction counting section rewritten
- `docs/design/classification.md` — thread correction source clarified

---

## 2026-04-06 — Fix: exclude dry runs from analysis page

**What changed:**
- **fix:** Analysis page included dry-run data in all metrics (emails classified,
  moved/skipped counts, source breakdown, LLM confidence distribution, etc.).
  Dry runs inflate these numbers since emails are classified but not actually
  moved. Added `r.dry_run = 0` filter to the base query — propagates to all
  analysis metrics.

**Files modified:**
- `src/mailsort/web/routes/analyze.py` — added dry_run filter to base query
- `docs/design/web-ui.md` — note that analysis excludes dry runs

---

## 2026-04-06 — Fix: correction badges and analysis page queries

**What changed:**
- **fix:** Analysis page "User Corrections" card was always showing 0 — the query
  looked for `classification_source='manual'` but corrections are stored as
  `'correction'`. Fixed to query `'correction'` directly.
- **fix:** "Rule Corrections" table on analysis page never appeared — same root
  cause (joined on `'manual'` instead of `'correction'`).
- **fix:** "Skipped Then Sorted" table missed correction-sourced rows — now checks
  both `'manual'` and `'correction'`.
- **feat:** Audit log source filter dropdown now includes "Correction" option.
- **feat:** Correction badges are orange (`bg-orange-50 text-orange-700`) across
  all three locations: audit list, audit detail, and email history table.
  Corrections with a `rule_id` link to the rule detail page.
- **feat:** Analysis page bar chart uses orange for correction source.
- **fix:** System test README had `--config` after the subcommand (should be before).

**Files modified:**
- `src/mailsort/web/routes/analyze.py` — 3 query fixes
- `src/mailsort/web/templates/audit/list.html` — correction dropdown + badge
- `src/mailsort/web/templates/audit/detail.html` — correction badge (detail + history)
- `src/mailsort/web/templates/analyze.html` — orange bar color
- `tests/system/README.md` — `--config` placement
- `docs/design/web-ui.md` — badge color reference, analysis section updated

---

## 2026-04-05 — Feat: auto-bootstrap on first scheduler start

**What changed:**
- **feat:** The scheduler (`mailsort start`) now automatically runs bootstrap on
  the first tick if no completed bootstrap run exists. Classification is skipped
  that tick and starts on the next interval. This means `docker compose up` just
  works — no separate `mailsort bootstrap` step needed.
- **design:** Only `status='completed'` bootstrap runs satisfy the check. Failed,
  abandoned, or stuck `'running'` bootstraps trigger a retry on the next tick.
  `reconcile_stale_runs` handles stuck rows (marks them `'abandoned'`).
- **design:** Auto-bootstrap runs under the same `flock` as classification,
  preventing concurrent bootstrap + classification or duplicate bootstraps.

**Files modified:**
- `src/mailsort/scheduler.py` — `_needs_bootstrap()`, `_run_auto_bootstrap()`,
  auto-bootstrap check in `_scheduled_run()`
- `tests/test_scheduler.py` — 11 new tests (6 for `_needs_bootstrap`, 3 for
  `_run_auto_bootstrap`, 2 for `_scheduled_run` integration)

---

## 2026-04-05 — Feat: folder description regeneration (`mailsort describe`)

**What changed:**
- **feat:** New `mailsort describe` CLI subcommand to regenerate folder descriptions
  using fresh email samples and the LLM. Supports `--folder`, `--pattern`, `--all`,
  and `--dry-run` options. Folder paths accept short form (e.g., `Affairs/Banks`)
  which auto-prefixes with `INBOX/`.
- **feat:** Web UI `/folders` page now has per-folder "Regenerate" links and a
  "Regenerate All" button with confirmation dialog. Both POST to new endpoints
  (`/folders/regenerate`, `/folders/regenerate-all`) and show flash-style result
  messages.
- **design:** Unlike initial generation, regeneration does NOT fall back to generic
  placeholders. If the LLM fails, the old description is preserved and the error
  is reported.
- **design:** Manual config overrides (`folder_description_overrides`) are always
  skipped with a warning, protecting user-set descriptions.

**Files modified:**
- `src/mailsort/classifier/descriptions.py` — `RegenerationResult`, `BulkRegenerationReport`,
  `regenerate_folder_description()`, `regenerate_descriptions_for_folders()`
- `src/mailsort/main.py` — `describe` command, `_resolve_describe_targets()`,
  `_report_describe_results()`
- `src/mailsort/web/routes/folders.py` — POST `/folders/regenerate`,
  `/folders/regenerate-all`
- `src/mailsort/web/templates/folders.html` — regenerate buttons, flash messages
- `tests/test_descriptions.py` — 9 new tests for regeneration logic

---

## 2026-04-05 — Feat: system test scenarios L3a, L14, L9, L17 + supporting features

**What changed:**
- **feat:** Cat 2b correction-reversal detection (`_detect_correction_reversals` in `learner.py`).
  Detects when a user moves a previously-corrected email to a new folder (sort-back).
  Records a `classification_source='manual'` row, enabling confirming-sort counting
  and partial confidence recovery.
- **feat:** Bootstrap now processes `manual_rules` from config — creates rules with
  `source='manual'` that are exempt from computed confidence. Existing auto rules
  are upgraded to manual if they match.
- **feat:** `VerificationResult.metadata` dict for passing data between verification steps.
- **fix:** Confirming sorts in `_count_net_corrections` and web UI now exclude bootstrap
  runs from the count (prevents inflated confirming sort numbers).
- **test:** System test scenarios: L3a "3 strikes" (3 chase corrections → confidence
  drops below rule_move), L9 (verify low-confidence rule won't fire), L14 (sort-back
  recovery — confirming sort partially restores confidence), L17 (manual rule exempt
  from confidence recomputation).
- **test:** Added L3a-1 and L3a-2 chase emails to `generate_inbox_emails.py`.
- **test:** Added `verify_learning_step3` (L3a/L9), `verify_learning_step4` (L14),
  `verify_learning_step5` (L17) to `verify_results.py`.
- **config:** Added `manual_rules` to `config.test.yaml` for L17 testing.

---

## 2026-04-05 — Feat: computed confidence model

**What changed:**
- **feat:** Replaced static confidence model (set once, modified by one-way penalties)
  with a computed model: `confidence = max(0, base × coherence × staleness − net_corrections × penalty)`.
  Confidence is recomputed each cycle from live audit_log state — bidirectional, so rules
  recover when evidence improves.
- **feat:** `BaseConfidenceConfig` — per-rule-type base confidence with floor/cap/per_evidence
  scaling (exact_sender: 0.80–0.95, sender_domain: 0.75–0.90, list_id: fixed 0.95).
- **feat:** Coherence factor from 30-day window with min-sample guard (< 3 emails → 1.0).
- **feat:** Staleness factor from `last_relevant_at` (365d threshold, 365d decay, floor 0.6).
- **feat:** Corrections recorded as `classification_source='correction'` with `rule_id`.
  Net corrections = corrections_against − confirming_manual_sorts. Corrections age out at 30d.
- **feat:** `correction_penalty` reduced 0.15 → 0.05 (volume-independent per-correction deduction).
- **feat:** Deactivation at configurable threshold (0.50) instead of below `rule_move`.
- **feat:** Reactivation over duplication — `find_rule_any_status` + `reactivate_rule` prevents
  duplicate rule rows when evidence re-accumulates for an inactive rule.
- **feat:** `_count_all_time_evidence` LIMIT dynamically computed from config caps/floors.
- **refactor:** `last_hit_at` → `last_relevant_at` (migration 11). `hit_count` retained for display.
- **refactor:** Removed `adjust_rule_confidence` (staleness decay) and `_penalize_rule` (direct
  penalty). Both replaced by `compute_rule_confidence()`.
- **refactor:** Dedup fix: `_already_handled_email_ids` allows re-correction after a new rule move
  (previous dedup was too aggressive — blocked corrections permanently).
- **feat:** Web UI rule detail: Performance card split into All Time / Last 30 Days columns
  with coherence, evidence, corrections against/confirming/net. Added `correction` badge (red).

**Files:** `config.py`, `db/migrations.py`, `audit/learner.py`, `classifier/rules.py`,
`orchestrator.py`, `main.py`, `web/routes/rules.py`, `web/templates/rules/detail.html`,
`web/templates/rules/list.html`, `config.yaml.example`, plus all test files.

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
