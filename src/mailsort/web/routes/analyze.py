"""Threshold analysis route — interactive version of `mailsort analyze`."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request

from mailsort.db.database import Database

router = APIRouter()


# ---------------------------------------------------------------------------
# Query helpers — extracted for testability (Layer 2 tests call these directly)
# ---------------------------------------------------------------------------

def _dedup_cte() -> str:
    """Return the dedup CTE SQL fragment.

    Keeps only the most recent audit row per email_id, excluding bootstrap,
    dry runs, manual, and system rows.  Callers must supply two window params
    (outer + subquery) via ``cte_params``.
    """
    return (
        "WITH latest AS ("
        "  SELECT a.* FROM audit_log a"
        "  JOIN runs r ON r.run_id = a.run_id"
        "  WHERE r.trigger != 'bootstrap' AND r.dry_run = 0"
        "    AND a.created_at >= datetime('now', ?)"
        "    AND a.classification_source NOT IN ('manual', 'system')"
        "    AND a.id = ("
        "      SELECT MAX(a2.id) FROM audit_log a2"
        "      JOIN runs r2 ON r2.run_id = a2.run_id"
        "      WHERE a2.email_id = a.email_id"
        "        AND r2.trigger != 'bootstrap' AND r2.dry_run = 0"
        "        AND a2.created_at >= datetime('now', ?)"
        "        AND a2.classification_source NOT IN ('manual', 'system')"
        "    )"
        ") "
    )


def get_skipped_then_sorted(db: Database, window: str) -> list[dict]:
    """Deduplicated skipped-then-sorted: one row per email_id.

    Keeps only the latest LLM skip row per email before joining to the
    manual/correction row.  Fixes the N×1 cross-join inflation bug.
    """
    rows = db.execute(
        "WITH latest_skip AS ("
        "  SELECT a.* FROM audit_log a"
        "  JOIN runs r ON r.run_id = a.run_id"
        "  WHERE r.trigger != 'bootstrap' AND r.dry_run = 0"
        "    AND a.classification_source = 'llm' AND a.moved = 0"
        "    AND a.created_at >= datetime('now', ?)"
        "    AND a.id = ("
        "      SELECT MAX(a2.id) FROM audit_log a2"
        "      JOIN runs r2 ON r2.run_id = a2.run_id"
        "      WHERE a2.email_id = a.email_id"
        "        AND r2.trigger != 'bootstrap' AND r2.dry_run = 0"
        "        AND a2.classification_source = 'llm' AND a2.moved = 0"
        "        AND a2.created_at >= datetime('now', ?)"
        "    )"
        ") "
        "SELECT s.email_id, s.id AS skip_audit_id, s.from_address, s.subject,"
        "       s.target_folder AS llm_folder, s.confidence, s.skip_reason,"
        "       m.target_folder AS manual_folder "
        "FROM latest_skip s "
        "JOIN audit_log m ON s.email_id = m.email_id "
        "JOIN runs rm ON rm.run_id = m.run_id "
        "WHERE rm.trigger != 'bootstrap'"
        "  AND m.classification_source IN ('manual', 'correction') AND m.moved = 1",
        (window, window),
    ).fetchall()
    return [dict(r) for r in rows]


def get_llm_corrections(db: Database, window: str) -> list[dict]:
    """LLM-moved emails later corrected by the user to a different folder."""
    rows = db.execute(
        "SELECT DISTINCT a1.email_id, a1.id AS move_audit_id,"
        "       a1.from_address, a1.subject,"
        "       a1.target_folder AS llm_folder, a1.confidence,"
        "       a2.target_folder AS corrected_folder,"
        "       a2.id AS correction_audit_id "
        "FROM audit_log a1 "
        "JOIN runs r1 ON r1.run_id = a1.run_id "
        "JOIN audit_log a2 ON a1.email_id = a2.email_id "
        "JOIN runs r2 ON r2.run_id = a2.run_id "
        "WHERE r1.trigger != 'bootstrap' AND r2.trigger != 'bootstrap'"
        "  AND a1.classification_source = 'llm' AND a1.moved = 1"
        "  AND a2.classification_source = 'correction' AND a2.moved = 1"
        "  AND a1.target_folder != a2.target_folder"
        "  AND a1.created_at >= datetime('now', ?)",
        (window,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_folder_gap_cards(skipped_then_sorted: list[dict]) -> list[dict]:
    """Card 1: group wrong-folder skipped-then-sorted by destination folder."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in skipped_then_sorted:
        if row["llm_folder"] != row["manual_folder"]:
            groups[row["manual_folder"]].append(row)

    cards = []
    for folder, emails in sorted(groups.items(), key=lambda x: -len(x[1])):
        llm_counts: dict[str, int] = defaultdict(int)
        for e in emails:
            llm_counts[e["llm_folder"]] += 1
        llm_summary = sorted(llm_counts.items(), key=lambda x: -x[1])
        cards.append({
            "folder": folder,
            "count": len(emails),
            "llm_summary": llm_summary,
            "emails": emails,
        })
    return cards


