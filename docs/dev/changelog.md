# Development Changelog

Running log of significant changes, decisions, and rationale. Reverse
chronological — newest entries first.

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
