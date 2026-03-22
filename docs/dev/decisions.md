# Design Decisions

Log of architectural and design decisions that shaped the current
implementation. Explains WHY the code works the way it does. Reverse
chronological — newest entries first.

---

## 2026-03-21 — System test vs unit test boundary for bootstrap scenarios

**Context:** Some bootstrap behaviors (deleted folder evidence filtering,
coverage calculation accuracy, per-contact error isolation) are documented
in the architecture phase cards but not tested in the system test plan.
Should they be system tests or unit/integration tests?

**Decision:** Cover these in unit/integration tests, not in the system
test plan.

**Rationale:** System tests add value when they exercise **real network
I/O, real JMAP behavior, or real LLM non-determinism** — things that
mocks can't faithfully reproduce. These three scenarios are pure logic
that is fully testable with mocks:

- **Deleted folder filtering** — a set membership check
  (`target_folder in live_folders`). The integration test creates two
  folder trees and verifies the rule is deactivated. No JMAP needed.
- **Coverage calculation** — calls `classify()` against a DB. No
  network I/O. The integration test verifies exact match/unmatch counts.
- **Per-contact error isolation** — requires injecting a malformed
  ContactCard, which isn't possible via normal JMAP. The unit test mocks
  a bad record alongside a good one.

A system test for these would add setup complexity (multi-step JMAP
operations, folder creation/deletion) without catching bugs that the
unit tests miss. The system test plan notes these as "covered by
unit/integration tests" with references to the specific test functions.

**Affected docs:** `docs/planning/system-test-plan.md` §3.4,
`docs/architecture.md` bootstrap phase cards (Covered by: field).

---

## 2026-03-21 — Create all eligible rules (not priority-blocks)

**Context:** When a sender qualifies for multiple rule types (e.g., has a
list-id AND enough emails for an exact_sender rule), should we create only
the highest-priority rule, or all eligible rules?

**Options considered:**
1. **Priority blocks** — create list_id rule, skip exact_sender since list_id
   covers it. Simpler rule table, but if the list_id rule decays or gets
   deactivated, the sender has no fallback.
2. **Create all eligible** — create both list_id and exact_sender independently.
   Classification-time priority (list_id > exact_sender > sender_domain)
   determines which fires. More rules in the table, but fallback resilience.

**Decision:** Option 2 — create all eligible rules independently.

**Rationale:**
- If a broader rule is deactivated (confidence decay, correction penalty),
  the narrower rule still covers the sender.
- The rule detail UI shows all evidence-backed rules, giving full visibility.
- No behavioral difference at classification time — priority still resolves.
- Small cost: slightly more rules in the DB, slightly more DB queries during
  `maybe_create_rule`. Negligible.

**Affected code:** `audit/learner.py` (`maybe_create_rule` returns `list[int]`),
`bootstrap.py` (`_create_rules_from_evidence` removed dedup).

---

## 2026-03-21 — Dry runs do not record rule hit_count

**Context:** During `mailsort dry-run`, rules still match emails (for
classification logging), but should the `hit_count` and `last_hit_at`
columns be updated?

**Decision:** No — dry runs skip hit recording.

**Rationale:**
- `hit_count` is used in the web UI to show rule activity.
- `last_hit_at` is used by confidence decay (rules not hit in 90+ days lose
  confidence). A dry run resetting this clock would prevent legitimate decay.
- Dry runs should be read-only from the rules perspective — observe but don't
  mutate.

**Affected code:** `classifier/rules.py` (`record_hits` parameter on
`RuleEngine`), `orchestrator.py` (passes `record_hits=not dry_run`).

---

## 2026-03-21 — Bootstrap rule source is 'auto', not 'bootstrap'

**Context:** Bootstrap creates rules by delegating to the same
`learner.maybe_create_rule` used during live learning. Should bootstrap
rules have a distinct `source` value?

**Decision:** No — bootstrap rules use `source='auto'`, same as live-learned
rules.

**Rationale:** Bootstrap uses the exact same coherence checks and thresholds
as live learning. There's no behavioral difference. A separate source value
would add complexity without value — the `runs` table already distinguishes
bootstrap runs via `trigger='bootstrap'`.
