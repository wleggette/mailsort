# mailsort

<!-- house-style:begin -->
## House style

This section is written by the `house-style` skill in the tools repo
(`~/Workspace/tools/house-style/sync.py`). Edit `house-style/templates/claude-block.md`
there and re-run the sync; an edit here is overwritten on the next run.
`tools/docs/house-style.md` states the rules in full.

### Sibling repos in this workspace

`mailsort.code-workspace` opens these repos alongside this one:

- **kaylix** (`~/Workspace/kaylix`, deploy)
- **tools** (`~/Workspace/tools`, tools)

Opening them makes them Claude working directories, so reading them needs no permission.
Their own `CLAUDE.md` files do not load here; read them before changing anything there.

**Every write to a sibling asks first, unless it is marked writable above.** Name the
file, show the diff, say why the change cannot live here, and get a yes. An approval
covers the change described, not the next one. This holds in auto mode and for every
tool. kaylix is the one difference: the change is written and committed on a worktree
branch first, and the ask is made on that committed branch before it is merged and
deployed (§ Working in kaylix).

Enforcement: `.claude/settings.json` puts each read-only sibling but kaylix in
`permissions.ask` for `Edit`, `Write` and `NotebookEdit`, and
`.claude/hooks/guard-external-writes.py` asks for any Bash command naming a sibling that
is not recognizably read-only (`cat`, `grep`, `rg`, `find`, `diff`, `sed -n`, read-only
`git`, `sync.py --check`, `plan check`). For kaylix the hook passes every write inside a
worktree under `.claude/worktrees/`, asks on `git push` — the merge approval — and asks on
any other write to the main checkout, edits included. It follows `cd` and variables set
earlier in the same command; a path held in a variable set elsewhere is not caught. Ask
anyway.

### Working in kaylix

kaylix is a shared checkout: several sessions edit it, and the deploy timer on edwin
pulls `main` every five minutes, so a commit on `main` is effectively published.

- Prepare a change in a worktree, never in the main checkout, cut from `origin/main`
  after a fetch, since the local `main` may lag:
  `git -C ~/Workspace/kaylix worktree add .claude/worktrees/<topic> -b <topic> origin/main`.
  That path is gitignored there and is what `EnterWorktree` expects.
- `git add` explicit paths, never `-A` or `.`. `git commit -- <paths>`, not a bare
  `git commit`: a bare commit writes the whole index, including another session's
  staged files. This is the rule in kaylix always, because it manages the whole
  deployment. Elsewhere it is judgement: apply it whenever another session may have
  work staged in the same checkout.
- **A kaylix change ends merged and deployed, never waiting on a branch.** Commit it on
  the worktree branch; nothing asks while it is written. From any other workspace, show
  it in chat: the branch and the worktree path, `git diff main...<topic>`, what it
  deploys, and which change in which repo it pairs with. Then, in the same turn: fetch,
  rebase the branch onto `origin/main` if `main` has moved (the branch is yours), and
  `git push origin <topic>:main`. The guard asks on that push; the operator's click, with
  the change shown above it, is the approval, and a no ends the change. In the kaylix
  workspace itself the push does not ask. Then run the deploy (§ Remote hosts and root)
  and watch its output, `git worktree remove` it, and delete the branch locally and on
  origin.
  Last, `git -C ~/Workspace/kaylix merge --ff-only origin/main` when the main checkout
  is on `main` with a clean tree, so the operator's kaylix window shows what edwin runs.
- **A kaylix change paired with another repo's change deploys with it**, in the same
  sitting. Neither half is done while the other is unmerged or undeployed; order the two
  deploys so each finds what it needs.
- Never leave edits uncommitted in the main checkout: the next session's commit may
  carry them.
- **Never rebase or reset a branch another session may be working on**, and never push
  someone else's unpushed commits to get yours out. When your work is ready and `main`
  carries theirs, cut a worktree from `origin/main`, cherry-pick your own commits into
  it, and push that:
  `git worktree add .claude/worktrees/<topic> -b <topic> origin/main` then
  `git cherry-pick <sha>...`. The checkout is then divergent, which is the price and is
  recoverable — the duplicated commits keep their patch-ids, so whoever pushes next runs
  `git pull --rebase` and git drops them silently. Rebase the shared branch only when
  you know no other session is in it.

### Remote hosts and root

