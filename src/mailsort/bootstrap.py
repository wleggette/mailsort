"""Bootstrap: scan existing folders to seed rules and folder descriptions.

Reads recent emails from each non-system folder, records them as evidence
in audit_log, then runs the same auto-rule creation logic used during live
learning. This ensures bootstrap rules pass the same coherence checks as
rules created from ongoing manual sorts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from mailsort.audit.learner import Learner
from mailsort.audit.writer import AuditWriter
from mailsort.classifier.features import extract_features, refresh_contacts
from mailsort.classifier.rules import RuleEngine
from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import EmailFeatures

logger = logging.getLogger(__name__)

# Full properties including list-id (needed for the most reliable rule type).
_FULL_PROPERTIES = [
    "id", "threadId", "mailboxIds", "from", "to",
    "subject", "receivedAt", "keywords", "preview",
    "header:list-id:asText",
]

# Fallback for folders where header properties cause invalidArguments.
_MINIMAL_PROPERTIES = [
    "id", "threadId", "mailboxIds", "from", "to",
    "subject", "receivedAt", "keywords", "preview",
]


@dataclass
class BootstrapReport:
    folders_scanned: int = 0
    emails_sampled: int = 0
    rules_created: int = 0
    descriptions_generated: int = 0
    contacts_imported: int = 0
    emails_matched_by_rules: int = 0
    emails_unmatched: int = 0
    errors: list[str] = field(default_factory=list)


def run_bootstrap(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    *,
    max_per_folder: int = 50,
) -> BootstrapReport:
    """Scan existing folders and seed rules + descriptions.

    Returns a BootstrapReport summarizing what was created.
    """
    audit = AuditWriter(db)
    run_id = audit.start_run(trigger="bootstrap")
    report = BootstrapReport()

    rule_engine = RuleEngine(db, cfg.classification.thresholds)
    learner = Learner(db, rule_engine, cfg.classification)

    try:
        # Phase 1: Collect evidence from all folders
        logger.info("Phase 1/4: Scanning folders for email evidence...")
        _collect_evidence(
            cfg, db, jmap, tree, run_id, report,
            max_per_folder=max_per_folder,
        )
        logger.info(
            "Phase 1/4 complete: %d folders scanned, %d emails sampled",
            report.folders_scanned, report.emails_sampled,
        )

        # Phase 2: Evaluate candidate rules using learner's coherence checks
        logger.info("Phase 2/4: Creating rules from evidence...")
        _create_rules_from_evidence(db, learner, report)
        logger.info("Phase 2/4 complete: %d rules created", report.rules_created)

        # Phase 3: Import contacts from Fastmail
        logger.info("Phase 3/4: Importing contacts from Fastmail...")
        report.contacts_imported = refresh_contacts(db, jmap, cfg.known_contact_overrides)
        logger.info("Phase 3/4 complete: %d contacts imported", report.contacts_imported)

        # Phase 4: Calculate rule coverage
        logger.info("Phase 4/4: Calculating rule coverage...")
        _calculate_coverage(db, rule_engine, report)

        audit.finish_run(
            run_id,
            status="completed",
            emails_seen=report.emails_sampled,
            emails_moved=0,
        )
    except Exception as e:
        logger.exception("Bootstrap failed")
        audit.finish_run(run_id, status="failed", error_summary=str(e)[:500])
        report.errors.append(str(e))

    _log_report(report)
    return report


def _collect_evidence(
    cfg: Config,
    db: Database,
    jmap: JMAPClient,
    tree: MailboxTree,
    run_id: str,
    report: BootstrapReport,
    *,
    max_per_folder: int = 50,
) -> None:
    """Scan each target folder and record emails as bootstrap evidence."""
    # Pre-load known email_ids so re-running bootstrap doesn't duplicate evidence
    known_ids = {
        row["email_id"]
        for row in db.execute("SELECT DISTINCT email_id FROM audit_log").fetchall()
    }

    folder_paths = sorted(tree.all_folder_paths())
    total_folders = len(folder_paths)

    for idx, folder_path in enumerate(folder_paths, 1):
        mailbox_id = tree.id_for(folder_path)
        if not mailbox_id:
            continue

        try:
            email_ids = jmap.query_folder_emails(mailbox_id, limit=max_per_folder)
            if not email_ids:
                continue
        except Exception as e:
            logger.warning("Skipping folder %s (query failed): %s", folder_path, e)
            report.errors.append(f"Skipping {folder_path}")
            continue

        # Try full properties first (includes list-id for rule creation),
        # fall back to minimal if the folder doesn't support header properties
        # (e.g., read-only tokens may not support header:* access).
        try:
            emails = jmap.get_emails(email_ids, properties=_FULL_PROPERTIES)
        except Exception:
            try:
                emails = jmap.get_emails(email_ids, properties=_MINIMAL_PROPERTIES)
                logger.debug("Folder %s: fell back to minimal properties", folder_path)
            except Exception as e:
                logger.warning("Skipping folder %s (fetch failed): %s", folder_path, e)
                report.errors.append(f"Skipping {folder_path}")
                continue

        report.folders_scanned += 1
        logger.info("  [%d/%d] %s — %d emails", idx, total_folders, folder_path, len(emails))

        for email in emails:
            features = extract_features(email)
            if features.email_id in known_ids:
                continue
            try:
                db.execute(
                    "INSERT INTO audit_log "
                    "(run_id, email_id, thread_id, from_address, from_domain, "
                    " subject, list_id, source_folder, target_folder, confidence, "
                    " classification_source, moved, skip_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        features.email_id,
                        features.thread_id,
                        features.from_address,
                        features.from_domain,
                        features.subject,
                        features.list_id,
                        folder_path,
                        folder_path,
                        1.0,
                        "manual",
                        True,
                        None,
                    ),
                )
                known_ids.add(features.email_id)
                report.emails_sampled += 1
            except Exception:
                logger.debug("Failed audit insert for %s", features.email_id)

        db.commit()

        # Generate folder description if one doesn't exist and no manual override
        if folder_path not in (cfg.folder_description_overrides or {}):
            _maybe_generate_description(db, folder_path, emails)
            report.descriptions_generated += 1

        logger.debug(
            "Scanned %s: %d emails sampled", folder_path, len(emails),
        )


def _create_rules_from_evidence(
    db: Database,
    learner: Learner,
    report: BootstrapReport,
) -> None:
    """Query the bootstrap evidence and create rules using learner's logic."""
    # Find distinct (from_address, from_domain, list_id, target_folder) combos
    rows = db.execute(
        """SELECT DISTINCT from_address, from_domain, list_id, target_folder
           FROM audit_log
           WHERE classification_source = 'manual' AND moved = 1"""
    ).fetchall()

    seen_rules: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["from_address"] or "", row["target_folder"])
        if key in seen_rules:
            continue
        seen_rules.add(key)

        rule_id = learner.maybe_create_rule(
            from_address=row["from_address"],
            from_domain=row["from_domain"],
            list_id=row["list_id"],
            target_folder=row["target_folder"],
        )
        if rule_id:
            report.rules_created += 1


