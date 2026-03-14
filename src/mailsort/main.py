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


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(cfg: Config) -> None:
    log_cfg = cfg.logging_config
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    use_json = log_cfg.format.lower() == "json"

    if use_json:
        formatter = _JSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stdout_handler]

    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=log_cfg.max_size_mb * 1024 * 1024,
        backupCount=log_cfg.backup_count,
    )
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        handlers=handlers,
    )

    # Suppress noisy HTTP client logging — only show on WARNING+ or DEBUG level
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _safe_load_config(config_path: str) -> Config:
    """Load config with user-friendly error messages instead of tracebacks."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        click.echo(f"Error: Config file not found: {config_path}", err=True)
        raise SystemExit(1)
    except Exception as e:
        # Pydantic ValidationError, YAML parse errors, etc.
        msg = str(e)
        # Extract the useful part from Pydantic's verbose errors
        if "FASTMAIL_API_TOKEN" in msg:
            click.echo(
                "Error: FASTMAIL_API_TOKEN is not set.\n"
                "  Set it as an environment variable:\n"
                "    export FASTMAIL_API_TOKEN=fmu1-...\n"
                "  Or add it to a .env file and run:\n"
                "    export $(grep -v '^#' .env | xargs)",
                err=True,
            )
        elif "validation error" in msg.lower():
            # Show just the first error line, not the full Pydantic dump
            lines = msg.strip().splitlines()
            click.echo(f"Error: Invalid configuration — {lines[-1].strip()}", err=True)
        else:
            click.echo(f"Error: Failed to load config — {e}", err=True)
        raise SystemExit(1)


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
    cfg = _safe_load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)
    mode = "DRY RUN" if dry_run else "LIVE"

    with Database(cfg.db_path) as db:
        run_migrations(db)
        AuditWriter(db).reconcile_stale_runs()
        logger.info("Database ready at %s", cfg.db_path)

        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes, exclude_patterns=cfg.exclude_folder_patterns)
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
    cfg = _safe_load_config(ctx.obj["config_path"])
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
        if session.is_read_only:
            click.echo(f"  Permissions : READ-ONLY (bootstrap, dry-run, analyze OK; moves will fail)")
        else:
            click.echo(f"  Permissions : read/write")

        mailboxes = jmap.get_all_mailboxes()
        tree = MailboxTree.build(mailboxes)
        click.echo(f"\nMailbox tree: {len(tree.all_folder_paths())} target folders")


@cli.command()
@click.option("--max-per-folder", default=50, show_default=True, help="Max emails to sample per folder")
@click.pass_context
def bootstrap(ctx: click.Context, max_per_folder: int) -> None:
    """Scan existing folders to seed rules and folder descriptions.

    Safe to run multiple times — existing evidence is never duplicated.
    New emails that appeared since the last bootstrap are added.
    """
    cfg = _safe_load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)

    with Database(cfg.db_path) as db:
        run_migrations(db)
        logger.info("Database ready at %s", cfg.db_path)

        with JMAPClient(cfg.fastmail_api_token, cfg.fastmail.session_url) as jmap:
            mailboxes = jmap.get_all_mailboxes()
            tree = MailboxTree.build(mailboxes, exclude_patterns=cfg.exclude_folder_patterns)

            report = run_bootstrap(
                cfg, db, jmap, tree, max_per_folder=max_per_folder,
            )

    pct = (report.emails_matched_by_rules / report.emails_sampled * 100
           if report.emails_sampled > 0 else 0)

    click.echo(f"\nBootstrap complete:")
    click.echo(f"  Folders scanned : {report.folders_scanned}")
    click.echo(f"  Emails sampled  : {report.emails_sampled}")
    click.echo(f"  Rules created   : {report.rules_created}")
    click.echo(f"  Rule coverage   : {report.emails_matched_by_rules}/{report.emails_sampled} ({pct:.0f}%) matched, {report.emails_unmatched} unmatched")
    click.echo(f"  Descriptions    : {report.descriptions_generated}")
    click.echo(f"  Contacts        : {report.contacts_imported}")
    if report.errors:
        click.echo(f"  Errors          : {len(report.errors)}")


@cli.command("export-rules")
@click.option("--inactive", is_flag=True, help="Include inactive/suggested rules")
@click.pass_context
def export_rules(ctx: click.Context, inactive: bool) -> None:
    """Export all rules to YAML for review."""
    import yaml as _yaml

    cfg = _safe_load_config(ctx.obj["config_path"])

    with Database(cfg.db_path) as db:
        run_migrations(db)
        where = "" if inactive else "WHERE active = 1"
        rows = db.execute(
            f"SELECT rule_type, condition_value, target_folder_path, confidence, "
            f"source, hit_count, last_hit_at, active, created_at "
            f"FROM rules {where} ORDER BY rule_type, condition_value"
        ).fetchall()

        rules = []
        for r in rows:
            entry: dict = {
                "type": r["rule_type"],
                "value": r["condition_value"],
                "folder": r["target_folder_path"],
                "confidence": r["confidence"],
                "source": r["source"],
                "hits": r["hit_count"],
            }
            if r["last_hit_at"]:
                entry["last_hit"] = r["last_hit_at"]
            if not r["active"]:
                entry["active"] = False
            rules.append(entry)

    click.echo(_yaml.dump({"rules": rules}, default_flow_style=False, sort_keys=False))
    click.echo(f"# {len(rules)} rule(s) exported", err=True)


@cli.command()
@click.option("--days", default=30, show_default=True, help="Analysis window in days")
@click.pass_context
def analyze(ctx: click.Context, days: int) -> None:
    """Analyze confidence thresholds based on audit data."""
    cfg = _safe_load_config(ctx.obj["config_path"])

    with Database(cfg.db_path) as db:
        run_migrations(db)
        _print_analysis(db, cfg, days)


def _print_analysis(db: Database, cfg: Config, days: int) -> None:
    """Query audit_log and print threshold analysis report.

    Excludes bootstrap runs — only analyzes real classification passes.
    """
    window = f"-{days} days"

    # Base filter: exclude bootstrap runs, only look at recent data
    base = (
        "FROM audit_log a JOIN runs r ON r.run_id = a.run_id "
        "WHERE r.trigger != 'bootstrap' AND a.created_at >= datetime('now', ?)"
    )

    # Overall counts
    total = db.execute(f"SELECT COUNT(*) {base}", (window,)).fetchone()[0]
    if total == 0:
        click.echo("No classification data found. Run 'mailsort run' or 'mailsort dry-run' first.")
        return

    moved = db.execute(
        f"SELECT COUNT(*) {base} AND a.moved = 1", (window,)
    ).fetchone()[0]
    skipped = total - moved

    # By source
    source_rows = db.execute(
        f"SELECT a.classification_source, COUNT(*) as n, SUM(a.moved) as m {base} "
        "GROUP BY a.classification_source ORDER BY n DESC", (window,)
    ).fetchall()

    # User corrections: manual sorts from learner detection (not bootstrap seed data)
    corrections = db.execute(
        f"SELECT COUNT(*) {base} AND a.classification_source = 'manual'", (window,)
    ).fetchone()[0]

    error_rate = corrections / moved * 100 if moved > 0 else 0.0

    click.echo(f"\n{'═' * 62}")
    click.echo(f"  Mailsort Threshold Analysis — last {days} days · {total} emails")
    click.echo(f"{'═' * 62}")

    click.echo(f"\n── Classification Sources {'─' * 37}")
    for r in source_rows:
        pct = r["n"] / total * 100
        bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        click.echo(f"  {r['classification_source']:16s} {r['n']:5d} ({pct:4.1f}%)  {bar}")

    click.echo(f"\n── Move Outcomes {'─' * 46}")
    click.echo(f"  Moved:            {moved:5d} ({moved / total * 100:.0f}%)")
    click.echo(f"  Skipped:          {skipped:5d} ({skipped / total * 100:.0f}%)")
    click.echo(f"  User corrections: {corrections:5d} ({error_rate:.1f}% error rate)")

    # LLM confidence distribution
    llm_rows = db.execute(
        "SELECT "
        "  CASE "
        "    WHEN a.confidence >= 0.90 THEN '0.90–1.00' "
        "    WHEN a.confidence >= 0.80 THEN '0.80–0.89' "
        "    WHEN a.confidence >= 0.70 THEN '0.70–0.79' "
        "    WHEN a.confidence >= 0.60 THEN '0.60–0.69' "
        "    ELSE '< 0.60' "
        "  END AS bucket, "
        "  SUM(CASE WHEN a.moved = 1 THEN 1 ELSE 0 END) AS moved, "
        "  SUM(CASE WHEN a.moved = 0 THEN 1 ELSE 0 END) AS skipped "
        f"{base} AND a.classification_source = 'llm' "
        "GROUP BY bucket ORDER BY bucket DESC", (window,)
    ).fetchall()

    if llm_rows:
        click.echo(f"\n── LLM Confidence Distribution {'─' * 31}")
        click.echo(f"  {'Confidence':<14s} {'Moved':>6s}  {'Skipped':>7s}")
        for r in llm_rows:
            marker = ""
            if r["bucket"] == "0.80–0.89":
                marker = f"  ← current threshold ({cfg.classification.thresholds.llm_move})"
            click.echo(f"  {r['bucket']:<14s} {r['moved']:>6d}  {r['skipped']:>7d}{marker}")

    # Skipped LLM emails that the user later sorted to the same folder
    skipped_then_sorted = db.execute(
        "SELECT a1.email_id, a1.target_folder AS llm_folder, a1.confidence, "
        "       a2.target_folder AS manual_folder "
        "FROM audit_log a1 "
        "JOIN runs r1 ON r1.run_id = a1.run_id "
        "JOIN audit_log a2 ON a1.email_id = a2.email_id "
        "JOIN runs r2 ON r2.run_id = a2.run_id "
        "WHERE r1.trigger != 'bootstrap' AND r2.trigger != 'bootstrap' "
        "  AND a1.classification_source = 'llm' AND a1.moved = 0 "
        "  AND a2.classification_source = 'manual' AND a2.moved = 1 "
        "  AND a1.created_at >= datetime('now', ?)", (window,)
    ).fetchall()

    if skipped_then_sorted:
        same_folder = [r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]
        click.echo(f"\n── Skipped Emails You Later Sorted {'─' * 27}")
        click.echo(
            f"  {len(same_folder)} of {len(skipped_then_sorted)} skipped LLM emails "
            f"were manually sorted to the SAME folder the LLM suggested."
        )
        if same_folder:
            avg_conf = sum(r["confidence"] for r in same_folder) / len(same_folder)
            click.echo(f"  Average LLM confidence on those: {avg_conf:.2f}")

    # Recommendations
    click.echo(f"\n── Recommendations {'─' * 43}")
    if skipped_then_sorted and len([r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]) > 3:
        same = [r for r in skipped_then_sorted if r["llm_folder"] == r["manual_folder"]]
        avg = sum(r["confidence"] for r in same) / len(same)
        suggested = round(avg - 0.05, 2)
        click.echo(
            f"  ⚡ llm_move: {cfg.classification.thresholds.llm_move} → {suggested} "
            f"would capture ~{len(same)} more emails/month"
        )
    else:
        click.echo(f"  ✓ llm_move: {cfg.classification.thresholds.llm_move} — insufficient data to suggest changes")

    rule_corrections = db.execute(
        "SELECT COUNT(*) FROM audit_log a1 "
        "JOIN runs r1 ON r1.run_id = a1.run_id "
        "JOIN audit_log a2 ON a1.email_id = a2.email_id "
        "JOIN runs r2 ON r2.run_id = a2.run_id "
        "WHERE r1.trigger != 'bootstrap' AND r2.trigger != 'bootstrap' "
        "  AND a1.classification_source = 'rule' AND a1.moved = 1 "
        "  AND a2.classification_source = 'manual' AND a2.moved = 1 "
        "  AND a1.target_folder != a2.target_folder "
        "  AND a1.created_at >= datetime('now', ?)", (window,)
    ).fetchone()[0]
    click.echo(f"  {'✓' if rule_corrections == 0 else '⚠'} rule_move: {cfg.classification.thresholds.rule_move} — {rule_corrections} correction(s)")

    click.echo()


@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the scheduler (runs classification every N minutes)."""
    cfg = _safe_load_config(ctx.obj["config_path"])
    setup_logging(cfg)
    logger = logging.getLogger(__name__)

    logger.info("Starting mailsort scheduler (interval=%dm)", cfg.scheduler.interval_minutes)
    start_scheduler(cfg)


if __name__ == "__main__":
    cli()
