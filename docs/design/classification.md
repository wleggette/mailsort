# Classification Pipeline

The classification pipeline determines which folder an email belongs in. It uses
a tiered approach: thread context → rule engine → LLM cache → LLM. First match wins.

## Pipeline Steps

```
1. Assign a unique run_id for this scan
2. Extract features from all inbox emails (including threadId)
3. Compute classification_version (hash of folder descriptions + LLM model)
4. Resolve classification: thread context → rule engine → LLM cache → LLM
5. Build a MoveDecision for each email (confidence gate)
6. Apply post-classification eligibility gates (unread, flagged, too_new)
7. Persist decisions to audit_log (moved boolean + skip_reason + cached)
8. Execute a batched Email/set for all eligible moves
9. Reconcile per-email move results from JMAP into audit_log
10. Update run summary metrics and mark the run completed
```

Classification resolution (step 4) tries sources in order:

```
4a. Check thread context
    - If another email in this thread was previously sorted → inherit that folder
    - Otherwise → continue
4b. Check the rule engine
    - If a rule matches → return it (confidence gate applied later in step 5)
    - If no rule matches → fall through to LLM cache
4c. Check LLM cache (audit_log lookup)
    - If a prior LLM result for this email_id exists with created_at ≥
      classification_version_changed_at → reuse it (cached=True)
    - Otherwise → fall through to fresh LLM call
4d. Call the LLM classifier (if privacy gate allows)
    - If LLM returns a valid folder → return it (confidence gate applied later)
    - If LLM fails or is gated → skip_reason set (llm_unavailable, llm_skip_*)
```

Thread context (step 4a) handles replies and forwarded messages where the sender
changes but the conversation topic doesn't — e.g., your husband replying to a
vendor email, or a family member reply-chain. It runs before rules because a
known thread context is more reliable than any pattern match.

## Eligibility & Gating

**Pre-classification filters** (emails excluded before classification):
- `skip_senders` — never classify these senders (filtered out entirely)
- Deduplication — skip emails already processed in this run

**All remaining inbox emails are classified** (thread → rules → LLM cache → LLM), then
post-classification gates determine whether the move actually happens:

- **Confidence gate** — classification confidence must meet threshold
- **Folder resolution** (only if confident enough to move) — target folder must
  exist in the mailbox tree → `skip_reason="unknown_folder"` if missing
- **Eligibility gates** (always run, override prior skip_reason):
  - **Unread** (`$seen` keyword absent) → `skip_reason="unread"`
  - **Flagged** (`$flagged` keyword present) → `skip_reason="flagged"`
  - **Too new** (`receivedAt` within `min_age_minutes`) → `skip_reason="too_new"`

**Precedence:** Eligibility gates (unread, flagged, too_new) always run and
override the confidence gate's `skip_reason` when they apply. These represent
user-intent signals (e.g., the user flagged an email to keep it visible) and
should be clearly reflected in the audit log regardless of classification
confidence. For example, a flagged email with below-threshold LLM confidence
will show `skip_reason="flagged"`, not `"below_threshold"`.

This means the audit log shows what mailsort *would* do with every email,
giving full visibility into classification quality even for emails that aren't
eligible to move yet.

---

## Thread Context Resolution

