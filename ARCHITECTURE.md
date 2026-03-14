# Mailsort — Fastmail Inbox Classifier & Sorter

## Overview

Mailsort is a self-hosted email classification service that periodically scans read, unflagged messages in a Fastmail inbox and moves them to the appropriate subfolder. It uses a tiered classification approach: deterministic rules handle known patterns, and an LLM classifier handles ambiguous cases. All decisions are logged for review, undo, and continuous learning.

**Deployment target:** Docker container on an Intel NUC (home server)

**Primary API:** Fastmail JMAP (RFC 8621) via `https://api.fastmail.com/jmap/api/`

**LLM provider:** Anthropic API (Claude Haiku for classification)

---

## Table of Contents

1. [Folder Structure](#1-folder-structure)
2. [System Architecture](#2-system-architecture)
3. [JMAP Integration](#3-jmap-integration)
4. [Classification Pipeline](#4-classification-pipeline)
5. [Rule Engine](#5-rule-engine)
6. [LLM Classifier](#6-llm-classifier)
7. [Confidence Gate & Mover](#7-confidence-gate--mover)
8. [Audit Log & Learning](#8-audit-log--learning)
9. [Bootstrapping](#9-bootstrapping)
10. [Configuration](#10-configuration)
11. [Project Structure](#11-project-structure)
12. [Data Models](#12-data-models)
13. [Operational Concerns](#13-operational-concerns)
14. [Docker & Deployment](#14-docker--deployment)
15. [Development Phases](#15-development-phases)
16. [Open Questions & Future Work](#16-open-questions--future-work)

---

## 1. Folder Structure

The user's Fastmail account is organized hierarchically:

```
INBOX                          ← All new mail lands here
INBOX/
  Affairs/
    Alerts/                    ← Automated alerts, monitoring notifications
    Banks/                     ← Bank statements, transaction alerts
    Insurance/                 ← Policy docs, claims, renewals
    Medical/                   ← Appointment confirmations, lab results
    Government/                ← Tax, DMV, government correspondence
    Legal/                     ← Contracts, legal notices
    Utilities/                 ← Electric, gas, water, internet bills
  Shopping/
    Orders/                    ← Order confirmations, shipping updates
    Receipts/                  ← Purchase receipts
    Deals/                     ← Promotional offers, coupons
  Social/
    Newsletters/               ← Subscribed newsletters, digests
    Notifications/             ← Social media notifications
    Community/                 ← Forums, mailing lists, groups
  Work/
    Projects/                  ← Project-specific correspondence
    Admin/                     ← HR, payroll, benefits
    Recruiting/                ← Job-related, recruiting outreach
  Tech/
    GitHub/                    ← GitHub notifications
    Services/                  ← SaaS notifications, API alerts
    Dev/                       ← Developer newsletters, changelogs
  Travel/                      ← Flights, hotels, itineraries
  ...                          ← Additional user-defined folders
```

> **NOTE:** This is an example hierarchy. The actual folder structure is discovered
> dynamically at runtime via `Mailbox/get`. The config file maps folder paths to
> descriptions so the LLM understands each folder's purpose.

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Fastmail (JMAP)                     │
│  Mailbox/get · Email/query · Email/get · Email/set   │
└──────────────────────┬───────────────────────────────┘
                       │ HTTPS (Bearer token)
                       ▼
┌──────────────────────────────────────────────────────┐
│              Scheduler (APScheduler)                  │
│         Runs classification job every N minutes       │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│                  JMAP Client                          │
│  1. Fetch inbox mailbox ID                           │
│  2. Query read, unflagged emails older than N hours   │
│  3. Fetch metadata: from, subject, list-id, body     │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│              Feature Extractor                        │
│  Sender address, domain, List-Id, List-Unsubscribe,  │
│  subject patterns, body preview (~200 words)          │
└──────────┬───────────────────────────┬───────────────┘
           ▼                           ▼
┌─────────────────────┐  ┌─────────────────────────────┐
│    Rule Engine       │  │     LLM Classifier          │
│  SQLite lookup:      │  │  Claude Haiku via API       │
│  sender → folder     │  │  Only called if rules       │
│  domain → folder     │  │  return no match or low     │
│  list-id → folder    │  │  confidence                 │
│  subject regex →     │  │                             │
│    folder            │  │  Returns: folder + conf     │
└─────────┬───────────┘  └──────────────┬──────────────┘
          │                             │
          ▼                             ▼
┌──────────────────────────────────────────────────────┐
│               Confidence Gate                         │
│  If confidence >= threshold → proceed to move         │
│  If confidence < threshold → skip, leave in inbox     │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│              Email/set Mover                          │
│  Update mailboxIds: remove inbox, add target folder   │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│           Audit Log (SQLite)                          │
│  email_id, sender, subject, target_folder,            │
│  confidence, source (rule|llm), timestamp             │
│                                                       │
│  Feedback loop: manual sorts captured on next scan    │
│  → auto-generate rules from repeated patterns         │
└──────────────────────────────────────────────────────┘
```

---

## 3. JMAP Integration

### Authentication

Fastmail uses Bearer token auth. Generate an API token at:
Settings → Privacy & Security → Manage API tokens

The token needs the scopes:
- `urn:ietf:params:jmap:core`
- `urn:ietf:params:jmap:mail`
- `urn:ietf:params:jmap:contacts` (for contact lookup — requires contacts synced to Fastmail)

### Session Discovery

```
GET https://api.fastmail.com/jmap/session
Authorization: Bearer {token}
```

Response provides:
- `accounts` → your account ID (e.g., `u12345678`)
- `apiUrl` → `https://api.fastmail.com/jmap/api/` (POST all method calls here)
- `primaryAccounts` → which account to use for mail

### Key JMAP Methods

All calls are POST to `apiUrl` with JSON body:

```json
{
  "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
  "methodCalls": [...]
}
```

#### Mailbox/get — Discover folder tree

```json
["Mailbox/get", {
  "accountId": "u12345678",
  "properties": ["id", "name", "parentId", "role", "totalEmails", "unreadEmails"]
}, "m1"]
```

Returns all mailboxes. Build a tree from `parentId` relationships.
The inbox has `role: "inbox"`. Cache the mailbox ID → path mapping.

#### Email/query — Find eligible inbox messages

```json
["Email/query", {
  "accountId": "u12345678",
  "filter": {
    "inMailbox": "INBOX_ID",
    "hasKeyword": "$seen",
    "notKeyword": "$flagged"
  },
  "sort": [{"property": "receivedAt", "isAscending": false}],
  "limit": 100
}, "q1"]
```

**Important:** JMAP's `Email/query` filter supports `hasKeyword` and `notKeyword`
for filtering by `$seen` (read) and `$flagged` status. This handles the
"only process read, unflagged emails" requirement at the query level.

The age filter (only emails older than N hours) is applied using the `before`
filter condition with a computed UTC datetime. Fastmail's JMAP implementation
supports `before` in `Email/query`, so this can be done server-side:

```python
cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.scheduler.min_age_hours)).isoformat() + "Z"
filter["before"] = cutoff
```

This eliminates the need for client-side filtering on `receivedAt`.

#### Email/get — Fetch metadata

```json
["Email/get", {
  "accountId": "u12345678",
  "#ids": {"resultOf": "q1", "name": "Email/query", "path": "/ids"},
  "properties": [
    "id", "threadId", "mailboxIds", "from", "to", "subject",
    "receivedAt", "keywords", "preview",
    "header:list-id:asText",
    "header:list-unsubscribe:asText"
  ]
}, "g1"]
```

Use result references to chain query → get in a single HTTP request.
The `preview` property gives a short text snippet without fetching the full body.
For richer classification, fetch `bodyValues` with `fetchTextBodyValues: true`.

#### ContactCard/get — Fetch contacts for sender enrichment

```json
["ContactCard/get", {
  "accountId": "u12345678",
  "properties": ["uid", "name", "emails"]
}, "c1"]
```

Fastmail implements JMAP Contacts (`urn:ietf:params:jmap:contacts`). Each
`ContactCard` has a `name` map and an `emails` map keyed by arbitrary IDs.
Query all contacts once at startup (and refresh daily), then build an in-memory
lookup from email address → `{name, groups}`.

**Graceful degradation:** If the `urn:ietf:params:jmap:contacts` scope is not
present in the session capabilities (e.g., token was created without it, or
contacts have not been synced to Fastmail), the system logs a warning and
continues without contact enrichment. The `llm_move_known_contact` stricter
threshold will not apply — all LLM classifications use `llm_move` instead.
The contacts table remains empty until the scope becomes available.

```python
def refresh_contacts_cache(jmap_client):
    """Load contacts from Fastmail. No-op if contacts scope is unavailable."""
    if "urn:ietf:params:jmap:contacts" not in jmap_client.session_capabilities:
        logger.warning(
            "Contacts scope not available — contact enrichment disabled. "
            "Grant the contacts scope and sync Apple Contacts via CardDAV to enable."
        )
        return
    # ... fetch and cache ContactCard objects
```

**Prerequisite:** Sync Apple Contacts to Fastmail via CardDAV. In macOS:
System Settings → Internet Accounts → Add Account → CardDAV → Fastmail.
After that, Apple Contacts syncs bidirectionally with Fastmail and mailsort
reads them automatically — no separate credentials or client needed.

**Contact-based prompt enrichment:**
When the sender's email address matches a contact, the LLM prompt is enriched
with the contact's name so it can classify by relationship context rather than
treating the sender as an unknown address.

```
From: husband@gmail.com [known contact: "John Smith"]
Subject: Can you look at this?
```

vs.

```
From: husband@gmail.com
Subject: Can you look at this?
```

The `known_contacts` config key accepts optional manual overrides for contacts
you want to annotate beyond what's in your address book (e.g., adding a
`relationship` hint like "spouse" or "parent" that CardDAV doesn't carry).

#### Email/set — Move to folder

`Email/set` replaces the full `mailboxIds` set for a message. To avoid stripping
unrelated mailbox memberships, mailsort must construct a new mailbox set from the
message's current `mailboxIds`: remove the inbox mailbox ID, add the target
folder ID, and preserve any other existing mailbox memberships.

```python
def build_updated_mailbox_ids(current_mailbox_ids: dict[str, bool],
                              inbox_id: str,
                              target_folder_id: str) -> dict[str, bool]:
    """Preserve existing mailbox memberships while removing inbox and adding target."""
    updated = dict(current_mailbox_ids)
    updated.pop(inbox_id, None)
    updated[target_folder_id] = True
    return updated
```

```json
["Email/set", {
  "accountId": "u12345678",
  "update": {
    "MSG_ID": {
      "mailboxIds": {"TARGET_FOLDER_ID": true, "OTHER_EXISTING_ID": true}
    }
  }
}, "s1"]
```

**Batch moves:** You can update multiple emails in a single `Email/set` call
by including multiple entries in the `update` object. Do this to minimize
API round trips.

### Rate Limiting & Best Practices

- Fastmail does not publish explicit rate limits for JMAP, but be respectful.
  Polling every 10-15 minutes is reasonable.
- Use result references to batch query + get into a single request.
- Batch Email/set updates (move multiple emails in one call).
- Cache the mailbox tree; only refresh it periodically (e.g., once per hour)
  or when a move fails with an unknown mailbox ID.

---

## 4. Classification Pipeline

For each eligible email, the pipeline runs in order:

```
1. Assign a unique run_id for this scan
2. Extract features from eligible emails (including threadId)
3. Resolve classification: thread context → rule engine → LLM
4. Build a MoveDecision for each email
5. Persist decisions to audit_log with decision_status = planned or skipped
6. Execute a batched Email/set for all planned decisions (mark attempted)
7. Reconcile per-email move results from JMAP into audit_log (moved or move_failed)
8. Update run summary metrics and mark the run completed
```

Classification resolution (step 3) tries sources in order:

```
3a. Check thread context
    - If another email in this thread was previously sorted → inherit that folder
    - Otherwise → continue
3b. Check the rule engine
    - If a rule matches with confidence >= threshold → use it
    - If a rule matches with confidence < threshold → fall through to LLM
    - If no rule matches → fall through to LLM
3c. Call the LLM classifier (if privacy gate allows)
    - If LLM returns confidence >= threshold → use it
    - If LLM returns confidence < threshold → SKIP (leave in inbox)
```

Thread context (step 2) handles replies and forwarded messages where the sender
changes but the conversation topic doesn't — e.g., your husband replying to a
vendor email, or a family member reply-chain. It runs before rules because a
known thread context is more reliable than any pattern match.

### Eligibility Criteria (applied before classification)

An email is eligible for classification if ALL of the following are true:
- It is in the inbox (has inbox mailbox ID in `mailboxIds`)
- It has the `$seen` keyword (has been read)
- It does NOT have the `$flagged` keyword
- Its `receivedAt` is older than `min_age_hours` (default: 4 hours)
- It has not already been processed in this run (deduplicate by email ID)
- It is not in the `skip_senders` list (manual exclude list in config)

### Thread Context Resolution

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
        WHERE thread_id = ? AND decision_status IN ('moved', 'manual') AND email_id != ?
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
  wrong (you manually move the email back or to a different folder), that manual
  move is detected by `detect_manual_sorts` and logged with
  `classification_source='manual'`. The next call to `resolve_thread_context`
  for that thread will now find multiple target_folders in audit_log; if the
  manual destination outvotes the original, it wins. If they're tied, the thread
  falls through to the rule engine rather than inheriting an ambiguous result.

---

## 5. Rule Engine

### Rule Types

Rules are stored in SQLite and matched in specificity order:

| Priority | Type             | Condition Example                    | Notes                          |
|----------|------------------|--------------------------------------|--------------------------------|
| 1        | `list_id`        | `list-id contains "github.com"`      | Most stable for newsletters and mailing lists |
| 2        | `exact_sender`   | `from == "noreply@chase.com"`        | High specificity for transactional senders |
| 3        | `sender_domain`  | `domain == "chase.com"`              | Useful only when domain history is coherent |
| 4        | `subject_regex`  | `subject matches "Order #\d+"`       | Lowest trust; manual or review-gated |

### Rule Schema

```sql
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,          -- exact_sender | sender_domain | list_id | subject_regex
    condition_value TEXT NOT NULL,    -- The match value (email, domain, regex, etc.)
    target_folder_path TEXT NOT NULL, -- e.g., "INBOX/Affairs/Banks"
    target_folder_id TEXT,           -- JMAP mailbox ID (resolved at runtime)
    confidence REAL DEFAULT 1.0,     -- 0.0 to 1.0
    hit_count INTEGER DEFAULT 0,     -- Times this rule has matched
    last_hit_at TEXT,                -- ISO timestamp
    source TEXT NOT NULL DEFAULT 'auto'
        CHECK(source IN ('auto','manual','bootstrap','llm_suggested')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active BOOLEAN DEFAULT 1
);

CREATE INDEX idx_rules_type_value ON rules(rule_type, condition_value);
CREATE INDEX idx_rules_active ON rules(active);
CREATE UNIQUE INDEX idx_rules_unique_active
    ON rules(rule_type, condition_value) WHERE active = 1;
```

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

---

## 6. LLM Classifier

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

### When to Call

The LLM is called only when the rule engine returns `None` (no match) or a
match with confidence below the threshold, **and** the privacy gate allows it.

### Prompt Design

```python
CLASSIFICATION_PROMPT = """You are an email classifier. Given an email's metadata,
classify it into exactly one of the following folders. Respond with JSON only.

## Folder Hierarchy

{folder_descriptions}

## Email to Classify

From: {from_address}
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

---

## 7. Confidence Gate & Mover

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

### Move Operation

```python
def move_email(jmap_client, email_id: str, current_mailbox_ids: dict[str, bool],
               target_folder_id: str) -> bool:
    """Move an email by updating its mailboxIds via Email/set.

    Preserves existing mailbox memberships while removing inbox and adding target.
    """
    new_mailbox_ids = build_updated_mailbox_ids(current_mailbox_ids, inbox_id, target_folder_id)
    result = jmap_client.call([
        ["Email/set", {
            "accountId": account_id,
            "update": {
                email_id: {
                    "mailboxIds": new_mailbox_ids
                }
            }
        }, "move"]
    ])
    # Check for errors in the response
    set_response = result["methodResponses"][0][1]
    if email_id in set_response.get("updated", {}):
        return True
    if email_id in set_response.get("notUpdated", {}):
        error = set_response["notUpdated"][email_id]
        logger.error(f"Failed to move {email_id}: {error}")
        return False
    return False
```

### Batch Moves

Accumulate all move decisions, then execute in a single `Email/set`:

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

---

## 8. Audit Log & Learning

### Audit Schema

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','abandoned')),
    trigger TEXT NOT NULL DEFAULT 'scheduler',
    eligible_count INTEGER,
    skipped_count INTEGER,
    attempted_count INTEGER,
    moved_count INTEGER,
    failed_count INTEGER,
    error_summary TEXT
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    email_id TEXT NOT NULL,
    thread_id TEXT,                      -- JMAP threadId for thread context lookups
    from_address TEXT,
    from_domain TEXT,
    subject TEXT,
    list_id TEXT,
    source_folder TEXT DEFAULT 'INBOX',
    target_folder TEXT NOT NULL,
    confidence REAL NOT NULL,
    classification_source TEXT NOT NULL,  -- rule | llm | thread | manual
    rule_id INTEGER,                     -- FK to rules.id if rule-based
    llm_reasoning TEXT,                  -- LLM explanation if LLM-based
    decision_status TEXT NOT NULL CHECK(decision_status IN (
        'planned', 'skipped', 'attempted', 'moved', 'move_failed', 'abandoned', 'manual'
    )),
    skip_reason TEXT,                    -- below_threshold | below_threshold_known_contact | skip_list | flagged
    move_error TEXT,                     -- JMAP error detail if move_failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY (rule_id) REFERENCES rules(id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE UNIQUE INDEX idx_audit_run_email ON audit_log(run_id, email_id);
CREATE INDEX idx_audit_email_created ON audit_log(email_id, created_at DESC);
CREATE INDEX idx_audit_thread ON audit_log(thread_id);  -- thread context lookups
CREATE INDEX idx_audit_domain ON audit_log(from_domain);
CREATE INDEX idx_audit_created ON audit_log(created_at);
```

Each scan is assigned a unique `run_id` at startup. All audit rows created during
that scan reference the same `run_id`. This provides per-run reporting, simplifies
idempotency guarantees, and makes recovery of interrupted runs explicit.

JMAP move execution is treated as a set of per-email outcomes, not an atomic
transaction. Mailsort persists planned decisions before calling `Email/set`,
then reconciles each message into `moved` or `move_failed` based on the JMAP
response. If the process crashes mid-run, the incomplete run remains visible
in `runs` and can be reconciled on the next startup.

### Contacts Cache Schema

```sql
CREATE TABLE contacts (
    email_address TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    relationship TEXT,          -- Optional override from config (e.g. "spouse", "parent")
    fastmail_uid TEXT,          -- ContactCard uid — used to detect updates
    refreshed_at TEXT NOT NULL
);

-- No index needed on email_address — it's the PRIMARY KEY
```

Populated at startup from Fastmail JMAP (`ContactCard/get`) and refreshed
daily. Config `known_contact_overrides` entries are merged in after the JMAP
fetch, with the override's `relationship` field augmenting (not replacing) the
name from the address book.

### Folder Descriptions Schema

```sql
CREATE TABLE folder_descriptions (
    folder_path TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    source TEXT NOT NULL,  -- 'auto' (bootstrap-generated) | 'manual' (config override)
    generated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Descriptions are loaded at startup by merging both sources, with manual
overrides taking precedence:

```python
def load_folder_descriptions(config: Config) -> dict[str, str]:
    """Merge auto-generated and manual-override descriptions."""
    descriptions = {
        row["folder_path"]: row["description"]
        for row in db.execute("SELECT folder_path, description FROM folder_descriptions")
    }
    # Manual overrides from config.yaml win
    descriptions.update(config.folder_description_overrides or {})
    return descriptions
```

### Learning from Manual Sorts

The learner (`audit/learner.py`) runs at the start of each scan, before
classification, so that newly learned rules are available for the current batch.
It detects user sorts through four complementary categories:

| Category | What it catches | How it works |
|----------|----------------|--------------|
| **1. Skipped sorts** | Emails mailsort left in inbox that the user moved | Query `audit_log WHERE moved=0` in last 7 days, check current `mailboxIds` |
| **2. Correction sorts** | Emails mailsort moved that the user relocated | Query `audit_log WHERE moved=1`, compare current mailbox to expected |
| **3. Inbox departures** | Emails the user sorted before mailsort processed them | Diff previous inbox snapshot against current inbox IDs; fetch departed emails to see where they went |
| **4. Daily folder scan** | Emails sorted outside any scan window entirely | Sample recent emails from each folder, find ones absent from `audit_log` |

Categories 1–2 handle emails mailsort already knows about. Categories 3–4
close the gap for emails mailsort never saw — the most common case being mail
you read and sort within a few minutes of arrival, before the next poll.

**Inbox snapshot (Category 3):** On each scan, the orchestrator queries ALL
inbox email IDs (no read/flag/age filter) and stores them in the
`inbox_snapshot` table. On the next scan, it diffs `previous - current -
already_processed` to find departures. Snapshots older than 2 days are cleaned
up automatically.

**Daily folder scan (Category 4):** Once per 24 hours, the learner samples
recent emails from each non-inbox folder and checks if any are absent from
`audit_log`. This catches emails that arrived and were sorted between two
consecutive scans (i.e., never appeared in any snapshot). The last-scan
timestamp is tracked in the `learner_state` table.

All four categories log detected sorts as `classification_source='manual'` in
`audit_log` and feed them into auto-rule generation:

```python
def detect_manual_sorts(jmap_client, previously_skipped: list[str], run_id: str):
    """Detect user corrections in two categories:

    1. Skipped emails the user moved out of the inbox (we left them, they sorted them).
    2. Mailsort-moved emails the user relocated to a different folder (corrections).

    Both are logged as manual classifications and fed into auto-rule generation.
    """
    # --- Category 1: skipped emails the user moved out of inbox ---
    if previously_skipped:
        emails = jmap_client.get_emails(previously_skipped, ["id", "mailboxIds"])
        for email in emails:
            if inbox_id not in email["mailboxIds"]:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path[new_folder_id]
                _record_manual_sort(run_id, email["id"], new_folder_path)

    # --- Category 2: mailsort-moved emails the user relocated ---
    recent_moves = db.execute("""
        SELECT email_id, target_folder FROM audit_log
        WHERE decision_status = 'moved'
          AND created_at >= datetime('now', '-7 days')
    """).fetchall()

    if recent_moves:
        move_ids = [row["email_id"] for row in recent_moves]
        expected_folders = {row["email_id"]: row["target_folder"] for row in recent_moves}
        emails = jmap_client.get_emails(move_ids[:100], ["id", "mailboxIds"])

        for email in emails:
            current_folder_ids = set(email["mailboxIds"].keys())
            expected_path = expected_folders[email["id"]]
            expected_id = folder_path_to_id.get(expected_path)

            # If the email is no longer in the folder mailsort put it in,
            # the user moved it — record the correction
            if expected_id and expected_id not in current_folder_ids:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path.get(new_folder_id)
                if new_folder_path and new_folder_path != "INBOX":
                    _record_manual_sort(run_id, email["id"], new_folder_path)


def _record_manual_sort(run_id: str, email_id: str, folder_path: str):
    """Log a manual classification and consider auto-generating a rule."""
    audit_log.insert(
        run_id=run_id,
        email_id=email_id,
        target_folder=folder_path,
        confidence=1.0,
        classification_source="manual",
        decision_status="manual",
    )
    maybe_create_rule(email_id, folder_path)
```

### Auto-Rule Generation

Auto-rule generation runs in two situations:
1. After detecting a manual sort (user moved an email we left in inbox)
2. After an LLM-classified move, once the same pattern has been seen N times

Rules are created in priority order — the most reliable signal wins. Subject regex
and body content are **not** auto-generated (see below).

```python
def maybe_create_rule(email_features, target_folder: str):
    """Create the most appropriate rule if there is sufficient evidence.

    Priority:
      1. list_id       — stable identifier for newsletters/mailing lists
      2. sender_domain — when domain history is coherent (most moves → same folder)
      3. exact_sender  — fallback for one-off transactional senders

    Each rule type has its own evidence threshold and minimum confidence
    requirement. Broader rules require more evidence and a coherence check
    before creation to avoid misrouting unrelated emails from the same source.
    """
    thresholds = config.classification.auto_rule_thresholds  # list_id: 2, exact_sender: 3, sender_domain: 5
    coherence_min = config.classification.auto_rule_domain_coherence  # 0.80

    # 1. List-Id rule — highest specificity, lowest threshold
    if email_features.list_id:
        count = db.execute("""
            SELECT COUNT(*) FROM audit_log
            WHERE list_id = ? AND target_folder = ? AND decision_status IN ('moved', 'manual')
        """, (email_features.list_id, target_folder)).fetchone()[0]

        if count >= thresholds["list_id"]:
            existing = db.find_rule("list_id", email_features.list_id)
            if not existing:
                db.create_rule(
                    rule_type="list_id",
                    condition_value=email_features.list_id,
                    target_folder_path=target_folder,
                    confidence=0.95,
                    source="auto",
                )
                logger.info(f"Auto-created list_id rule: {email_features.list_id} → {target_folder}")
                return

    # 2. Sender domain rule — requires volume AND coherence.
    #    Coherence: what fraction of this domain's total moves go to target_folder?
    #    A domain like amazon.com that splits across Orders/Receipts/Deals should
    #    never get a domain rule — it would misroute whichever folders are in the minority.
    domain_total = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_domain = ? AND decision_status IN ('moved', 'manual')
    """, (email_features.from_domain,)).fetchone()[0]

    domain_to_target = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_domain = ? AND target_folder = ? AND decision_status IN ('moved', 'manual')
    """, (email_features.from_domain, target_folder)).fetchone()[0]

    domain_distinct_senders = db.execute("""
        SELECT COUNT(DISTINCT from_address) FROM audit_log
        WHERE from_domain = ? AND target_folder = ? AND decision_status IN ('moved', 'manual')
    """, (email_features.from_domain, target_folder)).fetchone()[0]

    domain_coherence = domain_to_target / domain_total if domain_total > 0 else 0.0

    if (domain_to_target >= thresholds["sender_domain"]
            and domain_distinct_senders >= 3
            and domain_coherence >= coherence_min):
        existing = db.find_rule("sender_domain", email_features.from_domain)
        if not existing:
            confidence = min(0.90, 0.75 + (domain_to_target * 0.02))
            db.create_rule(
                rule_type="sender_domain",
                condition_value=email_features.from_domain,
                target_folder_path=target_folder,
                confidence=confidence,
                source="auto",
            )
            logger.info(
                f"Auto-created domain rule: {email_features.from_domain} → {target_folder} "
                f"(coherence={domain_coherence:.0%}, n={domain_to_target})"
            )
            return

    # 3. Exact sender fallback — narrow scope, moderate threshold
    count = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_address = ? AND target_folder = ? AND decision_status IN ('moved', 'manual')
    """, (email_features.from_address, target_folder)).fetchone()[0]

    if count >= thresholds["exact_sender"]:
        existing = db.find_rule("exact_sender", email_features.from_address)
        if not existing:
            db.create_rule(
                rule_type="exact_sender",
                condition_value=email_features.from_address,
                target_folder_path=target_folder,
                confidence=min(0.95, 0.80 + (count * 0.03)),
                source="auto",
            )
            logger.info(f"Auto-created sender rule: {email_features.from_address} → {target_folder}")
```

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

The write path runs inside `classify_by_llm` after a successful classification.
If the LLM's reasoning mentions a pattern that has now recurred N times with
consistent reasoning across different emails, a pending rule is written:

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

```sql
-- pending_rules reuses the rules schema with active=0 and source="llm_suggested"
-- The summary report surfaces these for review:
SELECT rule_type, condition_value, target_folder_path, confidence, created_at
FROM rules
WHERE active = 0 AND source = 'llm_suggested'
ORDER BY created_at DESC;
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

## 9. Bootstrapping

Bootstrap does not use a separate, looser rule-creation strategy. Historical
emails are treated as evidence inputs to the same candidate evaluation logic
used during live learning. This ensures broad rules such as `sender_domain`
are only created when their cross-folder history is sufficiently coherent.

```python
def bootstrap_rules(jmap_client, max_per_folder: int = 50):
    """Scan existing folders and feed historical evidence into the normal rule evaluator."""
    mailboxes = jmap_client.get_all_mailboxes()
    report = {"created": [], "rejected": [], "multi_folder_domains": []}

    # Phase 1: Collect evidence from all folders into audit_log
    for mailbox in mailboxes:
        if mailbox["role"] == "inbox" or is_system_folder(mailbox):
            continue

        folder_path = build_folder_path(mailbox, mailboxes)

        email_ids = jmap_client.query_emails(
            in_mailbox=mailbox["id"],
            limit=max_per_folder,
            sort=[{"property": "receivedAt", "isAscending": False}]
        )

        emails = jmap_client.get_emails(
            email_ids, ["id", "threadId", "from", "subject", "header:list-id:asText"]
        )

        # Record each email as bootstrap evidence in audit_log
        for email in emails:
            features = extract_features(email)
            audit_log.insert(
                run_id=bootstrap_run_id,
                email_id=email["id"],
                thread_id=email.get("threadId"),
                from_address=features.from_address,
                from_domain=features.from_domain,
                list_id=features.list_id,
                target_folder=folder_path,
                confidence=1.0,
                classification_source="manual",
                decision_status="moved",
            )

        # Generate folder description if one doesn't already exist
        # (manual config.yaml description takes precedence — checked before calling this)
        if not db.get_folder_description(folder_path):
            description = generate_folder_description(folder_path, emails)
            if description:
                db.upsert_folder_description(folder_path, description, source="auto")
                logger.info(f"Generated description for {folder_path}: {description}")

    # Phase 2: Evaluate candidate rules using the same thresholds and coherence
    # checks as maybe_create_rule — now that audit_log has cross-folder history
    evidence = collect_bootstrap_candidates()
    for candidate in evidence:
        result = maybe_create_rule_from_evidence(
            candidate=candidate,
            source="bootstrap",
        )
        if result.created:
            report["created"].append(result)
        elif result.reason == "low_coherence":
            report["rejected"].append(result)
            if result.rule_type == "sender_domain":
                report["multi_folder_domains"].append(result)

    log_bootstrap_report(report)

    # Load contacts after folder/rule seeding so first classification run
    # has full contact enrichment available
    refresh_contacts_cache(jmap_client)


FOLDER_DESCRIPTION_PROMPT = """You are helping configure an email classifier.
Given a folder name and a sample of email subjects and senders stored in it,
write a single concise sentence (under 20 words) describing what kind of emails
belong in this folder.

Folder path: {folder_path}

Sample emails (sender — subject):
{samples}

Respond with ONLY the description sentence, no quotes, no punctuation at the end."""


def generate_folder_description(folder_path: str, emails: list) -> Optional[str]:
    """Ask the LLM to describe a folder based on sample emails inside it."""
    if not emails:
        return None

    samples = "\n".join(
        f"  {e['from'][0]['email'] if e.get('from') else '?'} — {e.get('subject', '(no subject)')}"
        for e in emails[:15]  # Cap at 15 samples; enough signal, minimal cost
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": FOLDER_DESCRIPTION_PROMPT.format(
                folder_path=folder_path,
                samples=samples,
            )
        }]
    )
    return response.content[0].text.strip()
```

---

## 10. Configuration

### config.yaml

```yaml
# Fastmail settings
fastmail:
  api_url: "https://api.fastmail.com/jmap/api/"
  session_url: "https://api.fastmail.com/jmap/session"
  # Token is in FASTMAIL_API_TOKEN env var

# Scheduling
scheduler:
  interval_minutes: 15
  min_age_hours: 4          # Don't move emails younger than this
  max_batch_size: 100       # Max emails to process per run

# Classification
classification:
  thresholds:
    rule_move: 0.85
    llm_move: 0.80
    llm_move_known_contact: 0.93   # Stricter for personal/family senders
  # Per-type thresholds for auto-rule creation.
  # Higher thresholds for broader rules; coherence required for domain rules.
  auto_rule_thresholds:
    list_id: 2              # list_id uniquely identifies one sender source
    exact_sender: 3         # Narrow scope, 3 confirmations is sufficient
    sender_domain: 5        # Broad scope — more evidence required
  auto_rule_domain_coherence: 0.80  # 80%+ of domain moves must go to same folder
  llm_model: "claude-haiku-4-5-20251001"
  llm_max_preview_chars: 500
  llm_use_preview: true             # Send email preview text to the LLM
  llm_allow_known_contacts: false   # If false, skip LLM for known contacts
  llm_redact_patterns:              # Regex patterns to redact before sending to LLM
    - "\\b\\d{3}-\\d{2}-\\d{4}\\b"             # SSN
    - "\\b(?:\\d[ -]*){13,16}\\b"              # Credit card numbers
  llm_suggest_rule_after_n: 5       # Suggest a subject regex rule after N consistent LLM classifications
  llm_skip_senders:                 # Never send these senders' emails to the LLM
    # - "spouse@example.com"
  llm_skip_domains:                 # Never send emails from these domains to the LLM
    # - "bank.example.com"

# Folder descriptions — OPTIONAL manual overrides only.
# Bootstrap auto-generates descriptions for all folders by scanning their contents.
# Only add entries here to correct a description the LLM got wrong.
folder_description_overrides:
  # "INBOX/Affairs/Legal": "Contracts, NDAs, and legal correspondence from attorneys"

# Manual rules (override auto-generated rules)
manual_rules:
  - type: exact_sender
    value: "important@example.com"
    folder: "INBOX/Affairs/Legal"
    confidence: 1.0

# Skip list — never auto-move these senders
skip_senders:
  - "spouse@example.com"
  - "boss@company.com"

# Contacts are loaded automatically from Fastmail (synced from Apple Contacts).
# Use this section only to add relationship hints that CardDAV doesn't carry,
# or to annotate addresses not in your address book.
known_contact_overrides:
  # "husband@gmail.com":
  #   relationship: "spouse"   # Extra hint for the LLM beyond just the name

# Logging
logging:
  level: INFO
  file: "/app/data/mailsort.log"
  max_size_mb: 10
  backup_count: 3
```

---

## 11. Project Structure

```
~/Workspace/mailsort/
├── ARCHITECTURE.md              ← This document
├── README.md
├── pyproject.toml               ← Python project config (uv/poetry)
├── Dockerfile
├── docker-compose.yml
├── config.yaml                  ← User configuration
│
├── src/
│   └── mailsort/
│       ├── __init__.py
│       ├── main.py              ← Entry point, scheduler setup
│       ├── config.py            ← Config loading & validation
│       │
│       ├── jmap/
│       │   ├── __init__.py
│       │   ├── client.py        ← JMAP HTTP client (session, auth, method calls)
│       │   ├── models.py        ← Pydantic models for JMAP objects (Email, Mailbox)
│       │   ├── mailbox_tree.py  ← Mailbox tree builder & path resolver
│       │   └── contacts.py      ← ContactCard/get fetcher & contacts cache refresh
│       │
│       ├── classifier/
│       │   ├── __init__.py
│       │   ├── pipeline.py      ← Main classification orchestrator
│       │   ├── features.py      ← Feature extraction from email objects
│       │   ├── rules.py         ← Rule engine (SQLite-backed)
│       │   └── llm.py           ← LLM classifier (Anthropic API)
│       │
│       ├── mover/
│       │   ├── __init__.py
│       │   └── mover.py         ← Confidence gate + batch Email/set moves
│       │
│       ├── audit/
│       │   ├── __init__.py
│       │   ├── logger.py        ← Audit log writer
│       │   ├── learner.py       ← Manual sort detection + auto-rule generation
│       │   └── models.py        ← SQLAlchemy/Pydantic models for audit tables
│       │
│       └── db/
│           ├── __init__.py
│           ├── database.py      ← SQLite connection management
│           └── migrations.py    ← Schema creation & migrations
│
├── tests/
│   ├── conftest.py
│   ├── test_jmap_client.py
│   ├── test_classifier.py
│   ├── test_rules.py
│   ├── test_llm.py
│   ├── test_mover.py
│   ├── test_learner.py
│   └── fixtures/
│       ├── sample_emails.json   ← Test email data
│       └── sample_mailboxes.json
│
├── scripts/
│   ├── bootstrap.py             ← One-time bootstrap script
│   ├── dry_run.py               ← Run classification without moving anything
│   └── export_rules.py          ← Export current rules to YAML for review
│
└── data/                        ← Docker volume mount point
    ├── mailsort.db              ← SQLite database (rules + audit log)
    └── mailsort.log             ← Application log
```

---

## 12. Data Models

### Pydantic Models

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class EmailFeatures(BaseModel):
    """Extracted features from an email for classification."""
    email_id: str
    thread_id: str                       # JMAP threadId — used for thread context
    from_address: str
    from_domain: str
    to_addresses: list[str]
    subject: str
    list_id: Optional[str] = None
    list_unsubscribe: Optional[str] = None
    received_at: datetime
    preview: str
    keywords: list[str]            # JMAP keywords ($seen, $flagged, etc.)
    current_mailbox_ids: dict[str, bool]

class Classification(BaseModel):
    """Result of classifying an email."""
    folder_path: str
    folder_id: Optional[str] = None  # Resolved JMAP mailbox ID
    confidence: float
    source: str                      # "thread" | "rule" | "llm" | "manual"
    rule_id: Optional[int] = None
    reasoning: Optional[str] = None

class MoveDecision(BaseModel):
    """Final decision on whether and where to move an email."""
    email_id: str
    features: EmailFeatures
    classification: Classification
    should_move: bool
    decision_status: str               # "planned" | "skipped"
    skip_reason: Optional[str] = None  # "below_threshold" | "below_threshold_known_contact" | "flagged" | "skip_list" | "llm_skip_*"
```

---

## 13. Operational Concerns

### Concurrent Run Protection

APScheduler may fire a new run while a previous one is still in progress (e.g.,
if a run takes longer than the scheduler interval). Two simultaneous runs would
process the same inbox emails and write conflicting audit records.

Two layers of protection:

1. **APScheduler `max_instances=1`** — prevents the scheduler from launching a
   second instance of the job while one is already running.
2. **`runs` table lock** — before starting work, insert a row into `runs` with
   `status='running'`. If a row with `status='running'` already exists, skip.

```python
def acquire_run_lock(run_id: str) -> bool:
    """Attempt to acquire the run lock by inserting a new run. Returns False if already running."""
    existing = db.execute(
        "SELECT run_id FROM runs WHERE status = 'running'"
    ).fetchone()
    if existing:
        logger.warning(f"Run {existing['run_id']} still in progress, skipping")
        return False
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger) VALUES (?, datetime('now'), 'running', 'scheduler')",
        (run_id,)
    )
    return True

def finish_run(run_id: str, status: str = "completed", error_summary: str = None):
    db.execute(
        "UPDATE runs SET status=?, finished_at=datetime('now'), error_summary=? WHERE run_id=?",
        (status, error_summary, run_id)
    )
```

On startup, reconcile stale runs — any row with `status='running'` from a
previous process is marked `abandoned`:

```python
def reconcile_stale_runs():
    """Mark interrupted runs as abandoned on startup."""
    db.execute("UPDATE runs SET status='abandoned' WHERE status='running'")
```

### Error Handling Design

Every I/O boundary (JMAP API, Anthropic API, SQLite) is wrapped so that
failures are logged, partial progress is preserved, and one bad email never
kills the entire batch. Four principles govern error handling:

#### 1. Guaranteed audit logging

Classification decisions are always written to `audit_log`, even when the
JMAP move call crashes. The orchestrator wraps the move step in `try/except`
and writes audit rows in a `finally` block:

```python
outcomes: dict[str, bool] = {}
try:
    if planned and not dry_run:
        outcomes = jmap.move_emails(moves)
except Exception:
    logger.exception("JMAP move_emails failed — decisions will still be logged")
finally:
    # Always log, even on move crash. When outcomes is empty,
    # planned decisions are recorded as moved=False — accurate
    # since nothing was confirmed moved.
    audit.log_decisions(run_id, decisions, outcomes)
```

The outer `run_classification_pass` also catches any exception from the full
run body and calls `finish_run(status="failed")`, so the `runs` table always
reflects what happened.

#### 2. Per-email isolation

A classification failure for one email must not prevent the remaining emails
from being processed. The orchestrator wraps each `pipeline.classify()` call:

```python
for features in eligible:
    try:
        classification, skip_reason = pipeline.classify(features)
    except Exception:
        logger.exception("Classification failed for %s, skipping", features.email_id)
        classification, skip_reason = None, "classification_error"
```

Within the pipeline itself, the thread context DB lookup and JMAP fallback
are each individually wrapped so failures degrade to the next classification
tier (rules, then LLM) rather than crashing.

#### 3. Defensive audit writes

`AuditWriter.finish_run()` is called from exception handlers and must never
mask the original error. It catches and logs its own DB failures internally:

```python
def finish_run(self, run_id, *, status, ...):
    try:
        self._db.execute("UPDATE runs SET status=? ...", ...)
        self._db.commit()
    except Exception:
        logger.exception("Failed to write finish_run for %s", run_id)
```

`AuditWriter.log_decisions()` uses per-row isolation — if one insert fails
(e.g., constraint violation), remaining rows are still written:

```python
for d in decisions:
    try:
        self.log_decision(run_id, d, moved=moved)
        logged += 1
    except Exception:
        logger.exception("Failed to log audit row for %s", d.email_id)
```

#### 4. Graceful degradation across tiers

Each I/O tier degrades independently without blocking the others:

| Tier | Failure mode | Behavior |
|------|-------------|----------|
| **JMAP query/fetch** | Network error, HTTP 5xx | Run marked `failed`, exception propagated to caller |
| **Thread context DB** | SQLite error | Logged, returns `None` — falls through to rule engine |
| **Thread context JMAP** | `Thread/get` or `Email/get` failure | Logged, returns `None` — falls through to rule engine |
| **Rule engine** | Bad regex in `subject_regex` rule | Logged per-rule, continues to next rule |
| **LLM (Anthropic)** | API timeout, rate limit, parse error | Returns `Classification(confidence=0.0, reasoning="api_error")` — email is skipped, not crashed |
| **JMAP move** | `Email/set` network error | Logged, `outcomes` stays empty, all decisions logged as `moved=False` |
| **Audit DB** | Insert/commit failure | Logged per-row, remaining rows still attempted |

Anthropic API failures are handled at the LLM classifier level — `classify()`
catches all exceptions and returns a safe default. Since rules don't need the
LLM, rule-matched emails can still be moved in the same run. Only emails that
require LLM classification are skipped.

### Database Migration Versioning

Migrations are tracked via a `schema_version` table:

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

On startup, `migrations.py` checks the current version and applies any pending
migrations in order. Migrations are never skipped or re-applied.

```python
MIGRATIONS = [
    (1, "create_rules_table"),
    (2, "create_runs_table"),
    (3, "create_audit_log_table"),
    (4, "create_contacts_table"),
    (5, "create_folder_descriptions_table"),
]

def run_migrations():
    current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for version, name in MIGRATIONS:
        if version > current:
            apply_migration(version, name)
            db.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (version,))
            logger.info(f"Applied migration {version}: {name}")
```

### Deleted Folder Handling

If a Fastmail folder is renamed or deleted, rules pointing to it will have a
stale `target_folder_id` and a `target_folder_path` that no longer exists in the
mailbox tree.

On each startup (and whenever `Mailbox/get` is refreshed), reconcile rules
against the live mailbox tree:

```python
def reconcile_rules(live_folder_paths: set[str]):
    """Deactivate rules whose target folder no longer exists."""
    stale = db.execute("""
        SELECT id, target_folder_path FROM rules
        WHERE active = 1
    """).fetchall()

    for rule in stale:
        if rule["target_folder_path"] not in live_folder_paths:
            db.execute(
                "UPDATE rules SET active=0, updated_at=datetime('now') WHERE id=?",
                (rule["id"],)
            )
            logger.warning(
                f"Deactivated rule {rule['id']}: target folder "
                f"'{rule['target_folder_path']}' no longer exists"
            )
```

Deactivated rules are retained (not deleted) so they appear in the summary
report for review. If you renamed a folder, you can update the rule's
`target_folder_path` and re-activate it rather than losing the rule's history.

---

## 14. Docker & Deployment

### Dockerfile

Because this project uses a `src/` layout, the package source must be copied
into the image before `pip install .` is executed. Otherwise the package may
not be importable at runtime.

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy package source and metadata — src/ must be present before pip install
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

CMD ["python", "-m", "mailsort.main"]
```

### docker-compose.yml

```yaml
services:
  mailsort:
    build: .
    container_name: mailsort
    restart: unless-stopped
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    environment:
      - FASTMAIL_API_TOKEN=${FASTMAIL_API_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - TZ=America/Chicago
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### .env file (not committed)

```
FASTMAIL_API_TOKEN=fmu1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## 15. Development Phases

### Phase 1: Foundation ✅
- [x] Project scaffolding (`pyproject.toml`, `src/` layout, hatchling build)
- [x] Config loading with pydantic (`config.py`, `config.yaml`, env var secrets)
- [x] JMAP client: session discovery, auth, method calls (`jmap/client.py`)
- [x] Mailbox tree discovery and path resolution (`jmap/mailbox_tree.py`)
- [x] Email querying with eligibility filters (`query_inbox_emails`)
- [x] SQLite database setup with versioned migrations (`db/database.py`, `db/migrations.py`)

### Phase 2: Classification ✅
- [x] Feature extractor (`classifier/features.py`)
- [x] Rule engine: CRUD, specificity-ordered matching (`classifier/rules.py`)
- [x] LLM classifier with structured prompt + privacy gate (`classifier/llm.py`)
- [x] Classification pipeline: thread context → rules → LLM (`classifier/pipeline.py`)
- [x] Confidence gate logic (`mover/mover.py`)

### Phase 3: Moving & Logging ✅
- [x] Email mover: batch `Email/set` via `move_emails` (`jmap/client.py`)
- [x] Audit log writer with run lifecycle (`audit/writer.py`)
- [x] Run orchestrator: full classify → move → log pass (`orchestrator.py`)
- [x] Dry-run mode: `mailsort dry-run` CLI command (`main.py`)
- [x] Error handling: per-email isolation, guaranteed audit logging, defensive
      DB writes (see §13 Error Handling Design)
- [x] Undo via keyword tagging: `$mailsort-moved` keyword added to moved emails
      via JMAP patch in `Email/set` (`jmap/client.py`)

### Phase 4: Learning ✅
- [x] Bootstrap: scan existing folders → seed rules + folder descriptions
      (`bootstrap.py`, `mailsort bootstrap` CLI command)
- [x] Manual sort detection — four categories (`audit/learner.py`):
  - [x] Category 1: skipped emails the user moved out of inbox
  - [x] Category 2: mailsort-moved emails the user relocated
  - [x] Category 3: inbox departures via snapshot diff (emails sorted before
        mailsort processed them) — `inbox_snapshot` table, migration 7
  - [x] Category 4: daily folder scan for emails with no audit_log record
        (`learner_state` table tracks last-scan time)
- [x] Auto-rule generation from repeated patterns: list_id → sender_domain
      (with coherence check) → exact_sender (`audit/learner.py`)
- [x] Rule confidence adjustment: decay by 0.10 for rules not hit in 90+ days,
      floor at 0.50 (`audit/learner.py`)

### Phase 5: Scheduling & Deployment ✅
- [x] APScheduler integration: `BlockingScheduler` with `max_instances=1`,
      runs on configurable interval (`scheduler.py`, `mailsort start` CLI)
- [x] Dockerfile and docker-compose (`Dockerfile`, `docker-compose.yml`)
- [x] Graceful shutdown: SIGTERM/SIGINT handlers stop the scheduler cleanly
      (`scheduler.py`)
- [x] Health check endpoint: `GET /health` on configurable port (default 8025),
      returns JSON with last run status, Docker HEALTHCHECK wired up
      (`health.py`, `scheduler.health_check_port` config)

### Phase 6: Observability & Tuning
- [x] Structured logging: JSON or text format via `logging_config.format` config
      toggle (`main.py` `_JSONFormatter`)
- [x] Export-rules: `mailsort export-rules [--inactive]` dumps rules to YAML
      (`main.py`)
- [x] Confidence threshold analysis: `mailsort analyze [--days N]` shows
      classification sources, move outcomes, LLM confidence distribution,
      skipped-then-manually-sorted stats, and recommendations (`main.py`)
- [ ] Optional: simple web UI for reviewing audit log and rules

---

## 16. Open Questions & Future Work

### Open Questions
1. **Mailbox ID stability:** Do Fastmail mailbox IDs change if folders are
   renamed? Need to handle re-resolution gracefully.
2. **Multi-label emails:** Some emails could fit multiple folders. Current
   design picks the single best match. Is that sufficient?

### Future Enhancements
- **Web dashboard:** Flask/FastAPI app for viewing audit log, managing rules,
  adjusting thresholds, and triggering manual scans.
- **JMAP push notifications:** Instead of polling, use JMAP's EventSource
  push mechanism to react to new EmailDelivery state changes in near-realtime.
- **Rule decay:** Automatically lower confidence on rules that haven't matched
  in 90+ days. Deactivate rules that haven't matched in 180 days.
- **Feedback loop tightening:** If the user moves an email *back* from a sorted
  folder to the inbox, treat that as negative signal and reduce the rule's
  confidence.
- **Multiple account support:** Extend to handle multiple Fastmail accounts
  or even non-Fastmail JMAP servers.