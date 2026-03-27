---
description: Create a git commit message and commit all changes
---

## When to commit

- **Commit per logical unit of work**, not per individual file edit.
- A logical unit is a coherent change that could be described in one commit message — e.g., "add Phase 4 learning tests" (which may touch the test plan, the test runner, the verifier, and the fixture data).
- Do NOT commit after every small edit during a multi-step task. Accumulate changes and commit when the unit is complete and verified.
- When the user asks you to commit, commit everything outstanding.

## How to commit

1. Run `git status` to see what files have changed.
2. Run `git diff --stat` to get a summary of changes.
3. Run `git diff` (or `git diff <file>` for key files) to understand the actual changes.
4. Compose a commit message following conventional commits format:
   - First line: `type: short summary` (e.g. `feat:`, `fix:`, `docs:`, `refactor:`, `test:`)
   - Blank line
   - Bullet-point body summarising the key changes
5. Stage all changes and commit:
   ```
   git add -A && git commit -m "<message>"
   ```
