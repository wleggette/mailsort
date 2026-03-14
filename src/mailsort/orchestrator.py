"""Run orchestrator: executes a full classification-and-move pass.

Implements the Phase 3 pipeline:
  1. Query eligible inbox emails
  2. Fetch and extract features
  3. Classify each email (thread → rules → LLM)
  4. Build move decisions (confidence gate)
  5. Log all decisions to audit_log
  6. Execute batch move via JMAP
  7. Finalize run with summary counts
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

from mailsort.audit.learner import Learner
from mailsort.audit.writer import AuditWriter
from mailsort.classifier.descriptions import generate_descriptions_for_new_folders
from mailsort.classifier.features import (
    extract_features, load_contacts,
    refresh_contacts, should_refresh_contacts, mark_contacts_refreshed,
)
from mailsort.classifier.llm import LLMClassifier
from mailsort.classifier.pipeline import ClassificationPipeline
from mailsort.classifier.rules import RuleEngine
from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import MoveDecision
from mailsort.mover.mover import build_move_decision

logger = logging.getLogger(__name__)


def run_classification_pass(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    *,
    dry_run: bool = False,
    trigger: str = "scheduler",
) -> str:
    """Execute one full classification-and-move pass.

    Returns the run_id.
    """
    audit = AuditWriter(db)
    run_id = audit.start_run(trigger=trigger)

    try:
        seen, moved = _execute_run(cfg, db, jmap, tree, audit, run_id, dry_run=dry_run)
        audit.finish_run(run_id, status="completed", emails_seen=seen, emails_moved=moved)
    except Exception as e:
        logger.exception("Run %s failed", run_id)
        audit.finish_run(run_id, status="failed", error_summary=str(e)[:500])
        raise

    return run_id


def _execute_run(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    audit: AuditWriter,
    run_id: str,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Inner run logic. Returns (emails_seen, emails_moved)."""
    run_start = time.monotonic()
    mode = "DRY RUN" if dry_run else "live"
    short_id = run_id[:8]

    logger.info("── Run %s started (%s) ──", short_id, mode)

    # ------------------------------------------------------------------
    # 0. Learning
    # ------------------------------------------------------------------
    rule_engine = RuleEngine(db, cfg.classification.thresholds)
    learner = Learner(db, rule_engine, cfg.classification)

    try:
        all_inbox_ids = set(jmap.query_inbox_emails(
            inbox_id=tree.inbox_id, limit=500, filter_eligible=False,
        ))
    except Exception:
        logger.warning("Failed to query inbox for snapshot, skipping departure detection")
        all_inbox_ids = None

    manual_sorts = 0
    folder_scan_sorts = 0
    rules_adjusted = 0
    try:
        manual_sorts = learner.detect_manual_sorts(jmap, tree, run_id, current_inbox_ids=all_inbox_ids)
        folder_scan_sorts = learner.scan_folders_for_unknown_sorts(jmap, tree, run_id)
        rules_adjusted = learner.adjust_rule_confidence()
        learner.cleanup_old_snapshots()
    except Exception:
        logger.exception("Learning step failed, continuing with classification")

    logger.info(
        "Learning: %d manual sort(s) detected, %d folder scan finding(s), %d rule(s) adjusted",
        manual_sorts, folder_scan_sorts, rules_adjusted,
    )

    # Daily contact refresh
    try:
        if should_refresh_contacts(db, refresh_hours=cfg.scheduler.contacts_refresh_hours):
            count = refresh_contacts(db, jmap, cfg.known_contact_overrides)
            mark_contacts_refreshed(db)
            logger.info("Contacts: refreshed %d contact email(s)", count)
        else:
            logger.debug("Contacts: up to date")
    except Exception:
        logger.warning("Contacts: refresh failed, using cached data")

    # Generate descriptions for any new folders that don't have one yet
    try:
        new_descs = generate_descriptions_for_new_folders(
            db, tree.all_folder_paths(),
            anthropic_api_key=cfg.anthropic_api_key,
            llm_model=cfg.classification.llm_model,
            folder_description_overrides=cfg.folder_description_overrides,
        )
        if new_descs:
            logger.info("Generated %d new folder description(s)", new_descs)
    except Exception:
        logger.warning("Folder description generation failed, continuing")

    # ------------------------------------------------------------------
    # 1. Query eligible inbox emails
    # ------------------------------------------------------------------
    email_ids = jmap.query_inbox_emails(
        inbox_id=tree.inbox_id,
        min_age_hours=cfg.scheduler.min_age_hours,
        limit=cfg.scheduler.max_batch_size,
    )
    inbox_total = len(all_inbox_ids) if all_inbox_ids is not None else "?"
    logger.info("Inbox: %d eligible emails (%s total in inbox)", len(email_ids), inbox_total)

    # Save inbox snapshot for next run's departure detection
    if all_inbox_ids is not None:
        try:
            learner.save_inbox_snapshot(run_id, list(all_inbox_ids))
        except Exception:
            logger.warning("Failed to save inbox snapshot")

    if not email_ids:
        elapsed = time.monotonic() - run_start
        logger.info("── Run %s completed (%.1fs) — nothing to process ──", short_id, elapsed)
        return 0, 0

    # ------------------------------------------------------------------
    # 2. Fetch and extract features
    # ------------------------------------------------------------------
    emails = jmap.get_emails(email_ids)
    features_list = [extract_features(email) for email in emails]

    skip_senders = set(cfg.skip_senders)
    eligible = [f for f in features_list if f.from_address not in skip_senders]
    skipped_by_sender = len(features_list) - len(eligible)
    if skipped_by_sender:
        logger.debug("Filtered %d email(s) by skip_senders", skipped_by_sender)

    # ------------------------------------------------------------------
    # 3. Build pipeline
    # ------------------------------------------------------------------
    contacts = load_contacts(db)
    folder_descriptions = _load_folder_descriptions(cfg, db)

    llm_classifier: Optional[LLMClassifier] = None
    if cfg.anthropic_api_key:
        llm_classifier = LLMClassifier(
            api_key=cfg.anthropic_api_key,
            config=cfg.classification,
            valid_folder_paths=tree.all_folder_paths(),
        )

    pipeline = ClassificationPipeline(
        db=db,
        rule_engine=rule_engine,
        llm_classifier=llm_classifier,
        jmap_client=jmap,
        mailbox_tree=tree,
        contacts=contacts,
        folder_descriptions=folder_descriptions,
    )

    # ------------------------------------------------------------------
    # 4. Classify + build move decisions
    # ------------------------------------------------------------------
    decisions: list[MoveDecision] = []
    source_counts: Counter = Counter()

    for features in eligible:
        try:
            classification, skip_reason = pipeline.classify(features)
        except Exception:
            logger.exception("Classification failed for %s, skipping", features.email_id)
            classification, skip_reason = None, "classification_error"

        decision = build_move_decision(
            features=features,
            classification=classification,
            contacts=contacts,
            thresholds=cfg.classification.thresholds,
            skip_reason=skip_reason,
        )
        # Resolve folder_id
        if decision.should_move and decision.classification.folder_path != "INBOX":
            folder_id = tree.id_for(decision.classification.folder_path)
            if folder_id:
                decision.classification.folder_id = folder_id
            else:
                logger.warning(
                    "No mailbox ID for '%s', skipping %s",
                    decision.classification.folder_path, decision.email_id,
                )
                decision.should_move = False
                decision.skip_reason = "unknown_folder"

        # Track source for summary
        source_counts[decision.classification.source] += 1

        # DEBUG: per-email detail
        if decision.should_move:
            logger.debug(
                "  → %s: %s (%s, conf=%.2f) → %s",
                features.email_id[:12], features.from_address,
                decision.classification.source, decision.classification.confidence,
                decision.classification.folder_path,
            )
        else:
            logger.debug(
                "  ✕ %s: %s — skipped (%s)",
                features.email_id[:12], features.from_address,
                decision.skip_reason or "unknown",
            )

        decisions.append(decision)

    planned = [d for d in decisions if d.should_move]
    skipped_count = len(decisions) - len(planned)

    # Classification summary
    source_parts = ", ".join(f"{n} {src}" for src, n in source_counts.most_common())
    logger.info("Classification: %s, %d skipped", source_parts, skipped_count)

    # ------------------------------------------------------------------
    # 5. Execute moves
    # ------------------------------------------------------------------
    outcomes: dict[str, bool] = {}
    try:
        if planned and not dry_run:
            moves = [
                (d.email_id, d.classification.folder_id, d.features.current_mailbox_ids)
                for d in planned
                if d.classification.folder_id
            ]
            outcomes = jmap.move_emails(moves, inbox_id=tree.inbox_id)
            moved_count = sum(1 for v in outcomes.values() if v)
            failed_count = sum(1 for v in outcomes.values() if not v)
            logger.info("Moves: %d planned → %d moved, %d failed", len(planned), moved_count, failed_count)
        elif planned and dry_run:
            logger.info("Moves: %d planned → DRY RUN (not moved)", len(planned))
        else:
            logger.info("Moves: nothing to move")
    except Exception:
        logger.exception("JMAP move_emails failed — decisions will still be logged")
    finally:
        audit.log_decisions(run_id, decisions, outcomes)

    emails_moved = sum(1 for v in outcomes.values() if v)
    elapsed = time.monotonic() - run_start
    logger.info("── Run %s completed (%.1fs) ──", short_id, elapsed)

    return len(eligible), emails_moved


def _load_folder_descriptions(cfg: Config, db: Database) -> str:
    """Load folder descriptions from DB + config overrides, formatted for LLM prompt."""
    descriptions: dict[str, str] = {}
    rows = db.execute("SELECT folder_path, description FROM folder_descriptions").fetchall()
    for row in rows:
        descriptions[row["folder_path"]] = row["description"]
    descriptions.update(cfg.folder_description_overrides or {})

    if not descriptions:
        return "(no folder descriptions available)"

    return "\n".join(f"- {path}: {desc}" for path, desc in sorted(descriptions.items()))
