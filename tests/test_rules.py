"""Tests for the rule engine."""

from __future__ import annotations

from mailsort.classifier.rules import RuleEngine
from mailsort.config import ThresholdsConfig
from mailsort.db.database import Database
from mailsort.jmap.models import EmailFeatures


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


def _make_engine(db: Database) -> RuleEngine:
    return RuleEngine(db, ThresholdsConfig())


# ------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------

def test_exact_sender_match(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )
    features = _make_features()
    clf = engine.classify(features)
    assert clf is not None
    assert clf.folder_path == "INBOX/Affairs/Banks"
    assert clf.source == "rule"


def test_sender_domain_match(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="sender_domain",
        condition_value="chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.90,
        source="auto",
    )
    features = _make_features(from_address="alerts@chase.com")
    clf = engine.classify(features)
    assert clf is not None
    assert clf.folder_path == "INBOX/Affairs/Banks"


def test_list_id_match(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="list_id",
        condition_value="<mailsort.github.com>",
        target_folder_path="INBOX/Tech/GitHub",
        confidence=0.95,
        source="bootstrap",
    )
    features = _make_features(list_id="<mailsort.github.com>", from_address="noreply@github.com")
    clf = engine.classify(features)
    assert clf is not None
    assert clf.folder_path == "INBOX/Tech/GitHub"


def test_subject_regex_match(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="subject_regex",
        condition_value=r"Order #\d+",
        target_folder_path="INBOX/Shopping/Orders",
        confidence=0.90,
        source="manual",
    )
    features = _make_features(subject="Your Order #12345 has shipped")
    clf = engine.classify(features)
    assert clf is not None
    assert clf.folder_path == "INBOX/Shopping/Orders"


def test_no_match_returns_none(db: Database):
    engine = _make_engine(db)
    features = _make_features(from_address="unknown@random.org", from_domain="random.org")
    clf = engine.classify(features)
    assert clf is None


def test_below_threshold_returns_none(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.50,  # below 0.85 rule_move threshold
        source="auto",
    )
    features = _make_features()
    clf = engine.classify(features)
    assert clf is None


def test_inactive_rule_ignored(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
        active=False,
    )
    features = _make_features()
    clf = engine.classify(features)
    assert clf is None


# ------------------------------------------------------------------
# Priority: list_id > exact_sender > domain
# ------------------------------------------------------------------

def test_list_id_takes_priority_over_exact_sender(db: Database):
    engine = _make_engine(db)
    list_rule_id = engine.create_rule(
        rule_type="list_id",
        condition_value="<mailsort.github.com>",
        target_folder_path="INBOX/Tech/GitHub",
        confidence=0.95,
        source="bootstrap",
    )
    engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@github.com",
        target_folder_path="INBOX/Social/Notifications",
        confidence=0.95,
        source="manual",
    )
    features = _make_features(
        from_address="noreply@github.com",
        list_id="<mailsort.github.com>",
    )
    clf = engine.classify(features)
    assert clf.folder_path == "INBOX/Tech/GitHub"
    assert clf.rule_id == list_rule_id


def test_exact_sender_takes_priority_over_domain(db: Database):
    engine = _make_engine(db)
    exact_rule_id = engine.create_rule(
        rule_type="exact_sender",
        condition_value="statements@bigbank.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )
    engine.create_rule(
        rule_type="sender_domain",
        condition_value="bigbank.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.90,
        source="auto",
    )
    features = _make_features(
        from_address="statements@bigbank.com",
        from_domain="bigbank.com",
    )
    clf = engine.classify(features)
    assert clf is not None
    assert clf.rule_id == exact_rule_id


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------

def test_hit_count_incremented(db: Database):
    engine = _make_engine(db)
    rule_id = engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )
    features = _make_features()
    engine.classify(features)
    engine.classify(features)

    row = db.execute("SELECT hit_count FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["hit_count"] == 2


def test_hit_count_not_incremented_when_record_hits_false(db: Database):
    engine = RuleEngine(db, ThresholdsConfig(), record_hits=False)
    rule_id = engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Affairs/Banks",
        confidence=0.95,
        source="bootstrap",
    )
    features = _make_features()
    clf = engine.classify(features)
    assert clf is not None
    assert clf.folder_path == "INBOX/Affairs/Banks"

    row = db.execute("SELECT hit_count, last_hit_at FROM rules WHERE id = ?", (rule_id,)).fetchone()
    assert row["hit_count"] == 0
    assert row["last_hit_at"] is None


def test_reconcile_folders_deactivates_stale(db: Database):
    engine = _make_engine(db)
    engine.create_rule(
        rule_type="exact_sender",
        condition_value="noreply@chase.com",
        target_folder_path="INBOX/Deleted/Folder",
        confidence=0.95,
        source="bootstrap",
    )
    count = engine.reconcile_folders({"INBOX/Affairs/Banks", "INBOX/Tech/GitHub"})
    assert count == 1
    assert engine.find_existing_rule("exact_sender", "noreply@chase.com") is None
