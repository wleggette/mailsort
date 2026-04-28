"""Tests for the /analyze route and query helpers.

Layer 1: Route-level tests (FastAPI TestClient, assert template context).
Layer 2: Query-level tests (call extracted functions directly on seeded DB).
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mailsort.config import Config
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations as _run_migrations
from mailsort.web.app import create_app
from mailsort.web.routes.analyze import (
    build_folder_gap_cards,
    build_llm_accuracy,
    get_eligibility_gated,
    get_known_contact_cards,
    get_learning_effectiveness,
    get_llm_corrections,
    get_skipped_then_sorted,
    _metric_color,
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_run(db: Database, run_id: str, *, trigger: str = "scheduler",
              dry_run: bool = False):
    db.execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at, status, trigger, dry_run) "
        "VALUES (?, datetime('now'), 'completed', ?, ?)",
        (run_id, trigger, dry_run),
    )


def _seed_audit(db: Database, *, email_id: str, run_id: str = "run-1",
                from_address: str = "test@example.com",
                from_domain: str = "example.com",
                target_folder: str = "INBOX",
                confidence: float = 0.50,
                classification_source: str = "llm",
                moved: bool = False,
                skip_reason: str | None = None,
                rule_id: int | None = None,
                subject: str = "Test subject") -> int:
    """Insert an audit_log row and return its id."""
    _seed_run(db, run_id)
    db.execute(
        "INSERT INTO audit_log "
        "(run_id, email_id, from_address, from_domain, subject, "
        " source_folder, target_folder, confidence, classification_source, "
        " moved, skip_reason, rule_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, email_id, from_address, from_domain, subject,
         "INBOX", target_folder, confidence, classification_source,
         moved, skip_reason, rule_id),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_rule(db: Database, *, condition_value: str,
               target_folder: str = "INBOX/Affairs/Stores",
               rule_type: str = "exact_sender",
               source: str = "auto",
               active: bool = True) -> int:
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, "
        "confidence, source, active, created_at, updated_at) "
        "VALUES (?, ?, ?, 0.90, ?, ?, datetime('now'), datetime('now'))",
        (rule_type, condition_value, target_folder, source, active),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_contact(db: Database, email: str, relationship: str | None = None):
    db.execute(
        "INSERT OR IGNORE INTO contacts (email_address, display_name, relationship) "
        "VALUES (?, ?, ?)",
        (email, email.split("@")[0], relationship),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Layer 2: Query-level tests
# ---------------------------------------------------------------------------

def test_dedup_no_inflation(db: Database):
    """An email classified across 5 runs should produce 1 skipped-then-sorted row."""
    for i in range(5):
        _seed_audit(db, email_id="msg-dup", run_id=f"run-{i}",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold",
                    target_folder="INBOX", confidence=0.40)
    _seed_audit(db, email_id="msg-dup", run_id="run-manual",
                classification_source="manual", moved=True,
                target_folder="Affairs/Stores")
    result = get_skipped_then_sorted(db, "-30 days")
    assert len(result) == 1
    assert result[0]["email_id"] == "msg-dup"
    assert result[0]["manual_folder"] == "Affairs/Stores"


def test_dedup_keeps_latest_row(db: Database):
    """The kept row should be the most recent (highest id)."""
    _seed_audit(db, email_id="msg-lat", run_id="run-old",
                classification_source="llm", moved=False,
                target_folder="INBOX", confidence=0.30)
    _seed_audit(db, email_id="msg-lat", run_id="run-new",
                classification_source="llm", moved=False,
                target_folder="Affairs/Banks", confidence=0.55)
    _seed_audit(db, email_id="msg-lat", run_id="run-manual",
                classification_source="manual", moved=True,
                target_folder="Affairs/Banks")
    result = get_skipped_then_sorted(db, "-30 days")
    assert len(result) == 1
    assert result[0]["llm_folder"] == "Affairs/Banks"
    assert result[0]["confidence"] == 0.55


def test_folder_gap_groups_by_destination(db: Database):
    """Wrong-folder emails grouped by manual_folder."""
    _seed_audit(db, email_id="msg-a", target_folder="INBOX",
                classification_source="llm", moved=False)
    _seed_audit(db, email_id="msg-a", run_id="run-m1",
                classification_source="manual", moved=True,
                target_folder="Affairs/Stores")
    _seed_audit(db, email_id="msg-b", target_folder="INBOX",
                classification_source="llm", moved=False)
    _seed_audit(db, email_id="msg-b", run_id="run-m2",
                classification_source="manual", moved=True,
                target_folder="Affairs/Stores")

    rows = get_skipped_then_sorted(db, "-30 days")
    cards = build_folder_gap_cards(rows)
    assert len(cards) == 1
    assert cards[0]["folder"] == "Affairs/Stores"
    assert cards[0]["count"] == 2


def test_folder_gap_excludes_same_folder(db: Database):
    """Same-folder matches should not appear in gap cards."""
    _seed_audit(db, email_id="msg-sf", target_folder="Affairs/Stores",
                classification_source="llm", moved=False)
    _seed_audit(db, email_id="msg-sf", run_id="run-m",
                classification_source="manual", moved=True,
                target_folder="Affairs/Stores")

    rows = get_skipped_then_sorted(db, "-30 days")
    cards = build_folder_gap_cards(rows)
    assert len(cards) == 0


def test_llm_corrections(db: Database):
    """LLM-moved emails later corrected by user."""
    _seed_audit(db, email_id="msg-cor", target_folder="Affairs/Banks",
                classification_source="llm", moved=True, confidence=0.90)
    _seed_audit(db, email_id="msg-cor", run_id="run-cor",
                classification_source="correction", moved=True,
                target_folder="Affairs/Stores")

    result = get_llm_corrections(db, "-30 days")
    assert len(result) == 1
    assert result[0]["llm_folder"] == "Affairs/Banks"
    assert result[0]["corrected_folder"] == "Affairs/Stores"


def test_known_contact_card_appears(db: Database):
    """Card appears when >= min_skips known-contact threshold blocks."""
    _seed_contact(db, "spouse@example.com", "spouse")
    for i in range(3):
        _seed_audit(db, email_id=f"msg-kc-{i}", run_id=f"run-kc-{i}",
                    from_address="spouse@example.com",
                    from_domain="example.com",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold_known_contact",
                    target_folder="People/Family", confidence=0.80)

    cards = get_known_contact_cards(db, "-30 days", min_skips=3,
                                    coherence_threshold=0.80)
    assert len(cards) == 1
    assert cards[0]["sender"] == "spouse@example.com"
    assert cards[0]["relationship"] == "spouse"
    assert cards[0]["blocked_count"] == 3


def test_known_contact_card_absent_below_threshold(db: Database):
    """No card when < min_skips."""
    _seed_contact(db, "friend@example.com")
    for i in range(2):
        _seed_audit(db, email_id=f"msg-kc2-{i}", run_id=f"run-kc2-{i}",
                    from_address="friend@example.com",
                    classification_source="llm", moved=False,
                    skip_reason="below_threshold_known_contact",
                    target_folder="People/Friends", confidence=0.80)

    cards = get_known_contact_cards(db, "-30 days", min_skips=3,
                                    coherence_threshold=0.80)
    assert len(cards) == 0


def test_eligibility_gate_counts(db: Database):
    """Correct breakdown by skip_reason."""
    _seed_audit(db, email_id="msg-f1", classification_source="llm",
                moved=False, skip_reason="flagged")
    _seed_audit(db, email_id="msg-f2", classification_source="llm",
                moved=False, skip_reason="flagged")
    _seed_audit(db, email_id="msg-u1", classification_source="llm",
                moved=False, skip_reason="unread")
    _seed_audit(db, email_id="msg-t1", classification_source="llm",
                moved=False, skip_reason="too_new")

    result = get_eligibility_gated(db, "-30 days")
    assert result["total"] == 4
    assert result["flagged"] == 2
    assert result["unread"] == 1
    assert result["too_new"] == 1


def test_learning_effectiveness(db: Database):
    """Rule learning stats with hit counts."""
    rule_id = _seed_rule(db, condition_value="store@example.com")
    _seed_audit(db, email_id="msg-r1", classification_source="rule",
                moved=True, rule_id=rule_id,
                target_folder="INBOX/Affairs/Stores")
    _seed_audit(db, email_id="msg-r2", classification_source="rule",
                moved=True, rule_id=rule_id,
                target_folder="INBOX/Affairs/Stores")

    result = get_learning_effectiveness(db, "-30 days")
    assert result["total_auto_rules"] == 1
    assert result["total_emails_sorted"] == 2
    assert result["recent_rules_count"] == 1
    assert result["recent_rules"][0]["emails_sorted"] == 2


def test_llm_accuracy_metrics(db: Database):
    """System effectiveness, move precision, threshold precision."""
    sources = [{"name": "llm", "count": 10, "moved": 4, "pct": 100.0}]
    llm_corrections = [{"email_id": "c1"}]
    skipped = [
        {"llm_folder": "INBOX", "manual_folder": "Affairs/Stores"},
        {"llm_folder": "INBOX", "manual_folder": "Affairs/Banks"},
        {"llm_folder": "Affairs/Stores", "manual_folder": "Affairs/Stores"},
    ]
    eligibility = {"total": 0, "flagged": 0, "unread": 0, "too_new": 0}

    result = build_llm_accuracy(sources, llm_corrections, skipped, eligibility)
    assert result["llm_total"] == 10
    assert result["llm_moved"] == 4
    assert result["llm_corrected"] == 1
    assert result["moved_correctly"] == 3
    assert result["later_sorted"] == 3
    assert result["agreed"] == 1
    assert result["disagreed"] == 2
    # System effectiveness: (3 + 2) / (4 + 3) = 5/7 = 71%
    assert result["system_effectiveness"] == 71
    # Move precision: 3 / 4 = 75%
    assert result["move_precision"] == 75
    # Threshold precision: 2 / 3 = 67%
    assert result["threshold_precision"] == 67


def test_llm_accuracy_zero_denominators():
    """N/A when no emails moved or sorted."""
    sources = [{"name": "llm", "count": 5, "moved": 0, "pct": 100.0}]
    result = build_llm_accuracy(sources, [], [], {"total": 0})
    assert result["system_effectiveness"] is None
    assert result["move_precision"] is None
    assert result["threshold_precision"] is None


def test_metric_color():
    assert "green" in _metric_color(85)
    assert "amber" in _metric_color(60)
    assert "red" in _metric_color(30)
    assert "gray" in _metric_color(None)


# ---------------------------------------------------------------------------
# Layer 1: Route-level tests
# ---------------------------------------------------------------------------

@pytest.fixture
def web_db():
    """File-backed temp DB for route tests (middleware opens its own connection)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    database.connect()
    _run_migrations(database)
    yield database, db_path
    database.close()
    os.unlink(db_path)


