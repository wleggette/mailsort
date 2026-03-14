"""Pydantic models for JMAP objects and internal data structures."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# JMAP wire models
# ---------------------------------------------------------------------------

class EmailAddress(BaseModel):
    name: Optional[str] = None
    email: str


class JMAPMailbox(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = Field(None, alias="parentId")
    role: Optional[str] = None
    total_emails: int = Field(0, alias="totalEmails")
    unread_emails: int = Field(0, alias="unreadEmails")

    model_config = {"populate_by_name": True}


class JMAPEmail(BaseModel):
    id: str
    thread_id: str = Field(alias="threadId")
    mailbox_ids: dict[str, bool] = Field(alias="mailboxIds")
    from_addresses: Optional[list[EmailAddress]] = Field(None, alias="from")
    to_addresses: Optional[list[EmailAddress]] = Field(None, alias="to")
    subject: str = ""
    received_at: str = Field("", alias="receivedAt")
    keywords: dict[str, bool] = Field(default_factory=dict)
    preview: str = ""
    list_id: Optional[str] = Field(None, alias="header:list-id:asText")
    list_unsubscribe: Optional[str] = Field(None, alias="header:list-unsubscribe:asText")

    model_config = {"populate_by_name": True}

    @property
    def from_address(self) -> str:
        if self.from_addresses:
            return self.from_addresses[0].email
        return ""

    @property
    def from_domain(self) -> str:
        addr = self.from_address
        if "@" in addr:
            return addr.split("@", 1)[1].lower()
        return ""

    @property
    def received_at_dt(self) -> Optional[datetime]:
        if not self.received_at:
            return None
        return datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))


class JMAPSession(BaseModel):
    """Parsed JMAP session response."""
    account_id: str
    api_url: str
    capabilities: set[str]
    account_capabilities: dict[str, dict] = Field(default_factory=dict)
    is_read_only: bool = False

    @classmethod
    def from_response(cls, data: dict) -> "JMAPSession":
        # Primary mail account ID
        mail_cap = "urn:ietf:params:jmap:mail"
        primary_accounts = data.get("primaryAccounts", {})
        account_id = primary_accounts.get(mail_cap) or next(iter(data.get("accounts", {})), "")

        # Parse account-level capabilities and read-only status
        accounts = data.get("accounts", {})
        account_data = accounts.get(account_id, {})
        account_caps = account_data.get("accountCapabilities", {})
        is_read_only = account_data.get("isReadOnly", False)

        # Fastmail also indicates per-capability read-only in some cases
        mail_caps = account_caps.get(mail_cap, {})
        if mail_caps.get("isReadOnly", False):
            is_read_only = True

        return cls(
            account_id=account_id,
            api_url=data["apiUrl"],
            capabilities=set(data.get("capabilities", {}).keys()),
            account_capabilities=account_caps,
            is_read_only=is_read_only,
        )


# ---------------------------------------------------------------------------
# Internal pipeline models (Section 12 of architecture)
# ---------------------------------------------------------------------------

class EmailFeatures(BaseModel):
    """Extracted features from a JMAP email for classification."""
    email_id: str
    thread_id: str
    from_address: str
    from_domain: str
    to_addresses: list[str]
    subject: str
    list_id: Optional[str] = None
    list_unsubscribe: Optional[str] = None
    received_at: datetime
    preview: str
    keywords: list[str]
    current_mailbox_ids: dict[str, bool]

    @classmethod
    def from_jmap_email(cls, email: JMAPEmail) -> "EmailFeatures":
        return cls(
            email_id=email.id,
            thread_id=email.thread_id,
            from_address=email.from_address,
            from_domain=email.from_domain,
            to_addresses=[a.email for a in (email.to_addresses or [])],
            subject=email.subject,
            list_id=email.list_id,
            list_unsubscribe=email.list_unsubscribe,
            received_at=email.received_at_dt or datetime.utcnow(),
            preview=email.preview,
            keywords=list(email.keywords.keys()),
            current_mailbox_ids=email.mailbox_ids,
        )


class Classification(BaseModel):
    """Result of classifying an email."""
    folder_path: str
    folder_id: Optional[str] = None
    confidence: float
    source: str  # "thread" | "rule" | "llm" | "manual"
    rule_id: Optional[int] = None
    reasoning: Optional[str] = None


class MoveDecision(BaseModel):
    """Final decision on whether and where to move an email."""
    email_id: str
    features: EmailFeatures
    classification: Classification
    should_move: bool
    skip_reason: Optional[str] = None
