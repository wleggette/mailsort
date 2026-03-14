"""Tests for the learning module: manual sort detection, auto-rule generation, confidence decay."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.audit.learner import Learner
from mailsort.classifier.rules import RuleEngine
from mailsort.config import ClassificationConfig, ThresholdsConfig
from mailsort.db.database import Database
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail, JMAPMailbox


def _make_tree() -> MailboxTree:
    mailboxes = [
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-orders", name="Orders", parentId="mb-shopping"),
        JMAPMailbox(id="mb-shopping", name="Shopping", parentId="mb-inbox"),
    ]
    return MailboxTree.build(mailboxes)


def _make_learner(db: Database) -> Learner:
    rule_engine = RuleEngine(db, ThresholdsConfig())
    return Learner(db, rule_engine, ClassificationConfig())


def _seed_audit_row(db: Database, *, email_id: str, from_address: str,
                    from_domain: str, target_folder: str, moved: bool = True,
                    list_id: str | None = None, run_id: str = "run-seed"):
    """Helper to insert a row into audit_log."""
    # Ensure the run exists
    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES (?, datetime('now'), 'completed')",
        (run_id,),
    )
    db.execute(
        "INSERT INTO audit_log "
        "(run_id, email_id, from_address, from_domain, list_id, "
        " source_folder, target_folder, confidence, classification_source, moved) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, email_id, from_address, from_domain, list_id,
         "INBOX", target_folder, 0.95, "rule", moved),
    )
    db.commit()


# ------------------------------------------------------------------
# Auto-rule generation: exact_sender
# ------------------------------------------------------------------

def test_auto_rule_exact_sender(db: Database):
    learner = _make_learner(db)

    # Seed 3 moves from the same sender to the same folder (threshold = 3)
    for i in range(3):
        _seed_audit_row(db, email_id=f"e-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    rule_id = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert rule_id is not None

    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["rule_type"] == "exact_sender"
    assert row["condition_value"] == "noreply@chase.com"
    assert row["source"] == "auto"


def test_auto_rule_not_created_below_threshold(db: Database):
    learner = _make_learner(db)

    # Only 2 moves — below the exact_sender threshold of 3
    for i in range(2):
        _seed_audit_row(db, email_id=f"e-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    rule_id = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert rule_id is None


# ------------------------------------------------------------------
# Auto-rule generation: list_id (lower threshold)
# ------------------------------------------------------------------

def test_auto_rule_list_id(db: Database):
    learner = _make_learner(db)

    # list_id threshold is 2
    for i in range(2):
        _seed_audit_row(db, email_id=f"e-{i}", from_address=f"bot{i}@github.com",
                        from_domain="github.com", target_folder="INBOX/Tech/GitHub",
                        list_id="github-notifications.github.com")

    rule_id = learner.maybe_create_rule(
        from_address="bot0@github.com",
        from_domain="github.com",
        list_id="github-notifications.github.com",
        target_folder="INBOX/Tech/GitHub",
    )
    assert rule_id is not None

    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["rule_type"] == "list_id"


# ------------------------------------------------------------------
# Auto-rule generation: domain (coherence check)
# ------------------------------------------------------------------

def test_auto_rule_domain_with_coherence(db: Database):
    learner = _make_learner(db)

    # 5 emails from 3 distinct senders at chase.com, all going to Banks
    for i in range(5):
        sender = f"user{i % 3}@chase.com"
        _seed_audit_row(db, email_id=f"e-{i}", from_address=sender,
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    rule_id = learner.maybe_create_rule(
        from_address="user0@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert rule_id is not None

    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["rule_type"] == "sender_domain"


def test_auto_rule_domain_rejected_low_coherence(db: Database):
    learner = _make_learner(db)

    # 3 from amazon.com → Orders, 3 from amazon.com → Receipts
    # Coherence = 50% — below the 80% threshold
    for i in range(3):
        _seed_audit_row(db, email_id=f"orders-{i}", from_address=f"s{i}@amazon.com",
                        from_domain="amazon.com", target_folder="INBOX/Shopping/Orders")
    for i in range(3):
        _seed_audit_row(db, email_id=f"other-{i}", from_address=f"s{i+3}@amazon.com",
                        from_domain="amazon.com", target_folder="INBOX/Affairs/Banks")

    rule_id = learner.maybe_create_rule(
        from_address="s0@amazon.com",
        from_domain="amazon.com",
        list_id=None,
        target_folder="INBOX/Shopping/Orders",
    )
    # Domain rule should be rejected, and exact_sender shouldn't meet threshold (only 1 move each)
    assert rule_id is None


def test_auto_rule_not_duplicated(db: Database):
    learner = _make_learner(db)

    for i in range(3):
        _seed_audit_row(db, email_id=f"e-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    rule_id_1 = learner.maybe_create_rule(
        from_address="noreply@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )
    rule_id_2 = learner.maybe_create_rule(
        from_address="noreply@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )
    assert rule_id_1 is not None
    assert rule_id_2 is None  # already exists


# ------------------------------------------------------------------
# Manual sort detection
# ------------------------------------------------------------------

def test_detect_skipped_email_moved_by_user(db: Database):
    tree = _make_tree()
    learner = _make_learner(db)

    # Seed a skipped email
    _seed_audit_row(db, email_id="e-skip", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX",
                    moved=False)

    # Mock JMAP: the email is now in Banks (user moved it)
    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-skip"
    email.mailbox_ids = {"mb-banks": True}  # no longer in inbox
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-detect', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-detect")
    assert found == 1

    # Should have a manual sort logged
    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-skip' AND classification_source='manual'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 1


def test_detect_correction_of_mailsort_move(db: Database):
    tree = _make_tree()
    learner = _make_learner(db)

    # Seed: mailsort moved email to Orders, but user relocated to Banks
    _seed_audit_row(db, email_id="e-moved", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-moved"
    email.mailbox_ids = {"mb-banks": True}  # user moved to Banks
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-detect', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-detect")
    assert found == 1

    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-moved' AND classification_source='manual'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"


# ------------------------------------------------------------------
# Rule confidence adjustment
# ------------------------------------------------------------------

def test_confidence_decay_on_stale_rules(db: Database):
    learner = _make_learner(db)

    # Create a rule that was last hit 100 days ago
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, last_hit_at) "
        "VALUES ('exact_sender', 'old@example.com', 'INBOX/Affairs/Banks', "
        "0.90, 'auto', datetime('now', '-100 days'))"
    )
    db.commit()

    adjusted = learner.adjust_rule_confidence()
    assert adjusted == 1

    row = db.execute("SELECT confidence FROM rules WHERE condition_value='old@example.com'").fetchone()
    assert row["confidence"] == 0.80  # 0.90 - 0.10


def test_confidence_not_decayed_for_recent_rules(db: Database):
    learner = _make_learner(db)

    # Rule hit 30 days ago — should NOT be decayed
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, last_hit_at) "
        "VALUES ('exact_sender', 'recent@example.com', 'INBOX/Affairs/Banks', "
        "0.90, 'auto', datetime('now', '-30 days'))"
    )
    db.commit()

    adjusted = learner.adjust_rule_confidence()
    assert adjusted == 0


def test_confidence_floor_at_050(db: Database):
    learner = _make_learner(db)

    # Rule at 0.55 with stale hit — should only go to 0.50, not below
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, last_hit_at) "
        "VALUES ('exact_sender', 'floor@example.com', 'INBOX/Affairs/Banks', "
        "0.55, 'auto', datetime('now', '-100 days'))"
    )
    db.commit()

    learner.adjust_rule_confidence()
    row = db.execute("SELECT confidence FROM rules WHERE condition_value='floor@example.com'").fetchone()
    assert row["confidence"] == 0.50


# ------------------------------------------------------------------
# Option C: Inbox departure detection (snapshot diff)
# ------------------------------------------------------------------

def _make_jmap_email_obj(email_id: str, from_email: str, mailbox_ids: dict,
                         list_id: str | None = None) -> JMAPEmail:
    data = {
        "id": email_id,
        "threadId": f"thread-{email_id}",
        "mailboxIds": mailbox_ids,
        "from": [{"name": "Test", "email": from_email}],
        "to": [{"email": "user@fastmail.com"}],
        "subject": f"Email from {from_email}",
        "receivedAt": "2026-03-10T10:00:00Z",
        "keywords": {"$seen": True},
        "preview": "Test preview.",
    }
    if list_id:
        data["header:list-id:asText"] = list_id
    return JMAPEmail.model_validate(data)


def test_inbox_departure_detected(db: Database):
    """Email in previous snapshot but gone from inbox → detected as manual sort."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Create a completed run with a snapshot containing "e-departed"
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('prev-run', datetime('now', '-1 hour'), 'completed')"
    )
    db.execute(
        "INSERT INTO inbox_snapshot (email_id, run_id) VALUES ('e-departed', 'prev-run')"
    )
    db.execute(
        "INSERT INTO inbox_snapshot (email_id, run_id) VALUES ('e-still-here', 'prev-run')"
    )
    db.commit()

    # Current inbox no longer has "e-departed" but still has "e-still-here"
    current_inbox_ids = {"e-still-here", "e-new"}

    # Mock JMAP: e-departed is now in Banks
    mock_jmap = MagicMock()
    departed_email = _make_jmap_email_obj("e-departed", "noreply@chase.com", {"mb-banks": True})
    mock_jmap.get_emails.return_value = [departed_email]

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('current-run', datetime('now'), 'running')"
    )
    db.commit()

    found = learner._detect_inbox_departures(mock_jmap, tree, "current-run", current_inbox_ids)
    assert found == 1

    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-departed' AND classification_source='manual'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["from_address"] == "noreply@chase.com"


