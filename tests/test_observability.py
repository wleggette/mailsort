"""Tests for Phase 6: structured logging, export-rules, and threshold analysis."""

from __future__ import annotations

import json
import logging

from click.testing import CliRunner

from mailsort.config import Config, ClassificationConfig, FastmailConfig, SchedulerConfig, LoggingConfig
from mailsort.db.database import Database
from mailsort.db.migrations import run_migrations
from mailsort.main import _JSONFormatter, cli


# ------------------------------------------------------------------
# Structured JSON logging
# ------------------------------------------------------------------

def test_json_formatter_basic():
    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="mailsort.test", level=logging.INFO, pathname="", lineno=0,
        msg="Found %d emails", args=(5,), exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)
    assert data["level"] == "INFO"
    assert data["logger"] == "mailsort.test"
    assert data["message"] == "Found 5 emails"
    assert "timestamp" in data


def test_json_formatter_with_exception():
    formatter = _JSONFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="mailsort.test", level=logging.ERROR, pathname="", lineno=0,
            msg="Something failed", args=(), exc_info=sys.exc_info(),
        )
    output = formatter.format(record)
    data = json.loads(output)
    assert data["level"] == "ERROR"
    assert "exception" in data
    assert "ValueError" in data["exception"]


# ------------------------------------------------------------------
# Export rules
# ------------------------------------------------------------------

def test_export_rules_empty(db: Database, test_config: Config, tmp_path):
    """Export with no rules should produce an empty list."""
    config_path = tmp_path / "config.yaml"
    import yaml
    config_path.write_text(yaml.dump({
        "fastmail_api_token": "test-token",
        "db_path": str(db._path),
    }))

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "export-rules"])
    assert result.exit_code == 0
    assert "rules: []\n" in result.output or "rules:" in result.output


def test_export_rules_with_data(tmp_path):
    """Export should include seeded rules."""
    import yaml

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.connect()
    run_migrations(db)
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('exact_sender', 'noreply@chase.com', 'INBOX/Affairs/Banks', 0.95, 'bootstrap')"
    )
    db.execute(
        "INSERT INTO rules (rule_type, condition_value, target_folder_path, confidence, source) "
        "VALUES ('list_id', 'github.com', 'INBOX/Tech/GitHub', 0.95, 'auto')"
    )
    db.commit()
    db.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "fastmail_api_token": "test-token",
        "db_path": str(db_path),
    }))

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "export-rules"])
    assert result.exit_code == 0
    assert "noreply@chase.com" in result.output
    assert "github.com" in result.output
    assert "INBOX/Affairs/Banks" in result.output


# ------------------------------------------------------------------
# Threshold analysis
# ------------------------------------------------------------------

def test_analyze_no_data(db: Database, tmp_path):
    """Analyze with empty audit_log should report no data."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "fastmail_api_token": "test-token",
        "db_path": str(db._path),
    }))

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "analyze"])
    assert result.exit_code == 0
    assert "No audit data found" in result.output


def test_analyze_with_data(tmp_path):
    """Analyze with audit data should show classification sources and outcomes."""
    import yaml

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.connect()
    run_migrations(db)

    db.execute(
        "INSERT INTO runs (run_id, started_at, status) "
        "VALUES ('run-1', datetime('now'), 'completed')"
    )
    for i in range(5):
        db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, from_address, from_domain, target_folder, "
            " confidence, classification_source, moved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1", f"email-rule-{i}", "noreply@chase.com", "chase.com",
                "INBOX/Affairs/Banks", 0.95, "rule", True,
            ),
        )
    for i in range(3):
        db.execute(
            "INSERT INTO audit_log "
            "(run_id, email_id, from_address, from_domain, target_folder, "
            " confidence, classification_source, moved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1", f"email-llm-{i}", f"sender{i}@example.com", "example.com",
                "INBOX/Shopping/Orders", 0.85, "llm", True,
            ),
        )
    db.commit()
    db.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "fastmail_api_token": "test-token",
        "db_path": str(db_path),
    }))

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_path), "analyze", "--days", "30"])
    assert result.exit_code == 0
    assert "8 emails" in result.output  # 5 rule + 3 llm
    assert "rule" in result.output
    assert "llm" in result.output
    assert "Moved:" in result.output
