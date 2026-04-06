"""Tests for folder description generation — LLM and fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailsort.classifier.descriptions import (
    generate_folder_description,
    generate_descriptions_for_new_folders,
    regenerate_folder_description,
    regenerate_descriptions_for_folders,
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


# ------------------------------------------------------------------
# regenerate_folder_description
# ------------------------------------------------------------------

def test_regenerate_overwrites_existing(db: Database):
    """Regeneration should replace the old description with a new one."""
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Banks', 'Old auto description', 'auto')"
    )
    db.commit()

    emails = [_make_email("e1", "noreply@chase.com", "Your January statement")]

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.return_value = "Bank statements and fraud alerts"

        result = regenerate_folder_description(
            db, "INBOX/Affairs/Banks", emails,
            anthropic_api_key="test-key",
        )

    assert result.success
    assert result.old_description == "Old auto description"
    assert result.new_description == "Bank statements and fraud alerts"

    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row["description"] == "Bank statements and fraud alerts"


def test_regenerate_creates_new_if_none_exists(db: Database):
    """Regeneration should create a description if the folder has none."""
    emails = [_make_email("e1", "noreply@chase.com", "Statement")]

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.return_value = "Banking notifications"

        result = regenerate_folder_description(
            db, "INBOX/Affairs/Banks", emails,
            anthropic_api_key="test-key",
        )

    assert result.success
    assert result.old_description is None
    assert result.new_description == "Banking notifications"

    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row is not None
    assert row["description"] == "Banking notifications"


def test_regenerate_skips_manual_override(db: Database):
    """Regeneration should skip folders with config overrides."""
    emails = [_make_email("e1", "noreply@chase.com", "Statement")]

    result = regenerate_folder_description(
        db, "INBOX/Affairs/Banks", emails,
        anthropic_api_key="test-key",
        folder_description_overrides={"INBOX/Affairs/Banks": "Manual override"},
    )

    assert result.skipped
    assert result.skip_reason == "manual override in config"
    assert not result.success


def test_regenerate_error_no_api_key(db: Database):
    """Regeneration should fail if no API key is provided."""
    emails = [_make_email("e1", "noreply@chase.com", "Statement")]

    result = regenerate_folder_description(
        db, "INBOX/Affairs/Banks", emails,
        anthropic_api_key="",
    )

    assert not result.success
    assert result.error == "no Anthropic API key configured"


def test_regenerate_error_no_emails(db: Database):
    """Regeneration should fail if no emails are available."""
    result = regenerate_folder_description(
        db, "INBOX/Affairs/Banks", [],
        anthropic_api_key="test-key",
    )

    assert not result.success
    assert result.error == "no sample emails available"


def test_regenerate_keeps_old_on_llm_failure(db: Database):
    """If LLM fails during regeneration, old description should be preserved."""
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Banks', 'Original description', 'auto')"
    )
    db.commit()

    emails = [_make_email("e1", "noreply@chase.com", "Statement")]

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.side_effect = Exception("API down")

        result = regenerate_folder_description(
            db, "INBOX/Affairs/Banks", emails,
            anthropic_api_key="test-key",
        )

    assert not result.success
    assert result.old_description == "Original description"
    assert "API down" in result.error

    # Old description should still be in the database
    row = db.execute(
        "SELECT description FROM folder_descriptions WHERE folder_path = 'INBOX/Affairs/Banks'"
    ).fetchone()
    assert row["description"] == "Original description"


# ------------------------------------------------------------------
# regenerate_descriptions_for_folders (batch)
# ------------------------------------------------------------------

def test_regenerate_batch(db: Database):
    """Batch regeneration should process multiple folders."""
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Banks', 'Old banks desc', 'auto')"
    )
    db.execute(
        "INSERT INTO folder_descriptions (folder_path, description, source) "
        "VALUES ('INBOX/Affairs/Stores', 'Old stores desc', 'auto')"
    )
    db.commit()

    emails = [_make_email("e1", "test@example.com", "Test")]

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = ["e1"]
    mock_jmap.get_emails.return_value = emails

    mock_tree = MagicMock()
    mock_tree.id_for.return_value = "mb-1"

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.return_value = "New description"

        report = regenerate_descriptions_for_folders(
            db, mock_jmap, mock_tree,
            ["INBOX/Affairs/Banks", "INBOX/Affairs/Stores"],
            anthropic_api_key="test-key",
        )

    assert report.succeeded == 2
    assert report.failed == 0
    assert report.skipped == 0


def test_regenerate_batch_skips_missing_folders(db: Database):
    """Batch regeneration should skip folders not in the mailbox tree."""
    mock_jmap = MagicMock()
    mock_tree = MagicMock()
    mock_tree.id_for.return_value = None  # folder not found

    report = regenerate_descriptions_for_folders(
        db, mock_jmap, mock_tree,
        ["INBOX/Nonexistent"],
        anthropic_api_key="test-key",
    )

    assert report.succeeded == 0
    assert report.skipped == 1
    assert report.results[0].skip_reason == "folder not found in mailbox tree"


def test_regenerate_batch_report_counts(db: Database):
    """Batch report should correctly count succeeded, failed, skipped."""
    emails = [_make_email("e1", "test@example.com", "Test")]

    mock_jmap = MagicMock()
    mock_jmap.query_folder_emails.return_value = ["e1"]
    mock_jmap.get_emails.return_value = emails

    mock_tree = MagicMock()
    # First folder found, second not
    mock_tree.id_for.side_effect = lambda p: "mb-1" if "Banks" in p else None

    with patch("mailsort.classifier.descriptions._generate_via_llm") as mock_llm:
        mock_llm.return_value = "New description"

        report = regenerate_descriptions_for_folders(
            db, mock_jmap, mock_tree,
            ["INBOX/Affairs/Banks", "INBOX/Nonexistent"],
            anthropic_api_key="test-key",
            folder_description_overrides={},
        )

    assert report.succeeded == 1
    assert report.skipped == 1
    assert len(report.results) == 2
