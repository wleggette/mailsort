"""Rules routes — list, detail, toggle, create."""

from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/rules")


@router.get("/")
async def rules_list(request: Request, filter: str = "active"):
    db = request.state.db
    templates = request.app.state.templates

    if filter == "all":
        where = ""
    elif filter == "inactive":
        where = "WHERE active = 0"
    elif filter == "suggested":
        where = "WHERE active = 0 AND source = 'llm_suggested'"
    else:
        where = "WHERE active = 1"

    rules = db.execute(
        f"SELECT * FROM rules {where} ORDER BY rule_type, condition_value"
    ).fetchall()

    # Counts for tabs
    count_active = db.execute("SELECT COUNT(*) FROM rules WHERE active = 1").fetchone()[0]
    count_inactive = db.execute("SELECT COUNT(*) FROM rules WHERE active = 0").fetchone()[0]
    count_suggested = db.execute(
        "SELECT COUNT(*) FROM rules WHERE active = 0 AND source = 'llm_suggested'"
    ).fetchone()[0]
    count_all = count_active + count_inactive

    return templates.TemplateResponse("rules/list.html", {
        "request": request,
        "rules": rules,
        "filter": filter,
        "counts": {
            "active": count_active,
            "inactive": count_inactive,
            "suggested": count_suggested,
            "all": count_all,
        },
        "nav_active": "rules",
    })


@router.get("/{rule_id}")
async def rule_detail(request: Request, rule_id: int):
    db = request.state.db
    templates = request.app.state.templates

    rule = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not rule:
        return templates.TemplateResponse("rules/detail.html", {
            "request": request,
            "rule": None,
            "audit_rows": [],
            "nav_active": "rules",
        })

    # Recent audit log entries that matched this rule
    audit_rows = db.execute(
        "SELECT * FROM audit_log WHERE rule_id = ? ORDER BY created_at DESC LIMIT 50",
        (rule_id,),
    ).fetchall()

    # Coherence stats and evidence emails for this rule's condition
    evidence_rows = []
    if rule["rule_type"] in ("exact_sender", "list_id", "sender_domain"):
        col = {
            "exact_sender": "from_address",
            "sender_domain": "from_domain",
            "list_id": "list_id",
        }[rule["rule_type"]]

        cond_val = rule["condition_value"]

        to_target = db.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {col} COLLATE NOCASE = ? AND target_folder = ? AND moved = 1",
            (cond_val, rule["target_folder_path"]),
        ).fetchone()[0]
        total = db.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {col} COLLATE NOCASE = ? AND moved = 1",
            (cond_val,),
        ).fetchone()[0]
        coherence = to_target / total * 100 if total > 0 else 0

        # All emails matching this condition (evidence for why the rule exists)
        evidence_rows = db.execute(
            f"SELECT * FROM audit_log WHERE {col} COLLATE NOCASE = ? ORDER BY created_at DESC LIMIT 100",
            (cond_val,),
        ).fetchall()
    else:
        to_target = 0
        total = 0
        coherence = 0

    return templates.TemplateResponse("rules/detail.html", {
        "request": request,
        "rule": rule,
        "audit_rows": audit_rows,
        "evidence_rows": evidence_rows,
        "coherence": coherence,
        "evidence_count": to_target,
        "evidence_total": total,
        "nav_active": "rules",
    })


@router.post("/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int):
    db = request.state.db
    rule = db.execute("SELECT active FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if rule:
        new_active = 0 if rule["active"] else 1
        db.execute(
            "UPDATE rules SET active = ?, updated_at = datetime('now') WHERE id = ?",
            (new_active, rule_id),
        )
        db.commit()
    return RedirectResponse(url=f"/rules/{rule_id}", status_code=303)


@router.post("/create")
async def create_rule(
    request: Request,
    rule_type: str = Form(...),
    condition_value: str = Form(...),
    target_folder_path: str = Form(...),
    confidence: float = Form(0.90),
):
    db = request.state.db
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'manual', datetime('now'), datetime('now'))",
        (rule_type, condition_value, target_folder_path, confidence),
    )
    db.commit()
    return RedirectResponse(url="/rules", status_code=303)
