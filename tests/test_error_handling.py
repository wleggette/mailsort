"""Tests for error handling across I/O boundaries.

Verifies that:
- JMAP move failures don't prevent audit logging
- Per-email classification errors don't kill the batch
- Audit writer DB errors are logged but don't mask operational errors
- Thread context DB errors fall through gracefully
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mailsort.audit.writer import AuditWriter
from mailsort.classifier.pipeline import ClassificationPipeline
from mailsort.classifier.rules import RuleEngine
from mailsort.config import (
    Config, ClassificationConfig, FastmailConfig, SchedulerConfig, ThresholdsConfig,
)
from mailsort.db.database import Database
from mailsort.jmap.mailbox_tree import MailboxTree
from mailsort.jmap.models import (
    Classification, EmailFeatures, JMAPEmail, JMAPMailbox, MoveDecision,
)
from mailsort.mover.mover import build_move_decision
from mailsort.orchestrator import run_classification_pass


def _make_config() -> Config:
    return Config(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(interval_minutes=15, min_age_minutes=240, max_batch_size=100),
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
    ]
    return MailboxTree.build(mailboxes)


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


def _make_jmap_email(email_id: str = "email-001", from_email: str = "noreply@chase.com") -> JMAPEmail:
    return JMAPEmail.model_validate({
        "id": email_id,
        "threadId": "thread-001",
        "mailboxIds": {"mb-inbox": True},
        "from": [{"name": "Chase", "email": from_email}],
        "to": [{"email": "user@fastmail.com"}],
        "subject": "Your statement",
        "receivedAt": "2026-03-10T10:00:00Z",
        "keywords": {"$seen": True},
        "preview": "Your January statement is available.",
    })


# ------------------------------------------------------------------
# JMAP move_emails crash → decisions still logged
# ------------------------------------------------------------------

def test_jmap_move_crash_still_logs_decisions(db: Database):
    """If jmap.move_emails() throws, audit_log rows should still be written."""
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
    mock_jmap.move_emails.side_effect = ConnectionError("Network down")

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    # Decisions should still be in audit_log
    row = db.execute("SELECT * FROM audit_log WHERE email_id='email-001'").fetchone()
    assert row is not None
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 0  # move was not confirmed

    # Run should be 'error' (move failure is non-fatal but recorded)
    run_row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run_row["status"] == "error"
    assert run_row["error_summary"] is not None

    # Planned entries should have skip_reason='move_failed'
    assert row["skip_reason"] == "move_failed"


# ------------------------------------------------------------------
# Per-email classification error → other emails still processed
# ------------------------------------------------------------------

def test_classification_error_isolates_to_single_email(db: Database):
    """If classifying one email throws, the rest should still be processed."""
    cfg = _make_config()
    tree = _make_tree()

    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'good@example.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.commit()

    email_good = _make_jmap_email(email_id="email-good", from_email="good@example.com")
    email_bad = _make_jmap_email(email_id="email-bad", from_email="bad@example.com")

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.return_value = ["email-bad", "email-good"]
    mock_jmap.get_emails.return_value = [email_bad, email_good]
    mock_jmap.get_thread_email_ids.return_value = []
    mock_jmap.move_emails.return_value = {"email-good": True}

    # Patch the pipeline's classify to throw on the first email only.
    # This tests the orchestrator's per-email try/except.
    original_import = __import__
    call_count = {"n": 0}
    original_classify = ClassificationPipeline.classify

    def classify_with_crash(self, features):
        call_count["n"] += 1
        if features.email_id == "email-bad":
            raise RuntimeError("Simulated classification crash")
        return original_classify(self, features)

    with patch.object(ClassificationPipeline, "classify", classify_with_crash):
        run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    # The bad email should be logged as skipped (classification_error)
    bad_row = db.execute("SELECT * FROM audit_log WHERE email_id='email-bad'").fetchone()
    assert bad_row is not None
    assert bad_row["moved"] == 0
    assert bad_row["skip_reason"] == "classification_error"

    # The good email should have been classified and moved
    good_row = db.execute("SELECT * FROM audit_log WHERE email_id='email-good'").fetchone()
    assert good_row is not None
    assert good_row["moved"] == 1
    assert good_row["target_folder"] == "INBOX/Affairs/Banks"


# ------------------------------------------------------------------
# finish_run DB failure → does not raise (defensive)
# ------------------------------------------------------------------

def test_finish_run_db_failure_does_not_raise(db: Database):
    """finish_run should log but not raise if the DB write fails."""
    audit = AuditWriter(db)
    run_id = audit.start_run()

    # Close the DB to force a write error
    db.close()

    # Should not raise — error is logged internally
    audit.finish_run(run_id, status="completed", emails_seen=5, emails_moved=3)

    # Reconnect for cleanup (fixture needs it)
    db.connect()


# ------------------------------------------------------------------
# log_decisions per-row isolation
# ------------------------------------------------------------------

def test_log_decisions_partial_failure(db: Database):
    """If one audit_log insert fails, the rest should still be written."""
    audit = AuditWriter(db)
    run_id = audit.start_run()

    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="rule",
    )
    d1 = build_move_decision(_make_features(email_id="e-1"), clf, {}, ThresholdsConfig())
    d2 = build_move_decision(_make_features(email_id="e-2"), clf, {}, ThresholdsConfig())

    # Patch log_decision to fail on the first call, succeed on the second
    original_log = audit.log_decision
    call_count = {"n": 0}

    def flaky_log(run_id, decision, moved):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulated DB error")
        return original_log(run_id, decision, moved)

    with patch.object(audit, "log_decision", side_effect=flaky_log):
        audit.log_decisions(run_id, [d1, d2], {"e-1": True, "e-2": True})

    # Second decision should have been written despite first failing
    rows = db.execute("SELECT email_id FROM audit_log ORDER BY email_id").fetchall()
    assert len(rows) == 1
    assert rows[0]["email_id"] == "e-2"


# ------------------------------------------------------------------
# Thread context DB error → falls through to rule engine
# ------------------------------------------------------------------

def test_thread_context_db_error_falls_through(db: Database):
    """If the thread context DB query fails, classification falls through to rules."""
    rule_engine = RuleEngine(db, ThresholdsConfig())
    rule_engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )

    mock_jmap = MagicMock()
    mock_tree = MagicMock()
    mock_tree.inbox_id = "mb-inbox"

    # Create a pipeline with a broken DB for the thread context query
    mock_db = MagicMock()
    mock_db.execute.side_effect = RuntimeError("DB locked")

    pipeline = ClassificationPipeline(
        db=mock_db,
        rule_engine=rule_engine,
        llm_classifier=None,
        jmap_client=mock_jmap,
        mailbox_tree=mock_tree,
        contacts={},
        folder_descriptions="",
    )

    features = _make_features()
    # The pipeline should catch the DB error in thread context
    # and fall through. The rule engine uses its own db handle (the real one),
    # but here the rule engine was created with the real db. However, the
    # pipeline's _db is the mock. Thread context will fail, then rules will
    # be tried via self._rules which has the real db.
    clf, skip = pipeline.classify(features)

    assert clf is not None
    assert clf.source == "rule"
    assert clf.folder_path == "INBOX/Affairs/Banks"


# ------------------------------------------------------------------
# Full run with JMAP query failure → run marked failed
# ------------------------------------------------------------------

def test_jmap_query_failure_completes_with_zero(db: Database):
    """If the JMAP inbox query throws, run should complete gracefully with 0 seen."""
    cfg = _make_config()
    tree = _make_tree()

    mock_jmap = MagicMock()
    mock_jmap.query_inbox_emails.side_effect = ConnectionError("JMAP unreachable")

    run_id = run_classification_pass(cfg, db, mock_jmap, tree, trigger="test")

    # The run should complete (graceful degradation) with 0 emails
    row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert row["emails_seen"] == 0
