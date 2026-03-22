# Architecture Documentation Methodology

How to structure and maintain architecture documentation for a software
project. Designed to keep docs useful, accurate, and tightly coupled to
the codebase.

---

## 1. Three-Part Architecture Section

An architecture section should contain three distinct views, each
serving a different purpose:

### A. Functional Component Diagram

A **static view** showing modules, their responsibilities, and call
dependencies. Not execution order — just "what exists and who calls whom."

Organize by layers (e.g., External, Infrastructure, Classification,
Decision, Learning, Entry Points). Each module box should include:

- **Module name and file path** — ties the diagram to code
- **One-line responsibility** — what it does, not how
- **Key behaviors** worth calling out (e.g., "hit_count updated only
  on live runs")

This diagram answers: *"What are the moving parts?"*

### B. Bootstrap / Initialization Sequence

A **phase card sequence** showing the one-time or first-run setup
process. Use phase cards (see §2 below) rather than ladder/swimlane
diagrams — they're more readable and directly map to test scenarios.

This sequence answers: *"How does the system get from zero to ready?"*

### C. Per-Run / Steady-State Sequence

A **flowchart** showing what happens on each regular execution cycle.
Use decision diamonds (◇) to show branching logic explicitly. This is
the most test-relevant diagram because every diamond is a test scenario.

This sequence answers: *"What happens every time the system runs?"*

## 2. Phase Cards

A phase card is a structured summary of one step in a sequence. Fixed
format:

```
┌─ Phase N: Name ──────────────────────────────────────────────┐
│  Input:     What data/state enters this phase                │
│  Module:    Which code modules are involved                  │
│  Output:    What is produced (DB rows, API calls, reports)   │
│  Decisions: Branching logic, thresholds, filters             │
│  Tests:     Scenario IDs from the test plan                  │
└──────────────────────────────────────────────────────────────┘
```

### Why phase cards over ladder diagrams

- **Readable** — no need to trace arrows across participants
- **Testable** — each Decision bullet maps directly to a test scenario
- **Maintainable** — adding a decision is a bullet point, not a
  diagram restructuring exercise
- **Cross-referenced** — the Tests field creates a bidirectional link
  to the test plan

### When to use a flowchart instead

Use a flowchart (with decision diamonds) when the step has **significant
branching logic** — multiple conditional paths with different outcomes.

*Example:* A classification pipeline that tries thread context → rules →
LLM with fallback at each stage is better as a flowchart than a phase
card because the branching IS the interesting part.

Phase cards work better for **sequential phases** where each phase has
internal decisions but the overall flow is linear.

## 3. Implementation Notes in Diagrams

When the conceptual model differs from the code structure, add a
one-line note rather than distorting the diagram:

```
┌─ Phase 2: Generate Descriptions ─────────────────────┐
│  (in code: done per-folder inline during Phase 1)    │
│  ...                                                 │
```

This keeps the diagram clean for thinking about test scenarios while
being honest about the implementation.

Similarly, for infrastructure steps that run before every sequence but
aren't interesting enough for their own phase:

```
┌─ Phase 1: Collect Evidence ──────────────────────────┐
│  (pre: reconcile_folders — deactivate stale rules)   │
│  ...                                                 │
```

## 4. Keeping Architecture in Sync with Code

### What lives in the architecture doc

- **Diagrams and sequences** — the visual/structural view
- **Phase cards** with test cross-references
- **Key algorithms in pseudocode** — only when the logic is complex
  enough that reading the code isn't sufficient

### What lives in design docs (not architecture)

- **Detailed schemas** (SQL DDL, Pydantic models)
- **API contracts** (JMAP methods, LLM prompts)
- **Configuration reference** (all fields with defaults)
- **Error handling patterns**

### Sync discipline

- **After any behavioral change**, update the affected phase card's
  Decisions and Tests fields.
- **After adding a module**, add it to the component diagram.
- **After changing execution order**, update the relevant sequence.
- **Pseudocode in docs should match real code** — if a function
  signature changes (e.g., return type), update the pseudocode. When
  in doubt, simplify the pseudocode rather than letting it drift.

## 5. Pseudocode Conventions

When including pseudocode in architecture docs:

- **Simplify** — show the decision logic, not the error handling.
  The reader should understand the algorithm, not re-implement it.
- **Use real function names** — so readers can find the actual code.
- **Mark simplifications** — if you omit error handling or batching,
  say so: `# (error handling omitted for clarity)`
- **Keep it short** — if pseudocode exceeds ~30 lines, it probably
  belongs in a design doc, not the architecture overview.

## 6. Documentation Hierarchy

```
Architecture doc        → "What are the parts? What order do they run?"
  ↓ references
Design docs (per module)→ "How does this module work in detail?"
  ↓ references
Code                    → "What does it actually do?"
  ↓ verified by
Test plan               → "Does it do what the architecture says?"
  ↑ cross-referenced from
Architecture phase cards (Tests: field)
```

Each level adds detail. No level should duplicate another — the
architecture doc should not contain SQL schemas (that's the data models
design doc), and design docs should not repeat the sequence diagrams
(that's the architecture doc).
