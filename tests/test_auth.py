"""Tests for Google SSO authentication — middleware, session CRUD, allowlist, routes."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mailsort.config import AuthConfig, Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations as _run_migrations
from mailsort.web.app import create_app
from mailsort.web.routes.auth import (
    _create_session,
    _delete_session,
    cleanup_expired_sessions,
    get_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _make_client(db_path: str, *, auth_enabled: bool = False,
                 allowed_emails: list[str] | None = None) -> TestClient:
    """Create a TestClient with optional auth configuration."""
    auth = AuthConfig()
    if auth_enabled:
        auth = AuthConfig(
            google_client_id="test-client-id.apps.googleusercontent.com",
            allowed_emails=allowed_emails or [],
            session_lifetime_hours=720,
        )
    cfg = Config(
        fastmail_api_token="test-token-abc123",
        anthropic_api_key="test-anthropic-key",
        db_path=db_path,
        auth=auth,
        google_client_secret="test-secret" if auth_enabled else "",
    )
    app = create_app(cfg)
    return TestClient(app)


def _seed_session(db: Database, *, session_id: str = "sess-abc",
                  email: str = "user@example.com",
                  name: str = "Test User",
                  hours_until_expiry: int = 24) -> str:
    """Insert a session row directly and return the session ID."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours_until_expiry)
    db.execute(
        """INSERT INTO sessions (id, email, name, picture_url, user_agent,
                                 ip_address, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, email, name, None, "TestAgent/1.0", "127.0.0.1",
         now.isoformat(), expires.isoformat()),
    )
    db.commit()
    return session_id


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


class TestSessionCRUD:
    def test_create_session(self, web_db):
        db, _ = web_db
        sid = _create_session(
            db, email="a@b.com", name="A", picture_url=None,
            user_agent="UA", ip_address="1.2.3.4", lifetime_hours=1,
        )
        assert len(sid) > 20
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        assert row["email"] == "a@b.com"
        assert row["name"] == "A"

    def test_get_session_valid(self, web_db):
        db, _ = web_db
        _seed_session(db, session_id="valid-1")
        result = get_session(db, "valid-1")
        assert result is not None
        assert result["email"] == "user@example.com"

    def test_get_session_missing(self, web_db):
        db, _ = web_db
        assert get_session(db, "nonexistent") is None

    def test_get_session_expired(self, web_db):
        db, _ = web_db
        _seed_session(db, session_id="expired-1", hours_until_expiry=-1)
        result = get_session(db, "expired-1")
        assert result is None
        # Should also be deleted from DB
        row = db.execute("SELECT * FROM sessions WHERE id = 'expired-1'").fetchone()
        assert row is None

    def test_delete_session(self, web_db):
        db, _ = web_db
        _seed_session(db, session_id="del-1")
        _delete_session(db, "del-1")
        row = db.execute("SELECT * FROM sessions WHERE id = 'del-1'").fetchone()
        assert row is None

    def test_cleanup_expired_sessions(self, web_db):
        db, _ = web_db
        _seed_session(db, session_id="active", hours_until_expiry=24)
        _seed_session(db, session_id="expired-a", hours_until_expiry=-1)
        _seed_session(db, session_id="expired-b", hours_until_expiry=-2)
        deleted = cleanup_expired_sessions(db)
        assert deleted == 2
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    def test_auth_disabled_passes_through(self, web_db):
        """When auth is disabled, all routes are accessible without a session."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_auth_enabled_redirects_to_login(self, web_db):
        """When auth is enabled and no session cookie, redirect to /auth/login."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]

    def test_auth_enabled_valid_session(self, web_db):
        """With a valid session cookie, protected routes are accessible."""
        db, db_path = web_db
        _seed_session(db, session_id="good-sess")
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "good-sess")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_auth_enabled_expired_session_redirects(self, web_db):
        """An expired session cookie should redirect to login."""
        db, db_path = web_db
        _seed_session(db, session_id="old-sess", hours_until_expiry=-1)
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "old-sess")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]

    def test_auth_enabled_invalid_session_redirects(self, web_db):
        """A bogus session cookie should redirect to login."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "totally-bogus")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302

    def test_login_page_accessible_without_session(self, web_db):
        """The login page itself should be accessible without auth."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        resp = client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "Sign in with Google" in resp.text

    def test_login_page_shows_forbidden_error(self, web_db):
        """Login page with ?error=forbidden shows the error message."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        resp = client.get("/auth/login?error=forbidden", follow_redirects=False)
        assert resp.status_code == 200
        assert "not authorized" in resp.text

    def test_health_exempt_from_auth(self, web_db):
        """The /health endpoint should be exempt from auth."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        resp = client.get("/health", follow_redirects=False)
        # /health may not exist (404) but should NOT be 302
        assert resp.status_code != 302


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_session(self, web_db):
        db, db_path = web_db
        _seed_session(db, session_id="logout-sess")
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "logout-sess")
        resp = client.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]
        # Session should be deleted from DB
        row = db.execute("SELECT * FROM sessions WHERE id = 'logout-sess'").fetchone()
        assert row is None

    def test_logout_without_session(self, web_db):
        """Logout with no session cookie should still redirect gracefully."""
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=True)
        resp = client.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Session revocation (settings)
# ---------------------------------------------------------------------------


class TestSessionRevocation:
    def test_revoke_session(self, web_db):
        db, db_path = web_db
        _seed_session(db, session_id="current")
        _seed_session(db, session_id="other", email="other@example.com")
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "current")
        resp = client.post("/settings/revoke-session/other", follow_redirects=False)
        assert resp.status_code == 302
        assert db.execute("SELECT * FROM sessions WHERE id = 'other'").fetchone() is None
        assert db.execute("SELECT * FROM sessions WHERE id = 'current'").fetchone() is not None

    def test_revoke_other_sessions(self, web_db):
        db, db_path = web_db
        _seed_session(db, session_id="keep")
        _seed_session(db, session_id="del-1", email="a@b.com")
        _seed_session(db, session_id="del-2", email="c@d.com")
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "keep")
        resp = client.post("/settings/revoke-other-sessions", follow_redirects=False)
        assert resp.status_code == 302
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT * FROM sessions WHERE id = 'keep'").fetchone() is not None


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_auth_absent_defaults_disabled(self):
        cfg = Config(
            fastmail_api_token="t",
            db_path=":memory:",
        )
        assert cfg.auth.google_client_id is None
        assert cfg.auth.allowed_emails == []
        assert cfg.auth.session_lifetime_hours == 720

    def test_auth_present(self):
        cfg = Config(
            fastmail_api_token="t",
            db_path=":memory:",
            auth=AuthConfig(
                google_client_id="id.apps.googleusercontent.com",
                allowed_emails=["a@b.com"],
                session_lifetime_hours=48,
            ),
        )
        assert cfg.auth.google_client_id == "id.apps.googleusercontent.com"
        assert cfg.auth.allowed_emails == ["a@b.com"]
        assert cfg.auth.session_lifetime_hours == 48

    def test_google_client_secret_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret-123")
        cfg = Config(
            fastmail_api_token="t",
            db_path=":memory:",
        )
        assert cfg.google_client_secret == "test-secret-123"

    def test_google_client_secret_defaults_empty(self):
        cfg = Config(
            fastmail_api_token="t",
            db_path=":memory:",
        )
        assert cfg.google_client_secret == ""


# ---------------------------------------------------------------------------
# Settings page — sessions panel visibility
# ---------------------------------------------------------------------------


class TestSettingsSessionsPanel:
    def test_sessions_panel_hidden_when_auth_disabled(self, web_db):
        _, db_path = web_db
        client = _make_client(db_path, auth_enabled=False)
        resp = client.get("/settings")
        assert "Active Sessions" not in resp.text

    def test_sessions_panel_visible_when_auth_enabled(self, web_db):
        db, db_path = web_db
        _seed_session(db, session_id="vis-sess")
        client = _make_client(db_path, auth_enabled=True)
        client.cookies.set("session_id", "vis-sess")
        resp = client.get("/settings")
        assert "Active Sessions" in resp.text
        assert "user@example.com" in resp.text
        assert "(this session)" in resp.text