def _make_client(db_path: str) -> TestClient:
    """Create a TestClient using a file-backed DB the middleware can reopen."""
    cfg = Config(
        fastmail_api_token="test-token-abc123",
        anthropic_api_key="test-anthropic-key",
        db_path=db_path,
    )
    app = create_app(cfg)
    return TestClient(app)


def test_analyze_empty_db(web_db):
    """Empty DB renders without error, shows 'no data' message."""
    _db, db_path = web_db
    client = _make_client(db_path)
    response = client.get("/analyze?days=30")
    assert response.status_code == 200
    assert "No classification data" in response.text


def test_analyze_with_data(web_db):
    """Page renders with seeded data and contains new card headings."""
    db, db_path = web_db
    _seed_audit(db, email_id="msg-1", target_folder="INBOX",
                classification_source="llm", moved=False,
                skip_reason="below_threshold", confidence=0.40)
    _seed_audit(db, email_id="msg-1", run_id="run-m",
                classification_source="manual", moved=True,
                target_folder="Affairs/Stores")
    _seed_audit(db, email_id="msg-2", run_id="run-2",
                target_folder="Affairs/Banks",
                classification_source="llm", moved=True, confidence=0.90)

    client = _make_client(db_path)
    response = client.get("/analyze?days=30")
    assert response.status_code == 200
    assert "Classification Analysis" in response.text
    assert "LLM Accuracy Summary" in response.text
    assert "Folder Description Gaps" in response.text


def test_analyze_learning_card(web_db):
    """Learning Effectiveness card appears with auto rules."""
    db, db_path = web_db
    rule_id = _seed_rule(db, condition_value="news@example.com")
    _seed_audit(db, email_id="msg-lr", classification_source="rule",
                moved=True, rule_id=rule_id,
                target_folder="INBOX/Affairs/Stores")

    client = _make_client(db_path)
    response = client.get("/analyze?days=30")
    assert response.status_code == 200
    assert "Learning Effectiveness" in response.text
    assert "news@example.com" in response.text