def get_known_contact_cards(
    db: Database, window: str, min_skips: int, coherence_threshold: float,
) -> list[dict]:
    """Card 2: known contact sorting breakdown per sender."""
    cte = _dedup_cte()
    cte_params = (window, window)

    # Senders with enough known-contact threshold blocks
    sender_rows = db.execute(
        f"{cte} SELECT from_address, COUNT(*) as cnt "
        "FROM latest "
        "WHERE skip_reason = 'below_threshold_known_contact' "
        "GROUP BY from_address HAVING cnt >= ? "
        "ORDER BY cnt DESC",
        (*cte_params, min_skips),
    ).fetchall()

    cards = []
    for sr in sender_rows:
        sender = sr["from_address"]
        blocked_count = sr["cnt"]

        # Sorting mechanism counts for this sender
        thread_count = db.execute(
            f"{cte} SELECT COUNT(*) FROM latest "
            "WHERE from_address = ? AND classification_source = 'thread'",
            (*cte_params, sender),
        ).fetchone()[0]
        rule_count = db.execute(
            f"{cte} SELECT COUNT(*) FROM latest "
            "WHERE from_address = ? AND classification_source = 'rule'",
            (*cte_params, sender),
        ).fetchone()[0]
        llm_moved = db.execute(
            f"{cte} SELECT COUNT(*) FROM latest "
            "WHERE from_address = ? AND classification_source = 'llm' AND moved = 1",
            (*cte_params, sender),
        ).fetchone()[0]

        # Coherence: folder distribution for all moved emails from this sender
        coherence_rows = db.execute(
            "SELECT target_folder, COUNT(*) as cnt "
            "FROM audit_log WHERE from_address = ? AND moved = 1 "
            "  AND created_at >= datetime('now', ?) "
            "GROUP BY target_folder ORDER BY cnt DESC",
            (sender, window),
        ).fetchall()
        coh_total = sum(r["cnt"] for r in coherence_rows)
        coherence = []
        top_pct = 0
        for cr in coherence_rows:
            pct = round(cr["cnt"] / coh_total * 100) if coh_total > 0 else 0
            coherence.append({"folder": cr["target_folder"], "pct": pct, "count": cr["cnt"]})
            if not top_pct:
                top_pct = pct

        # Coherence note
        coherence_note = None
        has_active_rule = db.execute(
            "SELECT COUNT(*) FROM rules "
            "WHERE active = 1 AND rule_type = 'exact_sender' "
            "  AND condition_value COLLATE NOCASE = ?",
            (sender,),
        ).fetchone()[0] > 0
        if top_pct < coherence_threshold * 100:
            coherence_note = (
                f"Coherence ({top_pct}%) is below the auto-rule threshold "
                f"({int(coherence_threshold * 100)}%)."
            )
        elif not has_active_rule:
            coherence_note = (
                f"Coherence ({top_pct}%) meets the threshold — "
                f"a rule may be created soon."
            )

        # Contact relationship
        contact = db.execute(
            "SELECT relationship FROM contacts WHERE email_address = ?",
            (sender,),
        ).fetchone()
        relationship = contact["relationship"] if contact else None

        # Threshold-blocked email details (join to manual/correction for user folder)
        blocked_emails = db.execute(
            "WITH blocked AS ("
            "  SELECT a.* FROM audit_log a"
            "  JOIN runs r ON r.run_id = a.run_id"
            "  WHERE r.trigger != 'bootstrap' AND r.dry_run = 0"
            "    AND a.classification_source = 'llm' AND a.moved = 0"
            "    AND a.skip_reason = 'below_threshold_known_contact'"
            "    AND a.from_address = ?"
            "    AND a.created_at >= datetime('now', ?)"
            "    AND a.id = ("
            "      SELECT MAX(a2.id) FROM audit_log a2"
            "      JOIN runs r2 ON r2.run_id = a2.run_id"
            "      WHERE a2.email_id = a.email_id"
            "        AND r2.trigger != 'bootstrap' AND r2.dry_run = 0"
            "        AND a2.classification_source = 'llm' AND a2.moved = 0"
            "        AND a2.created_at >= datetime('now', ?)"
            "    )"
            ") "
            "SELECT b.email_id, b.id AS audit_id, b.subject,"
            "       b.target_folder AS llm_folder, b.confidence,"
            "       m.target_folder AS manual_folder "
            "FROM blocked b "
            "LEFT JOIN audit_log m ON b.email_id = m.email_id"
            "  AND m.classification_source IN ('manual', 'correction')"
            "  AND m.moved = 1 "
            "ORDER BY b.created_at DESC",
            (sender, window, window),
        ).fetchall()

        cards.append({
            "sender": sender,
            "relationship": relationship,
            "thread_count": thread_count,
            "rule_count": rule_count,
            "llm_moved_count": llm_moved,
            "blocked_count": blocked_count,
            "coherence": coherence,
            "coherence_top_pct": top_pct,
            "coherence_threshold_pct": int(coherence_threshold * 100),
            "coherence_note": coherence_note,
            "has_active_rule": has_active_rule,
            "blocked_emails": [dict(r) for r in blocked_emails],
        })
    return cards