```python
def resolve_thread_context(email_features: EmailFeatures) -> Optional[Classification]:
    """If another email in this thread was already sorted, inherit that folder.

    Lookup order:
      1. audit_log — covers all emails mailsort has ever processed or observed
      2. JMAP fallback — checks live mailboxIds of thread siblings for emails
         sorted before mailsort existed (e.g., during bootstrap period)
    """
    if not email_features.thread_id:
        return None

    # 1. Check audit_log for any prior sort of a sibling in this thread
    row = db.execute("""
        SELECT target_folder, COUNT(*) as n
        FROM audit_log
        WHERE thread_id = ? AND moved = 1 AND email_id != ?
        GROUP BY target_folder
        ORDER BY n DESC, created_at DESC
        LIMIT 1
    """, (email_features.thread_id, email_features.email_id)).fetchone()

    if row:
        return Classification(
            folder_path=row["target_folder"],
            confidence=0.95,
            source="thread",
            reasoning=f"Thread sibling already sorted here ({row['n']} prior message(s))",
        )

    # 2. JMAP fallback — fetch sibling email IDs from the thread, check their
    #    current mailboxIds to see if any are already filed outside the inbox
    thread_email_ids = jmap_client.get_thread_email_ids(email_features.thread_id)
    siblings = [eid for eid in thread_email_ids if eid != email_features.email_id]

    if siblings:
        sibling_emails = jmap_client.get_emails(siblings[:10], ["id", "mailboxIds"])
        for sibling in sibling_emails:
            non_inbox = [mid for mid in sibling["mailboxIds"] if mid != inbox_id]
            if non_inbox:
                folder_path = mailbox_id_to_path.get(non_inbox[0])
                if folder_path:
                    return Classification(
                        folder_path=folder_path,
                        confidence=0.90,
                        source="thread",
                        reasoning="Thread sibling found in non-inbox folder via JMAP",
                    )

    return None
```

**Edge cases:**
- **Thread spans multiple folders:** The audit_log query groups by `target_folder`
  and picks the most common (then most recent). If a thread genuinely splits
  across folders (rare), the email falls through to the rule engine.
- **Reply to a newsletter:** The newsletter's `list_id` rule fires in step 3
  before this reaches the LLM. Thread context is a secondary path for cases
  where the sender identity changes mid-thread.
- **JMAP `Thread/get`:** Fastmail supports `Thread/get` to retrieve all email
  IDs for a thread ID. Add this as a helper on the JMAP client.
- **Batching the JMAP fallback:** The fallback path (step 2 in
  `resolve_thread_context`) issues a `Thread/get` + `Email/get` round-trip per
  email. In a batch of 100 emails this could mean 100+ extra JMAP calls.
  To avoid this, collect all unresolved `thread_id`s after the audit_log check,
  batch them into a single `Thread/get` call, then batch the resulting sibling
  email IDs into a single `Email/get` for `mailboxIds`. This reduces the JMAP
  fallback to at most 2 extra calls per scan regardless of batch size.
- **Mislabeled thread correction:** If a thread-inherited move turns out to be
  wrong (you manually move the email back or to a different folder), the
  correction is detected by `detect_corrections` and logged with
  `classification_source='correction'` (with the original `rule_id` if
  applicable). If the thread move was skipped (not executed), the user's sort
  is detected by `detect_manual_sorts` as `classification_source='manual'`.
  Either way, the next call to `resolve_thread_context`
  for that thread will now find multiple target_folders in audit_log; if the
  manual destination outvotes the original, it wins. If they're tied, the thread
  falls through to the rule engine rather than inheriting an ambiguous result.

---

## Rule Engine

### Rule Types

Rules are stored in SQLite and matched in specificity order:

| Priority | Type             | Condition Example                    | Notes                          |
|----------|------------------|--------------------------------------|--------------------------------|
| 1        | `list_id`        | `list-id contains "github.com"`      | Most stable for newsletters and mailing lists |
| 2        | `exact_sender`   | `from == "noreply@chase.com"`        | High specificity for transactional senders |
| 3        | `sender_domain`  | `domain == "chase.com"`              | Useful only when domain history is coherent |
| 4        | `subject_regex`  | `subject matches "Order #\d+"`       | Lowest trust; manual or review-gated |

### Rule Matching Logic

