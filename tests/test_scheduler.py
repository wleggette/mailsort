"""Tests for the scheduler module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.scheduler import _scheduled_run


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

    mock_run_pass.return_value = "run-123"
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
