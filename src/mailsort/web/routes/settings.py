"""Settings route — read-only config display + session management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from mailsort.jmap.client import JMAPClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")


@router.get("/")
async def settings_view(request: Request):
    cfg = request.app.state.cfg
    templates = request.app.state.templates

    # Fetch live JMAP session info for the Fastmail card
    jmap_info: dict = {}
    try:
        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            session = jmap.get_session()
            jmap_info = {
                "account_id": session.account_id,
                "capabilities": len(session.capabilities),
                "contacts": "urn:ietf:params:jmap:contacts" in session.capabilities,
                "is_read_only": session.is_read_only,
            }
    except Exception:
        logger.warning("Failed to fetch JMAP session for settings page", exc_info=True)

    # Fetch active sessions if auth is enabled
    sessions = []
    current_session_id = None
    if request.app.state.auth_enabled:
        db = request.state.db
        current_session_id = request.cookies.get("session_id")
        sessions = [
            dict(row) for row in db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        ]

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "cfg": cfg,
            "jmap_info": jmap_info,
            "nav_active": "settings",
            "sessions": sessions,
            "current_session_id": current_session_id,
        },
    )


@router.post("/revoke-session/{session_id}")
async def revoke_session(request: Request, session_id: str):
    """Revoke (delete) a specific session."""
    db = request.state.db
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    logger.info("Session %s… revoked from settings", session_id[:8])
    return RedirectResponse(url="/settings", status_code=302)


@router.post("/revoke-other-sessions")
async def revoke_other_sessions(request: Request):
    """Revoke all sessions except the current one."""
    db = request.state.db
    current_session_id = request.cookies.get("session_id")
    if current_session_id:
        db.execute("DELETE FROM sessions WHERE id != ?", (current_session_id,))
    else:
        db.execute("DELETE FROM sessions")
    db.commit()
    logger.info("All other sessions revoked from settings")
    return RedirectResponse(url="/settings", status_code=302)
