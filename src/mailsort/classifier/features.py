"""Feature extraction and contact enrichment for email classification."""

from __future__ import annotations

import logging
import re
from typing import Optional

from mailsort.db.database import Database
from mailsort.jmap.models import EmailFeatures, JMAPEmail

logger = logging.getLogger(__name__)


def extract_features(email: JMAPEmail) -> EmailFeatures:
    """Build an EmailFeatures from a raw JMAP email object."""
    return EmailFeatures.from_jmap_email(email)


# ---------------------------------------------------------------------------
# Contact enrichment
# ---------------------------------------------------------------------------

class ContactInfo:
    """Lightweight contact record for prompt enrichment and threshold gating."""

    __slots__ = ("email_address", "display_name", "relationship")

    def __init__(self, email_address: str, display_name: str, relationship: str | None = None):
        self.email_address = email_address
        self.display_name = display_name
        self.relationship = relationship

    def label(self) -> str:
        """Format a concise label for LLM prompt injection."""
        parts = [self.display_name]
        if self.relationship:
            parts.append(f"({self.relationship})")
        return " ".join(parts)


def load_contacts(db: Database) -> dict[str, ContactInfo]:
    """Load the contacts cache into memory.

    Returns a dict mapping email address → ContactInfo.
    """
    contacts: dict[str, ContactInfo] = {}
    rows = db.execute("SELECT email_address, display_name, relationship FROM contacts").fetchall()
    for row in rows:
        contacts[row["email_address"]] = ContactInfo(
            email_address=row["email_address"],
            display_name=row["display_name"],
            relationship=row["relationship"],
        )
    return contacts


def get_contact_for_sender(
    features: EmailFeatures,
    contacts: dict[str, ContactInfo],
) -> Optional[ContactInfo]:
    """Look up the sender in the contacts cache."""
    return contacts.get(features.from_address)


# ---------------------------------------------------------------------------
# Preview redaction
# ---------------------------------------------------------------------------

def redact_preview(preview: str, patterns: list[str]) -> str:
    """Redact sensitive patterns from the preview before sending to the LLM."""
    result = preview
    for pattern in patterns:
        try:
            result = re.sub(pattern, "[REDACTED]", result)
        except re.error:
            logger.warning("Invalid redaction pattern: %s", pattern)
    return result
