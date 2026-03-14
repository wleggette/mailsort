"""FastAPI application factory for the mailsort web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: Config) -> FastAPI:
    """Create and configure the FastAPI web application."""
    app = FastAPI(title="Mailsort", docs_url=None, redoc_url=None)

    # Store config on app state for access in routes
    app.state.cfg = cfg
    app.state.db_path = cfg.db_path

    # Templates
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app.state.templates = templates

    # Static files (minimal custom CSS if needed)
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Database dependency — open/close per request
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
    from mailsort.web.routes.contacts import router as contacts_router
    from mailsort.web.routes.folders import router as folders_router
    from mailsort.web.routes.settings import router as settings_router
    app.include_router(dashboard_router)
    app.include_router(rules_router)
    app.include_router(audit_router)
    app.include_router(contacts_router)
    app.include_router(folders_router)
    app.include_router(settings_router)

    return app
