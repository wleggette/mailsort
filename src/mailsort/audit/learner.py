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
from typing import Optional

from mailsort.classifier.features import extract_features
from mailsort.classifier.rules import RuleEngine
from mailsort.config import ClassificationConfig
from mailsort.db.database import Database
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail

logger = logging.getLogger(__name__)


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
    ) -> int:
        """Detect user corrections and log them. Returns count of manual sorts found.

        Categories:
          1. Skipped emails the user moved out of the inbox.
          2. Mailsort-moved emails the user relocated to a different folder.
          3. Inbox departures: emails seen in previous scan but gone now
             (user sorted before mailsort processed them).
        """
        found = 0

        # Category 1: emails we skipped (moved=0) that are no longer in the inbox
        found += self._detect_skipped_sorts(jmap, tree, run_id)

        # Category 2: emails we moved that the user relocated
        found += self._detect_correction_sorts(jmap, tree, run_id)

        # Category 3: inbox departures (Option C)
        if current_inbox_ids is not None:
            found += self._detect_inbox_departures(jmap, tree, run_id, current_inbox_ids)

        if found:
            logger.info("Detected %d manual sort(s)", found)
        return found

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
                 AND created_at >= datetime('now', ?)""",
            (lookback,),
        ).fetchall()
        if not rows:
            return 0

        skipped_ids = [row["email_id"] for row in rows]
        found = 0

        try:
            emails = jmap.get_emails(skipped_ids[:100], ["id", "mailboxIds"])
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
        """Find emails we moved that the user subsequently relocated."""
        lookback = f"-{self._config.learner_lookback_days} days"
        rows = self._db.execute(
            """SELECT email_id, target_folder FROM audit_log
               WHERE moved = 1 AND classification_source != 'manual'
                 AND created_at >= datetime('now', ?)""",
            (lookback,),
        ).fetchall()
        if not rows:
            return 0

        expected = {row["email_id"]: row["target_folder"] for row in rows}
        email_ids = list(expected.keys())
        found = 0

        try:
            emails = jmap.get_emails(email_ids[:100], ["id", "mailboxIds"])
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
                        self._record_manual_sort(run_id, email.id, new_path)
                        found += 1
        return found

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
    ) -> int:
        """Scan non-inbox folders for recent emails with no audit_log record.

        This catches emails sorted by the user outside of any scan window
        (e.g., moved within seconds of arrival). Runs once per day.

        Returns count of manual sorts found.
        """
        if not self._should_run_folder_scan():
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

    def _should_run_folder_scan(self) -> bool:
        """Check if the daily folder scan should run (at most once per 24h)."""
        row = self._db.execute(
            "SELECT value FROM learner_state WHERE key = 'last_folder_scan'"
        ).fetchone()
        if not row:
            return True
        # Run if last scan was more than 24 hours ago
        check = self._db.execute(
            "SELECT ? < datetime('now', '-24 hours') AS due",
            (row["value"],),
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
            """SELECT from_address, from_domain, thread_id, subject, list_id
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
    ) -> None:
        """Insert a manual classification audit_log row and try auto-rule creation."""
        self._db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, thread_id, from_address, from_domain, "
            " subject, list_id, source_folder, target_folder, confidence, "
            " classification_source, moved, skip_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, email_id, thread_id, from_address, from_domain,
                subject, list_id, "INBOX", folder_path,
                1.0, "manual", True, None,
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
    ) -> Optional[int]:
        """Create the most appropriate rule if there is sufficient evidence.

        Priority: list_id → sender_domain (with coherence) → exact_sender.
        Returns the rule ID if one was created, else None.
        """
        thresholds = self._config.auto_rule_thresholds
        coherence_min = self._config.auto_rule_domain_coherence

        # 1. List-Id rule
        if list_id:
            count = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE list_id = ? AND target_folder = ? AND moved = 1",
                (list_id, target_folder),
            ).fetchone()[0]
            if count >= thresholds.list_id:
                existing = self._rules.find_existing_rule("list_id", list_id)
                if not existing:
                    rule_id = self._rules.create_rule(
                        rule_type="list_id",
                        condition_value=list_id,
                        target_folder_path=target_folder,
                        confidence=0.95,
                        source="auto",
                    )
                    logger.info("Auto-created list_id rule: %s → %s", list_id, target_folder)
                    return rule_id

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
                existing = self._rules.find_existing_rule("sender_domain", from_domain)
                if not existing:
                    confidence = min(0.90, 0.75 + (domain_to_target * 0.02))
                    rule_id = self._rules.create_rule(
                        rule_type="sender_domain",
                        condition_value=from_domain,
                        target_folder_path=target_folder,
                        confidence=confidence,
                        source="auto",
                    )
                    logger.info(
                        "Auto-created domain rule: %s → %s (coherence=%.0f%%, n=%d)",
                        from_domain, target_folder, coherence * 100, domain_to_target,
                    )
                    return rule_id

        # 3. Exact sender fallback
        if from_address:
            count = self._db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE from_address = ? AND target_folder = ? AND moved = 1",
                (from_address, target_folder),
            ).fetchone()[0]
            if count >= thresholds.exact_sender:
                existing = self._rules.find_existing_rule("exact_sender", from_address)
                if not existing:
                    confidence = min(0.95, 0.80 + (count * 0.03))
                    rule_id = self._rules.create_rule(
                        rule_type="exact_sender",
                        condition_value=from_address,
                        target_folder_path=target_folder,
                        confidence=confidence,
                        source="auto",
                    )
                    logger.info("Auto-created sender rule: %s → %s", from_address, target_folder)
                    return rule_id

        return None

    # ------------------------------------------------------------------
    # Rule confidence adjustment
    # ------------------------------------------------------------------

    def adjust_rule_confidence(self) -> int:
        """Lower confidence on rules that haven't matched recently. Returns count adjusted."""
        adjusted = 0
        rows = self._db.execute(
            """SELECT id, confidence, hit_count, last_hit_at FROM rules
               WHERE active = 1 AND last_hit_at IS NOT NULL
                 AND last_hit_at < datetime('now', '-90 days')
                 AND confidence > 0.50"""
        ).fetchall()

        for row in rows:
            new_confidence = max(0.50, row["confidence"] - 0.10)
            if new_confidence < row["confidence"]:
                self._db.execute(
                    "UPDATE rules SET confidence = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_confidence, row["id"]),
                )
                adjusted += 1
                logger.info(
                    "Lowered confidence on rule %d: %.2f → %.2f (last hit: %s)",
                    row["id"], row["confidence"], new_confidence, row["last_hit_at"],
                )

        if adjusted:
            self._db.commit()
            logger.info("Adjusted confidence on %d stale rule(s)", adjusted)
        return adjusted
