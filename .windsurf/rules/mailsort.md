---
description: Rules for working on the mailsort project
---

## Architecture Document Maintenance

After completing any feature, bug fix, or phase of development:

1. **Update `ARCHITECTURE.md`** to reflect the current state of the code. If new modules, patterns, or behaviors were added, document them in the appropriate section.
2. **Update the development checklist** in §15 (Development Phases) — check off completed items and add file references for newly implemented components.

## General

- The existing DB schema should not be changed without explicit user approval.
- All new code must have corresponding tests. Run the full test suite before declaring work complete.
- Use the existing patterns in the codebase (e.g., `Database` context manager, `MoveDecision` model, `AuditWriter` for run lifecycle).
