"""Dashboard route — landing page with system overview."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    db = request.state.db
    templates = request.app.state.templates

    # Last run
    last_run = db.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    # Recent runs
    recent_runs = db.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 20"
    ).fetchall()

    # Quick stats
    total_rules = db.execute("SELECT COUNT(*) FROM rules WHERE active = 1").fetchone()[0]
    total_contacts = db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    total_folders = db.execute("SELECT COUNT(*) FROM folder_descriptions").fetchone()[0]
    total_processed = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    unique_emails = db.execute("SELECT COUNT(DISTINCT email_id) FROM audit_log").fetchone()[0]

    # Learner state
    last_contact_refresh = db.execute(
        "SELECT value FROM learner_state WHERE key = 'last_contacts_refresh'"
    ).fetchone()
    last_folder_scan = db.execute(
        "SELECT value FROM learner_state WHERE key = 'last_folder_scan'"
    ).fetchone()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "last_run": last_run,
            "recent_runs": recent_runs,
            "stats": {
                "rules": total_rules,
                "contacts": total_contacts,
                "folders": total_folders,
                "processed": total_processed,
                "unique_emails": unique_emails,
            },
            "last_contact_refresh": last_contact_refresh["value"] if last_contact_refresh else "Never",
            "last_folder_scan": last_folder_scan["value"] if last_folder_scan else "Never",
            "nav_active": "dashboard",
        },
    )
