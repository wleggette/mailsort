# Audit Log & Run Lifecycle

The audit system provides full traceability for every classification decision
mailsort makes. It tracks run lifecycle, per-email decisions, and outcome
reporting.

## Run Lifecycle

Each scan is assigned a unique `run_id` at startup. All audit rows created during
that scan reference the same `run_id`. This provides per-run reporting, simplifies
idempotency guarantees, and makes recovery of interrupted runs explicit.

The `runs` table stores the effective `dry_run` flag for each run. If a live
run detects a read-only JMAP token, it is automatically downgraded to dry-run
mode — `run_classification_pass` returns a `RunResult` dataclass with
`read_only_downgrade=True` so callers can display the downgrade.

JMAP move execution is treated as a set of per-email outcomes, not an atomic
transaction. Mailsort persists planned decisions before calling `Email/set`,
then reconciles each message into `moved` or `move_failed` based on the JMAP
response. If the process crashes mid-run, the incomplete run remains visible
in `runs` and can be reconciled on the next startup. Live runs (`dry_run=0`)
are abandoned unconditionally; dry runs only after `stale_dry_run_minutes`.

## Run Reporting & Logging

Each run produces a structured summary that accounts for every email in the
inbox. The numbers must add up to the total inbox count for clarity.

### Classification Scope

All inbox emails are classified and logged — see [classification.md](classification.md)
for the full list of pre-classification filters and post-classification gates.

### Outcome Categories

Every email in the audit log has one of these outcomes:

| Outcome | Badge in UI | When |
|---------|-------------|------|
| **moved** | `moved` (green) | Email was moved to target folder |
| **dry run** | `dry run` (blue) | Would have moved, but `--dry-run` mode |
| **unread** | `unread` (gray) | Email not yet read by user |
| **flagged** | `flagged` (gray) | Email flagged by user |
| **too new** | `too new` (gray) | Email received less than `min_age_minutes` ago |
| **below threshold** | `below threshold` (gray) | Confidence below move threshold |
| **below threshold (known contact)** | `below threshold (known contact)` (gray) | LLM confidence below stricter known-contact threshold |
| **no classification** | `no classification` (gray) | No rule/thread/LLM match |
| **llm unavailable** | `llm unavailable` (gray) | LLM not configured or API error |
| **unknown folder** | `unknown folder` (gray) | Target folder no longer exists |

The UI shows just the reason — no "left in inbox /" prefix.

**Note on `source="system"`:** When all classification tiers fail (LLM
unavailable, gated, or API error), `build_move_decision` creates a fallback
classification with `source="system"`. The skip_reason is the specific cause
(e.g., `llm_unavailable`, `no_classification`). The UI shows an amber badge
for the system source in the classification source column.

### Log Format

```
── Run a1b2c3d4 started (live) ──
Inbox: 49 emails
Learning: 3 user sort(s) detected, 0 rule(s) adjusted
  From inbox:       1  (user manually sorted from inbox before we processed)
  From other:       2  (user moved a mailsort-sorted email to a different folder)
Classification: 49 emails
  Rule match:      17
  Thread match:     8
  LLM match:       23
  No match:         1
  LLM cache: 12 hit(s)
Outcome:
  Moved:           35  (rule: 17, thread: 8, llm: 10)
  Unread:           5
  Flagged:          1
  Too new:          2
  Below threshold:  4
  No classification: 2
── Run a1b2c3d4 completed (44.4s) ──
```

Key naming decisions:
- **"Would move" / "Moved"** — dry run says "would move", live run says "moved"
- **"From inbox" / "From other"** for corrections — distinguishes between emails
  the user sorted themselves (before mailsort saw them) vs emails the user
  relocated after mailsort moved them
- **No "left in inbox" prefix** — the outcome reason stands on its own

### Correction Counting

The learning step detects user sorts across five categories, but for analysis
reporting only **Category 2** (correction sorts) matters — these are emails
mailsort moved that the user relocated to a different folder. They are stored
with `classification_source='correction'` and optionally carry the `rule_id`
of the original move.

Categories 1 (skipped sorts), 3 (inbox departures), and 4 (folder scan) are
stored as `classification_source='manual'`. These represent user sorting
activity, not corrections of mailsort mistakes.

### Web UI Visualization

- **Dashboard**: total processed count (all non-bootstrap audit rows, including
  dry runs) and unique email count
- **Audit log**: each entry shows its classification source (color-coded badge)
  and outcome. Corrections with a `rule_id` link to the rule detail page.
- **Analyze**: correction count = distinct emails with
  `classification_source='correction'`. Error rate = corrections / moved × 100.
  Excludes bootstrap and dry runs.
