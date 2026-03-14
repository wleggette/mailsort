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
from typing import Optional

from mailsort.audit.learner import Learner
from mailsort.audit.writer import AuditWriter
from mailsort.classifier.features import extract_features, load_contacts
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

    # 0. Learning: detect manual sorts, inbox departures, daily folder scan,
    #    and adjust stale rule confidence. Runs before classification so
    #    newly created rules are available for the current batch.
    rule_engine = RuleEngine(db, cfg.classification.thresholds)
    learner = Learner(db, rule_engine, cfg.classification)

    # Query ALL current inbox email IDs (no age/read filter) for snapshot diff.
    # This is a broader query than the eligibility query — we want to track
    # every email in the inbox so we can detect when ANY of them depart.
    try:
        all_inbox_ids = set(jmap.query_inbox_emails(
            inbox_id=tree.inbox_id, limit=500, filter_eligible=False,
        ))
    except Exception:
        logger.exception("Failed to query inbox for snapshot, skipping departure detection")
        all_inbox_ids = None

    try:
        learner.detect_manual_sorts(jmap, tree, run_id, current_inbox_ids=all_inbox_ids)
        learner.scan_folders_for_unknown_sorts(jmap, tree, run_id)
        learner.adjust_rule_confidence()
        learner.cleanup_old_snapshots()
    except Exception:
        logger.exception("Learning step failed, continuing with classification")

    # 1. Query eligible inbox emails (read, unflagged, old enough)
    email_ids = jmap.query_inbox_emails(
        inbox_id=tree.inbox_id,
        min_age_hours=cfg.scheduler.min_age_hours,
        limit=cfg.scheduler.max_batch_size,
    )
    logger.info("Found %d eligible inbox emails", len(email_ids))

    # Save inbox snapshot for next run's departure detection (Option C).
    # Uses the broader all_inbox_ids set, not just the eligible ones.
    if all_inbox_ids is not None:
        try:
            learner.save_inbox_snapshot(run_id, list(all_inbox_ids))
        except Exception:
            logger.exception("Failed to save inbox snapshot")

    if not email_ids:
        return 0, 0

    # 2. Fetch full email objects and extract features
    emails = jmap.get_emails(email_ids)
    features_list = [extract_features(email) for email in emails]

    # Filter skip_senders
    skip_senders = set(cfg.skip_senders)
    eligible = [f for f in features_list if f.from_address not in skip_senders]
    logger.info("%d emails eligible after skip_senders filter", len(eligible))

    # 3. Load contacts + folder descriptions, build pipeline
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

    # 4. Classify + build move decisions
    #    Per-email isolation: one classification failure doesn't block the batch.
    decisions: list[MoveDecision] = []
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
        decisions.append(decision)

    planned = [d for d in decisions if d.should_move]
    logger.info("Classification done: %d to move, %d to skip", len(planned), len(decisions) - len(planned))

    # 5. Execute moves (unless dry-run)
    #    Wrapped in try/except so audit logging always happens even if the
    #    JMAP call crashes. Decisions are logged in the finally block.
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
            logger.info("Moves executed: %d moved, %d failed", moved_count, failed_count)
        elif planned and dry_run:
            logger.info("Dry run — skipping %d moves", len(planned))
    except Exception:
        logger.exception("JMAP move_emails failed — decisions will still be logged")
    finally:
        # 6. Always log decisions, even if the move call crashed.
        #    If move_emails threw, outcomes is empty so all planned decisions
        #    are logged as moved=False — accurate since nothing was confirmed moved.
        audit.log_decisions(run_id, decisions, outcomes)

    emails_moved = sum(1 for v in outcomes.values() if v)
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
