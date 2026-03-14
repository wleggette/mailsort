"""JMAP HTTP client: session discovery, auth, and method calls."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

from mailsort.jmap.models import JMAPEmail, JMAPMailbox, JMAPSession

logger = logging.getLogger(__name__)

JMAP_MAIL_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
]

EMAIL_PROPERTIES = [
    "id",
    "threadId",
    "mailboxIds",
    "from",
    "to",
    "subject",
    "receivedAt",
    "keywords",
    "preview",
    "header:list-id:asText",
    "header:list-unsubscribe:asText",
]

# Fallback for read-only tokens that reject header:* properties
_EMAIL_PROPERTIES_MINIMAL = [
    "id",
    "threadId",
    "mailboxIds",
    "from",
    "to",
    "subject",
    "receivedAt",
    "keywords",
    "preview",
]


class JMAPError(Exception):
    """Raised when a JMAP method call returns an error response."""

    def __init__(self, method: str, error_type: str, description: str = ""):
        self.method = method
        self.error_type = error_type
        self.description = description
        super().__init__(f"JMAP error in {method}: {error_type} — {description}")


class ReadOnlyTokenError(Exception):
    """Raised when a write operation is attempted with a read-only API token."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(
            f"Cannot {operation}: Fastmail API token is read-only. "
            f"Generate a read/write token at Settings → Privacy & Security → Manage API tokens."
        )