def get_learning_effectiveness(db: Database, window: str) -> dict:
    """Card 3: rule learning stats."""
    total_auto = db.execute(
        "SELECT COUNT(*) FROM rules WHERE source = 'auto'"
    ).fetchone()[0]
    total_sorted = db.execute(
        "SELECT COUNT(DISTINCT email_id) FROM audit_log "
        "WHERE classification_source = 'rule' AND moved = 1"
    ).fetchone()[0]
    recent_rules = db.execute(
        "SELECT id, rule_type, condition_value, target_folder_path,"
        "       active, created_at "
        "FROM rules WHERE source = 'auto'"
        "  AND created_at >= datetime('now', ?) "
        "ORDER BY created_at DESC",
        (window,),
    ).fetchall()

    rules_with_hits = []
    for r in recent_rules:
        hits = db.execute(
            "SELECT COUNT(DISTINCT email_id) FROM audit_log "
            "WHERE rule_id = ? AND moved = 1",
            (r["id"],),
        ).fetchone()[0]
        rules_with_hits.append({
            "id": r["id"],
            "rule_type": r["rule_type"],
            "condition_value": r["condition_value"],
            "target_folder": r["target_folder_path"],
            "active": r["active"],
            "created_at": r["created_at"],
            "emails_sorted": hits,
        })
    rules_with_hits.sort(key=lambda x: -x["emails_sorted"])

    return {
        "total_auto_rules": total_auto,
        "total_emails_sorted": total_sorted,
        "recent_rules_count": len(recent_rules),
        "recent_rules": rules_with_hits,
    }


