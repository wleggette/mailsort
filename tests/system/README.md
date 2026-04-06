# System Tests

End-to-end tests against a real Fastmail test account. See `SYSTEM_TEST_PLAN.md` in the project root for the full test design.

## Prerequisites

1. A dedicated Fastmail test account with folders: `Affairs/Banks`, `Affairs/Stores`, `People/Children`
2. API token with full read/write access
3. `ANTHROPIC_API_KEY` set for LLM classification tests
4. `pyyaml` installed (`pip install pyyaml`)

## Quick Start

```bash
# Set env vars
export FASTMAIL_API_TOKEN="fmu1-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# Setup only (for development) — run from project root
python tests/system/run_system_test.py --setup-only

# Then interact manually:
mailsort web --config tests/system/config.test.yaml --port 8081
mailsort dry-run --config tests/system/config.test.yaml
mailsort run --config tests/system/config.test.yaml

# Full automated sequence
python tests/system/run_system_test.py

# Cleanup only
python tests/system/run_system_test.py --cleanup
```

The test harness defaults to `tests/system/config.test.yaml` — no need to copy
it to the project root. Override with `--config <path>` if needed.

## Files

| File | Purpose |
|------|---------|
| `config.test.yaml` | Test configuration (used in-place, not copied to root) |
| `fixtures/folder_emails.json` | Static fixture emails for bootstrap (groups A–I) |
| `generate_inbox_emails.py` | Dynamic inbox email generator with relative timestamps |
| `load_fixtures.py` | JMAP email loader (import emails into test account) |
| `run_system_test.py` | Test orchestrator (setup-only or full sequence) |
| `verify_results.py` | DB result validator |

## Test Data

~76 static fixture emails across 3 folders, plus ~12 dynamic inbox emails. Covers:

- **Rule creation**: exact_sender, sender_domain, list_id (high + low coherence)
- **Contact interaction**: known/unknown × concentrated/split
- **Classification sources**: rule, domain rule, list_id, thread, LLM
- **Eligibility gates**: unread, flagged, too_new, eligible
- **Feedback loop**: user correction → rule penalty → deactivation

All test emails are tagged with `$mailsort-test` keyword for easy cleanup.
