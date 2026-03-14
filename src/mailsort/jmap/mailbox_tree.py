"""Mailbox tree builder and path resolver.

Converts a flat list of JMAPMailbox objects (with parentId relationships) into
a bidirectional mapping between JMAP mailbox IDs and human-readable folder paths
like "INBOX/Affairs/Banks".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from mailsort.jmap.models import JMAPMailbox

logger = logging.getLogger(__name__)

# Fastmail system folder roles to skip during classification
_SYSTEM_ROLES = {"trash", "junk", "drafts", "sent", "templates", "archive"}


@dataclass
class MailboxTree:
    """Bidirectional path ↔ ID index built from a flat mailbox list."""

    _id_to_path: dict[str, str] = field(default_factory=dict)
    _path_to_id: dict[str, str] = field(default_factory=dict)
    _inbox_id: Optional[str] = field(default=None)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def path_for(self, mailbox_id: str) -> Optional[str]:
        """Return the folder path for a mailbox ID, or None if unknown."""
        return self._id_to_path.get(mailbox_id)

    def id_for(self, path: str) -> Optional[str]:
        """Return the mailbox ID for a folder path, or None if unknown."""
        return self._path_to_id.get(path)

    @property
    def inbox_id(self) -> str:
        if self._inbox_id is None:
            raise RuntimeError("No inbox mailbox found in tree")
        return self._inbox_id

    def all_folder_paths(self) -> set[str]:
        """All known folder paths (excludes inbox itself)."""
        return set(self._path_to_id.keys())

    def is_system_folder(self, mailbox_id: str) -> bool:
        return mailbox_id not in self._id_to_path

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, mailboxes: list[JMAPMailbox]) -> "MailboxTree":
        """Build the tree from a flat list of JMAPMailbox objects."""
        tree = cls()
        by_id = {m.id: m for m in mailboxes}

        for mailbox in mailboxes:
            if mailbox.role == "inbox":
                tree._inbox_id = mailbox.id
                # Don't add inbox itself to the path map — it's the source, not a target
                continue

            if mailbox.role in _SYSTEM_ROLES:
                continue

            path = _build_path(mailbox, by_id)
            if path:
                tree._id_to_path[mailbox.id] = path
                tree._path_to_id[path] = mailbox.id

        logger.info(
            "Mailbox tree built: %d folders, inbox_id=%s",
            len(tree._id_to_path),
            tree._inbox_id,
        )
        return tree


def _build_path(mailbox: JMAPMailbox, by_id: dict[str, JMAPMailbox]) -> Optional[str]:
    """Walk parentId chain to construct a full path like 'INBOX/Affairs/Banks'."""
    parts: list[str] = [mailbox.name]
    current = mailbox

    # Guard against cycles (shouldn't happen, but be safe)
    visited: set[str] = {mailbox.id}

    while current.parent_id is not None:
        parent = by_id.get(current.parent_id)
        if parent is None:
            logger.warning("Mailbox %r has unknown parentId %r", mailbox.id, current.parent_id)
            return None

        if parent.id in visited:
            logger.warning("Cycle detected in mailbox tree at %r", parent.id)
            return None

        visited.add(parent.id)

        if parent.role == "inbox":
            parts.append("INBOX")
            break
        parts.append(parent.name)
        current = parent

    parts.reverse()
    return "/".join(parts)
