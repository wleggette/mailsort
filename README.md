# Mailsort

Self-hosted email classification service for Fastmail. Periodically scans read, unflagged inbox messages and moves them to the appropriate subfolder using deterministic rules and an LLM classifier.

## Documentation

- [Product Requirements](docs/prd.md) — goals, scope, user stories
- [Architecture](docs/architecture.md) — component diagram, bootstrap & per-run sequences
- **Design docs** — detailed subsystem design:
  - [JMAP Integration](docs/design/jmap-integration.md)
  - [Classification Pipeline](docs/design/classification.md)
  - [Learning & Auto-Rules](docs/design/learning.md)
  - [Audit Log](docs/design/audit.md)
  - [Data Models](docs/design/data-models.md)
  - [Web UI](docs/design/web-ui.md)
- [Configuration Reference](docs/configuration.md)
- [Operations & Deployment](docs/operations.md)
- **Planning** — [phases](docs/planning/phases.md), [open questions](docs/planning/open-questions.md), [system test plan](docs/planning/system-test-plan.md)
- **Dev** — [changelog](docs/dev/changelog.md), [design ideas](docs/dev/design-ideas.md), [scratch notes](docs/dev/scratch.md)

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Validate config and Fastmail connectivity
mailsort check-config

# Bootstrap: scan existing folders to seed rules
mailsort bootstrap

# Single classification pass (useful for testing)
mailsort run

# Dry run: classify but don't move
mailsort dry-run

# Start the scheduler (runs every N minutes)
mailsort start
```

## Docker

```bash
cp .env.example .env  # add your API tokens
docker compose up -d
```

## Configuration

Edit `config.yaml`. API tokens are set via environment variables:

- `FASTMAIL_API_TOKEN` — Fastmail API token
- `ANTHROPIC_API_KEY` — Anthropic API key (for LLM classification)

## Development

```bash
pip install -e ".[dev]"
pytest
```
