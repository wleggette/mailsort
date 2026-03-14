"""Tests for database migrations."""

from __future__ import annotations

from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations


def test_migrations_apply_once(db: Database):
    """Running migrations twice should be idempotent."""
    run_migrations(db)  # second call
    version = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 7


def test_all_tables_created(db: Database):
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {"schema_version", "rules", "runs", "audit_log", "contacts", "folder_descriptions", "inbox_snapshot", "learner_state"}
    assert expected.issubset(tables)


def test_rules_table_schema(db: Database):
    db.execute("""
        INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source)
        VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')
    """)
    db.commit()
    row = db.execute("SELECT * FROM rules WHERE condition_value = 'noreply@chase.com'").fetchone()
    assert row["rule_type"] == "exact_sender"
    assert row["confidence"] == 0.95
    assert row["active"] == 1


def test_rules_invalid_source_rejected(db: Database):
    import pytest
    with pytest.raises(Exception):
        db.execute("""
            INSERT INTO rules (rule_type, condition_value, target_folder_path, source)
            VALUES ('exact_sender', 'x@x.com', 'INBOX/Test', 'invalid_source')
        """)
        db.commit()


def test_audit_log_schema(db: Database):
    db.execute("""
        INSERT INTO audit_log
            (email_id, thread_id, from_address, target_folder, confidence,
             classification_source, moved)
        VALUES
            ('email-001', 'thread-001', 'noreply@chase.com',
             'INBOX/Affairs/Banks', 0.95, 'rule', 1)
    """)
    db.commit()
    row = db.execute("SELECT * FROM audit_log WHERE email_id = 'email-001'").fetchone()
    assert row["target_folder"] == "INBOX/Affairs/Banks"
    assert row["moved"] == 1
    assert row["skip_reason"] is None


def test_schema_version_tracked(db: Database):
    rows = db.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    versions = [r[0] for r in rows]
    assert versions == list(range(1, 8))
