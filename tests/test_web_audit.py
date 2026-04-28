"""Tests for the /audit route — dedup (unique) mode."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations as _run_migrations
from mailsort.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_run(db: Database, run_id: str, *, trigger: str = "scheduler"):
    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES (?, datetime('now'), 'completed', ?, 0)",
        (run_id, trigger),
    )


def _seed_audit(db: Database, *, email_id: str, run_id: str = "run-1",
                from_address: str = "test@example.com",
                target_folder: str = "INBOX",
                confidence: float = 0.50,
                classification_source: str = "llm",
                moved: bool = False,
                skip_reason: str | None = None):
    _seed_run(db, run_id)
    db.execute(
        "INSERT INTO audit_log "
        "(run_id, email_id, from_address, from_domain, subject, "
        " source_folder, target_folder, confidence, classification_source, "
        " moved, skip_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, email_id, from_address, "example.com", "Test subject",
         "INBOX", target_folder, confidence, classification_source,
         moved, skip_reason),
    )
    db.commit()


@pytest.fixture
def web_db():
    """File-backed temp DB for route tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    database.connect()
    _run_migrations(database)
    yield database, db_path
    database.close()
    os.unlink(db_path)


def _make_client(db_path: str) -> TestClient:
    cfg = Config(
        fastmail_api_token="test-token-abc123",
        anthropic_api_key="test-anthropic-key",
        db_path=db_path,
    )
    app = create_app(cfg)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_unique_dedup_same_outcome(web_db):
    """5 identical LLM skip rows for same email → 1 unique row with event_count=5."""
    db, db_path = web_db
    for i in range(5):
        _seed_audit(db, email_id="msg-dup", run_id=f"run-{i}",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold")

    client = _make_client(db_path)
    response = client.get("/audit?days=30&unique=1")
    assert response.status_code == 200
    assert "1 unique events" in response.text
    assert "&times;5" in response.text


def test_unique_preserves_different_outcomes(web_db):
    """Same email: flagged → below_threshold → moved = 3 unique rows."""
    db, db_path = web_db
    _seed_audit(db, email_id="msg-multi", run_id="run-1",
                classification_source="llm", moved=False,
                skip_reason="flagged")
    _seed_audit(db, email_id="msg-multi", run_id="run-2",
                classification_source="llm", moved=False,
                skip_reason="below_threshold")
    _seed_audit(db, email_id="msg-multi", run_id="run-3",
                classification_source="llm", moved=True,
                target_folder="Affairs/Stores")

    client = _make_client(db_path)
    response = client.get("/audit?days=30&unique=1")
    assert response.status_code == 200
    assert "3 unique events" in response.text


def test_unique_off_shows_all_rows(web_db):
    """With unique=0, all raw rows are shown."""
    db, db_path = web_db
    for i in range(5):
        _seed_audit(db, email_id="msg-dup", run_id=f"run-{i}",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold")

    client = _make_client(db_path)
    response = client.get("/audit?days=30&unique=0")
    assert response.status_code == 200
    assert "5 classification entries" in response.text


def test_run_id_filter_disables_unique(web_db):
    """When filtering by run_id, unique mode is disabled even if unique=1."""
    db, db_path = web_db
    for i in range(3):
        _seed_audit(db, email_id="msg-dup", run_id="run-same",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold")

    client = _make_client(db_path)
    response = client.get("/audit?days=30&unique=1&run_id=run-same")
    assert response.status_code == 200
    assert "3 classification entries" in response.text


def test_unique_default_on(web_db):
    """Default request (no unique param) uses unique mode."""
    db, db_path = web_db
    for i in range(3):
        _seed_audit(db, email_id="msg-dup", run_id=f"run-{i}",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold")

    client = _make_client(db_path)
    response = client.get("/audit?days=30")
    assert response.status_code == 200
    assert "1 unique events" in response.text


def test_unique_correction_shows_separately(web_db):
    """An email moved by LLM then corrected shows as 2 unique rows."""
    db, db_path = web_db
    _seed_audit(db, email_id="msg-cor", run_id="run-1",
                classification_source="llm", moved=True,
                target_folder="Affairs/Banks")
    _seed_audit(db, email_id="msg-cor", run_id="run-2",
                classification_source="correction", moved=True,
                target_folder="Affairs/Stores")

    client = _make_client(db_path)
    response = client.get("/audit?days=30&unique=1")
    assert response.status_code == 200
    assert "2 unique events" in response.text
