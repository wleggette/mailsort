"""End-to-end integration tests with mocked JMAP.

Exercises the full lifecycle:
  Pass 1: classify + move emails via rules
  Pass 2: learning detects user corrections, penalizes rules, re-classifies
  Pass 3: verify deactivated rules no longer match

All JMAP calls are mocked — no real emails touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import JMAPEmail, JMAPMailbox
from mailsort.orchestrator import run_classification_pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15, min_age_minutes=240, max_batch_size=100),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token",
        anthropic_api_key="",
        db_path=":memory:",
    )


def _tree() -> MailboxTree:
    return MailboxTree.build([
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-orders", name="Orders", parentId="mb-shopping"),
        JMAPMailbox(id="mb-shopping", name="Shopping", parentId="mb-inbox"),
        JMAPMailbox(id="mb-travel", name="Travel", parentId="mb-inbox"),
    ])


def _email(
    email_id: str,
    from_email: str,
    subject: str = "Test email",
    mailbox_ids: dict | None = None,
    keywords: dict | None = None,
    received_at: str = "2026-03-10T10:00:00Z",
) -> JMAPEmail:
    return JMAPEmail.model_validate({
        "id": email_id,
        "threadId": f"thread-{email_id}",
        "mailboxIds": mailbox_ids or {"mb-inbox": True},
        "from": [{"name": "Sender", "email": from_email}],
        "to": [{"email": "user@fastmail.com"}],
        "subject": subject,
        "receivedAt": received_at,
        "keywords": keywords if keywords is not None else {"$seen": True},
        "preview": "Preview text.",
    })


def _seed_rules(db: Database) -> dict[str, int]:
    """Seed rules and return {condition_value: rule_id}."""
    rules = {}
    for val, folder, conf in [
        ("noreply@chase.com", "INBOX/Affairs/Banks", 0.95),
        ("orders@amazon.com", "INBOX/Shopping/Orders", 0.90),
    ]:
        cursor = db.execute(
            "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
            "confidence, source, active) VALUES ('exact_sender', ?, ?, ?, 'bootstrap', 1)",
            (val, folder, conf),
        )
        rules[val] = cursor.lastrowid
    db.commit()
    return rules


# ---------------------------------------------------------------------------
# Pass 1: Initial classification + move
# ---------------------------------------------------------------------------

def test_pass1_classify_and_move(db: Database, monkeypatch):
    """First run: classify 3 emails, move 2 via rules, skip 1 (no LLM)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    rules = _seed_rules(db)

    e_chase = _email("e-chase", "noreply@chase.com", "Your statement")
    e_amazon = _email("e-amazon", "orders@amazon.com", "Your order shipped")
    e_unknown = _email("e-unknown", "random@nobody.com", "Hello there")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [
        {"e-chase", "e-amazon", "e-unknown"},           # unfiltered (all inbox)
        ["e-chase", "e-amazon", "e-unknown"],            # filtered (eligible)
    ]
    mock_jmap.get_emails.return_value = [e_chase, e_amazon, e_unknown]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {
        "e-chase": True,
        "e-amazon": True,
    }

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # --- Verify move_emails was called with the right emails ---
    mock_jmap.move_emails.assert_called_once()
    moves = mock_jmap.move_emails.call_args[0][0]
    moved_ids = {m[0] for m in moves}
    assert moved_ids == {"e-chase", "e-amazon"}

    # --- Verify audit_log ---
    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ? ORDER BY email_id", (run_id,)
    ).fetchall()
    assert len(rows) == 3

    by_email = {r["email_id"]: dict(r) for r in rows}

    # Chase: rule match, moved
    assert by_email["e-chase"]["classification_source"] == "rule"
    assert by_email["e-chase"]["target_folder"] == "INBOX/Affairs/Banks"
    assert by_email["e-chase"]["moved"] == 1
    assert by_email["e-chase"]["rule_id"] == rules["noreply@chase.com"]
    assert by_email["e-chase"]["email_received_at"] is not None

    # Amazon: rule match, moved
    assert by_email["e-amazon"]["classification_source"] == "rule"
    assert by_email["e-amazon"]["target_folder"] == "INBOX/Shopping/Orders"
    assert by_email["e-amazon"]["moved"] == 1
    assert by_email["e-amazon"]["rule_id"] == rules["orders@amazon.com"]

    # Unknown: no match, skipped
    assert by_email["e-unknown"]["moved"] == 0
    assert by_email["e-unknown"]["skip_reason"] is not None

    # --- Verify run summary ---
    run_row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["emails_seen"] == 3
    assert run_row["emails_moved"] == 2


