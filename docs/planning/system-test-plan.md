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
- [6. Phase 4: Learning](#6-phase-4-learning)
  - [6.1 User Correction Simulation](#61-user-correction-simulation)
  - [6.2 Learning Verification Checklist](#62-learning-verification-checklist)
- [7. Phase 5: Feedback Loop](#7-phase-5-feedback-loop)
  - [7.1 Rule Confidence Penalty](#71-rule-confidence-penalty)
  - [7.2 Feedback Verification Checklist](#72-feedback-verification-checklist)
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
| **CA1** | Confidence by rule type | `list_id`: 0.95, `sender_domain`: min(0.90, 0.75 + n×0.02), `exact_sender`: min(0.95, 0.80 + n×0.03) | Confidence matches formula for rule type | All rules from Groups A–J |
| **CA2** | Rule type stored | `rule_type` column matches: `list_id`, `sender_domain`, or `exact_sender` | Correct rule type recorded | All rules from Groups A–J |
| **CA3** | Target folder stored | `target_folder` matches the dominant folder from evidence | Correct target | All rules from Groups A–J |

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
- [ ] **Confidence values**: rule confidence matches the formula for each rule type (CA1–CA3)
- [ ] **Hit counts unchanged**: all rules have `hit_count=0` and `last_hit_at IS NULL` after bootstrap (coverage check is read-only, must not record hits)
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

Pre-classification filters determine whether an email is even considered for
classification. These are checked before the classification pipeline runs.

| ID | Scenario | Keywords | receivedAt | Expected Outcome | Tested By |
|----|----------|----------|------------|-----------------|----------|
| **E1** | Read, unflagged, old enough | `$seen` | 5h ago | classified (moved or below_threshold) | Inbox gen: E1 (`noreply@chase.com`) |
| **E2** | Unread | (none) | 5h ago | skip_reason=unread | Inbox gen: E2 (`orders@amazon.com`) |
| **E3** | Read + flagged | `$seen`, `$flagged` | 5h ago | skip_reason=flagged | Inbox gen: E3 (`noreply@chase.com`) |
| **E4** | Read, unflagged, too new | `$seen` | now | skip_reason=too_new | Inbox gen: E4 (`alerts@bankofamerica.com`) |
| **E5** | Unread + flagged + new | `$flagged` | now | skip_reason=unread (checked first) | Inbox gen: E5 (`noreply@target.com`) |

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

### 4.3 No-LLM Dry Run Verification Checklist

The no-LLM dry run temporarily unsets `ANTHROPIC_API_KEY` and runs
`mailsort dry-run` before the full run. Verify:

- [ ] **Rule/thread matches work**: emails matching rules or thread context are classified normally (`classification_source='rule'` or `'thread'`)
- [ ] **LLM-dependent emails skipped**: emails without rule/thread match have `skip_reason='llm_unavailable'`
- [ ] **C5 specifically**: `random@unknown.com` has `skip_reason='llm_unavailable'`
- [ ] **No crash**: graceful degradation, no unhandled exceptions

### 4.4 Dry Run Verification Checklist

- [ ] **Run record created**: `runs` table has a row with `status='completed'` and `emails_moved=0` for the dry-run `run_id`
- [ ] **Hit counts unchanged**: all rules have `hit_count=0` and `last_hit_at IS NULL` after dry run (dry run does not record hits)
- [ ] **Audit log populated**: every inbox email has an `audit_log` row for each dry-run pass (query by `run_id`)
- [ ] **Classification sources correct**: rule, thread, llm, or none as expected
- [ ] **Skip reasons correct**: unread, flagged, too_new where expected
- [ ] **No emails moved**: all emails still in INBOX (dry run = read-only)
- [ ] **Rule matches use correct rule type**: exact_sender, sender_domain, or list_id
- [ ] **Classification priority — exact_sender over sender_domain**: `statements@bigbank.com` matches `exact_sender` rule (not `sender_domain` for `bigbank.com`)
- [ ] **Classification priority — list_id over exact_sender**: email with list-id `<updates.ymca.org>` from `activities@ymca.org` matches `list_id` rule (not `exact_sender`)
- [ ] **LLM called only when no rule/thread match**: classification_source=llm only for fallback cases

### 4.5 Dynamic Inbox Emails

A Python script (`tests/system/generate_inbox_emails.py`) creates inbox emails
with dynamic timestamps for testing all classification and eligibility scenarios
at runtime.

| Scenario | From | Keywords | receivedAt | Expected |
|----------|------|----------|------------|----------|
| E1: Rule match, eligible | `noreply@chase.com` | `$seen` | 5h ago | moved → Banks (rule) |
| E2: Rule match, unread | `orders@amazon.com` | (none) | 5h ago | unread |
| E3: Rule match, flagged | `noreply@chase.com` | `$seen`, `$flagged` | 5h ago | flagged |
| E4: Rule match, too new | `alerts@bankofamerica.com` | `$seen` | now | too_new |
| E5: Unread + flagged + new | `noreply@target.com` | `$flagged` | now | unread |
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

The generator uses `datetime.now(timezone.utc)` to produce `receivedAt`
timestamps relative to the current time, ensuring `too_new` scenarios work
regardless of when the test is run.

---

## 5. Phase 3: Live Move

Live run re-processes inbox emails and actually moves eligible ones via JMAP
`Email/set` with updated `mailboxIds`.

### 5.1 Age Gate Test

1. During dry run, one email was created with `receivedAt = now` → skip_reason=too_new
2. Wait for `min_age_minutes` (1 minute) to elapse
3. Run live: `mailsort run --config config.test.yaml`
4. Verify the previously-too-new email is now classified and moved
5. Verify unread/flagged emails remain in INBOX

### 5.2 Live Move Verification Checklist

- [ ] **Eligible emails moved**: emails with expected outcome "moved" are no longer in INBOX (verified via JMAP)
- [ ] **Correct target folders**: each moved email is in the folder predicted by its rule/LLM classification
- [ ] **Ineligible emails unchanged**: unread, flagged emails still in INBOX
- [ ] **audit_log updated**: `moved=1` for moved emails in this run
- [ ] **Age gate works**: previously-too-new email now moved after waiting
- [ ] **JMAP state consistent**: `Email/get` confirms new `mailboxIds` for moved emails

---

## 6. Phase 4: Learning & Feedback

Tests manual-sort detection (4 categories), correction penalty, dedup logic,
and behavioral impact of rule deactivation.

> **Note on current thresholds:** With `correction_penalty=0.15` and
> `rule_move=0.85`, every auto-created rule is deactivated after a single
> correction (max starting confidence is 0.95; 0.95 − 0.15 = 0.80 < 0.85).
> See `docs/dev/design-ideas.md` "Correction Penalty Tuning" for analysis.

### 6.1 Learning Scenarios

#### Category 1: Skipped Sorts

Emails mailsort left in inbox (`moved=0`) that the user subsequently moved
to a folder. Detected by `_detect_skipped_sorts`.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L1** | Skipped email sorted by user | Move `info@ambiguous-service.com` (`skip_reason=below_threshold`, still in inbox) from INBOX → Banks via JMAP | Manual audit row created for Banks. `from_inbox` count incremented | System test: JMAP move + `mailsort run` |
| **L2** | Skipped email still in inbox (negative) | Don't move any skipped emails | No false positives — no manual rows for emails still in inbox | System test: implicit (L1 run verifies no spurious detections) |

#### Category 2: Correction Sorts

Emails mailsort moved that the user relocated to a different folder.
Detected by `_detect_correction_sorts`. Penalizes originating rule.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L3** | Rule-based correction + penalty | Move `noreply@chase.com` (rule-moved, conf=0.95) from Banks → Stores via JMAP | Manual audit row for Stores. Chase rule penalized: 0.95 → 0.80. Rule deactivated (0.80 < 0.85) | System test: JMAP move + `mailsort run` |
| **L4** | Inbox return ignored (negative) | Move `alerts@megastore.com` (rule-moved) from Banks → INBOX via JMAP | **NOT** treated as correction (`new_path != "INBOX"` check). No manual row. Rule confidence unchanged | System test: JMAP move + `mailsort run` |
| **L5** | LLM-based correction, no rule penalty | Move `returns@megastore.com` (LLM-moved, `rule_id=NULL`) from Stores → Banks via JMAP | Manual audit row for Banks. `_penalize_rule(None)` exits early — no rule penalized | System test: JMAP move + `mailsort run` |
| **L6** | Dedup — same correction not double-counted | Run `mailsort run` again without new JMAP moves (after L3) | `_already_corrected_email_ids` filters chase email. No new manual row. Chase rule confidence still 0.80 | System test: second `mailsort run` |

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

#### Confidence Penalty & Feedback Loop

Rule confidence adjustments after corrections and staleness.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L9** | Deactivated rule stops matching | After L3 deactivates chase rule, run `mailsort run` again | Chase emails classified by LLM (not rule). `classification_source='llm'`, `rule_id=NULL` | System test: second `mailsort run` (same run as L6) |
| **L10** | Penalty boundary (conf exactly at threshold) | Rule with confidence=1.0 corrected → 0.85. Strict `<` comparison → rule stays active | Rule stays active (0.85 is not < 0.85) | *Deferred to unit test* (`test_correction_penalizes_originating_rule`) — no auto-created rule reaches 1.0 |
| **L11** | Staleness decay (90+ days without hit) | Rule with `last_hit_at` > 90 days ago | Confidence reduced by 0.10. Floor at 0.50 | *Deferred to unit test* (`test_confidence_decay_on_stale_rules`) — requires manipulating timestamps |

#### Auto-Rule Generation from Learning

Rules created when manual sort evidence accumulates past thresholds.

| ID | Scenario | Setup | Expected Behavior | Tested By |
|----|----------|-------|-------------------|-----------|
| **L12** | Accumulated manual sorts create rule | 3+ manual sorts from same sender to same folder | New `exact_sender` rule auto-created | *Deferred to unit test* (`test_auto_rule_exact_sender`) — would need 3+ JMAP moves from same sender |

### 6.2 Test Execution Sequence

1. **JMAP moves** (simulate user actions):
   - L3: `noreply@chase.com` Banks → Stores
   - L4: `alerts@megastore.com` Banks → INBOX
   - L5: `returns@megastore.com` Stores → Banks
   - L1: `info@ambiguous-service.com` INBOX → Banks
2. **Run `mailsort run`** — learning step detects moves
3. **Verify L1, L3, L4, L5** — audit rows, rule confidence, rule active status
4. **Run `mailsort run` again** (no new moves) — tests L6 dedup + L9 behavioral impact
5. **Verify L6, L9** — no double-counting, deactivated rule stops matching

### 6.3 Learning Verification Checklist

- [ ] **L1**: manual audit row for `ambiguous-service.com` → Banks
- [ ] **L3**: manual audit row for `chase.com` → Stores
- [ ] **L3**: chase rule confidence = 0.80 (was 0.95, penalized by 0.15)
- [ ] **L3**: chase rule `active=0` (0.80 < 0.85 threshold)
- [ ] **L4**: NO manual row for `megastore alerts` inbox return
- [ ] **L4**: megastore alerts rule confidence unchanged, still active
- [ ] **L5**: manual audit row for `megastore returns` → Banks
- [ ] **L5**: no rules changed confidence (except chase from L3)
- [ ] **L6**: no new manual rows for chase on second run (dedup)
- [ ] **L6**: chase rule confidence still 0.80 (no double penalty)
- [ ] **L9**: chase emails classified by LLM (`classification_source='llm'`)
- [ ] **L9**: chase emails have `rule_id=NULL` (deactivated rule not matched)

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
| **X6** | Correction → rule deactivation → re-classification by LLM | Corrected email's rule is deactivated; sender falls to LLM on next run | Phase 4: L3 (correction + penalty) + L9 (chase falls to LLM) |
| **X7** | Known contact below LLM threshold but above normal threshold | Contact email classified by LLM with confidence between 0.80–0.93 — blocked by stricter known-contact threshold | Phase 2: S8 (`testcontact@example.com`, `below_threshold_known_contact`) |

### 8.3 Error Handling & Edge Cases

| ID | Scenario | What It Tests | Tested By |
|----|----------|---------------|-----------|
| **X8** | LLM API error — per-email isolation | One email's LLM error doesn't prevent others from being classified and moved | Observed organically in Phase 3 (`shipment-tracking@amazon.com`). *Not reproducible on demand* — covered by unit test (`test_no_classification_logs_skip`) |
| **X9** | Folder deletion → rule deactivation → unknown_folder skip | Delete a folder after bootstrap; `reconcile_folders` deactivates its rules; emails targeting it get `skip_reason=unknown_folder` | *Deferred to integration test* (`test_deleted_folder_rule_deactivated_on_run`, `test_deleted_folder_email_gets_unknown_folder_skip`) — destructive JMAP operation, better with mocks |
| **X10** | skip_senders filtering (no audit row) | Email from `skip_senders` is filtered before classification — no audit_log row at all (unlike LLM skip which gets a row) | *Deferred to unit test* (`test_skip_sender_is_filtered`) — requires config change mid-test |
| **X11** | Inbox snapshot scope vs batch scope | Snapshot covers all inbox emails (up to 500); classification covers only `max_batch_size`. Emails beyond the batch can still be detected as departures | Tested implicitly — snapshot uses 500 limit, batch uses 250. Would need 500+ emails to observe truncation |
| **X12** | Dry run still runs learning step | Dry run detects corrections from previous live runs and adjusts rule confidence | Tested implicitly — learning step runs in all modes (`orchestrator.py` line 121) |

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
