"""Folders routes — folder tree with descriptions."""

from __future__ import annotations

import fnmatch

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

    # Build folder data with exclusion info
    folders = []
    for desc in descriptions:
        path = desc["folder_path"]
        excluded = any(fnmatch.fnmatch(path, pat) for pat in exclude_patterns)
        depth = path.count("/")
        folders.append({
            "path": path,
            "description": desc["description"],
            "source": desc["source"],
            "email_count": counts.get(path, 0),
            "excluded": excluded,
            "depth": depth,
            "updated_at": desc["updated_at"],
        })

    return templates.TemplateResponse("folders.html", {
        "request": request,
        "folders": folders,
        "exclude_patterns": exclude_patterns,
        "nav_active": "folders",
    })
