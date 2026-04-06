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

### Correction Subtotals

The learning step detects user sorts across five categories, but for reporting
they are grouped into two user-facing buckets:

| Bucket | Learner categories | Meaning |
|--------|-------------------|---------|
| **From inbox** | Category 1 (skipped sorts) + Category 3 (inbox departures) | User sorted an email out of the inbox themselves |
| **From other** | Category 2 (correction sorts) + Category 2b (correction reversals) + Category 4 (folder scan) | User moved an email from one non-inbox folder to another |

This distinction matters because "from inbox" tells you about emails mailsort
missed or couldn't classify, while "from other" tells you about emails
mailsort classified incorrectly.

### Web UI Visualization

The dashboard and audit log pages should reflect these same breakdowns:

- **Dashboard**: last run card shows the full inbox breakdown
- **Audit log**: each entry shows its classification source and outcome
- **Analyze**: correction counts split into "from inbox" and "from other"
  to distinguish missed emails from misclassified ones
