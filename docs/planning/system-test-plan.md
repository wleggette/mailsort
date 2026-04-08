# System Test Plan: End-to-End Testing with Fastmail Test Account

## Table of Contents

- [Goal](#goal)
- [Architecture → Test Plan Mapping](#architecture--test-plan-mapping)
- [1. Test Account Setup](#1-test-account-setup)
- [2. Test Infrastructure](#2-test-infrastructure)
- [3. Phase 1: Bootstrap](#3-phase-1-bootstrap)
  - [3.1 Folder Scenarios](#31-folder-scenarios)
  - [3.2 Email Feature Scenarios](#32-email-feature-scenarios)
  - [3.3 Description Generation Scenarios](#33-description-generation-scenarios)
  - [3.4 Rules from Evidence](#34-rules-from-evidence)
  - [3.5 Contact Import Scenarios](#35-contact-import-scenarios)
  - [3.6 Bootstrap Verification Checklist](#36-bootstrap-verification-checklist)
  - [3.7 Static Fixture Data](#37-static-fixture-data)
- [4. Phase 2: Dry Run](#4-phase-2-dry-run)
  - [4.1 Eligibility Gate Scenarios](#41-eligibility-gate-scenarios)
  - [4.2 Classification Source Scenarios](#42-classification-source-scenarios)
  - [4.3 No-LLM Dry Run Verification Checklist](#43-no-llm-dry-run-verification-checklist)
  - [4.4 Dry Run Verification Checklist](#44-dry-run-verification-checklist)
  - [4.5 Dynamic Inbox Emails](#45-dynamic-inbox-emails)
- [5. Phase 3: Live Move](#5-phase-3-live-move)
  - [5.1 Age Gate Test](#51-age-gate-test)
  - [5.2 Live Move Verification Checklist](#52-live-move-verification-checklist)
- [6. Phase 4: Learning & Feedback](#6-phase-4-learning--feedback)
  - [6.1 Learning Scenarios](#61-learning-scenarios)
  - [6.2 Test Execution Sequence](#62-test-execution-sequence)
  - [6.3 Learning Verification Checklist](#63-learning-verification-checklist)
  - [6.4 Scenarios Deferred to Unit Tests](#64-scenarios-deferred-to-unit-tests)
- [8. Cross-Cutting Edge Cases](#8-cross-cutting-edge-cases)
- [9. What This Validates That Unit/Integration Tests Don't](#9-what-this-validates)
- [10. Risks and Mitigations](#10-risks-and-mitigations)

---

## Goal

Validate the complete mailsort pipeline against a real Fastmail account with
controlled test data. This covers bootstrap → dry-run → live move → learning →
feedback loop — everything the integration tests verify with mocks, but now
against real JMAP.

This plan follows the methodology in
[system-test-methodology.md](../methodology/system-test-methodology.md).

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
  correction_penalty: 0.05
  coherence_lookback_days: 30
  coherence_min_sample: 3
  staleness_threshold_days: 365
  staleness_decay_days: 365
  staleness_floor: 0.6
  deactivation_threshold: 0.50
  base_confidence:
    list_id: 0.95
    exact_sender_floor: 0.80
    exact_sender_cap: 0.95
    exact_sender_per_evidence: 0.03
    sender_domain_floor: 0.75
    sender_domain_cap: 0.90
    sender_domain_per_evidence: 0.02

folder_description_overrides:
  "Affairs/Banks": "Bank statements, fraud alerts, and financial notifications"
  "Affairs/Stores": "Order confirmations, shipping updates, and return receipts"
  "People/Children": "School notices, activity signups, and pediatrician communications"

known_contact_overrides:
  "testcontact@example.com":
    relationship: "friend"

manual_rules:
  - rule_type: exact_sender
    condition_value: "admin@lincolnelementary.org"
    target_folder_path: "Children"

exclude_folder_patterns: []
skip_senders: []
```

Note: `min_age_minutes: 1` — short enough that most test emails pass the age
gate, but long enough that dynamically-generated "just arrived" emails trigger
the `too_new` skip reason.

### One-Time Setup

```bash
# 1. Create folders in test Fastmail account (manual, one-time)
# 2. Create API token (Settings → Privacy & Security → API Tokens, Mail full access)
# 3. Set environment variables
export FASTMAIL_TEST_TOKEN="fmu1-..."
export ANTHROPIC_API_KEY="sk-ant-..."
# 4. Create test config
cp tests/system/config.test.yaml config.test.yaml
```

---

## 2. Test Infrastructure

### Files

```
tests/system/
  config.test.yaml               # Test account config
  fixtures/
    folder_emails.json           # Static fixture emails for bootstrap seeding
  generate_inbox_emails.py       # Dynamic inbox email generator
  load_fixtures.py               # JMAP loader: creates emails in test folders
  run_system_test.py             # Orchestrates the full test sequence
  verify_results.py              # Queries DB and validates outcomes
```

### `load_fixtures.py` — JMAP Email Loader

1. Fetch mailbox tree via `Mailbox/get`
2. Create any missing folders via `Mailbox/set` (including empty test folders like `Affairs/Empty`)
3. Resolve target folder path → mailbox ID
4. Upload RFC 5322 message blob via JMAP `blob/upload`
5. Create email via `Email/import` with `mailboxIds` set to the target folder
6. Mark as read by setting `keywords: {"$seen": true}`

Idempotent: checks for existing folders and emails by subject+sender before creating.

### Run Modes

**Setup-only** — loads fixtures + bootstrap, then stops for manual work:

```bash
python tests/system/run_system_test.py --config config.test.yaml --setup-only
```

**Full sequence** — runs all phases with automated verification:

```bash
python tests/system/run_system_test.py --config config.test.yaml
```

**Step-by-step:**

```bash
python tests/system/load_fixtures.py --config config.test.yaml
mailsort bootstrap --config config.test.yaml
mailsort dry-run --config config.test.yaml
mailsort run --config config.test.yaml
```

### Cleanup

```bash
python tests/system/load_fixtures.py --config config.test.yaml --cleanup
rm -f data/test.db
```

---

## 3. Phase 1: Bootstrap

Bootstrap scans existing folders, collects email evidence into `audit_log`,
generates folder descriptions, creates rules from accumulated evidence, and
imports contacts. It runs in four internal sub-phases:

1. **No-LLM bootstrap (D3)** — temporarily unset `ANTHROPIC_API_KEY`, run bootstrap on a clean DB, verify all descriptions use `"Emails filed under {name}"` fallback, then wipe the DB
2. **Collect evidence** — scan folders, sample up to 50 emails each, insert into `audit_log` with `classification_source='manual'` and `moved=True`
3. **Create rules** — evaluate evidence per sender/domain/list-id using coherence and threshold checks
4. **Import contacts** — fetch `ContactCard` from Fastmail + merge config `known_contact_overrides`
5. **Coverage check** — report what percentage of evidence emails would match a created rule
6. **Idempotency check** — run bootstrap a second time, verify 0 new evidence rows, rules unchanged, descriptions unchanged

### 3.1 Folder Scenarios

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|----------|
| **F1** | Leaf folder with emails | `Affairs/Banks` with 20+ fixture emails | Scanned, up to 50 emails sampled, evidence rows inserted, description generated | Groups A–J (fixture emails in Banks, Stores, Children) |
| **F2** | Parent folder excluded by pattern | Set `exclude_folder_patterns: ["Affairs"]` | Removed from tree by `MailboxTree.build()` — never scanned. Children (`Affairs/Banks`) still present and scanned | Config variation |
| **F3** | Empty folder | `Affairs/Empty` (no fixture emails) | Scanned but 0 evidence rows. Fallback description: `"Emails filed under Empty"` | `load_fixtures.py` auto-creates folder via `Mailbox/set` |
| **F4** | System folder | Trash, Junk, Drafts, Sent | Excluded from tree by JMAP role — never scanned, no evidence, no description | Built-in (system folders exist by default) |
| **F5** | Re-run (idempotent) | Automated test always runs bootstrap twice | Second run inserts 0 new evidence rows. Existing descriptions preserved. Existing rules unchanged. Coverage report still works | `run_system_test.py` Phase 1 (bootstrap ×2, verify 0 new rows) |
| **F6** | Folder with >50 emails | A folder with 60+ emails | Only 50 most recent sampled (JMAP `Email/query` with `sort: receivedAt DESC`, `limit: 50`) | Group K (pushes Banks to 52 total) |
| **F7** | Nested folder path | `Affairs/Banks` (two levels deep under INBOX) | Path correctly resolved to `"Affairs/Banks"` in `audit_log.folder` and `folder_descriptions.folder_path` | Groups A–J (all use nested paths) |

### 3.2 Email Feature Scenarios

Each evidence email is inserted into `audit_log` with extracted features. These
scenarios verify that the feature extraction pipeline works correctly during
bootstrap.

| ID | Feature | Setup | Expected `audit_log` State | Tested By |
|----|---------|-------|---------------------------|----------|
| **EF1** | Basic fields | Standard email with From, Subject, Date | `from_address`, `subject`, `received_at` all populated | Groups A–J (all fixture emails) |
| **EF2** | List-Id header | Email with `List-Id: <newsletter.school.org>` | `list_id = '<newsletter.school.org>'` extracted and stored | Group F |
| **EF3** | No List-Id | Standard email without List-Id | `list_id IS NULL` | Groups A–E, H–J (no list-id) |
| **EF4** | List-Unsubscribe header | Email with `List-Unsubscribe` header present | Feature captured (used as signal for mailing-list evidence) | Group F (include `List-Unsubscribe` in fixtures) |
| **EF5** | Sender domain extraction | `noreply@chase.com` | Domain `chase.com` extractable from `from_address` for domain rule evaluation | Group A (`noreply@chase.com`) |
| **EF6** | Classification source | All bootstrap evidence emails | `classification_source = 'manual'` (user-sorted, not system-classified) | Groups A–J (all bootstrap evidence) |
| **EF7** | Moved flag | All bootstrap evidence emails | `moved = 1` (they are already in target folders) | Groups A–J |
| **EF8** | Skip reason | All bootstrap evidence emails | `skip_reason IS NULL` (not skipped — they're evidence, not inbox processing) | Groups A–J |
| **EF9** | Duplicate email | Same email scanned on re-run | Not re-inserted (idempotent check by `email_id`) | Procedural (F5: run bootstrap ×2) |

### 3.3 Description Generation Scenarios

Folder descriptions are generated after evidence collection. The system checks
for existing descriptions and config overrides before generating new ones.

| ID | Scenario | Config/State | Expected Behavior | Tested By |
|----|----------|-------------|-------------------|----------|
| **D1** | Config override present | `folder_description_overrides: {"Affairs/Banks": "..."}` | Override text stored directly. LLM NOT called for this folder | Config (`folder_description_overrides` for Banks, Stores, Children) |
| **D2** | No override, LLM available | `ANTHROPIC_API_KEY` set, no existing description | LLM called with `FOLDER_DESCRIPTION_PROMPT` + sample subjects/senders. Description stored in `folder_descriptions` table | Groups A–J (provide sample data for LLM prompt) |
| **D3** | No override, no LLM | `ANTHROPIC_API_KEY` not set | Fallback: `"Emails filed under {leaf_name}"` (e.g., `"Emails filed under Banks"`) | (a) `Affairs/Empty` always gets fallback (0 emails, nothing to prompt LLM with); (b) Phase 1 step 1: no-LLM bootstrap on clean DB, verify all descriptions are fallback, wipe, then proceed |
| **D4** | Existing description | Re-run bootstrap, description already in DB | Existing description preserved — LLM NOT called again | Procedural (F5: run bootstrap ×2) |
| **D5** | Empty folder, no override | Folder with 0 emails, no config override | Fallback description used (no email samples to send to LLM) | F3 (`Affairs/Empty`); also validates D3(a) |
| **D6** | Path normalization | Override key `"Affairs/Banks"` vs folder path `"INBOX/Affairs/Banks"` | Override matched after normalizing: leading `INBOX/` stripped, compared case-insensitively | Config (`folder_description_overrides`) |
| **D7** | All folders described | After bootstrap completes | Every non-system folder in tree has a row in `folder_descriptions` | Groups A–J (all folders receive evidence) |

### 3.4 Rules from Evidence

After evidence is collected, bootstrap evaluates all three rule types
independently and **creates every rule whose thresholds are met**. A single
sender can produce multiple rules (e.g., both `list_id` and `exact_sender`).
Classification-time priority (list_id → exact_sender → sender_domain)
determines which rule actually fires. Each rule type has its own threshold
and coherence requirements.

**Key definitions:**
- **Coherence** = (emails to target folder) / (total emails from that condition value across all folders)
- **Thresholds** (defaults from `AutoRuleThresholdsConfig`):
  - `list_id`: ≥2 emails, coherence ≥80%
  - `sender_domain`: ≥5 emails, ≥3 distinct senders to same folder, coherence ≥80%
  - `exact_sender`: ≥3 emails, coherence ≥80%

**Covered by unit/integration tests** (not in this system test plan — no real JMAP needed):
- Deleted folder evidence filtering — `test_bootstrap_skips_deleted_folder_evidence` in `test_bootstrap.py`
- Coverage calculation accuracy — `test_bootstrap_coverage_calculation` in `test_bootstrap.py`
- Per-contact error isolation — `test_refresh_contacts_bad_contact_skipped` in `test_contacts.py`

#### 3.4.1 list_id Rules

| ID | Scenario | Evidence Shape | Expected Outcome | What It Tests | Tested By |
|----|----------|---------------|------------------|---------------|----------|
| **LR1** | High coherence, at threshold (boundary) | 2× list-id `<newsletter.school.org>` → Children | `list_id` rule created for `<newsletter.school.org>` → Children | Boundary pass: minimum count (2) with high coherence | Group F |
| **LR2** | Low coherence | 2× `<alerts.mixed.com>` → Banks, 2× → Stores | NO rule (50% coherence < 80%) | Coherence rejection | Group G |
| **LR3** | Below count threshold | 1× `<rare.list.org>` → Banks | NO rule (count < 2) | Count threshold rejection | Group L |
| **LR4** | List-Id present and sender also qualifies for exact_sender | 4× list-id `<updates.ymca.org>` from `activities@ymca.org` → Children (sender also has 4× without list-id in Group A) | **Both** `list_id` rule AND `exact_sender` rule created. `list_id` fires at classification time | All eligible rules created; classification priority picks list_id | Group A + Group N (`activities@ymca.org`) |

#### 3.4.2 sender_domain Rules

| ID | Scenario | Evidence Shape | Expected Outcome | What It Tests | Tested By |
|----|----------|---------------|------------------|---------------|----------|
| **DR1** | High coherence, 3+ senders, 5+ total | `statements@bigbank.com` (3×), `alerts@bigbank.com` (3×), `fraud@bigbank.com` (2×) all → Banks | `sender_domain` rule for `bigbank.com` → Banks | Happy path: domain concentration | Group B |
| **DR2** | Low coherence — "Amazon problem" | `orders@megastore.com` (4×) → Stores, `alerts@megastore.com` (3×) → Banks | NO domain rule (max coherence 57%). Individual `exact_sender` rules form instead | Split domain across folders | Group C |
| **DR3** | High coherence but <3 distinct senders | 5× `single@concentrated.com` → Banks | NO domain rule (only 1 sender). `exact_sender` rule forms instead | Distinct sender threshold | Group J |
| **DR4** | High coherence, 3 senders, exactly 5 total | Threshold boundary | `sender_domain` rule created | Boundary: minimum passing | Group M (`@boundarybank.com`) |
| **DR5** | Domain qualifies and emails also have list_id | 5× from 3 senders at `@community.org`, all with list-id `<updates.community.org>` → Children | **Both** `sender_domain` rule for `community.org` AND `list_id` rule created. `list_id` fires at classification time | All eligible rules created; list_id + sender_domain coexistence | Group O |
| **DR6** | High coherence, exactly 2 distinct senders (boundary fail) | `info@twopeople.com` (3×) + `support@twopeople.com` (2×) all → Banks | NO domain rule (2 distinct senders < 3). `exact_sender` for `info@` only (3 ≥ 3) | Boundary: distinct sender threshold rejection | Group Q |

#### 3.4.3 exact_sender Rules

| ID | Scenario | Evidence Shape | Expected Outcome | What It Tests | Tested By |
|----|----------|---------------|------------------|---------------|----------|
| **ER1** | High coherence, above threshold | 5× `noreply@chase.com` → Banks | `exact_sender` rule for `noreply@chase.com` → Banks | Happy path | Group A |
| **ER2** | Low coherence | 2× `alice@family.com` → Banks, 2× → Children | NO rule (50% coherence) | Coherence rejection | Group H |
| **ER3** | Below count threshold | 2× `rare@oneoff.com` → Banks | NO rule (count < 3) | Count threshold rejection | Group I |
| **ER4** | Sender qualifies alongside domain rule | `statements@bigbank.com` (3×) and `alerts@bigbank.com` (3×) both → Banks, domain rule also exists | `exact_sender` rules created for both (alongside domain rule). `fraud@bigbank.com` (2×) below threshold — no exact_sender | All eligible rules created independently | Group B |
| **ER5** | Multiple senders, same domain, different folders | Per-address `exact_sender` rules for each address that qualifies | `orders@megastore.com` → Stores, `alerts@megastore.com` → Banks (separate rules) | Per-address routing when domain is split | Group C |
| **ER6** | Sender at split domain, below threshold | `returns@megastore.com` (2×) → Stores | NO rule (count < 3). Falls to LLM at inbox time | Below threshold at split domain | Group C (`returns@megastore.com`) |
| **ER7** | Exactly at count threshold (boundary) | 3× `receipts@shopify.com` → Stores | `exact_sender` rule created | Boundary pass: exactly at ≥3 threshold | Group P |
| **ER8** | Coherence exactly at 80% (boundary) | 4× `billing@utility.com` → Banks, 1× → Stores | `exact_sender` rule created (4/5 = 80% ≥ 80%) | Boundary pass: coherence exactly at threshold | Group R |

#### 3.4.4 Priority Interactions

| ID | Scenario | Evidence | Expected Rules Created | What It Tests | Tested By |
|----|----------|---------|----------------------|---------------|----------|
| **P1** | list_id + exact_sender coexist | Emails have list-id, sender also qualifies for exact_sender | Both `list_id` and `exact_sender` rules created. `list_id` fires at classification time | All eligible rules created; classification priority resolves | LR4 (Group A + Group N) |
| **P2** | sender_domain + exact_sender coexist | `@bigbank.com` domain rule created, individual senders also qualify | Domain rule + `exact_sender` rules for qualifying senders all created. `exact_sender` fires first at classification time | All eligible rules created; exact_sender beats sender_domain | Group B |
| **P3** | Split domain — exact_sender only | `@megastore.com` fails domain coherence | Only per-address `exact_sender` rules for qualifying senders (no domain rule) | Domain coherence prevents domain rule; exact_sender rules unaffected | Group C |

#### 3.4.5 Confidence Assignment

| ID | Scenario | Expected Confidence | What It Tests | Tested By |
|----|----------|-------------------|---------------|----------|
| **CA1** | Confidence by rule type (from `BaseConfidenceConfig`) | `list_id`: 0.95, `sender_domain`: min(0.90, 0.75 + n×0.02), `exact_sender`: min(0.95, 0.80 + n×0.03) | `confidence` matches formula for rule type and evidence count | All rules from Groups A–J |
| **CA2** | Rule type stored | `rule_type` column matches: `list_id`, `sender_domain`, or `exact_sender` | Correct rule type recorded | All rules from Groups A–J |
| **CA3** | Target folder stored | `target_folder` matches the dominant folder from evidence | Correct target | All rules from Groups A–J |
| **CA4** | Initial confidence matches `BaseConfidenceConfig` formula | All rules have `confidence` matching the formula for their type and evidence count | `BaseConfidenceConfig` applied correctly at creation time | All rules from Groups A–J |

### 3.5 Contact Import Scenarios

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|----------|
| **CI1** | Contacts fetched from Fastmail | Test account has `ContactCard` entries | Contacts cached in `contacts` table | Test account address book |
| **CI2** | Config override merged | `known_contact_overrides` has `testcontact@example.com` | Entry appears in contacts cache with `relationship: "friend"` | Config (`known_contact_overrides`) |
| **CI3** | No contacts | Empty address book, no overrides | Empty contacts table; LLM uses standard threshold for all senders | Config variation (remove overrides) |
| **CI4** | Re-run idempotent | Run bootstrap twice | Contacts refreshed (upserted), not duplicated | Procedural (F5: run bootstrap ×2) |
| **CI5** | Contacts scope unavailable | JMAP token lacks `urn:ietf:params:jmap:contacts` scope | Contacts import skipped gracefully (log warning, empty contacts table). LLM uses standard threshold for all senders | Config variation (revoke contacts scope) |

### 3.6 Bootstrap Verification Checklist

After bootstrap completes, verify:

- [ ] **Bootstrap run record**: `runs` table has a row with `trigger='bootstrap'`, `status='completed'`, `emails_moved=0`
- [ ] **Evidence rows**: `audit_log` contains one row per sampled email, all with `classification_source='manual'` and `moved=1`
- [ ] **No duplicates**: running bootstrap again inserts 0 new rows
- [ ] **Folder descriptions**: every non-system leaf folder has a description in `folder_descriptions`
- [ ] **Config overrides applied**: folders with `folder_description_overrides` have the override text, not LLM-generated
- [ ] **Rules created**: expected rules exist in `rules` table with `active=1`
- [ ] **Rules NOT created**: conditions below threshold or coherence produce no rule
- [ ] **All eligible rules created**: each rule type that meets its thresholds is created independently (e.g., both `list_id` and `exact_sender` for the same sender)
- [ ] **Confidence values**: rule confidence matches the `BaseConfidenceConfig` formula for each rule type (CA1–CA4)
- [ ] **Hit counts unchanged**: all rules have `hit_count=0` and `last_relevant_at IS NULL` after bootstrap (coverage check is read-only, must not record hits)
- [ ] **Contacts imported**: `contacts` table populated from Fastmail + config overrides
- [ ] **Coverage report**: bootstrap prints coverage percentage (evidence emails that would match a rule)
- [ ] **Sampling cap (F6)**: folders with >50 emails sample exactly 50 (most recent by `receivedAt`)

### 3.7 Static Fixture Data

Pre-defined emails loaded from `tests/system/fixtures/folder_emails.json`. These seed
the folders with enough evidence for bootstrap to create rules.

**Group A: Clean exact_sender rules (high coherence) — tests ER1**

| Sender | Folder | Count | Expected Rule |
|--------|--------|-------|---------------|
| `noreply@chase.com` | Banks | 5 | exact_sender |
| `alerts@bankofamerica.com` | Banks | 4 | exact_sender |
| `orders@amazon.com` | Stores | 5 | exact_sender |
| `noreply@target.com` | Stores | 4 | exact_sender |
| `admin@lincolnelementary.org` | Children | 5 | exact_sender |
| `activities@ymca.org` | Children | 4 | exact_sender (also has list_id rule from Group N — both coexist; see LR4) |

**Group B: Domain coherence — high — tests DR1**

| Sender | Folder | Count |
|--------|--------|-------|
| `statements@bigbank.com` | Banks | 3 |
| `alerts@bigbank.com` | Banks | 3 |
| `fraud@bigbank.com` | Banks | 2 |
| | | **8 total, 3 distinct senders, 100% coherence → `sender_domain` rule for `bigbank.com` + `exact_sender` rules for `statements@` (3×) and `alerts@` (3×); `fraud@` (2×) below exact_sender threshold** |

**Group C: Domain coherence — low, "Amazon problem" — tests DR2, ER5, ER6**

| Sender | Folder | Count | Expected |
|--------|--------|-------|----------|
| `orders@megastore.com` | Stores | 4 | → exact_sender rule |
| `alerts@megastore.com` | Banks | 3 | → exact_sender rule |
| `returns@megastore.com` | Stores | 2 | → NO rule (count < 3) |
| | | **9 total, 44% max coherence** | → NO domain rule |

**Group D: Known contact, split — tests CI2, ER2**

| Sender | Folder | Count |
|--------|--------|-------|
| `testcontact@example.com` | Banks | 2 |
| `testcontact@example.com` | Children | 2 |
| `testcontact@example.com` | Stores | 1 |
| | | **5 total, 40% coherence → NO rule; LLM with known-contact threshold** |

**Group E: Known contact, concentrated — tests ER1, CI1**

| Sender | Folder | Count |
|--------|--------|-------|
| `testfriend@gmail.com` | Children | 4 |
| | | **100% coherence → exact_sender rule** |

**Group F: List-Id rule — tests LR1**

| List-Id | Sender | Folder | Count |
|---------|--------|--------|-------|
| `<newsletter.school.org>` | `newsletter@school.org` | Children | 2 |
| | | | **2 total, 100% coherence → list_id rule (boundary: exactly at ≥2 threshold). Sender is unique to this group — no overlap with other rules** |

**Group G: List-Id low coherence — tests LR2**

| List-Id | Sender | Folder | Count |
|---------|--------|--------|-------|
| `<alerts.mixed.com>` | `a@mixed.com` | Banks | 2 |
| `<alerts.mixed.com>` | `b@mixed.com` | Stores | 2 |
| | | | **4 total, 50% coherence → NO list_id rule** |

**Group H: Unknown sender, split — tests ER2**

| Sender | Folder | Count |
|--------|--------|-------|
| `alice@family.com` | Banks | 2 |
| `alice@family.com` | Children | 2 |
| | | **50% coherence → NO rule** |

**Group I: Below threshold — tests ER3**

| Sender | Folder | Count |
|--------|--------|-------|
| `rare@oneoff.com` | Banks | 2 |
| | | **count < 3 → NO rule** |

**Group J: Domain high coherence, <3 distinct senders — tests DR3**

| Sender | Folder | Count |
|--------|--------|-------|
| `single@concentrated.com` | Banks | 5 |
| | | **5 total, 1 distinct sender, 100% coherence → NO domain rule; exact_sender rule only** |

**Group K: Bulk multi-sender domain + sampling cap — tests F6, DR, ER**

| Sender | Folder | Count |
|--------|--------|-------|
| `portal@myhealth.com` | Medical | 20 |
| `labs@myhealth.com` | Medical | 18 |
| `appointments@myhealth.com` | Medical | 17 |
| | | **55 total, 3 distinct senders, 100% coherence. Bootstrap samples only 50 of 55 (F6). First 5 emails in fixture array are oldest and excluded by the cap (2 portal, 2 labs, 1 appointments). After sampling: portal ~18, labs ~16, appointments ~16 — all above thresholds. Expected: `sender_domain` rule for `myhealth.com` + 3 `exact_sender` rules, all surviving the sampling cap** |

**Group L: Single list-id email — tests LR3**

| List-Id | Sender | Folder | Count |
|---------|--------|--------|-------|
| `<rare.list.org>` | `digest@rare.org` | Banks | 1 |
| | | | **count < 2 → NO list_id rule** |

**Group M: Domain boundary case (exactly 5 total, 3 senders) — tests DR4**

| Sender | Folder | Count |
|--------|--------|-------|
| `alpha@boundarybank.com` | Stores | 2 |
| `beta@boundarybank.com` | Stores | 2 |
| `gamma@boundarybank.com` | Stores | 1 |
| | | **5 total, 3 distinct senders, 100% coherence → `sender_domain` rule for `boundarybank.com` (boundary pass)** |

**Group N: List-Id + exact_sender coexistence — tests LR4**

| List-Id | Sender | Folder | Count |
|---------|--------|--------|-------|
| `<updates.ymca.org>` | `activities@ymca.org` | Children | 4 |
| | | | **Same sender as Group A (4× without list-id). Combined: 8 emails, all → Children. Sender qualifies for exact_sender (8 ≥ 3) and list-id qualifies for list_id (4 ≥ 2). Expected: both `list_id` and `exact_sender` rules created; `list_id` fires at classification time** |

**Group O: List-Id + sender_domain coexistence — tests DR5**

| List-Id | Sender | Folder | Count |
|---------|--------|--------|-------|
| `<updates.community.org>` | `events@community.org` | Children | 2 |
| `<updates.community.org>` | `news@community.org` | Children | 2 |
| `<updates.community.org>` | `admin@community.org` | Children | 1 |
| | | | **5 total, 3 distinct senders, 100% coherence, all with same list-id. Expected: `sender_domain` rule for `community.org` AND `list_id` rule for `<updates.community.org>` both created; `list_id` fires at classification time** |

**Group P: exact_sender count boundary — tests ER7**

| Sender | Folder | Count |
|--------|--------|-------|
| `receipts@shopify.com` | Stores | 3 |
| | | **3 total, 100% coherence → `exact_sender` rule (boundary: exactly at ≥3 threshold)** |

**Group Q: sender_domain distinct sender boundary — tests DR6**

| Sender | Folder | Count |
|--------|--------|-------|
| `info@twopeople.com` | Banks | 3 |
| `support@twopeople.com` | Banks | 2 |
| | | **5 total, 2 distinct senders, 100% coherence → NO `sender_domain` rule (2 < 3 senders). `exact_sender` for `info@` only (3 ≥ 3); `support@` below threshold (2 < 3)** |

**Group R: exact_sender coherence boundary — tests ER8**

| Sender | Folder | Count |
|--------|--------|-------|
| `billing@utility.com` | Banks | 4 |
| `billing@utility.com` | Stores | 1 |
| | | **5 total, coherence = 4/5 = 80% (exactly at threshold). Count to target = 4 ≥ 3 → `exact_sender` rule created (boundary: coherence exactly at ≥80%)** |

**Total: 153 fixture emails across 4 folders (Banks: 43, Stores: 27, Children: 28, Medical: 55)**

Each fixture email includes: `from`, `to`, `subject`, `textBody`, `receivedAt`
(spread across last 30 days), `keywords` (`{"$seen": true}`), `targetFolder`,
and optionally `listId`.

---

## 4. Phase 2: Dry Run

Dry run classifies all inbox emails but does **not** move them. It produces
`audit_log` entries with classification results that can be verified without
side effects. It runs in three internal sub-phases:

1. **Generate inbox emails** — create dynamic inbox emails with controlled timestamps, keywords, and senders for classification and eligibility testing
2. **No-LLM dry run (S9)** — temporarily unset `ANTHROPIC_API_KEY`, run `mailsort dry-run`, verify rule/thread matches still work and LLM-dependent emails get `skip_reason='llm_unavailable'`
3. **Full dry run** — run `mailsort dry-run` with LLM enabled, verify classification sources, eligibility gates, and skip reasons

Each dry-run pass gets its own `run_id` so verifiers can query by run.

### 4.1 Eligibility Gate Scenarios

Eligibility gates are applied **after** classification. Every inbox email is
fully classified (thread → rules → LLM), so the audit log shows what mailsort
*would* do. Then ineligible emails have `should_move` overridden to false.

| ID | Scenario | Keywords | receivedAt | Expected Outcome | Tested By |
|----|----------|----------|------------|-----------------|----------|
| **E1** | Read, unflagged, old enough | `$seen` | 5h ago | classified (moved or below_threshold) | Inbox gen: E1 (`noreply@chase.com`) |
| **E2** | Unread | (none) | 5h ago | classified (source=rule or llm), skip_reason=unread | Inbox gen: E2 (`orders@amazon.com`) |
| **E3** | Read + flagged | `$seen`, `$flagged` | 5h ago | classified (source=rule or llm), skip_reason=flagged | Inbox gen: E3 (`noreply@chase.com`) |
| **E4** | Read, unflagged, too new | `$seen` | now | classified (source=rule or llm), skip_reason=too_new | Inbox gen: E4 (`alerts@bankofamerica.com`) |
| **E5** | Unread + flagged + new | `$flagged` | now | classified (source=rule or llm), skip_reason=unread (checked first) | Inbox gen: E5 (`noreply@target.com`) |

### 4.2 Classification Source Scenarios

These test which classification source is used for each inbox email, based on
the rules and contacts created during bootstrap.

| ID | Scenario | Expected Source | Expected Outcome | Tested By |
|----|----------|----------------|-----------------|----------|
| **S1** | Sender has exact_sender rule, eligible | rule | moved | Inbox gen: E1 (`noreply@chase.com`) |
| **S2** | Sender's domain has sender_domain rule, eligible | rule | moved | Inbox gen: S2 (`support@bigbank.com`) |
| **S3** | Email has list_id with list_id rule, eligible | rule | moved | Inbox gen: S3 (`newsletter@lincolnelementary.org`) |
| **S4** | Thread sibling was previously sorted | thread | moved | Inbox gen: S4 (`rare@oneoff.com` with In-Reply-To) |
| **S5** | No rule, LLM classifies above threshold | llm | moved | Inbox gen: S5 (`newsletter@newbank.com`) |
| **S6** | No rule, LLM below threshold | llm | below_threshold | Inbox gen: S6 (`info@ambiguous-service.com`) |
| **S7** | Known contact, LLM above known-contact threshold | llm | moved | Inbox gen: S7 (`testcontact@example.com`, strong banking content) |
| **S8** | Known contact, LLM between normal and known-contact threshold | llm | below_threshold_known_contact | Inbox gen: S8 (`testcontact@example.com`) |
| **S9** | No rule, no LLM configured | — | llm_unavailable | No-LLM dry run (§4.3) |
| **S10** | LLM cache hit on second dry run | llm | same as first run, `cached=1` | Run full dry run twice with no config change; second run's audit rows for LLM-classified emails have `cached=1` |
| **S11** | Ineligible email still gets LLM classification | llm | `skip_reason=flagged`, `source='llm'` | Inbox gen: S11 (`updates@newinsurance.com`) — flagged, no rule match, LLM classifies, audit shows target folder |

### 4.3 No-LLM Dry Run Verification Checklist

The no-LLM dry run temporarily unsets `ANTHROPIC_API_KEY` and runs
`mailsort dry-run` before the full run. Verify:

- [ ] **Rule/thread matches work**: emails matching rules or thread context are classified normally (`classification_source='rule'` or `'thread'`)
- [ ] **LLM-dependent emails skipped**: emails without rule/thread match have `skip_reason='llm_unavailable'`
- [ ] **C5 specifically**: `random@unknown.com` has `skip_reason='llm_unavailable'`
- [ ] **No crash**: graceful degradation, no unhandled exceptions

### 4.4 Dry Run Verification Checklist

- [ ] **Run record created**: `runs` table has a row with `status='completed'` and `emails_moved=0` for the dry-run `run_id`
- [ ] **Hit counts unchanged**: all rules have `hit_count=0` after dry run (dry run does not record hits). `last_relevant_at` IS NOT NULL — `compute_rule_confidence()` runs during the learning step even on dry runs and populates it from bootstrap audit_log evidence
- [ ] **Audit log populated**: every inbox email has an `audit_log` row for each dry-run pass (query by `run_id`)
- [ ] **Classification sources correct**: rule, thread, llm, or none as expected
- [ ] **Skip reasons correct**: unread, flagged, too_new where expected
- [ ] **No emails moved**: all emails still in INBOX (dry run = read-only)
- [ ] **Rule matches use correct rule type**: exact_sender, sender_domain, or list_id
- [ ] **Classification priority — exact_sender over sender_domain**: `statements@bigbank.com` matches `exact_sender` rule (not `sender_domain` for `bigbank.com`)
- [ ] **Classification priority — list_id over exact_sender**: email with list-id `<updates.ymca.org>` from `activities@ymca.org` matches `list_id` rule (not `exact_sender`)
- [ ] **LLM called only when no rule/thread match AND no valid cache**: classification_source=llm only for fallback cases; second run should show `cached=1` for repeat LLM emails
- [ ] **Cache hits recorded**: second dry run has `cached=1` rows for previously-LLM-classified emails that are still in inbox
- [ ] **Non-LLM sources not cached**: thread and rule classification rows have `cached=0`

### 4.5 Dynamic Inbox Emails

A Python script (`tests/system/generate_inbox_emails.py`) creates inbox emails
with dynamic timestamps for testing all classification and eligibility scenarios
at runtime.

| Scenario | From | Keywords | receivedAt | Expected |
|----------|------|----------|------------|----------|
| E1: Rule match, eligible | `noreply@chase.com` | `$seen` | 5h ago | moved → Banks (rule) |
| E2: Rule match, unread | `orders@amazon.com` | (none) | 5h ago | source=rule, skip_reason=unread |
| E3: Rule match, flagged | `noreply@chase.com` | `$seen`, `$flagged` | 5h ago | source=rule, skip_reason=flagged |
| E4: Rule match, too new | `alerts@bankofamerica.com` | `$seen` | now | source=rule, skip_reason=too_new |
| E5: Unread + flagged + new | `noreply@target.com` | `$flagged` | now | source=rule, skip_reason=unread |
| S11: Flagged, no rule, LLM classifies | `updates@newinsurance.com` | `$seen`, `$flagged` | 5h ago | source=llm, skip_reason=flagged, target_folder shows LLM's choice |
| S2: Domain rule match | `support@bigbank.com` | `$seen` | 5h ago | moved → Banks (rule) |
| S3: List-Id rule match | `newsletter@lincolnelementary.org` | `$seen` | 5h ago | moved → Children (list_id rule) |
| S4: Thread match | `rare@oneoff.com` (In-Reply-To) | `$seen` | 5h ago | moved → Banks (thread context) |
| S5: LLM above threshold | `newsletter@newbank.com` | `$seen` | 5h ago | LLM classifies |
| S6: LLM below threshold | `info@ambiguous-service.com` | `$seen` | 5h ago | below_threshold |
| S7: Known contact, high-confidence | `testcontact@example.com` | `$seen` | 5h ago | moved (LLM ≥0.93, strong banking subject/body) |
| S8: Known contact, ambiguous | `testcontact@example.com` | `$seen` | 5h ago | below_threshold_known_contact (ambiguous content) |
| C1: Known contact with rule | `testfriend@gmail.com` | `$seen` | 5h ago | moved → Children (rule) |
| C4: Unknown exact, split | `alice@family.com` | `$seen` | 5h ago | LLM (no rule) |
| C5: No match at all | `random@unknown.com` | `$seen` | 5h ago | LLM or no_classification |
| R5a: Megastore below threshold | `returns@megastore.com` | `$seen` | 5h ago | LLM (no rule) |
| R5b: Megastore per-address | `orders@megastore.com` | `$seen` | 5h ago | moved → Stores (exact_sender) |
| R5c: Megastore per-address | `alerts@megastore.com` | `$seen` | 5h ago | moved → Banks (exact_sender) |
| P1: exact_sender over sender_domain | `statements@bigbank.com` | `$seen` | 5h ago | moved → Banks (exact_sender, not sender_domain) |
| P2: list_id over exact_sender | `activities@ymca.org` (List-Id: `<updates.ymca.org>`) | `$seen` | 5h ago | moved → Children (list_id, not exact_sender) |
| L3a-1: Chase correction target #2 | `noreply@chase.com` | `$seen` | 5h ago | moved → Banks (rule) — corrected in Phase 4 for L3a |
| L3a-2: Chase correction target #3 | `noreply@chase.com` | `$seen` | 5h ago | moved → Banks (rule) — corrected in Phase 4 for L3a |

The generator uses `datetime.now(timezone.utc)` to produce `receivedAt`
timestamps relative to the current time, ensuring `too_new` scenarios work
regardless of when the test is run.

---

## 5. Phase 3: Live Move

Live run re-processes inbox emails and actually moves eligible ones via JMAP
`Email/set` with updated `mailboxIds`.

### 5.1 Age Gate Test

Two emails test the age gate:

- **E4** (`BofA too new`): `receivedAt = now + 5min` — always too_new across
  all phases. Used by dry-run and too-new-blocked verification.
- **Age gate email**: Injected fresh by `phase_age_gate` with
  `receivedAt = now` right before step 1. Transitions from too_new → eligible.

Steps:

1. `phase_age_gate` injects a fresh email (`alerts@bankofamerica.com`,
   `receivedAt = now`) into the inbox
2. Run live → both E4 and the age-gate email have `skip_reason=too_new`
3. Wait for `min_age_minutes` (1 minute) + 5s buffer
4. Run live → age-gate email is now eligible and moved; E4 remains too_new
5. Verify unread/flagged emails remain in INBOX

### 5.2 Live Move Verification Checklist

- [ ] **Eligible emails moved**: emails with expected outcome "moved" are no longer in INBOX (verified via JMAP)
- [ ] **Correct target folders**: each moved email is in the folder predicted by its rule/LLM classification
- [ ] **Ineligible emails unchanged**: unread, flagged emails still in INBOX
- [ ] **audit_log updated**: `moved=1` for moved emails in this run
- [ ] **Age gate works**: previously-too-new email now moved after waiting
- [ ] **JMAP state consistent**: `Email/get` confirms new `mailboxIds` for moved emails
- [ ] **No move_failed entries**: all moved emails have `skip_reason IS NULL` (confirms move pipeline succeeded cleanly)

---

## 6. Phase 4: Learning & Feedback

Tests manual-sort detection (5 categories), computed confidence model behavior,
dedup logic, and behavioral impact of confidence changes on classification.

> **Computed confidence model:** Rule confidence is recomputed each cycle from
> live state: `confidence = max(0, base × coherence × staleness − net_corrections × 0.05)`.
> With `correction_penalty=0.05`, three corrections stop any rule. A single
> correction reduces confidence but the rule stays active and continues firing
> until confidence drops below `rule_move` (0.85).

### 6.1 Learning Scenarios

#### Category 1: Skipped Sorts

Emails mailsort left in inbox (`moved=0`) that the user subsequently moved
to a folder. Detected by `_detect_skipped_sorts`.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L1** | Skipped email sorted by user | Move `info@ambiguous-service.com` (`skip_reason=below_threshold`, still in inbox) from INBOX → Banks via JMAP | Manual audit row created for Banks. `from_inbox` count incremented | System test: JMAP move + `mailsort run` |
| **L2** | Skipped email still in inbox (negative) | Don't move any skipped emails | No false positives — no manual rows for emails still in inbox | System test: implicit (L1 run verifies no spurious detections) |
| **L2a** | Skipped-sort excludes mailsort-moved emails | Dry run (Phase 2) creates `moved=0` for chase email; live run (Phase 3) moves same email (`moved=1`) | `_detect_skipped_sorts` excludes the email (NOT IN subquery filters non-manual `moved=1`). No false manual row for emails mailsort successfully moved | System test: verified in Phase 4 learning pass — check no manual rows for rule-moved emails |
| **L2b** | User move-and-return re-detection | User moves skipped email out of inbox (manual row created), then moves back to inbox, then sorts again | `_already_handled_email_ids` allows re-detection because no newer rule move exists after the manual row — but if the email re-enters inbox and is re-skipped, a new skipped row (newer than the manual row) makes it eligible again | *Deferred to unit test* (`test_skipped_sort_still_detected_after_user_move_and_return`) — requires multi-step JMAP sequence |
| **L2c** | Skipped-sort dedup prevents duplicate manual rows | Run `mailsort run` twice without new user moves (step 4 in execution sequence — same run that tests L6) | Second run creates no new manual row for L1's email (`_already_handled_email_ids` filters — most recent manual row is newer than any rule move). Count of manual rows for `ambiguous-service.com` = 1 | System test: second `mailsort run` (same run as L6) |

#### Category 2: Correction Sorts

Emails mailsort moved that the user relocated to a different folder.
Detected by `_detect_correction_sorts`. Corrections feed into the computed
confidence model as `net_corrections_in_window`.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L3** | Single correction — rule still fires | Move `noreply@chase.com` (rule-moved, conf≈0.95) from Banks → Stores via JMAP | Correction audit row for Stores (`classification_source='correction'`, `rule_id`=chase rule). After `compute_rule_confidence()`: confidence ≈ 0.95 × coherence × 1.0 − 0.05 ≈ 0.88 (1 net correction). **Still above `rule_move` (0.85)** — rule continues to fire. Broader rules (sender_domain) not explicitly penalized — coherence handles cascade | System test: JMAP move + `mailsort run` |
| **L3a** | 3 corrections — rule stops firing | After L3 (1 correction), move 2 more rule-moved chase emails (L3a-1, L3a-2) from Banks → Children and Stores via JMAP. Run `mailsort run` | 3 total correction rows for chase rule. Confidence ≈ 0.95 × coherence × 1.0 − 0.15 ≈ 0.78. **Below `rule_move` (0.85)** — rule stops firing, falls to LLM. Rule stays `active=1` (0.78 > `deactivation_threshold` 0.50). This is the "3 strikes stops firing" boundary | System test: 2 additional JMAP moves + `mailsort run` (step 6–8 in execution sequence) |
| **L3b** | Corrections + low coherence → deactivation | Rule with coherence ≈ 0.55 (emails split across folders) AND 2 net corrections | Confidence ≈ 0.95 × 0.55 × 1.0 − 2×0.05 = 0.5225 − 0.10 = 0.42. **Below `deactivation_threshold` (0.50)** — rule set to `active=0`. Neither factor alone would deactivate, but combined they cross the threshold | *Deferred to unit test* (`test_corrections_plus_low_coherence_deactivates`) — requires precise coherence setup |
| **L4** | Inbox return ignored (negative) | Move `alerts@megastore.com` (rule-moved) from Banks → INBOX via JMAP | **NOT** treated as correction (`new_path != "INBOX"` check). No manual row. Rule confidence unchanged | System test: JMAP move + `mailsort run` |
| **L5** | LLM-based correction, no rule correction | Move `returns@megastore.com` (LLM-moved, `rule_id=NULL`) from Stores → Banks via JMAP | Manual audit row for Banks. No rule to attribute correction to — `net_corrections` unaffected for all rules | System test: JMAP move + `mailsort run` |
| **L6** | Dedup — same correction not double-counted | Run `mailsort run` again without new JMAP moves (after L3) | `_already_handled_email_ids` filters chase email (most recent correction is newer than most recent rule move). No new correction row. `compute_rule_confidence()` is idempotent — same correction count, same confidence | System test: second `mailsort run` |
| **L6a** | Re-correction after new rule move | After L3 (chase corrected to Stores), move chase email back to INBOX. Next cycle, rule moves it to Banks again. User moves it to Stores again | Second correction row created (`_already_handled_email_ids` allows it — new rule move is newer than previous correction). `compute_rule_confidence()` now counts 2 corrections. Confidence drops further (2 × 0.05 = 0.10 penalty) | *Deferred to unit test* (`test_re_correction_after_new_rule_move`) — requires multi-step move sequence |

#### Category 3: Inbox Departures

Emails that were in the inbox last scan but are gone now — user sorted them
before mailsort processed them. Detected by `_detect_inbox_departures` via
snapshot diff.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L7** | Inbox departure detected | Move an unprocessed inbox email to a folder between two runs | Departure detected via snapshot diff. Manual audit row created | *Deferred to unit test* (`test_inbox_departure_detected`) — requires email in snapshot but not in audit_log, hard to set up after live run processed everything |

#### Category 4: Daily Folder Scan

Emails in non-inbox folders with no `audit_log` record. Runs at most once
per `folder_scan_interval_hours` (24h default).

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L8** | Folder scan finds unknown email | Place email directly in a folder (not via mailsort) | Scan detects it, creates manual audit row | *Deferred to unit test* (`test_folder_scan_finds_unknown_emails`) — 24h interval makes it impractical in system test |

#### Computed Confidence Model Behavior

Rule confidence is recomputed each cycle from live state. These scenarios test
the interaction of corrections, coherence, staleness, and deactivation.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L9** | Low-confidence rule stops firing | After L3, chase rule confidence ≈ 0.88 — still above `rule_move` (0.85). If additional corrections bring it below 0.85 (see L3a), the rule stays `active=1` but doesn't fire | Chase emails classified by LLM (`classification_source='llm'`, `rule_id=NULL`). Rule stays `active=1` — confidence is between `deactivation_threshold` (0.50) and `rule_move` (0.85). This is the "confidence gate" zone: alive but dormant | *Deferred to unit test* (`test_low_confidence_rule_stops_firing`) — requires rule with confidence in the 0.50–0.85 range at classification time |
| **L10** | `rule_move` boundary (0.85) | Rule with confidence at exactly 0.85 | Confidence ≥ 0.85 → rule fires. Confidence at 0.8499 → rule does NOT fire (strict `<` for firing, `>=` for gate). Tests the `rule_move` threshold | *Deferred to unit test* (`test_rule_move_boundary`) |
| **L10a** | `deactivation_threshold` boundary (0.50) | Rule with confidence at exactly 0.50 | Confidence ≥ 0.50 → rule stays `active=1`. Confidence at 0.4999 → `active=0`. Strict `<` comparison for deactivation | *Deferred to unit test* (`test_deactivation_threshold_boundary`) |
| **L10b** | Confidence floor at 0 | Rule with extreme corrections and low coherence | `max(0, ...)` prevents negative confidence. Value is exactly 0.0, not negative. Rule deactivated | *Deferred to unit test* (`test_confidence_floor_at_zero`) |
| **L11** | Staleness factor (365+ days since `last_relevant_at`) | Rule with `last_relevant_at` > 365 days ago | Staleness factor decays from 1.0 toward floor (0.6) over 365 days past threshold. Confidence = `base × coherence × staleness`. At 730 days: `staleness ≈ 0.6` | *Deferred to unit test* (`test_staleness_factor_decay`) — requires manipulating timestamps |
| **L13** | Coherence drift → confidence drop | Rule with emails splitting across two folders in lookback window | `coherence_factor < 1.0`. Confidence drops proportionally. If coherence later recovers, confidence recovers | *Deferred to unit test* (`test_coherence_drift_reduces_confidence`) |
| **L13a** | Coherence alone → deactivation | Rule with coherence ≈ 0.50 (no corrections, no staleness) | Confidence = 0.95 × 0.50 × 1.0 − 0 = 0.475 < 0.50 → **deactivated by coherence alone**. No corrections needed. Tests that coherence < ~0.53 deactivates any rule | *Deferred to unit test* (`test_coherence_alone_deactivates`) — requires precise coherence setup |
| **L14** | Correction sort-back recovery (net corrections) | After L3a (3 corrections), move chase L3a-1 email from Children back to Banks via JMAP (confirming sort). Run `mailsort run` | Confirming sort detected (`classification_source='manual'`, `from_address=noreply@chase.com`, `target_folder=Banks`). `net_corrections = max(0, 3 − 1) = 2`. Confidence partially recovers: ≈ 0.95 × coherence − 0.10 ≈ 0.83. Still below `rule_move` but higher than L3a | System test: JMAP move + `mailsort run` (step 9–11 in execution sequence) |
| **L14a** | Recovery → rule resumes firing | Rule at 0.78 (below `rule_move`). Corrections age out of 30d window (or confirming sorts cancel them) | Confidence recovers above 0.85. **Rule resumes firing** — next classification uses rule, not LLM. Tests the bidirectional nature: confidence can go back up | *Deferred to unit test* (`test_recovery_rule_resumes_firing`) — requires two compute cycles with changing inputs |
| **L15** | Correction aging (30d window) | Correction older than `coherence_lookback_days` (30d) | Correction falls outside window. `net_corrections = 0`. Confidence recovers | *Deferred to unit test* (`test_correction_aging_outside_window`) |
| **L16** | Staleness dead zone recovery via `last_relevant_at` | Stale rule that can't fire (below confidence gate), but user manually sorts matching email to target folder | `last_relevant_at` updated from audit_log (user sort counts). Staleness resets to 1.0. Confidence recovers. Rule may resume firing if other factors are healthy | *Deferred to unit test* (`test_staleness_recovery_via_last_relevant_at`) |
| **L17** | Manual rule exemption from computed confidence | Test config includes `manual_rules` entry for `admin@lincolnelementary.org` → Children. Bootstrap creates this rule with `source='manual'`. After all runs (with corrections and confidence recomputation) | `source='manual'` rule is skipped by `compute_rule_confidence()`. Confidence = 1.0 (unchanged from creation) regardless of coherence, staleness, or corrections affecting other rules | System test: verify manual rule confidence unchanged after all Phase 4 runs |
| **L18** | Deactivation at threshold (0.50) | Rule with very low coherence and corrections | Confidence drops below 0.50 → `active=0`. Rule no longer considered at classification time | *Deferred to unit test* (`test_deactivation_at_threshold`) |
| **L18a** | Full deactivation → reactivation cycle | Rule deactivated (L18). New evidence: user manually sorts 3+ emails matching the condition to the target folder | `maybe_create_rule` finds inactive rule via `find_rule_any_status`. Reactivates with confidence from `BaseConfidenceConfig`. `compute_rule_confidence` runs — if corrections aged out and coherence recovered, rule stays active and **resumes firing**. Tests the complete lifecycle: fire → deactivate → reactivate → fire | *Deferred to unit test* (`test_deactivation_reactivation_cycle`) — requires multi-step state manipulation |
| **L19** | Minimum sample guard (coherence_factor=1.0 when <3 emails) | Rule with <3 emails in lookback window | `coherence_factor = 1.0` (benefit of the doubt). Confidence not penalized by sparse data. New rule with 2 emails in window gets full `base × 1.0 × staleness` confidence | *Deferred to unit test* (`test_minimum_sample_guard`) |
| **L19a** | `last_relevant_at` NULL for new rule | Brand new rule (just created by `maybe_create_rule`) | `last_relevant_at` is NULL. `_compute_staleness` returns 1.0 (no staleness penalty). `compute_rule_confidence` doesn't crash and produces a valid confidence | *Deferred to unit test* (`test_new_rule_null_last_relevant_at`) |

#### Auto-Rule Generation from Learning

Rules created when manual sort evidence accumulates past thresholds.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L12** | Accumulated manual sorts create rule | 3+ manual sorts from same sender to same folder | New `exact_sender` rule auto-created | *Deferred to unit test* (`test_auto_rule_exact_sender`) — would need 3+ JMAP moves from same sender |

### 6.2 Test Execution Sequence

**Batch 1 — single correction + skipped sort:**

1. **JMAP moves** (simulate user actions):
   - L3: `noreply@chase.com` (E1, rule-moved) Banks → Stores
   - L4: `alerts@megastore.com` (R5c, rule-moved) Banks → INBOX
   - L5: `returns@megastore.com` (R5a, LLM-moved) Stores → Banks
   - L1: `info@ambiguous-service.com` (S6, skipped in inbox) INBOX → Banks
2. **Run `mailsort run`** — learning step detects moves
3. **Verify L1, L2a, L3, L4, L5** — audit rows, rule confidence, rule active status. L2a: no false manual rows for rule-moved emails that weren't corrected

**Dedup pass:**

4. **Run `mailsort run` again** (no new moves) — tests L6 dedup + L2c dedup
5. **Verify L6, L2c** — no double-counting corrections, no duplicate manual rows for L1

**Batch 2 — two more corrections ("3 strikes"):**

6. **JMAP moves** (2 more chase corrections):
   - L3a: `noreply@chase.com` (L3a-1, rule-moved) Banks → Children
   - L3a: `noreply@chase.com` (L3a-2, rule-moved) Banks → Stores
7. **Run `mailsort run`** — detects 2 more corrections for chase rule
8. **Verify L3a** — 3 total corrections, chase confidence ≈ 0.78, below `rule_move`, stays `active=1`

**Sort-back recovery:**

9. **JMAP move** (confirming sort):
   - L14: `noreply@chase.com` (L3a-1, previously corrected to Children) Children → Banks
10. **Run `mailsort run`** — detects confirming sort
11. **Verify L14** — net corrections = 2 (3 corrections − 1 confirming), confidence partially recovers

**Manual rule exemption (verified across all runs):**

12. **Verify L17** — manual rule for `admin@lincolnelementary.org` → Children has `confidence=1.0` and `source='manual'` unchanged after all runs

### 6.3 Learning Verification Checklist

**Batch 1 verification (steps 2–3):**

- [x] **L1**: manual audit row for `ambiguous-service.com` → Banks
- [x] **L2a**: NO false manual rows for rule-moved emails (chase E1 has `moved=1` from live run; NOT reported as skipped sort)
- [x] **L3**: correction audit row for `chase.com` → Stores (`classification_source='correction'`, `rule_id`=chase rule)
- [x] **L3**: chase rule confidence dropped from original (computed confidence model)
- [x] **L3**: chase rule `active=1` (confidence > `deactivation_threshold` 0.50)
- [x] **L4**: NO manual row for `megastore alerts` inbox return
- [x] **L4**: megastore alerts rule confidence unchanged, still active
- [x] **L5**: correction audit row for `megastore returns` → Banks
- [x] **L5**: no rules changed confidence significantly (except chase from L3)

**Dedup verification (steps 4–5):**

- [x] **L6**: no new correction rows for chase on second run (`_already_handled_email_ids` — most recent correction newer than most recent rule move)
- [x] **L6**: chase rule confidence reduced from original (idempotent on re-run)
- [x] **L9**: chase rule confidence below `rule_move` (0.85) — would fall through to LLM

**Batch 2 verification (steps 7–8):**

- [x] **L3a**: 3 total correction rows for chase rule (`classification_source='correction'`, `rule_id`=chase rule)
- [x] **L3a**: chase confidence ≈ 0.54 (3 net corrections penalty). **Below `rule_move` (0.85)** — rule stops firing
- [x] **L3a**: chase rule stays `active=1` (0.54 > `deactivation_threshold` 0.50)
- [x] **L9**: chase rule won't fire — confidence below `rule_move` threshold

**Sort-back verification (steps 10–11):**

- [x] **L14**: confirming sort audit row for `noreply@chase.com` → Banks (`classification_source='manual'`, detected by Cat 2b)
- [x] **L14**: chase confidence recovers from 0.54 → 0.61 (net corrections = 2, partial recovery)

**Manual rule exemption (step 12):**

- [x] **L17**: manual rule for `admin@lincolnelementary.org` → Children: `confidence=1.0`, `source='manual'`, unchanged after all runs

**Deferred to unit test:**

- [ ] **L3b** *(unit test)*: corrections + low coherence → confidence < 0.50 → **deactivated** (`active=0`)
- [ ] **L6a** *(unit test)*: re-correction creates second correction row after new rule move
- [ ] **L9** *(unit test)*: rule with confidence in 0.50–0.85 range → LLM classifies (not rule), rule stays `active=1`
- [ ] **L10** *(unit test)*: rule at exactly 0.85 fires; at 0.8499 does not
- [ ] **L10a** *(unit test)*: rule at exactly 0.50 stays active; at 0.4999 deactivated
- [ ] **L13a** *(unit test)*: coherence ≈ 0.50 alone → confidence 0.475 → deactivated (no corrections needed)
- [ ] **L14a** *(unit test)*: corrections age out → confidence recovers above 0.85 → rule **resumes firing**
- [ ] **L18a** *(unit test)*: deactivated rule reactivated by `maybe_create_rule` → fires again after confidence computation

---

## 8. Cross-Cutting Edge Cases

Interactions that span multiple subsystems or phases. Most are exercised by
the phase-specific tests above; this section provides traceability.

### 8.1 Classification Priority Chain

The pipeline resolves classification via thread → rules → LLM. Each tier
interacts with data produced by other phases (bootstrap evidence, learning
corrections, contact imports).

| ID | Scenario | What It Tests | Tested By |
|----|----------|---------------|-----------|
| **X1** | Known contact with rule — rule wins over LLM threshold | Rule fires before LLM; known-contact threshold (0.93) is irrelevant when a rule matches | Phase 2/3: C1 (`testfriend@gmail.com`) |
| **X2** | Split-domain senders: per-address routing | `exact_sender` rules route individual addresses correctly; unruled addresses at the same domain fall to LLM | Phase 2/3: R5a/R5b/R5c (`@megastore.com`) |
| **X3** | Thread context overrides rule/LLM | Thread sibling match takes priority over all other classification tiers | Phase 2/3: S4 (`rare@oneoff.com` with In-Reply-To) |
| **X4** | Multiple rules coexist, priority determines which fires | list_id > exact_sender > sender_domain at classification time | Phase 2/3: P1 (`statements@bigbank.com`), P2 (`activities@ymca.org`) |

### 8.2 Lifecycle Interactions

Behaviors that depend on state accumulated across multiple phases.

| ID | Scenario | What It Tests | Tested By |
|----|----------|---------------|-----------|
| **X5** | Bootstrap idempotency end-to-end | Running bootstrap twice produces identical rules, descriptions, and evidence | Phase 1: F5 (run bootstrap ×2, verify 0 new rows) |
| **X6** | Correction → low confidence → re-classification by LLM | Corrected email's rule loses confidence via computed model; if confidence drops below `rule_move`, sender falls to LLM on next run. Rule stays `active=1` | Phase 4: L3 (correction + computed confidence) + L9 (chase falls to LLM) |
| **X7** | Known contact below LLM threshold but above normal threshold | Contact email classified by LLM with confidence between 0.80–0.93 — blocked by stricter known-contact threshold | Phase 2: S8 (`testcontact@example.com`, `below_threshold_known_contact`) |

### 8.3 Error Handling & Edge Cases

| ID | Scenario | What It Tests | Tested By |
|----|----------|---------------|-----------|
| **X8** | LLM API error — per-email isolation | One email's LLM error doesn't prevent others from being classified and moved | Observed organically in Phase 3 (`shipment-tracking@amazon.com`). *Not reproducible on demand* — covered by unit test (`test_no_classification_logs_skip`) |
| **X9** | Folder deletion → rule deactivation → unknown_folder skip | Delete a folder after bootstrap; `reconcile_folders` deactivates its rules; emails targeting it get `skip_reason=unknown_folder` | *Deferred to integration test* (`test_deleted_folder_rule_deactivated_on_run`, `test_deleted_folder_email_gets_unknown_folder_skip`) — destructive JMAP operation, better with mocks |
| **X10** | skip_senders filtering (no audit row) | Email from `skip_senders` is filtered before classification — no audit_log row at all (unlike LLM skip which gets a row) | *Deferred to unit test* (`test_skip_sender_is_filtered`) — requires config change mid-test |
| **X11** | Inbox snapshot scope vs batch scope | Snapshot covers all inbox emails (up to 500); classification covers only `max_batch_size`. Emails beyond the batch can still be detected as departures | *Deferred to integration test* (`test_snapshot_captures_beyond_batch_for_departure_detection`) — uses `max_batch_size=3` to test with 5 emails |
| **X12** | Dry run still runs learning step | Dry run detects corrections from previous live runs and runs `compute_rule_confidence()` — recomputing confidence from live state | *Deferred to integration test* (`test_dry_run_detects_corrections_and_computes_confidence`) — verifies correction detection + computed confidence in `dry_run=True` mode |
| **X13** | JMAP move fails (read-only token) — entries show `move_failed`, run status `error` | `skip_reason='move_failed'` set on planned entries when move raises; run finishes as `'error'` not `'completed'`; UI shows "move failed" not "dry run" | *Deferred to unit test* (`test_move_exception_sets_move_failed_and_error_status`) — requires injecting JMAP error |
| **X14** | Scheduler fires exactly one initial run | No duplicate runs within the first interval window after `scheduler.start()` | *Deferred to unit test* — scheduler timer mechanics, not classification logic |
| **X15** | Email deleted between query and fetch (W1) | Email ID returned by `query_inbox_emails` but deleted before `get_emails` | `get_emails` returns fewer emails than IDs requested; orchestrator processes only returned ones, no crash, no orphan audit row | *Deferred to unit test* (`test_email_vanishes_between_query_and_fetch`) — requires mock JMAP returning subset of requested IDs |
| **X16** | Partial move success (W2) | `move_emails` returns `{a: True, b: False}` — one email moved, one failed | Audit log correctly records `moved=1` for success, `moved=0` for failure; `emails_moved` count reflects only successes | *Deferred to unit test* (`test_partial_move_success_records_mixed_outcomes`) — requires mock returning mixed outcomes |
| **X17** | Email absent from move response (W2) | `move_emails` returns `{a: True}` but email `b` is missing from response entirely | `b` recorded as `moved=0` (outcomes.get default); no crash | *Deferred to unit test* (`test_move_response_missing_email_records_not_moved`) — requires mock omitting an email from response |
| **X18** | Read-only token auto-downgrades to dry run | `mailsort run` with read-only JMAP token | Run completes as dry run (no `move_failed` errors, no wasted move attempts). `RunResult.read_only_downgrade=True`. CLI output indicates "read-only" downgrade | *Deferred to unit test* (`test_read_only_token_auto_downgrades_to_dry_run`) — requires mock `is_read_only`. **Observable in system test** when token is read-only: Phase 3 dry-run should show downgrade warning in logs, not `move_failed` |
| **X19** | Concurrent live run prevented by lock | Two `mailsort run` invocations on the same database | Second run returns immediately with "another live run in progress" warning; no duplicate audit entries | *Deferred to unit test* (`test_live_run_acquires_lock`) — requires spawning concurrent processes on same DB |
| **X20** | Dry run bypasses lock | `mailsort dry-run` while a live run holds the lock | Dry run proceeds normally, no blocking | *Deferred to unit test* (`test_dry_run_does_not_acquire_lock`) — requires concurrent process orchestration |
| **X21** | Correction dedup allows re-correction after new rule move | Rule moves email to Banks → user corrects to Stores → email returns to inbox → rule moves to Banks again → user corrects to Stores again | Second correction row created (dedup sees new rule move is newer than previous correction). Two corrections counted by `compute_rule_confidence()`. Tests the `_already_handled_email_ids` fix for move-correct-move-correct cycles | *Deferred to unit test* (`test_re_correction_after_new_rule_move`) — requires multi-step audit_log sequence |
| **X22** | Cache invalidation on folder description change | Regenerate descriptions (e.g., add a folder) → `classification_version` changes → all LLM cache misses on next run → fresh API calls. `cached=0` for all LLM rows | *Deferred to unit test* (`test_cache_invalidated_on_description_change`) — requires modifying folder descriptions between runs |
| **X23** | Cache invalidation on LLM model change | Change `llm_model` config between runs → version hash changes → cache misses, `cached=0` | *Deferred to unit test* (`test_cache_invalidated_on_model_change`) — requires config change between runs |
| **X24** | Cache hit preserves confidence gate behavior | Cached LLM result with confidence below threshold → still skipped (`below_threshold`). Cached result above threshold → still moved. Cache reuse doesn't bypass the confidence gate | *Deferred to unit test* (`test_cached_result_still_applies_confidence_gate`) — verifiable by checking `should_move` matches expectation for cached row |
| **X25** | New rule supersedes cached LLM result | Email was LLM-classified last run. A rule is created that matches it. Next run: rule fires (`source='rule'`), LLM cache never consulted | *Deferred to unit test* (`test_new_rule_supersedes_llm_cache`) — requires adding rule between runs |
| **X26** | `audit_log.cached` defaults to 0 for non-LLM sources | Thread and rule classifications always have `cached=0` | Verified by existing scenarios (S1–S4) — add assertion to Phase 2/3 verification checklist |

---

## 9. What This Validates That Unit/Integration Tests Don't

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

## 10. Risks and Mitigations

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
- **Duplicate runs**: if the scheduler fires the initial run twice (manual call +
  APScheduler catchup), test results may include duplicate audit entries. Fixed by
  removing the manual `_scheduled_run` call; verified by unit test
