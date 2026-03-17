# System Test Plan: End-to-End Testing with Fastmail Test Account

## Goal

Validate the complete mailsort pipeline against a real Fastmail account with
controlled test data. This covers bootstrap → dry-run → live move → learning →
feedback loop — everything the integration tests verify with mocks, but now
against real JMAP.

---

## 1. Test Account Setup

### Prerequisites

- A dedicated Fastmail test account (separate from your real account)
- An API token for the test account with full read/write access
- A separate `config.test.yaml` pointing to a test database (`data/test.db`)
- The `ANTHROPIC_API_KEY` env var set (for LLM classification tests)

### Folder Structure

Create these folders in the test Fastmail account:

```
INBOX/
  Affairs/
    Banks/
    Stores/
  People/
    Children/
```

These are simple, representative of your real account's structure, and cover:
- **Banks**: transactional emails (statements, alerts, fraud notices)
- **Stores**: order confirmations, shipping, returns
- **Children**: school notices, activity updates, pediatrician

### Config File

```yaml
# config.test.yaml
fastmail:
  api_url: "https://api.fastmail.com/jmap/api/"
  session_url: "https://api.fastmail.com/jmap/session"

scheduler:
  interval_minutes: 15
  min_age_minutes: 1    # 1 minute — allows testing too_new gate quickly
  max_batch_size: 200

classification:
  thresholds:
    rule_move: 0.85
    llm_move: 0.80
    llm_move_known_contact: 0.93
  correction_penalty: 0.15

folder_description_overrides:
  "Affairs/Banks": "Bank statements, fraud alerts, and financial notifications"
  "Affairs/Stores": "Order confirmations, shipping updates, and return receipts"
  "People/Children": "School notices, activity signups, and pediatrician communications"

known_contact_overrides:
  "testcontact@example.com":
    relationship: "friend"

exclude_folder_patterns: []
skip_senders: []
```

Note: `min_age_minutes: 1` — short enough that most test emails pass the age
gate, but long enough that dynamically-generated "just arrived" emails trigger
the `too_new` skip reason.

---

## 2. Test Data Design

### 2.1 Rule Generation Condition Matrix

The auto-rule engine evaluates three rule types in priority order. Each has
different thresholds. The test data must exercise every cell in this matrix:

**Thresholds (defaults from `ClassificationConfig`):**
- `exact_sender`: ≥3 emails to target folder, coherence ≥80%
- `sender_domain`: ≥5 emails to target folder, ≥3 distinct senders to same folder, coherence ≥80%
- `list_id`: ≥2 emails to target folder, coherence ≥80%

**Coherence** = (emails to target folder) / (total emails from that condition value across all folders)

#### Test Conditions: Rule Creation

| # | Condition | Data Shape | Expected Rule | What It Tests |
|---|-----------|-----------|---------------|---------------|
| R1 | Exact sender, high coherence | 4× `noreply@chase.com` → Banks | exact_sender created | Happy path: single sender, one folder |
| R2 | Exact sender, low coherence | 2× `alice@family.com` → Banks, 2× → Children | NO rule (coherence 50%) | Coherence rejection for exact sender |
| R3 | Exact sender, below threshold | 2× `rare@oneoff.com` → Banks | NO rule (count < 3) | Count threshold rejection |
| R4 | Domain, high coherence, 3+ senders | 3× different `@bigbank.com` senders → Banks, 5+ total | sender_domain created | Domain rule with coherence + distinct sender check |
| R5 | Domain, low coherence (different addresses → different folders) | `orders@megastore.com` → Stores, `alerts@megastore.com` → Banks | NO domain rule | Same domain, different senders to different folders — the "Amazon problem" |
| R6 | Domain, high coherence but <3 senders | 5× `single@concentrated.com` → Banks | exact_sender only (not domain) | Domain rule requires ≥3 distinct senders |
| R7 | List-Id, high coherence | 3× list-id `<news.school.org>` → Children | list_id created | List-Id rule (highest priority) |
| R8 | List-Id, low coherence | 2× list-id `<alerts.mixed.com>` → Banks, 2× → Stores | NO list_id rule | List-Id split across folders |
| R9 | Domain rule blocks exact sender | Domain rule created for `@bigbank.com` | Individual senders NOT also ruled | Priority: domain wins, no redundant exact_sender |

