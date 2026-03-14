"""Tests for the bootstrap module (mocked JMAP, real DB + rules)."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.bootstrap import run_bootstrap
from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail, JMAPMailbox


def _make_config() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token",
        anthropic_api_key="",
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


def _make_jmap_email(email_id: str, from_email: str, list_id: str | None = None) -> JMAPEmail:
    data = {
        "id": email_id,
        "threadId": f"thread-{email_id}",
        "mailboxIds": {"mb-banks": True},
        "from": [{"name": "Test", "email": from_email}],
        "to": [{"email": "user@fastmail.com"}],
        "subject": f"Email from {from_email}",
        "receivedAt": "2026-03-10T10:00:00Z",
        "keywords": {"$seen": True},
        "preview": "Test email preview.",
    }
    if list_id:
        data["header:list-id:asText"] = list_id
    return JMAPEmail.model_validate(data)


# ------------------------------------------------------------------
# Bootstrap creates rules from folder evidence
# ------------------------------------------------------------------

def test_bootstrap_creates_sender_rules(db: Database):
    """Bootstrap should create exact_sender rules when threshold is met."""
    cfg = _make_config()
    tree = _make_tree()

    # Simulate 3 emails from chase.com in the Banks folder
    emails = [
        _make_jmap_email(f"e-{i}", "noreply@chase.com") for i in range(3)
    ]

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = [e.id for e in emails]
    mock_jmap.get_emails.return_value = emails

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.folders_scanned > 0
    assert report.emails_sampled >= 3

    # Should have created an exact_sender rule for chase
    rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'noreply@chase.com'"
    ).fetchone()
    assert rule is not None
    assert rule["rule_type"] == "exact_sender"
    assert rule["source"] == "auto"


def test_bootstrap_creates_list_id_rules(db: Database):
    """Bootstrap should create list_id rules at lower threshold (2)."""
    cfg = _make_config()
    tree = _make_tree()

    emails = [
        _make_jmap_email(f"e-{i}", f"bot{i}@github.com", list_id="notifications.github.com")
        for i in range(2)
    ]

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = [e.id for e in emails]
    mock_jmap.get_emails.return_value = emails

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'notifications.github.com'"
    ).fetchone()
    assert rule is not None
    assert rule["rule_type"] == "list_id"


def test_bootstrap_generates_folder_descriptions(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    emails = [_make_jmap_email("e-0", "noreply@chase.com")]

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = ["e-0"]
    mock_jmap.get_emails.return_value = emails

    run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    row = db.execute(
        "SELECT * FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row is not None
    assert row["source"] == "auto"


def test_bootstrap_empty_folders_no_crash(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.get_emails.return_value = []

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.rules_created == 0
    assert report.emails_sampled == 0
    # Run should still be completed
    row = db.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row["status"] == "completed"


def test_bootstrap_jmap_error_handled(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.side_effect = ConnectionError("JMAP down")

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.emails_sampled == 0
    assert len(report.errors) > 0
