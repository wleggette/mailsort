# Design Ideas

Captured ideas for future features with enough context to pick them up later
without re-investigating from scratch.

---

## ~~Correction Penalty Tuning — One-Strike Deactivation Problem~~

**Status:** Superseded (2026-04-05) — replaced by the Computed Confidence Model in
"Coherence Drift on Active Rules" below. The correction penalty is now 0.05 (3-strike
rule) as part of a computed formula that also incorporates coherence and staleness.
Research questions below remain useful for validating tuning once the system is running.

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

## ~~Web UI Threshold Analysis Page (`/analyze`)~~ — Implemented

**Status:** Implemented (2026-03-27)

Implemented as `web/routes/analyze.py` + `web/templates/analyze.html`.
Includes classification sources bar chart, LLM confidence distribution table,
skipped-then-sorted table, rule corrections table, and recommendations.
Date range picker (7d / 30d / 90d).

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

---

## ~~Dry-Run Aware Stale Run Reconciliation~~

**Status:** Implemented (2026-04-03) — M10 migration added `dry_run` column
to `runs` table. `reconcile_stale_runs` now only abandons `dry_run=0` rows.
Dashboard shows "dry run" badge for dry-run runs.
---

## Coherence Drift on Active Rules

**Status:** Needs implementation (2026-04-04)

### Problem

Rules are auto-created only when coherence ≥ 80% (`auto_rule_domain_coherence`), but
coherence is never re-evaluated after creation. At classification time, the rule engine
checks only the rule's stored `confidence` value — not live coherence.

Over time, as new emails from a sender arrive and get sorted to different folders (by
LLM, thread context, or manual moves), live coherence can drift well below the creation
threshold. The rule continues to fire because its stored confidence remains high.

**Observed example:** Rule 8 (`exact_sender` for `yzhuang1@gmail.com`) has 16% live
coherence but is still actively moving mail, because its stored confidence still exceeds
the `rule_move` threshold (0.85).

Existing safeguards do not cover this case:

- **`_penalize_rule`** only fires on explicit user corrections to a rule's move.
- **`adjust_rule_confidence`** only decays rules that haven't been hit in 90 days. A
  rule that keeps matching is never touched.

### Design: Computed Confidence Model

**Summary:** Replace the current static confidence model (set once at creation, modified
only by per-event penalties) with a **computed confidence** model where confidence is
derived from live state each cycle. Confidence reflects reality bidirectionally — if
conditions improve, confidence recovers; if conditions worsen, confidence drops.

#### Formula

```
confidence = base_confidence × coherence_factor × staleness_factor
             − (net_corrections_in_window × correction_penalty)
```

Each factor is computed from live data every cycle and written to the `confidence` column.
The rule engine at classification time reads the stored value as before — no changes to
the classification hot path.

**Components:**

- **`base_confidence`** — set once at rule creation from rule type and evidence count.
  Never changes. Stored in a new `base_confidence` column (or computed from evidence
  count at query time). All values are configurable via `BaseConfidenceConfig`.
  - `list_id`: `base_confidence.list_id` (default 0.95)
  - `exact_sender`: `min(cap, floor + evidence_count × per_evidence)`
    where defaults are floor=0.80, cap=0.95, per_evidence=0.03
  - `sender_domain`: `min(cap, floor + evidence_count × per_evidence)`
    where defaults are floor=0.75, cap=0.90, per_evidence=0.02

- **`coherence_factor`** — live coherence computed from audit_log within the lookback
  window (default 30 days). `(emails matching condition → rule's target folder) /
  (all emails matching condition that were moved)`. Ranges 0.0–1.0.
  - Minimum sample size: ≥ 3 emails in the window. Below this, coherence_factor = 1.0
    (benefit of the doubt).

