"""Tests for folder description loading, path normalisation, and filtering."""

from __future__ import annotations

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.orchestrator import _load_folder_descriptions, _normalise_folder_path


def _make_config(**overrides) -> Config:
    defaults = dict(
        fastmail=FastmailConfig(),
        scheduler=SchedulerConfig(),
        classification=ClassificationConfig(),
        fastmail_api_token="test-token",
        anthropic_api_key="",
        db_path=":memory:",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_db() -> Database:
    db = Database(":memory:")
    db.connect()
    run_migrations(db)
    return db


# ------------------------------------------------------------------
# _normalise_folder_path
# ------------------------------------------------------------------

class TestNormaliseFolderPath:
    valid = {"INBOX/Affairs/Banks", "INBOX/Affairs/Stores", "INBOX/People/Children"}

    def test_exact_match(self):
        assert _normalise_folder_path("INBOX/Affairs/Banks", self.valid) == "INBOX/Affairs/Banks"

    def test_adds_inbox_prefix(self):
        assert _normalise_folder_path("Affairs/Banks", self.valid) == "INBOX/Affairs/Banks"

    def test_nonexistent_returns_none(self):
        assert _normalise_folder_path("Affairs/Medical", self.valid) is None

    def test_empty_path_returns_none(self):
        assert _normalise_folder_path("", self.valid) is None

    def test_bare_name_no_match(self):
        assert _normalise_folder_path("Banks", self.valid) is None


# ------------------------------------------------------------------
# _load_folder_descriptions — filtering
# ------------------------------------------------------------------

class TestLoadFolderDescriptions:
    def test_db_descriptions_filtered_to_valid_paths(self):
        db = _make_db()
        db.execute(
            "INSERT INTO folder_descriptions (folder_path, description, source) "
            "VALUES (?, ?, ?)", ("INBOX/Affairs/Banks", "Bank emails", "auto"),
        )
        db.execute(
            "INSERT INTO folder_descriptions (folder_path, description, source) "
            "VALUES (?, ?, ?)", ("INBOX/Affairs", "Parent folder", "auto"),
        )
        db.commit()

        cfg = _make_config()
        # Only Banks is valid (Affairs excluded from tree)
        valid = {"INBOX/Affairs/Banks", "INBOX/Affairs/Stores"}
        result = _load_folder_descriptions(cfg, db, valid)

        assert "INBOX/Affairs/Banks: Bank emails" in result
        assert "Parent folder" not in result

    def test_config_overrides_normalised_to_inbox_prefix(self):
        db = _make_db()
        cfg = _make_config(folder_description_overrides={
            "Affairs/Banks": "Overridden bank description",
        })
        valid = {"INBOX/Affairs/Banks", "INBOX/Affairs/Stores"}
        result = _load_folder_descriptions(cfg, db, valid)

        assert "INBOX/Affairs/Banks: Overridden bank description" in result

    def test_config_overrides_for_nonexistent_folders_dropped(self):
        db = _make_db()
        cfg = _make_config(folder_description_overrides={
            "Affairs/Banks": "Valid override",
            "Affairs/Legal": "No such folder",
            "Nonexistent": "Also no such folder",
        })
        valid = {"INBOX/Affairs/Banks", "INBOX/Affairs/Stores"}
        result = _load_folder_descriptions(cfg, db, valid)

        assert "INBOX/Affairs/Banks" in result
        assert "Legal" not in result
        assert "Nonexistent" not in result

    def test_excluded_parent_descriptions_dropped(self):
        """Descriptions for folders excluded via exclude_folder_patterns
        (and therefore not in valid_paths) are not passed to the LLM."""
        db = _make_db()
        db.execute(
            "INSERT INTO folder_descriptions (folder_path, description, source) "
            "VALUES (?, ?, ?)", ("INBOX/Affairs/Banks", "Bank emails", "auto"),
        )
        db.commit()

        cfg = _make_config(folder_description_overrides={
            "Affairs": "Parent only — do NOT sort here",
            "People": "Parent only — do NOT sort here",
            "People/Children": "School stuff",
        })
        # Parents excluded from tree, children remain
        valid = {"INBOX/Affairs/Banks", "INBOX/People/Children"}
        result = _load_folder_descriptions(cfg, db, valid)

        assert "INBOX/Affairs/Banks" in result
        assert "INBOX/People/Children" in result
        assert "do NOT sort here" not in result

    def test_config_override_wins_over_db(self):
        db = _make_db()
        db.execute(
            "INSERT INTO folder_descriptions (folder_path, description, source) "
            "VALUES (?, ?, ?)", ("INBOX/Affairs/Banks", "Auto-generated", "auto"),
        )
        db.commit()

        cfg = _make_config(folder_description_overrides={
            "Affairs/Banks": "Manual override wins",
        })
        valid = {"INBOX/Affairs/Banks"}
        result = _load_folder_descriptions(cfg, db, valid)

        assert "Manual override wins" in result
        assert "Auto-generated" not in result

    def test_empty_valid_paths_returns_placeholder(self):
        db = _make_db()
        cfg = _make_config()
        result = _load_folder_descriptions(cfg, db, set())
        assert result == "(no folder descriptions available)"
