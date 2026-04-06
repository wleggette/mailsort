"""Threshold analysis route — interactive version of `mailsort analyze`."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/analyze")
async def analyze(request: Request, days: int = 30):
    db = request.state.db
    templates = request.app.state.templates
    cfg = request.app.state.cfg

    window = f"-{days} days"

    # Deduplicated base: keep only the most recent audit row per email_id.
    # Excludes bootstrap, dry runs, and manual rows. Uses MAX(a.id) per
    # email_id so an email classified across multiple cycles counts once
    # with its final outcome.
    dedup_cte = (
        "WITH latest AS ("
        "  SELECT a.* FROM audit_log a"
        "  JOIN runs r ON r.run_id = a.run_id"
        "  WHERE r.trigger != 'bootstrap' AND r.dry_run = 0"
        "    AND a.created_at >= datetime('now', ?)"
        "    AND a.classification_source != 'manual'"
        "    AND a.id = ("
        "      SELECT MAX(a2.id) FROM audit_log a2"
        "      JOIN runs r2 ON r2.run_id = a2.run_id"
        "      WHERE a2.email_id = a.email_id"
        "        AND r2.trigger != 'bootstrap' AND r2.dry_run = 0"
        "        AND a2.created_at >= datetime('now', ?)"
        "        AND a2.classification_source != 'manual'"
        "    )"
        ") "
    )
    # Queries against the CTE need 2 window params (outer + subquery)
    cte_params = (window, window)

    # Overall counts
    total = db.execute(
        f"{dedup_cte} SELECT COUNT(*) FROM latest", cte_params
    ).fetchone()[0]
    moved = db.execute(
        f"{dedup_cte} SELECT COUNT(*) FROM latest WHERE moved = 1", cte_params
    ).fetchone()[0]
    skipped = total - moved

    # By source
    source_rows = db.execute(
        f"{dedup_cte} SELECT classification_source as source, COUNT(*) as n, "
        "SUM(CASE WHEN moved = 1 THEN 1 ELSE 0 END) as moved "
        "FROM latest GROUP BY classification_source ORDER BY n DESC", cte_params
    ).fetchall()

    sources = []
    for r in source_rows:
        pct = r["n"] / total * 100 if total > 0 else 0
        sources.append({
            "name": r["source"],
            "count": r["n"],
            "moved": r["moved"] or 0,
            "pct": round(pct, 1),
            "bar_width": int(pct),
        })

    # True corrections (emails mailsort moved that the user relocated)
    corrections = db.execute(
        "SELECT COUNT(DISTINCT a.email_id) FROM audit_log a "
        "JOIN runs r ON r.run_id = a.run_id "
        "WHERE r.trigger != 'bootstrap' AND a.classification_source = 'correction' "
        "  AND a.created_at >= datetime('now', ?)", (window,)
    ).fetchone()[0]
    error_rate = corrections / moved * 100 if moved > 0 else 0.0

    # LLM confidence distribution (also deduplicated)
    buckets = [
        ("< 0.60", "confidence < 0.60"),
        ("0.60\u20130.69", "confidence >= 0.60 AND confidence < 0.70"),
        ("0.70\u20130.79", "confidence >= 0.70 AND confidence < 0.80"),
        ("0.80\u20130.89", "confidence >= 0.80 AND confidence < 0.90"),
        ("0.90\u20131.00", "confidence >= 0.90"),
    ]
    confidence_dist = []
    for label, condition in buckets:
        row = db.execute(
            f"{dedup_cte} SELECT "
            f"SUM(CASE WHEN moved = 1 THEN 1 ELSE 0 END) as moved, "
            f"SUM(CASE WHEN moved = 0 THEN 1 ELSE 0 END) as skipped "
            f"FROM latest WHERE classification_source = 'llm' AND {condition}",
            cte_params
        ).fetchone()
        m = row["moved"] or 0
        s = row["skipped"] or 0
        if m > 0 or s > 0:
            confidence_dist.append({
                "label": label,
                "moved": m,
                "skipped": s,
                "total": m + s,
                "is_threshold": label == "0.80–0.89",
            })

    # Skipped LLM emails that user later sorted to same folder
    skipped_then_sorted = db.execute(
        "SELECT a1.email_id, a1.from_address, a1.subject, "
        "       a1.target_folder AS llm_folder, a1.confidence, "
        "       a2.target_folder AS manual_folder "
        "FROM audit_log a1 "
        "JOIN runs r1 ON r1.run_id = a1.run_id "
        "JOIN audit_log a2 ON a1.email_id = a2.email_id "
        "JOIN runs r2 ON r2.run_id = a2.run_id "
        "WHERE r1.trigger != 'bootstrap' AND r2.trigger != 'bootstrap' "
        "  AND a1.classification_source = 'llm' AND a1.moved = 0 "
        "  AND a2.classification_source IN ('manual', 'correction') AND a2.moved = 1 "
        "  AND a1.created_at >= datetime('now', ?)", (window,)
    ).fetchall()

    same_folder = [r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]
    avg_conf_same = (
        sum(r["confidence"] for r in same_folder) / len(same_folder)
        if same_folder else 0
    )

    # Rule corrections (rule moved to A, user moved to B)
    rule_corrections = db.execute(
        "SELECT a1.email_id, a1.from_address, a1.subject, "
        "       a1.target_folder AS rule_folder, a1.confidence, a1.rule_id, "
        "       a2.target_folder AS corrected_folder, a2.id AS correction_audit_id "
        "FROM audit_log a1 "
        "JOIN runs r1 ON r1.run_id = a1.run_id "
        "JOIN audit_log a2 ON a1.email_id = a2.email_id "
        "JOIN runs r2 ON r2.run_id = a2.run_id "
        "WHERE r1.trigger != 'bootstrap' AND r2.trigger != 'bootstrap' "
        "  AND a1.classification_source = 'rule' AND a1.moved = 1 "
        "  AND a2.classification_source = 'correction' AND a2.moved = 1 "
        "  AND a1.target_folder != a2.target_folder "
        "  AND a1.created_at >= datetime('now', ?)", (window,)
    ).fetchall()

    # Rule confidence distribution
    rule_threshold = cfg.classification.thresholds.rule_move
    rule_buckets = [
        ("< 0.70", "r.confidence < 0.70"),
        ("0.70–0.79", "r.confidence >= 0.70 AND r.confidence < 0.80"),
        ("0.80–0.84", "r.confidence >= 0.80 AND r.confidence < 0.85"),
        ("0.85–0.89", "r.confidence >= 0.85 AND r.confidence < 0.90"),
        ("0.90–0.94", "r.confidence >= 0.90 AND r.confidence < 0.95"),
        ("0.95–1.00", "r.confidence >= 0.95"),
    ]
    rule_conf_dist = []
    for label, condition in rule_buckets:
        row = db.execute(
            f"SELECT "
            f"SUM(CASE WHEN r.active = 1 THEN 1 ELSE 0 END) as active, "
            f"SUM(CASE WHEN r.active = 0 THEN 1 ELSE 0 END) as inactive "
            f"FROM rules r WHERE {condition}"
        ).fetchone()
        a = row["active"] or 0
        i = row["inactive"] or 0
        if a > 0 or i > 0:
            is_threshold_bucket = label == "0.85–0.89" or label == "0.80–0.84"
            below_threshold = "< 0.70" in label or "0.70" in label or "0.80–0.84" in label
            rule_conf_dist.append({
                "label": label,
                "active": a,
                "inactive": i,
                "total": a + i,
                "is_threshold": is_threshold_bucket,
                "below_threshold": below_threshold,
            })

    total_rules = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    active_rules = db.execute("SELECT COUNT(*) FROM rules WHERE active = 1").fetchone()[0]
    inactive_rules = total_rules - active_rules

    # Recommendations
    llm_threshold = cfg.classification.thresholds.llm_move
    llm_rec = {"status": "ok", "current": llm_threshold, "message": "insufficient data to suggest changes"}
    if len(same_folder) > 3:
        suggested = round(avg_conf_same - 0.05, 2)
        llm_rec = {
            "status": "warn",
            "current": llm_threshold,
            "message": f"consider lowering to {suggested} — {len(same_folder)} emails were correctly classified but blocked by threshold (avg confidence {avg_conf_same:.2f})",
        }

    rule_rec = {"status": "ok", "current": rule_threshold, "message": f"{len(rule_corrections)} correction(s)"}
    if len(rule_corrections) > 0:
        rule_rec["status"] = "warn"

    return templates.TemplateResponse(
        request=request,
        name="analyze.html",
        context={
            "days": days,
            "total": total,
            "moved": moved,
            "skipped": skipped,
            "corrections": corrections,
            "error_rate": round(error_rate, 1),
            "sources": sources,
            "confidence_dist": confidence_dist,
            "skipped_then_sorted": skipped_then_sorted,
            "same_folder": same_folder,
            "avg_conf_same": round(avg_conf_same, 2),
            "rule_corrections": rule_corrections,
            "rule_conf_dist": rule_conf_dist,
            "total_rules": total_rules,
            "active_rules": active_rules,
            "inactive_rules": inactive_rules,
            "rule_threshold": rule_threshold,
            "llm_rec": llm_rec,
            "rule_rec": rule_rec,
            "nav_active": "analyze",
        },
    )
