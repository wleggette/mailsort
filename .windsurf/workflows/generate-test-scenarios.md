---
description: Generate comprehensive test scenarios from architecture and design docs for a specific feature or phase
---

When asked to generate test scenarios for a feature or system test phase, follow this process:

## 1. Gather all source material

Read these in order, collecting every behavioral requirement:

- **Design doc** for the feature (`docs/design/*.md`) — the intended behavior, algorithms, edge cases mentioned in prose
- **Architecture doc** (`docs/architecture.md`) — sequence diagrams, phase cards, cross-component interactions
- **Actual implementation code** (`src/mailsort/`) — every branch, guard, early return, error handler, and configurable threshold. The code is the ground truth; docs may be aspirational.
- **Existing test plan** (`docs/planning/system-test-plan.md`) — what's already covered
- **Existing unit tests** (`tests/`) — what's already tested at a lower level

## 2. Extract all distinct behaviors

For the feature under test, enumerate:

- **All behavioral categories** (e.g., detection categories, classification tiers, rule types)
- **All configurable thresholds** that affect behavior (and their current values)
- **All conditional branches** in the code — each `if/elif/else`, each early `return`, each `continue`
- **All error handling paths** — what happens when I/O fails, data is missing, or state is unexpected
- **All interactions with other subsystems** — what does this feature read from? Write to? Depend on?

## 3. For each behavior, generate scenarios across 5 dimensions

For every distinct behavior identified in step 2, consider:

### a. Happy path
The normal case where inputs meet all criteria and the feature does its main job.

### b. Boundary cases  
Inputs exactly at thresholds. Test both sides:
- Exactly at threshold (should pass)
- One below threshold (should fail)

### c. Negative cases
Inputs that should be explicitly rejected or ignored:
- Below required counts
- Low coherence
- Wrong state (e.g., already processed, already corrected)
- Deliberately excluded (e.g., inbox returns, system folders)

### d. Interaction/priority cases
When multiple behaviors could apply, which one wins?
- Priority ordering between detection categories
- Rule type priority at classification time
- What happens when the same email triggers multiple behaviors

### e. State mutation verification
After the behavior runs, verify all side effects:
- What DB rows were created/modified?
- What DB rows were NOT modified (that shouldn't be)?
- What counts changed? What timestamps updated?
- Are there invariants that must hold? (e.g., hit_count=0 after dry run)

## 4. Cross-reference with existing coverage

For each scenario, check:
- Is it already in the system test plan? → Skip or note as covered
- Is it already in unit tests? → Note as "covered by unit test, skip in system test"
- Is it practically testable in a system test? Consider:
  - Does it require real JMAP I/O to be meaningful?
  - Can the preconditions be set up via JMAP operations?
  - Does it depend on timing (e.g., 24h interval) that makes it impractical?
  - Would it require manipulating internal DB state directly?

## 5. Do the math

For any threshold-based behavior:
- Calculate the actual values with current config (don't assume — compute)
- Verify that the test plan's expected outcomes match the math
- Flag any cases where the documented expectation contradicts the implementation
  (e.g., "FP1 expects confidence stays above threshold, but 0.95 - 0.15 = 0.80 < 0.85")

## 6. Present results

Output a table of proposed scenarios with:

| ID | Category | Scenario | Setup (what JMAP operations or state changes) | Expected behavior | Verification checks | Testable in system test? | Notes |

Group by behavioral category. Flag:
- **Gaps**: scenarios not covered by existing tests
- **Contradictions**: where docs say one thing but code does another
- **Design tensions**: where the behavior may be correct per code but questionable per intent
- **Impractical**: scenarios better suited to unit tests (with explanation)

## 7. Recommend a practical test set

From the full table, recommend which scenarios to implement in the system test, considering:
- High-value scenarios that exercise real JMAP behavior
- Edge cases that unit tests can't faithfully reproduce
- A reasonable number of JMAP operations (each move is a real API call)
- Scenarios that can share setup (e.g., one correction tests both detection AND penalty)
