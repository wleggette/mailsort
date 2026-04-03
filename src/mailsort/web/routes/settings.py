"""Settings route — read-only config display."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

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

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "cfg": cfg,
            "jmap_info": jmap_info,
            "nav_active": "settings",
        },
    )
