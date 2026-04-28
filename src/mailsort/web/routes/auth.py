"""Authentication routes — Google SSO login, callback, and logout."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from mailsort.db.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# ---------------------------------------------------------------------------
# Authlib OAuth registry (configured lazily per-app in setup_oauth)
# ---------------------------------------------------------------------------

_oauth = OAuth()


def setup_oauth(google_client_id: str, google_client_secret: str) -> None:
    """Register the Google OAuth client.  Called once from create_app."""
    _oauth.register(
        name="google",
        client_id=google_client_id,
        client_secret=google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_redirect_uri(request: Request) -> str:
    """Return the OAuth callback URL, preferring the config override."""
    cfg = request.app.state.cfg
    if cfg.auth.redirect_uri:
        return cfg.auth.redirect_uri
    return str(request.url_for("auth_callback"))


def _create_session(
    db: Database,
    email: str,
    name: str | None,
    picture_url: str | None,
    user_agent: str | None,
    ip_address: str | None,
    lifetime_hours: int,
) -> str:
    """Insert a new session row and return the session ID."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=lifetime_hours)
    db.execute(
        """INSERT INTO sessions (id, email, name, picture_url, user_agent,
                                 ip_address, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            email,
            name,
            picture_url,
            user_agent,
            ip_address,
            now.isoformat(),
            expires.isoformat(),
        ),
    )
    db.commit()
    return session_id


def _delete_session(db: Database, session_id: str) -> None:
    """Delete a single session by ID."""
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()


def cleanup_expired_sessions(db: Database) -> int:
    """Delete expired sessions.  Returns number of rows deleted."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.commit()
    return cursor.rowcount


def get_session(db: Database, session_id: str) -> dict | None:
    """Look up a session by ID.  Returns None if missing or expired."""
    row = db.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        _delete_session(db, session_id)
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login")
async def auth_login(request: Request):
    """Show the login page with 'Sign in with Google' button."""
    error = request.query_params.get("error")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error},
    )


@router.get("/start")
async def auth_start(request: Request):
    """Redirect to Google's OAuth consent screen."""
    redirect_uri = _build_redirect_uri(request)
    return await _oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def auth_callback(request: Request):
    """Handle the OAuth callback from Google."""
    cfg = request.app.state.cfg
    db: Database = request.state.db

    try:
        token = await _oauth.google.authorize_access_token(request)
    except Exception:
        logger.warning("OAuth token exchange failed", exc_info=True)
        return RedirectResponse(url="/auth/login", status_code=302)

    userinfo = token.get("userinfo", {})
    email = userinfo.get("email", "")

    # Allowlist check (empty list = allow all)
    if cfg.auth.allowed_emails and email not in cfg.auth.allowed_emails:
        logger.warning("Login rejected for %s — not in allowed_emails", email)
        return RedirectResponse(url="/auth/login?error=forbidden", status_code=302)

    session_id = _create_session(
        db=db,
        email=email,
        name=userinfo.get("name"),
        picture_url=userinfo.get("picture"),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
        lifetime_hours=cfg.auth.session_lifetime_hours,
    )

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=cfg.auth.session_lifetime_hours * 3600,
        secure=request.url.scheme == "https",
    )
    logger.info("User %s logged in (session %s…)", email, session_id[:8])
    return response


@router.post("/logout")
async def auth_logout(request: Request):
    """Delete the current session and clear the cookie."""
    db: Database = request.state.db
    session_id = request.cookies.get("session_id")
    if session_id:
        _delete_session(db, session_id)
        logger.info("Session %s… logged out", session_id[:8])

    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie("session_id")
    return response
