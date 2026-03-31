"""Contacts routes — list and refresh action."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/contacts")


@router.get("/")
async def contacts_list(request: Request, q: str = ""):
    db = request.state.db
    templates = request.app.state.templates

    if q:
        rows = db.execute(
            "SELECT * FROM contacts WHERE email_address LIKE ? OR display_name LIKE ? "
            "ORDER BY display_name",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM contacts ORDER BY display_name").fetchall()

    total = db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    last_refresh = db.execute(
        "SELECT value FROM learner_state WHERE key = 'last_contacts_refresh'"
    ).fetchone()

    return templates.TemplateResponse(
        request=request,
        name="contacts/list.html",
        context={
            "contacts": rows,
            "total": total,
            "search": q,
            "last_refresh": last_refresh["value"] if last_refresh else "Never",
            "nav_active": "contacts",
        },
    )
