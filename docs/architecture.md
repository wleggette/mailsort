# Mailsort — System Architecture

High-level system architecture: component diagrams, bootstrap sequence, and per-run sequence. For detailed subsystem design, see the [design/](design/) directory.

## Table of Contents

1. [Functional Component Diagram](#functional-component-diagram)
2. [Bootstrap Sequence](#bootstrap-sequence)
3. [Per-Run Sequence](#per-run-sequence)
4. [Project Structure](#project-structure)

---

## Functional Component Diagram

Static view of modules, responsibilities, and call dependencies. Arrows
show "calls / depends on". Modules are grouped by layer.

```
┌─────────────────────────────────────────────────────────────────────┐
│  EXTERNAL                                                           │
│                                                                     │
│  ┌──────────────────────────────┐  ┌─────────────────────────────┐  │
│  │     Fastmail JMAP API        │  │     Anthropic LLM API       │  │
│  │  Mailbox/get · Email/query   │  │  Claude Haiku               │  │
│  │  Email/get · Email/set       │  │  Classification + folder    │  │
│  │  ContactCard/get · Thread/get│  │  description generation     │  │
│  └──────────────┬───────────────┘  └──────────────┬──────────────┘  │
└─────────────────┼──────────────────────────────────┼────────────────┘
                  │                                  │
┌─────────────────┼──────────────────────────────────┼────────────────┐
│  JMAP LAYER     │                                  │                │
│                 ▼                                  │                │
│  ┌──────────────────────────────┐                  │                │
│  │     JMAP Client              │                  │                │
│  │     jmap/client.py           │                  │                │
│  │  Session, auth, method calls │                  │                │
│  │  Batch moves, thread lookup  │                  │                │
│  └──────────────┬───────────────┘                  │                │
│                 │                                  │                │
│  ┌──────────────▼───────────────┐                  │                │
│  │     Mailbox Tree             │                  │                │
│  │     jmap/mailbox_tree.py     │                  │                │
│  │  Folder path ↔ ID resolution │                  │                │
│  │  Excluded folder filtering   │                  │                │
│  └──────────────────────────────┘                  │                │
└────────────────────────────────────────────────────┼────────────────┘
                                                     │
┌────────────────────────────────────────────────────┼────────────────┐
│  CLASSIFICATION LAYER                              │                │
│                                                    │                │
│  ┌──────────────────────────────┐                  │                │
│  │     Feature Extractor        │                  │                │
│  │     classifier/features.py   │                  │                │
│  │  Email → EmailFeatures       │                  │                │
│  │  Contact refresh (daily)     │                  │                │
│  └──────────────┬───────────────┘                  │                │
│                 │                                  │                │
│  ┌──────────────▼───────────────┐                  │                │
│  │     Classification Pipeline  │                  │                │
│  │     classifier/pipeline.py   │                  │                │
│  │  Orchestrates: thread →      │                  │                │
│  │  rules → LLM. First match    │                  │                │
│  │  wins.                       │                  │                │
│  └──┬────────────┬──────────────┘                  │                │
│     │            │                                 │                │
│     ▼            ▼                                 ▼                │
│  ┌────────────┐  ┌──────────────────────────────────────────────┐   │
│  │ Rule Engine│  │     LLM Classifier                           │   │
│  │ classifier/│  │     classifier/llm.py                        │   │
│  │ rules.py   │  │  Privacy gates: llm_skip_senders/domains     │   │
│  │            │  │  Contact enrichment in prompt                │   │
│  │ list_id >  │  │  Structured JSON response parsing            │   │
│  │ exact >    │  └──────────────────────────────────────────────┘   │
│  │ domain >   │                                                     │
│  │ regex      │  ┌──────────────────────────────────────────────┐   │
│  │            │  │     Folder Descriptions                      │   │
│  │ hit_count  │  │     classifier/descriptions.py               │   │
│  │ (live only)│  │  LLM-generated or fallback per folder        │   │
│  └────────────┘  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  DECISION LAYER                                                     │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │     Move Decision Builder              mover/mover.py        │   │
│  │                                                              │   │
│  │  Confidence Gate          │  Eligibility Gates               │   │
│  │  rule: ≥0.85              │  unread  → skip                  │   │
│  │  llm:  ≥0.80              │  flagged → skip                  │   │
│  │  llm+contact: ≥0.93       │  too_new → skip                  │   │
│  │  thread: bypass           │  unknown_folder → skip            │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  LEARNING LAYER                                                     │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │     Learner                            audit/learner.py      │   │
│  │                                                              │   │
│  │  Manual sort detection (4 categories):                       │   │
│  │    Cat 1: skipped emails user moved from inbox               │   │
│  │    Cat 2: mailsort-moved emails user relocated               │   │
│  │    Cat 3: inbox departures (snapshot diff)                   │   │
│  │    Cat 4: daily folder scan                                  │   │
│  │                                                              │   │
│  │  Auto-rule generation:                                       │   │
│  │    Create all eligible: list_id + sender_domain + exact      │   │
│  │                                                              │   │
│  │  Confidence adjustment:                                      │   │
│  │    Correction penalty: −0.15 per correction                  │   │
│  │    Staleness decay: −0.10 after 90 days without a hit        │   │
│  └───────────────────────────┬──────────────────────────────────┘   │
│                              │                                      │
│  ┌───────────────────────────▼──────────────────────────────────┐   │
│  │     Audit Writer                       audit/writer.py       │   │
│  │  Run lifecycle: start_run → log_decisions → finish_run       │   │
│  │  Per-email: classification + outcome + skip_reason           │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  ENTRY POINTS                                                       │
│                                                                     │
│  ┌────────────────┐ ┌────────────────┐ ┌─────────────────────────┐  │
│  │  Orchestrator   │ │  Bootstrap     │ │  Scheduler              │  │
│  │  orchestrator.py│ │  bootstrap.py  │ │  scheduler.py           │  │
│  │                 │ │                │ │                         │  │
│  │  Wires a single │ │  One-time seed:│ │  APScheduler timer      │  │
│  │  classify+move  │ │  evidence,     │ │  Triggers orchestrator  │  │
│  │  pass. Calls    │ │  rules, desc,  │ │  every N minutes.       │  │
│  │  learner, then  │ │  contacts.     │ │  max_instances=1        │  │
│  │  pipeline, then │ │  Uses learner  │ │  + flock before JMAP    │  │
│  │  mover.         │ │  for rule eval.│ │  + health check :8025   │  │
│  │                 │ │                │ │  + web UI :8080         │  │
│  │  _acquire_run_  │ │                │ │                         │  │
│  │  lock / _release│ │                │ │                         │  │
│  └────────────────┘ └────────────────┘ └─────────────────────────┘  │
│                                                                     │
│  ┌────────────────┐ ┌──────────────────────────────────────────┐    │
│  │  CLI            │ │  Web UI                                  │    │
│  │  main.py        │ │  web/                                    │    │
│  │                 │ │  Dashboard, audit log, rules, contacts,  │    │
│  │  bootstrap,     │ │  folders, settings views.                │    │
│  │  dry-run, run,  │ │  Read-only monitoring + manual rule      │    │
│  │  analyze,       │ │  creation/toggle.                        │    │
│  │  check-config,  │ │                                          │    │
│  │  export-rules   │ │                                          │    │
│  │                 │ │                                          │    │
│  │  Docker deleg:  │ │                                          │    │
│  │  run/dry-run →  │ │                                          │    │
│  │  docker exec if │ │                                          │    │
│  │  container up   │ │                                          │    │
│  └────────────────┘ └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  INFRASTRUCTURE                                                     │
│                                                                     │
│  ┌────────────────────────────┐  ┌───────────────────────────────┐  │
│  │  Database                  │  │  Config                       │  │
│  │  db/database.py            │  │  config.py                    │  │
│  │  db/migrations.py          │  │  Pydantic model from YAML    │  │
│  │  SQLite: rules, audit_log, │  │  Secrets from env vars       │  │
│  │  runs, contacts,           │  │                               │  │
│  │  folder_descriptions,      │  │                               │  │
│  │  inbox_snapshot,           │  │                               │  │
│  │  learner_state             │  │                               │  │
│  └────────────────────────────┘  └───────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Bootstrap Sequence

One-time initialization run via `mailsort bootstrap`. Seeds the database
with evidence, rules, descriptions, and contacts from existing folders.

```
┌─ Phase 1: Collect Evidence (per folder) ─────────────────────────────┐
│  (pre: reconcile_folders — deactivate rules for deleted folders)     │
│  Input:    Fastmail folders + up to 50 most recent emails each       │
│  Module:   bootstrap.py → jmap/client.py → classifier/features.py    │
│  Output:   audit_log rows with classification_source='manual'        │
│  Decisions:                                                          │
│    • Skip system folders (role = trash/junk/sent/drafts)             │
│    • Skip excluded folder patterns (config)                          │
│    • Cap at max_per_folder (default 50, most recent by receivedAt)   │
│    • Skip already-known email_ids on re-run (idempotency)            │
│  System Tests:         F1–F7, EF1–EF9                                │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Phase 2: Generate Folder Descriptions ──────────────────────────────┐
│  (in code: done per-folder inline during Phase 1)                    │
│  Input:    Folder path + sample emails from Phase 1                  │
│  Module:   classifier/descriptions.py → Anthropic API (optional)     │
│  Output:   folder_descriptions table rows                            │
│  Decisions:                                                          │
│    • Config override present? → use override, skip LLM               │
│    • Description already exists? → keep existing, skip               │
│    • LLM available? → call with FOLDER_DESCRIPTION_PROMPT            │
│    • LLM unavailable? → fallback: "Emails filed under {leaf_name}"  │
│    • Empty folder (0 emails)? → always fallback                      │
│  System Tests:         D1–D7                                         │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Phase 3: Create Rules from Evidence ────────────────────────────────┐
│  Input:    audit_log evidence (distinct sender/domain/list-id/folder │
│            combinations)                                             │
│  Module:   bootstrap.py → audit/learner.py → classifier/rules.py     │
│  Output:   rules table rows (active=1, source='auto')                │
│  Decisions:                                                          │
│    • All eligible rule types created independently per sender:       │
│      - list_id:       ≥2 emails to target, coherence ≥80%           │
│      - sender_domain: ≥5 emails, ≥3 distinct senders, coh ≥80%     │
│      - exact_sender:  ≥3 emails to target, coherence ≥80%           │
│    • Skip if rule already exists (find_existing_rule)                │
│    • Skip evidence pointing to deleted folders                       │
│  System Tests:         LR1–LR4, DR1–DR6, ER1–ER8, P1–P3, CA1–CA3    │
│  CI Tests: deleted folder filtering (test_bootstrap.py)            │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Phase 4: Import Contacts ───────────────────────────────────────────┐
│  Input:    Fastmail ContactCard/get + config known_contact_overrides  │
│  Module:   classifier/features.py → jmap/client.py                   │
│  Output:   contacts table rows                                       │
│  Decisions:                                                          │
│    • Contacts scope unavailable? → skip gracefully, log warning      │
│    • Config overrides merged (adds relationship hints, extra addrs)  │
│    • Per-contact error isolation (one bad record doesn't block rest)  │
│  System Tests:         CI1–CI5                                       │
│  CI Tests: error isolation (test_contacts.py)                        │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ Phase 5: Coverage Check (read-only: summary/reporting) ─────────────┐
│  Input:    All audit_log evidence rows + created rules                │
│  Module:   bootstrap.py → classifier/rules.py                        │
│  Output:   Coverage percentage in bootstrap report                   │
│            (% of evidence emails that would match a rule)            │
│  Decisions:                                                          │
│    • Per evidence email: run classify() — match or no-match          │
│    • Exclude evidence for deleted folders                            │
│    • No state changes — purely diagnostic                            │
│  System Tests:         §3.6 checklist (coverage report)              │
│  CI Tests: calculation accuracy (test_bootstrap.py)                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Per-Run Sequence

Triggered by `mailsort run` (live) or `mailsort dry-run`. The scheduler
calls the same path with `dry_run=False` on a timer.

```
  [CLI]
        │
        ◇ Docker container running?
        yes → docker exec mailsort mailsort run/dry-run
        │     (all runs happen inside the same kernel)
        no ▼
  [CLI / Scheduler]
        │
        ◇ dry_run?
        no → acquire flock(data/mailsort.run.lock)
        │    fail? → "Another live run in progress" → exit 1
        yes → skip lock
        │
        │  run_classification_pass(dry_run=T/F)
        ▼
  ┌─ Step 1: Pre-work ────────────────────────────────┐
  │  (pre: start_run() → run_id)                      │
  │  • reconcile_folders — deactivate stale rules     │
  │  • detect_manual_sorts (Cat 1-3):                 │
  │      Cat 1: skipped emails user moved from inbox  │
  │      Cat 2: mailsort-moved emails user relocated  │
  │      Cat 3: inbox departures (snapshot diff)      │
  │    → log as manual + maybe_create_rule()          │
  │  • scan_folders_for_unknown_sorts (Cat 4, daily)  │
  │    → log as manual + maybe_create_rule()          │
  │  • refresh_contacts (if stale, daily)             │
  │  • generate_descriptions (new folders only)       │
  └───────────────────────┬───────────────────────────┘
                          ▼
  ┌─ Step 2: Fetch Inbox ─────────────────────────────┐
  │  • query ALL inbox email IDs (no filters)         │
  │  • save_inbox_snapshot (for next run's Cat 3)     │
  │  • get_emails — fetch metadata for batch          │
  └───────────────────────┬───────────────────────────┘
                          ▼
  ┌─ Step 3: Filter + Extract ────────────────────────┐
  │  • remove skip_senders                            │
  │  • extract_features → EmailFeatures[]             │
  └───────────────────────┬───────────────────────────┘
                          ▼
          ┌───── per email ─────┐
          ▼                     │
  ┌─ Step 4: Classify ─────────────────────────────────────────────┐
  │                                                                │
  │  ◇ Thread context?                                             │
  │  │  audit_log: sibling in same thread already sorted?          │
  │  │  JMAP fallback: thread sibling in non-inbox folder?         │
  │  yes → Classification(source="thread")                         │
  │  │                                                             │
  │  no ▼                                                          │
  │  ◇ Rule match?                                                 │
  │  │  Try in order: list_id → exact_sender → sender_domain       │
  │  │  → subject_regex. First match above threshold wins.         │
  │  │  (hit_count updated only if live run)                       │
  │  yes → Classification(source="rule", rule_id=N)                │
  │  │                                                             │
  │  no ▼                                                          │
  │  ◇ LLM available?                                              │
  │  no → skip_reason="llm_unavailable"                            │
  │  │                                                             │
  │  yes ▼                                                         │
  │  ◇ Privacy gate?                                               │
  │  │  llm_skip_senders? llm_skip_domains?                        │
  │  │  known contact + llm_allow_known_contacts=false?            │
  │  blocked → skip_reason="llm_skip_*"                            │
  │  │                                                             │
  │  allowed ▼                                                     │
  │  Call LLM (with contact enrichment if known)                   │
  │  → Classification(source="llm", confidence=N)                  │
  │  │  (api_error → skip_reason="llm_api_error")                  │
  └────────────────────────────────────────────────────────────────┘
          │
          ▼
  ┌─ Step 5: Build Move Decision (per email) ──────────────────────┐
  │                                                                │
  │  ◇ Confidence gate                                             │
  │  │  rule:          confidence ≥ 0.85?                          │
  │  │  llm:           confidence ≥ 0.80?                          │
  │  │  llm + contact: confidence ≥ 0.93?                          │
  │  │  thread:        always passes                               │
  │  no → skip_reason="below_threshold" (or "…_known_contact")    │
  │  │                                                             │
  │  yes ▼                                                         │
  │  ◇ Eligibility gates                                           │
  │  │  $seen absent?       → skip_reason="unread"                 │
  │  │  $flagged present?   → skip_reason="flagged"                │
  │  │  receivedAt too new? → skip_reason="too_new"                │
  │  │  folder not in tree? → skip_reason="unknown_folder"         │
  │  any fail → should_move=false                                  │
  │  │                                                             │
  │  all pass ▼                                                    │
  │  MoveDecision(should_move=true)                                │
  └────────────────────────────────────────────────────────────────┘
          │
          └───── end per email ─┘
                          │
                          ▼
  ┌─ Step 6: Execute Moves ───────────────────────────┐
  │                                                   │
  │  ◇ dry_run?                                       │
  │  yes → skip JMAP call, log "would move" count     │
  │  │                                                │
  │  no ▼                                             │
  │  Batch Email/set: remove inbox, add target folder │
  │  Tag with $mailsort-moved keyword                 │
  │  Per-email outcome: moved or move_failed          │
  └───────────────────────┬───────────────────────────┘
                          ▼
  ┌─ Step 7: Log + Finish ────────────────────────────┐
  │  • log_decisions — every email to audit_log       │
  │    (classification + outcome + skip_reason)       │
  │  • finish_run(status, emails_seen, emails_moved)  │
  └───────────────────────────────────────────────────┘
```

---

## Project Structure

```
~/Workspace/mailsort/
├── docs/                           ← Documentation (you are here)
│   ├── prd.md                      ← Product requirements
│   ├── architecture.md             ← This document
│   ├── design/                     ← Detailed subsystem design
│   │   ├── jmap-integration.md
│   │   ├── classification.md
│   │   ├── learning.md
│   │   ├── audit.md
│   │   ├── data-models.md
│   │   └── web-ui.md
│   ├── configuration.md            ← Config reference
│   ├── operations.md               ← Docker, deployment, monitoring
│   ├── planning/                   ← Living/evolving docs
│   │   ├── phases.md
│   │   ├── open-questions.md
│   │   └── system-test-plan.md
│   └── dev/                        ← Development working area
│       ├── changelog.md
│       ├── design-ideas.md
│       └── scratch.md
│
├── src/
│   └── mailsort/
│       ├── __init__.py
│       ├── main.py              ← Entry point, CLI commands
│       ├── config.py            ← Config loading & validation (Pydantic)
│       ├── orchestrator.py      ← Run orchestrator: classification pass + move
│       ├── bootstrap.py         ← Bootstrap: scan folders, seed evidence, create rules
│       ├── scheduler.py         ← APScheduler setup for periodic runs
│       ├── health.py            ← Health check endpoint
│       │
│       ├── jmap/
│       │   ├── client.py        ← JMAP HTTP client (session, auth, method calls)
│       │   ├── models.py        ← Pydantic models for JMAP objects (Email, Mailbox)
│       │   └── mailbox_tree.py  ← Mailbox tree builder & path resolver
│       │
│       ├── classifier/
│       │   ├── pipeline.py      ← Tiered classification: thread → rules → LLM
│       │   ├── features.py      ← Feature extraction + contacts cache refresh
│       │   ├── rules.py         ← Rule engine (SQLite-backed)
│       │   ├── llm.py           ← LLM classifier (Anthropic API)
│       │   └── descriptions.py  ← Auto-generate folder descriptions via LLM
│       │
│       ├── mover/
│       │   └── mover.py         ← Confidence gate + move decision builder
│       │
│       ├── audit/
│       │   ├── writer.py        ← Audit log writer (runs + audit_log tables)
│       │   └── learner.py       ← Manual sort detection + auto-rule generation
│       │
│       └── db/
│           ├── database.py      ← SQLite connection management
│           └── migrations.py    ← Schema creation & migrations
│
├── tests/
│   ├── conftest.py              ← Shared fixtures (in-memory DB, sample data)
│   ├── test_*.py                ← Unit and integration tests
│   ├── fixtures/
│   │   ├── sample_emails.json
│   │   └── sample_mailboxes.json
│   └── system/                  ← End-to-end tests against real Fastmail
│       ├── config.test.yaml
│       ├── run_system_test.py
│       ├── load_fixtures.py
│       ├── generate_inbox_emails.py
│       ├── verify_results.py
│       └── fixtures/
│           └── folder_emails.json
│
├── scripts/                     ← Ad-hoc analysis scripts
│   └── analyze_list_unsubscribe.py
│
└── data/                        ← Docker volume mount point
    ├── mailsort.db              ← SQLite database (rules + audit log)
    ├── mailsort.run.lock        ← flock-based exclusive run lock (auto-created)
    └── mailsort.log             ← Application log
```
