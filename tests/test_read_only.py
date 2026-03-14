"""Tests for read-only token detection and write guards."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.jmap.client import JMAPClient, ReadOnlyTokenError
from mailsort.jmap.models import JMAPSession

import pytest


def _make_session(*, is_read_only: bool = False) -> JMAPSession:
    return JMAPSession(
        account_id="u12345",
        api_url="http://test/api",
        capabilities={"urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"},
        is_read_only=is_read_only,
    )


def _make_client(*, is_read_only: bool = False) -> JMAPClient:
    client = JMAPClient.__new__(JMAPClient)
    client._token = "test"
    client._session_url = "http://test"
    client._http = MagicMock()
    client._session = _make_session(is_read_only=is_read_only)
    return client


# ------------------------------------------------------------------
# Session parsing: read-only detection
# ------------------------------------------------------------------

def test_session_read_only_from_account():
    """isReadOnly on the account should set is_read_only=True."""
    data = {
        "apiUrl": "http://test/api",
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "u123"},
        "accounts": {
            "u123": {
                "isReadOnly": True,
                "accountCapabilities": {
                    "urn:ietf:params:jmap:mail": {},
                },
            }
        },
        "capabilities": {"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {}},
    }
    session = JMAPSession.from_response(data)
    assert session.is_read_only is True


def test_session_read_write():
    """Normal read/write account should have is_read_only=False."""
    data = {
        "apiUrl": "http://test/api",
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "u123"},
        "accounts": {
            "u123": {
                "isReadOnly": False,
                "accountCapabilities": {
                    "urn:ietf:params:jmap:mail": {},
                },
            }
        },
        "capabilities": {"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {}},
    }
    session = JMAPSession.from_response(data)
    assert session.is_read_only is False


def test_session_read_only_from_mail_capability():
    """isReadOnly on the mail capability should also be detected."""
    data = {
        "apiUrl": "http://test/api",
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "u123"},
        "accounts": {
            "u123": {
                "isReadOnly": False,
                "accountCapabilities": {
                    "urn:ietf:params:jmap:mail": {"isReadOnly": True},
                },
            }
        },
        "capabilities": {"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {}},
    }
    session = JMAPSession.from_response(data)
    assert session.is_read_only is True


# ------------------------------------------------------------------
# Write guard: move_emails with read-only token
# ------------------------------------------------------------------

def test_move_emails_raises_on_read_only():
    """move_emails should raise ReadOnlyTokenError if token is read-only."""
    client = _make_client(is_read_only=True)

    with pytest.raises(ReadOnlyTokenError, match="read-only"):
        client.move_emails(
            [("email-1", "mb-banks", {"mb-inbox": True})],
            inbox_id="mb-inbox",
        )


def test_move_emails_allowed_when_read_write():
    """move_emails should proceed normally with a read/write token."""
    client = _make_client(is_read_only=False)
    client.call = MagicMock(return_value={
        "methodResponses": [
            ["Email/set", {"updated": {"email-1": None}, "notUpdated": {}}, "s1"]
        ]
    })

    result = client.move_emails(
        [("email-1", "mb-banks", {"mb-inbox": True})],
        inbox_id="mb-inbox",
    )
    assert result == {"email-1": True}


# ------------------------------------------------------------------
# Client property
# ------------------------------------------------------------------

def test_client_is_read_only_property():
    assert _make_client(is_read_only=True).is_read_only is True
    assert _make_client(is_read_only=False).is_read_only is False
