# System Test Methodology

How to derive, structure, and maintain a system test plan from the
architecture documentation. This methodology is general-purpose but
illustrated with examples from the mailsort system test plan.

---

## 1. Start from Sequences, Not Components

The component diagram tells you **what exists**. The workflow sequence 
diagrams tell you **what happens** — and that's what you test. Each step 
in a sequence is a potential test boundary.

For each step in a sequence diagram, ask:

- **What are the inputs?** (data, config, external state)
- **What are the outputs?** (DB writes, API calls, return values, logs)
- **What decisions are made?** (branches, filters, thresholds)
- **What can go wrong?** (errors, missing data, edge cases)

Every decision point produces at least one test scenario. Every error
path produces at least one negative test.

## 2. Phase Cards Bridge Architecture to Tests

A phase card is a structured summary of one step in a sequence. It has
a fixed format:

```
┌─ Phase N: Name ──────────────────────────────────────────────┐
│  Input:     What data/state enters this phase                │
│  Module:    Which code modules are involved                  │
│  Output:    What is produced (DB rows, API calls, reports)   │
│  Decisions: Branching logic, thresholds, filters             │
│  Tests:     Scenario IDs from the test plan                  │
└──────────────────────────────────────────────────────────────┘
```

The phase card lives in the architecture document. The "Tests:" field
creates a bidirectional link to the test plan — you can trace from
architecture to tests, and from tests back to architecture.

**Rule:** If a Decision bullet has no corresponding test ID, it's a
coverage gap.

## 3. Three Layers of Coverage

For each phase, think about coverage at three layers:

### Happy path

Does the normal case work end-to-end?

*Example:* Email from known sender → matches exact_sender rule → moved
to correct folder → audit_log row with `moved=1`.

### Decision boundaries

For each decision diamond, test **both sides**:

| Decision | Positive test | Negative test |
|---|---|---|
| Threshold met? | Exactly at threshold (boundary) | One below threshold |
| Coherence ≥ 80%? | 80% coherence (boundary) | 50% coherence |
| Config present? | Override supplied | No override |
| Scope available? | Scope granted | Scope missing |
| Rule exists? | Rule already created | No prior rule |

**Rule:** Boundary tests are more valuable than interior tests. Test at
the threshold, not well above it.

### Error paths

For each I/O call in the phase:

- **API unavailable** → Does the system degrade gracefully?
- **Malformed data** → Does per-item isolation work?
- **Partial failure** → Are remaining items still processed?
- **Crash mid-phase** → Is state recoverable on next run?

Error paths are often better covered by unit/integration tests than
system tests. The system test plan should note which error scenarios
are deferred to unit tests.

## 4. Fixture Design Follows Decisions

Don't design fixtures first and hope they cover the cases. Instead:

1. **List all decision boundaries** from the phase cards
2. **For each boundary**, determine the minimal evidence shape that
   triggers each side (pass vs reject)
3. **Design the fixture group** that produces that shape
4. **Name the group** (e.g., "Group B: Domain coherence — high") and
   **cross-reference** it to the scenario ID

Each fixture group should exist because a specific decision needs
testing. If a group doesn't map to any scenario, it's dead weight.
If a scenario doesn't map to any group, it's untestable.

*Example:*
- Decision: sender_domain requires ≥5 emails, ≥3 distinct senders,
  coherence ≥80%
- Group B: 8 emails from 3 senders at `@bigbank.com`, all → Banks
  (100% coherence) → tests DR1 (happy path)
- Group C: 7 emails from `@megastore.com` split across Banks and
  Stores (57% coherence) → tests DR2 (coherence rejection)

## 5. Verification Checklists Assert Outputs

Every Output listed in a phase card should have a corresponding item
in the phase's verification checklist.

A good checklist item:
- Names the **specific table or artifact** being checked
- States the **expected value or condition**
- Is **automatable** (can be verified by querying the DB or JMAP)

```
- [ ] **Rules created**: expected rules exist in `rules` table
      with `active=1`
```

Avoid vague checklist items like "everything looks right" — they're
not automatable and don't catch regressions.

## 6. Cross-Phase Interactions

Some behaviors only emerge when multiple phases interact. These need
a separate "Cross-Cutting Edge Cases" section with:

- **Phases Involved** column (e.g., "Bootstrap + Dry Run")
- **What It Tests** — the interaction, not a single phase
- Fixture data that **participates in multiple phases**

*Examples:*
- Bootstrap creates rule → dry run uses it → live move executes it
- User corrects a move → learner detects it → rule confidence penalized
  → next run uses lower confidence

## 7. Maintaining the Test Plan

### After an architecture change

1. Re-read the affected phase cards
2. For each changed or new Decision, check if a test scenario covers it
3. If not, add a scenario + fixture group
4. Update the "Tests:" field on the phase card

### After a fixture change

1. Check which scenarios reference the changed group
2. Verify the evidence shape still triggers the intended decision
3. Update counts and expected outcomes in the scenario table

### Periodic audit

Walk each phase card and ask: "Is every Decision bullet covered by at
least one scenario ID in the Tests field?" This is the single most
reliable way to find coverage gaps.

## 8. What System Tests Don't Cover

System tests validate the **end-to-end pipeline against real external
services**. They are not the right place to test:

| Better as unit/integration test | Why |
|---|---|
| Error isolation (one bad email in batch) | Needs precise failure injection |
| Config validation edge cases | No JMAP needed |
| LLM response parsing (malformed JSON) | Mock is more reliable than hoping LLM returns bad data |
| Scheduler timer behavior | Timer mechanics, not classification logic |
| DB migration correctness | Schema-level, no external dependencies |

The test plan should explicitly note which scenarios are **deferred to
unit tests** so the gap is visible, not hidden.

## 9. System Test Configuration

System tests use `tests/system/config.test.yaml` **in-place** — it is never
copied to the project root. The test harness (`run_system_test.py`) defaults
to this path, so running from the project root requires no `--config` flag:

```bash
# From project root — config is picked up automatically
python tests/system/run_system_test.py
python tests/system/run_system_test.py --setup-only
python tests/system/run_system_test.py --cleanup
```

For manual CLI commands against the test database, pass the path explicitly:

```bash
mailsort dry-run --config tests/system/config.test.yaml
mailsort web --config tests/system/config.test.yaml --port 8081
```

**Rules:**

- All system-test-specific configuration (thresholds, intervals, fixture
  tuning) lives in `tests/system/config.test.yaml`. Do not duplicate these
  values into a root-level file.
- When adding new configurable parameters, add them to
  `tests/system/config.test.yaml` with test-appropriate values alongside
  the production example in `config.yaml.example`.
- The `--config` flag on `run_system_test.py` is an override for
  non-standard layouts; the default should always work from the project root.