#### Test Conditions: Contact Interaction

| # | Condition | Data Shape | Expected Behavior |
|---|-----------|-----------|------------------|
| C1 | Known contact, concentrated in one folder | 4× `testfriend@gmail.com` → Children | exact_sender rule created; at classification time, rule matches and known-contact threshold doesn't apply (rule > LLM) |
| C2 | Known contact, split across folders | 2× `testcontact@example.com` → Banks, 2× → Children, 1× → Stores | NO rule (low coherence); falls to LLM with `llm_move_known_contact` threshold (0.93) |
| C3 | Unknown sender (domain), split across folders | 3× `info@splitdomain.com` → Banks, 3× `returns@splitdomain.com` → Stores | NO domain rule (50% coherence); individual exact_sender rules form per address if ≥3 |
| C4 | Unknown sender (exact), split across folders | 2× `alice@family.com` → Banks, 2× `alice@family.com` → Children | NO exact_sender rule (50% coherence); falls to LLM with standard threshold (0.80) |
| C5 | Unknown sender, no prior evidence | 1× `brand-new@unknown.com` | No rule match; LLM classification with standard `llm_move` threshold (0.80) |

#### Test Conditions: Classification Source at Inbox Time

| # | Scenario | Expected Source | Expected Outcome |
|---|----------|----------------|-----------------|
| S1 | Sender has exact_sender rule, eligible | rule | moved |
| S2 | Sender's domain has domain rule, eligible | rule | moved |
| S3 | Email has list_id with list_id rule, eligible | rule | moved |
| S4 | Thread sibling was previously sorted | thread | moved |
| S5 | No rule, LLM classifies above threshold | llm | moved |
| S6 | No rule, LLM below threshold | llm | below_threshold |
| S7 | Known contact, LLM above known-contact threshold | llm | moved |
| S8 | Known contact, LLM between normal and known-contact threshold | llm | below_threshold_known_contact |
| S9 | No rule, no LLM configured | — | no_classification / llm_unavailable |

#### Test Conditions: Eligibility Gates

| # | Scenario | Keywords | receivedAt | Expected Outcome |
|---|----------|----------|------------|-----------------|
| E1 | Read, unflagged, old | `$seen` | 5h ago | moved (or whatever classification says) |
| E2 | Unread | (none) | 5h ago | unread |
| E3 | Read + flagged | `$seen`, `$flagged` | 5h ago | flagged |
| E4 | Read, unflagged, too new | `$seen` | 30min ago | too_new |
| E5 | Unread + flagged + new | `$flagged` | 30min ago | unread (checked first) |

### 2.2 Static Fixtures (JSON)

Pre-defined emails loaded from `tests/fixtures/folder_emails.json`. These seed
the folders with enough evidence for bootstrap to create rules.

**Group A: Clean exact_sender rules (high coherence)**

| Sender | Folder | Count | Expected Rule |
|--------|--------|-------|---------------|
| `noreply@chase.com` | Banks | 5 | exact_sender (R1) |
| `alerts@bankofamerica.com` | Banks | 4 | exact_sender (R1) |
| `orders@amazon.com` | Stores | 5 | exact_sender (R1) |
| `noreply@target.com` | Stores | 4 | exact_sender (R1) |
| `admin@lincolnelementary.org` | Children | 5 | exact_sender (R1) |
| `activities@ymca.org` | Children | 4 | exact_sender (R1) |

**Group B: Domain coherence — high (R4)**

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `statements@bigbank.com` | Banks | 3 | |
| `alerts@bigbank.com` | Banks | 3 | |
| `fraud@bigbank.com` | Banks | 2 | |
| | | **8 total, 3 distinct senders, 100% coherence** | → domain rule for `bigbank.com` |

**Group C: Domain coherence — low, "Amazon problem" (R5)**

Different addresses at the same domain route to different folders. The domain
has low coherence so no `sender_domain` rule is created, but each individual
address that meets the ≥3 threshold gets its own `exact_sender` rule.

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `orders@megastore.com` | Stores | 4 | → exact_sender rule |
| `alerts@megastore.com` | Banks | 3 | → exact_sender rule |
| `returns@megastore.com` | Stores | 2 | → NO rule (count < 3) |
| | | **9 total across domain, 44% max coherence** | → NO domain rule; per-address exact_sender rules only |