# ---------------------------------------------------------------------------
# Pass 2: Learning detects user correction → penalizes rule
# ---------------------------------------------------------------------------

def test_pass2_learning_detects_correction_and_penalizes(db: Database, monkeypatch):
    """Second run after user corrects a move.

    Setup:
      - Pass 1 moved e-chase to Banks via rule (conf 0.95)
      - User relocated e-chase from Banks to Travel (correction)
      - Pass 2 should detect the correction and penalize the rule
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    rules = _seed_rules(db)

    # --- Simulate pass 1: move e-chase to Banks ---
    e_chase = _email("e-chase", "noreply@chase.com", "Your statement")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [
        {"e-chase"},
        ["e-chase"],
    ]
    mock_jmap.get_emails.return_value = [e_chase]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {"e-chase": True}

    run1_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # Confirm rule confidence is still original
    rule_row = db.execute(
        "SELECT confidence, active FROM rules WHERE id = ?",
        (rules["noreply@chase.com"],),
    ).fetchone()
    assert rule_row["confidence"] == 0.95
    assert rule_row["active"] == 1

    # --- Simulate pass 2: user has moved e-chase from Banks to Travel ---
    # Now the learner will query audit_log for moved emails and check their
    # current location via JMAP. We mock get_emails to show e-chase is in Travel.
    mock_jmap2 = MagicMock()
    # Unfiltered inbox: empty (e-chase is no longer in inbox)
    mock_jmap2.query_inbox_emails.side_effect = [
        set(),   # unfiltered
        [],      # filtered (eligible) — nothing to classify
    ]
    # The learner's _detect_correction_sorts calls get_emails for recently moved emails
    # It will find e-chase and see it's now in Travel (mb-travel) instead of Banks (mb-banks)
    corrected_chase = MagicMock()
    corrected_chase.id = "e-chase"
    corrected_chase.mailbox_ids = {"mb-travel": True}  # user moved to Travel
    mock_jmap2.get_emails.return_value = [corrected_chase]
    mock_jmap2.get_contacts.return_value = []
    mock_jmap2.query_folder_emails.return_value = []
    mock_jmap2.session_capabilities = set()
    mock_jmap2.is_read_only = False

    run2_id = run_classification_pass(cfg, db, mock_jmap2, tree, dry_run=False, trigger="test")

    # --- Verify rule was penalized ---
    rule_row = db.execute(
        "SELECT confidence, active FROM rules WHERE id = ?",
        (rules["noreply@chase.com"],),
    ).fetchone()
    assert abs(rule_row["confidence"] - 0.80) < 1e-9  # 0.95 - 0.15
    assert rule_row["active"] == 0  # 0.80 < 0.85 threshold → deactivated

    # --- Verify a manual sort was recorded ---
    manual_row = db.execute(
        "SELECT * FROM audit_log WHERE email_id = 'e-chase' AND classification_source = 'manual'"
    ).fetchone()
    assert manual_row is not None
    assert manual_row["target_folder"] == "INBOX/Travel"

    # --- Verify dedup: running again doesn't double-penalize ---
    original_conf = rule_row["confidence"]
    mock_jmap3 = MagicMock()
    mock_jmap3.query_inbox_emails.side_effect = [set(), []]
    # e-chase still in Travel, but already corrected
    mock_jmap3.get_emails.return_value = [corrected_chase]
    mock_jmap3.get_contacts.return_value = []
    mock_jmap3.query_folder_emails.return_value = []
    mock_jmap3.session_capabilities = set()
    mock_jmap3.is_read_only = False

    run3_id = run_classification_pass(cfg, db, mock_jmap3, tree, dry_run=False, trigger="test")

    rule_row = db.execute(
        "SELECT confidence FROM rules WHERE id = ?",
        (rules["noreply@chase.com"],),
    ).fetchone()
    assert rule_row["confidence"] == original_conf  # unchanged — dedup works


# ---------------------------------------------------------------------------
# Full lifecycle: move → correct → deactivate → re-classify
# ---------------------------------------------------------------------------

def test_full_lifecycle_move_correct_deactivate(db: Database, monkeypatch):
    """Multi-pass lifecycle test.

    Pass 1: Rule matches chase → moves to Banks
    Pass 2: User corrected to Travel → rule penalized & deactivated
    Pass 3: New chase email arrives → rule no longer matches (deactivated),
            falls through to no classification
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    rules = _seed_rules(db)
    chase_rule_id = rules["noreply@chase.com"]

    # --- Pass 1: move chase email ---
    e1 = _email("e-chase-1", "noreply@chase.com", "Jan statement")

    jmap1 = MagicMock()
    jmap1.query_inbox_emails.side_effect = [{"e-chase-1"}, ["e-chase-1"]]
    jmap1.get_emails.return_value = [e1]
    jmap1.get_thread_email_ids.return_value = []
    jmap1.get_contacts.return_value = []
    jmap1.query_folder_emails.return_value = []
    jmap1.session_capabilities = set()
    jmap1.is_read_only = False
    jmap1.move_emails.return_value = {"e-chase-1": True}

    run_classification_pass(cfg, db, jmap1, tree, dry_run=False, trigger="test")

    row = db.execute("SELECT moved, rule_id FROM audit_log WHERE email_id = 'e-chase-1'").fetchone()
    assert row["moved"] == 1
    assert row["rule_id"] == chase_rule_id

    # --- Pass 2: user corrected e-chase-1 → Travel ---
    jmap2 = MagicMock()
    jmap2.query_inbox_emails.side_effect = [set(), []]
    corrected = MagicMock()
    corrected.id = "e-chase-1"
    corrected.mailbox_ids = {"mb-travel": True}
    jmap2.get_emails.return_value = [corrected]
    jmap2.get_contacts.return_value = []
    jmap2.query_folder_emails.return_value = []
    jmap2.session_capabilities = set()
    jmap2.is_read_only = False

    run_classification_pass(cfg, db, jmap2, tree, dry_run=False, trigger="test")

    rule = db.execute("SELECT confidence, active FROM rules WHERE id = ?", (chase_rule_id,)).fetchone()
    assert rule["active"] == 0  # deactivated after correction

    # --- Pass 3: new chase email arrives → rule is deactivated → no match ---
    e2 = _email("e-chase-2", "noreply@chase.com", "Feb statement")

    jmap3 = MagicMock()
    jmap3.query_inbox_emails.side_effect = [{"e-chase-2"}, ["e-chase-2"]]
    # Learner will try to check corrected emails again — return already-corrected ones
    already_corrected = MagicMock()
    already_corrected.id = "e-chase-1"
    already_corrected.mailbox_ids = {"mb-travel": True}
    jmap3.get_emails.side_effect = [
        [already_corrected],  # learner: check corrections
        [e2],                  # orchestrator: fetch features
    ]
    jmap3.get_thread_email_ids.return_value = []
    jmap3.get_contacts.return_value = []
    jmap3.query_folder_emails.return_value = []
    jmap3.session_capabilities = set()
    jmap3.is_read_only = False

    run_classification_pass(cfg, db, jmap3, tree, dry_run=False, trigger="test")

    # e-chase-2 should NOT have been moved — rule is deactivated
    row2 = db.execute(
        "SELECT moved, skip_reason FROM audit_log WHERE email_id = 'e-chase-2'"
    ).fetchone()
    assert row2 is not None
    assert row2["moved"] == 0
    assert row2["skip_reason"] is not None  # no_classification or llm_unavailable