def get_eligibility_gated(db: Database, window: str) -> dict:
    """Card 4: eligibility gate breakdown."""
    cte = _dedup_cte()
    cte_params = (window, window)
    rows = db.execute(
        f"{cte} SELECT skip_reason, COUNT(*) as cnt FROM latest "
        "WHERE skip_reason IN ('unread', 'flagged', 'too_new') "
        "GROUP BY skip_reason",
        cte_params,
    ).fetchall()
    counts = {r["skip_reason"]: r["cnt"] for r in rows}
    flagged = counts.get("flagged", 0)
    unread = counts.get("unread", 0)
    too_new = counts.get("too_new", 0)
    return {
        "total": flagged + unread + too_new,
        "flagged": flagged,
        "unread": unread,
        "too_new": too_new,
    }


def build_llm_accuracy(
    sources: list[dict],
    llm_corrections: list[dict],
    skipped_then_sorted: list[dict],
    eligibility: dict,
) -> dict:
    """Card 5: LLM accuracy tree and precision metrics."""
    # LLM totals from source breakdown
    llm_src = next((s for s in sources if s["name"] == "llm"), None)
    llm_total = llm_src["count"] if llm_src else 0
    llm_moved = llm_src["moved"] if llm_src else 0
    llm_corrected = len(llm_corrections)
    moved_correctly = llm_moved - llm_corrected
    llm_skipped = llm_total - llm_moved

    same_folder = [r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]
    diff_folder = [r for r in skipped_then_sorted if r["llm_folder"] != r["manual_folder"]]
    later_sorted = len(skipped_then_sorted)
    agreed = len(same_folder)
    disagreed = len(diff_folder)
    still_pending = llm_skipped - later_sorted

    # Precision metrics
    total_with_outcomes = llm_moved + later_sorted
    correct_outcomes = moved_correctly + disagreed
    se = round(correct_outcomes / total_with_outcomes * 100) if total_with_outcomes > 0 else None
    mp = round(moved_correctly / llm_moved * 100) if llm_moved > 0 else None
    tp = round(disagreed / later_sorted * 100) if later_sorted > 0 else None

    return {
        "llm_total": llm_total,
        "llm_moved": llm_moved,
        "llm_corrected": llm_corrected,
        "moved_correctly": moved_correctly,
        "llm_skipped": llm_skipped,
        "later_sorted": later_sorted,
        "agreed": agreed,
        "disagreed": disagreed,
        "still_pending": still_pending,
        "system_effectiveness": se,
        "move_precision": mp,
        "threshold_precision": tp,
        "se_n": correct_outcomes,
        "se_d": total_with_outcomes,
        "mp_n": moved_correctly,
        "mp_d": llm_moved,
        "tp_n": disagreed,
        "tp_d": later_sorted,
    }


def _metric_color(pct: int | None) -> str:
    """Return Tailwind text color class for a percentage metric."""
    if pct is None:
        return "text-gray-400"
    if pct >= 80:
        return "text-green-600"
    if pct >= 50:
        return "text-amber-600"
    return "text-red-600"


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