**Group D: Known contact, split across folders — low coherence (C2)**

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `testcontact@example.com` | Banks | 2 | |
| `testcontact@example.com` | Children | 2 | |
| `testcontact@example.com` | Stores | 1 | |
| | | **5 total, 40% max coherence** | → NO rule; LLM with known-contact threshold |

**Group E: Known contact, concentrated — high coherence (C1)**

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `testfriend@gmail.com` | Children | 4 | |
| | | **4 total, 100% coherence** | → exact_sender rule |

**Group F: List-Id rule (R7)**

| List-Id | Folder | Count | Notes |
|---------|--------|-------|-------|
| `<newsletter.school.org>` | Children | 3 | → list_id rule |

**Group G: List-Id low coherence (R8)**

| List-Id | Sender varies | Folder | Count | Notes |
|---------|--------------|--------|-------|-------|
| `<alerts.mixed.com>` | `a@mixed.com` | Banks | 2 | |
| `<alerts.mixed.com>` | `b@mixed.com` | Stores | 2 | |
| | | | **4 total, 50% coherence** | → NO list_id rule |

**Group H: Unknown sender (exact), split across folders — low coherence (C4)**

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `alice@family.com` | Banks | 2 | |
| `alice@family.com` | Children | 2 | |
| | | **4 total, 50% coherence** | → NO rule; LLM with standard threshold |

**Group I: Below threshold (R3)**

| Sender | Folder | Count | Notes |
|--------|--------|-------|-------|
| `rare@oneoff.com` | Banks | 2 | → NO rule (count < 3) |

**Total: 66 fixture emails across 3 folders**

Each fixture email includes:
- `from` (name + email)
- `to` (test account email)
- `subject` (realistic, varied per sender)
- `textBody` (short realistic body text)
- `receivedAt` (spread across last 30 days)
- `keywords` (`{"$seen": true}` — marked as read)
- `targetFolder` (which folder to place it in)
- `listId` (optional, for list-id rule tests)

### 2.3 Dynamic Inbox Generator

A Python script (`tests/system/generate_inbox_emails.py`) that creates inbox
emails with dynamic timestamps for testing all classification and eligibility
scenarios at runtime:

| Scenario | From | Keywords | receivedAt | Expected |
|----------|------|----------|------------|----------|
| E1: Rule match, eligible | `noreply@chase.com` | `$seen` | 5h ago | moved → Banks (rule) |
| E2: Rule match, unread | `orders@amazon.com` | (none) | 5h ago | unread |
| E3: Rule match, flagged | `noreply@chase.com` | `$seen`, `$flagged` | 5h ago | flagged |
| E4: Rule match, too new | `alerts@bankofamerica.com` | `$seen` | now | too_new |
| E5: Unread + flagged + new | `noreply@target.com` | `$flagged` | now | unread (checked first) |
| S2: Domain rule match | `support@bigbank.com` | `$seen` | 5h ago | moved → Banks (rule, new address at ruled domain) |
| S3: List-Id rule match | `newsletter@lincolnelementary.org` | `$seen` | 5h ago | moved → Children (list_id rule) |
| S4: Thread match | `rare@oneoff.com` (In-Reply-To verification code) | `$seen` | 5h ago | moved → Banks (thread context only — no rule for this sender) |
| S5: LLM above threshold | `newsletter@newbank.com` | `$seen` | 5h ago | LLM classifies |
| S6: LLM below threshold | `info@ambiguous-service.com` | `$seen` | 5h ago | below_threshold (minimal content) |
| S8: Known contact, ambiguous | `testcontact@example.com` | `$seen` | 5h ago | LLM with known-contact threshold |
| C1: Known contact with rule | `testfriend@gmail.com` | `$seen` | 5h ago | moved → Children (rule wins over LLM) |
| C4: Unknown exact, split | `alice@family.com` | `$seen` | 5h ago | LLM (no rule, 50% coherence) |
| C5: No match at all | `random@unknown.com` | `$seen` | 5h ago | LLM or no_classification |
| R5a: Megastore below threshold | `returns@megastore.com` | `$seen` | 5h ago | LLM (no rule, <3 emails) |
| R5b: Megastore per-address → Stores | `orders@megastore.com` | `$seen` | 5h ago | moved → Stores (exact_sender rule) |
| R5c: Megastore per-address → Banks | `alerts@megastore.com` | `$seen` | 5h ago | moved → Banks (exact_sender rule, same domain different folder) |

