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
                    list_id: str | None = None, run_id: str = "run-seed",
                    rule_id: int | None = None,
                    classification_source: str = "rule"):
    """Helper to insert a row into audit_log."""
    # Ensure the run exists
    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES (?, datetime('now'), 'completed')",
        (run_id,),
    )
    db.execute(
        "INSERT INTO audit_log "
        "(run_id, email_id, from_address, from_domain, list_id, "
        " source_folder, target_folder, confidence, classification_source, moved, rule_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, email_id, from_address, from_domain, list_id,
         "INBOX", target_folder, 0.95, classification_source, moved, rule_id),
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


def test_auto_rule_exact_sender_rejected_low_coherence(db: Database):
    """A sender that appears across multiple folders with low coherence should NOT get a rule."""
    learner = _make_learner(db)

    # noreply@okta.com: 3 to Alerts, 5 to other folders → coherence = 3/8 = 37%
    for i in range(3):
        _seed_audit_row(db, email_id=f"okta-alerts-{i}", from_address="noreply@okta.com",
                        from_domain="okta.com", target_folder="INBOX/Affairs/Alerts")
    for i in range(5):
        _seed_audit_row(db, email_id=f"okta-other-{i}", from_address="noreply@okta.com",
                        from_domain="okta.com", target_folder="INBOX/Affairs/Banks")

    # Count threshold met (3 >= 3) but coherence too low (37% < 80%)
    rule_id = learner.maybe_create_rule(
        from_address="noreply@okta.com",
        from_domain="okta.com",
        list_id=None,
        target_folder="INBOX/Affairs/Alerts",
    )
    assert rule_id is None


def test_auto_rule_list_id_rejected_low_coherence(db: Database):
    """A list_id that appears across multiple folders should NOT get a rule."""
    learner = _make_learner(db)

    # Same list_id sent to two different folders: 2 to Newsletters, 2 to Spam
    # Coherence = 2/4 = 50% — below the 80% threshold
    for i in range(2):
        _seed_audit_row(db, email_id=f"news-{i}", from_address=f"bot{i}@news.com",
                        from_domain="news.com", target_folder="INBOX/Social/Newsletters",
                        list_id="<weekly-digest.news.com>")
    for i in range(2):
        _seed_audit_row(db, email_id=f"spam-{i}", from_address=f"bot{i+2}@news.com",
                        from_domain="news.com", target_folder="INBOX/Affairs/Banks",
                        list_id="<weekly-digest.news.com>")

    rule_id = learner.maybe_create_rule(
        from_address="bot0@news.com",
        from_domain="news.com",
        list_id="<weekly-digest.news.com>",
        target_folder="INBOX/Social/Newsletters",
    )
    assert rule_id is None


def test_auto_rule_exact_sender_high_coherence_created(db: Database):
    """A sender with enough volume AND high coherence SHOULD get a rule."""
    learner = _make_learner(db)

    # 10 to Banks, 1 to Alerts → coherence = 10/11 = 91%
    for i in range(10):
        _seed_audit_row(db, email_id=f"chase-banks-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")
    _seed_audit_row(db, email_id="chase-alert-0", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Alerts")

    rule_id = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert rule_id is not None

    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["rule_type"] == "exact_sender"


def test_auto_rule_personal_sender_across_many_folders_rejected(db: Database):
    """A personal sender (like a spouse) who emails about many topics should NOT get a rule."""
    learner = _make_learner(db)

    # husband@gmail.com sends to 5 different folders — coherence per folder is ~20%
    folders = [
        "INBOX/Affairs/Banks",
        "INBOX/Affairs/Medical",
        "INBOX/Shopping/Orders",
        "INBOX/Projects/2025/Taxes",
        "INBOX/People/Family",
    ]
    for i, folder in enumerate(folders):
        for j in range(3):
            _seed_audit_row(db, email_id=f"husband-{i}-{j}", from_address="husband@gmail.com",
                            from_domain="gmail.com", target_folder=folder)

    # Try to create a rule for any one folder — should fail (coherence = 3/15 = 20%)
    rule_id = learner.maybe_create_rule(
        from_address="husband@gmail.com",
        from_domain="gmail.com",
        list_id=None,
        target_folder="INBOX/People/Family",
    )
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
    """Category 1 (skipped sort) should count as from_inbox."""
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
    assert found.total == 1
    assert found.from_inbox == 1  # skipped sort = from inbox
    assert found.from_other == 0

    # Should have a manual sort logged
    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-skip' AND classification_source='manual'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 1


def test_detect_correction_of_mailsort_move(db: Database):
    """Category 2 (correction sort) should count as from_other."""
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
    assert found.total == 1
    assert found.from_inbox == 0
    assert found.from_other == 1  # correction sort = from other

    row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-moved' AND classification_source='manual'"
    ).fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"