```python
def classify_by_rules(email_features: EmailFeatures) -> Optional[Classification]:
    """Try rules in specificity order. Return the highest-confidence applicable rule."""

    # 1. List-Id match — most stable identifier for newsletters/mailing lists
    if email_features.list_id:
        rule = db.find_rule("list_id", email_features.list_id)
        if rule and rule.confidence >= config.classification.thresholds.rule_move:
            return Classification(folder_path=rule.target_folder_path,
                                  confidence=rule.confidence,
                                  source="rule",
                                  rule_id=rule.id)

    # 2. Exact sender match
    rule = db.find_rule("exact_sender", email_features.from_address)
    if rule and rule.confidence >= config.classification.thresholds.rule_move:
        return Classification(folder_path=rule.target_folder_path,
                              confidence=rule.confidence,
                              source="rule",
                              rule_id=rule.id)

    # 3. Sender domain match
    rule = db.find_rule("sender_domain", email_features.from_domain)
    if rule and rule.confidence >= config.classification.thresholds.rule_move:
        return Classification(folder_path=rule.target_folder_path,
                              confidence=rule.confidence,
                              source="rule",
                              rule_id=rule.id)

    # 4. Subject regex (check all active regex rules)
    for rule in db.find_rules_by_type("subject_regex"):
        if re.search(rule.condition_value, email_features.subject):
            return Classification(folder_path=rule.target_folder_path,
                                  confidence=rule.confidence,
                                  source="rule",
                                  rule_id=rule.id)

    return None  # No rule matched → fall through to LLM
```

**Dry-run behaviour:** During `mailsort dry-run`, rule matching still runs
normally (the classification result is logged to `audit_log`) but
`hit_count` is **not** updated. This prevents dry runs from inflating hit
statistics. The `RuleEngine` accepts a `record_hits` flag (default `True`)
that the orchestrator sets to `False` for dry runs. Note: `last_relevant_at`
is maintained by `compute_rule_confidence()` from audit_log data, not by
the rule engine directly.