The generator uses `datetime.now(timezone.utc)` to produce `receivedAt`
timestamps relative to the current time, ensuring `too_new` scenarios work
regardless of when the test is run.

### 2.4 Dynamic Inbox Generator Code

```python
def generate_inbox_emails() -> list[dict]:
    """Generate test emails for inbox with dynamic receivedAt times."""
    now = datetime.now(timezone.utc)
    return [
        {
            "from": "noreply@chase.com",
            "subject": f"Test: Chase statement {now:%Y%m%d%H%M}",
            "receivedAt": (now - timedelta(hours=5)).isoformat(),
            "keywords": {"$seen": True},
            "expectedOutcome": "moved",
            "expectedFolder": "Affairs/Banks",
        },
        {
            "from": "noreply@chase.com",
            "subject": f"Test: Chase unread {now:%Y%m%d%H%M}",
            "receivedAt": (now - timedelta(hours=5)).isoformat(),
            "keywords": {},  # unread
            "expectedOutcome": "unread",
        },
        {
            "from": "alice@family.com",
            "subject": f"Test: Alice split {now:%Y%m%d%H%M}",
            "receivedAt": (now - timedelta(hours=5)).isoformat(),
            "keywords": {"$seen": True},
            "expectedOutcome": "LLM (no rule, 50% coherence)",
        },
        # ... etc for each scenario
    ]
```

### 2.5 Files

```
tests/system/
  README.md                      # How to run the system tests
  config.test.yaml               # Test account config
  fixtures/
    folder_emails.json           # Static fixture emails for bootstrap seeding
  generate_inbox_emails.py       # Dynamic inbox email generator
  load_fixtures.py               # JMAP loader: creates emails in test folders
  run_system_test.py             # Orchestrates the full test sequence
  verify_results.py              # Queries DB and validates outcomes
```

### 2.6 `load_fixtures.py` — JMAP Email Loader

Uses JMAP `Email/set` with `create` to inject emails directly into the test
account. For each fixture email:

1. Resolve target folder path → mailbox ID via `Mailbox/get`
2. Upload RFC 5322 message blob via JMAP `blob/upload`
3. Create email via `Email/import` with `mailboxIds` set to the target folder
4. Mark as read by setting `keywords: {"$seen": true}`

The loader is idempotent: it checks for existing emails by subject+sender
before creating, so re-running doesn't duplicate.

### 2.7 Run Modes

The system test supports two run modes:

#### Setup-only mode

Loads fixtures and runs bootstrap, then stops. Use this for interactive
development and manual testing against the test account.

```bash
python tests/system/run_system_test.py --config config.test.yaml --setup-only
```

This runs:
1. Connect to test Fastmail account, verify folder structure
2. Load static fixture emails into folders via JMAP
3. Load dynamic inbox emails via JMAP
4. Run `mailsort bootstrap --config config.test.yaml`
5. Print summary of rules created, coverage, and folder descriptions
6. **Stop** — the test database and loaded emails remain for manual work

After setup, you can:
- Start the web UI: `mailsort web --config config.test.yaml --port 8081`
- Run dry-run: `mailsort dry-run --config config.test.yaml`
- Run live: `mailsort run --config config.test.yaml`
- Inspect the DB: `sqlite3 data/test.db`

#### Full test sequence

```bash
python tests/system/run_system_test.py --config config.test.yaml
```

Runs the complete automated test sequence:

```
Phase 1: Setup
  - Connect to test Fastmail account
  - Verify folder structure exists
  - Load fixture emails into folders via JMAP
  - Load dynamic inbox emails via JMAP (includes too_new email created now)

Phase 2: Bootstrap
  - Run `mailsort bootstrap --config config.test.yaml`
  - Verify rules created (exact_sender for chase, amazon, etc.)
  - Verify folder descriptions generated
  - Verify coverage percentage

Phase 3: Dry Run
  - Run `mailsort dry-run --config config.test.yaml`
  - Verify audit_log entries for all inbox emails
  - Verify correct classification sources (rule vs LLM)
  - Verify eligibility outcomes (moved, unread, flagged, too_new)
  - Verify the just-created email has skip_reason=too_new
  - Verify no emails actually moved

Phase 4: Age Gate Test
  - Wait for min_age_minutes (1 minute) to elapse
  - Run `mailsort run --config config.test.yaml`
  - Verify the previously-too-new email is now moved
  - Verify unread/flagged emails are still in inbox

Phase 5: Live Run Verification
  - Verify eligible emails actually moved to correct folders via JMAP
  - Verify ineligible emails still in inbox
  - Verify audit_log moved=1 for moved emails

Phase 6: User Correction Simulation
  - Move one email back from Banks to Stores via JMAP (simulate user correction)
  - Run `mailsort run --config config.test.yaml` again
  - Verify correction detected in learning step
  - Verify rule confidence penalized
  - Verify manual audit_log row created

Phase 7: Cleanup
  - Delete all test emails from the account
  - Remove test database
```

### 2.8 `verify_results.py` — Result Validator

Queries the test database and validates expectations:

```python
def verify_bootstrap(db):
    """Verify bootstrap created expected rules and descriptions."""
    rules = db.execute("SELECT * FROM rules WHERE active = 1").fetchall()
    assert len(rules) >= 6  # at least 6 sender rules
    # ... specific checks

def verify_dry_run(db, run_id, expected_outcomes):
    """Verify each email got the expected classification and outcome."""
    for email in expected_outcomes:
        row = db.execute(
            "SELECT * FROM audit_log WHERE email_id = ? AND run_id = ?",
            (email["id"], run_id),
        ).fetchone()
        assert row["skip_reason"] == email["expectedOutcome"]
        # ...
```

---

## 3. What Will Be Created

### Files

```
tests/system/
  README.md                      # How to run the system tests
  config.test.yaml               # Test account config
  fixtures/
    folder_emails.json           # Static fixture emails for bootstrap seeding
  generate_inbox_emails.py       # Dynamic inbox email generator
  load_fixtures.py               # JMAP loader: creates emails in test folders
  run_system_test.py             # Orchestrates the full test sequence
  verify_results.py              # Queries DB and validates outcomes
```

### `load_fixtures.py` — JMAP Email Loader

Uses JMAP `Email/set` with `create` to inject emails directly into the test
account. For each fixture email:

1. Resolve target folder path → mailbox ID via `Mailbox/get`
2. Upload RFC 5322 message blob via JMAP `blob/upload`
3. Create email via `Email/import` with `mailboxIds` set to the target folder
4. Mark as read by setting `keywords: {"$seen": true}`

The loader is idempotent: it checks for existing emails by subject+sender
before creating, so re-running doesn't duplicate.

### `generate_inbox_emails.py` — Dynamic Inbox Generator

Generates inbox emails with dynamic timestamps:

```python
def generate_inbox_emails() -> list[dict]:
    """Generate test emails for inbox with dynamic receivedAt times."""
    now = datetime.now(timezone.utc)
    return [
        {
            "from": "noreply@chase.com",
            "subject": f"Test: Chase statement {now:%Y%m%d%H%M}",
            "receivedAt": (now - timedelta(hours=5)).isoformat(),
            "keywords": {"$seen": True},
            "expectedOutcome": "moved",
            "expectedFolder": "Affairs/Banks",
        },
        {
            "from": "noreply@chase.com",
            "subject": f"Test: Chase unread {now:%Y%m%d%H%M}",
            "receivedAt": (now - timedelta(hours=5)).isoformat(),
            "keywords": {},  # unread
            "expectedOutcome": "unread",
        },
        # ... etc for each scenario
    ]
```

### `run_system_test.py` — Test Orchestrator

Runs the full test sequence:

```
Phase 1: Setup
  - Connect to test Fastmail account
  - Verify folder structure exists
  - Load fixture emails into folders via JMAP
  - Load dynamic inbox emails via JMAP

Phase 2: Bootstrap
  - Run `mailsort bootstrap --config config.test.yaml`
  - Verify rules created (exact_sender for chase, amazon, etc.)
  - Verify folder descriptions generated
  - Verify coverage percentage

Phase 3: Dry Run
  - Run `mailsort dry-run --config config.test.yaml`
  - Verify audit_log entries for all inbox emails
  - Verify correct classification sources (rule vs LLM)
  - Verify eligibility outcomes (moved, unread, flagged, too_new)
  - Verify no emails actually moved

Phase 4: Live Run
  - Run `mailsort run --config config.test.yaml`
  - Verify eligible emails actually moved to correct folders via JMAP
  - Verify ineligible emails still in inbox
  - Verify audit_log moved=1 for moved emails

Phase 5: User Correction Simulation
  - Move one email back from Banks to Stores via JMAP (simulate user correction)
  - Run `mailsort run --config config.test.yaml` again
  - Verify correction detected in learning step
  - Verify rule confidence penalized
  - Verify manual audit_log row created

Phase 6: Cleanup
  - Delete all test emails from the account
  - Remove test database
```

### `verify_results.py` — Result Validator

Queries the test database and validates expectations:

```python
def verify_bootstrap(db):
    """Verify bootstrap created expected rules and descriptions."""
    rules = db.execute("SELECT * FROM rules WHERE active = 1").fetchall()
    assert len(rules) >= 6  # at least 6 sender rules
    # ... specific checks

def verify_dry_run(db, run_id, expected_outcomes):
    """Verify each email got the expected classification and outcome."""
    for email in expected_outcomes:
        row = db.execute(
            "SELECT * FROM audit_log WHERE email_id = ? AND run_id = ?",
            (email["id"], run_id),
        ).fetchone()
        assert row["skip_reason"] == email["expectedOutcome"]
        # ...
```

---

## 4. How to Run

### One-time setup

```bash
# 1. Create folders in test Fastmail account (manual, one-time)
#    Affairs/Banks, Affairs/Stores, People/Children

# 2. Create API token for test account
#    Fastmail → Settings → Privacy & Security → API Tokens
#    Scopes: Mail (full access)

# 3. Set environment variables
export FASTMAIL_TEST_TOKEN="fmu1-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Create test config
cp tests/system/config.test.yaml config.test.yaml
# Edit to set correct test account email in `to` field
```

### Running

```bash
# Full automated sequence
python tests/system/run_system_test.py --config config.test.yaml

# Or step by step:
python tests/system/load_fixtures.py --config config.test.yaml   # Load test data
mailsort bootstrap --config config.test.yaml                      # Bootstrap
mailsort dry-run --config config.test.yaml                        # Dry run
mailsort run --config config.test.yaml                            # Live run

# Verify via web UI
mailsort web --config config.test.yaml --port 8081
```

### Cleanup

```bash
python tests/system/load_fixtures.py --config config.test.yaml --cleanup
rm -f data/test.db
```

---

## 5. What This Validates That Unit/Integration Tests Don't

| Aspect | Unit/Integration Tests | System Tests |
|--------|----------------------|--------------|
| JMAP API calls | Mocked | Real Fastmail API |
| Email creation | N/A | Real `Email/import` |
| Email moving | Mock returns `True` | Real `Email/set` update |
| Mailbox tree | Hardcoded 5 folders | Real Fastmail folders |
| LLM classification | Mocked or skipped | Real Anthropic API calls |
| Bootstrap evidence | Seeded rows in SQLite | Real folder scan via JMAP |
| Eligibility (unread/flagged) | Synthetic keywords | Real JMAP keyword state |
| Learning corrections | Mocked mailbox check | Real JMAP mailbox relocation |
| End-to-end latency | Instant | Real network latency |
| Data integrity | In-memory SQLite | File-based SQLite |

---

## 6. Risks and Mitigations

- **Test account pollution**: all test emails have a `[TEST]` prefix in subject
  and a `$mailsort-test` keyword for easy cleanup
- **API rate limits**: Fastmail has generous limits but the loader batches
  creates into groups of 50 to stay safe
- **Flaky LLM**: LLM classification is non-deterministic; the verifier checks
  that LLM was *used* (source=llm) but doesn't assert specific folder choices
  for LLM-classified emails
- **Clock skew**: dynamic `receivedAt` uses UTC and the `too_new` check uses
  UTC, so clock skew between local and Fastmail shouldn't matter
- **Cost**: each run uses ~10-20 LLM calls at Haiku pricing (~$0.01 total)
