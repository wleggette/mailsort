# AI Pair Programming Methodology

Guidelines for effective collaboration between a human developer and an
AI coding assistant (e.g., Windsurf Cascade, Cursor, Copilot Chat).
Focused on maintaining quality, context, and velocity across sessions.

---

## 1. Context Management

AI assistants have limited memory across sessions. The human-AI pair
must actively manage context to avoid re-discovery and re-explanation.

### Working notes (`scratch.md`)

Keep an ephemeral working notes file (gitignored) that the AI reads at
session start and updates during work:

- **Session start:** AI reads scratch to pick up where the last session
  left off — current task, pending items, open questions.
- **During work:** AI appends significant decisions, deferred items, and
  investigation findings.
- **Session end:** AI updates scratch with what was completed, what's
  still pending, and any blockers.
- **Task complete:** Clear scratch. Anything worth keeping moves to
  permanent docs.

### Permanent context

For context that should survive across all sessions (not just the
current task), use:

- **Design decisions log** — why the code works the way it does
- **Changelog** — what changed and when
- **Architecture docs** — the structural map of the system
- **Project rules file** (e.g., `.windsurf/rules/`) — behavioral
  guidelines the AI follows automatically

### Anti-patterns

- **Don't rely on chat history** — it gets truncated or lost between
  sessions. If it matters, write it down.
- **Don't repeat context verbally** — if you find yourself re-explaining
  a design decision, it should be in the decisions log.
- **Don't let the AI guess** — if the AI doesn't have context, it will
  make assumptions. Better to say "read X first" than to correct bad
  assumptions after the fact.

## 2. Task Execution

### Planning before doing

For non-trivial tasks (more than a single file change):

1. **AI proposes a plan** — numbered steps, one in-progress at a time
2. **Human approves or adjusts** — before any code is written
3. **Documentation first** — for new features or phases, update all
   relevant documentation (design docs, config reference, architecture,
   operations, system test plan) before writing implementation code.
   Present doc changes for review; coding begins only after docs are
   approved.
4. **AI executes step by step** — updating the plan as new information
   arrives
5. **Both verify** — AI runs tests, human reviews the approach

### Minimal, focused changes

- **Prefer edits over rewrites** — modify existing code rather than
  replacing entire files.
- **One concern per change** — don't mix a bug fix with a refactor.
- **Run tests after each logical change** — not just at the end.

### When to stop and ask

The AI should stop and ask the human when:

- **The task is ambiguous** — multiple valid interpretations exist
- **A design decision is needed** — not just an implementation choice
- **Something unexpected is found** — a bug, an inconsistency, a
  constraint that changes the approach
- **The change is destructive** — deleting files, changing schemas,
  altering public interfaces

## 3. Code Quality

### Follow existing patterns

The AI should match the codebase's existing style:

- **Same error handling patterns** — if the codebase uses per-item
  isolation, don't introduce fail-fast for new code.
- **Same naming conventions** — if methods use `snake_case`, don't
  introduce `camelCase`.
- **Same abstraction level** — if similar features use a certain
  pattern (e.g., a config class + module + tests), follow the same
  structure for new features.

### Documentation before implementation

For **new features and phases**, documentation comes first — write the
design into the docs, get human approval, then implement. For **bug
fixes and refactors**, update docs after to reflect what changed.

In all cases, treat documentation as part of the work, not a
follow-up task:

- **Architecture change** → update the diagram/phase card
- **New config field** → update the config reference doc
- **Behavioral change** → update the design doc + CLI help text
- **Design decision** → log it in the decisions file

### Testing discipline

- **Write or update tests before declaring done** — not "I'll add
  tests later."
- **Test both sides of every decision** — positive case (criteria met)
  and negative case (criteria not met).
- **Don't weaken existing tests** — if a test needs to change, the AI
  should explain why and get approval.
- **Run the full suite** — not just the tests for the changed module.

## 4. Communication Style

### Human → AI

- **Be specific about intent** — "make it configurable" is better than
  "fix it." "Add a test for the boundary case where coherence is exactly
  80%" is better than "add more tests."
- **Reference files and line numbers** — "look at learner.py line 485"
  is faster than "look at the rule creation function."
- **State constraints upfront** — "don't change the DB schema" prevents
  wasted work.

### AI → Human

- **Be terse and direct** — state what you did, what you found, or what
  you need. No preamble.
- **Show, don't describe** — edit the file rather than explaining what
  you would do.
- **Surface risks early** — "this will also affect X, is that okay?"
  before making the change.
- **Summarize after clusters of work** — after multiple related changes,
  give a concise summary of what changed and what's left.

## 5. Session Workflow

A typical productive session:

```
1. AI reads scratch.md and project rules
2. Human states the task
3. AI proposes a plan (or asks clarifying questions)
4. Human approves/adjusts
5. For new features: AI updates documentation first, human reviews
6. AI executes implementation, testing as it goes
7. AI updates scratch with progress, decisions, deferred items
8. Human reviews and commits
9. AI updates changelog and decisions log if applicable
```

### Multi-session tasks

When a task spans multiple sessions:

1. **End of session:** AI writes a clear handoff in scratch — what's
   done, what's next, what decisions were made.
2. **Start of next session:** AI reads scratch, summarizes its
   understanding, and picks up from the plan.
3. **Don't re-do work** — if the previous session left passing tests
   and committed code, build on it rather than re-evaluating.

## 6. Commit Discipline

### When to commit

- **After each logical unit of work** — not after every file change,
  but not after hours of accumulated changes either.
- **Before switching tasks** — commit the current work before starting
  something unrelated.
- **Tests must pass** — never commit broken tests.

### Commit messages

Use conventional commits format:

```
type: short summary

- Bullet points explaining what changed and why
- Reference specific files/modules when helpful
```

Types: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`

The AI should compose the commit message by reviewing the actual diff,
not from memory of what it did. This catches any changes that were made
outside the AI's awareness.