- Name the user in every ssh: `kaylix@edwin`, `wleggette@edwin`, `root@ruslan.kaylix.net`.
  `~/.ssh/config` supplies a default, and the two edwin accounts see different units.
- **After a push that is meant to deploy, trigger the deploy** rather than waiting up to
  five minutes for `kaylix-deploy.timer`:
  `ssh kaylix@edwin /opt/kaylix/edwin/deploy/deploy.sh`. That is the script the timer
  runs: no root, about ten seconds, idempotent. Watch its output; anything verified before
  it finishes measures the previous version. `systemctl start kaylix-deploy` fails as
  `kaylix` with `Interactive authentication required`, and is not a reason for a root
  batch.
- `wleggette@edwin` and `nigel` hold the operator's personal files: ask before first use
  in a session, saying what for. That approval covers the session, not one command.
- Root on any host goes through the batch gate in `~/.claude/CLAUDE.md` § Root and
  privileged access: batch file, preview in chat, `rootgate run` in the same turn.
  `sudo` does not work on the Linux hosts.
- A secret never enters git, argv, or a traced shell variable. Pipe it from `op read`.

### Secrets

Two places, and they do not mix.

**Deployed, on edwin, nigel and ruslan.** A service reads credentials from
`/etc/<svc>/<svc>.secrets` as environment variables, seeded on the host by the kaylix
`setup.sh` from a committed `<svc>.secrets.example` that lists every key with an empty
value. Nothing credential-bearing is in git, argv or a config file; configuration
controls behaviour and never carries a credential. **The one list of secrets files, per
host and service, is the table in kaylix `ARCHITECTURE.md` § Secrets Strategy**; the
pattern itself is `kaylix/docs/methodology/secret-management.md`. A repo's README points
at its row and does not restate it: the mapping from repo to secrets file is not
one-to-one, and two lists drift. The pre-commit hook refuses the file itself; the rest is
convention, checked by the house-style review against that table.

**On a client machine, the operator's Macs.** Credentials live in 1Password and are read
with `op` (`op read "op://<vault>/<item>/<field>"`), piped into the consuming command,
never assigned to a variable or placed in argv. No secrets file, `.env` or keychain
export sits on disk; a `.env.local` a tool insists on is gitignored and is not the source
of record.

### Scripts

- `#!/bin/bash` on line 1, `set -euo pipefail` as the first command,
  `BASEDIR=$(cd "$(dirname "$0")" && pwd)` where the script references files beside it.
  `#!/usr/bin/env bash` is accepted for a script that runs only on a Mac, where
  `/bin/bash` is 3.2. `.githooks/pre-commit.d/20-shell-header` refuses a staged script
  that breaks the first two and one that fails `bash -n`.
- One `.service` plus `.timer` pair per scheduled job. No cron line, no `sleep` loop.

### Documents and comments

- Every repo has a `README.md` that says what it is, how it is deployed, and lists the
  top-level documents with one line each. `CLAUDE.md` does not repeat the overview.
- Top-level documents in caps, one word where one will do: `README.md`, `CLAUDE.md`,
  `ARCHITECTURE.md`. A document at the top level is claiming to be read before the repo
  makes sense. Everything else is kebab-case under `docs/`, in subdirectories once a flat
  listing runs past a screenful. The `plants` vault is the exception: Title Case with
  spaces.
- A living document states what is true now, as though it had always read that way.
  History belongs in git or a dedicated changelog: no "previously", "(updated)", "since
  the last measurement", and no conclusion stated and then withdrawn — a correction
  replaces the text. Keep the reasoning behind a decision, which is what stops it being
  relitigated; drop the chronology of reaching it. A date in a document is a fact about
  the system — when a measurement was taken, when a device shipped — never when the
  document changed. Two things describe the system rather than the document and are
  fine: a design that states a transition ("what the alert does now / after this
  change"), and a fact whose content is variation ("three scans disagreed"), written as
  a property rather than a diary.
- A living document carries no unresolved question. A question about current state is
  a `**Q**` task in a plan, naming the document that will hold the answer; the document
  says what is determined and cites the plan id for what is not. Stated uncertainty with
  its confidence — "inferred from circumstantial evidence" — is determined state and
  stays.
- Comments record the constraint, not the reasoning that found it, in about three lines
  per block at most.
- Write literally. No metaphors, no stock phrases; quantities rather than
  characterisations.
- A title, heading or subject line names its subject, in terms that make sense before the
  content is read. It is a label, and is never written to intrigue.
<!-- house-style:end -->
