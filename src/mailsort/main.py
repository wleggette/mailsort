"""Entry point and CLI for mailsort."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import click

from mailsort.audit.writer import AuditWriter
from mailsort.bootstrap import run_bootstrap
from mailsort.config import Config, load_config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.orchestrator import run_classification_pass
from mailsort.scheduler import start_scheduler


def setup_logging(cfg: Config) -> None:
    log_cfg = cfg.logging_config
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=log_cfg.max_size_mb * 1024 * 1024,
        backupCount=log_cfg.backup_count,
    )
    handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--config", "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """Mailsort — Fastmail inbox classifier and sorter."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run a single classification-and-move pass."""
    _run_pass(ctx, dry_run=False)


@cli.command("dry-run")
@click.pass_context
def dry_run(ctx: click.Context) -> None:
    """Classify emails but don't move anything (decisions are still logged)."""
    _run_pass(ctx, dry_run=True)


def _run_pass(ctx: click.Context, *, dry_run: bool) -> None:
    cfg = load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)
    mode = "DRY RUN" if dry_run else "LIVE"

    with Database(cfg.db_path) as db:
        run_migrations(db)
        AuditWriter(db).reconcile_stale_runs()
        logger.info("Database ready at %s", cfg.db_path)

        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes)
            logger.info(
                "Mailbox tree loaded: %d folders, inbox=%s",
                len(tree.all_folder_paths()),
                tree.inbox_id,
            )

            run_id = run_classification_pass(
                cfg, db, jmap, tree, dry_run=dry_run, trigger="cli",
            )

        # Report summary
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row:
            click.echo(f"\n[{mode}] Run {run_id[:8]}… complete:")
            click.echo(f"  Status : {row['status']}")
            click.echo(f"  Seen   : {row['emails_seen']}")
            click.echo(f"  Moved  : {row['emails_moved']}")
            if row["error_summary"]:
                click.echo(f"  Error  : {row['error_summary']}")


@cli.command()
@click.pass_context
def check_config(ctx: click.Context) -> None:
    """Validate config and verify Fastmail connectivity."""
    cfg = load_config(ctx.obj["config_path"])
    setup_logging(cfg)

    click.echo(f"Config loaded from {ctx.obj['config_path']}")
    click.echo(f"  Fastmail session URL : {cfg.fastmail.session_url}")
    click.echo(f"  Scheduler interval   : {cfg.scheduler.interval_minutes}m")
    click.echo(f"  Min email age        : {cfg.scheduler.min_age_hours}h")
    click.echo(f"  LLM model            : {cfg.classification.llm_model}")

    with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
        session = jmap.get_session()
        click.echo(f"\nJMAP session OK")
        click.echo(f"  Account ID  : {session.account_id}")
        click.echo(f"  Capabilities: {len(session.capabilities)}")
        contacts_ok = "urn:ietf:params:jmap:contacts" in session.capabilities
        click.echo(f"  Contacts    : {'available' if contacts_ok else 'NOT available (no contact enrichment)'}")

        mailboxes = jmap.get_all_mailboxes()
        tree = MailboxTree.build(mailboxes)
        click.echo(f"\nMailbox tree: {len(tree.all_folder_paths())} target folders")


@cli.command()
@click.option("--max-per-folder", default=50, show_default=True, help="Max emails to sample per folder")
@click.pass_context
def bootstrap(ctx: click.Context, max_per_folder: int) -> None:
    """Scan existing folders to seed rules and folder descriptions."""
    cfg = load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)

    with Database(cfg.db_path) as db:
        run_migrations(db)
        logger.info("Database ready at %s", cfg.db_path)

        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes)

            report = run_bootstrap(
                cfg, db, jmap, tree, max_per_folder=max_per_folder,
            )

    click.echo(f"\nBootstrap complete:")
    click.echo(f"  Folders scanned : {report.folders_scanned}")
    click.echo(f"  Emails sampled  : {report.emails_sampled}")
    click.echo(f"  Rules created   : {report.rules_created}")
    click.echo(f"  Descriptions    : {report.descriptions_generated}")
    if report.errors:
        click.echo(f"  Errors          : {len(report.errors)}")


@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the scheduler (runs classification every N minutes)."""
    cfg = load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)

    logger.info("Starting mailsort scheduler (interval=%dm)", cfg.scheduler.interval_minutes)
    start_scheduler(cfg)


if __name__ == "__main__":
    cli()
