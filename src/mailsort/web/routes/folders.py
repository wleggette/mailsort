"""Folders routes — folder tree with descriptions."""

from __future__ import annotations

import fnmatch
import json

from fastapi import APIRouter, Request

router = APIRouter(prefix="/folders")


@router.get("/")
async def folders_list(request: Request):
    db = request.state.db
    cfg = request.app.state.cfg
    templates = request.app.state.templates

    # Folder descriptions
    descriptions = db.execute(
        "SELECT * FROM folder_descriptions ORDER BY folder_path"
    ).fetchall()

    # Email counts per folder from audit_log
    counts = {}
    count_rows = db.execute(
        "SELECT target_folder, COUNT(*) as n FROM audit_log GROUP BY target_folder"
    ).fetchall()
    for row in count_rows:
        counts[row["target_folder"]] = row["n"]

    # Excluded patterns
    exclude_patterns = cfg.exclude_folder_patterns

    # Live folder paths (persisted by orchestrator/bootstrap on each run)
    live_folder_paths = set()
    row = db.execute("SELECT value FROM learner_state WHERE key = 'live_folder_paths'").fetchone()
    if row:
        try:
            live_folder_paths = set(json.loads(row["value"]))
        except (json.JSONDecodeError, TypeError):
            pass

    # Build folder data with exclusion and stale info
    folders = []
    for desc in descriptions:
        path = desc["folder_path"]
        excluded = any(fnmatch.fnmatch(path, pat) for pat in exclude_patterns)
        stale = bool(live_folder_paths) and path not in live_folder_paths
        depth = path.count("/")
        folders.append({
            "path": path,
            "description": desc["description"],
            "source": desc["source"],
            "email_count": counts.get(path, 0),
            "excluded": excluded,
            "stale": stale,
            "depth": depth,
            "updated_at": desc["updated_at"],
        })

    stale_count = sum(1 for f in folders if f["stale"])

    return templates.TemplateResponse(
        request=request,
        name="folders.html",
        context={
            "folders": folders,
            "exclude_patterns": exclude_patterns,
            "stale_count": stale_count,
            "nav_active": "folders",
        },
    )
