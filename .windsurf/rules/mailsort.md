---
description: Rules for working on the mailsort project
---

## Architecture Document Maintenance

After completing any feature, bug fix, or phase of development:

1. **Update `ARCHITECTURE.md`** to reflect the current state of the code. If new modules, patterns, or behaviors were added, document them in the appropriate section.
2. **Update the development checklist** in §15 (Development Phases) — check off completed items and add file references for newly implemented components.
3. **When a behavioral change is made** (e.g., idempotency, safety guarantees, new guards), update BOTH the architecture doc AND the CLI help text (`--help` docstrings) in the same change. Do not wait to be asked — treat docs and help text as part of the implementation, not a follow-up task.

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

## General

- The existing DB schema should not be changed without explicit user approval.
- Use the existing patterns in the codebase (e.g., `Database` context manager, `MoveDecision` model, `AuditWriter` for run lifecycle).
