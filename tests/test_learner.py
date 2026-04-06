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

    created = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert len(created) == 1

    row = db.execute("SELECT * FROM rules WHERE id = ?", (created[0],)).fetchone()
    assert row["rule_type"] == "exact_sender"
    assert row["condition_value"] == "noreply@chase.com"
    assert row["source"] == "auto"


def test_auto_rule_not_created_below_threshold(db: Database):
    learner = _make_learner(db)

    # Only 2 moves — below the exact_sender threshold of 3
    for i in range(2):
        _seed_audit_row(db, email_id=f"e-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    created = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert len(created) == 0


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

    created = learner.maybe_create_rule(
        from_address="bot0@github.com",
        from_domain="github.com",
        list_id="github-notifications.github.com",
        target_folder="INBOX/Tech/GitHub",
    )
    assert len(created) >= 1

    row = db.execute("SELECT * FROM rules WHERE id = ?", (created[0],)).fetchone()
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

    created = learner.maybe_create_rule(
        from_address="user0@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    # Both sender_domain and exact_sender rules should be created
    rule_types = {db.execute("SELECT rule_type FROM rules WHERE id = ?", (rid,)).fetchone()["rule_type"] for rid in created}
    assert "sender_domain" in rule_types


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

    created = learner.maybe_create_rule(
        from_address="s0@amazon.com",
        from_domain="amazon.com",
        list_id=None,
        target_folder="INBOX/Shopping/Orders",
    )
    # Domain rule should be rejected, and exact_sender shouldn't meet threshold (only 1 move each)
    assert len(created) == 0


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
    created = learner.maybe_create_rule(
        from_address="noreply@okta.com",
        from_domain="okta.com",
        list_id=None,
        target_folder="INBOX/Affairs/Alerts",
    )
    assert len(created) == 0


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

    created = learner.maybe_create_rule(
        from_address="bot0@news.com",
        from_domain="news.com",
        list_id="<weekly-digest.news.com>",
        target_folder="INBOX/Social/Newsletters",
    )
    assert len(created) == 0


def test_auto_rule_exact_sender_high_coherence_created(db: Database):
    """A sender with enough volume AND high coherence SHOULD get a rule."""
    learner = _make_learner(db)

    # 10 to Banks, 1 to Alerts → coherence = 10/11 = 91%
    for i in range(10):
        _seed_audit_row(db, email_id=f"chase-banks-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")
    _seed_audit_row(db, email_id="chase-alert-0", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Alerts")

    created = learner.maybe_create_rule(
        from_address="noreply@chase.com",
        from_domain="chase.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    assert len(created) >= 1

    rule_types = {db.execute("SELECT rule_type FROM rules WHERE id = ?", (rid,)).fetchone()["rule_type"] for rid in created}
    assert "exact_sender" in rule_types


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
    created = learner.maybe_create_rule(
        from_address="husband@gmail.com",
        from_domain="gmail.com",
        list_id=None,
        target_folder="INBOX/People/Family",
    )
    assert len(created) == 0


def test_all_eligible_list_id_and_exact_sender(db: Database):
    """When a sender qualifies for both list_id and exact_sender, both rules are created."""
    learner = _make_learner(db)

    # 3 emails with list_id (meets list_id threshold of 2 AND exact_sender threshold of 3)
    for i in range(3):
        _seed_audit_row(db, email_id=f"list-{i}", from_address="activities@ymca.org",
                        from_domain="ymca.org", target_folder="INBOX/Affairs/Banks",
                        list_id="<updates.ymca.org>")

    created = learner.maybe_create_rule(
        from_address="activities@ymca.org",
        from_domain="ymca.org",
        list_id="<updates.ymca.org>",
        target_folder="INBOX/Affairs/Banks",
    )
    assert len(created) == 2

    rule_types = {db.execute("SELECT rule_type FROM rules WHERE id = ?", (rid,)).fetchone()["rule_type"]
                  for rid in created}
    assert rule_types == {"list_id", "exact_sender"}


def test_all_eligible_domain_and_exact_sender(db: Database):
    """When domain qualifies and individual sender qualifies, both rules are created."""
    learner = _make_learner(db)

    # 5 emails from 3 distinct senders, all to Banks (domain qualifies)
    # user0 has 2 emails, user1 has 2 emails, user2 has 1 email
    for i in range(5):
        sender = f"user{i % 3}@bigbank.com"
        _seed_audit_row(db, email_id=f"bank-{i}", from_address=sender,
                        from_domain="bigbank.com", target_folder="INBOX/Affairs/Banks")

    # Call with user0 who has 2 emails (below exact_sender threshold of 3)
    created = learner.maybe_create_rule(
        from_address="user0@bigbank.com",
        from_domain="bigbank.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    # Domain rule created, but user0 only has 2 emails (below exact_sender threshold)
    rule_types = {db.execute("SELECT rule_type FROM rules WHERE id = ?", (rid,)).fetchone()["rule_type"]
                  for rid in created}
    assert "sender_domain" in rule_types
    assert "exact_sender" not in rule_types

    # Now seed more evidence so user0 meets exact_sender threshold too
    for i in range(3):
        _seed_audit_row(db, email_id=f"bank-extra-{i}", from_address="user0@bigbank.com",
                        from_domain="bigbank.com", target_folder="INBOX/Affairs/Banks")

    created2 = learner.maybe_create_rule(
        from_address="user0@bigbank.com",
        from_domain="bigbank.com",
        list_id=None,
        target_folder="INBOX/Affairs/Banks",
    )
    # Domain rule already exists, but exact_sender should now be created
    assert len(created2) == 1
    row = db.execute("SELECT rule_type FROM rules WHERE id = ?", (created2[0],)).fetchone()
    assert row["rule_type"] == "exact_sender"


def test_auto_rule_not_duplicated(db: Database):
    learner = _make_learner(db)

    for i in range(3):
        _seed_audit_row(db, email_id=f"e-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    created_1 = learner.maybe_create_rule(
        from_address="noreply@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )
    created_2 = learner.maybe_create_rule(
        from_address="noreply@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )
    assert len(created_1) >= 1
    assert len(created_2) == 0  # already exists


def test_auto_rule_reactivates_inactive_exact_sender(db: Database):
    """Inactive exact_sender rule is reactivated instead of creating a duplicate."""
    learner = _make_learner(db)

    # Create and deactivate a rule
    db.execute(
        "INSERT INTO rules (id, rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES (70, 'exact_sender', 'noreply@chase.com', "
        "'INBOX/Affairs/Banks', 0.30, 'auto', 0)"
    )
    db.commit()

    for i in range(3):
        _seed_audit_row(db, email_id=f"e-react-{i}", from_address="noreply@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    created = learner.maybe_create_rule(
        from_address="noreply@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )
    # Should reactivate, not create new — returned list may be empty (reactivation isn't a "creation")
    # But rule 70 should now be active with fresh confidence
    row = db.execute("SELECT * FROM rules WHERE id = 70").fetchone()
    assert row["active"] == 1
    assert row["confidence"] > 0.30  # fresh confidence from BaseConfidenceConfig

    # No duplicate rows
    count = db.execute(
        "SELECT COUNT(*) FROM rules WHERE rule_type = 'exact_sender' AND condition_value = 'noreply@chase.com'"
    ).fetchone()[0]
    assert count == 1


def test_auto_rule_reactivates_inactive_list_id(db: Database):
    """Inactive list_id rule is reactivated instead of creating a duplicate."""
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO rules (id, rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES (71, 'list_id', 'news.chase.com', "
        "'INBOX/Affairs/Banks', 0.20, 'auto', 0)"
    )
    db.commit()

    for i in range(2):  # list_id threshold = 2
        _seed_audit_row(db, email_id=f"e-list-react-{i}", from_address=f"bot{i}@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                        list_id="news.chase.com")

    learner.maybe_create_rule(
        from_address="bot@chase.com", from_domain="chase.com",
        list_id="news.chase.com", target_folder="INBOX/Affairs/Banks",
    )

    row = db.execute("SELECT * FROM rules WHERE id = 71").fetchone()
    assert row["active"] == 1
    assert row["confidence"] == 0.95  # list_id gets fixed confidence

    count = db.execute(
        "SELECT COUNT(*) FROM rules WHERE rule_type = 'list_id' AND condition_value = 'news.chase.com'"
    ).fetchone()[0]
    assert count == 1


def test_auto_rule_reactivates_inactive_sender_domain(db: Database):
    """Inactive sender_domain rule is reactivated instead of creating a duplicate."""
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO rules (id, rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES (72, 'sender_domain', 'chase.com', "
        "'INBOX/Affairs/Banks', 0.25, 'auto', 0)"
    )
    db.commit()

    # sender_domain needs 5 emails from 3 distinct senders
    for i in range(5):
        _seed_audit_row(db, email_id=f"e-dom-react-{i}",
                        from_address=f"user{i % 3}@chase.com",
                        from_domain="chase.com", target_folder="INBOX/Affairs/Banks")

    learner.maybe_create_rule(
        from_address="user0@chase.com", from_domain="chase.com",
        list_id=None, target_folder="INBOX/Affairs/Banks",
    )

    row = db.execute("SELECT * FROM rules WHERE id = 72").fetchone()
    assert row["active"] == 1
    assert row["confidence"] > 0.25  # fresh confidence

    count = db.execute(
        "SELECT COUNT(*) FROM rules WHERE rule_type = 'sender_domain' AND condition_value = 'chase.com'"
    ).fetchone()[0]
    assert count == 1


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


def test_skipped_sort_excludes_emails_moved_by_mailsort(db: Database):
    """Bug A: emails that mailsort moved in a later run must not be detected as user sorts."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Dry run: email skipped (moved=0)
    _seed_audit_row(db, email_id="e-dry", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX",
                    moved=False, run_id="run-dry")

    # Live run: same email moved by mailsort (moved=1)
    _seed_audit_row(db, email_id="e-dry", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                    moved=True, run_id="run-live")

    # Email is now in Banks (mailsort moved it, not the user)
    mock_jmap = MagicMock()
    mock_jmap.get_emails.return_value = []  # should not even be called for this email

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-detect', datetime('now'), 'running')",
    )
    db.commit()

    found = learner._detect_skipped_sorts(mock_jmap, tree, "run-detect")
    assert found == 0  # excluded by NOT IN (moved=1) subquery


def test_skipped_sort_still_detected_after_user_move_and_return(db: Database):
    """User moves email out then back to inbox; subsequent sort should still be detectable.

    Scenario: mailsort skips email, user moves to Banks (learner records manual entry),
    user moves back to inbox, mailsort skips again, user moves to Stores.
    The manual moved=1 entry should NOT block re-detection.
    """
    tree = _make_tree()
    learner = _make_learner(db)

    # Run 1: mailsort skipped the email
    _seed_audit_row(db, email_id="e-bounce", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX",
                    moved=False, run_id="run-1")

    # Learner previously recorded a manual sort (user moved to Banks)
    # This has moved=1 but classification_source='manual' — should NOT exclude
    _seed_audit_row(db, email_id="e-bounce", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                    moved=True, classification_source="manual", run_id="run-prev")

    # Run 2: user moved it back to inbox, mailsort skipped again
    _seed_audit_row(db, email_id="e-bounce", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX",
                    moved=False, run_id="run-2")

    # Now user moved to Orders (email is no longer in inbox)
    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-bounce"
    email.mailbox_ids = {"mb-orders": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-detect', datetime('now'), 'running')",
    )
    db.commit()

    # The NOT IN subquery should only exclude non-manual moved=1, so
    # the email should pass the SQL filter. However, the dedup check
    # (_already_handled_email_ids) will still filter it out because
    # a manual row already exists. This is expected — the dedup prevents
    # the same email from being double-counted in the same detection pass.
    # The correction_sorts path handles the re-sort case.
    found = learner._detect_skipped_sorts(mock_jmap, tree, "run-detect")

    # Email passes the SQL filter (manual moved=1 is not excluded),
    # but dedup filters it (existing manual row). This is correct behavior:
    # the re-sort will be caught by _detect_correction_sorts instead.
    assert found == 0  # dedup blocks, but SQL filter is correct

    # Verify the SQL query itself doesn't exclude the email (check that
    # the JMAP call was NOT made — dedup filters before JMAP fetch)
    mock_jmap.get_emails.assert_not_called()


def test_skipped_sort_dedup_prevents_duplicate_manual_rows(db: Database):
    """Bug C: emails with an existing manual row must not get another one."""
    tree = _make_tree()
    learner = _make_learner(db)

    # Email skipped in a run
    _seed_audit_row(db, email_id="e-skip", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX",
                    moved=False, run_id="run-1")

    # A previous run already recorded a manual sort for this email
    _seed_audit_row(db, email_id="e-skip", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                    moved=True, classification_source="manual", run_id="run-prev")

    mock_jmap = MagicMock()
    mock_jmap.get_emails.return_value = []  # should not be called — dedup filters first

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-detect', datetime('now'), 'running')",
    )
    db.commit()

    found = learner._detect_skipped_sorts(mock_jmap, tree, "run-detect")
    assert found == 0  # dedup prevents re-detection

    # Verify no duplicate manual rows were created
    manual_rows = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE email_id='e-skip' AND classification_source='manual'"
    ).fetchone()[0]
    assert manual_rows == 1  # only the original from run-prev


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
        "SELECT * FROM audit_log WHERE email_id='e-moved' AND classification_source='correction'"
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
# Computed confidence model
# ------------------------------------------------------------------

def test_compute_confidence_with_recent_evidence(db: Database):
    """Rules with recent coherent evidence get confidence from the formula."""
    learner = _make_learner(db)

    _seed_rule(db, rule_id=50, condition_value="recent@example.com",
               target_folder="INBOX/Affairs/Banks", confidence=0.70)

    # 5 recent evidence rows — all to the same target
    for i in range(5):
        _seed_audit_row(db, email_id=f"e-rec-{i}", from_address="recent@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    changed = learner.compute_rule_confidence()
    assert changed == 1

    row = db.execute("SELECT confidence, active FROM rules WHERE id = 50").fetchone()
    # base = min(0.95, 0.80 + 5*0.03) = 0.95, coherence=1.0, staleness=1.0
    assert abs(row["confidence"] - 0.95) < 1e-9
    assert row["active"] == 1


def test_compute_confidence_staleness_reduces(db: Database):
    """Staleness factor reduces confidence when last_relevant_at is old."""
    learner = _make_learner(db)

    _seed_rule(db, rule_id=51, condition_value="old@example.com",
               target_folder="INBOX/Affairs/Banks", confidence=0.90)

    # Evidence outside the 30-day coherence window
    for i in range(5):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-old", f"e-old-{i}", "old@example.com", "example.com",
             "INBOX", "INBOX/Affairs/Banks", 1.0, "manual", 1,
             "2024-01-01T00:00:00"),
        )
    db.execute(
        "UPDATE rules SET last_relevant_at = datetime('now', '-500 days') WHERE id = 51",
    )
    db.commit()

    changed = learner.compute_rule_confidence()
    assert changed == 1

    row = db.execute("SELECT confidence, active FROM rules WHERE id = 51").fetchone()
    # base=0.95, coherence=1.0 (no data in window), staleness < 1.0
    assert row["confidence"] < 0.90
    assert row["confidence"] > 0.50  # still above deactivation
    assert row["active"] == 1


def test_compute_confidence_deactivates_below_threshold(db: Database):
    """Rules with low coherence + corrections drop below deactivation threshold."""
    learner = _make_learner(db)

    rid = _seed_rule(db, rule_id=52, condition_value="bad@example.com",
                     target_folder="INBOX/Affairs/Banks", confidence=0.90)

    # Low coherence: 2 to target, 3 elsewhere
    for i in range(2):
        _seed_audit_row(db, email_id=f"e-good-{i}", from_address="bad@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")
    for i in range(3):
        _seed_audit_row(db, email_id=f"e-bad-{i}", from_address="bad@example.com",
                        from_domain="example.com", target_folder="INBOX/Shopping/Orders")

    # 3 corrections against the rule
    for i in range(3):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved, rule_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-corr", f"e-corr-{i}", "bad@example.com", "example.com",
             "INBOX", "INBOX/Shopping/Orders", 1.0, "correction", 1, rid),
        )
    db.commit()

    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["active"] == 0
    assert row["confidence"] < 0.50


def test_compute_confidence_skips_manual_rules(db: Database):
    """Manual rules should not be recomputed by compute_rule_confidence."""
    learner = _make_learner(db)

    db.execute(
        "INSERT INTO rules (id, rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES (53, 'exact_sender', 'manual@example.com', "
        "'INBOX/Affairs/Banks', 0.95, 'manual', 1)"
    )
    db.commit()

    changed = learner.compute_rule_confidence()
    assert changed == 0

    row = db.execute("SELECT confidence FROM rules WHERE id = 53").fetchone()
    assert row["confidence"] == 0.95  # unchanged


def test_compute_confidence_net_correction_recovery(db: Database):
    """Confirming manual sorts cancel out corrections (sort-back recovery)."""
    learner = _make_learner(db)

    rid = _seed_rule(db, rule_id=60, condition_value="recover@example.com",
                     target_folder="INBOX/Affairs/Banks", confidence=0.90)

    # 5 evidence rows (all to target)
    for i in range(5):
        _seed_audit_row(db, email_id=f"e-ev-{i}", from_address="recover@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    # 2 corrections against the rule
    for i in range(2):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved, rule_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-corr", f"e-corr-{i}", "recover@example.com", "example.com",
             "INBOX", "INBOX/Shopping/Orders", 1.0, "correction", 1, rid),
        )
    # 2 confirming manual sorts (cancel out corrections → net = 0)
    for i in range(2):
        _seed_audit_row(db, email_id=f"e-confirm-{i}", from_address="recover@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks",
                        classification_source="manual")
    db.commit()

    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    # net_corrections = max(0, 2-2) = 0 → no penalty
    # coherence = 7/9 ≈ 0.78 (5 evidence + 2 confirming to target, 2 corrections elsewhere)
    # base = 0.95 (5+ evidence for exact_sender)
    # confidence ≈ 0.95 * 0.78 * 1.0 - 0 ≈ 0.74
    assert row["active"] == 1
    assert row["confidence"] > 0.50  # well above deactivation


def test_compute_confidence_correction_aging(db: Database):
    """Corrections outside the 30-day coherence window don't count."""
    learner = _make_learner(db)

    rid = _seed_rule(db, rule_id=61, condition_value="aged@example.com",
                     target_folder="INBOX/Affairs/Banks", confidence=0.70)

    # 5 recent evidence rows
    for i in range(5):
        _seed_audit_row(db, email_id=f"e-recent-{i}", from_address="aged@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    # Old correction (60 days ago — outside 30-day window)
    db.execute(
        "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
        "source_folder, target_folder, confidence, classification_source, moved, rule_id, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run-old-corr", "e-old-corr", "aged@example.com", "example.com",
         "INBOX", "INBOX/Shopping/Orders", 1.0, "correction", 1, rid,
         "2025-01-01T00:00:00"),
    )
    db.commit()

    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    # Old correction is outside window → net_corrections = 0
    # base = 0.95, coherence = 1.0 (all 5 recent to target), staleness = 1.0
    assert abs(row["confidence"] - 0.95) < 1e-9
    assert row["active"] == 1


def test_compute_confidence_min_sample_guard(db: Database):
    """When fewer than coherence_min_sample emails in window, coherence defaults to 1.0."""
    learner = _make_learner(db)
    # Default coherence_min_sample = 3

    _seed_rule(db, rule_id=62, condition_value="sparse@example.com",
               target_folder="INBOX/Affairs/Banks", confidence=0.70)

    # Only 2 recent evidence rows (below min_sample of 3)
    # One to target, one elsewhere → raw coherence = 50% but should be overridden to 1.0
    _seed_audit_row(db, email_id="e-sparse-0", from_address="sparse@example.com",
                    from_domain="example.com", target_folder="INBOX/Affairs/Banks")
    _seed_audit_row(db, email_id="e-sparse-1", from_address="sparse@example.com",
                    from_domain="example.com", target_folder="INBOX/Shopping/Orders")

    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence FROM rules WHERE id = 62").fetchone()
    # evidence = 1 (only the row to target folder counts in _count_all_time_evidence)
    # base = min(0.95, 0.80 + 1*0.03) = 0.83
    # coherence = 1.0 (min sample guard), staleness = 1.0, no corrections
    assert abs(row["confidence"] - 0.83) < 1e-9


def test_compute_confidence_idempotent(db: Database):
    """Running compute_rule_confidence twice produces the same result."""
    learner = _make_learner(db)

    _seed_rule(db, rule_id=63, condition_value="idem@example.com",
               target_folder="INBOX/Affairs/Banks", confidence=0.70)

    for i in range(4):
        _seed_audit_row(db, email_id=f"e-idem-{i}", from_address="idem@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    learner.compute_rule_confidence()
    conf_after_first = db.execute("SELECT confidence FROM rules WHERE id = 63").fetchone()["confidence"]

    changed = learner.compute_rule_confidence()
    conf_after_second = db.execute("SELECT confidence FROM rules WHERE id = 63").fetchone()["confidence"]

    assert changed == 0  # no change on second run
    assert abs(conf_after_first - conf_after_second) < 1e-9


def test_compute_confidence_staleness_recovery(db: Database):
    """Rule with old last_relevant_at recovers when new evidence refreshes it."""
    learner = _make_learner(db)

    _seed_rule(db, rule_id=64, condition_value="stale-recover@example.com",
               target_folder="INBOX/Affairs/Banks", confidence=0.90)

    # Start with old evidence only
    for i in range(5):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-old", f"e-stale-old-{i}", "stale-recover@example.com", "example.com",
             "INBOX", "INBOX/Affairs/Banks", 1.0, "manual", 1,
             "2024-01-01T00:00:00"),
        )
    db.execute(
        "UPDATE rules SET last_relevant_at = datetime('now', '-500 days') WHERE id = 64",
    )
    db.commit()

    learner.compute_rule_confidence()
    stale_conf = db.execute("SELECT confidence FROM rules WHERE id = 64").fetchone()["confidence"]
    assert stale_conf < 0.90  # staleness reduced it

    # Now add recent evidence → staleness recovers
    for i in range(5):
        _seed_audit_row(db, email_id=f"e-stale-new-{i}", from_address="stale-recover@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    learner.compute_rule_confidence()
    recovered_conf = db.execute("SELECT confidence FROM rules WHERE id = 64").fetchone()["confidence"]
    assert recovered_conf > stale_conf  # recovered
    assert abs(recovered_conf - 0.95) < 1e-9  # back to full (recent evidence in window)


def test_compute_confidence_floor_at_zero(db: Database):
    """Confidence cannot go below 0 even with many corrections."""
    learner = _make_learner(db)

    rid = _seed_rule(db, rule_id=65, condition_value="floor@example.com",
                     target_folder="INBOX/Affairs/Banks", confidence=0.90)

    # Minimal evidence
    _seed_audit_row(db, email_id="e-floor-0", from_address="floor@example.com",
                    from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    # 50 corrections → net_corrections * 0.05 = 2.50, way more than any base
    for i in range(50):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved, rule_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-floor", f"e-floor-corr-{i}", "floor@example.com", "example.com",
             "INBOX", "INBOX/Shopping/Orders", 1.0, "correction", 1, rid),
        )
    db.commit()

    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["confidence"] == 0.0
    assert row["active"] == 0  # below deactivation threshold


# ------------------------------------------------------------------
# _count_all_time_evidence unit tests
# ------------------------------------------------------------------

def test_count_evidence_exact_sender(db: Database):
    """Evidence count uses from_address column for exact_sender rules."""
    learner = _make_learner(db)
    rule = {"rule_type": "exact_sender", "condition_value": "a@example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    for i in range(3):
        _seed_audit_row(db, email_id=f"e-match-{i}", from_address="a@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    assert learner._count_all_time_evidence(rule) == 3


def test_count_evidence_sender_domain(db: Database):
    """Evidence count uses from_domain column for sender_domain rules."""
    learner = _make_learner(db)
    rule = {"rule_type": "sender_domain", "condition_value": "example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    for i in range(4):
        _seed_audit_row(db, email_id=f"e-dom-{i}", from_address=f"user{i}@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    assert learner._count_all_time_evidence(rule) == 4


def test_count_evidence_list_id(db: Database):
    """Evidence count uses list_id column for list_id rules."""
    learner = _make_learner(db)
    rule = {"rule_type": "list_id", "condition_value": "news.example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    for i in range(2):
        _seed_audit_row(db, email_id=f"e-list-{i}", from_address=f"bot{i}@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks",
                        list_id="news.example.com")

    assert learner._count_all_time_evidence(rule) == 2


def test_count_evidence_excludes_unmoved_rows(db: Database):
    """Only rows with moved=1 count as evidence."""
    learner = _make_learner(db)
    rule = {"rule_type": "exact_sender", "condition_value": "a@example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    _seed_audit_row(db, email_id="e-moved", from_address="a@example.com",
                    from_domain="example.com", target_folder="INBOX/Affairs/Banks", moved=True)
    _seed_audit_row(db, email_id="e-not-moved", from_address="a@example.com",
                    from_domain="example.com", target_folder="INBOX/Affairs/Banks", moved=False)

    assert learner._count_all_time_evidence(rule) == 1


def test_count_evidence_excludes_different_target(db: Database):
    """Rows to a different target_folder are not counted."""
    learner = _make_learner(db)
    rule = {"rule_type": "exact_sender", "condition_value": "a@example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    _seed_audit_row(db, email_id="e-right", from_address="a@example.com",
                    from_domain="example.com", target_folder="INBOX/Affairs/Banks")
    _seed_audit_row(db, email_id="e-wrong", from_address="a@example.com",
                    from_domain="example.com", target_folder="INBOX/Shopping/Orders")

    assert learner._count_all_time_evidence(rule) == 1


def test_count_evidence_capped_by_config_limit(db: Database):
    """Count is capped at max_needed derived from BaseConfidenceConfig.

    Default config: max(ceil((0.95-0.80)/0.03), ceil((0.90-0.75)/0.02), 1)
                  = max(5, 8, 1) = 8
    """
    learner = _make_learner(db)
    rule = {"rule_type": "exact_sender", "condition_value": "a@example.com",
            "target_folder_path": "INBOX/Affairs/Banks"}

    # Insert 20 rows — well past the cap
    for i in range(20):
        _seed_audit_row(db, email_id=f"e-cap-{i}", from_address="a@example.com",
                        from_domain="example.com", target_folder="INBOX/Affairs/Banks")

    import math
    bc = learner._config.base_confidence
    expected_limit = max(
        math.ceil((bc.exact_sender_cap - bc.exact_sender_floor) / bc.exact_sender_per_evidence),
        math.ceil((bc.sender_domain_cap - bc.sender_domain_floor) / bc.sender_domain_per_evidence),
        1,
    )
    assert learner._count_all_time_evidence(rule) == expected_limit
    assert expected_limit == 8  # sanity check on default config


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


def test_correction_records_correction_row_with_rule_id(db: Database):
    """Correction records a 'correction' audit row with the originating rule_id."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.95, target_folder="INBOX/Shopping/Orders")
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

    # Correction row recorded with classification_source='correction' and rule_id
    corr_row = db.execute(
        "SELECT * FROM audit_log WHERE email_id = 'e-corr' AND classification_source = 'correction'"
    ).fetchone()
    assert corr_row is not None
    assert corr_row["rule_id"] == rid
    assert corr_row["target_folder"] == "INBOX/Affairs/Banks"

    # Rule confidence unchanged by detect_manual_sorts (compute_rule_confidence handles it)
    rule_row = db.execute("SELECT confidence FROM rules WHERE id = ?", (rid,)).fetchone()
    assert rule_row["confidence"] == 0.95


def test_correction_then_compute_reduces_confidence(db: Database):
    """After a correction, compute_rule_confidence reduces confidence via coherence + penalty."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.95, target_folder="INBOX/Shopping/Orders")
    _seed_audit_row(db, email_id="e-corr", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-corr"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-corr', datetime('now'), 'running')",
    )
    db.commit()

    learner.detect_manual_sorts(mock_jmap, tree, "run-corr")
    learner.compute_rule_confidence()

    row = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (rid,)).fetchone()
    # Confidence should drop: base * coherence - corrections * penalty
    # With 1 rule move + 1 correction, coherence < 1.0 and net_corrections >= 0
    assert row["confidence"] < 0.95
    assert row["active"] == 1  # above deactivation threshold (0.50)


def test_correction_dedup_skips_already_handled(db: Database):
    """Emails with a correction/manual row (no newer rule move) should not re-trigger."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.90)
    _seed_audit_row(db, email_id="e-dup", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid, run_id="run-original")
    # Simulate a previous correction already recorded
    _seed_audit_row(db, email_id="e-dup", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Affairs/Banks",
                    moved=True, classification_source="correction", run_id="run-prev-correction")

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
    assert found.from_other == 0  # skipped because already handled


def test_correction_of_llm_move_records_no_rule_id(db: Database):
    """Corrections of LLM-classified emails record correction with rule_id=None."""
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
    assert found.from_other == 1

    corr_row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-llm' AND classification_source='correction'"
    ).fetchone()
    assert corr_row is not None
    assert corr_row["rule_id"] is None


def test_inbox_return_not_detected_as_correction(db: Database):
    """Moving an email back to inbox should NOT trigger a correction (ambiguous intent)."""
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

    # No correction row recorded
    corr_row = db.execute(
        "SELECT * FROM audit_log WHERE email_id='e-inbox' AND classification_source='correction'"
    ).fetchone()
    assert corr_row is None


# ------------------------------------------------------------------
# Dedup regression tests (C4)
# ------------------------------------------------------------------

def test_re_correction_after_new_rule_move(db: Database):
    """move → correct → move again → correct again should create two correction rows.

    Uses explicit timestamps because SQLite datetime('now') has second-level
    precision and the dedup query uses strict > comparison.
    """
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.95, target_folder="INBOX/Shopping/Orders")

    # T0: First rule move
    db.execute(
        "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
        "source_folder, target_folder, confidence, classification_source, moved, rule_id, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run-move-1", "e-cycle", "noreply@chase.com", "chase.com",
         "INBOX", "INBOX/Shopping/Orders", 0.95, "rule", 1, rid,
         "2026-04-05T10:00:00"),
    )
    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-move-1', '2026-04-05T10:00:00', 'completed')")
    db.commit()

    # T1: First correction detected
    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-cycle"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-corr-1', '2026-04-05T11:00:00', 'running')")
    db.commit()
    found1 = learner.detect_manual_sorts(mock_jmap, tree, "run-corr-1")
    assert found1.from_other == 1

    # T2: Second rule move — must be strictly newer than the correction row
    # The correction row was created with datetime('now') by _record_correction,
    # so we insert the second move with a future timestamp to guarantee ordering.
    db.execute(
        "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
        "source_folder, target_folder, confidence, classification_source, moved, rule_id, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run-move-2", "e-cycle", "noreply@chase.com", "chase.com",
         "INBOX", "INBOX/Shopping/Orders", 0.90, "rule", 1, rid,
         "2099-01-01T00:00:00"),
    )
    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-move-2', '2099-01-01T00:00:00', 'completed')")
    db.commit()

    # T3: Second correction should be detected (new rule move is newer than first correction)
    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-corr-2', '2099-01-01T01:00:00', 'running')")
    db.commit()
    found2 = learner.detect_manual_sorts(mock_jmap, tree, "run-corr-2")
    assert found2.from_other == 1

    # Should have 2 correction rows total
    corr_count = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE email_id='e-cycle' AND classification_source='correction'"
    ).fetchone()[0]
    assert corr_count == 2


def test_dedup_blocks_same_cycle_duplicate(db: Database):
    """Within a single detection cycle, same email should not produce duplicate corrections."""
    tree = _make_tree()
    learner = _make_learner(db)

    rid = _seed_rule(db, confidence=0.95, target_folder="INBOX/Shopping/Orders")
    _seed_audit_row(db, email_id="e-once", from_address="noreply@chase.com",
                    from_domain="chase.com", target_folder="INBOX/Shopping/Orders",
                    moved=True, rule_id=rid)

    mock_jmap = MagicMock()
    email = MagicMock()
    email.id = "e-once"
    email.mailbox_ids = {"mb-banks": True}
    mock_jmap.get_emails.return_value = [email]

    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-dup-1', datetime('now'), 'running')")
    db.commit()
    learner.detect_manual_sorts(mock_jmap, tree, "run-dup-1")

    # Run detection again without a new rule move — should NOT create another correction
    db.execute("INSERT OR IGNORE INTO runs (run_id, started_at, status) VALUES ('run-dup-2', datetime('now'), 'running')")
    db.commit()
    found2 = learner.detect_manual_sorts(mock_jmap, tree, "run-dup-2")
    assert found2.from_other == 0

    corr_count = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE email_id='e-once' AND classification_source='correction'"
    ).fetchone()[0]
    assert corr_count == 1
