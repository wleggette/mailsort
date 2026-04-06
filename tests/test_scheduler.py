"""Tests for the scheduler module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.bootstrap import BootstrapReport
from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.orchestrator import RunResult
from mailsort.scheduler import _needs_bootstrap, _run_auto_bootstrap, _scheduled_run


def _make_config() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token",
        anthropic_api_key="",
        db_path=":memory:",
    )


@patch("mailsort.scheduler.run_classification_pass")
@patch("mailsort.scheduler.MailboxTree")
@patch("mailsort.scheduler.JMAPClient")
@patch("mailsort.scheduler.run_migrations")
def test_scheduled_run_calls_orchestrator(
    mock_migrations, mock_jmap_cls, mock_tree_cls, mock_run_pass,
):
    """_scheduled_run should open DB, JMAP, build tree, and call run_classification_pass."""
    cfg = _make_config()

    mock_jmap = MagicMock()
    mock_jmap_cls.return_value = mock_jmap
    mock_jmap.get_all_mailboxes.return_value = []

    mock_tree = MagicMock()
    mock_tree_cls.build.return_value = mock_tree

    mock_run_pass.return_value = RunResult(run_id="run-123", dry_run=False, read_only_downgrade=False)
    # Mock the DB query for the summary log
    mock_db_instance = MagicMock()
    mock_db_instance.execute.return_value.fetchone.return_value = {
        "status": "completed", "emails_seen": 5, "emails_moved": 3,
        "error_summary": None, "run_id": "run-123",
    }

    with patch("mailsort.scheduler.Database") as mock_db_cls:
        mock_db_cls.return_value.__enter__ = MagicMock(return_value=mock_db_instance)
        mock_db_cls.return_value.__exit__ = MagicMock(return_value=False)
        _scheduled_run(cfg=cfg)

    mock_run_pass.assert_called_once()
    call_kwargs = mock_run_pass.call_args[1]
    assert call_kwargs["trigger"] == "scheduler"
    assert call_kwargs["dry_run"] is False


@patch("mailsort.scheduler.run_classification_pass")
@patch("mailsort.scheduler.MailboxTree")
@patch("mailsort.scheduler.JMAPClient")
@patch("mailsort.scheduler.run_migrations")
def test_scheduled_run_handles_jmap_error(
    mock_migrations, mock_jmap_cls, mock_tree_cls, mock_run_pass,
):
    """If JMAP fails during a scheduled run, the error should be caught (not crash the scheduler)."""
    cfg = _make_config()

    mock_jmap = MagicMock()
    mock_jmap_cls.return_value = mock_jmap
    mock_jmap.get_all_mailboxes.side_effect = ConnectionError("JMAP down")

    with patch("mailsort.scheduler.Database") as mock_db_cls:
        mock_db_instance = MagicMock()
        mock_db_cls.return_value.__enter__ = MagicMock(return_value=mock_db_instance)
        mock_db_cls.return_value.__exit__ = MagicMock(return_value=False)
        # Should NOT raise — error is caught internally
        _scheduled_run(cfg=cfg)

    mock_run_pass.assert_not_called()


# ------------------------------------------------------------------
# _needs_bootstrap — real DB tests
# ------------------------------------------------------------------

def _make_db() -> Database:
    """Create an in-memory DB with migrations applied.

    Uses the context manager to call connect(), but returns the db
    without closing it (caller is responsible).
    """
    db = Database(":memory:")
    db.connect()
    run_migrations(db)
    return db


def test_needs_bootstrap_true_when_no_runs():
    """Empty runs table → bootstrap needed."""
    db = _make_db()
    assert _needs_bootstrap(db) is True


def test_needs_bootstrap_true_when_only_failed():
    """A failed bootstrap does not count — should retry."""
    db = _make_db()
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES ('r1', datetime('now'), 'failed', 'bootstrap', 0)"
    )
    db.commit()
    assert _needs_bootstrap(db) is True


def test_needs_bootstrap_true_when_only_abandoned():
    """An abandoned bootstrap (killed mid-run) does not count — should retry."""
    db = _make_db()
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES ('r1', datetime('now'), 'abandoned', 'bootstrap', 0)"
    )
    db.commit()
    assert _needs_bootstrap(db) is True


def test_needs_bootstrap_true_when_only_running():
    """A stuck 'running' bootstrap does not count — reconcile_stale_runs handles it."""
    db = _make_db()
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES ('r1', datetime('now'), 'running', 'bootstrap', 0)"
    )
    db.commit()
    assert _needs_bootstrap(db) is True


def test_needs_bootstrap_false_when_completed():
    """A completed bootstrap → no bootstrap needed."""
    db = _make_db()
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES ('r1', datetime('now'), 'completed', 'bootstrap', 0)"
    )
    db.commit()
    assert _needs_bootstrap(db) is False


def test_needs_bootstrap_false_ignores_non_bootstrap_runs():
    """Completed scheduler runs don't satisfy the bootstrap check."""
    db = _make_db()
    db.execute(
        "INSERT INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES ('r1', datetime('now'), 'completed', 'scheduler', 0)"
    )
    db.commit()
    assert _needs_bootstrap(db) is True


