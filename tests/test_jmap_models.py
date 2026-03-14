"""Tests for JMAP wire models and EmailFeatures extraction."""

from __future__ import annotations

from mailsort.jmap.models import EmailFeatures, JMAPEmail, JMAPSession


def test_jmap_email_from_address(sample_emails: list[JMAPEmail]):
    chase = sample_emails[0]
    assert chase.from_address == "noreply@chase.com"
    assert chase.from_domain == "chase.com"


def test_jmap_email_list_id(sample_emails: list[JMAPEmail]):
    github = sample_emails[1]
    assert github.list_id == "<mailsort.github.com>"


def test_jmap_email_no_list_id(sample_emails: list[JMAPEmail]):
    chase = sample_emails[0]
    assert chase.list_id is None


def test_jmap_email_received_at_parsed(sample_emails: list[JMAPEmail]):
    chase = sample_emails[0]
    dt = chase.received_at_dt
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 3


def test_email_features_from_jmap(sample_emails: list[JMAPEmail]):
    github = sample_emails[1]
    features = EmailFeatures.from_jmap_email(github)

    assert features.email_id == "email-002"
    assert features.thread_id == "thread-002"
    assert features.from_address == "noreply@github.com"
    assert features.from_domain == "github.com"
    assert features.list_id == "<mailsort.github.com>"
    assert features.subject == "[mailsort] PR #42 opened by contributor"
    assert "$seen" in features.keywords


def test_email_features_keywords(sample_emails: list[JMAPEmail]):
    flagged = sample_emails[4]
    features = EmailFeatures.from_jmap_email(flagged)
    assert "$flagged" in features.keywords
    assert "$seen" in features.keywords


def test_jmap_session_from_response():
    raw = {
        "apiUrl": "https://api.fastmail.com/jmap/api/",
        "primaryAccounts": {
            "urn:ietf:params:jmap:mail": "u12345678",
        },
        "accounts": {"u12345678": {}},
        "capabilities": {
            "urn:ietf:params:jmap:core": {},
            "urn:ietf:params:jmap:mail": {},
        },
    }
    session = JMAPSession.from_response(raw)
    assert session.account_id == "u12345678"
    assert session.api_url == "https://api.fastmail.com/jmap/api/"
    assert "urn:ietf:params:jmap:mail" in session.capabilities
