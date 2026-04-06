"""Folders routes — folder tree with descriptions and regeneration."""

from __future__ import annotations

import fnmatch
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from mailsort.classifier.descriptions import (
    regenerate_descriptions_for_folders,
    regenerate_folder_description,
)
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree

logger = logging.getLogger(__name__)

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

    # Flash-style message from redirect after regeneration
    msg = request.query_params.get("msg", "")

    return templates.TemplateResponse(
        request=request,
        name="folders.html",
        context={
            "folders": folders,
            "exclude_patterns": exclude_patterns,
            "stale_count": stale_count,
            "nav_active": "folders",
            "msg": msg,
        },
    )


@router.post("/regenerate")
async def regenerate_single(request: Request, folder_path: str = Form(...)):
    """Regenerate the description for a single folder."""
    cfg = request.app.state.cfg
    db = request.state.db

    if not cfg.anthropic_api_key:
        return RedirectResponse(
            "/folders/?msg=Error:+ANTHROPIC_API_KEY+not+configured",
            status_code=303,
        )

    try:
        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes, exclude_patterns=cfg.exclude_folder_patterns)

            mailbox_id = tree.id_for(folder_path)
            if not mailbox_id:
                return RedirectResponse(
                    f"/folders/?msg=Folder+not+found:+{folder_path}",
                    status_code=303,
                )

            email_ids = jmap.query_folder_emails(mailbox_id, limit=50)
            emails = jmap.get_emails(email_ids) if email_ids else []

        result = regenerate_folder_description(
            db, folder_path, emails,
            anthropic_api_key=cfg.anthropic_api_key,
            llm_model=cfg.classification.llm_model,
            folder_description_overrides=cfg.folder_description_overrides,
        )

        if result.success:
            msg = f"Regenerated+description+for+{folder_path}"
        elif result.skipped:
            msg = f"Skipped+{folder_path}:+{result.skip_reason}"
        else:
            msg = f"Error+regenerating+{folder_path}:+{result.error}"
    except Exception:
        logger.exception("Failed to regenerate description for %s", folder_path)
        msg = f"Error+regenerating+{folder_path}"

    return RedirectResponse(f"/folders/?msg={msg}", status_code=303)


@router.post("/regenerate-all")
async def regenerate_all(request: Request):
    """Regenerate descriptions for all non-overridden folders."""
    cfg = request.app.state.cfg
    db = request.state.db

    if not cfg.anthropic_api_key:
        return RedirectResponse(
            "/folders/?msg=Error:+ANTHROPIC_API_KEY+not+configured",
            status_code=303,
        )

    try:
        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes, exclude_patterns=cfg.exclude_folder_patterns)
            all_paths = sorted(tree.all_folder_paths())

            report = regenerate_descriptions_for_folders(
                db, jmap, tree, all_paths,
                anthropic_api_key=cfg.anthropic_api_key,
                llm_model=cfg.classification.llm_model,
                folder_description_overrides=cfg.folder_description_overrides,
            )

        msg = f"Regenerated+{report.succeeded},+skipped+{report.skipped},+failed+{report.failed}"
    except Exception:
        logger.exception("Failed to regenerate all descriptions")
        msg = "Error+during+bulk+regeneration"

    return RedirectResponse(f"/folders/?msg={msg}", status_code=303)