def _maybe_generate_description(
    db: Database,
    folder_path: str,
    emails: list,
) -> None:
    """Store a simple description based on the folder name if none exists.

    Full LLM-based description generation is deferred to a future phase.
    For now, use the folder path as a placeholder description.
    """
    existing = db.execute(
        "SELECT 1 FROM folder_descriptions WHERE folder_path = ?",
        (folder_path,),
    ).fetchone()
    if existing:
        return

    # Use the leaf folder name as a basic description
    leaf = folder_path.rsplit("/", 1)[-1] if "/" in folder_path else folder_path
    description = f"Emails filed under {leaf}"

    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) VALUES (?, ?, 'auto')",
        (folder_path, description),
    )
    db.commit()



def _calculate_coverage(
    db: Database,
    rule_engine: RuleEngine,
    report: BootstrapReport,
) -> None:
    """Check how many sampled emails would be matched by the created rules."""
    rows = db.execute(
        "SELECT DISTINCT email_id, from_address, from_domain, list_id, target_folder "
        "FROM audit_log WHERE classification_source = 'manual' AND moved = 1"
    ).fetchall()

    matched = 0
    for row in rows:
        features = EmailFeatures(
            email_id=row["email_id"],
            thread_id="",
            from_address=row["from_address"] or "",
            from_domain=row["from_domain"] or "",
            to_addresses=[],
            subject="",
            list_id=row["list_id"],
            received_at="2000-01-01T00:00:00+00:00",
            preview="",
            keywords=[],
            current_mailbox_ids={},
        )
        clf = rule_engine.classify(features)
        if clf and clf.folder_path == row["target_folder"]:
            matched += 1

    report.emails_matched_by_rules = matched
    report.emails_unmatched = report.emails_sampled - matched
    logger.info(
        "Rule coverage: %d/%d emails matched (%.0f%%), %d unmatched",
        matched, report.emails_sampled,
        matched / report.emails_sampled * 100 if report.emails_sampled > 0 else 0,
        report.emails_unmatched,
    )


def _log_report(report: BootstrapReport) -> None:
    logger.info(
        "Bootstrap complete: %d folders scanned, %d emails sampled, "
        "%d rules created, %d descriptions generated, %d contacts imported",
        report.folders_scanned,
        report.emails_sampled,
        report.rules_created,
        report.descriptions_generated,
        report.contacts_imported,
    )
    if report.errors:
        logger.warning("Bootstrap had %d error(s): %s", len(report.errors), "; ".join(report.errors))