- **`staleness_factor`** — based on `last_relevant_at` (see "Staleness: `last_relevant_at`
  Over `last_hit_at`" below). 1.0 if a matching email was sorted to the rule's target
  folder within the staleness threshold (default 365 days). Decays linearly toward a
  floor (0.6) over the decay period (default 365 days) after the threshold. Resets to
  1.0 when any matching email is sorted to the target folder (by rule, LLM, or user).
  - Formula: if `days_since_last_relevant ≤ staleness_threshold`: 1.0
  - Otherwise: `max(0.6, 1.0 − (days_past_threshold / staleness_decay_days) × 0.4)`

- **`net_corrections_in_window`** — count of user corrections against this rule in the
  lookback window, minus manual sorts *to* the rule's target folder for the same
  condition (sender/domain/list-id) in the same window. Floored at 0.
  - A "correction" = user relocated an email that this rule moved to a different non-inbox
    folder (Category 2 detection).
  - A "confirming sort" = user manually sorted an email matching this rule's condition to
    the rule's target folder.
  - `net_corrections = max(0, corrections_away − confirming_manual_sorts)`

- **`correction_penalty`** — fixed per-correction amount (default 0.05, configurable).

#### Key Properties

- **State-based, not time-based.** If nothing changes between cycles, confidence doesn't
  change. The formula is idempotent — running twice with the same inputs produces the
  same result.
- **Bidirectional.** If coherence improves, confidence goes up. If a stale rule gets a
  hit, confidence recovers. If corrections are sorted back, penalty shrinks. No one-way
  ratchets.
- **Deactivation at floor.** Confidence is floored at 0 (`max(0, ...)`). When computed
  confidence drops below `deactivation_threshold` (default 0.50), the rule is set to
  `active=0`. A rule below 0.50 has either very poor coherence (<~55%) or heavy
  corrections — it's not recovering without a significant pattern change. The threshold
  of 0.50 was chosen because it approximates `staleness_floor` (0.6) × minimum
  `base_confidence` (0.80) = 0.48 ≈ 0.50: the lowest confidence a merely-stale rule
  with good coherence would reach. Anything below that reflects genuinely bad evidence.
  Since `maybe_create_rule` uses `find_rule_any_status`, deactivated rules are
  reactivated (not duplicated) if evidence later supports it.
- **Confidence gate handles most cases.** Between `deactivation_threshold` (0.50) and
  `rule_move` (0.85), the rule stays `active=1` but doesn't fire — the rule engine's
  existing confidence gate filters it out. If conditions improve, confidence recovers
  and the rule resumes firing without any deactivation/reactivation cycle.
- **Immediate correction response.** Each correction subtracts 0.05 from confidence
  regardless of evidence volume. 3 corrections stops any rule. This is independent of
  the coherence factor, so high-volume rules don't absorb corrections.
- **Corrections are recoverable.** User can sort back to cancel corrections (1:1).
  Corrections also age out of the 30-day window automatically.

#### Rule Reactivation Over Duplication

The current `maybe_create_rule` skips creation if `find_existing_rule` finds an active
rule with the same type+condition. For rules above `deactivation_threshold` but below
`rule_move`, the computed model keeps them `active=1` with low confidence — the confidence
formula will raise the rule's confidence if evidence improves.

For rules that *are* deactivated (confidence below `deactivation_threshold`,
`reconcile_folders` for deleted folders, or manual deactivation via the UI),
`maybe_create_rule` should be changed to:

```python
existing = find_rule_any_status(type, value)  # active=0 or active=1
if existing:
    reactivate_rule(existing, new_base_confidence)  # reactivate + reset base
else:
    create_rule(...)
```

No duplicate rows. One row per type+condition. Reactivation resets `base_confidence` from
current evidence.

> **System test coverage:** Verify that (a) `maybe_create_rule` finds inactive rules and
> reactivates them instead of creating duplicates, (b) reactivation resets
> `base_confidence` from current evidence, (c) after reactivation, the confidence formula
> runs normally on the reactivated rule, (d) only one row exists per type+condition.

#### Manual Rules

Rules with `source = 'manual'` are **exempt** from computed confidence adjustments. The
user explicitly created them; modifying confidence silently would be surprising. Instead,
the web UI shows a warning badge when live coherence drops below
`auto_rule_domain_coherence` (default 0.80 — same threshold used for rule creation), and
the user decides whether to adjust or deactivate.

> **System test coverage:** Verify that (a) `compute_rule_confidence` skips rules with
> `source='manual'`, (b) manual rules retain their original confidence regardless of
> coherence or staleness, (c) the web UI shows the warning badge when live coherence is
> below 80% for a manual rule.

#### Windowed vs. All-Time Coherence

The confidence formula uses **windowed** coherence (30-day lookback) for responsiveness.
The rule detail page in the web UI shows **both** all-time and windowed coherence for
context: e.g., "all-time coherence: 65%, last 30 days: 16%".

#### Staleness: `last_relevant_at` Over `last_hit_at`

**Problem discovered:** If staleness is based on `last_hit_at` (when the rule last fired
at classification time), a stale rule that drops below the confidence gate enters a dead
zone: it can't fire → can't get a hit → can't reset staleness → stays stale. The rule is
stuck, and because it's still `active=1`, it blocks `maybe_create_rule` from creating a
replacement.

Walk-through of the dead zone:

1. Rule is 500 days stale. Staleness = 0.85. Confidence = 0.95 × 1.0 × 0.85 = 0.81.
   Below `rule_move` (0.85) — doesn't fire.
2. Sender emails again. Rule found at classification but 0.81 < 0.85 → skipped. Email
   falls to LLM.
3. LLM sorts it to the correct folder. Audit_log entry recorded.
4. Next cycle: `compute_rule_confidence` runs. Coherence is fine. But `last_hit_at`
   hasn't updated — the rule didn't fire in step 2.
5. Staleness still 500 days → confidence stays 0.81. Rule can't recover.

**Decision:** Replace `last_hit_at` with **`last_relevant_at`** for staleness computation.
`last_relevant_at` tracks the most recent email matching the rule's condition that was
sorted to the rule's target folder — regardless of whether the *rule itself* was
responsible for the sort (could be rule, LLM, thread context, or manual user action).

**How it's maintained:** The `compute_rule_confidence` method already queries audit_log for
coherence (matching emails in the lookback window). The `MAX(moved_at)` from that same
query provides `last_relevant_at` at no extra cost. Updated each cycle as a side effect
of the confidence computation.

**Column decision:** Store only `last_relevant_at` on the rules table. Drop `last_hit_at`.

Rationale for dropping `last_hit_at`:

- `last_relevant_at` answers the more useful question: "is this sender/condition still
  active?" vs. `last_hit_at`'s "did this specific rule fire recently?"
- More meaningful in the UI: "Last matching email: 3 days ago" tells the user whether the
  pattern is alive. "Last fired: 500 days ago" is misleading if the sender is active but
  the rule happens to be below the confidence gate.
- `last_hit_at` can be derived from audit_log if ever needed: `MAX(moved_at) WHERE
  rule_id = ? AND classification_source = 'rule'`. It's a rare enough query that it
  doesn't need a denormalized column.
- Eliminates the dead zone: if the sender emails again and their mail goes to the right
  folder by any means, staleness resets and the rule recovers naturally.

**Alternatives considered for column storage:**

1. **Store both `last_hit_at` and `last_relevant_at`.** Two columns. `last_hit_at` has
   marginal UI value — if the user cares whether the rule fired, they can check confidence
   (high confidence + recent relevance = firing). Rejected: unnecessary complexity.
2. **Derive both from audit_log, store neither.** Always accurate, no sync risk. Rejected:
   requires joins/subqueries on every UI page load. `last_relevant_at` is cheap to maintain
   as a side effect of the confidence computation.
3. **Update `last_hit_at` when a rule *would have matched* regardless of confidence gate.**
   Fixes the dead zone. Rejected: changes the semantics of `last_hit_at` — the column name
   implies the rule actually fired, which would be confusing.

### Alternatives Considered

#### Confidence model alternatives

1. **Static confidence with one-way penalties (original design).** Confidence set at
   creation, reduced by correction penalties (−0.15) and staleness decay (−0.10/cycle).
   Never increases. Recovery requires deactivation + re-creation via `maybe_create_rule`.
   - Why discarded: one-way ratchet creates dead zones where a rule is too weak to fire
     but still blocks creation of a replacement. Not responsive to improving conditions.
     Per-cycle penalties are time-based, not state-based — a rule is penalized identically
     whether coherence changed or not.

2. **Confidence as one-way cap (intermediate design).** Each cycle, cap confidence to
   `min(current, max_allowed_from_coherence)`. Never increases.
   - Why discarded: same dead-zone problem. A rule capped at 0.73 can't fire (below
     0.85) and can't recover even if coherence returns to 95%. Would still require
     deactivation + re-creation for recovery, creating duplicate rows.

