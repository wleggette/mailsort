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

# Copy test config to project root
cp tests/system/config.test.yaml config.test.yaml

# Setup only (for development)
python tests/system/run_system_test.py \
  --config config.test.yaml \
  --to-email your-test@fastmail.com \
  --setup-only

# Then interact manually:
mailsort web --config config.test.yaml --port 8081
mailsort dry-run --config config.test.yaml
mailsort run --config config.test.yaml

# Full automated sequence
python tests/system/run_system_test.py \
  --config config.test.yaml \
  --to-email your-test@fastmail.com

# Cleanup only
python tests/system/run_system_test.py \
  --config config.test.yaml \
  --to-email your-test@fastmail.com \
  --cleanup
```

## Files

| File | Purpose |
|------|---------|
| `config.test.yaml` | Test config template (copy to project root) |
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
