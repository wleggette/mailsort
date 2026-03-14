"""Tests for the classification pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.classifier.features import ContactInfo
from mailsort.classifier.llm import LLMClassifier
from mailsort.classifier.pipeline import ClassificationPipeline
from mailsort.classifier.rules import RuleEngine
from mailsort.config import ClassificationConfig, ThresholdsConfig
from mailsort.db.database import Database
from mailsort.jmap.models import Classification, EmailFeatures


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


def _make_pipeline(
    db: Database,
    *,
    llm_response: Classification | None = None,
    llm_allowed: bool = True,
) -> ClassificationPipeline:
    """Build a pipeline with a real DB + rule engine but mocked JMAP and LLM."""
    rule_engine = RuleEngine(db, ThresholdsConfig())

    # Mock LLM
    mock_llm = MagicMock(spec=LLMClassifier)
    mock_llm.should_call.return_value = (llm_allowed, None if llm_allowed else "llm_skip_sender")
    if llm_response:
        mock_llm.classify.return_value = llm_response
    else:
        mock_llm.classify.return_value = Classification(
            folder_path="INBOX", confidence=0.0, source="llm", reasoning="no match"
        )

    # Mock JMAP client
    mock_jmap = MagicMock()
    mock_jmap.get_thread_email_ids.return_value = []

    # Mock mailbox tree
    mock_tree = MagicMock()
    mock_tree.inbox_id = "mb-inbox"
    mock_tree.path_for.return_value = None

    return ClassificationPipeline(
        db=db,
        rule_engine=rule_engine,
        llm_classifier=mock_llm,
        jmap_client=mock_jmap,
        mailbox_tree=mock_tree,
        contacts={},
        folder_descriptions="INBOX/Affairs/Banks: Bank stuff",
    )


# ------------------------------------------------------------------
# Thread context (via audit_log)
# ------------------------------------------------------------------

def test_thread_context_from_audit_log(db: Database):
    # Seed audit_log with a prior sort in the same thread
    db.execute("""
        INSERT INTO audit_log
            (email_id, thread_id, from_address, target_folder, confidence,
             classification_source, moved)
        VALUES
            ('email-000', 'thread-001', 'vendor@example.com',
             'INBOX/Affairs/Banks', 0.95, 'rule', 1)
    """)
    db.commit()

    pipeline = _make_pipeline(db)
    features = _make_features(email_id="email-001", thread_id="thread-001")
    clf, skip = pipeline.classify(features)

    assert clf is not None
    assert clf.source == "thread"
    assert clf.folder_path == "INBOX/Affairs/Banks"
    assert skip is None


# ------------------------------------------------------------------
# Rule engine fallback
# ------------------------------------------------------------------

def test_rule_match_bypasses_llm(db: Database):
    pipeline = _make_pipeline(db)
    pipeline._rules.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )
    features = _make_features()
    clf, skip = pipeline.classify(features)

    assert clf is not None
    assert clf.source == "rule"
    assert clf.folder_path == "INBOX/Affairs/Banks"
    # LLM should not have been called
    pipeline._llm.classify.assert_not_called()


# ------------------------------------------------------------------
# LLM fallback
# ------------------------------------------------------------------

def test_llm_called_when_no_rule_matches(db: Database):
    llm_result = Classification(
        folder_path="INBOX/Shopping/Orders", confidence=0.88, source="llm", reasoning="shipping"
    )
    pipeline = _make_pipeline(db, llm_response=llm_result)
    features = _make_features(from_address="orders@amazon.com", from_domain="amazon.com")
    clf, skip = pipeline.classify(features)

    assert clf is not None
    assert clf.source == "llm"
    assert clf.folder_path == "INBOX/Shopping/Orders"


def test_llm_gated_returns_skip(db: Database):
    pipeline = _make_pipeline(db, llm_allowed=False)
    features = _make_features()
    clf, skip = pipeline.classify(features)

    assert clf is None
    assert skip == "llm_skip_sender"


# ------------------------------------------------------------------
# No LLM configured
# ------------------------------------------------------------------

def test_no_llm_available(db: Database):
    rule_engine = RuleEngine(db, ThresholdsConfig())
    mock_jmap = MagicMock()
    mock_jmap.get_thread_email_ids.return_value = []
    mock_tree = MagicMock()
    mock_tree.inbox_id = "mb-inbox"

    pipeline = ClassificationPipeline(
        db=db,
        rule_engine=rule_engine,
        llm_classifier=None,
        jmap_client=mock_jmap,
        mailbox_tree=mock_tree,
        contacts={},
        folder_descriptions="",
    )
    features = _make_features(from_address="unknown@unknown.com", from_domain="unknown.com")
    clf, skip = pipeline.classify(features)
    assert clf is None
    assert skip == "llm_unavailable"
