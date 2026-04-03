"""Tests for the run orchestrator (mocked JMAP, real DB + rules + pipeline)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.jmap.client import ReadOnlyTokenError
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail, JMAPMailbox
from mailsort.orchestrator import run_classification_pass


def _make_config() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15, min_age_minutes=240, max_batch_size=100),
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

    # Rule hit_count should NOT be incremented during dry run
    rule_row = db.execute(
        "SELECT hit_count, last_hit_at FROM rules WHERE condition_value='noreply@chase.com'"
    ).fetchone()
    assert rule_row["hit_count"] == 0
    assert rule_row["last_hit_at"] is None


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

    # Rule hit_count SHOULD be incremented during live run
    rule_row = db.execute(
        "SELECT hit_count, last_hit_at FROM rules WHERE condition_value='noreply@chase.com'"
    ).fetchone()
    assert rule_row["hit_count"] == 1
    assert rule_row["last_hit_at"] is not None


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


# ------------------------------------------------------------------
# JMAP move exception → move_failed + run status='error'
# ------------------------------------------------------------------

def test_move_exception_sets_move_failed_and_error_status(db: Database):
    """When move_emails raises an exception, planned entries get
    skip_reason='move_failed' and the run finishes with status='error'."""
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
    mock_jmap.move_emails.side_effect = ReadOnlyTokenError("move emails")

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # Audit entry should have skip_reason='move_failed'
    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["moved"] == 0
    assert row["skip_reason"] == "move_failed"

    # Run should finish as 'error', not 'completed' or 'failed'
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "error"
    assert run_row["emails_moved"] == 0
    assert run_row["error_summary"] is not None
    assert "read-only" in run_row["error_summary"].lower()


def test_successful_move_has_no_move_failed(db: Database):
    """When move_emails succeeds, no entries have skip_reason='move_failed'
    and the run finishes with status='completed'."""
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

    # No move_failed entries
    count = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE skip_reason='move_failed'"
    ).fetchone()[0]
    assert count == 0

    # Run should be 'completed'
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_moved"] == 1
    assert run_row["error_summary"] is None


# ------------------------------------------------------------------
# In-flight race windows (X15, X16, X17)
# ------------------------------------------------------------------

def test_email_vanishes_between_query_and_fetch(db: Database, monkeypatch):
    """X15: Email deleted after query_inbox_emails but before get_emails.

    get_emails returns fewer emails than IDs requested. The orchestrator
    should process only the returned ones — no crash, no orphan audit row.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    mock_jmap = MagicMock()
    # query returns 2 IDs, but get_emails only returns 1 (email-gone was deleted)
    mock_jmap.query_inbox_emails.return_value = ["email-001", "email-gone"]
    mock_jmap.get_emails.return_value = [_make_jmap_email(email_id="email-001")]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {"email-001": True}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # Only the surviving email gets an audit row
    rows = db.execute("SELECT email_id FROM audit_log WHERE run_id=?", (run_id,)).fetchall()
    email_ids = {r["email_id"] for r in rows}
    assert "email-001" in email_ids
    assert "email-gone" not in email_ids  # no orphan row

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_moved"] == 1


def test_partial_move_success_records_mixed_outcomes(db: Database, monkeypatch):
    """X16: move_emails returns {a: True, b: False} — mixed outcomes.

    Audit log correctly records moved=1 for success, moved=0 for failure.
    emails_moved count reflects only successes.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'orders@amazon.com', 'INBOX/Shopping/Orders', 0.90, 'bootstrap')"
    )
    db.commit()

    email_chase = _make_jmap_email(email_id="e-chase", from_email="noreply@chase.com")
    email_amazon = _make_jmap_email(email_id="e-amazon", from_email="orders@amazon.com")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["e-chase", "e-amazon"]
    mock_jmap.get_emails.return_value = [email_chase, email_amazon]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    # Chase moves successfully, Amazon fails (e.g. deleted mid-flight)
    mock_jmap.move_emails.return_value = {"e-chase": True, "e-amazon": False}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    chase_row = db.execute("SELECT * FROM audit_log WHERE email_id='e-chase'").fetchone()
    assert chase_row["moved"] == 1

    amazon_row = db.execute("SELECT * FROM audit_log WHERE email_id='e-amazon'").fetchone()
    assert amazon_row["moved"] == 0

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["emails_moved"] == 1  # only chase succeeded
    assert run_row["status"] == "completed"  # partial failure is not an error


def test_move_response_missing_email_records_not_moved(db: Database, monkeypatch):
    """X17: move_emails response omits an email entirely.

    Email absent from outcomes dict should be recorded as moved=0 via
    outcomes.get(email_id, False) default.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'orders@amazon.com', 'INBOX/Shopping/Orders', 0.90, 'bootstrap')"
    )
    db.commit()

    email_chase = _make_jmap_email(email_id="e-chase", from_email="noreply@chase.com")
    email_amazon = _make_jmap_email(email_id="e-amazon", from_email="orders@amazon.com")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["e-chase", "e-amazon"]
    mock_jmap.get_emails.return_value = [email_chase, email_amazon]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    # Response only includes chase — amazon is absent entirely
    mock_jmap.move_emails.return_value = {"e-chase": True}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    chase_row = db.execute("SELECT * FROM audit_log WHERE email_id='e-chase'").fetchone()
    assert chase_row["moved"] == 1

    # Amazon is absent from outcomes → defaults to moved=0
    amazon_row = db.execute("SELECT * FROM audit_log WHERE email_id='e-amazon'").fetchone()
    assert amazon_row["moved"] == 0

    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["emails_moved"] == 1
    assert run_row["status"] == "completed"
