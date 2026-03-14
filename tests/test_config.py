"""Tests for config loading and validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mailsort.config import Config, load_config


def test_load_config_from_yaml(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
fastmail:
  api_url: "https://api.fastmail.com/jmap/api/"
  session_url: "https://api.fastmail.com/jmap/session"
scheduler:
  interval_minutes: 10
  min_age_hours: 2
classification:
  thresholds:
    rule_move: 0.90
""")
    cfg = load_config(config_file, require_secrets=False)
    assert cfg.scheduler.interval_minutes == 10
    assert cfg.scheduler.min_age_hours == 2
    assert cfg.classification.thresholds.rule_move == 0.90


def test_config_defaults():
    cfg = Config(fastmail_api_token="dummy-token")
    assert cfg.scheduler.interval_minutes == 15
    assert cfg.scheduler.min_age_hours == 4
    assert cfg.classification.llm_model == "claude-haiku-4-5-20251001"
    assert cfg.classification.thresholds.llm_move_known_contact == 0.93
    assert cfg.skip_senders == []


def test_config_requires_fastmail_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scheduler:\n  interval_minutes: 15\n")
    with pytest.raises(Exception, match="FASTMAIL_API_TOKEN"):
        load_config(config_file, require_secrets=True)


def test_config_loads_token_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FASTMAIL_API_TOKEN", "env-token-xyz")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("scheduler:\n  interval_minutes: 15\n")
    cfg = load_config(config_file)
    assert cfg.fastmail_api_token == "env-token-xyz"


def test_config_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")