# ---------------------------------------------------------------------------
# Move failure: JMAP returns False for some emails
# ---------------------------------------------------------------------------

def test_partial_move_failure(db: Database, monkeypatch):
    """When some moves succeed and others fail, audit log reflects both correctly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    _seed_rules(db)

    e_chase = _email("e-chase", "noreply@chase.com", "Statement")
    e_amazon = _email("e-amazon", "orders@amazon.com", "Shipped")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [
        {"e-chase", "e-amazon"},
        ["e-chase", "e-amazon"],
    ]
    mock_jmap.get_emails.return_value = [e_chase, e_amazon]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {
        "e-chase": True,
        "e-amazon": False,  # JMAP failed to move this one
    }

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    rows = {
        r["email_id"]: dict(r)
        for r in db.execute("SELECT * FROM audit_log WHERE run_id = ?", (run_id,)).fetchall()
    }
    assert rows["e-chase"]["moved"] == 1
    assert rows["e-amazon"]["moved"] == 0  # failed move recorded as not moved

    run_row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run_row["emails_moved"] == 1  # only 1 successful


# ---------------------------------------------------------------------------
# Eligibility gates: unread, flagged, too_new
# ---------------------------------------------------------------------------

def test_unread_email_classified_but_not_moved(db: Database, monkeypatch):
    """Unread emails should be classified (get audit row) but not moved."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    _seed_rules(db)

    # Unread email: no $seen keyword
    e_unread = _email("e-unread", "noreply@chase.com", "Statement", keywords={})

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-unread"}, ["e-unread"]]
    mock_jmap.get_emails.return_value = [e_unread]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    mock_jmap.move_emails.assert_not_called()

    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-unread'").fetchone()
    assert row is not None
    assert row["classification_source"] == "rule"  # classified successfully
    assert row["target_folder"] == "INBOX/Affairs/Banks"  # knows where it would go
    assert row["moved"] == 0
    assert row["skip_reason"] == "unread"