3. **Option A: Multi-factor multiplicative model.** Four independent factors:
   `base × coherence × staleness × correction_factor`, where correction_factor is
   derived from `corrections / (corrections + hits)`.
   - Why discarded: the correction factor is diluted by volume. 1 correction out of 200
     emails produces a correction_factor of 0.995 — invisible. User corrections should
     have immediate, volume-independent impact. Also, 4 factors make tuning harder without
     adding meaningful differentiation over simpler models.

4. **Option B: Coherence-derived (two-factor).** `base × coherence × staleness`, with no
   separate correction signal. Corrections affect confidence only through their impact on
   coherence.
   - Why discarded: at high volume, corrections are diluted. 3 corrections out of 200
     emails only drops coherence to 98.5% — the rule keeps firing. The user would need to
     correct 30+ emails (15%) before the rule stops. This is unacceptable for a system
     that should respond swiftly to user feedback.

5. **Option C with ratio-based correction penalty.** Like the chosen design, but
   correction penalty = `correction_ratio × weight` instead of flat per-correction.
   - Why discarded: same dilution problem as Option B. The per-correction penalty needs to
     be volume-independent to ensure 3 corrections always stops a rule.

#### Architecture alternatives (where to compute)

1. **Check coherence at classification time.** Query audit_log before applying each rule.
   - Why discarded: adds a DB query per rule per email per cycle. Couples classification
     latency to audit_log size. Breaks the separation between the rule engine (fast
     lookup) and the learning layer (periodic analysis).

2. **Event-driven re-evaluation.** Recompute after every audit_log write.
   - Why discarded: every write would need to identify all affected rules and re-score
     them. Over-engineered for the problem. Email sorting is not real-time.

3. **Periodic computation in the learning step (chosen).** Compute confidence for all
   active auto rules every cycle, write results to DB. Rule engine reads stored values.
   - Pro: fits existing architecture, no classification slowdown, keeps rule engine as a
     simple confidence-threshold lookup.
   - Con: rules can be stale for up to one cycle (default 5 min). Acceptable.

