"""Tests for the confidence gate and move decision builder."""

from __future__ import annotations

from mailsort.classifier.features import ContactInfo
from mailsort.config import ThresholdsConfig
from mailsort.jmap.models import Classification, EmailFeatures
from mailsort.mover.mover import build_move_decision, should_move


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


def _thresholds() -> ThresholdsConfig:
    return ThresholdsConfig()  # rule_move=0.85, llm_move=0.80, llm_move_known_contact=0.93


# ------------------------------------------------------------------
# Thread context bypasses threshold
# ------------------------------------------------------------------

def test_thread_source_always_moves():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.50, source="thread"
    )
    move, reason = should_move(clf, _make_features(), {}, _thresholds())
    assert move is True
    assert reason is None


# ------------------------------------------------------------------
# Rule-based threshold
# ------------------------------------------------------------------

def test_rule_above_threshold_moves():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="rule"
    )
    move, reason = should_move(clf, _make_features(), {}, _thresholds())
    assert move is True


def test_rule_below_threshold_skips():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.70, source="rule"
    )
    move, reason = should_move(clf, _make_features(), {}, _thresholds())
    assert move is False
    assert reason == "below_threshold"


# ------------------------------------------------------------------
# LLM threshold — unknown sender
# ------------------------------------------------------------------

def test_llm_above_threshold_moves():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.85, source="llm"
    )
    move, reason = should_move(clf, _make_features(), {}, _thresholds())
    assert move is True


def test_llm_below_threshold_skips():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.75, source="llm"
    )
    move, reason = should_move(clf, _make_features(), {}, _thresholds())
    assert move is False
    assert reason == "below_threshold"


# ------------------------------------------------------------------
# LLM threshold — known contact gets stricter threshold
# ------------------------------------------------------------------

def test_llm_known_contact_above_strict_threshold():
    contacts = {"husband@gmail.com": ContactInfo("husband@gmail.com", "Husband")}
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="llm"
    )
    features = _make_features(from_address="husband@gmail.com")
    move, reason = should_move(clf, features, contacts, _thresholds())
    assert move is True


def test_llm_known_contact_below_strict_threshold():
    contacts = {"husband@gmail.com": ContactInfo("husband@gmail.com", "Husband")}
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.85, source="llm"
    )
    features = _make_features(from_address="husband@gmail.com")
    move, reason = should_move(clf, features, contacts, _thresholds())
    assert move is False
    assert reason == "below_threshold_known_contact"


def test_llm_known_contact_between_thresholds_skips():
    """0.85 would pass for a stranger but not for a known contact (threshold=0.93)."""
    contacts = {"husband@gmail.com": ContactInfo("husband@gmail.com", "Husband")}
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.90, source="llm"
    )
    features = _make_features(from_address="husband@gmail.com")
    move, reason = should_move(clf, features, contacts, _thresholds())
    assert move is False
    assert reason == "below_threshold_known_contact"


# ------------------------------------------------------------------
# build_move_decision
# ------------------------------------------------------------------

def test_build_decision_with_classification():
    clf = Classification(
        folder_path="INBOX/Affairs/Banks", confidence=0.95, source="rule"
    )
    decision = build_move_decision(_make_features(), clf, {}, _thresholds())
    assert decision.should_move is True
    assert decision.skip_reason is None


def test_build_decision_no_classification():
    decision = build_move_decision(
        _make_features(), None, {}, _thresholds(), skip_reason="llm_skip_sender"
    )
    assert decision.should_move is False
    assert decision.skip_reason == "llm_skip_sender"