@router.get("/analyze")
async def analyze(request: Request, days: int = 30):
    db = request.state.db
    templates = request.app.state.templates
    cfg = request.app.state.cfg

    window = f"-{days} days"
    cte = _dedup_cte()
    cte_params = (window, window)

    # --- Existing: overall counts ---
    total = db.execute(
        f"{cte} SELECT COUNT(*) FROM latest", cte_params
    ).fetchone()[0]
    moved = db.execute(
        f"{cte} SELECT COUNT(*) FROM latest WHERE moved = 1", cte_params
    ).fetchone()[0]
    skipped = total - moved

    # --- Existing: source breakdown ---
    source_rows = db.execute(
        f"{cte} SELECT classification_source as source, COUNT(*) as n, "
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

    # --- Existing: corrections ---
    corrections = db.execute(
        "SELECT COUNT(DISTINCT a.email_id) FROM audit_log a "
        "JOIN runs r ON r.run_id = a.run_id "
        "WHERE r.trigger != 'bootstrap' AND a.classification_source = 'correction' "
        "  AND a.created_at >= datetime('now', ?)", (window,)
    ).fetchone()[0]
    error_rate = corrections / moved * 100 if moved > 0 else 0.0

    # --- Existing: LLM confidence distribution ---
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
            f"{cte} SELECT "
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
                "is_threshold": label == "0.80\u20130.89",
            })

    # --- Existing: rule corrections ---
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

    # --- Existing: rule confidence distribution ---
    rule_threshold = cfg.classification.thresholds.rule_move
    rule_buckets = [
        ("< 0.70", "r.confidence < 0.70"),
        ("0.70\u20130.79", "r.confidence >= 0.70 AND r.confidence < 0.80"),
        ("0.80\u20130.84", "r.confidence >= 0.80 AND r.confidence < 0.85"),
        ("0.85\u20130.89", "r.confidence >= 0.85 AND r.confidence < 0.90"),
        ("0.90\u20130.94", "r.confidence >= 0.90 AND r.confidence < 0.95"),
        ("0.95\u20131.00", "r.confidence >= 0.95"),
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
            is_threshold_bucket = label in ("0.85\u20130.89", "0.80\u20130.84")
            below_threshold = "< 0.70" in label or "0.70" in label or "0.80\u20130.84" in label
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

    # --- Existing: recommendations ---
    skipped_then_sorted = get_skipped_then_sorted(db, window)
    same_folder = [r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]
    avg_conf_same = (
        sum(r["confidence"] for r in same_folder) / len(same_folder)
        if same_folder else 0
    )

    llm_threshold = cfg.classification.thresholds.llm_move
    llm_rec = {"status": "ok", "current": llm_threshold, "message": "insufficient data to suggest changes"}
    if len(same_folder) > 3:
        suggested = round(avg_conf_same - 0.05, 2)
        llm_rec = {
            "status": "warn",
            "current": llm_threshold,
            "message": (
                f"consider lowering to {suggested} — {len(same_folder)} emails were correctly "
                f"classified but blocked by threshold (avg confidence {avg_conf_same:.2f})"
            ),
        }

    rule_rec = {"status": "ok", "current": rule_threshold, "message": f"{len(rule_corrections)} correction(s)"}
    if len(rule_corrections) > 0:
        rule_rec["status"] = "warn"

    # --- NEW: Card data ---
    llm_corrections = get_llm_corrections(db, window)
    folder_gaps = build_folder_gap_cards(skipped_then_sorted)
    known_contacts = get_known_contact_cards(
        db, window,
        min_skips=cfg.classification.min_known_contact_skips,
        coherence_threshold=cfg.classification.auto_rule_domain_coherence,
    )
    learning = get_learning_effectiveness(db, window)
    eligibility = get_eligibility_gated(db, window)
    llm_accuracy = build_llm_accuracy(sources, llm_corrections, skipped_then_sorted, eligibility)

    return templates.TemplateResponse(
        request=request,
        name="analyze.html",
        context={
            # Existing
            "days": days,
            "total": total,
            "moved": moved,
            "skipped": skipped,
            "corrections": corrections,
            "error_rate": round(error_rate, 1),
            "sources": sources,
            "confidence_dist": confidence_dist,
            "llm_max_total": max((b["total"] for b in confidence_dist), default=1),
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
            # New cards
            "folder_gaps": folder_gaps,
            "known_contacts": known_contacts,
            "learning": learning,
            "eligibility": eligibility,
            "llm_accuracy": llm_accuracy,
            "metric_color": _metric_color,
        },
    )
