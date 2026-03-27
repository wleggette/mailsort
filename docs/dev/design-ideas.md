# Design Ideas

Captured ideas for future features with enough context to pick them up later
without re-investigating from scratch.

---

## Correction Penalty Tuning — One-Strike Deactivation Problem

**Status:** Needs investigation (2026-03-26)

### Problem

With current thresholds, **every rule is deactivated after a single user
correction**. The `correction_penalty` (0.15) is large enough relative to
the gap between any rule's starting confidence and the `rule_move` threshold
(0.85) that no auto-created rule survives even one correction:

| Rule type | Max confidence | After 1 correction | Deactivated? |
|-----------|---------------|--------------------| -------------|
| list_id | 0.95 | 0.80 | Yes (< 0.85) |
| exact_sender (5 emails) | 0.95 | 0.80 | Yes |
| exact_sender (3 emails) | 0.89 | 0.74 | Yes |
| sender_domain (8 emails) | 0.90 | 0.75 | Yes |
| sender_domain (5 emails) | 0.85 | 0.70 | Yes |

A rule would need `confidence ≥ 1.0` to survive one correction. No
auto-created rule reaches 1.0.

### Secondary problem: recovery in the new direction is nearly impossible

After deactivation, the old evidence (e.g., 5 bootstrap rows → Banks) poisons
the coherence calculation for the corrected direction (e.g., Stores). Example:

- chase.com: 6 audit_log rows → Banks, 1 manual correction → Stores
- `maybe_create_rule(target_folder="Stores")`: coherence = 1/7 = 14% → no rule
- Would need **many** manual sorts to Stores before coherence reaches 80%

Meanwhile, the old direction (Banks) has excellent coherence (6/7 = 86%) and
would be instantly recreated if any manual sort to Banks is detected — even
though the user just told the system that Banks was wrong for this email.

### Options to consider

**Option A: Reduce correction_penalty (e.g., 0.05)**
- Pro: Rules survive 1-2 corrections, gradual degradation
- Pro: Matches the design doc's stated intent ("accumulated negative signal")
- Con: Bad rules persist longer — user sees the same misroute multiple times
  before the rule deactivates
- Deactivation after: 2-3 corrections (depending on starting confidence)

**Option B: Raise starting confidence for high-evidence rules**
- Pro: Rules backed by 10+ emails are harder to kill than rules from 3 emails
- Pro: Proportional — more evidence = more trust
- Con: More complex confidence formula
- Example: `exact_sender` with 10 emails → confidence 1.10 (capped at 1.0),
  survives 1 correction (0.85 = threshold)

**Option C: Scale penalty by evidence count**
- Pro: A correction against a rule with 50 evidence emails is a weaker signal
  than against one with 3 emails
- Pro: Natural dampening — well-established rules are resilient
- Con: More complex, harder to reason about
- Example: `penalty = base_penalty / log2(evidence_count + 1)`

**Option D: Use a correction ratio instead of absolute penalty**
- Pro: Deactivation requires a *pattern* of corrections, not a single event
- Pro: A single accidental drag-and-drop doesn't kill a good rule
- Con: Needs a new tracking mechanism (correction count per rule)
- Example: Deactivate when `corrections / (corrections + hits) > 0.2`

**Option E: Keep one-strike but fix recovery**
- Pro: Aggressive correction is "safe by default" — the stated goal
- Pro: Simpler than changing the penalty math
- Con: Recovery still depends on manual sorts
- Fix: When a correction is detected, evaluate rule creation for the OLD
  folder too (not just the correction destination). If the old evidence
  still has high coherence, leave the rule active but flag it for review
  instead of deactivating.

### Research questions — analyze your real inbox before deciding

Run these against your production account (after deploying) to understand
what correction patterns actually look like:

1. **How often do you correct mailsort?** If corrections are rare (< 1% of
   moves), the one-strike policy may be fine — you'd rarely hit it. If
   corrections are frequent (> 5%), one-strike kills too many rules.

   ```sql
   -- Correction rate after mailsort is running on real account
   SELECT
     COUNT(*) FILTER (WHERE classification_source = 'manual' AND
       email_id IN (SELECT email_id FROM audit_log WHERE moved = 1
                    AND classification_source != 'manual')) as corrections,
     COUNT(*) FILTER (WHERE moved = 1 AND classification_source != 'manual') as total_moves,
     ROUND(100.0 * corrections / total_moves, 1) as correction_pct
   FROM audit_log WHERE created_at >= datetime('now', '-30 days');
   ```

2. **Are corrections one-offs or patterns?** If the same rule gets corrected
   multiple times, it's genuinely wrong. If corrections are scattered across
   many rules with 1 correction each, they may be accidents.

   ```sql
   -- Corrections per rule
   SELECT r.rule_type, r.condition_value, COUNT(*) as corrections
   FROM audit_log a
   JOIN rules r ON r.id = a.rule_id
   WHERE a.classification_source = 'manual'
     AND a.email_id IN (SELECT email_id FROM audit_log WHERE moved = 1 AND rule_id IS NOT NULL)
   GROUP BY r.id ORDER BY corrections DESC;
   ```

3. **How many senders route to multiple folders?** This is the "Amazon
   problem" — senders that legitimately split across folders. If many of
   your senders do this, aggressive correction makes sense. If most senders
   are coherent, the penalty is too harsh.

   ```sql
   -- Senders that route to multiple folders
   SELECT from_address, COUNT(DISTINCT target_folder) as folders, COUNT(*) as emails
   FROM audit_log WHERE moved = 1
   GROUP BY from_address HAVING folders > 1
   ORDER BY emails DESC LIMIT 20;
   ```

4. **What's the typical evidence count behind a rule when it gets corrected?**
   If rules with 20+ evidence emails get corrected, the correction is probably
   an accident. If rules with 3 evidence emails get corrected, they were
   probably wrong.

   ```sql
   -- Evidence count for corrected rules
   SELECT r.condition_value, r.rule_type,
     (SELECT COUNT(*) FROM audit_log WHERE from_address = r.condition_value
      AND moved = 1 AND classification_source != 'manual') as evidence_count
   FROM rules r WHERE r.confidence < 0.85
   ORDER BY evidence_count DESC;
   ```

5. **Do inbox returns happen?** If users frequently move emails back to inbox
   (re-read, respond later), the "ignore inbox returns" design is important.
   If inbox returns are rare, it doesn't matter.

### Script idea

Consider building an analysis script (like `scripts/analyze_list_unsubscribe.py`)
that runs against your real account after a few weeks of operation and
calculates the metrics above. The answers will determine which option is right.

---

## Web UI Threshold Analysis Page (`/analyze`)

**Status:** Designed, not implemented (2026-03-21)

### Concept

Interactive web version of the `mailsort analyze` CLI command. Would show
classification source breakdown, move outcomes, LLM confidence histogram with
current threshold marked, skipped-then-sorted table, and rule correction stats.
Date range picker for filtering.

Full design in [docs/design/web-ui.md](../design/web-ui.md) Phase 5. The CLI
version (`mailsort analyze --days N`) is already implemented in `main.py`.

### Why not yet implemented

Lower priority than the core monitoring pages (dashboard, audit log, rules)
which are all complete. The CLI `analyze` command covers the same data for now.

---

## List-Unsubscribe Combined Rule

**Status:** Not prioritized (2026-03-21)

### Concept

A new rule type that combines `sender_domain` + presence of the `List-Unsubscribe`
header to classify bulk/marketing emails that lack a `List-Id` header. This would
fill the gap between `list_id` rules (which require a `List-Id` header) and
`sender_domain` rules (which require ≥5 emails from ≥3 distinct senders).

Example: `domain=substack.com + has_unsubscribe=True → Social/Newsletters`

### Analysis (2026-03-21)