def test_inbox_departure_counts_as_from_inbox(db: Database):
    """Category 3 (inbox departure) should count as from_inbox."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Set up a previous snapshot with "e-departed"
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('prev-run', datetime('now', '-1 hour'), 'completed')"
    )
    db.execute(
        "INSERT INTO inbox_snapshot (email_id, run_id) VALUES ('e-departed', 'prev-run')"
    )
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('current-run', datetime('now'), 'running')"
    )
    db.commit()

    # e-departed is now in Banks (user sorted it from inbox before we processed)
    mock_jmap = MagicMock()
    departed_email = _make_jmap_email_obj("e-departed", "noreply@chase.com", {"mb-banks": True})
    mock_jmap.get_emails.return_value = [departed_email]

    # Current inbox no longer has e-departed
    found = learner.detect_manual_sorts(mock_jmap, tree, "current-run", current_inbox_ids=set())
    assert found.from_inbox == 1  # inbox departure = from inbox
    assert found.from_other == 0
    assert found.total == 1


def test_manual_sort_counts_total_property():
    """ManualSortCounts.total should sum from_inbox + from_other."""
    from mailsort.audit.learner import ManualSortCounts
    counts = ManualSortCounts(from_inbox=3, from_other=5)
    assert counts.total == 8


def test_folder_scan_counts_as_from_other():
    """Cat 4 (folder scan) should be added to from_other by the orchestrator.

    The learner returns folder scan count separately; the orchestrator adds it
    to sort_counts.from_other. This test verifies the documented contract.
    """
    from mailsort.audit.learner import ManualSortCounts
    sort_counts = ManualSortCounts(from_inbox=1, from_other=2)
    folder_scan_sorts = 3
    sort_counts.from_other += folder_scan_sorts  # orchestrator pattern
    assert sort_counts.from_inbox == 1
    assert sort_counts.from_other == 5  # 2 (cat 2) + 3 (cat 4)
    assert sort_counts.total == 6


def test_multiple_categories_accumulate_correctly(db: Database):
    """When both Cat 1 (skipped) and Cat 2 (correction) fire, counts go to correct buckets."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Cat 1: a skipped email the user moved from inbox
    _seed_audit_row(db, email_id="e-skip-1", from_address="a@example.com",
                    from_domain="example.com", target_folder="INBOX", moved=False)

    # Cat 2: a mailsort-moved email the user relocated
    _seed_audit_row(db, email_id="e-correction-1", from_address="b@example.com",
                    from_domain="example.com", target_folder="INBOX/Shopping/Orders",
                    moved=True)
    _seed_audit_row(db, email_id="e-correction-2", from_address="c@example.com",
                    from_domain="example.com", target_folder="INBOX/Shopping/Orders",
                    moved=True)

    mock_jmap = MagicMock()

    # For Cat 1: e-skip-1 is now in Banks
    skip_email = MagicMock()
    skip_email.id = "e-skip-1"
    skip_email.mailbox_ids = {"mb-banks": True}

    # For Cat 2: both corrections are now in Banks (relocated from Orders)
    corr_email_1 = MagicMock()
    corr_email_1.id = "e-correction-1"
    corr_email_1.mailbox_ids = {"mb-banks": True}
    corr_email_2 = MagicMock()
    corr_email_2.id = "e-correction-2"
    corr_email_2.mailbox_ids = {"mb-banks": True}

    # get_emails is called twice: once for Cat 1, once for Cat 2
    mock_jmap.get_emails.side_effect = [
        [skip_email],          # Cat 1: skipped sorts
        [corr_email_1, corr_email_2],  # Cat 2: correction sorts
    ]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-multi', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-multi")
    assert found.from_inbox == 1   # Cat 1: 1 skipped sort
    assert found.from_other == 2   # Cat 2: 2 correction sorts
    assert found.total == 3


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


