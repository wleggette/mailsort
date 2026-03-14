"""Tests for folder description generation — LLM and fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.classifier.descriptions import (
    generate_folder_description,
    generate_descriptions_for_new_folders,
    _fallback_description,
)
from mailsort.db.database import Database
from mailsort.jmap.models import JMAPEmail


def _make_email(email_id: str, from_email: str, subject: str) -> JMAPEmail:
    return JMAPEmail.model_validate({
        "id": email_id,
        "threadId": "t1",
        "mailboxIds": {"mb-1": True},
        "from": [{"email": from_email}],
        "to": [{"email": "user@example.com"}],
        "subject": subject,
        "receivedAt": "2026-03-10T10:00:00Z",
        "keywords": {},
        "preview": "",
    })


# ------------------------------------------------------------------
# Fallback description (no LLM)
# ------------------------------------------------------------------

def test_fallback_description_leaf_name():
    assert _fallback_description("INBOX/Affairs/Banks") == "Emails filed under Banks"


def test_fallback_description_top_level():
    assert _fallback_description("Projects") == "Emails filed under Projects"


# ------------------------------------------------------------------
# generate_folder_description: skip if already populated
# ------------------------------------------------------------------

def test_skip_if_already_populated(db: Database):
    """Should not overwrite an existing description."""
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Banks', 'Existing description', 'manual')"
    )
    db.commit()

    emails = [_make_email("e1", "chase@chase.com", "Statement")]
    result = generate_folder_description(
        db, "INBOX/Affairs/Banks", emails,
        anthropic_api_key="test-key",
    )
    assert result is None

    # Description should be unchanged
    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row["description"] == "Existing description"


def test_skip_if_manual_override(db: Database):
    """Should not generate if folder has a config override."""
    emails = [_make_email("e1", "chase@chase.com", "Statement")]
    result = generate_folder_description(
        db, "INBOX/Affairs/Banks", emails,
        anthropic_api_key="test-key",
        folder_description_overrides={"INBOX/Affairs/Banks": "Manual override"},
    )
    assert result is None


# ------------------------------------------------------------------
# generate_folder_description: fallback when no API key
# ------------------------------------------------------------------

def test_fallback_when_no_api_key(db: Database):
    """Should use fallback description when no Anthropic API key is configured."""
    emails = [_make_email("e1", "chase@chase.com", "Statement")]
    result = generate_folder_description(
        db, "INBOX/Affairs/Banks", emails,
        anthropic_api_key="",
    )
    assert result == "Emails filed under Banks"

    row = db.execute(
        "SELECT * FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row is not None
    assert row["description"] == "Emails filed under Banks"
    assert row["source"] == "auto"


def test_fallback_when_no_emails(db: Database):
    """Should use fallback when no sample emails are provided."""
    result = generate_folder_description(
        db, "INBOX/Affairs/Banks", [],
        anthropic_api_key="test-key",
    )
    assert result == "Emails filed under Banks"


# ------------------------------------------------------------------
# generate_folder_description: LLM generation
# ------------------------------------------------------------------

def test_llm_generation(db: Database):
    """Should call the LLM and store the result."""
    emails = [
        _make_email("e1", "noreply@chase.com", "Your January statement"),
        _make_email("e2", "alerts@chase.com", "Fraud alert"),
    ]

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.return_value = "Bank statements and transaction alerts"

        result = generate_folder_description(
            db, "INBOX/Affairs/Banks", emails,
            anthropic_api_key="test-key",
        )

    assert result == "Bank statements and transaction alerts"

    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row["description"] == "Bank statements and transaction alerts"


def test_llm_failure_falls_back(db: Database):
    """If LLM call fails, should fall back to name-based description."""
    emails = [_make_email("e1", "noreply@chase.com", "Statement")]

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.side_effect = Exception("API down")

        result = generate_folder_description(
            db, "INBOX/Affairs/Banks", emails,
            anthropic_api_key="test-key",
        )

    assert result == "Emails filed under Banks"


# ------------------------------------------------------------------
# generate_descriptions_for_new_folders
# ------------------------------------------------------------------

def test_generate_for_new_folders_only(db: Database):
    """Should only generate for folders without existing descriptions."""
    # Pre-populate one folder
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Banks', 'Already described', 'auto')"
    )
    db.commit()

    count = generate_descriptions_for_new_folders(
        db,
        {"INBOX/Affairs/Banks", "INBOX/Shopping/Orders"},
        anthropic_api_key="",
    )

    # Only Orders should get a new description
    assert count == 1

    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Shopping/Orders'"
    ).fetchone()
    assert row is not None
    assert "Orders" in row["description"]

    # Banks should be unchanged
    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row["description"] == "Already described"