**Computed confidence:** Rule confidence is no longer static — it is
recomputed each cycle by `compute_rule_confidence()` in the learning step
from live coherence, staleness, and correction data. The rule engine reads
the stored `confidence` value at classification time as before. See
[learning.md](learning.md#computed-confidence-model) for details.

---

## LLM Classifier

### Privacy & Data Minimization

LLM classification is opt-in at the content level and follows data minimization
principles. Only the minimum metadata needed for classification is sent to
Anthropic.

Default policy:
- Do not send full bodies
- Send `preview` only when `llm_use_preview` is enabled
- Allow per-sender, per-domain, and per-folder opt-outs
- Optionally disable LLM classification for known contacts entirely
- Redact obvious sensitive tokens (SSNs, credit card numbers) before
  transmission when `llm_redact_patterns` is configured

```python
def should_call_llm(features: EmailFeatures, contacts: dict[str, Contact]) -> tuple[bool, Optional[str]]:
    """Check privacy gates before invoking the LLM."""
    if features.from_address in config.classification.llm_skip_senders:
        return False, "llm_skip_sender"
    if features.from_domain in config.classification.llm_skip_domains:
        return False, "llm_skip_domain"
    if features.from_address in contacts and not config.classification.llm_allow_known_contacts:
        return False, "llm_skip_known_contact"
    return True, None
```

Emails that fail the privacy gate are left in the inbox (logged as `skipped`
with the corresponding `skip_reason`). They can still be classified by rules
or thread context — only the LLM call is suppressed.

### Prompt Design

```python
CLASSIFICATION_PROMPT = """You are an email classifier. Given an email's metadata,
classify it into exactly one of the following folders. Respond with JSON only.

## Folder Hierarchy

{folder_descriptions}

## Email to Classify

From: {from_line}
Subject: {subject}
List-Id: {list_id}
Date: {received_at}
Preview: {preview}

## Response Format

Respond with ONLY a JSON object, no markdown, no explanation:
{{
  "folder": "INBOX/Affairs/Banks",
  "confidence": 0.92,
  "reasoning": "Chase bank transaction alert"
}}

Rules:
- "folder" must be an exact path from the list above
- "confidence" is 0.0 to 1.0 (1.0 = certain)
- If you're unsure, set confidence below 0.7
- If the email doesn't fit any folder well, use "INBOX" with low confidence
"""
```

### API Call

```python
import anthropic

client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

def classify_by_llm(email_features: EmailFeatures,
                    folder_descriptions: str) -> Classification:
    response = client.messages.create(
        model=config.classification.llm_model,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": CLASSIFICATION_PROMPT.format(
                folder_descriptions=folder_descriptions,
                from_address=email_features.from_address,
                subject=email_features.subject,
                list_id=email_features.list_id or "(none)",
                received_at=email_features.received_at,
                preview=email_features.preview[:500],
            )
        }]
    )

    try:
        result = json.loads(response.content[0].text)
    except (json.JSONDecodeError, KeyError):
        logger.warning(f"LLM returned unparseable response: {response.content[0].text[:200]}")
        return Classification(
            folder_path="INBOX",
            confidence=0.0,
            source="llm",
            reasoning="parse_error",
        )

    folder = result.get("folder", "INBOX")
    if folder not in valid_folder_paths:
        logger.warning(f"LLM returned unknown folder '{folder}', falling back to INBOX")
        folder = "INBOX"
        result["confidence"] = 0.0

    return Classification(
        folder_path=folder,
        confidence=float(result.get("confidence", 0.0)),
        source="llm",
        reasoning=result.get("reasoning", ""),
    )
```

### Cost Estimate

- Haiku input: ~$0.80/MTok, output: ~$4/MTok
- Average email classification: ~300 input tokens, ~50 output tokens
- Per email: ~$0.00044
- 50 emails/day: ~$0.022/day → ~$0.66/month
- Negligible cost even at high volume

### Subject Regex Rules — Manual or LLM-Suggested Only

Subject patterns (`Order #\d+`, `Your .* statement is ready`) are useful but
**must not be auto-generated** from literal subjects. A literal subject is either
too specific (matches nothing new) or, if naively generalized, too broad.

Instead:
- **Manual rules** in `config.yaml` for known patterns you write yourself.
- **LLM-suggested rules for review:** when the LLM classifies the same type of
  email repeatedly with consistent reasoning, it can propose a subject regex that
  is written to `pending_rules` with `source="llm_suggested"` and `active=0`.
  These surface in the summary report for human confirmation before activating.

```python
def maybe_suggest_subject_rule(email_features: EmailFeatures, result: dict):
    """After LLM classification, check if a subject pattern recurs enough to suggest a rule."""
    reasoning = result.get("reasoning", "")
    folder = result.get("folder")
    if not reasoning or not folder:
        return

    # Count how many times the LLM gave near-identical reasoning for this folder
    count = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE target_folder = ?
          AND llm_reasoning = ?
          AND classification_source = 'llm'
          AND decision_status IN ('moved', 'manual')
    """, (folder, reasoning)).fetchone()[0]

    if count >= config.classification.llm_suggest_rule_after_n:
        existing_suggestion = db.execute("""
            SELECT id FROM rules
            WHERE active = 0 AND source = 'llm_suggested'
              AND target_folder_path = ?
              AND condition_value = ?
        """, (folder, reasoning)).fetchone()

        if not existing_suggestion:
            db.create_rule(
                rule_type="subject_regex",
                condition_value=reasoning,  # Human reviews and converts to regex before activating
                target_folder_path=folder,
                confidence=0.85,
                source="llm_suggested",
                active=False,
            )
            logger.info(f"Suggested rule for review: '{reasoning}' → {folder}")
```

### Content Signals — Headers Only, Not Body

Body text is too variable to produce reliable rules. The right content-derived
signals are already in `EmailFeatures` as structured headers:

- `list_id` — handled above; strongest content signal
- `list_unsubscribe` — presence is a reliable "bulk/marketing" indicator;
  useful in a `combined` rule with sender domain when `list_id` is absent
  (e.g., `domain=substack.com + has_unsubscribe=True → Social/Newsletters`)

Body preview (`preview`) is used only by the LLM classifier, not rule matching.

---

## LLM Classification Cache

When thread context and rules miss, the orchestrator checks for a cached LLM
result before calling the API. This avoids redundant LLM calls for emails that
remain in the inbox across multiple runs.

### Mechanism

1. At the start of each run, the orchestrator computes a **classification
   version** — a SHA-256 hash of the folder descriptions string and the LLM
   model name. This is stored in `learner_state` alongside a
   `classification_version_changed_at` timestamp.
2. For each email that needs LLM classification, the orchestrator queries
   `audit_log` for a prior row with `classification_source = 'llm'` and
   `created_at ≥ classification_version_changed_at`.
3. If found, the cached `Classification` is reused and `MoveDecision.cached`
   is set to `True`. The `audit_log.cached` column is set to 1.
4. If not found, a fresh LLM API call is made.

### Cache Invalidation

The cache is invalidated when:
- **Folder descriptions change** (e.g., a folder is added/removed/renamed, or
  a description is updated) → new hash → new version timestamp.
- **LLM model changes** (e.g., `llm_model` config updated) → new hash.

Both cause `classification_version_changed_at` to update, so all prior LLM
audit rows become stale (their `created_at < classification_version_changed_at`).

### Pipeline Split

The classification pipeline exposes two methods used by the orchestrator:

- `classify_without_llm(features)` — thread + rules only (cheap, no API calls)
- `classify_llm(features)` — LLM only (called only on cache miss)

The original `classify()` method remains as a convenience wrapper that calls
both in sequence.

---

## Confidence Gate & Mover

### Confidence Thresholds

```yaml
# config.yaml → classification.thresholds
classification:
  thresholds:
    rule_move: 0.85            # Rules need 85%+ to auto-move
    llm_move: 0.80             # LLM needs 80%+ to auto-move
    llm_move_known_contact: 0.93  # Stricter threshold when sender is a known contact
                                  # — prefer inbox over wrong folder for personal email
```

Thread-inherited classifications (`source="thread"`) bypass the LLM threshold
entirely — if a thread sibling was already sorted somewhere, that's treated as
a reliable signal regardless of sender type.

### Confidence Gate Logic

```python
def should_move(decision: MoveDecision, contacts: dict[str, Contact]) -> tuple[bool, Optional[str]]:
    """Apply the appropriate confidence threshold based on sender and classification source.

    Returns (should_move, skip_reason).
    """
    clf = decision.classification

    # Thread context bypasses LLM thresholds — sibling sort is reliable regardless of sender
    if clf.source == "thread":
        return True, None

    # Rule-based classifications use the rule threshold
    if clf.source == "rule":
        if clf.confidence >= config.classification.thresholds.rule_move:
            return True, None
        return False, "below_threshold"

    # LLM classifications: stricter threshold for known contacts
    if clf.source == "llm":
        is_known_contact = decision.features.from_address in contacts
        threshold = (
            config.classification.thresholds.llm_move_known_contact
            if is_known_contact
            else config.classification.thresholds.llm_move
        )
        if clf.confidence >= threshold:
            return True, None
        reason = "below_threshold_known_contact" if is_known_contact else "below_threshold"
        return False, reason

    return False, "unknown_source"
```

The `skip_reason` distinction (`below_threshold` vs `below_threshold_known_contact`)
is logged to `audit_log` so the summary report can show you which inbox items
were held back specifically because they were from known contacts — useful for
tuning the threshold over time.

### Batch Move Operation

```python
def batch_move(jmap_client, moves: list[MoveDecision]) -> dict[str, str]:
    """Move multiple emails in one JMAP call, preserving mailbox memberships.

    Returns a dict mapping email_id to outcome: 'moved' or 'move_failed'.
    """
    updates = {}
    for decision in moves:
        new_mailbox_ids = build_updated_mailbox_ids(
            current_mailbox_ids=decision.features.current_mailbox_ids,
            inbox_id=inbox_id,
            target_folder_id=decision.classification.folder_id,
        )
        updates[decision.email_id] = {"mailboxIds": new_mailbox_ids}

    result = jmap_client.call([
        ["Email/set", {
            "accountId": account_id,
            "update": updates
        }, "batch_move"]
    ])

    # Reconcile per-email outcomes
    set_response = result["methodResponses"][0][1]
    outcomes = {}
    for email_id in updates:
        if email_id in set_response.get("updated", {}):
            outcomes[email_id] = "moved"
        elif email_id in set_response.get("notUpdated", {}):
            error = set_response["notUpdated"][email_id]
            logger.error(f"Failed to move {email_id}: {error}")
            outcomes[email_id] = "move_failed"
        else:
            outcomes[email_id] = "move_failed"
    return outcomes
```
