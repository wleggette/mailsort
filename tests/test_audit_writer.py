"""Tests for the audit writer."""

from __future__ import annotations

from mailsort.audit.writer import AuditWriter
from mailsort.config import ThresholdsConfig
from mailsort.db.database import Database
from mailsort.jmap.models import Classification, EmailFeatures, MoveDecision
from mailsort.mover.mover import build_move_decision


def _make_features(**overrides) -> EmailFeatures:
    defaults = dict(
        email_id="email-001",
        thread_id="thread-001",
        from_address="noreply@chase.com",
        from_domain="chase.com",
        to_addresses=["user@fastmail.com"],
        subject="Your statement is ready",
        list_id=None,
        list_unsubscribe=None,
        received_at="2026-03-10T10:00:00+00:00",
        preview="Your January statement is available.",
        keywords=["$seen"],
        current_mailbox_ids={"mb-inbox": True},
    )
    defaults.update(overrides)
    return EmailFeatures(**defaults)


# ------------------------------------------------------------------
# Run lifecycle
# ------------------------------------------------------------------

def test_start_run(db: Database):
    audit = AuditWriter(db)
    run_id = audit.start_run(trigger="test")

    row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["trigger"] == "test"


def test_finish_run(db: Database):
    audit = AuditWriter(db)
    run_id = audit.start_run()
    audit.finish_run(run_id, status="completed", emails_seen=10, emails_moved=7)

    row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "completed"
    assert row["emails_seen"] == 10
    assert row["emails_moved"] == 7
    assert row["finished_at"] is not None


def test_reconcile_stale_runs(db: Database):
    audit = AuditWriter(db)
    # Simulate two stale runs
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('stale-1', datetime('now'), 'running')"
    )
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('stale-2', datetime('now'), 'running')"
    )
    db.commit()

    count = audit.reconcile_stale_runs()
    assert count == 2

    for rid in ("stale-1", "stale-2"):
        row = db.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert row["status"] == "abandoned"


def test_reconcile_leaves_completed_alone(db: Database):
    audit = AuditWriter(db)
    db.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES ('done-1', datetime('now'), 'completed')"
    )
    db.commit()

    count = audit.reconcile_stale_runs()
    assert count == 0

    row = db.execute("SELECT status FROM runs WHERE run_id=?", ("done-1",)).fetchone()
    assert row["status"] == "completed"


# ------------------------------------------------------------------
# Decision logging
# ------------------------------------------------------------------

def test_log_decisions_moved(db: Database):
    audit = AuditWriter(db)
    run_id = audit.start_run()

    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="rule",
    )
    features = _make_features()
    decision = build_move_decision(features, clf, {}, ThresholdsConfig())

    outcomes = {"email-001": True}
    audit.log_decisions(run_id, [decision], outcomes)

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["moved"] == 1
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["classification_source"] == "rule"
    assert row["run_id"] == run_id


def test_log_decisions_skipped(db: Database):
    audit = AuditWriter(db)
    run_id = audit.start_run()

    features = _make_features()
    decision = build_move_decision(
        features, None, {}, ThresholdsConfig(), skip_reason="llm_skip_sender",
    )

    audit.log_decisions(run_id, [decision], {})

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["moved"] == 0
    assert row["skip_reason"] == "llm_skip_sender"


def test_log_decisions_move_failed(db: Database):
    audit = AuditWriter(db)
    run_id = audit.start_run()

    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="rule",
    )
    features = _make_features()
    decision = build_move_decision(features, clf, {}, ThresholdsConfig())

    # JMAP reported failure
    outcomes = {"email-001": False}
    audit.log_decisions(run_id, [decision], outcomes)

    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row["moved"] == 0
