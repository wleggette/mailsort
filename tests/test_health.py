"""Tests for the health check endpoint.

Uses temp file DBs because the health server opens its own connection
and can't share an in-memory database.
"""

from __future__ import annotations

import json
import tempfile
import urllib.request
from pathlib import Path

import pytest

from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.health import start_health_server


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """Create a temp-file DB with migrations applied."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.connect()
    run_migrations(db)
    yield db
    db.close()


def test_health_endpoint_returns_ok(tmp_db: Database):
    """GET /health should return 200 with JSON status."""
    server = start_health_server(str(tmp_db._path), port=0)
    assert server is not None

    _, port = server.server_address
    try:
        url = f"http://127.0.0.1:{port}/health"
        resp = urllib.request.urlopen(url, timeout=2)
        assert resp.status == 200

        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["service"] == "mailsort"
        assert "last_run" in data
    finally:
        server.shutdown()


def test_health_endpoint_shows_last_run(tmp_db: Database):
    """After a run is recorded, /health should include its details."""
    tmp_db.execute(
        "INSERT INTO runs (run_id, started_at, status, emails_seen, emails_moved) "
        "VALUES ('run-health', datetime('now'), 'completed', 10, 7)"
    )
    tmp_db.commit()

    server = start_health_server(str(tmp_db._path), port=0)
    assert server is not None

    _, port = server.server_address
    try:
        url = f"http://127.0.0.1:{port}/health"
        resp = urllib.request.urlopen(url, timeout=2)
        data = json.loads(resp.read())

        assert data["ok"] is True
        assert data["last_run"] is not None
        assert data["last_run"]["status"] == "completed"
        assert data["last_run"]["emails_seen"] == 10
        assert data["last_run"]["emails_moved"] == 7
    finally:
        server.shutdown()


def test_health_reports_failed_run(tmp_db: Database):
    """A failed last run should set ok=False."""
    tmp_db.execute(
        "INSERT INTO runs (run_id, started_at, status, error_summary) "
        "VALUES ('run-fail', datetime('now'), 'failed', 'JMAP unreachable')"
    )
    tmp_db.commit()

    server = start_health_server(str(tmp_db._path), port=0)
    assert server is not None

    _, port = server.server_address
    try:
        url = f"http://127.0.0.1:{port}/health"
        resp = urllib.request.urlopen(url, timeout=2)
        data = json.loads(resp.read())

        assert data["ok"] is False
        assert "JMAP unreachable" in data["error"]
    finally:
        server.shutdown()


def test_health_404_on_other_paths(tmp_db: Database):
    """Non /health paths should return 404."""
    server = start_health_server(str(tmp_db._path), port=0)
    assert server is not None

    _, port = server.server_address
    try:
        url = f"http://127.0.0.1:{port}/notfound"
        try:
            urllib.request.urlopen(url, timeout=2)
            assert False, "Expected HTTP error"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
