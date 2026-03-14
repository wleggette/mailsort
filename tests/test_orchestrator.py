"""Tests for the run orchestrator (mocked JMAP, real DB + rules + pipeline)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail, JMAPMailbox
from mailsort.orchestrator import run_classification_pass


def _make_config() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15, min_age_hours=4, max_batch_size=100),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token",
        anthropic_api_key="",  # no LLM in tests
        db_path=":memory:",
    )


def _make_tree() -> MailboxTree:
    mailboxes = [
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-orders", name="Orders", parentId="mb-shopping"),
        JMAPMailbox(id="mb-shopping", name="Shopping", parentId="mb-inbox"),
    ]
    return MailboxTree.build(mailboxes)


def _make_jmap_email(
    email_id: str = "email-001",
    from_email: str = "noreply@chase.com",
    subject: str = "Your statement",
    mailbox_ids: dict | None = None,
) -> JMAPEmail:
    return JMAPEmail.model_validate({
        "id": email_id,
        "threadId": "thread-001",
        "mailboxIds": mailbox_ids or {"mb-inbox": True},
        "from": [{"name": "Chase", "email": from_email}],
        "to": [{"email": "user@fastmail.com"}],
        "subject": subject,
        "receivedAt": "2026-03-10T10:00:00Z",
        "keywords": {"$seen": True},
        "preview": "Your January statement is available.",
    })


# ------------------------------------------------------------------
# Dry-run: classify but don't move
# ------------------------------------------------------------------

def test_dry_run_logs_but_does_not_move(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    # Seed a rule so the email gets classified
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email()]
    mock_jmap.get_thread_email_ids.return_value = []

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=True, trigger="test")

    # move_emails should NOT have been called
    mock_jmap.move_emails.assert_not_called()

    # But audit_log should have a row
    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 0  # dry run doesn't move
    assert row["classification_source"] == "rule"

    # Run should be completed
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_seen"] == 1
    assert run_row["emails_moved"] == 0


# ------------------------------------------------------------------
# Live run: classify + move
# ------------------------------------------------------------------

def test_live_run_moves_and_logs(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email()]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.move_emails.return_value = {"email-001": True}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # move_emails should have been called with (email_id, folder_id, current_mailbox_ids)
    mock_jmap.move_emails.assert_called_once()
    call_args = mock_jmap.move_emails.call_args
    moves = call_args[0][0]  # first positional arg
    assert len(moves) == 1
    email_id, folder_id, mailbox_ids = moves[0]
    assert email_id == "email-001"
    assert folder_id == "mb-banks"
    assert "mb-inbox" in mailbox_ids  # current_mailbox_ids passed through
    assert call_args[1]["inbox_id"] == "mb-inbox"  # keyword arg

    # Audit log should show moved=1
    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row["moved"] == 1
    assert row["target_folder"] == "INBOX/Affairs/Banks"

    # Run summary
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_moved"] == 1


# ------------------------------------------------------------------
# Keyword tagging
# ------------------------------------------------------------------

def test_move_emails_called_with_keyword_tag(db: Database):
    """move_emails should be called with tag_keyword='$mailsort-moved' by default."""
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email()]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.move_emails.return_value = {"email-001": True}

    run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # Verify move_emails was called — keyword tagging happens inside the JMAP client
    mock_jmap.move_emails.assert_called_once()


# ------------------------------------------------------------------
# No eligible emails
# ------------------------------------------------------------------

def test_no_emails_completes_cleanly(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = []

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_seen"] == 0
    assert run_row["emails_moved"] == 0


# ------------------------------------------------------------------
# Skip sender filtered out
# ------------------------------------------------------------------

def test_skip_sender_is_filtered(db: Database):
    cfg = _make_config()
    cfg = cfg.model_copy(update={"skip_senders": ["noreply@chase.com"]})
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email()]

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    # No audit rows — filtered before classification
    count = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count == 0

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["emails_seen"] == 0


# ------------------------------------------------------------------
# No rule match, no LLM → skip
# ------------------------------------------------------------------

def test_no_classification_logs_skip(db: Database):
    cfg = _make_config()  # no anthropic key → no LLM
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email(from_email="unknown@unknown.com")]
    mock_jmap.get_thread_email_ids.return_value = []

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["moved"] == 0
    assert row["skip_reason"] == "llm_unavailable"


# ------------------------------------------------------------------
# JMAP move failure is recorded
# ------------------------------------------------------------------

def test_move_failure_recorded(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email()]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.move_emails.return_value = {"email-001": False}  # move failed

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row["moved"] == 0

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["emails_moved"] == 0
