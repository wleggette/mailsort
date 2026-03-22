---
description: Rules for working on the mailsort project
---

## General Methodology

- Follow the guides in `docs/methodology/`: architecture-documentation, system-test-methodology, ai-pair-programming.
- **For non-trivial tasks** (multiple files or multi-step), propose a plan before writing code. Get approval before proceeding.
- Use `docs/dev/scratch.md` for working notes across sessions (gitignored). See `docs/methodology/ai-pair-programming.md` §1.

## Documentation Maintenance

After completing any feature, bug fix, or phase of development:

1. **Update the relevant doc in `docs/`** to reflect the current state of the code:
   - `docs/architecture.md` — diagrams and sequences
   - `docs/design/` — subsystem design (jmap-integration, classification, learning, audit, data-models, web-ui)
   - `docs/configuration.md` — config fields and defaults
   - `docs/operations.md` — deployment and operational concerns
2. **Update the development checklist** in `docs/planning/phases.md`.
3. **When a behavioral change is made**, update BOTH the relevant design doc AND the CLI help text (`--help` docstrings) in the same change.
4. **System test plan** (`docs/planning/system-test-plan.md`): follow `docs/methodology/system-test-methodology.md`. After any architecture change, re-audit affected phase cards.

## Dev Logs

- **`docs/dev/changelog.md`** — append an entry for every `feat:`, `fix:`, or `refactor:` commit. Focus on what changed behaviorally.
- **`docs/dev/design-ideas.md`** — explored-but-deferred feature ideas. Remove when built.
- **`docs/dev/decisions.md`** — log of design decisions with options, tradeoffs, and rationale. Permanent records.

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

- **All intervals, thresholds, and tunable values must be configurable** — no hardcoded magic numbers.
- Pattern: add field with default in the Pydantic config class (`config.py`), document in `config.yaml.example`.

## Project Constraints

- The existing DB schema should not be changed without explicit user approval.
- Use existing codebase patterns (e.g., `Database` context manager, `MoveDecision` model, `AuditWriter` for run lifecycle).
