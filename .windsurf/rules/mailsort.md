---
description: Rules for working on the mailsort project
---

## Documentation Maintenance

After completing any feature, bug fix, or phase of development:

1. **Update the relevant doc in `docs/`** to reflect the current state of the code. If new modules, patterns, or behaviors were added, document them in the appropriate file:
   - `docs/architecture.md` — high-level diagrams and sequences
   - `docs/design/` — subsystem design (jmap-integration, classification, learning, audit, data-models, web-ui)
   - `docs/configuration.md` — config fields and defaults
   - `docs/operations.md` — deployment and operational concerns
2. **Update the development checklist** in `docs/planning/phases.md` — check off completed items and add file references for newly implemented components.
3. **When a behavioral change is made** (e.g., idempotency, safety guarantees, new guards), update BOTH the relevant design doc AND the CLI help text (`--help` docstrings) in the same change. Do not wait to be asked — treat docs and help text as part of the implementation, not a follow-up task.

## Changelog (`docs/dev/changelog.md`)

- **Append an entry for every `feat:`, `fix:`, or `refactor:` commit** — not for `docs:` or `test:`-only changes.
- Format: `## YYYY-MM-DD — short description` followed by bullet points.
- Focus on *what changed behaviorally*, not file-level diffs.

## Design Ideas (`docs/dev/design-ideas.md`)

- **When a feature idea is explored but deferred**, document the idea, the analysis, and why it was deferred — so it can be revisited later without re-doing the research.
- This is for things **not yet built**. If it gets built, move the entry to the appropriate design doc and remove it from here.

## Design Decisions (`docs/dev/decisions.md`)

- **When choosing between multiple approaches**, log the options considered, tradeoffs, and which was chosen with rationale.
- **When discovering a non-obvious constraint** that shaped the implementation (e.g., JMAP behavior, SQLite limitation), document it.
- These explain **why the code works the way it does**. They are permanent records, not ephemeral notes.

## Testing

- All new code must have corresponding tests. Run the full test suite before declaring work complete.
- **When an algorithmic change is made** (e.g., threshold logic, coherence checks, rule creation criteria, classification pipeline behavior), add or update unit tests that specifically exercise the changed behavior — including boundary cases and rejection cases. Do not rely on existing tests covering the change implicitly; verify with explicit targeted tests.
- When a guard or filter is added (e.g., coherence threshold on a rule type that didn't have one before), add both a **positive test** (rule created when criteria met) and a **negative test** (rule rejected when criteria not met, with realistic multi-folder data).

## I/O Safety & Logging

All code that performs I/O (JMAP API calls, Anthropic API calls, SQLite writes) must follow these patterns:

- **Wrap all I/O in try/except** — never let a network or DB error propagate uncaught through a code path that would prevent audit logging or skip remaining work.
- **Log at the right level**: `logger.warning` for expected/recoverable failures (folder scan skip, contact fetch failure), `logger.exception` only when the full traceback is needed for debugging.
- **Defensive audit writes** — methods called from exception handlers (e.g., `finish_run`) must catch their own errors internally so they don't mask the original exception.
- **Per-item isolation** — when processing a batch (emails, folders, contacts), one item's failure must not prevent the rest from being processed.
- **Graceful degradation** — if an optional data source is unavailable (contacts scope, LLM API), log a warning and continue without it rather than failing the entire run.

## Configuration

- **All intervals, thresholds, and tunable values must be configurable** — never hardcode a magic number that controls behavior (refresh intervals, batch sizes, confidence thresholds, lookback windows, etc.).
- Pattern: add the field with a sensible default in the appropriate Pydantic config class in `config.py`, and document the default in `config.yaml.example`.
- The user's working `config.yaml` is gitignored; `config.yaml.example` is the committed reference.

## Working Notes (`docs/dev/scratch.md`)

- **At the start of a multi-step task**, write a brief summary of the goal and current plan to `docs/dev/scratch.md` under a dated heading.
- **When a significant decision is made** during conversation (architecture choice, rejected approach, discovered constraint), append it to scratch so it survives between sessions.
- **When deferring something** (noticed a bug, spotted a doc inconsistency, identified a future improvement), add it to a "Deferred" section in scratch rather than forgetting it.
- **At the end of a working session** (before commit), update scratch with: what was completed, what's still pending, and any open questions.
- **When starting a new session**, read `docs/dev/scratch.md` to pick up context from the previous session.
- **When a task is fully complete**, clear the scratch content and replace with `*(empty — no active task)*`.
- Scratch is **not committed** (gitignored). Anything worth keeping permanently should be moved to the appropriate doc in `docs/` before clearing.

## General

- The existing DB schema should not be changed without explicit user approval.
- Use the existing patterns in the codebase (e.g., `Database` context manager, `MoveDecision` model, `AuditWriter` for run lifecycle).
