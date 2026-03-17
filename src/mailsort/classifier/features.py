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
# Contact refresh from Fastmail
# ---------------------------------------------------------------------------

def refresh_contacts(
    db: Database,
    jmap_client: "JMAPClient",
    known_contact_overrides: dict | None = None,
) -> int:
    """Fetch contacts from Fastmail and update the contacts table.

    Per-contact isolation: one bad contact record doesn't prevent the rest.
    Returns count of contact emails imported.

    Args:
        db: Database connection.
        jmap_client: Authenticated JMAP client.
        known_contact_overrides: Config overrides with relationship hints.
    """
    try:
        raw_contacts = jmap_client.get_contacts()
    except Exception as e:
        logger.warning("Failed to fetch contacts from Fastmail: %s", e)
        return 0

    if not raw_contacts:
        logger.info("No contacts returned from Fastmail (scope may be unavailable)")
        return 0

    overrides = known_contact_overrides or {}
    count = 0
    seen_addresses: set[str] = set()

    for contact in raw_contacts:
        try:
            imported = _import_single_contact(db, contact, overrides, seen_addresses)
            count += imported
        except Exception:
            logger.debug("Failed to import contact uid=%s", contact.get("uid", "?"))

    # Insert override-only contacts not already imported from Fastmail
    for addr, override in overrides.items():
        addr_lower = addr.lower().strip()
        if addr_lower in seen_addresses:
            continue
        seen_addresses.add(addr_lower)
        relationship = override.relationship if hasattr(override, "relationship") else None
        try:
            db.execute(
                "INSERT OR REPLACE INTO contacts "
                "(email_address, display_name, relationship, fastmail_uid, refreshed_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (addr_lower, addr_lower.split("@")[0], relationship, None),
            )
            count += 1
        except Exception:
            logger.debug("Failed to insert override contact %s", addr_lower)

    # Remove contacts that no longer exist in Fastmail or overrides
    removed = 0
    if seen_addresses:
        try:
            placeholders = ",".join("?" for _ in seen_addresses)
            cursor = db.execute(
                f"DELETE FROM contacts WHERE email_address NOT IN ({placeholders})",
                tuple(seen_addresses),
            )
            removed = cursor.rowcount
        except Exception:
            logger.warning("Failed to clean up stale contacts")

    try:
        db.commit()
    except Exception:
        logger.warning("Failed to commit contacts batch")

    if removed:
        logger.info("Removed %d stale contact(s) no longer in Fastmail", removed)
    logger.info("Refreshed %d contact email(s) from Fastmail", count)
    return count


def _import_single_contact(
    db: Database,
    contact: dict,
    overrides: dict,
    seen_addresses: set[str] | None = None,
) -> int:
    """Parse and insert one ContactCard. Returns number of email addresses imported."""
    name_map = contact.get("name", {})
    display_name = ""
    if isinstance(name_map, dict):
        display_name = name_map.get("full", "")
        if not display_name:
            given = name_map.get("given", "")
            surname = name_map.get("surname", "")
            display_name = f"{given} {surname}".strip()
    else:
        display_name = str(name_map)
    if not display_name:
        display_name = "(unknown)"

    emails_map = contact.get("emails", {})
    if not isinstance(emails_map, dict):
        return 0

    imported = 0
    for _entry_id, entry in emails_map.items():
        addr = entry.get("address") or entry.get("value", "") if isinstance(entry, dict) else str(entry)
        if not addr:
            continue
        addr = addr.lower().strip()
        if seen_addresses is not None:
            seen_addresses.add(addr)

        override = overrides.get(addr)
        relationship = None
        if override and hasattr(override, "relationship"):
            relationship = override.relationship

        db.execute(
            "INSERT OR REPLACE INTO contacts "
            "(email_address, display_name, relationship, fastmail_uid, refreshed_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (addr, display_name, relationship, contact.get("uid")),
        )
        imported += 1

    return imported


def should_refresh_contacts(db: Database, refresh_hours: int = 24) -> bool:
    """Check if contacts should be refreshed based on configurable interval."""
    try:
        row = db.execute(
            "SELECT value FROM learner_state WHERE key = 'last_contacts_refresh'"
        ).fetchone()
        if not row:
            return True
        check = db.execute(
            "SELECT ? < datetime('now', ? || ' hours') AS due",
            (row["value"], f"-{refresh_hours}"),
        ).fetchone()
        return bool(check and check["due"])
    except Exception:
        return True


def mark_contacts_refreshed(db: Database) -> None:
    """Record that contacts were just refreshed."""
    try:
        db.execute(
            "INSERT OR REPLACE INTO learner_state (key, value) "
            "VALUES ('last_contacts_refresh', datetime('now'))"
        )
        db.commit()
    except Exception:
        logger.warning("Failed to mark contacts refresh time")


# Type import for the JMAP client (avoid circular import at module level)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from mailsort.jmap.client import JMAPClient


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
