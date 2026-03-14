"""Configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class FastmailConfig(BaseModel):
    api_url: str = "https://api.fastmail.com/jmap/api/"
    session_url: str = "https://api.fastmail.com/jmap/session"


class SchedulerConfig(BaseModel):
    interval_minutes: int = 15
    min_age_hours: int = 4
    max_batch_size: int = 100
    health_check_port: int = 8025
    contacts_refresh_hours: int = 24


class ThresholdsConfig(BaseModel):
    rule_move: float = 0.85
    llm_move: float = 0.80
    llm_move_known_contact: float = 0.93
    rule_learn: float = 0.70


class AutoRuleThresholdsConfig(BaseModel):
    list_id: int = 2
    exact_sender: int = 3
    sender_domain: int = 5


class ClassificationConfig(BaseModel):
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    auto_rule_thresholds: AutoRuleThresholdsConfig = Field(default_factory=AutoRuleThresholdsConfig)
    auto_rule_domain_coherence: float = 0.80
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_max_preview_chars: int = 500
    llm_use_preview: bool = True
    llm_allow_known_contacts: bool = True
    llm_redact_patterns: list[str] = Field(default_factory=list)
    llm_suggest_rule_after_n: int = 5
    llm_skip_senders: list[str] = Field(default_factory=list)
    llm_skip_domains: list[str] = Field(default_factory=list)
    learner_lookback_days: int = 7


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/mailsort.log"
    max_size_mb: int = 10
    backup_count: int = 3
    format: str = "text"  # "text" or "json"


class ManualRule(BaseModel):
    type: str
    value: str
    folder: str
    confidence: float = 1.0


class KnownContactOverride(BaseModel):
    relationship: Optional[str] = None


class Config(BaseModel):
    fastmail: FastmailConfig = Field(default_factory=FastmailConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    folder_description_overrides: dict[str, str] = Field(default_factory=dict)
    manual_rules: list[ManualRule] = Field(default_factory=list)
    skip_senders: list[str] = Field(default_factory=list)
    exclude_folder_patterns: list[str] = Field(default_factory=list)
    known_contact_overrides: dict[str, KnownContactOverride] = Field(default_factory=dict)
    logging_config: LoggingConfig = Field(default_factory=LoggingConfig)

    # Secrets — loaded from environment, not config.yaml
    fastmail_api_token: str = Field(default="")
    anthropic_api_key: str = Field(default="")

    # Runtime: path to the SQLite database
    db_path: str = Field(default="data/mailsort.db")

    @model_validator(mode="after")
    def load_secrets_from_env(self) -> "Config":
        if not self.fastmail_api_token:
            token = os.environ.get("FASTMAIL_API_TOKEN", "")
            object.__setattr__(self, "fastmail_api_token", token)
        if not self.anthropic_api_key:
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            object.__setattr__(self, "anthropic_api_key", key)
        return self

    @model_validator(mode="after")
    def validate_required_secrets(self) -> "Config":
        if not self.fastmail_api_token:
            raise ValueError(
                "FASTMAIL_API_TOKEN is required. "
                "Set it as an environment variable or in a .env file."
            )
        return self


def load_config(
    config_path: Path | str = "config.yaml",
    *,
    require_secrets: bool = True,
) -> Config:
    """Load configuration from a YAML file, merging with environment variables.

    Args:
        config_path: Path to the config.yaml file.
        require_secrets: If False, skip validation of required secrets
                         (useful for tests).
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    if not require_secrets:
        # Provide dummy values so validation passes in tests
        data.setdefault("fastmail_api_token", "test-token")

    return Config.model_validate(data)
