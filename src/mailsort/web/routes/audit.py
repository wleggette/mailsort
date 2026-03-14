"""Audit log routes — filterable list and detail view."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/audit")

PER_PAGE = 50


@router.get("/")
async def audit_list(
    request: Request,
    source: str = "",
    moved: str = "",
    folder: str = "",
    sender: str = "",
    subject: str = "",
    days: int = 30,
    run_id: str = "",
    page: int = 1,
):
    db = request.state.db
    templates = request.app.state.templates

    conditions = ["a.created_at >= datetime('now', ?)"]
    params: list = [f"-{days} days"]

    # Exclude bootstrap runs
    conditions.append("r.trigger != 'bootstrap'")

    if source:
        conditions.append("a.classification_source = ?")
        params.append(source)
    if moved == "1":
        conditions.append("a.moved = 1")
    elif moved == "0":
        conditions.append("a.moved = 0")
    if folder:
        conditions.append("a.target_folder = ?")
        params.append(folder)
    if sender:
        conditions.append("a.from_address LIKE ?")
        params.append(f"%{sender}%")
    if subject:
        conditions.append("a.subject LIKE ?")
        params.append(f"%{subject}%")
    if run_id:
        conditions.append("a.run_id LIKE ?")
        params.append(f"{run_id}%")

    where = " AND ".join(conditions)
    base = f"FROM audit_log a JOIN runs r ON r.run_id = a.run_id WHERE {where}"

    # Count
    total = db.execute(f"SELECT COUNT(*) {base}", tuple(params)).fetchone()[0]

    # Paginate
    offset = (page - 1) * PER_PAGE
    rows = db.execute(
        f"SELECT a.* {base} ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (PER_PAGE, offset),
    ).fetchall()

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    # Distinct folders for filter dropdown
    folders = db.execute(
        "SELECT DISTINCT target_folder FROM audit_log ORDER BY target_folder"
    ).fetchall()

    return templates.TemplateResponse("audit/list.html", {
        "request": request,
        "rows": rows,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "filters": {
            "source": source,
            "moved": moved,
            "folder": folder,
            "sender": sender,
            "subject": subject,
            "days": days,
            "run_id": run_id,
        },
        "folders": [r["target_folder"] for r in folders],
        "nav_active": "audit",
    })


@router.get("/{audit_id}")
async def audit_detail(request: Request, audit_id: int):
    db = request.state.db
    templates = request.app.state.templates

    row = db.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()

    # Fetch all audit entries for the same email (for history card)
    email_history = []
    if row:
        email_history = db.execute(
            "SELECT * FROM audit_log WHERE email_id = ? ORDER BY created_at DESC",
            (row["email_id"],),
        ).fetchall()

    return templates.TemplateResponse("audit/detail.html", {
        "request": request,
        "row": row,
        "email_history": email_history,
        "nav_active": "audit",
    })
