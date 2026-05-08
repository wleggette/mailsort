# Design Ideas

Captured ideas for future features with enough context to pick them up later
without re-investigating from scratch.

## Table of Contents

### Active
- [List-Unsubscribe Combined Rule](#list-unsubscribe-combined-rule) — Not prioritized
- [Coherence Double-Counting — System Test Scenarios](#coherence-double-counting--system-test-scenarios) — Testing follow-up for 2026-05-08 fix

### Implemented
- [~~Google SSO for Web UI~~](#google-sso-for-web-ui--implemented-2026-04-28) → decisions.md §2026-04-28
- [~~Analysis Page Improvements~~](#analysis-page-improvements--implemented-2026-04-27) → decisions.md §2026-04-27
- [~~Audit Log — Deduplicated View~~](#audit-log--deduplicated-view--implemented-2026-04-28) → decisions.md §2026-04-28
- [~~Rules Detail Page — Duplicate Inflation~~](#rules-detail-page--duplicate-inflation-in-evidence--matches--fixed-2026-04-28) → decisions.md §2026-04-28

---

## Coherence Double-Counting — System Test Scenarios

**Status:** Testing follow-up (2026-05-08)

### Context

The coherence calculation in `maybe_create_rule()`, `_compute_coherence()`, and
`_count_all_time_evidence()` was double-counting emails that had been moved by
the LLM and then corrected by the user. Both rows had `moved=1`, inflating the
denominator and making coherence artificially low.

**Real-world example:** `BCBSIL_noreply@emailcxt.bcbsil.health` — 7 LLM moves to
`Affairs/Uncommon/Insurance`, 5 of which were corrected to `Affairs/Medical`, plus
1 manual sort to Medical. The query saw 13 `moved=1` rows (7 Insurance + 6 Medical)
instead of 8 distinct emails (2 Insurance + 6 Medical). Coherence for Medical was
46% instead of 75%, blocking rule creation.

**Fix applied (commit 574a378):** All coherence queries now include a
`MAX(created_at)` subquery to only count the latest `moved=1` row per `email_id`.

### Unit tests added (not yet run)

4 regression tests in `tests/test_learner.py`:

1. **`test_exact_sender_coherence_excludes_superseded_moves`** — 3 emails LLM→Orders
   then corrected→Banks. Without fix: 3/6=50%. With fix: 3/3=100%. Rule created.
2. **`test_domain_coherence_excludes_superseded_moves`** — 5 emails from 3 senders,
   all corrected. Without fix: 5/10=50%. With fix: 5/5=100%. Domain rule created.
3. **`test_list_id_coherence_excludes_superseded_moves`** — 2 list-id emails
   corrected. Without fix: 2/4=50%. With fix: 2/2=100%. List-id rule created.
4. **`test_mixed_superseded_and_fresh_moves_coherence`** — 3 corrected + 2
   non-corrected. Final state: 3 Banks, 2 Orders. Coherence = 60% — correctly
   blocks rule (negative test).

### System test scenarios (to add to system-test-plan.md)

These scenarios test the fix end-to-end with real JMAP moves. They belong in
**Phase 4: Learning & Feedback** (§6.1, auto-rule generation section).

#### Scenario L12a: Corrected LLM moves create rule (superseded move dedup)

| Step | Action | Expected |
|------|--------|----------|
| 1 | Seed 3 inbox emails from `corrections@testdomain.com` | Emails in INBOX |
| 2 | Run `mailsort run` — LLM classifies all 3 to `Affairs/Stores` | 3 audit rows: `source=llm, moved=1, target=Stores` |
| 3 | JMAP move all 3 from Stores → `Affairs/Banks` | Emails now in Banks |
| 4 | Run `mailsort run` — learner detects 3 corrections | 3 correction rows: `source=correction, moved=1, target=Banks` |
| 5 | Verify `maybe_create_rule` was triggered | `exact_sender` rule created for `corrections@testdomain.com` → Banks |
| 6 | Verify coherence | `to_target=3, total=3` (not 3/6). Superseded LLM rows excluded |

**What it tests:** The `MAX(created_at)` subquery correctly filters superseded
moves so that corrections don't poison coherence. Without the fix, step 5 would
fail (coherence = 3/6 = 50%, below 80% threshold).

**Prerequisite:** The sender must NOT match any existing rule (so LLM is used),
and must NOT be in `skip_senders`.

#### Scenario L12b: Partial corrections don't create rule (boundary)

| Step | Action | Expected |
|------|--------|----------|
| 1 | Seed 5 inbox emails from `partial@testdomain2.com` | Emails in INBOX |
| 2 | Run `mailsort run` — LLM classifies all 5 to `Affairs/Stores` | 5 LLM move rows |
| 3 | JMAP move 3 of 5 from Stores → Banks (leave 2 in Stores) | 3 in Banks, 2 in Stores |
| 4 | Run `mailsort run` — learner detects 3 corrections | 3 correction rows |
| 5 | Verify NO rule created | Coherence = 3/5 = 60% (3 corrections + 2 uncorrected). Below 80% |

**What it tests:** Partial corrections correctly reduce coherence. The fix
doesn't over-correct by ignoring non-superseded LLM moves.

#### Scenario L12c: compute_rule_confidence uses deduped coherence

| Step | Action | Expected |
|------|--------|----------|
| 1 | Create an `exact_sender` rule manually via bootstrap evidence (5 emails, all to Banks) | Rule active, confidence ~0.95 |
| 2 | Run `mailsort run` — rule moves 3 new emails to Banks | 3 rule-move rows |
| 3 | JMAP move 2 of the 3 from Banks → Stores | 2 corrections |
| 4 | Run `mailsort run` — `compute_rule_confidence()` runs | Coherence computed from deduped rows: 6 to Banks (5 bootstrap + 1 uncorrected), 2 to Stores. Coherence = 6/8 = 75% |
| 5 | Verify confidence | `base × 0.75 × staleness − corrections × penalty`. Not `base × 0.375` (which would be the buggy 6/16 calculation) |

**What it tests:** `_compute_coherence()` (used by `compute_rule_confidence()`)
also correctly excludes superseded rows, not just `maybe_create_rule()`.

### Additional testing notes

**Existing tests to verify pass:** All existing `test_learner.py` coherence tests
should still pass because they use distinct `email_id` values per row (no
superseded moves). The fix is a no-op for single-row-per-email scenarios.

**Edge case to consider:** If two `moved=1` rows for the same `email_id` have the
exact same `created_at` timestamp (unlikely but theoretically possible with fast
test seeding), the `MAX(created_at)` subquery would match both. The `_seed_audit_row`
helper uses `datetime('now')` which has second-level granularity. Tests that insert
multiple rows for the same email_id in quick succession should add a small delay or
explicit timestamps. The 4 unit tests above use sequential inserts which should get
distinct timestamps, but verify on a fast machine.

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

## ~~Google SSO for Web UI~~ — IMPLEMENTED (2026-04-28)

Moved to `decisions.md` §2026-04-28 (Phase 9 — Google SSO Authentication).

---

## ~~Analysis Page Improvements~~ — IMPLEMENTED (2026-04-27)

Moved to `decisions.md` §2026-04-27. See commit `7af0ad4`.

---

## ~~Audit Log — Deduplicated View~~ — IMPLEMENTED (2026-04-28)

Moved to `decisions.md` §2026-04-28.

---

## ~~Rules Detail Page — Duplicate Inflation in Evidence & Matches~~ — FIXED (2026-04-28)

Moved to `decisions.md` §2026-04-28 (Rules Detail dedup).
