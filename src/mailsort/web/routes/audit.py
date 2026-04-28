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
    unique: str = "1",
):
    db = request.state.db
    templates = request.app.state.templates

    # Unique mode: on by default, disabled when filtering by run_id
    use_unique = unique == "1" and not run_id

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

    if use_unique:
        # Dedup by (email_id, classification_source, moved, skip_reason).
        # Keeps the latest row (highest a.id) per unique outcome.
        # Also computes event_count — how many raw rows share this outcome.
        dedup_condition = (
            "AND a.id = ("
            "  SELECT MAX(a2.id) FROM audit_log a2"
            "  WHERE a2.email_id = a.email_id"
            "    AND a2.classification_source = a.classification_source"
            "    AND a2.moved = a.moved"
            "    AND COALESCE(a2.skip_reason, '') = COALESCE(a.skip_reason, '')"
            "    AND a2.created_at >= datetime('now', ?)"
            ")"
        )
        base_unique = f"{base} {dedup_condition}"
        unique_params = tuple(params) + (f"-{days} days",)

        total = db.execute(
            f"SELECT COUNT(*) {base_unique}", unique_params
        ).fetchone()[0]

        offset = (page - 1) * PER_PAGE
        rows = db.execute(
            f"SELECT a.*, ("
            f"  SELECT COUNT(*) FROM audit_log a3"
            f"  WHERE a3.email_id = a.email_id"
            f"    AND a3.classification_source = a.classification_source"
            f"    AND a3.moved = a.moved"
            f"    AND COALESCE(a3.skip_reason, '') = COALESCE(a.skip_reason, '')"
            f"    AND a3.created_at >= datetime('now', ?)"
            f") AS event_count "
            f"{base_unique} ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (f"-{days} days",) + unique_params + (PER_PAGE, offset),
        ).fetchall()
    else:
        total = db.execute(f"SELECT COUNT(*) {base}", tuple(params)).fetchone()[0]

        offset = (page - 1) * PER_PAGE
        rows = db.execute(
            f"SELECT a.*, 1 AS event_count {base} ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (PER_PAGE, offset),
        ).fetchall()

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    # Distinct folders for filter dropdown
    folders = db.execute(
        "SELECT DISTINCT target_folder FROM audit_log ORDER BY target_folder"
    ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="audit/list.html",
        context={
            "rows": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "use_unique": use_unique,
            "filters": {
                "source": source,
                "moved": moved,
                "folder": folder,
                "sender": sender,
                "subject": subject,
                "days": days,
                "run_id": run_id,
                "unique": unique,
            },
            "folders": [r["target_folder"] for r in folders],
            "nav_active": "audit",
        },
    )


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

    return templates.TemplateResponse(
        request=request,
        name="audit/detail.html",
        context={
            "row": row,
            "email_history": email_history,
            "nav_active": "audit",
        },
    )