### Scenarios and Rationale

All scenarios use `rule_move` threshold = 0.85.

#### Scenario 1: Coherence drift (sender routes to multiple folders over time)

Rule starts healthy, drifts as the sender's emails split across folders.

| State | Coherence | Staleness | Net Corrections | Confidence | Fires? |
|-------|-----------|-----------|-----------------|------------|--------|
| Healthy (150/150 to target) | 100% | 1.0 | 0 | 0.95 × 1.0 × 1.0 − 0 = **0.95** | ✓ |
| 10 emails elsewhere (140/150) | 93% | 1.0 | 0 | 0.95 × 0.93 − 0 = **0.88** | ✓ |
| 20 emails elsewhere (130/150) | 87% | 1.0 | 0 | 0.95 × 0.87 − 0 = **0.83** | ✗ |
| 30 emails elsewhere (120/150) | 80% | 1.0 | 0 | 0.95 × 0.80 − 0 = **0.76** | ✗ |
| Pattern reverses (155/170) | 91% | 1.0 | 0 | 0.95 × 0.91 − 0 = **0.86** | ✓ |

**Rationale:** 20 misrouted emails out of 150 (13%) is enough to stop the rule. This is
appropriate — that's a meaningful error rate. When the pattern reverses and coherence
recovers above ~90%, the rule starts firing again automatically. No manual intervention
needed.

> **System test coverage:** Verify that (a) a rule with drifting coherence sees computed
> confidence drop below `rule_move` and stops firing at classification, (b) when coherence
> recovers, confidence rises and the rule resumes firing in the same cycle, (c) the rule
> stays `active=1` throughout — no deactivation/reactivation. Needs audit_log rows showing
> emails splitting across folders over time.

#### Scenario 2: Staleness (periodic sender goes silent)

Rule for a newsletter or periodic sender. No emails arrive for a long time.

| Days Since Last Relevant | Staleness Factor | Confidence (base=0.95, coh=95%) | Fires? |
|-------------------------|-----------------|-------------------------------|--------|
| 30 | 1.0 | 0.95 × 0.95 × 1.0 = **0.90** | ✓ |
| 90 | 1.0 | **0.90** | ✓ (quarterly ✓) |
| 180 | 1.0 | **0.90** | ✓ (semi-annual ✓) |
| 365 | 1.0 | **0.90** | ✓ (annual ✓) |
| 400 | 0.96 | 0.95 × 0.95 × 0.96 = **0.87** | ✓ |
| 450 | 0.91 | 0.95 × 0.95 × 0.91 = **0.82** | ✗ |
| 548 (threshold + decay) | 0.80 | 0.95 × 0.95 × 0.80 = **0.72** | ✗ |
| 730 (2 years) | 0.60 | 0.95 × 0.95 × 0.60 = **0.54** | ✗ (floor) |
| Matching email sorted → fresh | 1.0 | 0.95 × 0.95 × 1.0 = **0.90** | ✓ |

**Rationale:** 365-day threshold ensures quarterly newsletters, annual statements, and
seasonal senders never decay. Only senders silent for 13+ months start losing confidence.
The decay is gentle — 90 days past the threshold to stop firing, another year to reach
the floor. If the sender emails again at any point, staleness resets to 1.0 and confidence
recovers immediately. Staleness is the least important factor — the coherence audit
catches genuinely wrong rules; staleness only catches disappeared senders.

> **System test coverage:** Verify that (a) rules within the 365-day threshold have
> staleness_factor = 1.0, (b) rules past the threshold decay linearly per the formula,
> (c) a new matching email sorted to the target folder resets `last_relevant_at` and
> restores staleness_factor to 1.0 regardless of who sorted it (rule, LLM, manual).
> Test with simulated timestamps spanning >365 days.

#### Scenario 3: User corrections — high-evidence rule (200 emails)

User manually relocates emails that the rule moved. Corrections have immediate,
volume-independent impact.

| State | Coherence | Net Corr. | Confidence | Fires? |
|-------|-----------|-----------|------------|--------|
| 0 corrections (200/200) | 100% | 0 | 0.95 − 0 = **0.95** | ✓ |
| 1 correction (199/200) | 99.5% | 1 | 0.95 − 0.05 = **0.90** | ✓ |
| 2 corrections (198/200) | 99% | 2 | 0.94 − 0.10 = **0.84** | ✗ |
| 3 corrections (197/200) | 98.5% | 3 | 0.94 − 0.15 = **0.79** | ✗ |
| User sorts 1 back | 98.5% | 2 | 0.94 − 0.10 = **0.84** | ✗ |
| User sorts 2 back | 98.5% | 1 | 0.94 − 0.05 = **0.89** | ✓ |
| User sorts 3 back | 99% | 0 | 0.94 − 0 = **0.94** | ✓ |

