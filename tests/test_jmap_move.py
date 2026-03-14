"""Tests for move_emails: mailbox preservation and keyword tagging.

These test the JMAP update payload construction, not actual HTTP calls.
We mock JMAPClient.call() and inspect what payload it receives.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.jmap.client import JMAPClient


def _make_client() -> JMAPClient:
    """Create a JMAPClient with a pre-cached session so it skips HTTP."""
    client = JMAPClient.__new__(JMAPClient)
    client._token = "test"
    client._session_url = "http://test"
    client._http = MagicMock()

    # Pre-cache a fake session (read/write)
    session = MagicMock()
    session.account_id = "u12345"
    session.api_url = "http://test/api"
    session.capabilities = set()
    session.is_read_only = False
    client._session = session
    return client


def _mock_success_response(*email_ids: str) -> dict:
    """Build a fake Email/set success response."""
    return {
        "methodResponses": [
            ["Email/set", {
                "updated": {eid: None for eid in email_ids},
                "notUpdated": {},
            }, "s1"]
        ]
    }


# ------------------------------------------------------------------
# Mailbox preservation: inbox removed, target added, others kept
# ------------------------------------------------------------------

def test_move_preserves_other_mailbox_memberships():
    """An email in inbox + another folder should keep the other folder after move."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("email-1"))

    moves = [
        ("email-1", "mb-banks", {"mb-inbox": True, "mb-label-important": True}),
    ]
    client.move_emails(moves, inbox_id="mb-inbox")

    # Inspect the payload sent to call()
    call_args = client.call.call_args[0][0]  # first positional arg = method_calls
    update_payload = call_args[0][1]["update"]["email-1"]

    expected_mailboxes = {"mb-banks": True, "mb-label-important": True}
    assert update_payload["mailboxIds"] == expected_mailboxes
    assert "mb-inbox" not in update_payload["mailboxIds"]


def test_move_removes_inbox_adds_target():
    """Basic case: email only in inbox → inbox removed, target added."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("email-1"))

    moves = [
        ("email-1", "mb-banks", {"mb-inbox": True}),
    ]
    client.move_emails(moves, inbox_id="mb-inbox")

    call_args = client.call.call_args[0][0]
    update_payload = call_args[0][1]["update"]["email-1"]

    assert update_payload["mailboxIds"] == {"mb-banks": True}


def test_move_email_in_multiple_non_inbox_folders():
    """Email in inbox + two other folders → both other folders preserved."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("email-1"))

    moves = [
        ("email-1", "mb-banks", {"mb-inbox": True, "mb-archive": True, "mb-starred": True}),
    ]
    client.move_emails(moves, inbox_id="mb-inbox")

    call_args = client.call.call_args[0][0]
    update_payload = call_args[0][1]["update"]["email-1"]

    assert update_payload["mailboxIds"] == {
        "mb-banks": True,
        "mb-archive": True,
        "mb-starred": True,
    }


# ------------------------------------------------------------------
# Keyword tagging: uses patch path, doesn't overwrite existing keywords
# ------------------------------------------------------------------

def test_keyword_tag_uses_patch_path():
    """The $mailsort-moved keyword should be set via JMAP patch path, not by replacing keywords."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("email-1"))

    moves = [("email-1", "mb-banks", {"mb-inbox": True})]
    client.move_emails(moves, inbox_id="mb-inbox", tag_keyword="$mailsort-moved")

    call_args = client.call.call_args[0][0]
    update_payload = call_args[0][1]["update"]["email-1"]

    # Should use patch path syntax (keywords/...) not replace the full keywords dict
    assert "keywords/$mailsort-moved" in update_payload
    assert update_payload["keywords/$mailsort-moved"] is True
    # Should NOT have a top-level "keywords" key that would replace all keywords
    assert "keywords" not in update_payload


def test_keyword_tag_disabled():
    """When tag_keyword=None, no keyword patch should be in the payload."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("email-1"))

    moves = [("email-1", "mb-banks", {"mb-inbox": True})]
    client.move_emails(moves, inbox_id="mb-inbox", tag_keyword=None)

    call_args = client.call.call_args[0][0]
    update_payload = call_args[0][1]["update"]["email-1"]

    # No keyword-related keys at all
    keyword_keys = [k for k in update_payload if "keyword" in k.lower()]
    assert keyword_keys == []


# ------------------------------------------------------------------
# Batch: multiple emails in one call
# ------------------------------------------------------------------

def test_batch_move_multiple_emails():
    """Multiple emails with different mailbox states should each be handled correctly."""
    client = _make_client()
    client.call = MagicMock(return_value=_mock_success_response("e-1", "e-2"))

    moves = [
        ("e-1", "mb-banks", {"mb-inbox": True}),
        ("e-2", "mb-orders", {"mb-inbox": True, "mb-archive": True}),
    ]
    outcomes = client.move_emails(moves, inbox_id="mb-inbox")

    assert outcomes == {"e-1": True, "e-2": True}

    call_args = client.call.call_args[0][0]
    updates = call_args[0][1]["update"]

    assert updates["e-1"]["mailboxIds"] == {"mb-banks": True}
    assert updates["e-2"]["mailboxIds"] == {"mb-orders": True, "mb-archive": True}