# ------------------------------------------------------------------
# _run_auto_bootstrap
# ------------------------------------------------------------------

@patch("mailsort.scheduler.run_bootstrap")
@patch("mailsort.scheduler._needs_bootstrap")
def test_run_auto_bootstrap_runs_when_needed(mock_needs, mock_bootstrap):
    """When no completed bootstrap exists, run_bootstrap should be called."""
    mock_needs.return_value = True
    mock_bootstrap.return_value = BootstrapReport(folders_scanned=5, rules_created=3)

    cfg = _make_config()
    db, jmap, tree = MagicMock(), MagicMock(), MagicMock()

    result = _run_auto_bootstrap(cfg, db, jmap, tree)

    assert result is True
    mock_bootstrap.assert_called_once_with(cfg, db, jmap, tree)


@patch("mailsort.scheduler.run_bootstrap")
@patch("mailsort.scheduler._needs_bootstrap")
def test_run_auto_bootstrap_skips_when_not_needed(mock_needs, mock_bootstrap):
    """When a completed bootstrap exists, run_bootstrap should NOT be called."""
    mock_needs.return_value = False

    cfg = _make_config()
    db, jmap, tree = MagicMock(), MagicMock(), MagicMock()

    result = _run_auto_bootstrap(cfg, db, jmap, tree)

    assert result is False
    mock_bootstrap.assert_not_called()


@patch("mailsort.scheduler.run_bootstrap")
@patch("mailsort.scheduler._needs_bootstrap")
def test_run_auto_bootstrap_returns_true_on_errors(mock_needs, mock_bootstrap):
    """Even if bootstrap has errors, it was attempted — return True to skip classification."""
    mock_needs.return_value = True
    mock_bootstrap.return_value = BootstrapReport(errors=["JMAP timeout"])

    cfg = _make_config()
    result = _run_auto_bootstrap(cfg, MagicMock(), MagicMock(), MagicMock())

    assert result is True


# ------------------------------------------------------------------
# _scheduled_run — auto-bootstrap integration
# ------------------------------------------------------------------

@patch("mailsort.scheduler._run_auto_bootstrap", return_value=True)
@patch("mailsort.scheduler.run_classification_pass")
@patch("mailsort.scheduler.MailboxTree")
@patch("mailsort.scheduler.JMAPClient")
@patch("mailsort.scheduler.run_migrations")
def test_scheduled_run_bootstraps_and_skips_classification(
    mock_migrations, mock_jmap_cls, mock_tree_cls, mock_run_pass, mock_auto_bootstrap,
):
    """When auto-bootstrap runs, classification should be skipped this tick."""
    cfg = _make_config()

    mock_jmap = MagicMock()
    mock_jmap_cls.return_value = mock_jmap
    mock_jmap.get_all_mailboxes.return_value = []
    mock_tree_cls.build.return_value = MagicMock()

    with patch("mailsort.scheduler.Database") as mock_db_cls:
        mock_db_instance = MagicMock()
        mock_db_cls.return_value.__enter__ = MagicMock(return_value=mock_db_instance)
        mock_db_cls.return_value.__exit__ = MagicMock(return_value=False)
        _scheduled_run(cfg=cfg)

    mock_auto_bootstrap.assert_called_once()
    mock_run_pass.assert_not_called()


@patch("mailsort.scheduler._run_auto_bootstrap", return_value=False)
@patch("mailsort.scheduler.run_classification_pass")
@patch("mailsort.scheduler.MailboxTree")
@patch("mailsort.scheduler.JMAPClient")
@patch("mailsort.scheduler.run_migrations")
def test_scheduled_run_classifies_after_bootstrap_done(
    mock_migrations, mock_jmap_cls, mock_tree_cls, mock_run_pass, mock_auto_bootstrap,
):
    """When bootstrap is already completed, classification should proceed normally."""
    cfg = _make_config()

    mock_jmap = MagicMock()
    mock_jmap_cls.return_value = mock_jmap
    mock_jmap.get_all_mailboxes.return_value = []
    mock_tree_cls.build.return_value = MagicMock()

    mock_run_pass.return_value = RunResult(run_id="run-456", dry_run=False, read_only_downgrade=False)
    mock_db_instance = MagicMock()
    mock_db_instance.execute.return_value.fetchone.return_value = {
        "status": "completed", "emails_seen": 5, "emails_moved": 3,
        "error_summary": None, "run_id": "run-456",
    }

    with patch("mailsort.scheduler.Database") as mock_db_cls:
        mock_db_cls.return_value.__enter__ = MagicMock(return_value=mock_db_instance)
        mock_db_cls.return_value.__exit__ = MagicMock(return_value=False)
        _scheduled_run(cfg=cfg)

    mock_auto_bootstrap.assert_called_once()
    mock_run_pass.assert_called_once()
