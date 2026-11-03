# mailsort

<!-- house-style:begin -->
## House style

This section is written by `~/Workspace/tools/house-style/sync.py` from
`house-style/templates/claude-block.md` there; an edit made here is overwritten on the next
sync. `tools/docs/house-style.md` states the rules in full. Each rule below that names a
skill is carried out by loading that skill first.

**A request unrelated to this session's earlier work:** say in one line, before starting,
that `/clear` would run it without the earlier context, which every request re-sends.

### Sibling repos in this workspace

`mailsort.code-workspace` opens these repos alongside this one:

- **kaylix** (`~/Workspace/kaylix`, deploy)
- **tools** (`~/Workspace/tools`, common)
- **agents** (`~/Workspace/agents`, common)

Reading them needs no permission. Their own `CLAUDE.md` files do not load here; read them
before changing anything there.

**Every write to a sibling asks first, unless it is marked writable above.** Name the
file, show the diff, say why the change cannot live here, and get a yes; the approval
covers that change only, in auto mode and for every tool. kaylix differs: the change is
committed on a worktree branch and the ask is the push (`kaylix-change` skill). A grant
lifts the asks for one plan's work; ask the operator, then use the `grant` skill. The
guard hook catches what it can read from command text; ask anyway when it cannot.

### kaylix

Load the `kaylix-change` skill before editing, committing or pushing anything in kaylix,
and before rebasing or resetting in a checkout other sessions use. In short: a worktree
under `.claude/worktrees/` cut from `origin/main`, never the main checkout; explicit paths
to `git add` and `git commit -- <paths>`; the change ends merged and deployed, with any
change it pairs with.

### Remote hosts, root and secrets

- Name the user in every ssh: `kaylix@edwin`, `wleggette@edwin`, `root@ruslan.kaylix.net`.
- `wleggette@edwin` and `nigel` hold the operator's personal files: ask before first use in
  a session, saying what for.
- Root on any host goes through the batch gate in `~/.claude/CLAUDE.md` § Root and
  privileged access. `sudo` does not work on the Linux hosts.
- A deployed service reads credentials from `/etc/<svc>/<svc>.secrets`, seeded from a
  committed `<svc>.secrets.example` and listed once in kaylix `ARCHITECTURE.md` § Secrets
  Strategy. On the Macs they are read from 1Password with `op read` into a pipe. A secret
  never enters git, argv, a config file, or a traced shell variable.

### Scripts and dependencies

- `#!/bin/bash` on line 1, `set -euo pipefail` first, `BASEDIR=$(cd "$(dirname "$0")" &&
  pwd)` where the script references files beside it; `#!/usr/bin/env bash` for a Mac-only
  script. One `.service` plus `.timer` pair per scheduled job, never cron or a `sleep` loop.
- Code from another estate repo is pinned to a tag or commit. Move the pin to the latest
  version, in a commit of its own, whenever you change the repo or need something new.

### Commits, documents and comments

- Load the `commit` skill before every commit.
- Load the `write-doc` skill before writing a document or adding more than a few lines to
  one; a document new or grown by 10 lines needs its review and `Doc-review:` trailer.
- Every edit to a living document: current state, not history; no unresolved question
  (that is a `**Q**` task in a plan); literal, with quantities rather than
  characterisations; a heading names its subject.
- Documents other than the root `README.md`, `CLAUDE.md` and a few top-level files are
  kebab-case under `docs/`. Comments record the constraint, about three lines at most.
<!-- house-style:end -->
