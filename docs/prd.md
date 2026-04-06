# Mailsort — Product Requirements

## Overview

Mailsort is a self-hosted email classification service that periodically scans read, unflagged messages in a Fastmail inbox and moves them to the appropriate subfolder. It uses a tiered classification approach: deterministic rules handle known patterns, and an LLM classifier handles ambiguous cases. All decisions are logged for review, undo, and continuous learning.

## Deployment Target

- **Platform:** Docker container on an Intel NUC (home server)
- **Primary API:** Fastmail JMAP (RFC 8621) via `https://api.fastmail.com/jmap/api/`
- **LLM provider:** Anthropic API (Claude Haiku for classification)

## Goals

1. **Automated inbox triage** — move read, unflagged emails to the correct subfolder without manual intervention
2. **Deterministic-first classification** — rules handle known senders/patterns; LLM handles the long tail
3. **Continuous learning** — detect manual sorts and automatically create rules from repeated patterns
4. **Full auditability** — every classification decision is logged with reasoning, confidence, and outcome
5. **Privacy-conscious** — only minimal metadata sent to the LLM; per-sender/domain opt-outs supported
6. **Safe by default** — conservative thresholds, age gates, and dry-run mode prevent premature moves

## User Stories

- As a user, I want emails from known senders to be automatically sorted so I don't have to manually file them
- As a user, I want to review what mailsort did (and would have done) via an audit log
- As a user, I want to verify mailsort's classification accuracy before letting it move emails
- As a user, I want mailsort to learn from my manual sorting so it improves over time
- As a user, I want to control which senders' emails are sent to the LLM for privacy reasons
- As a user, I want a web dashboard to monitor classification activity and manage rules
- As a user, I want to deploy mailsort as a set-and-forget Docker container that runs automatically, with a simple web UI for monitoring when I want to check in

## Scope

### In Scope

- Fastmail inbox classification via JMAP
- Rule-based classification (list_id, exact_sender, sender_domain, subject_regex)
- LLM classification via Anthropic API
- Thread context inheritance
- Bootstrap from existing folder contents
- Manual sort detection (5 categories) and auto-rule generation
- Confidence-based gating with per-source thresholds
- Contact enrichment from Fastmail address book
- Web UI for monitoring and rule management
- Docker deployment with health checks
- Visibility into emails consistently left in inbox (threshold analysis, skipped-then-sorted reporting) so users can decide whether to create manual rules

### Out of Scope (Current)

- Multiple Fastmail account support
- Non-Fastmail JMAP servers
- JMAP push notifications (currently polling)
- Full email body analysis for rule generation
- Mobile-specific UI
- Todoist task integration (automatically create tasks from flagged emails in the inbox)
- Rule causation explorer in UI (e.g., visualize a domain rule alongside its related exact_sender rules and the evidence that created them)
- Proactive failure/stall alerting (notify when mailsort stops working or a run fails, without requiring manual dashboard checks)
- UI for creating/editing subject regex rules manually (current rule create form only supports sender-based types)
- LLM-suggested regex rules — surface candidate patterns from repeated LLM classifications for human review and approval
- `List-Unsubscribe` combined rule (domain + has-unsubscribe → folder) — analysis of 2,628 emails found only 1.7% (45 emails from 37 low-volume domains) would benefit; existing `exact_sender` and `sender_domain` rules already cover 58% of the unsub-only gap, and the remaining senders will qualify for `exact_sender` rules as volume accumulates
- Monitoring stack integration (Grafana/Prometheus metrics export, failure alerting, notifications)

## Folder Structure

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

## CLI Interface

```bash
mailsort bootstrap      # One-time: scan folders, seed rules, generate descriptions
mailsort run             # Single classification + move pass
mailsort dry-run         # Classify but don't move
mailsort start           # Start scheduler (runs every N minutes)
mailsort check-config    # Validate config and Fastmail connectivity
mailsort export-rules    # Dump rules to YAML
mailsort analyze         # Confidence threshold analysis
mailsort web             # Start web UI server
```
