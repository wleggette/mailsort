"""Rules routes — list, detail, toggle, create."""

from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/rules")


@router.get("/")
async def rules_list(
    request: Request,
    filter: str = "active",
    type: str = "",
    search: str = "",
    folder: str = "",
    conf_min: str = "",
    conf_max: str = "",
):
    db = request.state.db
    templates = request.app.state.templates

    conditions: list[str] = []
    params: list = []

    # Tab filter
    if filter == "inactive":
        conditions.append("active = 0")
    elif filter == "suggested":
        conditions.append("active = 0 AND source = 'llm_suggested'")
    elif filter == "all":
        pass  # no active filter
    else:
        conditions.append("active = 1")

    # Search filters
    if type:
        conditions.append("rule_type = ?")
        params.append(type)
    if search:
        conditions.append("condition_value LIKE ?")
        params.append(f"%{search}%")
    if folder:
        conditions.append("target_folder_path LIKE ?")
        params.append(f"%{folder}%")
    if conf_min:
        try:
            conditions.append("confidence >= ?")
            params.append(float(conf_min))
        except ValueError:
            pass
    if conf_max:
        try:
            conditions.append("confidence < ?")
            params.append(float(conf_max))
        except ValueError:
            pass

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rules = db.execute(
        f"SELECT * FROM rules {where} ORDER BY rule_type, condition_value",
        tuple(params),
    ).fetchall()

    # Counts for tabs (unaffected by search filters)
    count_active = db.execute("SELECT COUNT(*) FROM rules WHERE active = 1").fetchone()[0]
    count_inactive = db.execute("SELECT COUNT(*) FROM rules WHERE active = 0").fetchone()[0]
    count_suggested = db.execute(
        "SELECT COUNT(*) FROM rules WHERE active = 0 AND source = 'llm_suggested'"
    ).fetchone()[0]
    count_all = count_active + count_inactive

    # Distinct folders for filter dropdown
    folders = db.execute(
        "SELECT DISTINCT target_folder_path FROM rules ORDER BY target_folder_path"
    ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="rules/list.html",
        context={
            "rules": rules,
            "filter": filter,
            "counts": {
                "active": count_active,
                "inactive": count_inactive,
                "suggested": count_suggested,
                "all": count_all,
            },
            "filters": {
                "type": type,
                "search": search,
                "folder": folder,
                "conf_min": conf_min,
                "conf_max": conf_max,
            },
            "folders": [r["target_folder_path"] for r in folders],
            "nav_active": "rules",
        },
    )


@router.get("/{rule_id}")
async def rule_detail(request: Request, rule_id: int):
    db = request.state.db
    templates = request.app.state.templates

    rule = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not rule:
        return templates.TemplateResponse(
            request=request,
            name="rules/detail.html",
            context={
                "rule": None,
                "audit_rows": [],
                "nav_active": "rules",
            },
        )

    # Recent audit log entries that matched this rule
    audit_rows = db.execute(
        "SELECT * FROM audit_log WHERE rule_id = ? ORDER BY created_at DESC LIMIT 50",
        (rule_id,),
    ).fetchall()

    # ---- Coherence & evidence stats (all-time + windowed) ----
    _COL_MAP = {
        "exact_sender": "from_address",
        "sender_domain": "from_domain",
        "list_id": "list_id",
    }
    evidence_rows = []
    stats: dict = {
        "all_time": {"to_target": 0, "total": 0, "coherence": 0,
                     "corrections": 0, "confirming": 0, "net_corrections": 0},
        "windowed": {"to_target": 0, "total": 0, "coherence": 0,
                     "corrections": 0, "confirming": 0, "net_corrections": 0},
    }

    if rule["rule_type"] in _COL_MAP:
        col = _COL_MAP[rule["rule_type"]]
        cond_val = rule["condition_value"]
        target = rule["target_folder_path"]

        # --- All-time ---
        at_to_target = db.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {col} COLLATE NOCASE = ? AND target_folder = ? AND moved = 1",
            (cond_val, target),
        ).fetchone()[0]
        at_total = db.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {col} COLLATE NOCASE = ? AND moved = 1",
            (cond_val,),
        ).fetchone()[0]
        at_corrections = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'correction' AND rule_id = ?",
            (rule_id,),
        ).fetchone()[0]
        at_confirming = db.execute(
            f"""SELECT COUNT(*) FROM audit_log
                WHERE classification_source = 'manual' AND {col} = ? AND target_folder = ?""",
            (cond_val, target),
        ).fetchone()[0]
        stats["all_time"] = {
            "to_target": at_to_target, "total": at_total,
            "coherence": at_to_target / at_total * 100 if at_total > 0 else 0,
            "corrections": at_corrections, "confirming": at_confirming,
            "net_corrections": max(0, at_corrections - at_confirming),
        }

        # --- Windowed (30 days) ---
        lookback = "-30 days"
        w_to_target = db.execute(
            f"""SELECT COUNT(*) FROM audit_log
                WHERE {col} COLLATE NOCASE = ? AND target_folder = ? AND moved = 1
                  AND created_at >= datetime('now', ?)""",
            (cond_val, target, lookback),
        ).fetchone()[0]
        w_total = db.execute(
            f"""SELECT COUNT(*) FROM audit_log
                WHERE {col} COLLATE NOCASE = ? AND moved = 1
                  AND created_at >= datetime('now', ?)""",
            (cond_val, lookback),
        ).fetchone()[0]
        w_corrections = db.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE classification_source = 'correction' AND rule_id = ?
                 AND created_at >= datetime('now', ?)""",
            (rule_id, lookback),
        ).fetchone()[0]
        w_confirming = db.execute(
            f"""SELECT COUNT(*) FROM audit_log
                WHERE classification_source = 'manual' AND {col} = ? AND target_folder = ?
                  AND created_at >= datetime('now', ?)""",
            (cond_val, target, lookback),
        ).fetchone()[0]
        stats["windowed"] = {
            "to_target": w_to_target, "total": w_total,
            "coherence": w_to_target / w_total * 100 if w_total > 0 else 0,
            "corrections": w_corrections, "confirming": w_confirming,
            "net_corrections": max(0, w_corrections - w_confirming),
        }

        # All emails matching this condition (evidence table)
        evidence_rows = db.execute(
            f"SELECT * FROM audit_log WHERE {col} COLLATE NOCASE = ? ORDER BY created_at DESC LIMIT 100",
            (cond_val,),
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="rules/detail.html",
        context={
            "rule": rule,
            "audit_rows": audit_rows,
            "evidence_rows": evidence_rows,
            "stats": stats,
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
