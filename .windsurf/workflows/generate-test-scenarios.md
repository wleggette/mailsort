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

Format the output to match the existing system test plan style in `docs/planning/system-test-plan.md`:

### Scenario tables

Group all scenarios by **behavioral category** (e.g., "Category 1: Skipped Sorts", "Confidence Penalty & Feedback Loop"). Each category gets:
- A short description of the behavior and which code implements it
- A table with these columns:

| ID | Scenario | Setup | Expected Behavior | Tested By |

Where **Tested By** is one of:
- `System test: <brief description of JMAP operation + run>` — for scenarios tested in the system test
- `*Deferred to unit test* (<test function name>) — <reason>` — for scenarios not practical in a system test

List scenarios in **sequential order** (L1, L2, L3...) within each category. Number them sequentially across all categories (not per-category).

### Execution sequence

After the scenario tables, provide a numbered **test execution sequence** showing the concrete steps:
1. What JMAP moves to make (and in what order)
2. When to run `mailsort run`
3. When to verify

### Verification checklist

End with a **verification checklist** — one checkbox per verifiable assertion, referencing the scenario ID:
```
- [ ] **L1**: manual audit row for sender → folder
- [ ] **L3**: rule confidence = X.XX (was Y.YY)
```

### Flags

Call out separately (above or below the tables):
- **Gaps**: scenarios not covered by existing tests
- **Contradictions**: where docs say one thing but code does another
- **Design tensions**: where the behavior may be correct per code but questionable per intent

## 7. Recommend a practical test set

From the full scenario list, recommend which to implement in the system test:
- High-value scenarios that exercise real I/O behavior
- Edge cases that unit tests can't faithfully reproduce
- A reasonable number of operations (each move is a real API call)
- Scenarios that can share setup (e.g., one correction tests both detection AND penalty)
- Group moves that can be done before a single run to minimize the number of runs needed