def test_flagged_email_classified_but_not_moved(db: Database, monkeypatch):
    """Flagged emails should be classified but not moved."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    _seed_rules(db)

    # Read + flagged
    e_flagged = _email("e-flagged", "noreply@chase.com", "Statement",
                       keywords={"$seen": True, "$flagged": True})

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-flagged"}, ["e-flagged"]]
    mock_jmap.get_emails.return_value = [e_flagged]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    mock_jmap.move_emails.assert_not_called()

    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-flagged'").fetchone()
    assert row is not None
    assert row["classification_source"] == "rule"
    assert row["moved"] == 0
    assert row["skip_reason"] == "flagged"


def test_too_new_email_classified_but_not_moved(db: Database, monkeypatch):
    """Emails received less than min_age_minutes ago should be classified but not moved."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()  # min_age_minutes=240
    tree = _tree()
    _seed_rules(db)

    # Email received 1 hour ago — too new
    from datetime import datetime, timedelta, timezone
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    e_new = _email("e-new", "noreply@chase.com", "Statement", received_at=one_hour_ago)

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-new"}, ["e-new"]]
    mock_jmap.get_emails.return_value = [e_new]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    mock_jmap.move_emails.assert_not_called()

    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-new'").fetchone()
    assert row is not None
    assert row["classification_source"] == "rule"
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 0
    assert row["skip_reason"] == "too_new"


def test_old_read_email_is_moved(db: Database, monkeypatch):
    """A read, unflagged, old-enough email should be moved normally."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    _seed_rules(db)

    # Email received 5 hours ago, read, not flagged — fully eligible
    from datetime import datetime, timedelta, timezone
    five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    e_ok = _email("e-ok", "noreply@chase.com", "Statement", received_at=five_hours_ago)

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-ok"}, ["e-ok"]]
    mock_jmap.get_emails.return_value = [e_ok]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {"e-ok": True}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    mock_jmap.move_emails.assert_called_once()

    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-ok'").fetchone()
    assert row["moved"] == 1
    assert row["skip_reason"] is None


def test_mixed_eligibility_in_single_run(db: Database, monkeypatch):
    """A run with unread, flagged, too_new, and eligible emails — all classified, only eligible moved."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    tree = _tree()
    _seed_rules(db)

    from datetime import datetime, timedelta, timezone
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    e_unread = _email("e-unread", "noreply@chase.com", "Unread stmt", keywords={})
    e_flagged = _email("e-flagged", "noreply@chase.com", "Flagged stmt",
                       keywords={"$seen": True, "$flagged": True})
    e_new = _email("e-new", "noreply@chase.com", "New stmt", received_at=one_hour_ago)
    e_ok = _email("e-ok", "noreply@chase.com", "Old stmt", received_at=five_hours_ago)

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [
        {"e-unread", "e-flagged", "e-new", "e-ok"},
        ["e-unread", "e-flagged", "e-new", "e-ok"],
    ]
    mock_jmap.get_emails.return_value = [e_unread, e_flagged, e_new, e_ok]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False
    mock_jmap.move_emails.return_value = {"e-ok": True}

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    rows = {
        r["email_id"]: dict(r)
        for r in db.execute("SELECT * FROM audit_log WHERE run_id = ?", (run_id,)).fetchall()
    }

    assert len(rows) == 4  # all 4 classified

    # All classified as rule match
    for eid in rows:
        assert rows[eid]["classification_source"] == "rule"

    # Only e-ok moved
    assert rows["e-ok"]["moved"] == 1
    assert rows["e-ok"]["skip_reason"] is None
    assert rows["e-unread"]["moved"] == 0
    assert rows["e-unread"]["skip_reason"] == "unread"
    assert rows["e-flagged"]["moved"] == 0
    assert rows["e-flagged"]["skip_reason"] == "flagged"
    assert rows["e-new"]["moved"] == 0
    assert rows["e-new"]["skip_reason"] == "too_new"

    # Run summary
    run_row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run_row["emails_moved"] == 1


