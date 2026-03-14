# Mailsort

Self-hosted email classification service for Fastmail. Periodically scans read, unflagged inbox messages and moves them to the appropriate subfolder using deterministic rules and an LLM classifier.

See [ARCHITECTURE.md](ARCHITECTURE.md) for full design documentation.

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
