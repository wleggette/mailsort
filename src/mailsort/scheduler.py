"""APScheduler integration: runs classification passes on a configurable interval.

Uses APScheduler's BlockingScheduler with max_instances=1 to prevent
overlapping runs. Handles graceful shutdown on SIGTERM/SIGINT.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from mailsort.audit.writer import AuditWriter
from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.orchestrator import run_classification_pass

logger = logging.getLogger(__name__)


def start_scheduler(cfg: Config) -> None:
    """Start the blocking scheduler that runs classification passes on interval.

    This function blocks until the scheduler is shut down (via signal or error).
    """
    scheduler = BlockingScheduler()

    scheduler.add_job(
        _scheduled_run,
        trigger="interval",
        minutes=cfg.scheduler.interval_minutes,
        max_instances=1,
        kwargs={"cfg": cfg},
        id="mailsort_classification",
        name="Mailsort classification pass",
    )

    # Graceful shutdown on SIGTERM (Docker stop) and SIGINT (Ctrl+C)
    def _shutdown(signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down scheduler…", sig_name)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "Scheduler started: running every %d minutes",
        cfg.scheduler.interval_minutes,
    )

    # Run once immediately on startup, then on interval
    try:
        _scheduled_run(cfg=cfg)
    except Exception:
        logger.exception("Initial run failed, scheduler will retry on next interval")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def _scheduled_run(cfg: Config) -> None:
    """Execute a single classification pass. Called by the scheduler."""
    logger.info("Starting scheduled classification pass")

    with Database(cfg.db_path) as db:
        run_migrations(db)
        AuditWriter(db).reconcile_stale_runs()

        try:
            jmap = JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url)
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes)

            run_id = run_classification_pass(
                cfg, db, jmap, tree, dry_run=False, trigger="scheduler",
            )

            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                logger.info(
                    "Scheduled run %s complete: status=%s seen=%s moved=%s",
                    run_id[:8], row["status"], row["emails_seen"], row["emails_moved"],
                )
        except Exception:
            logger.exception("Scheduled classification pass failed")
        finally:
            jmap.close()
