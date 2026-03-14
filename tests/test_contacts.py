"""Tests for contact refresh: import from JMAP, daily refresh check, per-contact isolation."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailsort.classifier.features import (
    refresh_contacts, load_contacts, should_refresh_contacts, mark_contacts_refreshed,
)
from mailsort.config import KnownContactOverride
from mailsort.db.database import Database


# ------------------------------------------------------------------
# refresh_contacts: basic import
# ------------------------------------------------------------------

def test_refresh_contacts_imports_emails(db: Database):
    """Should import contact email addresses into the contacts table."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {
            "uid": "c1",
            "name": {"full": "John Smith"},
            "emails": {
                "e1": {"type": "personal", "value": "john@example.com"},
                "e2": {"type": "work", "value": "john@work.com"},
            },
        },
    ]

    count = refresh_contacts(db, mock_jmap)
    assert count == 2

    contacts = load_contacts(db)
    assert "john@example.com" in contacts
    assert "john@work.com" in contacts
    assert contacts["john@example.com"].display_name == "John Smith"


def test_refresh_contacts_applies_relationship_overrides(db: Database):
    """Config overrides should set the relationship field."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {
            "uid": "c1",
            "name": {"full": "Husband"},
            "emails": {"e1": {"value": "husband@gmail.com"}},
        },
    ]

    overrides = {"husband@gmail.com": KnownContactOverride(relationship="spouse")}
    refresh_contacts(db, mock_jmap, known_contact_overrides=overrides)

    contacts = load_contacts(db)
    assert contacts["husband@gmail.com"].relationship == "spouse"


def test_refresh_contacts_no_scope(db: Database):
    """If contacts scope is not available, should return 0 gracefully."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = set()  # no contacts scope
    mock_jmap.get_contacts.return_value = []

    count = refresh_contacts(db, mock_jmap)
    assert count == 0


def test_refresh_contacts_jmap_error(db: Database):
    """If JMAP call fails, should return 0 and not crash."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.side_effect = ConnectionError("network down")

    count = refresh_contacts(db, mock_jmap)
    assert count == 0


def test_refresh_contacts_bad_contact_skipped(db: Database):
    """One malformed contact should not prevent others from importing."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {"uid": "bad", "name": None, "emails": "not-a-dict"},  # malformed
        {
            "uid": "good",
            "name": {"full": "Good Contact"},
            "emails": {"e1": {"value": "good@example.com"}},
        },
    ]

    count = refresh_contacts(db, mock_jmap)
    assert count == 1

    contacts = load_contacts(db)
    assert "good@example.com" in contacts


def test_refresh_contacts_fastmail_format(db: Database):
    """Should handle Fastmail's actual ContactCard format with 'address' field."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {
            "uid": "92101776-7172-4ee3-830a-c07285b9d13b",
            "name": {
                "full": "Wesley Leggette",
                "components": [
                    {"kind": "surname", "value": "Leggette"},
                    {"kind": "given", "value": "Wesley"},
                ],
            },
            "emails": {
                "54e6eaf698ec26cb": {
                    "contexts": {"private": True},
                    "address": "wes@example.com",
                    "pref": 1,
                },
            },
        },
    ]

    count = refresh_contacts(db, mock_jmap)
    assert count == 1

    contacts = load_contacts(db)
    assert "wes@example.com" in contacts
    assert contacts["wes@example.com"].display_name == "Wesley Leggette"


def test_refresh_contacts_name_fallback(db: Database):
    """Should try given+surname if full name is missing."""
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {
            "uid": "c1",
            "name": {"given": "Jane", "surname": "Doe"},
            "emails": {"e1": {"value": "jane@example.com"}},
        },
    ]

    refresh_contacts(db, mock_jmap)
    contacts = load_contacts(db)
    assert contacts["jane@example.com"].display_name == "Jane Doe"


# ------------------------------------------------------------------
# Daily refresh check
# ------------------------------------------------------------------

def test_refresh_contacts_removes_stale(db: Database):
    """Contacts removed from Fastmail should be deleted locally on refresh."""
    # First, seed a contact that will become stale
    db.execute(
        "INSERT INTO contacts (email_address, display_name, refreshed_at) "
        "VALUES ('stale@example.com', 'Stale Person', datetime('now'))"
    )
    db.commit()

    # Refresh returns only a different contact — stale one should be removed
    mock_jmap = MagicMock()
    mock_jmap.session_capabilities = {"urn:ietf:params:jmap:contacts"}
    mock_jmap.get_contacts.return_value = [
        {
            "uid": "c1",
            "name": {"full": "Current Person"},
            "emails": {"e1": {"address": "current@example.com"}},
        },
    ]

    refresh_contacts(db, mock_jmap)

    contacts = load_contacts(db)
    assert "current@example.com" in contacts
    assert "stale@example.com" not in contacts


def test_should_refresh_contacts_first_time(db: Database):
    """First time ever — should return True."""
    assert should_refresh_contacts(db) is True


def test_should_refresh_contacts_recently_done(db: Database):
    """Just refreshed — should return False."""
    mark_contacts_refreshed(db)
    assert should_refresh_contacts(db) is False


def test_should_refresh_contacts_stale(db: Database):
    """Last refresh was 25 hours ago — should return True."""
    db.execute(
        "INSERT OR REPLACE INTO learner_state (key, value) "
        "VALUES ('last_contacts_refresh', datetime('now', '-25 hours'))"
    )
    db.commit()
    assert should_refresh_contacts(db) is True
