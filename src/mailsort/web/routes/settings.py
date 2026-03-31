"""Settings route — read-only config display."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/settings")


@router.get("/")
async def settings_view(request: Request):
    cfg = request.app.state.cfg
    templates = request.app.state.templates

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "cfg": cfg,
            "nav_active": "settings",
        },
    )
