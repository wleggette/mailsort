# Development Changelog

Running log of significant changes, decisions, and rationale. Reverse
chronological — newest entries first.

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
