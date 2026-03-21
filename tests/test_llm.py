"""Tests for the LLM classifier (mocked — no real API calls)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mailsort.classifier.features import ContactInfo
from mailsort.classifier.llm import LLMClassifier
from mailsort.config import ClassificationConfig
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


def _make_classifier(valid_paths: set[str] | None = None) -> LLMClassifier:
    paths = valid_paths or {"INBOX/Affairs/Banks", "INBOX/Tech/GitHub", "INBOX/Shopping/Orders"}
    return LLMClassifier(
        api_key="test-key",
        config=ClassificationConfig(),
        valid_folder_paths=paths,
    )


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------

def test_parse_valid_response():
    clf = _make_classifier()
    result = clf._parse_response('{"folder": "INBOX/Affairs/Banks", "confidence": 0.92, "reasoning": "bank alert"}')
    assert result.folder_path == "INBOX/Affairs/Banks"
    assert result.confidence == 0.92
    assert result.reasoning == "bank alert"


def test_parse_markdown_fenced_response():
    clf = _make_classifier()
    result = clf._parse_response('```json\n{"folder": "INBOX/Affairs/Banks", "confidence": 0.95, "reasoning": "bank alert"}\n```')
    assert result.folder_path == "INBOX/Affairs/Banks"
    assert result.confidence == 0.95
    assert result.reasoning == "bank alert"


def test_parse_invalid_json():
    clf = _make_classifier()
    result = clf._parse_response("This is not JSON at all")
    assert result.folder_path == "INBOX"
    assert result.confidence == 0.0
    assert result.reasoning == "parse_error"


def test_parse_unknown_folder():
    clf = _make_classifier()
    result = clf._parse_response('{"folder": "INBOX/Nonexistent/Folder", "confidence": 0.95}')
    assert result.folder_path == "INBOX"
    assert result.confidence == 0.0


def test_parse_missing_confidence():
    clf = _make_classifier()
    result = clf._parse_response('{"folder": "INBOX/Affairs/Banks"}')
    assert result.folder_path == "INBOX/Affairs/Banks"
    assert result.confidence == 0.0


# ------------------------------------------------------------------
# Privacy gate
# ------------------------------------------------------------------

def test_should_call_default_allows():
    clf = _make_classifier()
    allowed, reason = clf.should_call(_make_features(), {})
    assert allowed is True
    assert reason is None


def test_should_call_skip_sender():
    config = ClassificationConfig()
    config.llm_skip_senders = ["secret@personal.com"]
    clf = LLMClassifier(api_key="key", config=config, valid_folder_paths=set())

    features = _make_features(from_address="secret@personal.com")
    allowed, reason = clf.should_call(features, {})
    assert allowed is False
    assert reason == "llm_skip_sender"


def test_should_call_skip_sender_allows_other():
    config = ClassificationConfig()
    config.llm_skip_senders = ["secret@personal.com"]
    clf = LLMClassifier(api_key="key", config=config, valid_folder_paths=set())

    features = _make_features(from_address="other@example.com")
    allowed, reason = clf.should_call(features, {})
    assert allowed is True
    assert reason is None


def test_should_call_skip_domain():
    config = ClassificationConfig()
    config.llm_skip_domains = ["bank.example.com"]
    clf = LLMClassifier(api_key="key", config=config, valid_folder_paths=set())

    features = _make_features(from_address="alerts@bank.example.com", from_domain="bank.example.com")
    allowed, reason = clf.should_call(features, {})
    assert allowed is False
    assert reason == "llm_skip_domain"


def test_should_call_skip_domain_allows_other():
    config = ClassificationConfig()
    config.llm_skip_domains = ["bank.example.com"]
    clf = LLMClassifier(api_key="key", config=config, valid_folder_paths=set())

    features = _make_features(from_address="noreply@other.com", from_domain="other.com")
    allowed, reason = clf.should_call(features, {})
    assert allowed is True
    assert reason is None


def test_should_call_skip_known_contact_when_disabled():
    config = ClassificationConfig()
    config.llm_allow_known_contacts = False
    clf = LLMClassifier(api_key="key", config=config, valid_folder_paths=set())

    contacts = {"noreply@chase.com": ContactInfo("noreply@chase.com", "Chase")}
    allowed, reason = clf.should_call(_make_features(), contacts)
    assert allowed is False
    assert reason == "llm_skip_known_contact"


# ------------------------------------------------------------------
# Full classify (mocked API)
# ------------------------------------------------------------------

def test_classify_calls_api(monkeypatch):
    clf = _make_classifier()

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"folder": "INBOX/Affairs/Banks", "confidence": 0.92, "reasoning": "bank statement"}')]

    monkeypatch.setattr(clf._client.messages, "create", MagicMock(return_value=mock_response))

    result = clf.classify(
        _make_features(),
        folder_descriptions="INBOX/Affairs/Banks: Banking correspondence",
    )
    assert result.folder_path == "INBOX/Affairs/Banks"
    assert result.confidence == 0.92
    assert result.source == "llm"


def test_classify_with_contact_enrichment(monkeypatch):
    clf = _make_classifier()

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"folder": "INBOX/Affairs/Banks", "confidence": 0.85, "reasoning": "spouse forwarded bank info"}')]

    create_mock = MagicMock(return_value=mock_response)
    monkeypatch.setattr(clf._client.messages, "create", create_mock)

    contact = ContactInfo("husband@gmail.com", "John Smith", relationship="spouse")
    clf.classify(
        _make_features(from_address="husband@gmail.com"),
        folder_descriptions="INBOX/Affairs/Banks: Banking",
        contact=contact,
    )
    # Verify the prompt includes contact info
    call_args = create_mock.call_args
    prompt = call_args.kwargs["messages"][0]["content"]
    assert 'known contact: "John Smith (spouse)"' in prompt


def test_classify_api_error_returns_safe_default(monkeypatch):
    clf = _make_classifier()
    monkeypatch.setattr(
        clf._client.messages, "create",
        MagicMock(side_effect=Exception("Connection timeout")),
    )

    result = clf.classify(_make_features(), folder_descriptions="...")
    assert result.folder_path == "INBOX"
    assert result.confidence == 0.0
    assert result.reasoning == "api_error"