# ---------------------------------------------------------------------------
# Folder reconciliation: deleted folders
# ---------------------------------------------------------------------------

def test_deleted_folder_rule_deactivated_on_run(db: Database, monkeypatch):
    """Rules pointing to a deleted folder should be deactivated at the start of a run."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()
    rules = _seed_rules(db)

    # Tree WITHOUT Shopping/Orders — simulates a deleted folder
    tree_without_orders = MailboxTree.build([
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-travel", name="Travel", parentId="mb-inbox"),
    ])

    # Amazon rule targets INBOX/Shopping/Orders which no longer exists
    rule_before = db.execute("SELECT active FROM rules WHERE id = ?", (rules["orders@amazon.com"],)).fetchone()
    assert rule_before["active"] == 1

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [set(), []]
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_classification_pass(cfg, db, mock_jmap, tree_without_orders, dry_run=False, trigger="test")

    # Amazon rule should be deactivated; Chase rule (Banks still exists) should be active
    amazon_rule = db.execute("SELECT active FROM rules WHERE id = ?", (rules["orders@amazon.com"],)).fetchone()
    assert amazon_rule["active"] == 0

    chase_rule = db.execute("SELECT active FROM rules WHERE id = ?", (rules["noreply@chase.com"],)).fetchone()
    assert chase_rule["active"] == 1


def test_deleted_folder_email_gets_unknown_folder_skip(db: Database, monkeypatch):
    """If a rule is active but folder is deleted mid-run (edge case), email gets unknown_folder skip."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()

    # Create a rule for a folder that exists in the tree
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES ('exact_sender', 'noreply@ghost.com', "
        "'INBOX/Ghost/Folder', 0.95, 'bootstrap', 1)"
    )
    db.commit()

    tree = _tree()  # Ghost/Folder not in tree

    e = _email("e-ghost", "noreply@ghost.com", "Hello")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-ghost"}, ["e-ghost"]]
    mock_jmap.get_emails.return_value = [e]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, dry_run=False, trigger="test")

    # The rule should have been deactivated by reconcile_folders
    rule = db.execute(
        "SELECT active FROM rules WHERE condition_value = 'noreply@ghost.com'"
    ).fetchone()
    assert rule["active"] == 0

    # The email shouldn't match the deactivated rule, so no classification
    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-ghost'").fetchone()
    assert row is not None
    assert row["moved"] == 0
    assert row["skip_reason"] in ("no_classification", "llm_unavailable")


# ---------------------------------------------------------------------------
# Bootstrap: deleted folder filtering in coverage and rule creation
# ---------------------------------------------------------------------------

def test_bootstrap_coverage_excludes_deleted_folders(db: Database):
    """Coverage calculation should not count evidence for folders that no longer exist."""
    from mailsort.bootstrap import _calculate_coverage, BootstrapReport
    from mailsort.classifier.rules import RuleEngine
    from mailsort.config import ThresholdsConfig

    tree = _tree()
    rule_engine = RuleEngine(db, ThresholdsConfig())

    # Seed a rule for Banks (exists)
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES ('exact_sender', 'a@example.com', "
        "'INBOX/Affairs/Banks', 0.95, 'auto', 1)"
    )
    db.commit()

    # Seed evidence: 2 emails to Banks (exists), 1 email to DeletedFolder (gone)
    for i, (addr, folder) in enumerate([
        ("a@example.com", "INBOX/Affairs/Banks"),
        ("a@example.com", "INBOX/Affairs/Banks"),
        ("b@example.com", "INBOX/Deleted/Folder"),
    ]):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved) "
            "VALUES ('run-cov', ?, ?, 'example.com', 'INBOX', ?, 1.0, 'manual', 1)",
            (f"e-cov-{i}", addr, folder),
        )
    db.commit()

    report = BootstrapReport()
    _calculate_coverage(db, rule_engine, report, tree.all_folder_paths())

    # Only 2 evidence emails for existing folders; both matched by the rule
    assert report.emails_matched_by_rules == 2
    assert report.emails_unmatched == 0  # DeletedFolder evidence excluded


