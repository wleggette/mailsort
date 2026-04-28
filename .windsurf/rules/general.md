---
description: General development methodology rules
trigger: always_on
---

## General Methodology

- Follow the guides in `docs/methodology/`: architecture-documentation, system-test-methodology, ai-pair-programming.
- **For non-trivial tasks** (multiple files or multi-step), write the plan to `docs/dev/scratch.md` first, then present it for approval before writing code.
- Use `docs/dev/scratch.md` for working notes across sessions (gitignored). See `docs/methodology/ai-pair-programming.md` §1.

## Documentation Maintenance

After completing any feature, bug fix, or phase of development:

1. **Update the relevant doc in `docs/`** to reflect the current state of the code:
   - `docs/architecture.md` — diagrams and sequences
   - `docs/design/` — subsystem design docs (one per major module)
   - `docs/configuration.md` — config fields and defaults
   - `docs/operations.md` — deployment and operational concerns
2. **Update the development checklist** in `docs/planning/phases.md`.
3. **When a behavioral change is made**, update BOTH the relevant design doc AND the CLI help text (`--help` docstrings) in the same change.
4. **System test plan** (`docs/planning/system-test-plan.md`): follow `docs/methodology/system-test-methodology.md`. After any architecture change, re-audit affected phase cards.

## Dev Logs

- **`docs/dev/changelog.md`** — append an entry for every `feat:`, `fix:`, or `refactor:` commit. Focus on what changed behaviorally.
- **`docs/dev/design-ideas.md`** — explored-but-deferred feature ideas. Remove when built. **Update the TOC** at the top when adding, implementing, or removing ideas.
- **`docs/dev/decisions.md`** — log of design decisions with options, tradeoffs, and rationale. Permanent records.
- **After implementing a design idea**, migrate the decisions, alternatives considered, and rationale from `design-ideas.md` into `decisions.md` as a permanent record. Then remove the implemented section from `design-ideas.md` (or mark it as implemented with a pointer to the decision log). `design-ideas.md` should only contain ideas that are **not yet implemented**.

## Testing

- All new code must have corresponding tests. Run the full test suite before declaring work complete.
- **Algorithmic changes** (thresholds, coherence, rule creation, pipeline behavior): add targeted unit tests for the changed behavior, including boundary and rejection cases.
- **Guards and filters**: test both sides — positive (criteria met) and negative (criteria not met, with realistic data).

## I/O Safety & Logging

- **Wrap all I/O in try/except** — never let a network or DB error prevent audit logging or skip remaining work.
- **Log at the right level**: `logger.warning` for recoverable failures, `logger.exception` for unexpected errors needing tracebacks.
- **Defensive audit writes** — exception handlers must catch their own errors internally.
- **Per-item isolation** — one item's failure must not prevent the rest from being processed.
- **Graceful degradation** — if an optional data source is unavailable, log a warning and continue.

## Configuration

- **All intervals, thresholds, and tunable values must be configurable** — no hardcoded magic numbers that control behavior (refresh intervals, batch sizes, confidence thresholds, lookback windows, etc.).
- Pattern: add the field with a sensible default in the config model, and document it in the config example file.

## Destructive Changes

- **Schema changes** (database migrations, API contract changes, config format changes) require explicit user approval before implementation.
- **Deleting or renaming public interfaces** (CLI commands, API endpoints, config fields) — confirm with the user first.
- **Data loss operations** (dropping tables, clearing caches, resetting state) — never auto-run, always confirm.

## Codebase Consistency

- **Follow existing patterns** — when the codebase has established conventions (error handling, data access, model patterns), use them for new code rather than introducing alternatives.
- **Match abstraction level** — if similar features use a particular structure (e.g., config class + module + tests), follow the same structure for new features.
- **Don't refactor while implementing** — if you notice a pattern that should change, note it and address it separately. Don't mix refactoring with feature work.