# ------------------------------------------------------------------
# Feedback loop: confidence penalty on corrections
# ------------------------------------------------------------------

def _seed_rule(db: Database, *, rule_id: int = 1, condition_value: str = "noreply@chase.com",
               target_folder: str = "INBOX/Shopping/Orders", confidence: float = 0.90,
               rule_type: str = "exact_sender") -> int:
    """Insert a rule and return its ID."""
    db.execute(
        "INSERT INTO rules (id, rule_type, condition_value, target_folder_path, "
        "confidence, source, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'auto', 1, datetime('now'), datetime('now'))",
        (rule_id, rule_type, condition_value, target_folder, confidence),
    )
    db.commit()
    return rule_id


def test_correction_penalizes_originating_rule(db: Database):
    """When a user relocates a mailsort-moved email, the originating rule loses confidence."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Start at 1.0 so after −0.15 → 0.85, which equals the threshold (not below)
    rid = _seed_rule(db, confidence=1.0, target_folder="INBOX/Shopping/Orders")
    _seed_audit_row(db, email_id="e-corr", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-corr"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-pen', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-pen")
    assert found.from_other == 1

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.85  # 1.0 - 0.15
    assert row["active"] == 1  # 0.85 == threshold, not below → stays active


def test_correction_deactivates_rule_below_threshold(db: Database):
    """Rule should be deactivated when confidence drops below rule_move threshold."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Start at 0.90 — after −0.15 penalty → 0.75, which is below default rule_move (0.85)
    rid = _seed_rule(db, confidence=0.90)
    _seed_audit_row(db, email_id="e-corr", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-corr"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-deact', datetime('now'), 'running')",
    )
    db.commit()

    learner.detect_manual_sorts(mock_jmap, tree, "run-deact")

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.75
    assert row["active"] == 0  # 0.75 < 0.85 threshold → deactivated


def test_correction_dedup_skips_already_corrected(db: Database):
    """Emails that already have a manual audit_log row should not trigger another penalty."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.90)
    _seed_audit_row(db, email_id="e-dup", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid, run_id="run-original")
    # Simulate a previous correction already recorded
    _seed_audit_row(db, email_id="e-dup", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                    moved=True, classification_source="manual", run_id="run-prev-correction")

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-dup"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-dup', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-dup")
    assert found.from_other == 0  # skipped because already corrected

    row = db.execute("SELECT confidence FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.90  # unchanged


def test_correction_no_penalty_for_llm_classification(db: Database):
    """Corrections of LLM-classified emails (no rule_id) should not crash or penalize."""
    tree = _make_tree()
    learner = _make_learner(db)

    # LLM move has no rule_id
    _seed_audit_row(db, email_id="e-llm", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=None, classification_source="llm")

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-llm"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-llm', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-llm")
    assert found.from_other == 1  # correction detected
    # No crash, no rule penalty (rule_id is None)


def test_correction_penalty_floors_at_zero(db: Database):
    """Rule confidence should not go below 0.0 after penalty."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.05)
    _seed_audit_row(db, email_id="e-floor", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-floor"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-floor', datetime('now'), 'running')",
    )
    db.commit()

    learner.detect_manual_sorts(mock_jmap, tree, "run-floor")

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.0  # floored at 0, not negative
    assert row["active"] == 0  # deactivated


def test_inbox_return_not_penalized(db: Database):
    """Moving an email back to inbox should NOT trigger penalty (ambiguous intent)."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.90)
    _seed_audit_row(db, email_id="e-inbox", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-inbox"
    email.mailbox_ids = {"mb-inbox": True}  # back in inbox
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-inbox', datetime('now'), 'running')",
    )
    db.commit()

    found = learner.detect_manual_sorts(mock_jmap, tree, "run-inbox")
    assert found.from_other == 0  # not counted as correction

    row = db.execute("SELECT confidence FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.90  # unchanged