def test_inbox_departure_ignores_already_processed(db: Database):
    """Emails that mailsort already processed should not be flagged as departures."""
    tree = _make_tree()
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('prev-run', datetime('now', '-1 hour'), 'completed')"
    )
    db.execute(
        "INSERT INTO inbox_snapshot (email_id, run_id) VALUES ('e-processed', 'prev-run')"
    )
    # This email was already moved by mailsort
    _seed_audit_row(db, email_id="e-processed", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks", moved=True)

    current_inbox_ids = set()  # email is gone from inbox

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('current-run', datetime('now'), 'running')"
    )
    db.commit()

    found = learner._detect_inbox_departures(MagicMock(), tree, "current-run", current_inbox_ids)
    assert found == 0  # should be ignored — already in audit_log


def test_inbox_departure_no_previous_snapshot(db: Database):
    """First run ever — no previous snapshot, should return 0."""
    tree = _make_tree()
    learner = _make_learner(db)

    found = learner._detect_inbox_departures(MagicMock(), tree, "run-1", {"e-1"})
    assert found == 0


def test_save_and_load_snapshot(db: Database):
    """save_inbox_snapshot should persist and _get_previous_snapshot_ids should retrieve."""
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('snap-run', datetime('now'), 'completed')"
    )
    db.commit()

    learner.save_inbox_snapshot("snap-run", ["e-1", "e-2", "e-3"])

    previous = learner._get_previous_snapshot_ids()
    assert previous == {"e-1", "e-2", "e-3"}