Ran `scripts/analyze_list_unsubscribe.py` against 2,628 emails across INBOX,
Affairs/*, and People/* folders. Findings:

| Metric | Count | % |
|--------|-------|---|
| Total emails scanned | 2,628 | 100% |
| Have `List-Unsubscribe` header | 192 | 7.3% |
| Have `List-Unsubscribe` but NO `List-Id` | 156 | 5.9% |
| ↳ Coherent (all go to single folder) | 108 | 4.1% |
| ↳↳ Already covered by `sender_domain` rule | 34 | 1.3% |
| ↳↳ Already covered by `exact_sender` rule | 29 | 1.1% |
| **↳↳ True gap (no existing rule covers)** | **45** | **1.7%** |

The 45 true-gap emails come from 37 domains, almost all single-sender with
1–2 emails each — below the `exact_sender` threshold of 3. They'll naturally
get covered as more email arrives.

Top coherent domains (all go to one folder):

| Domain | Emails | Covered by |
|--------|--------|-----------|
| linkedin.com | 34 | sender_domain (7 senders, 97% coherence) |
| facebookmail.com | 6 | exact_sender (6/6) |
| e.progressive.com | 5 | exact_sender (5/5) |
| lmco.com | 5 | exact_sender (4/5) |

6 domains were split across folders (e.g., `citi.com` across Banks + INBOX)
and wouldn't qualify for any combined rule due to low coherence.

### Why not prioritized

1. **Small incremental value** — only 1.7% of emails would benefit, all from
   low-volume senders that will qualify for `exact_sender` rules over time.
2. **Existing rules cover most of the gap** — 58% of the coherent unsub-only
   emails are already handled by `sender_domain` or `exact_sender` rules.
3. **The combined rule would only help sooner** — it would classify emails at
   1–2 occurrences instead of waiting for 3 (exact_sender threshold). This is
   a marginal timing improvement, not a coverage improvement.

### Implementation notes (from building the analysis script)

**JMAP header property naming:**
- Fastmail's JMAP rejects `header:list-unsubscribe:asText` (the lowercase
  `:asText` variant used for `list-id`). It returns `invalidArguments`.
- The working property name is `header:List-Unsubscribe` (case-sensitive,
  no `:asText` suffix). Returns `null` when the header is absent.
- The existing `JMAPClient` falls back from `EMAIL_PROPERTIES` (which includes
  `header:list-unsubscribe:asText`) to `_EMAIL_PROPERTIES_NO_UNSUB` when it
  gets an error — so the client silently drops the header. If implementing
  this feature, the property name in `EMAIL_PROPERTIES` needs to be fixed to
  `header:List-Unsubscribe`.

**Where the header is already modeled:**
- `JMAPEmail.list_unsubscribe` field exists in `jmap/models.py` (aliased to
  `header:list-unsubscribe:asText`) — would need the alias updated.
- `EmailFeatures.list_unsubscribe` field exists — already carries the value
  through the pipeline.
- The `_EMAIL_PROPERTIES_NO_UNSUB` fallback in `jmap/client.py` would need
  updating if the property name changes.

**Rule engine changes needed:**
- New rule type `domain_unsubscribe` (or extend `sender_domain` with a flag).
- Auto-rule generation: evaluate domain coherence for emails where
  `list_unsubscribe IS NOT NULL AND list_id IS NULL`.
- Classification priority: would slot between `list_id` and `exact_sender`
  (since it's broader than exact_sender but more specific than plain
  sender_domain).

### Analysis script

`scripts/analyze_list_unsubscribe.py` — scans Fastmail folders and reports
List-Unsubscribe prevalence, coverage gaps, and domain coherence. Can be re-run
to reassess if this feature becomes worth implementing as email volume grows.

```bash
.venv/bin/python scripts/analyze_list_unsubscribe.py
```

Requires `FASTMAIL_API_TOKEN` in `.env` (read-write token works; read-only
token also works but the header property may fail on some token configurations).
