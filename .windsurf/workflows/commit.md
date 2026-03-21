---
description: Create a git commit message and commit all changes
---

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