class JMAPClient:
    """Thin JMAP client for Fastmail.

    Handles:
    - Session discovery and caching
    - Bearer token auth
    - Method call batching (POST to apiUrl)
    - Mailbox/get, Email/query, Email/get, Thread/get
    """

    def __init__(self, token: str, session_url: str):
        self._token = token
        self._session_url = session_url
        self._session: Optional[JMAPSession] = None
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def get_session(self) -> JMAPSession:
        """Fetch and cache the JMAP session. Returns cached value on repeat calls."""
        if self._session is not None:
            return self._session

        logger.debug("Fetching JMAP session from %s", self._session_url)
        response = self._http.get(self._session_url)
        response.raise_for_status()
        self._session = JMAPSession.from_response(response.json())
        logger.info(
            "JMAP session established — account_id=%s capabilities=%d",
            self._session.account_id,
            len(self._session.capabilities),
        )
        return self._session

    def invalidate_session(self) -> None:
        """Force session re-fetch on next call."""
        self._session = None

    @property
    def session_capabilities(self) -> set[str]:
        return self.get_session().capabilities

    @property
    def is_read_only(self) -> bool:
        return self.get_session().is_read_only

    # ------------------------------------------------------------------
    # Raw method calls
    # ------------------------------------------------------------------

    def call(
        self,
        method_calls: list[list],
        using: list[str] | None = None,
    ) -> dict:
        """POST one or more JMAP method calls and return the full response dict.

        Args:
            method_calls: List of [method_name, args_dict, call_id] triples.
            using: JMAP capability URNs. Defaults to core + mail.

        Raises:
            JMAPError: If any method response is an error.
            httpx.HTTPStatusError: On HTTP-level failures.
        """
        session = self.get_session()
        payload = {
            "using": using or JMAP_MAIL_USING,
            "methodCalls": method_calls,
        }
        logger.debug("JMAP call: %s", [m[0] for m in method_calls])

        response = self._http.post(session.api_url, json=payload)
        response.raise_for_status()
        data = response.json()

        # Surface method-level errors
        for method_name, result, call_id in data.get("methodResponses", []):
            if method_name == "error":
                raise JMAPError(
                    method=call_id,
                    error_type=result.get("type", "unknown"),
                    description=result.get("description", ""),
                )

        return data

    # ------------------------------------------------------------------
    # Mailbox operations
    # ------------------------------------------------------------------

    def get_all_mailboxes(self) -> list[JMAPMailbox]:
        """Fetch the full mailbox tree."""
        session = self.get_session()
        data = self.call([
            ["Mailbox/get", {
                "accountId": session.account_id,
                "properties": ["id", "name", "parentId", "role", "totalEmails", "unreadEmails"],
            }, "mb"],
        ])
        raw_list = data["methodResponses"][0][1].get("list", [])
        return [JMAPMailbox.model_validate(m) for m in raw_list]

    # ------------------------------------------------------------------
    # Email operations
    # ------------------------------------------------------------------

    def query_inbox_emails(
        self,
        inbox_id: str,
        limit: int = 100,
        *,
        filter_eligible: bool = False,
    ) -> list[str]:
        """Return IDs of inbox emails.

        Args:
            filter_eligible: If True, apply read/unflagged filters (no age
                filter — age is checked in Python after classification).
                If False (default), return ALL inbox emails.
        """
        session = self.get_session()

        query_filter: dict = {"inMailbox": inbox_id}
        if filter_eligible:
            query_filter["hasKeyword"] = "$seen"
            query_filter["notKeyword"] = "$flagged"

        data = self.call([
            ["Email/query", {
                "accountId": session.account_id,
                "filter": query_filter,
                "sort": [{"property": "receivedAt", "isAscending": False}],
                "limit": limit,
            }, "q1"],
        ])
        return data["methodResponses"][0][1].get("ids", [])

    def query_folder_emails(
        self,
        mailbox_id: str,
        limit: int = 50,
    ) -> list[str]:
        """Return IDs of recent emails in any folder (used by bootstrap)."""
        session = self.get_session()
        data = self.call([
            ["Email/query", {
                "accountId": session.account_id,
                "filter": {"inMailbox": mailbox_id},
                "sort": [{"property": "receivedAt", "isAscending": False}],
                "limit": limit,
            }, "q1"],
        ])
        return data["methodResponses"][0][1].get("ids", [])

    def get_emails(
        self,
        email_ids: list[str],
        properties: Optional[list[str]] = None,
    ) -> list[JMAPEmail]:
        """Fetch full email objects for the given IDs.

        If the full property set fails (e.g., read-only token rejecting
        header:* properties), automatically retries with minimal properties.
        """
        if not email_ids:
            return []

        session = self.get_session()
        props = properties or EMAIL_PROPERTIES

        try:
            data = self.call([
                ["Email/get", {
                    "accountId": session.account_id,
                    "ids": email_ids,
                    "properties": props,
                }, "g1"],
            ])
        except JMAPError:
            if properties is not None:
                raise  # caller specified explicit properties, don't override
            logger.debug("Email/get failed with full properties, retrying with minimal")
            data = self.call([
                ["Email/get", {
                    "accountId": session.account_id,
                    "ids": email_ids,
                    "properties": _EMAIL_PROPERTIES_MINIMAL,
                }, "g1"],
            ])

        raw_list = data["methodResponses"][0][1].get("list", [])
        return [JMAPEmail.model_validate(e) for e in raw_list]

    def get_thread_email_ids(self, thread_id: str) -> list[str]:
        """Return all email IDs belonging to a thread."""
        session = self.get_session()
        data = self.call([
            ["Thread/get", {
                "accountId": session.account_id,
                "ids": [thread_id],
            }, "t1"],
        ])
        threads = data["methodResponses"][0][1].get("list", [])
        if not threads:
            return []
        return threads[0].get("emailIds", [])

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def get_contacts(self) -> list[dict]:
        """Fetch all contacts from Fastmail via ContactCard/get.

        Returns a list of dicts with 'name' and 'emails' keys.
        Returns empty list if contacts scope is not available.
        """
        if "urn:ietf:params:jmap:contacts" not in self.session_capabilities:
            logger.warning(
                "Contacts scope not available — contact enrichment disabled. "
                "Grant the contacts scope to enable."
            )
            return []

        session = self.get_session()
        data = self.call(
            [
                ["ContactCard/get", {
                    "accountId": session.account_id,
                    "properties": ["uid", "name", "emails"],
                }, "c1"],
            ],
            using=JMAP_MAIL_USING + ["urn:ietf:params:jmap:contacts"],
        )
        return data["methodResponses"][0][1].get("list", [])

    def move_emails(
        self,
        moves: list[tuple[str, str, dict[str, bool]]],
        inbox_id: str,
        *,
        tag_keyword: str | None = "$mailsort-moved",
    ) -> dict[str, bool]:
        """Move emails to target folders in a single Email/set call.

        Preserves existing mailbox memberships: removes inbox, adds target,
        keeps everything else. Optionally tags each moved email with a keyword
        (uses JMAP patch path so existing keywords are not overwritten).

        Args:
            moves: List of (email_id, target_folder_id, current_mailbox_ids) triples.
            inbox_id: The JMAP mailbox ID of the inbox (to remove).
            tag_keyword: Keyword to add to moved emails, or None to skip.

        Returns:
            Dict mapping email_id → True (moved) or False (failed).
        """
        if not moves:
            return {}

        if self.is_read_only:
            raise ReadOnlyTokenError("move emails")

        session = self.get_session()
        updates: dict[str, dict] = {}
        for email_id, folder_id, current_mailbox_ids in moves:
            new_mailbox_ids = dict(current_mailbox_ids)
            new_mailbox_ids.pop(inbox_id, None)
            new_mailbox_ids[folder_id] = True
            update: dict = {"mailboxIds": new_mailbox_ids}
            if tag_keyword:
                update[f"keywords/{tag_keyword}"] = True
            updates[email_id] = update

        data = self.call([
            ["Email/set", {
                "accountId": session.account_id,
                "update": updates,
            }, "s1"],
        ])
        result = data["methodResponses"][0][1]
        updated = set(result.get("updated", {}).keys())
        not_updated = set(result.get("notUpdated", {}).keys())

        outcomes: dict[str, bool] = {}
        for email_id, _, _ in moves:
            if email_id in updated:
                outcomes[email_id] = True
            elif email_id in not_updated:
                err = result["notUpdated"][email_id]
                logger.error("Failed to move %s: %s", email_id, err)
                outcomes[email_id] = False
            else:
                logger.warning("Email %s absent from Email/set response", email_id)
                outcomes[email_id] = False

        return outcomes

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JMAPClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