**Rationale:** 3 corrections stops *any* rule regardless of evidence volume. This is the
"swift and immediate" response to user feedback. Sorting back recovers the rule 1:1 — the
user's explicit actions are always respected. At high volume, coherence barely moves from
a few corrections, but the per-correction penalty ensures the system responds anyway.

> **System test coverage:** Verify that (a) correction penalty is volume-independent —
> same penalty whether 5 or 200 emails back the rule, (b) 3 corrections stops the rule,
> (c) sorting back reduces net corrections and recovers the rule, (d) net corrections
> floor at 0 (sorting back more than you corrected doesn't boost confidence above base).

#### Scenario 4: User corrections — low-evidence rule (3 emails)

A rule created at the minimum threshold. Corrections hit both coherence and the penalty.

| State | Coherence | Net Corr. | Confidence (base=0.89) | Fires? |
|-------|-----------|-----------|----------------------|--------|
| 0 corrections (3/3) | 100% | 0 | 0.89 − 0 = **0.89** | ✓ |
| 1 correction (2/3) | 67% | 1 | 0.60 − 0.05 = **0.55** | ✗ |
| 1 sort back (3/4) | 75% | 0 | 0.67 − 0 = **0.67** | ✗ |
| 2 more confirming (5/6) | 83% | 0 | 0.74 − 0 = **0.74** | ✗ |
| 4 more confirming (7/8) | 88% | 0 | 0.78 − 0 = **0.78** | ✗ |
| 30 days pass, correction ages out | ~100% | 0 | 0.89 − 0 = **0.89** | ✓ |

**Rationale:** Low-evidence rules take a double hit (coherence drops sharply + correction
penalty). Even after sorting back (which cancels the penalty), the coherence denominator
has grown, requiring many confirming emails to recover. This is appropriate — a rule built
on thin evidence should need strong confirmation after a correction. The guaranteed
recovery path is the 30-day window: once the correction ages out and coherence returns
to its natural level, the rule resumes.

> **System test coverage:** Verify that (a) low-evidence rules are hit harder by a single
> correction than high-evidence rules (both coherence and penalty compound), (b) the
> 30-day aging path works — after the correction ages out of the window and coherence
> returns to natural level, confidence recovers to base.

#### Scenario 5: Correction aging and coherence interaction

User corrects 3 emails, then changes their sorting behavior permanently. Corrections
age out after 30 days — but does the rule revive incorrectly?

| Day | State | Coherence (30d window) | Net Corr. | Confidence | Fires? |
|-----|-------|----------------------|-----------|------------|--------|
| 0 | 3 corrections | 98.5% (197/200) | 3 | 0.94 − 0.15 = **0.79** | ✗ |
| 1–30 | User sorts to new folder B | Drops as B evidence accumulates | 3 | Dropping | ✗ |
| 31 | Corrections age out | ~70% (new behavior dominates window) | 0 | 0.95 × 0.70 = **0.67** | ✗ |
| 60 | Fully shifted to folder B | ~30% | 0 | 0.95 × 0.30 = **0.29** | ✗ |

**Rationale:** The corrections and coherence operate on the same 30-day window. By the
time corrections age out, the window is filled with new evidence reflecting the changed
behavior. Coherence keeps the rule dead even without the correction penalty. The old rule
never revives incorrectly. Meanwhile, `maybe_create_rule` evaluates the new pattern
(sender → folder B) and creates/reactivates a rule for the new destination once evidence
thresholds are met.

> **System test coverage:** Critical safety test. Verify that (a) after corrections age
> out, the old rule does NOT revive when the user has shifted to a new folder, (b) a new
> rule is created/reactivated for the new folder once evidence thresholds are met, (c) the
> old rule's coherence reflects the new behavior even after corrections leave the window.

#### Scenario 6: One-off correction mistake

User accidentally drags an email to the wrong folder, then sorts it back.

| State | Net Corr. | Confidence (base=0.95, coh≈100%) | Fires? |
|-------|-----------|----------------------------------|--------|
| Accidental correction | 1 | 0.95 − 0.05 = **0.90** | ✓ |
| User sorts it back | 0 | 0.95 − 0 = **0.95** | ✓ |

**Rationale:** A single accident drops confidence by 0.05 — not enough to stop the rule
(still above 0.85). And sorting it back cancels the penalty entirely. Accidental
drag-and-drop is a non-event.

> **System test coverage:** Verify that (a) a single correction does not stop a
> high-confidence rule, (b) sorting it back fully restores confidence to pre-correction
> level.

#### Scenario 7: Low-volume sender, corrections age out with insufficient new evidence

A sender who emails once a month. User corrects 1 email. Not enough new emails arrive
in 30 days for coherence to have a strong opinion.

| Day | State | Sample size | Coherence | Net Corr. | Confidence (base=0.89) | Fires? |
|-----|-------|-------------|-----------|-----------|----------------------|--------|
| 0 | 1 correction | 4 in window | 75% (3/4) | 1 | 0.67 − 0.05 = **0.62** | ✗ |
| 15 | 1 confirming email | 5 in window | 80% (4/5) | 1 | 0.71 − 0.05 = **0.66** | ✗ |
| 31 | Correction ages out | 2 in window | < min sample | 0 | 0.89 × 1.0 − 0 = **0.89** | ✓ |
| 31 | *(if min sample = 3)* | 2 in window | insufficient → 1.0 | 0 | **0.89** | ✓ |

The rule resumes firing because there isn't enough recent evidence to judge coherence,
and the correction aged out. If the user corrects again, the penalty kicks in immediately.

**Rationale:** For very low-volume senders, the system gives the rule the benefit of the
doubt when it lacks data. This is preferable to permanently killing a rule based on a
single correction from months ago. If the correction reflected a genuine change, the user
will correct again — and the penalty is immediate.

> **System test coverage:** Verify that (a) when sample size < `coherence_min_sample`,
> coherence_factor defaults to 1.0, (b) once corrections age out and sample is below
> minimum, confidence returns to base, (c) a subsequent correction immediately re-applies
> the penalty.

#### Scenario 8: Staleness dead zone recovery via `last_relevant_at`

A rule is stale enough that it can't fire. A new email arrives and is sorted to the
correct folder by the LLM. Does the rule recover?

| Day | State | last_relevant_at | Staleness | Confidence | Fires? |
|-----|-------|------------------|-----------|------------|--------|
| 0 | Rule is 500 days stale (no matching emails) | 500 days ago | 0.85 | 0.95 × 0.95 × 0.85 = **0.77** | ✗ |
| 1 | New email arrives, LLM sorts to target folder | today | 1.0 | 0.95 × 0.95 × 1.0 = **0.90** | ✓ |
| 2 | Rule fires on next matching email | today | 1.0 | **0.90** | ✓ |

**Rationale:** Because staleness is based on `last_relevant_at` (most recent matching
email sorted to the target folder by *any* method), the LLM sort in day 1 resets the
staleness factor. The rule recovers on the next confidence computation cycle. Under the
old `last_hit_at` model, the rule would remain stuck in a dead zone because it can't fire
→ can't get a hit → can't reset staleness.

> **System test coverage:** Critical regression test for the dead zone fix. Verify that
> (a) a stale rule below the confidence gate recovers when the LLM or user sorts a
> matching email to the target folder, (b) `last_relevant_at` is updated from the
> audit_log regardless of classification_source, (c) confidence is recomputed on the
> next cycle and the rule resumes firing.

### Interaction with Existing Code

#### Replaces: `_penalize_rule`

The current `_penalize_rule` method (called from `_detect_correction_sorts`) applies a
one-time −0.15 penalty per correction and deactivates when confidence drops below
threshold. This is replaced by the `net_corrections_in_window` component of the computed
formula. The detection logic in `_detect_correction_sorts` stays — it still records
manual audit_log rows — but instead of directly modifying rule confidence, corrections
are counted by the confidence computation.

Implementation note: corrections need to be identifiable in audit_log. Currently,
Category 2 corrections are logged as `classification_source='manual'`. To distinguish
"user sorted from inbox" (Cat 1/3/4) from "user corrected a mailsort move" (Cat 2), a
flag or separate source value may be needed (e.g. `classification_source='correction'`
or a `corrects_rule_id` column).

#### Replaces: `adjust_rule_confidence`

The current method applies −0.10 per cycle after 90 days of no hits. This is replaced
by the `staleness_factor` component. Same learning-step call site, but the logic changes
from "subtract a fixed amount" to "compute factor from `last_relevant_at` age."

#### New: `compute_rule_confidence`

A new method on `Learner` that runs every cycle in the learning step. Replaces both
`_penalize_rule` and `adjust_rule_confidence`. For each active auto rule (`source != 
'manual'`):

1. Query `base_confidence` (stored on rule).
2. Compute `coherence_factor` from audit_log within the lookback window.
3. Compute `staleness_factor` from `last_relevant_at` (derived from coherence query).
4. Count `net_corrections` in the lookback window.
5. Compute `confidence = max(0, base × coherence × staleness − (net_corrections × penalty))`.
6. If `confidence < deactivation_threshold` (default 0.50): set `active=0`.
7. Update `last_relevant_at` from `MAX(moved_at)` in the coherence query results.

> **System test coverage:** Verify that (a) the method is idempotent — running twice with
> the same data produces the same confidence, (b) all three factors contribute
> independently (test each factor in isolation with the others held at 1.0/0), (c)
> confidence is written to the DB and the rule engine reads the updated value on the next
> classification pass, (d) `last_relevant_at` is updated as a side effect, (e) confidence
> is floored at 0 (never negative), (f) rules are deactivated when confidence drops below
> `deactivation_threshold`, (g) deactivated rules are reactivated by `maybe_create_rule`
> when evidence supports it (no duplicates).

#### Changes to `maybe_create_rule`

`find_existing_rule` should search for rules in **any** status (active or inactive), not
just `active=1`. If an inactive rule exists for the same type+condition, reactivate it
with a fresh `base_confidence` computed from current evidence rather than creating a
duplicate. One row per type+condition in the database.

### Configurable Parameters

#### Existing params — reused

| Parameter | Location | Current default | Role in new design |
|-----------|----------|----------------|--------------------|
| `rule_move` | `ThresholdsConfig` | 0.85 | Confidence gate at classification time (unchanged) |
| `auto_rule_domain_coherence` | `ClassificationConfig` | 0.80 | Also used as manual rule warning threshold in web UI |
| `learner_lookback_days` | `ClassificationConfig` | 7 | Detection window for manual sorts / corrections (unchanged; separate from coherence window) |

#### Existing param — default changed

| Parameter | Location | Old default | New default | Reason |
|-----------|----------|------------|-------------|--------|
| `correction_penalty` | `ClassificationConfig` | 0.15 | 0.05 | 3-strike rule; coherence audit handles aggregate signal |

#### New params — add to `ClassificationConfig`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `coherence_lookback_days` | 30 | Window for coherence and correction counting (separate from `learner_lookback_days`) |
| `coherence_min_sample` | 3 | Minimum emails in window before coherence adjusts confidence |
| `staleness_threshold_days` | 365 | Days since `last_relevant_at` before staleness decay starts |
| `staleness_decay_days` | 365 | Duration of linear decay from 1.0 to floor |
| `staleness_floor` | 0.6 | Minimum staleness factor |
| `deactivation_threshold` | 0.50 | Confidence below which a rule is set to `active=0`; approximates `staleness_floor × min(base_confidence)` — below this, the rule has poor coherence or heavy corrections and won't recover without a pattern change |

#### New config model — `BaseConfidenceConfig` (nested in `ClassificationConfig`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `list_id` | 0.95 | Fixed base confidence for list_id rules |
| `exact_sender_floor` | 0.80 | Starting confidence for exact_sender with minimum evidence |
| `exact_sender_cap` | 0.95 | Maximum confidence for exact_sender regardless of evidence count |
| `exact_sender_per_evidence` | 0.03 | Confidence increase per additional email for exact_sender |
| `sender_domain_floor` | 0.75 | Starting confidence for sender_domain with minimum evidence |
| `sender_domain_cap` | 0.90 | Maximum confidence for sender_domain regardless of evidence count |
| `sender_domain_per_evidence` | 0.02 | Confidence increase per additional email for sender_domain |

#### Schema changes (rules table)

| Column | Action |
|--------|--------|
| `base_confidence` | Add (backfill from current `confidence`) |
| `last_relevant_at` | Add (backfill from `last_hit_at`) |
| `last_hit_at` | Drop |

### Implementation Plan

| Step | File | Change |
|------|------|--------|
| 1 | `config.py` | Add configurable parameters above to `ClassificationConfig` |
| 2 | `db/migrations.py` | Add `base_confidence` column to `rules` table (backfill from `confidence`). Replace `last_hit_at` with `last_relevant_at` column (backfill from `last_hit_at` values). |
| 3 | `learner.py` | New method `compute_rule_confidence()` implementing the formula; update `last_relevant_at` from `MAX(moved_at)` in the coherence query |
| 4 | `learner.py` | Remove `_penalize_rule` direct confidence writes; keep correction detection |
| 5 | `learner.py` | Replace `adjust_rule_confidence` with staleness factor in the new method |
| 6 | `learner.py` | Update `maybe_create_rule` to find inactive rules and reactivate |
| 7 | `classifier/rules.py` | Update `find_existing_rule` to optionally search all statuses; update rule hit recording to no longer maintain `last_hit_at` |
| 8 | `orchestrator.py` | Replace `adjust_rule_confidence()` call with `compute_rule_confidence()` |
| 9 | `audit/learner.py` | Add correction identification (distinguish Cat 2 from Cat 1/3/4 in audit_log) |
| 10 | `test_learner.py` | Tests: coherence above/below threshold, staleness curve, correction penalty, net correction recovery (sort-back), correction aging, low-evidence double hit, minimum sample guard, manual rule exemption, staleness dead zone recovery via `last_relevant_at` |
| 11 | Web UI | Rule detail page: show all-time + windowed coherence, `last_relevant_at`. Warning badge on manual rules with low coherence. |

---

## Forced Recomputation of Folder Descriptions

**Status:** Not yet implemented (2026-04-05)

### Problem

Folder descriptions are generated once — during bootstrap or when a new folder is first
discovered by the orchestrator. The `generate_folder_description` function explicitly
skips folders that already have a description in the `folder_descriptions` table. Once
written, a description is never updated automatically.

This becomes a problem when:

- **Bootstrap descriptions are poor.** Early descriptions may be based on a small or
  unrepresentative sample of emails (up to 15). As more mail accumulates, the description
  may no longer reflect the folder's actual contents.
- **Folder purpose evolves.** A folder originally used for one kind of email may shift
  over time (e.g., "Projects" starts receiving vendor invoices too).
- **Fallback descriptions persist.** If the LLM was unavailable during bootstrap (no API
  key, transient error), the folder gets a generic "Emails filed under X" fallback that
  never gets replaced.

Since folder descriptions are used in the LLM classification prompt, stale or inaccurate
descriptions degrade classification quality for all emails.

### Proposed Solution

Allow the user to force recomputation of folder descriptions at three granularity levels:

1. **All folders** — regenerate descriptions for every folder in the mailbox tree.
2. **Specific folders** — regenerate for folders matching a path pattern (e.g.,
   `INBOX/Affairs/*`).
3. **Individual folder** — regenerate for a single folder by exact path.

Recomputation should:

- Delete the existing row from `folder_descriptions` for the target folder(s).
- Fetch a fresh sample of emails from each folder via JMAP.
- Call the LLM to generate a new description from the fresh sample.
- Insert the new description with `source='auto'` and updated timestamps.
- Respect `folder_description_overrides` from config — folders with manual overrides
  are skipped (the override takes precedence).

#### CLI Interface

New subcommand under the `mailsort` CLI group:

```
mailsort regenerate-descriptions [OPTIONS]

Options:
  --folder TEXT    Regenerate for a specific folder path (repeatable)
  --pattern TEXT   Regenerate for folders matching a glob pattern (repeatable)
  --all            Regenerate for all folders
  --dry-run        Show which folders would be regenerated without doing it
```

Examples:

```bash
# Regenerate all descriptions
mailsort regenerate-descriptions --all

# Regenerate a single folder
mailsort regenerate-descriptions --folder "INBOX/Affairs/Banks"

# Regenerate folders matching a pattern
mailsort regenerate-descriptions --pattern "INBOX/Affairs/*"

# Preview what would be regenerated
mailsort regenerate-descriptions --all --dry-run
```

At least one of `--folder`, `--pattern`, or `--all` is required.

#### Web UI Interface

On the `/folders` page:

- **Per-folder action:** A "Regenerate" button or icon on each folder row. Triggers an
  async POST to regenerate the description for that single folder. Shows a spinner while
  in progress, then updates the description text in place.
- **Bulk action:** A "Regenerate All Descriptions" button (with confirmation dialog).
  Triggers regeneration for all non-overridden folders. Shows progress (e.g.,
  "Regenerating 12/45 folders…").

Folders with `source='manual'` (from config overrides) should show the override badge
and disable the regenerate button.

### Implementation Notes

**Core function changes (`classifier/descriptions.py`):**

- New function `regenerate_folder_description(db, folder_path, emails, ...)` — like
  `generate_folder_description` but deletes any existing description first. Alternatively,
  use `INSERT OR REPLACE` / `UPDATE` instead of the current `INSERT`.
- New function `regenerate_descriptions_for_folders(db, folder_paths, ...)` — batch
  version that iterates over a set of folder paths, fetching emails and regenerating each.
- The existing `generate_folder_description` stays unchanged for the normal flow (only
  generates if missing).

**JMAP interaction:**

- Regeneration needs to fetch sample emails for each target folder, which requires
  a `JMAPClient` instance and the `MailboxTree` (to resolve folder path → mailbox ID).
- The CLI command needs to set up JMAP (similar to `bootstrap` command) and the web UI
  already has access to config but would need a JMAP client for the email fetch.

**Sample quality:**

- Consider fetching more recent emails rather than the oldest (current bootstrap fetches
  up to `max_per_folder` without ordering preference). For regeneration, prefer
  `sort: receivedAt desc` to get the most recent emails, which better represent the
  folder's current purpose.
- The sample size (currently 15 emails sent to the LLM) could be configurable for
  regeneration, but the default should remain 15 for consistency.

**Web UI route (`web/routes/folders.py`):**

- New POST endpoint: `/folders/{path:path}/regenerate` — regenerate a single folder.
- New POST endpoint: `/folders/regenerate-all` — regenerate all folders.
- Both need access to a `JMAPClient`, which means either creating one per request or
  sharing a client via app state (the latter is preferable for connection reuse).

### Open Questions

1. **Should regeneration update `updated_at` vs. `generated_at`?** Currently
   `generated_at` uses a SQLite default. Regeneration should update both to reflect
   when the new description was created.
2. **Rate limiting for bulk regeneration.** Regenerating 50+ folders means 50+ LLM API
   calls. Should there be a concurrency limit or delay between calls? Haiku is fast and
   cheap, so this may not matter in practice.
3. **Should the web UI show the old vs. new description?** A diff or "previous
   description" tooltip could help the user confirm the regeneration improved things.
