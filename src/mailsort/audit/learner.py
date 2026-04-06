"""Learning from manual sorts and auto-rule generation.

Detects when the user manually sorts emails through four categories:
  1. Skipped emails the user moved out of the inbox.
  2. Mailsort-moved emails the user relocated to a different folder.
  3. Inbox departures: emails that disappeared from the inbox between scans
     (user sorted them before mailsort processed them).
  4. Daily folder scan: emails in non-inbox folders with no audit_log record
     (catches mail sorted outside of any scan window).

All detected manual sorts are logged and fed into auto-rule generation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from mailsort.classifier.features import extract_features
from mailsort.classifier.rules import RuleEngine
from mailsort.config import ClassificationConfig
from mailsort.db.database import Database
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail


@dataclass
class ManualSortCounts:
    """Correction counts grouped into user-facing buckets."""
    from_inbox: int = 0   # Cat 1 (skipped sorts) + Cat 3 (inbox departures)
    from_other: int = 0   # Cat 2 (correction sorts) + Cat 4 (folder scan)

    @property
    def total(self) -> int:
        return self.from_inbox + self.from_other

logger = logging.getLogger(__name__)

_RULE_TYPE_COLUMN: dict[str, str] = {
    "exact_sender": "from_address",
    "sender_domain": "from_domain",
    "list_id": "list_id",
    "subject_regex": "subject",
}


class Learner:
    """Detects manual sorts and generates rules from repeated patterns."""

    def __init__(
        self,
        db: Database,
        rule_engine: RuleEngine,
        config: ClassificationConfig,
    ):
        self._db = db
        self._rules = rule_engine
        self._config = config

    # ------------------------------------------------------------------
    # Manual sort detection
    # ------------------------------------------------------------------

    def detect_manual_sorts(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
        current_inbox_ids: set[str] | None = None,
    ) -> ManualSortCounts:
        """Detect user corrections and log them. Returns counts by bucket.

        From inbox (user sorted from inbox):
          - Category 1: skipped emails the user moved out of the inbox
          - Category 3: inbox departures (sorted before mailsort processed)

        From other (user relocated a mailsort-moved email):
          - Category 2: mailsort-moved emails the user relocated
        """
        counts = ManualSortCounts()

        # Category 1: emails we skipped (moved=0) that are no longer in the inbox
        counts.from_inbox += self._detect_skipped_sorts(jmap, tree, run_id)

        # Category 2: emails we moved that the user relocated
        counts.from_other += self._detect_correction_sorts(jmap, tree, run_id)

        # Category 2b: corrected emails the user moved again (sort-back recovery)
        counts.from_other += self._detect_correction_reversals(jmap, tree, run_id)

        # Category 3: inbox departures (Option C) — user sorted from inbox
        if current_inbox_ids is not None:
            counts.from_inbox += self._detect_inbox_departures(jmap, tree, run_id, current_inbox_ids)

        if counts.total:
            logger.info("Detected %d manual sort(s)", counts.total)
        return counts

    def _detect_skipped_sorts(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
    ) -> int:
        """Find emails we skipped in recent runs that the user moved out of inbox."""
        lookback = f"-{self._config.learner_lookback_days} days"
        rows = self._db.execute(
            """SELECT DISTINCT email_id FROM audit_log
               WHERE moved = 0
                 AND created_at >= datetime('now', ?)
                 AND email_id NOT IN (
                     SELECT email_id FROM audit_log
                      WHERE moved = 1 AND classification_source != 'manual'
                 )""",
            (lookback,),
        ).fetchall()
        if not rows:
            return 0

        skipped_ids = [row["email_id"] for row in rows]

        # Dedup: skip emails already handled (correction/manual row with no newer rule move)
        already_handled = self._already_handled_email_ids(skipped_ids)
        skipped_ids = [eid for eid in skipped_ids if eid not in already_handled]
        if not skipped_ids:
            return 0

        found = 0

        try:
            emails = jmap.get_emails(skipped_ids[:100], ["id", "threadId", "mailboxIds"])
        except Exception:
            logger.exception("Failed to fetch skipped emails for manual sort detection")
            return 0

        inbox_id = tree.inbox_id
        for email in emails:
            if inbox_id not in email.mailbox_ids:
                folder_id = next(iter(email.mailbox_ids), None)
                if folder_id:
                    folder_path = tree.path_for(folder_id)
                    if folder_path:
                        self._record_manual_sort(run_id, email.id, folder_path)
                        found += 1
        return found

    def _detect_correction_sorts(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
    ) -> int:
        """Find emails we moved that the user subsequently relocated.

        Corrections are recorded with classification_source='correction' and
        rule_id set to the rule that fired. The computed confidence model
        handles penalty application via compute_rule_confidence().
        """
        lookback = f"-{self._config.learner_lookback_days} days"
        # ORDER BY created_at ASC so the most recent move wins in the dict
        rows = self._db.execute(
            """SELECT email_id, target_folder, rule_id FROM audit_log
               WHERE moved = 1
                 AND classification_source NOT IN ('manual', 'correction')
                 AND created_at >= datetime('now', ?)
               ORDER BY created_at ASC""",
            (lookback,),
        ).fetchall()
        if not rows:
            return 0

        expected = {row["email_id"]: row["target_folder"] for row in rows}
        rule_ids = {row["email_id"]: row["rule_id"] for row in rows}
        email_ids = list(expected.keys())

        # Dedup: skip emails already handled (correction/manual row with no newer rule move)
        already_handled = self._already_handled_email_ids(email_ids)
        fetch_ids = [eid for eid in email_ids if eid not in already_handled]
        if not fetch_ids:
            return 0

        found = 0

        try:
            emails = jmap.get_emails(fetch_ids[:100], ["id", "threadId", "mailboxIds"])
        except Exception:
            logger.exception("Failed to fetch moved emails for correction detection")
            return 0

        for email in emails:
            expected_path = expected.get(email.id)
            if not expected_path:
                continue
            expected_id = tree.id_for(expected_path)
            if expected_id and expected_id not in email.mailbox_ids:
                new_folder_id = next(iter(email.mailbox_ids), None)
                if new_folder_id:
                    new_path = tree.path_for(new_folder_id)
                    if new_path and new_path != "INBOX":
                        self._record_correction(
                            run_id, email.id, new_path,
                            rule_id=rule_ids.get(email.id),
                        )
                        found += 1
        return found

    def _already_handled_email_ids(self, email_ids: list[str]) -> set[str]:
        """Return email_ids that already have a manual/correction row with no newer rule move.

        This handles the move-correct-move-correct cycle: if a new rule move
        exists after the last correction, the email is eligible for re-detection.
        """
        if not email_ids:
            return set()
        placeholders = ",".join("?" for _ in email_ids)
        rows = self._db.execute(
            f"""SELECT DISTINCT a.email_id FROM audit_log a
                WHERE a.email_id IN ({placeholders})
                  AND a.classification_source IN ('manual', 'correction')
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_log b
                      WHERE b.email_id = a.email_id
                        AND b.classification_source NOT IN ('manual', 'correction')
                        AND b.moved = 1
                        AND b.created_at > a.created_at
                  )""",
            email_ids,
        ).fetchall()
        return {r["email_id"] for r in rows}

    def _detect_correction_reversals(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
    ) -> int:
        """Find corrected emails the user moved again (Cat 2b: sort-back).

        After Cat 2 records a correction, the email is marked as "handled" by
        _already_handled_email_ids.  If the user subsequently moves that email
        to yet another folder (e.g. back to the rule's original target), Cat 2
        won't see it because the latest audit row is a correction, not a
        rule/LLM move.

        This pass queries the most-recent correction row per email, then checks
        whether the email's current JMAP mailbox still matches the correction
        target.  If not, the user moved it again and we record a 'manual' row
        for the new location.
        """
        lookback = f"-{self._config.learner_lookback_days} days"

        # Most-recent correction row per email (GROUP BY keeps the latest via MAX)
        rows = self._db.execute(
            """SELECT email_id, target_folder, rule_id,
                      MAX(created_at) as latest
               FROM audit_log
               WHERE classification_source = 'correction'
                 AND created_at >= datetime('now', ?)
               GROUP BY email_id""",
            (lookback,),
        ).fetchall()
        if not rows:
            return 0

        correction_target = {r["email_id"]: r["target_folder"] for r in rows}
        correction_rule = {r["email_id"]: r["rule_id"] for r in rows}
        email_ids = list(correction_target.keys())

        # Only consider emails whose most-recent audit row is still a
        # correction (no newer rule move that Cat 2 would handle instead).
        handled_by_cat2 = set()
        for eid in email_ids:
            newer = self._db.execute(
                """SELECT 1 FROM audit_log
                   WHERE email_id = ?
                     AND classification_source NOT IN ('manual', 'correction')
                     AND moved = 1
                     AND created_at > (
                         SELECT MAX(created_at) FROM audit_log
                         WHERE email_id = ? AND classification_source = 'correction'
                     )
                   LIMIT 1""",
                (eid, eid),
            ).fetchone()
            if newer:
                handled_by_cat2.add(eid)

        fetch_ids = [eid for eid in email_ids if eid not in handled_by_cat2]

        # Also skip emails where we already recorded a manual row newer than
        # the last correction (prevents double-counting on re-runs).
        still_needed = []
        for eid in fetch_ids:
            newer_manual = self._db.execute(
                """SELECT 1 FROM audit_log
                   WHERE email_id = ?
                     AND classification_source = 'manual'
                     AND created_at > (
                         SELECT MAX(created_at) FROM audit_log
                         WHERE email_id = ? AND classification_source = 'correction'
                     )
                   LIMIT 1""",
                (eid, eid),
            ).fetchone()
            if not newer_manual:
                still_needed.append(eid)

        if not still_needed:
            return 0

        found = 0
        try:
            emails = jmap.get_emails(still_needed[:100], ["id", "threadId", "mailboxIds"])
        except Exception:
            logger.exception("Failed to fetch corrected emails for reversal detection")
            return 0

        for email in emails:
            expected_path = correction_target.get(email.id)
            if not expected_path:
                continue
            expected_id = tree.id_for(expected_path)
            if expected_id and expected_id not in email.mailbox_ids:
                # Email is no longer where the correction put it
                new_folder_id = next(iter(email.mailbox_ids), None)
                if new_folder_id:
                    new_path = tree.path_for(new_folder_id)
                    if new_path and new_path != "INBOX":
                        self._record_manual_sort(run_id, email.id, new_path)
                        found += 1
        return found

    def _record_correction(
        self, run_id: str, email_id: str, folder_path: str,
        *, rule_id: int | None = None,
    ) -> None:
        """Log a correction (Cat 2) with the rule that fired."""
        row = self._db.execute(
            """SELECT from_address, from_domain, thread_id, subject, list_id, email_received_at
               FROM audit_log WHERE email_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (email_id,),
        ).fetchone()

        self._db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, thread_id, from_address, from_domain, "
            " subject, list_id, source_folder, target_folder, confidence, "
            " classification_source, rule_id, moved, skip_reason, email_received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, email_id,
                row["thread_id"] if row else None,
                row["from_address"] if row else None,
                row["from_domain"] if row else None,
                row["subject"] if row else None,
                row["list_id"] if row else None,
                "INBOX", folder_path,
                1.0, "correction", rule_id, True, None,
                row["email_received_at"] if row else None,
            ),
        )
        self._db.commit()
        logger.debug("Recorded correction: %s → %s (rule_id=%s)", email_id, folder_path, rule_id)

        if row and row["from_address"]:
            self.maybe_create_rule(
                from_address=row["from_address"],
                from_domain=row["from_domain"],
                list_id=row["list_id"],
                target_folder=folder_path,
            )

    # ------------------------------------------------------------------
    # Category 3: Inbox departures (Option C)
    # ------------------------------------------------------------------

    def save_inbox_snapshot(self, run_id: str, email_ids: list[str]) -> None:
        """Store the set of email IDs seen in the inbox for this run."""
        if not email_ids:
            return
        self._db.executemany(
            "INSERT INTO inbox_snapshot (email_id, run_id) VALUES (?, ?)",
            [(eid, run_id) for eid in email_ids],
        )
        self._db.commit()

    def _get_previous_snapshot_ids(self) -> set[str]:
        """Get email IDs from the most recent completed snapshot."""
        # Find the most recent run that has snapshot data
        row = self._db.execute(
            """SELECT DISTINCT s.run_id FROM inbox_snapshot s
               JOIN runs r ON r.run_id = s.run_id
               WHERE r.status = 'completed'
               ORDER BY r.started_at DESC LIMIT 1"""
        ).fetchone()
        if not row:
            return set()
        rows = self._db.execute(
            "SELECT email_id FROM inbox_snapshot WHERE run_id = ?",
            (row["run_id"],),
        ).fetchall()
        return {r["email_id"] for r in rows}

    def _get_already_processed_ids(self) -> set[str]:
        """Get email IDs that mailsort has already processed (any audit_log row)."""
        rows = self._db.execute(
            "SELECT DISTINCT email_id FROM audit_log"
        ).fetchall()
        return {r["email_id"] for r in rows}

    def _detect_inbox_departures(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
        current_inbox_ids: set[str],
    ) -> int:
        """Detect emails that were in the inbox last scan but are gone now.

        These are emails the user sorted before mailsort processed them.
        We fetch their current mailboxIds to see where they went.
        """
        previous_ids = self._get_previous_snapshot_ids()
        if not previous_ids:
            return 0

        already_processed = self._get_already_processed_ids()

        # Departed = was in previous snapshot, not in current inbox, never processed
        departed = previous_ids - current_inbox_ids - already_processed
        if not departed:
            return 0

        logger.debug("Found %d inbox departures to investigate", len(departed))
        found = 0

        try:
            emails = jmap.get_emails(list(departed)[:100])
        except Exception:
            logger.exception("Failed to fetch departed emails")
            return 0

        inbox_id = tree.inbox_id
        for email in emails:
            # Find where the email is now
            non_inbox = [mid for mid in email.mailbox_ids if mid != inbox_id]
            if non_inbox:
                folder_path = tree.path_for(non_inbox[0])
                if folder_path:
                    self._record_manual_sort_from_email(
                        run_id, email, folder_path,
                    )
                    found += 1

        return found

    def cleanup_old_snapshots(self) -> None:
        """Remove snapshot rows older than 2 days to prevent unbounded growth."""
        self._db.execute(
            "DELETE FROM inbox_snapshot WHERE captured_at < datetime('now', '-2 days')"
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Daily folder scan (Option B)
    # ------------------------------------------------------------------

    def scan_folders_for_unknown_sorts(
        self,
        jmap: JMAPClient,
        tree: MailboxTree,
        run_id: str,
        *,
        max_per_folder: int = 25,
        interval_hours: int = 24,
    ) -> int:
        """Scan non-inbox folders for recent emails with no audit_log record.

        This catches emails sorted by the user outside of any scan window
        (e.g., moved within seconds of arrival). Runs at most once per
        *interval_hours* (configurable via scheduler.folder_scan_interval_hours).

        Returns count of manual sorts found.
        """
        if not self._should_run_folder_scan(interval_hours):
            return 0

        logger.info("Running daily folder scan for unknown sorts")
        known_ids = self._get_already_processed_ids()
        found = 0

        for folder_path in sorted(tree.all_folder_paths()):
            mailbox_id = tree.id_for(folder_path)
            if not mailbox_id:
                continue

            try:
                email_ids = jmap.query_folder_emails(mailbox_id, limit=max_per_folder)
                if not email_ids:
                    continue
                unknown_ids = [eid for eid in email_ids if eid not in known_ids]
                if not unknown_ids:
                    continue
                emails = jmap.get_emails(unknown_ids)
            except Exception:
                logger.exception("Folder scan failed for %s, skipping", folder_path)
                continue

            for email in emails:
                self._record_manual_sort_from_email(run_id, email, folder_path)
                known_ids.add(email.id)
                found += 1

        self._mark_folder_scan_done()
        if found:
            logger.info("Daily folder scan found %d unknown sort(s)", found)
        return found

    def _should_run_folder_scan(self, interval_hours: int = 24) -> bool:
        """Check if the folder scan should run (at most once per interval_hours)."""
        row = self._db.execute(
            "SELECT value FROM learner_state WHERE key = 'last_folder_scan'"
        ).fetchone()
        if not row:
            return True
        check = self._db.execute(
            "SELECT ? < datetime('now', ? || ' hours') AS due",
            (row["value"], f"-{interval_hours}"),
        ).fetchone()
        return bool(check and check["due"])

    def _mark_folder_scan_done(self) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO learner_state (key, value) "
            "VALUES ('last_folder_scan', datetime('now'))"
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Recording manual sorts
    # ------------------------------------------------------------------

    def _record_manual_sort(
        self, run_id: str, email_id: str, folder_path: str,
    ) -> None:
        """Log a manual classification for an email we've seen before (has audit_log row)."""
        row = self._db.execute(
            """SELECT from_address, from_domain, thread_id, subject, list_id, email_received_at
               FROM audit_log WHERE email_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (email_id,),
        ).fetchone()

        self._insert_manual_audit_row(
            run_id, email_id, folder_path,
            thread_id=row["thread_id"] if row else None,
            from_address=row["from_address"] if row else None,
            from_domain=row["from_domain"] if row else None,
            subject=row["subject"] if row else None,
            list_id=row["list_id"] if row else None,
            email_received_at=row["email_received_at"] if row else None,
        )

    def _record_manual_sort_from_email(
        self, run_id: str, email: JMAPEmail, folder_path: str,
    ) -> None:
        """Log a manual classification using features from a live JMAP email object.

        Used for inbox departures and folder scans where there is no prior audit_log row.
        """
        self._insert_manual_audit_row(
            run_id, email.id, folder_path,
            thread_id=email.thread_id,
            from_address=email.from_address,
            from_domain=email.from_domain,
            subject=email.subject,
            list_id=email.list_id,
            email_received_at=email.received_at,
        )

    def _insert_manual_audit_row(
        self,
        run_id: str,
        email_id: str,
        folder_path: str,
        *,
        thread_id: str | None = None,
        from_address: str | None = None,
        from_domain: str | None = None,
        subject: str | None = None,
        list_id: str | None = None,
        email_received_at: str | None = None,
    ) -> None:
        """Insert a manual classification audit_log row and try auto-rule creation."""
        self._db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, thread_id, from_address, from_domain, "
            " subject, list_id, source_folder, target_folder, confidence, "
            " classification_source, moved, skip_reason, email_received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, email_id, thread_id, from_address, from_domain,
                subject, list_id, "INBOX", folder_path,
                1.0, "manual", True, None, email_received_at,
            ),
        )
        self._db.commit()
        logger.debug("Recorded manual sort: %s → %s", email_id, folder_path)

        if from_address:
            self.maybe_create_rule(
                from_address=from_address,
                from_domain=from_domain,
                list_id=list_id,
                target_folder=folder_path,
            )

    # ------------------------------------------------------------------
    # Auto-rule generation
    # ------------------------------------------------------------------

    def maybe_create_rule(
        self,
        from_address: str | None,
        from_domain: str | None,
        list_id: str | None,
        target_folder: str,
    ) -> list[int]:
        """Create every rule type whose evidence thresholds are met.

        Evaluated independently (not short-circuited):
          1. list_id       — stable identifier for newsletters/mailing lists
          2. sender_domain — when domain history is coherent
          3. exact_sender  — narrow scope for individual senders

        If an inactive rule with the same type+condition exists, it is
        reactivated instead of creating a duplicate (one row per type+condition).

        Classification-time priority determines which rule fires.
        Returns a list of created/reactivated rule IDs (may be empty).
        """
        thresholds = self._config.auto_rule_thresholds
        coherence_min = self._config.auto_rule_domain_coherence
        base_conf = self._config.base_confidence
        created: list[int] = []

        # 1. List-Id rule
        if list_id:
            to_target = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE list_id = ? AND target_folder = ? AND moved = 1",
                (list_id, target_folder),
            ).fetchone()[0]
            total = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE list_id = ? AND moved = 1",
                (list_id,),
            ).fetchone()[0]
            coherence = to_target / total if total > 0 else 0.0
            if to_target >= thresholds.list_id and coherence >= coherence_min:
                conf = base_conf.list_id
                existing = self._rules.find_rule_any_status("list_id", list_id)
                if existing and not existing["active"]:
                    self._rules.reactivate_rule(
                        existing["id"], confidence=conf,
                        target_folder_path=target_folder,
                    )
                    logger.info(
                        "Reactivated list_id rule %d: %s → %s (coherence=%.0f%%, n=%d)",
                        existing["id"], list_id, target_folder, coherence * 100, to_target,
                    )
                    created.append(existing["id"])
                elif not existing:
                    rule_id = self._rules.create_rule(
                        rule_type="list_id",
                        condition_value=list_id,
                        target_folder_path=target_folder,
                        confidence=conf,
                        source="auto",
                    )
                    logger.info(
                        "Auto-created list_id rule: %s → %s (coherence=%.0f%%, n=%d)",
                        list_id, target_folder, coherence * 100, to_target,
                    )
                    created.append(rule_id)

        # 2. Sender domain rule (with coherence check)
        if from_domain:
            domain_total = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE from_domain = ? AND moved = 1",
                (from_domain,),
            ).fetchone()[0]

            domain_to_target = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE from_domain = ? AND target_folder = ? AND moved = 1",
                (from_domain, target_folder),
            ).fetchone()[0]

            domain_distinct = self._db.execute(
                "SELECT COUNT(DISTINCT from_address) FROM audit_log "
                "WHERE from_domain = ? AND target_folder = ? AND moved = 1",
                (from_domain, target_folder),
            ).fetchone()[0]

            coherence = domain_to_target / domain_total if domain_total > 0 else 0.0

            if (domain_to_target >= thresholds.sender_domain
                    and domain_distinct >= 3
                    and coherence >= coherence_min):
                conf = min(
                    base_conf.sender_domain_cap,
                    base_conf.sender_domain_floor + domain_to_target * base_conf.sender_domain_per_evidence,
                )
                existing = self._rules.find_rule_any_status("sender_domain", from_domain)
                if existing and not existing["active"]:
                    self._rules.reactivate_rule(
                        existing["id"], confidence=conf,
                        target_folder_path=target_folder,
                    )
                    logger.info(
                        "Reactivated domain rule %d: %s → %s (coherence=%.0f%%, n=%d)",
                        existing["id"], from_domain, target_folder, coherence * 100, domain_to_target,
                    )
                    created.append(existing["id"])
                elif not existing:
                    rule_id = self._rules.create_rule(
                        rule_type="sender_domain",
                        condition_value=from_domain,
                        target_folder_path=target_folder,
                        confidence=conf,
                        source="auto",
                    )
                    logger.info(
                        "Auto-created domain rule: %s → %s (coherence=%.0f%%, n=%d)",
                        from_domain, target_folder, coherence * 100, domain_to_target,
                    )
                    created.append(rule_id)

        # 3. Exact sender (always evaluated)
        if from_address:
            to_target = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE from_address = ? AND target_folder = ? AND moved = 1",
                (from_address, target_folder),
            ).fetchone()[0]
            total = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE from_address = ? AND moved = 1",
                (from_address,),
            ).fetchone()[0]
            coherence = to_target / total if total > 0 else 0.0
            if to_target >= thresholds.exact_sender and coherence >= coherence_min:
                conf = min(
                    base_conf.exact_sender_cap,
                    base_conf.exact_sender_floor + to_target * base_conf.exact_sender_per_evidence,
                )
                existing = self._rules.find_rule_any_status("exact_sender", from_address)
                if existing and not existing["active"]:
                    self._rules.reactivate_rule(
                        existing["id"], confidence=conf,
                        target_folder_path=target_folder,
                    )
                    logger.info(
                        "Reactivated sender rule %d: %s → %s (coherence=%.0f%%, n=%d)",
                        existing["id"], from_address, target_folder, coherence * 100, to_target,
                    )
                    created.append(existing["id"])
                elif not existing:
                    rule_id = self._rules.create_rule(
                        rule_type="exact_sender",
                        condition_value=from_address,
                        target_folder_path=target_folder,
                        confidence=conf,
                        source="auto",
                    )
                    logger.info(
                        "Auto-created sender rule: %s → %s (coherence=%.0f%%, n=%d)",
                        from_address, target_folder, coherence * 100, to_target,
                    )
                    created.append(rule_id)

        return created

    # ------------------------------------------------------------------
    # Computed confidence model
    # ------------------------------------------------------------------

    def compute_rule_confidence(self) -> int:
        """Recompute confidence for all active auto rules from live state.

        Returns count of rules whose confidence changed.
        """
        rows = self._db.execute(
            "SELECT * FROM rules WHERE active = 1 AND source != 'manual'"
        ).fetchall()
        if not rows:
            return 0

        base_conf = self._config.base_confidence
        lookback_days = self._config.coherence_lookback_days
        min_sample = self._config.coherence_min_sample
        staleness_threshold = self._config.staleness_threshold_days
        staleness_decay = self._config.staleness_decay_days
        staleness_floor = self._config.staleness_floor
        penalty = self._config.correction_penalty
        deactivation = self._config.deactivation_threshold
        changed = 0

        for rule in rows:
            rule = dict(rule)
            old_conf = rule["confidence"]

            # 1. base_confidence — computed from all-time evidence
            evidence_count = self._count_all_time_evidence(rule)
            base = self._compute_base_confidence(rule["rule_type"], evidence_count, base_conf)

            # 2. coherence_factor — from audit_log within lookback window
            coherence, sample_count, last_relevant = self._compute_coherence(
                rule, lookback_days,
            )
            if sample_count < min_sample:
                coherence = 1.0  # benefit of the doubt

            # 3. staleness_factor — from last_relevant_at
            effective_last_relevant = last_relevant or rule.get("last_relevant_at")
            staleness = self._compute_staleness(
                effective_last_relevant,
                staleness_threshold, staleness_decay, staleness_floor,
            )

            # 4. net_corrections — in lookback window
            net_corrections = self._count_net_corrections(rule, lookback_days)

            # 5. confidence formula
            new_conf = max(0.0, base * coherence * staleness
                           - net_corrections * penalty)

            # 6. deactivation check
            if new_conf < deactivation:
                self._db.execute(
                    "UPDATE rules SET active = 0, confidence = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_conf, rule["id"]),
                )
                logger.info(
                    "Deactivated rule %d: confidence %.2f → %.2f (below threshold %.2f)",
                    rule["id"], old_conf, new_conf, deactivation,
                )
                changed += 1
            elif abs(new_conf - old_conf) > 0.001:
                update_fields = "confidence = ?, updated_at = datetime('now')"
                params: list = [new_conf]
                if effective_last_relevant:
                    update_fields += ", last_relevant_at = ?"
                    params.append(effective_last_relevant)
                params.append(rule["id"])
                self._db.execute(
                    f"UPDATE rules SET {update_fields} WHERE id = ?",
                    params,
                )
                logger.debug(
                    "Rule %d confidence: %.2f → %.2f (base=%.2f, coh=%.2f, stale=%.2f, corr=%d)",
                    rule["id"], old_conf, new_conf, base, coherence, staleness, net_corrections,
                )
                changed += 1
            elif effective_last_relevant:
                # Confidence unchanged but update last_relevant_at if available
                self._db.execute(
                    "UPDATE rules SET last_relevant_at = ? WHERE id = ?",
                    (effective_last_relevant, rule["id"]),
                )

        if changed:
            self._db.commit()
            logger.info("Recomputed confidence on %d rule(s)", changed)
        else:
            self._db.commit()  # commit last_relevant_at updates
        return changed

    def _count_all_time_evidence(self, rule: dict) -> int:
        """Count all-time evidence for base_confidence. Uses LIMIT to cap scan."""
        col = _RULE_TYPE_COLUMN[rule["rule_type"]]
        bc = self._config.base_confidence
        max_needed = max(
            math.ceil((bc.exact_sender_cap - bc.exact_sender_floor) / bc.exact_sender_per_evidence)
            if bc.exact_sender_per_evidence > 0 else 1,
            math.ceil((bc.sender_domain_cap - bc.sender_domain_floor) / bc.sender_domain_per_evidence)
            if bc.sender_domain_per_evidence > 0 else 1,
            1,  # list_id is fixed — 1 row suffices
        )
        return self._db.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT 1 FROM audit_log
                    WHERE {col} = ? AND target_folder = ? AND moved = 1
                    LIMIT ?
                )""",
            (rule["condition_value"], rule["target_folder_path"], max_needed),
        ).fetchone()[0]

    @staticmethod
    def _compute_base_confidence(
        rule_type: str, evidence_count: int, base_conf,
    ) -> float:
        """Compute base confidence from rule type and evidence count."""
        if rule_type == "list_id":
            return base_conf.list_id
        elif rule_type == "exact_sender":
            return min(
                base_conf.exact_sender_cap,
                base_conf.exact_sender_floor + evidence_count * base_conf.exact_sender_per_evidence,
            )
        elif rule_type == "sender_domain":
            return min(
                base_conf.sender_domain_cap,
                base_conf.sender_domain_floor + evidence_count * base_conf.sender_domain_per_evidence,
            )
        return 0.90  # fallback for subject_regex

    def _compute_coherence(
        self, rule: dict, lookback_days: int,
    ) -> tuple[float, int, str | None]:
        """Compute coherence factor and last_relevant_at from audit_log.

        Returns (coherence_factor, sample_count, last_relevant_at_str).
        """
        col = _RULE_TYPE_COLUMN[rule["rule_type"]]
        lookback = f"-{lookback_days} days"

        # Total emails matching condition that were moved in the window
        total_row = self._db.execute(
            f"""SELECT COUNT(*) AS cnt FROM audit_log
                WHERE {col} = ? AND moved = 1
                  AND created_at >= datetime('now', ?)""",
            (rule["condition_value"], lookback),
        ).fetchone()
        total = total_row["cnt"]

        # Emails matching condition moved to this rule's target folder
        target_row = self._db.execute(
            f"""SELECT COUNT(*) AS cnt, MAX(created_at) AS last_relevant
                FROM audit_log
                WHERE {col} = ? AND target_folder = ? AND moved = 1
                  AND created_at >= datetime('now', ?)""",
            (rule["condition_value"], rule["target_folder_path"], lookback),
        ).fetchone()
        to_target = target_row["cnt"]
        last_relevant = target_row["last_relevant"]

        coherence = to_target / total if total > 0 else 1.0
        return coherence, total, last_relevant

    @staticmethod
    def _compute_staleness(
        last_relevant_at: str | None,
        threshold_days: int,
        decay_days: int,
        floor: float,
    ) -> float:
        """Compute staleness factor from last_relevant_at timestamp."""
        if not last_relevant_at:
            return 1.0  # no data — benefit of the doubt

        from datetime import datetime, timezone
        try:
            last_dt = datetime.fromisoformat(last_relevant_at).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 1.0
        now = datetime.now(timezone.utc)
        days_since = (now - last_dt).total_seconds() / 86400

        if days_since <= threshold_days:
            return 1.0

        days_past = days_since - threshold_days
        return max(floor, 1.0 - (days_past / decay_days) * 0.4)

    def _count_net_corrections(self, rule: dict, lookback_days: int) -> int:
        """Count net corrections (corrections_against − confirming_sorts) in window."""
        lookback = f"-{lookback_days} days"

        # Corrections against this rule
        corrections = self._db.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE classification_source = 'correction'
                 AND rule_id = ?
                 AND created_at >= datetime('now', ?)""",
            (rule["id"], lookback),
        ).fetchone()[0]

        # Confirming manual sorts (matching condition → rule's target folder)
        # Exclude bootstrap runs — bootstrap evidence is historical data, not
        # a user response to a correction.
        col = _RULE_TYPE_COLUMN[rule["rule_type"]]
        confirming = self._db.execute(
            f"""SELECT COUNT(*) FROM audit_log
                WHERE classification_source = 'manual'
                  AND {col} = ?
                  AND target_folder = ?
                  AND created_at >= datetime('now', ?)
                  AND run_id NOT IN (
                      SELECT run_id FROM runs WHERE trigger = 'bootstrap'
                  )""",
            (rule["condition_value"], rule["target_folder_path"], lookback),
        ).fetchone()[0]

        return max(0, corrections - confirming)