def test_bootstrap_create_rules_excludes_deleted_folders(db: Database):
    """Rule creation should not consider evidence for folders that no longer exist."""
    from mailsort.bootstrap import _create_rules_from_evidence, BootstrapReport
    from mailsort.audit.learner import Learner
    from mailsort.classifier.rules import RuleEngine
    from mailsort.config import ClassificationConfig, ThresholdsConfig

    tree = _tree()
    rule_engine = RuleEngine(db, ThresholdsConfig())
    learner = Learner(db, rule_engine, ClassificationConfig())

    # Seed 5 manual sorts to a deleted folder (would exceed auto-rule threshold)
    for i in range(5):
        db.execute(
            "INSERT INTO audit_log (run_id, email_id, from_address, from_domain, "
            "source_folder, target_folder, confidence, classification_source, moved) "
            "VALUES ('run-del', ?, 'deleted@example.com', 'example.com', 'INBOX', "
            "'INBOX/Deleted/Folder', 1.0, 'manual', 1)",
            (f"e-del-{i}",),
        )
    db.commit()

    report = BootstrapReport()
    _create_rules_from_evidence(db, learner, report, tree.all_folder_paths())

    # No rules should be created — the folder doesn't exist
    assert report.rules_created == 0
    rules = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    assert rules == 0


# ---------------------------------------------------------------------------
# X9: Full folder deletion cascade in a single run
# ---------------------------------------------------------------------------

def test_deleted_folder_cascade_deactivate_and_fallthrough(db: Database, monkeypatch):
    """Full cascade: active rule → folder disappears → reconcile deactivates → email falls through.

    Tests X9 from the system test plan: a rule is active for a folder that
    existed at bootstrap time. The folder is then deleted (removed from tree).
    In a single run:
      1. reconcile_folders deactivates the rule
      2. An email that would have matched the rule arrives
      3. The email falls through to LLM (or no_classification if LLM unavailable)
      4. The email is NOT moved (no valid target)
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg()

    # Step 1: Create a rule for Orders folder (which exists in _tree())
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, active) VALUES ('exact_sender', 'orders@amazon.com', "
        "'INBOX/Shopping/Orders', 0.95, 'auto', 1)"
    )
    db.commit()

    # Verify rule is active before the run
    rule_before = db.execute("SELECT active, confidence FROM rules WHERE condition_value = 'orders@amazon.com'").fetchone()
    assert rule_before["active"] == 1
    assert rule_before["confidence"] == 0.95

    # Step 2: Build a tree WITHOUT Shopping/Orders — simulates folder deletion
    tree_without_orders = MailboxTree.build([
        JMAPMailbox(id="mb-inbox", name="INBOX", role="inbox"),
        JMAPMailbox(id="mb-banks", name="Banks", parentId="mb-affairs"),
        JMAPMailbox(id="mb-affairs", name="Affairs", parentId="mb-inbox"),
        JMAPMailbox(id="mb-travel", name="Travel", parentId="mb-inbox"),
    ])

    # Step 3: An email from orders@amazon.com arrives in this run
    amazon_email = _email("e-amazon-cascade", "orders@amazon.com", "Your order shipped")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = [{"e-amazon-cascade"}, ["e-amazon-cascade"]]
    mock_jmap.get_emails.return_value = [amazon_email]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.get_contacts.return_value = []
    mock_jmap.query_folder_emails.return_value = []
    mock_jmap.session_capabilities = set()
    mock_jmap.is_read_only = False

    run_classification_pass(cfg, db, mock_jmap, tree_without_orders, dry_run=False, trigger="test")

    # Verify Step 1 outcome: rule was deactivated by reconcile_folders
    rule_after = db.execute("SELECT active FROM rules WHERE condition_value = 'orders@amazon.com'").fetchone()
    assert rule_after["active"] == 0, "Rule should be deactivated after folder deletion"

    # Verify Step 3 outcome: email was classified but NOT by the rule
    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'e-amazon-cascade'").fetchone()
    assert row is not None, "Email should have an audit_log row"
    assert row["classification_source"] != "rule", (
        f"Email should NOT be classified by deactivated rule (got source={row['classification_source']})"
    )
    assert row["moved"] == 0, "Email should not be moved (no valid target folder)"
    assert row["skip_reason"] in ("no_classification", "llm_unavailable"), (
        f"Email should be skipped with no_classification or llm_unavailable (got {row['skip_reason']})"
    )
