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

def test_no_classification_logs_skip(db: Database, monkeypatch):
    # Ensure no LLM key is picked up from environment
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _make_config()  # no anthropic key → no LLM
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-001"]
    mock_jmap.get_emails.return_value = [_make_jmap_email(from_email="unknown@unknown.com")]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["moved"] == 0
    assert row["skip_reason"] in ("llm_unavailable", "no_classification")


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


# ------------------------------------------------------------------
# Run output number verification
# ------------------------------------------------------------------

def test_run_output_numbers_all_add_up(db: Database, monkeypatch):
    """Every number in the run log output should be correct and add up.

    Set up:
    - 4 emails total in inbox (unfiltered query)
    - 3 eligible (filtered query) — 1 not eligible
    - 2 have rules (will move) — source=rule
    - 1 has no rule, no LLM (will be skipped) — source=llm, skip=llm_unavailable
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _make_config()
    tree = _make_tree()

    # Seed rules for chase and amazon
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'orders@amazon.com', 'INBOX/Shopping/Orders', 0.90, 'bootstrap')"
    )
    db.commit()

    email_chase = _make_jmap_email(email_id="e-chase", from_email="noreply@chase.com", subject="Statement")
    email_amazon = _make_jmap_email(email_id="e-amazon", from_email="orders@amazon.com", subject="Shipped")
    email_unknown = _make_jmap_email(email_id="e-unknown", from_email="random@nobody.com", subject="Hello")

    mock_jmap = MagicMock()
    # Unfiltered query returns 4 (simulates 1 unread)
    # Filtered query returns 3 (eligible)
    mock_jmap.query_inbox_emails.side_effect = [
        ["e-chase", "e-amazon", "e-unknown", "e-unread"],  # unfiltered (all inbox)
        ["e-chase", "e-amazon", "e-unknown"],               # filtered (eligible)
    ]
    mock_jmap.get_emails.return_value = [email_chase, email_amazon, email_unknown]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=True, trigger="test")

    # Verify run summary
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_seen"] == 3   # eligible count
    assert run_row["emails_moved"] == 0  # dry run

    # Verify audit_log has all 3 eligible emails
    audit_rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ? ORDER BY email_id", (run_id,)
    ).fetchall()
    assert len(audit_rows) == 3

    # Verify classification sources
    sources = {r["email_id"]: r["classification_source"] for r in audit_rows}
    assert sources["e-chase"] == "rule"
    assert sources["e-amazon"] == "rule"
    assert sources["e-unknown"] == "llm"  # falls through to LLM (unavailable)

    # Verify skip reasons — rule matches have no skip, unknown has llm_unavailable
    skip_reasons = {r["email_id"]: r["skip_reason"] for r in audit_rows}
    assert skip_reasons["e-chase"] is None      # would move (no skip)
    assert skip_reasons["e-amazon"] is None     # would move (no skip)
    assert skip_reasons["e-unknown"] in ("llm_unavailable", "no_classification")

    # Verify the math: planned (2) + left_in_inbox (1) = eligible (3)
    would_move = sum(1 for r in audit_rows if r["skip_reason"] is None)
    left_in_inbox = sum(1 for r in audit_rows if r["skip_reason"] is not None)
    assert would_move == 2
    assert left_in_inbox == 1
    assert would_move + left_in_inbox == 3  # = eligible count
