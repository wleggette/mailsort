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

import fcntl
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from mailsort.audit.learner import Learner, ManualSortCounts
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


@dataclass
class RunResult:
    """Result of a classification pass."""

    run_id: str
    dry_run: bool  # effective mode (may differ from requested)
    read_only_downgrade: bool  # True if auto-downgraded due to read-only token


def _acquire_run_lock(db_path: str) -> int | None:
    """Acquire an exclusive file lock for live runs.

    Returns the file descriptor on success, or None if another live run
    already holds the lock.  The lock auto-releases when the fd is closed
    or the process exits (even on SIGKILL).
    """
    lock_path = Path(db_path).parent / "mailsort.run.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _release_run_lock(fd: int) -> None:
    """Release the exclusive run lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def run_classification_pass(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    *,
    dry_run: bool = False,
    trigger: str = "scheduler",
) -> RunResult:
    """Execute one full classification-and-move pass.

    Returns a :class:`RunResult` with the run_id and effective mode.
    If the JMAP token is read-only and ``dry_run=False``, the run is
    automatically downgraded to dry-run mode.

    Callers are responsible for acquiring the run lock (via
    ``_acquire_run_lock``) before invoking this for live runs.
    Dry runs do not need a lock.
    """
    read_only_downgrade = False
    effective_dry_run = dry_run
    if not dry_run and jmap.is_read_only:
        logger.warning("JMAP token is read-only — auto-downgrading to dry-run mode")
        effective_dry_run = True
        read_only_downgrade = True

    audit = AuditWriter(db)
    run_id = audit.start_run(trigger=trigger, dry_run=effective_dry_run)

    try:
        seen, moved, move_error = _execute_run(
            cfg, db, jmap, tree, audit, run_id, dry_run=effective_dry_run,
        )
        if move_error:
            audit.finish_run(
                run_id, status="error", emails_seen=seen,
                emails_moved=moved, error_summary=move_error,
            )
        else:
            audit.finish_run(run_id, status="completed", emails_seen=seen, emails_moved=moved)
    except Exception as e:
        logger.exception("Run %s failed", run_id)
        audit.finish_run(run_id, status="failed", error_summary=str(e)[:500])
        raise

    return RunResult(
        run_id=run_id,
        dry_run=effective_dry_run,
        read_only_downgrade=read_only_downgrade,
    )


def _execute_run(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    audit: AuditWriter,
    run_id: str,
    *,
    dry_run: bool = False,
) -> tuple[int, int, str | None]:
    """Inner run logic. Returns (emails_seen, emails_moved, move_error)."""
    run_start = time.monotonic()
    mode = "DRY RUN" if dry_run else "live"
    short_id = run_id[:8]

    logger.info("── Run %s started (%s) ──", short_id, mode)

    # ------------------------------------------------------------------
    # 0. Learning
    # ------------------------------------------------------------------
    rule_engine = RuleEngine(db, cfg.classification.thresholds, record_hits=not dry_run)
    learner = Learner(db, rule_engine, cfg.classification)

    # Deactivate rules whose target folder no longer exists
    live_folders = tree.all_folder_paths()
    deactivated = rule_engine.reconcile_folders(live_folders)
    if deactivated:
        logger.info("Deactivated %d rule(s) for deleted folders", deactivated)

    # Persist live folder paths for the web UI's stale-folder detection
    try:
        db.execute(
            "INSERT OR REPLACE INTO learner_state (key, value) VALUES ('live_folder_paths', ?)",
            (json.dumps(sorted(live_folders)),),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to persist live folder paths")

    # Query ALL inbox emails (unfiltered) for snapshot diff and inbox total
    try:
        all_inbox_ids = set(jmap.query_inbox_emails(
            inbox_id=tree.inbox_id, limit=500, filter_eligible=False,
        ))
    except Exception:
        logger.warning("Failed to query inbox for snapshot, skipping departure detection")
        all_inbox_ids = None

    sort_counts = ManualSortCounts()
    folder_scan_sorts = 0
    rules_adjusted = 0
    try:
        sort_counts = learner.detect_manual_sorts(jmap, tree, run_id, current_inbox_ids=all_inbox_ids)
        folder_scan_sorts = learner.scan_folders_for_unknown_sorts(
            jmap, tree, run_id,
            interval_hours=cfg.scheduler.folder_scan_interval_hours,
        )
        sort_counts.from_other += folder_scan_sorts  # Cat 4 = from_other
        rules_adjusted = learner.compute_rule_confidence()
        learner.cleanup_old_snapshots()
    except Exception:
        logger.exception("Learning step failed, continuing with classification")

    logger.info("Learning: %d user sort(s) detected, %d rule(s) adjusted", sort_counts.total, rules_adjusted)
    if sort_counts.total:
        logger.info("  From inbox:     %d  (user manually sorted from inbox)", sort_counts.from_inbox)
        logger.info("  From other:     %d  (user moved a sorted email to a different folder)", sort_counts.from_other)

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
    # 1. Query ALL inbox emails for classification
    # ------------------------------------------------------------------
    inbox_ids = list(all_inbox_ids) if all_inbox_ids is not None else []
    if not inbox_ids:
        # Fallback: query inbox if snapshot query failed
        try:
            inbox_ids = jmap.query_inbox_emails(
                inbox_id=tree.inbox_id, limit=cfg.scheduler.max_batch_size,
                filter_eligible=False,
            )
        except Exception:
            logger.exception("Failed to query inbox emails")
            inbox_ids = []

    logger.info("Inbox: %d emails", len(inbox_ids))

    # Save inbox snapshot for next run's departure detection
    if all_inbox_ids is not None:
        try:
            learner.save_inbox_snapshot(run_id, list(all_inbox_ids))
        except Exception:
            logger.warning("Failed to save inbox snapshot")

    if not inbox_ids:
        elapsed = time.monotonic() - run_start
        logger.info("── Run %s completed (%.1fs) — nothing to process ──", short_id, elapsed)
        return 0, 0, None

    # ------------------------------------------------------------------
    # 2. Fetch and extract features
    # ------------------------------------------------------------------
    emails = jmap.get_emails(inbox_ids[:cfg.scheduler.max_batch_size])
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
    folder_descriptions = _load_folder_descriptions(cfg, db, tree.all_folder_paths())

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

        # Eligibility gate: classify all emails but only move eligible ones
        if decision.should_move:
            if "$seen" not in features.keywords:
                decision.should_move = False
                decision.skip_reason = "unread"
            elif "$flagged" in features.keywords:
                decision.should_move = False
                decision.skip_reason = "flagged"
            else:
                age_cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg.scheduler.min_age_minutes)
                if features.received_at.tzinfo is None:
                    received_utc = features.received_at.replace(tzinfo=timezone.utc)
                else:
                    received_utc = features.received_at
                if received_utc > age_cutoff:
                    decision.should_move = False
                    decision.skip_reason = "too_new"

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
    left_in_inbox = [d for d in decisions if not d.should_move]

    # Classification summary
    source_parts = "  ".join(f"{src}: {n}" for src, n in source_counts.most_common())
    logger.info("Classification: %d emails", len(decisions))
    logger.info("  %s", source_parts)

    # Outcome breakdown by skip reason
    move_by_source: Counter = Counter()
    for d in planned:
        move_by_source[d.classification.source] += 1
    move_parts = ", ".join(f"{src}: {n}" for src, n in move_by_source.most_common())

    skip_reasons: Counter = Counter()
    for d in left_in_inbox:
        skip_reasons[d.skip_reason or "unknown"] += 1

    # ------------------------------------------------------------------
    # 5. Execute moves
    # ------------------------------------------------------------------
    outcomes: dict[str, bool] = {}
    move_error: str | None = None
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
            logger.info("Outcome:")
            logger.info("  Moved:          %d  (%s)", moved_count, move_parts)
            if failed_count:
                logger.info("  Move failed:    %d", failed_count)
        elif planned and dry_run:
            logger.info("Outcome:")
            logger.info("  Would move:     %d  (%s)", len(planned), move_parts)
        else:
            logger.info("Outcome:")

        # Log each skip reason on its own line
        for reason, count in skip_reasons.most_common():
            logger.info("  %-16s %d", reason.replace("_", " ").title() + ":", count)
    except Exception as exc:
        logger.exception("JMAP move_emails failed — decisions will still be logged")
        move_error = str(exc)[:500]
        for d in planned:
            d.skip_reason = "move_failed"
    finally:
        audit.log_decisions(run_id, decisions, outcomes)

    emails_moved = sum(1 for v in outcomes.values() if v)
    elapsed = time.monotonic() - run_start
    logger.info("── Run %s completed (%.1fs) ──", short_id, elapsed)

    return len(eligible), emails_moved, move_error


def _load_folder_descriptions(cfg: Config, db: Database, valid_paths: set[str]) -> str:
    """Load folder descriptions from DB + config overrides, formatted for LLM prompt.

    Only descriptions for paths in *valid_paths* (from the mailbox tree) are
    included.  Config overrides are normalised — if a key doesn't match a valid
    path directly, we try prepending ``INBOX/`` before discarding it.
    """
    descriptions: dict[str, str] = {}

    # DB descriptions (already INBOX/-prefixed)
    rows = db.execute("SELECT folder_path, description FROM folder_descriptions").fetchall()
    for row in rows:
        path = row["folder_path"]
        if path in valid_paths:
            descriptions[path] = row["description"]

    # Config overrides — normalise path format
    for path, desc in (cfg.folder_description_overrides or {}).items():
        normalised = _normalise_folder_path(path, valid_paths)
        if normalised:
            descriptions[normalised] = desc

    if not descriptions:
        return "(no folder descriptions available)"

    return "\n".join(f"- {path}: {desc}" for path, desc in sorted(descriptions.items()))


def _normalise_folder_path(path: str, valid_paths: set[str]) -> str | None:
    """Return the canonical form of *path* if it matches a valid path, else None.

    Tries the path as-is first, then with an ``INBOX/`` prefix.
    """
    if path in valid_paths:
        return path
    prefixed = f"INBOX/{path}"
    if prefixed in valid_paths:
        return prefixed
    return None
