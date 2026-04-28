"""FastAPI application factory for the mailsort web UI."""

from __future__ import annotations

import random
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: Config) -> FastAPI:
    """Create and configure the FastAPI web application."""
    app = FastAPI(title="MailSort", docs_url=None, redoc_url=None)

    # Store config on app state for access in routes
    app.state.cfg = cfg
    app.state.db_path = cfg.db_path

    # Templates
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app.state.templates = templates

    # Static files (minimal custom CSS if needed)
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Starlette session middleware — used by Authlib for OAuth CSRF state.
    # The secret is ephemeral (lost on restart), which is fine: it only
    # protects the short-lived OAuth dance, not long-lived user sessions.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        session_cookie="_oauth_state",
        max_age=600,  # 10 min — just enough for the OAuth round-trip
    )

    # Configure Authlib OAuth if auth is enabled
    auth_enabled = bool(cfg.auth.google_client_id)
    app.state.auth_enabled = auth_enabled
    if auth_enabled:
        from mailsort.web.routes.auth import setup_oauth
        setup_oauth(cfg.auth.google_client_id, cfg.google_client_secret)

    # Middleware order: last-defined = outermost = runs first.
    # Execution: db → auth → template_context → route handler.

    # Inject session into template globals for nav avatar/logout (innermost)
    @app.middleware("http")
    async def template_context_middleware(request: Request, call_next):
        response = await call_next(request)
        return response

    # Auth middleware — validate session cookie, redirect to login if missing
    _AUTH_EXEMPT = {"/auth/login", "/auth/start", "/auth/callback", "/auth/logout", "/health"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # No-op when auth is disabled
        if not auth_enabled:
            request.state.session = None
            templates.env.globals["session"] = None
            templates.env.globals["auth_enabled"] = False
            return await call_next(request)

        path = request.url.path

        # Skip auth for exempt routes and static files
        if path in _AUTH_EXEMPT or path.startswith("/static/"):
            request.state.session = None
            return await call_next(request)

        # Validate session cookie
        from mailsort.web.routes.auth import get_session, cleanup_expired_sessions
        session_id = request.cookies.get("session_id")
        session = get_session(request.state.db, session_id) if session_id else None

        if session is None:
            return RedirectResponse(url="/auth/login", status_code=302)

        request.state.session = session

        # Lazy session cleanup — 1-in-100 requests
        if random.randint(1, 100) == 1:
            cleanup_expired_sessions(request.state.db)

        # Set template globals for this request
        templates.env.globals["session"] = session
        templates.env.globals["auth_enabled"] = auth_enabled

        response = await call_next(request)
        return response

    # Database dependency — open/close per request (outermost)
    @app.middleware("http")
    async def db_middleware(request: Request, call_next):
        db = Database(cfg.db_path)
        db.connect()
        request.state.db = db
        try:
            response = await call_next(request)
            return response
        finally:
            db.close()

    # Register route modules
    from mailsort.web.routes.dashboard import router as dashboard_router
    from mailsort.web.routes.rules import router as rules_router
    from mailsort.web.routes.audit import router as audit_router
    from mailsort.web.routes.analyze import router as analyze_router
    from mailsort.web.routes.contacts import router as contacts_router
    from mailsort.web.routes.folders import router as folders_router
    from mailsort.web.routes.settings import router as settings_router
    from mailsort.web.routes.auth import router as auth_router
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(rules_router)
    app.include_router(audit_router)
    app.include_router(analyze_router)
    app.include_router(contacts_router)
    app.include_router(folders_router)
    app.include_router(settings_router)

    return app
