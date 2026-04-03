"""Audit log writer for recording classification decisions and move outcomes."""

from __future__ import annotations

import logging
import uuid

from mailsort.db.database import Database
from mailsort.jmap.models import MoveDecision

logger = logging.getLogger(__name__)


class AuditWriter:
    """Reads and writes the runs and audit_log tables."""

    def __init__(self, db: Database):
        self._db = db

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def start_run(self, trigger: str = "scheduler", dry_run: bool = False) -> str:
        """Insert a new run row and return its run_id."""
        run_id = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
            "VALUES (?, datetime('now'), 'running', ?, ?)",
            (run_id, trigger, dry_run),
        )
        self._db.commit()
        logger.info("Started run %s (trigger=%s, dry_run=%s)", run_id, trigger, dry_run)
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        emails_seen: int = 0,
        emails_moved: int = 0,
        error_summary: str | None = None,
    ) -> None:
        """Mark a run as finished with summary counts.

        This method is deliberately defensive: if the DB write fails, the
        error is logged but not raised, because finish_run is often called
        from an exception handler and must not mask the original error.
        """
        try:
            self._db.execute(
                "UPDATE runs SET status=?, finished_at=datetime('now'), "
                "emails_seen=?, emails_moved=?, error_summary=? "
                "WHERE run_id=?",
                (status, emails_seen, emails_moved, error_summary, run_id),
            )
            self._db.commit()
            logger.info(
                "Finished run %s: status=%s seen=%d moved=%d",
                run_id, status, emails_seen, emails_moved,
            )
        except Exception:
            logger.exception("Failed to write finish_run for %s", run_id)

    def reconcile_stale_runs(self, stale_dry_run_minutes: int = 60) -> int:
        """Mark leftover 'running' rows as 'abandoned'. Returns count.

        With ``flock``-based run locking, a live 'running' row that outlives
        its process is genuinely stale — the kernel released the lock
        when the process exited.  Live runs (``dry_run=0``) are abandoned
        unconditionally.

        Dry-run rows don't hold a lock and may overlap, so they are only
        abandoned after ``stale_dry_run_minutes`` (default 60) to avoid
        interfering with a legitimately running dry run.
        """
        cursor = self._db.execute(
            "UPDATE runs SET status='abandoned', finished_at=datetime('now') "
            "WHERE status='running' AND ("
            "  dry_run = 0"
            "  OR (dry_run = 1 AND started_at < datetime('now', ?))"
            ")",
            (f"-{stale_dry_run_minutes} minutes",),
        )
        self._db.commit()
        count = cursor.rowcount
        if count:
            logger.warning("Reconciled %d stale run(s) as abandoned", count)
        return count

    # ------------------------------------------------------------------
    # Decision logging
    # ------------------------------------------------------------------

    def log_decision(self, run_id: str, decision: MoveDecision, moved: bool) -> None:
        """Write a single audit_log row for a classification decision."""
        clf = decision.classification
        received_at = decision.features.received_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, thread_id, from_address, from_domain, "
            " subject, list_id, source_folder, target_folder, confidence, "
            " classification_source, rule_id, llm_reasoning, moved, skip_reason, "
            " email_received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                decision.email_id,
                decision.features.thread_id,
                decision.features.from_address,
                decision.features.from_domain,
                decision.features.subject,
                decision.features.list_id,
                "INBOX",
                clf.folder_path,
                clf.confidence,
                clf.source,
                clf.rule_id,
                clf.reasoning,
                moved,
                decision.skip_reason,
                received_at,
            ),
        )

    def log_decisions(
        self,
        run_id: str,
        decisions: list[MoveDecision],
        outcomes: dict[str, bool],
    ) -> None:
        """Batch-write audit_log rows for all decisions in a run.

        Per-row isolation: if one insert fails (e.g., constraint violation),
        remaining decisions are still logged.

        Args:
            decisions: All MoveDecision objects from the run.
            outcomes: email_id → True/False from JMAP for emails that were
                      attempted.  Emails not in outcomes were skipped.
        """
        logged = 0
        for d in decisions:
            if d.should_move:
                moved = outcomes.get(d.email_id, False)
            else:
                moved = False
            try:
                self.log_decision(run_id, d, moved=moved)
                logged += 1
            except Exception:
                logger.exception(
                    "Failed to log audit row for email %s in run %s",
                    d.email_id, run_id,
                )
        try:
            self._db.commit()
        except Exception:
            logger.exception("Failed to commit audit_log batch for run %s", run_id)
        if logged < len(decisions):
            logger.warning(
                "Audit logging incomplete for run %s: %d/%d rows written",
                run_id, logged, len(decisions),
            )
