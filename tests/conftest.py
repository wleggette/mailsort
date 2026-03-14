"""Shared pytest fixtures for mailsort tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.jmap.models import JMAPMailbox, JMAPEmail

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_mailboxes() -> list[JMAPMailbox]:
    data = json.loads((FIXTURES / "sample_mailboxes.json").read_text())
    return [JMAPMailbox.model_validate(m) for m in data]


@pytest.fixture
def sample_emails() -> list[JMAPEmail]:
    data = json.loads((FIXTURES / "sample_emails.json").read_text())
    return [JMAPEmail.model_validate(e) for e in data]


@pytest.fixture
def test_config() -> Config:
    """Minimal config for tests — no real secrets needed."""
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15, min_age_minutes=240, max_batch_size=100),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token-abc123",
        anthropic_api_key="test-anthropic-key",
        db_path=":memory:",
    )


@pytest.fixture
def db(test_config: Config) -> Database:
    """In-memory SQLite database with all migrations applied."""
    database = Database(test_config.db_path)
    database.connect()
    run_migrations(database)
    yield database
    database.close()