# ------------------------------------------------------------------
# Option B: Daily folder scan
# ------------------------------------------------------------------

def test_folder_scan_finds_unknown_emails(db: Database):
    """Emails in folders with no audit_log record should be detected."""
    tree = _make_tree()
    learner = _make_learner(db)

    email_in_banks = _make_jmap_email_obj("e-unknown", "noreply@chase.com", {"mb-banks": True})

    mock_jmap = MagicMock()
    # Only return the email when scanning the Banks folder (mb-banks)
    def query_side_effect(mailbox_id, limit=25):
        if mailbox_id == "mb-banks":
            return ["e-unknown"]
        return []
    mock_jmap.query_folder_emails.side_effect = query_side_effect
    mock_jmap.get_emails.return_value = [email_in_banks]

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('scan-run', datetime('now'), 'running')"
    )
    db.commit()

    found = learner.scan_folders_for_unknown_sorts(mock_jmap, tree, "scan-run")
    assert found == 1

    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-unknown'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["classification_source"] == "manual"


def test_folder_scan_skips_known_emails(db: Database):
    """Emails already in audit_log should be skipped by the folder scan."""
    tree = _make_tree()
    learner = _make_learner(db)

    _seed_audit_row(db, email_id="e-known", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = ["e-known"]

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('scan-run', datetime('now'), 'running')"
    )
    db.commit()

    found = learner.scan_folders_for_unknown_sorts(mock_jmap, tree, "scan-run")
    assert found == 0


def test_folder_scan_respects_daily_limit(db: Database):
    """Folder scan should not run again within 24 hours."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Mark scan as just done
    db.execute(
        "INSERT INTO learner_state (key, value) VALUES ('last_folder_scan', datetime('now'))"
    )
    db.commit()

    mock_jmap = MagicMock()
    found = learner.scan_folders_for_unknown_sorts(mock_jmap, tree, "run-1")
    assert found == 0
    mock_jmap.query_folder_emails.assert_not_called()


def test_folder_scan_runs_when_stale(db: Database):
    """Folder scan should run if last scan was more than 24 hours ago."""
    tree = _make_tree()
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO learner_state (key, value) VALUES ('last_folder_scan', datetime('now', '-25 hours'))"
    )
    db.commit()

    email = _make_jmap_email_obj("e-new", "unknown@example.com", {"mb-banks": True})
    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = ["e-new"]
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('scan-run', datetime('now'), 'running')"
    )
    db.commit()

    found = learner.scan_folders_for_unknown_sorts(mock_jmap, tree, "scan-run")
    assert found >= 1
