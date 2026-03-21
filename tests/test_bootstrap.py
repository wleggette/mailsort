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
    # Only return emails when scanning the Banks folder
    def query_side_effect(mailbox_id, limit=50):
        if mailbox_id == "mb-banks":
            return [e.id for e in emails]
        return []
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = emails
    mock_jmap.get_contacts.return_value = []

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.folders_scanned >= 1
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
    def query_side_effect(mailbox_id, limit=50):
        if mailbox_id == "mb-banks":
            return [e.id for e in emails]
        return []
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = emails
    mock_jmap.get_contacts.return_value = []

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
    mock_jmap.get_contacts.return_value = []

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
    mock_jmap.get_contacts.return_value = []

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.rules_created == 0
    assert report.emails_sampled == 0
    # Run should still be completed
    row = db.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row["status"] == "completed"


def test_bootstrap_idempotent_no_duplicate_evidence(db: Database):
    """Running bootstrap twice should not duplicate audit_log evidence rows."""
    cfg = _make_config()
    tree = _make_tree()

    emails = [
        _make_jmap_email(f"e-{i}", "noreply@chase.com") for i in range(3)
    ]

    def query_side_effect(mailbox_id, limit=50):
        if mailbox_id == "mb-banks":
            return [e.id for e in emails]
        return []

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = emails
    mock_jmap.get_contacts.return_value = []

    # First bootstrap
    report1 = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)
    assert report1.emails_sampled == 3

    # Second bootstrap — same emails should be skipped
    report2 = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)
    assert report2.emails_sampled == 0

    # Total audit_log rows should still be 3, not 6
    count = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count == 3


def test_bootstrap_run_record_created(db: Database):
    """Bootstrap should create a run record with trigger='bootstrap' and status='completed'."""
    cfg = _make_config()
    tree = _make_tree()

    emails = [_make_jmap_email(f"e-{i}", "noreply@chase.com") for i in range(3)]

    mock_jmap = MagicMock()
    def query_side_effect(mailbox_id, limit=50):
        if mailbox_id == "mb-banks":
            return [e.id for e in emails]
        return []
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = emails
    mock_jmap.get_contacts.return_value = []

    run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    row = db.execute(
        "SELECT * FROM runs WHERE trigger = 'bootstrap' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert row["emails_seen"] == 3
    assert row["emails_moved"] == 0


def test_bootstrap_skips_deleted_folder_evidence(db: Database):
    """Rules should not be created for evidence pointing to deleted folders."""
    cfg = _make_config()

    # Build a tree WITH a "Deals" folder
    mailboxes = [
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-deals", name="Deals", parentId="mb-shopping"),
        JMAPMailbox(id="mb-shopping", name="Shopping", parentId="mb-inbox"),
    ]
    tree_with_deals = MailboxTree.build(mailboxes)

    # Seed evidence for Deals folder via first bootstrap
    deals_emails = [_make_jmap_email(f"deals-{i}", "promos@deals.com") for i in range(3)]
    for e in deals_emails:
        e.mailbox_ids = {"mb-deals": True}
    banks_emails = [_make_jmap_email(f"banks-{i}", "noreply@chase.com") for i in range(3)]

    mock_jmap = MagicMock()
    def query_with_deals(mailbox_id, limit=50):
        if mailbox_id == "mb-deals":
            return [e.id for e in deals_emails]
        if mailbox_id == "mb-banks":
            return [e.id for e in banks_emails]
        return []
    mock_jmap.query_folder_emails.side_effect = query_with_deals
    mock_jmap.get_emails.side_effect = lambda ids, *a, **kw: (
        deals_emails if ids[0].startswith("deals") else banks_emails
    )
    mock_jmap.get_contacts.return_value = []

    run_bootstrap(cfg, db, mock_jmap, tree_with_deals, max_per_folder=50)

    # Verify both rules exist
    deals_rule = db.execute("SELECT * FROM rules WHERE condition_value = 'promos@deals.com'").fetchone()
    chase_rule = db.execute("SELECT * FROM rules WHERE condition_value = 'noreply@chase.com'").fetchone()
    assert deals_rule is not None
    assert chase_rule is not None

    # Now "delete" the Deals folder — rebuild tree without it
    tree_without_deals = _make_tree()  # no Deals folder

    # Second bootstrap with the smaller tree — deals rule should be deactivated
    mock_jmap2 = MagicMock()
    mock_jmap2.query_folder_emails.side_effect = lambda mid, limit=50: (
        [e.id for e in banks_emails] if mid == "mb-banks" else []
    )
    mock_jmap2.get_emails.return_value = banks_emails
    mock_jmap2.get_contacts.return_value = []

    run_bootstrap(cfg, db, mock_jmap2, tree_without_deals, max_per_folder=50)

    # Deals rule should be deactivated (reconcile_folders ran)
    deals_rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'promos@deals.com'"
    ).fetchone()
    assert deals_rule["active"] == 0

    # Chase rule should still be active
    chase_rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'noreply@chase.com'"
    ).fetchone()
    assert chase_rule["active"] == 1


def test_bootstrap_coverage_calculation(db: Database):
    """Coverage check should report correct match/unmatch counts."""
    cfg = _make_config()
    tree = _make_tree()

    # 3 emails from chase (will create rule) + 2 from rare (below threshold, no rule)
    chase_emails = [_make_jmap_email(f"chase-{i}", "noreply@chase.com") for i in range(3)]
    rare_emails = [_make_jmap_email(f"rare-{i}", "rare@oneoff.com") for i in range(2)]
    all_emails = chase_emails + rare_emails

    mock_jmap = MagicMock()
    def query_side_effect(mailbox_id, limit=50):
        if mailbox_id == "mb-banks":
            return [e.id for e in all_emails]
        return []
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = all_emails
    mock_jmap.get_contacts.return_value = []

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    # Chase: 3 emails → exact_sender rule created → 3 matched
    # Rare: 2 emails → below threshold → 0 matched
    assert report.emails_matched_by_rules == 3
    assert report.emails_unmatched == 2


def test_bootstrap_jmap_error_handled(db: Database):
    cfg = _make_config()
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.side_effect = ConnectionError("JMAP down")

    report = run_bootstrap(cfg, db, mock_jmap, tree, max_per_folder=50)

    assert report.emails_sampled == 0
    assert len(report.errors) > 0
