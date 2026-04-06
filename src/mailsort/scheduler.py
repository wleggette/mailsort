"""APScheduler integration: runs classification passes on a configurable interval.

Uses APScheduler's BlockingScheduler with max_instances=1 to prevent
overlapping runs. Handles graceful shutdown on SIGTERM/SIGINT.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from mailsort.audit.writer import AuditWriter
from mailsort.bootstrap import run_bootstrap
from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.health import start_health_server
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.orchestrator import run_classification_pass, _acquire_run_lock, _release_run_lock

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
        next_run_time=datetime.now(timezone.utc),
    )

    # Graceful shutdown on SIGTERM (Docker stop) and SIGINT (Ctrl+C)
    def _shutdown(signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down scheduler…", sig_name)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Start health check server in background thread
    health_server = start_health_server(cfg.db_path, port=cfg.scheduler.health_check_port)

    # Start web UI in background thread (if configured)
    web_server = _start_web_server(cfg)

    logger.info(
        "Scheduler started: running every %d minutes",
        cfg.scheduler.interval_minutes,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
    finally:
        if web_server:
            web_server.should_exit = True
        if health_server:
            health_server.shutdown()


def _needs_bootstrap(db: Database) -> bool:
    """Return True if no completed bootstrap run exists."""
    row = db.execute(
        "SELECT 1 FROM runs WHERE trigger='bootstrap' AND status='completed' LIMIT 1"
    ).fetchone()
    return row is None


def _run_auto_bootstrap(
    cfg: Config, db: Database, jmap: JMAPClient, tree: MailboxTree,
) -> bool:
    """Run bootstrap if no completed bootstrap exists.

    Returns True if bootstrap was attempted (caller should skip classification
    this tick), False if bootstrap was not needed.
    """
    if not _needs_bootstrap(db):
        return False

    logger.info("No completed bootstrap found — running auto-bootstrap")
    report = run_bootstrap(cfg, db, jmap, tree)

    if report.errors:
        logger.warning(
            "Auto-bootstrap finished with %d error(s) — will retry next tick",
            len(report.errors),
        )
    else:
        logger.info(
            "Auto-bootstrap complete: %d folders, %d rules, %d descriptions",
            report.folders_scanned, report.rules_created, report.descriptions_generated,
        )
    return True


def _scheduled_run(cfg: Config) -> None:
    """Execute a single classification pass. Called by the scheduler."""
    logger.info("Starting scheduled classification pass")

    # Acquire the run lock early (before expensive JMAP setup) so a
    # second instance fails fast instead of doing redundant work.
    lock_fd = _acquire_run_lock(cfg.db_path)
    if lock_fd is None:
        logger.warning("Scheduled run skipped — another live run holds the lock")
        return

    try:
        with Database(cfg.db_path) as db:
            run_migrations(db)
            AuditWriter(db).reconcile_stale_runs(
                stale_dry_run_minutes=cfg.scheduler.stale_dry_run_minutes,
            )

            try:
                jmap = JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url)
                mailboxes = jmap.get_all_mailboxes()
                tree = MailboxTree.build(mailboxes, exclude_patterns=cfg.exclude_folder_patterns)

                # Auto-bootstrap on first start: if no completed bootstrap
                # exists, run it now and skip classification this tick.
                if _run_auto_bootstrap(cfg, db, jmap, tree):
                    return

                result = run_classification_pass(
                    cfg, db, jmap, tree, dry_run=False, trigger="scheduler",
                )

                if result.read_only_downgrade:
                    logger.warning(
                        "Run %s auto-downgraded to dry-run (read-only token)",
                        result.run_id[:8],
                    )

                row = db.execute("SELECT * FROM runs WHERE run_id=?", (result.run_id,)).fetchone()
                if row:
                    logger.info(
                        "Scheduled run %s complete: status=%s seen=%s moved=%s",
                        result.run_id[:8], row["status"], row["emails_seen"], row["emails_moved"],
                    )
            except Exception:
                logger.exception("Scheduled classification pass failed")
            finally:
                jmap.close()
    finally:
        _release_run_lock(lock_fd)


def _start_web_server(cfg: Config) -> Optional[Any]:
    """Start the web UI in a background daemon thread using Uvicorn.

    Returns the uvicorn.Server instance (for shutdown), or None if disabled
    or if startup fails.
    """
    port = cfg.scheduler.web_port
    if port == 0:
        logger.info("Web UI disabled (web_port=0)")
        return None

    try:
        import asyncio
        import uvicorn
        from mailsort.web.app import create_app

        app = create_app(cfg)
        uv_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(uv_config)

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        logger.info("Web UI listening on port %d", port)
        return server
    except ImportError:
        logger.warning("Web UI dependencies not installed (uvicorn/fastapi), skipping")
        return None
    except OSError as e:
        logger.warning("Could not start web UI on port %d: %s", port, e)
        return None
    except Exception:
        logger.exception("Failed to start web UI")
        return None
