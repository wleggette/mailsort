"""JMAP email loader: injects fixture emails into a Fastmail test account.

Uses JMAP blob upload + Email/import to create real emails in target folders.
Idempotent: skips emails whose subject already exists in the target folder.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

JMAP_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
]

JMAP_USING_CONTACTS = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:contacts",
]

# Test contacts to auto-populate in the Fastmail test account.
# These must exist as ContactCard entries for CI1 (contacts fetched from Fastmail)
# and for known-contact threshold testing (S7, S8, C1).
TEST_CONTACTS = [
    {"name": "Test Contact", "email": "testcontact@example.com"},
    {"name": "Test Friend", "email": "testfriend@gmail.com"},
]


class JMAPLoader:
    """Lightweight JMAP client for loading test emails."""

    def __init__(self, token: str, session_url: str):
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        self._session_url = session_url
        self._session: dict | None = None

    def _get_session(self) -> dict:
        if self._session:
            return self._session
        resp = self._http.get(self._session_url)
        resp.raise_for_status()
        self._session = resp.json()
        return self._session

    @property
    def account_id(self) -> str:
        session = self._get_session()
        return list(session["accounts"].keys())[0]

    @property
    def api_url(self) -> str:
        return self._get_session()["apiUrl"]

    @property
    def account_email(self) -> str:
        """Derive the account email from the JMAP session.

        Fastmail exposes the account email as the 'name' field in the accounts map.
        """
        session = self._get_session()
        account_data = session["accounts"].get(self.account_id, {})
        name = account_data.get("name", "")
        if "@" not in name:
            raise RuntimeError(
                f"Could not derive email from JMAP session (account name={name!r}). "
                f"Pass --to-email explicitly."
            )
        return name

    @property
    def upload_url(self) -> str:
        return self._get_session()["uploadUrl"].replace("{accountId}", self.account_id)

    def call(self, method_calls: list) -> dict:
        payload = {"using": JMAP_USING, "methodCalls": method_calls}
        resp = self._http.post(self.api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        for method_name, result, call_id in data.get("methodResponses", []):
            if method_name == "error":
                raise RuntimeError(f"JMAP error in {call_id}: {result}")
        return data

    def get_mailboxes(self) -> dict[str, dict]:
        """Return {mailbox_id: {name, parentId, role}} for all mailboxes."""
        data = self.call([
            ["Mailbox/get", {"accountId": self.account_id}, "mb"],
        ])
        return {
            m["id"]: m
            for m in data["methodResponses"][0][1]["list"]
        }

    def resolve_folder_paths(self) -> dict[str, str]:
        """Return {folder_path: mailbox_id} mapping."""
        mailboxes = self.get_mailboxes()
        id_to_name = {m_id: m["name"] for m_id, m in mailboxes.items()}
        id_to_parent = {m_id: m.get("parentId") for m_id, m in mailboxes.items()}

        def build_path(m_id: str) -> str:
            parts = []
            current = m_id
            while current:
                parts.append(id_to_name[current])
                current = id_to_parent.get(current)
            parts.reverse()
            return "/".join(parts)

        return {build_path(m_id): m_id for m_id in mailboxes}

    def upload_blob(self, data: bytes) -> str:
        """Upload a blob and return its blobId."""
        resp = self._http.post(
            self.upload_url,
            content=data,
            headers={
                "Authorization": f"Bearer {self._http.headers['Authorization'].split()[-1]}",
                "Content-Type": "message/rfc822",
            },
        )
        resp.raise_for_status()
        return resp.json()["blobId"]

    def import_email(
        self,
        blob_id: str,
        mailbox_id: str,
        keywords: dict[str, bool] | None = None,
        received_at: str | None = None,
    ) -> str | None:
        """Import an email blob into a mailbox. Returns email ID or None on failure."""
        email_data: dict = {
            "blobId": blob_id,
            "mailboxIds": {mailbox_id: True},
            "keywords": keywords or {"$seen": True},
        }
        if received_at:
            email_data["receivedAt"] = received_at

        data = self.call([
            ["Email/import", {
                "accountId": self.account_id,
                "emails": {"e1": email_data},
            }, "imp"],
        ])
        result = data["methodResponses"][0][1]
        created = result.get("created", {})
        if "e1" in created:
            return created["e1"]["id"]
        not_created = result.get("notCreated", {})
        if "e1" in not_created:
            logger.error("Failed to import email: %s", not_created["e1"])
        return None

    def query_folder_subjects(self, mailbox_id: str, limit: int = 200) -> set[str]:
        """Return set of subjects already in a folder (for dedup)."""
        data = self.call([
            ["Email/query", {
                "accountId": self.account_id,
                "filter": {"inMailbox": mailbox_id},
                "limit": limit,
            }, "q1"],
        ])
        email_ids = data["methodResponses"][0][1].get("ids", [])
        if not email_ids:
            return set()

        data = self.call([
            ["Email/get", {
                "accountId": self.account_id,
                "ids": email_ids,
                "properties": ["subject"],
            }, "g1"],
        ])
        return {e["subject"] for e in data["methodResponses"][0][1].get("list", [])}

    def create_mailbox(self, name: str, parent_id: str | None = None) -> str:
        """Create a mailbox and return its ID."""
        create_data: dict = {"name": name}
        if parent_id:
            create_data["parentId"] = parent_id

        data = self.call([
            ["Mailbox/set", {
                "accountId": self.account_id,
                "create": {"mb1": create_data},
            }, "mc"],
        ])
        result = data["methodResponses"][0][1]
        created = result.get("created") or {}
        if "mb1" in created:
            return created["mb1"]["id"]
        not_created = result.get("notCreated") or {}
        if "mb1" in not_created:
            raise RuntimeError(f"Failed to create mailbox '{name}': {not_created['mb1']}")
        raise RuntimeError(f"Unexpected response creating mailbox '{name}': {result}")

    def ensure_folder_path(self, path: str) -> str:
        """Ensure a folder path exists, creating intermediate folders as needed.

        Returns the mailbox ID of the leaf folder.
        Always fetches a fresh folder map from JMAP to avoid conflicts with
        folders created by previous (possibly failed) runs.
        """
        # Always get fresh state from JMAP
        folder_map = self.resolve_folder_paths()
        logger.debug("Current folder map: %s", sorted(folder_map.keys()))

        # Find inbox name and ID (Fastmail uses "Inbox" not "INBOX")
        inbox_name = None
        inbox_id = None
        for fpath, mid in folder_map.items():
            if fpath.upper() == "INBOX":
                inbox_name = fpath
                inbox_id = mid
                break
        if not inbox_id:
            mailboxes = self.get_mailboxes()
            for mid, m in mailboxes.items():
                if m.get("role") == "inbox":
                    inbox_id = mid
                    inbox_name = m["name"]
                    break
        inbox_name = inbox_name or "Inbox"

        # Check if it already exists (try with and without inbox prefix)
        if path in folder_map:
            return folder_map[path]
        inbox_path = f"{inbox_name}/{path}"
        if inbox_path in folder_map:
            return folder_map[inbox_path]

        # Build path parts and create missing folders
        parts = path.split("/")
        parent_id = inbox_id
        current_path = inbox_name

        for part in parts:
            current_path = f"{current_path}/{part}"
            if current_path in folder_map:
                parent_id = folder_map[current_path]
            else:
                logger.info("Creating folder: %s", current_path)
                parent_id = self.create_mailbox(part, parent_id)
                folder_map[current_path] = parent_id

        return parent_id

    def delete_emails_with_keyword(self, keyword: str) -> int:
        """Delete all emails with a specific keyword. Returns count deleted."""
        data = self.call([
            ["Email/query", {
                "accountId": self.account_id,
                "filter": {"hasKeyword": keyword},
                "limit": 500,
            }, "q1"],
        ])
        email_ids = data["methodResponses"][0][1].get("ids", [])
        if not email_ids:
            return 0

        destroy_ids = email_ids
        data = self.call([
            ["Email/set", {
                "accountId": self.account_id,
                "destroy": destroy_ids,
            }, "d1"],
        ])
        destroyed = data["methodResponses"][0][1].get("destroyed", [])
        return len(destroyed)

    def create_contacts(self, contacts: list[dict]) -> int:
        """Create test ContactCard entries via JMAP. Returns count created.

        Each contact dict has 'name' and 'email' keys.
        Idempotent: skips contacts whose email already exists.
        """
        # Check if contacts scope is available
        session = self._get_session()
        capabilities = set(session.get("capabilities", {}).keys())
        if "urn:ietf:params:jmap:contacts" not in capabilities:
            logger.warning("Contacts scope not available — skipping contact creation")
            return 0

        # Fetch existing contacts to avoid duplicates
        payload = {
            "using": JMAP_USING_CONTACTS,
            "methodCalls": [
                ["ContactCard/get", {"accountId": self.account_id}, "c1"],
            ],
        }
        resp = self._http.post(self.api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        existing_emails: set[str] = set()
        for card in data["methodResponses"][0][1].get("list", []):
            emails_map = card.get("emails") or {}
            for entry in emails_map.values():
                addr = entry.get("value") or entry.get("address") or ""
                if addr:
                    existing_emails.add(addr.lower())

        created = 0
        for contact in contacts:
            if contact["email"].lower() in existing_emails:
                logger.debug("Contact already exists: %s", contact["email"])
                continue

            create_data = {
                "name": {"full": contact["name"]},
                "emails": {
                    "e1": {"value": contact["email"]},
                },
            }
            payload = {
                "using": JMAP_USING_CONTACTS,
                "methodCalls": [
                    ["ContactCard/set", {
                        "accountId": self.account_id,
                        "create": {f"ct{created}": create_data},
                    }, "cc"],
                ],
            }
            try:
                resp = self._http.post(self.api_url, json=payload)
                resp.raise_for_status()
                result = resp.json()["methodResponses"][0][1]
                if result.get("created"):
                    created += 1
                    logger.info("Created contact: %s <%s>", contact["name"], contact["email"])
                else:
                    logger.warning("Failed to create contact %s: %s",
                                   contact["email"], result.get("notCreated"))
            except Exception as e:
                logger.warning("Failed to create contact %s: %s", contact["email"], e)

        return created

    def close(self):
        self._http.close()


def build_rfc5322(
    from_name: str,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    received_at: str | None = None,
    list_id: str | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> bytes:
    """Build a minimal RFC 5322 message."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = email.utils.formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Subject"] = subject

    if received_at:
        dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        msg["Date"] = email.utils.format_datetime(dt)
    else:
        msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))

    if message_id:
        msg["Message-ID"] = message_id
    else:
        msg["Message-ID"] = f"<test-{hash(subject) & 0xFFFFFFFF:08x}@mailsort-test>"

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    if list_id:
        msg["List-Id"] = list_id

    # Tag for easy cleanup
    msg["X-Mailsort-Test"] = "true"

    # RFC 5322 requires \r\n line endings; MIMEText uses \n
    raw = msg.as_bytes()
    # Replace bare \n with \r\n (but not \r\n that's already correct)
    raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return raw


def load_folder_fixtures(
    loader: JMAPLoader,
    to_email: str,
    fixtures_path: Path,
    *,
    tag_keyword: str = "$mailsort-test",
) -> int:
    """Load static fixture emails into folders. Returns count loaded."""
    with open(fixtures_path) as f:
        data = json.load(f)

    emails = data["emails"]
    folder_map = loader.resolve_folder_paths()

    # Pre-fetch existing subjects per folder for dedup
    existing: dict[str, set[str]] = {}

    loaded = 0
    skipped = 0
    errors = 0
    days_offset = 0

    for i, em in enumerate(emails):
        folder_path = em["folder"]

        # Find mailbox ID — try with various inbox prefix casings
        mailbox_id = folder_map.get(folder_path)
        if not mailbox_id:
            mailbox_id = folder_map.get(f"Inbox/{folder_path}")
        if not mailbox_id:
            mailbox_id = folder_map.get(f"INBOX/{folder_path}")
        if not mailbox_id:
            logger.warning("Folder not found: %s (available: %s)",
                           folder_path, ", ".join(sorted(folder_map.keys())))
            errors += 1
            continue

        # Dedup check
        if folder_path not in existing:
            existing[folder_path] = loader.query_folder_subjects(mailbox_id)

        if em["subject"] in existing[folder_path]:
            skipped += 1
            continue

        # Spread receivedAt across last 30 days
        days_offset = (i * 30) // len(emails)
        received_at = (
            datetime.now(timezone.utc) - timedelta(days=30 - days_offset, hours=i % 12)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        rfc5322 = build_rfc5322(
            from_name=em["from_name"],
            from_email=em["from_email"],
            to_email=to_email,
            subject=em["subject"],
            body=em["body"],
            received_at=received_at,
            list_id=em.get("list_id"),
            message_id=em.get("message_id"),
        )

        try:
            blob_id = loader.upload_blob(rfc5322)
            email_id = loader.import_email(
                blob_id,
                mailbox_id,
                keywords={"$seen": True, tag_keyword: True},
                received_at=received_at,
            )
            if email_id:
                loaded += 1
                existing[folder_path].add(em["subject"])
            else:
                errors += 1
        except Exception as e:
            logger.error("Failed to load email '%s': %s", em["subject"], e)
            errors += 1

    logger.info("Loaded %d emails, skipped %d (already exist), %d errors", loaded, skipped, errors)
    return loaded


def load_inbox_emails(
    loader: JMAPLoader,
    to_email: str,
    inbox_emails: list[dict],
    *,
    tag_keyword: str = "$mailsort-test",
) -> int:
    """Load dynamic inbox emails. Returns count loaded."""
    folder_map = loader.resolve_folder_paths()

    # Find inbox ID (Fastmail uses "Inbox" not "INBOX")
    inbox_id = None
    for path, mid in folder_map.items():
        if path.upper() == "INBOX":
            inbox_id = mid
            break
    if not inbox_id:
        logger.error("Could not find inbox mailbox")
        return 0

    existing_subjects = loader.query_folder_subjects(inbox_id)
    loaded = 0

    for em in inbox_emails:
        if em["subject"] in existing_subjects:
            continue

        rfc5322 = build_rfc5322(
            from_name=em.get("from_name", em["from_email"].split("@")[0]),
            from_email=em["from_email"],
            to_email=to_email,
            subject=em["subject"],
            body=em.get("body", f"Test email from {em['from_email']}"),
            received_at=em.get("received_at"),
            list_id=em.get("list_id"),
            message_id=em.get("message_id"),
            in_reply_to=em.get("in_reply_to"),
        )

        try:
            blob_id = loader.upload_blob(rfc5322)
            keywords = dict(em.get("keywords", {"$seen": True}))
            keywords[tag_keyword] = True

            email_id = loader.import_email(
                blob_id,
                inbox_id,
                keywords=keywords,
                received_at=em.get("received_at"),
            )
            if email_id:
                loaded += 1
                existing_subjects.add(em["subject"])
        except Exception as e:
            logger.error("Failed to load inbox email '%s': %s", em["subject"], e)

    logger.info("Loaded %d inbox emails", loaded)
    return loaded


def cleanup_test_emails(loader: JMAPLoader, tag_keyword: str = "$mailsort-test") -> int:
    """Delete all emails tagged with the test keyword."""
    count = loader.delete_emails_with_keyword(tag_keyword)
    logger.info("Deleted %d test emails", count)
    return count


def main():
    parser = argparse.ArgumentParser(description="Load fixture emails into Fastmail test account")
    parser.add_argument("--config", default="tests/system/config.test.yaml", help="Path to test config")
    parser.add_argument("--cleanup", action="store_true", help="Delete all test emails instead of loading")
    parser.add_argument("--to-email", default=None, help="Test account email address (auto-detected from JMAP session if omitted)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Load config for token
    import os
    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    if not token:
        print("Error: FASTMAIL_API_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    # Load config for session URL
    import yaml
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")

    loader = JMAPLoader(token, session_url)

    try:
        to_email = args.to_email or loader.account_email
        print(f"Using recipient address: {to_email}")

        if args.cleanup:
            count = cleanup_test_emails(loader)
            print(f"Cleaned up {count} test emails")
        else:
            # Create test contacts first
            contacts_created = loader.create_contacts(TEST_CONTACTS)
            print(f"Created {contacts_created} test contacts")

            fixtures_path = Path(__file__).parent / "fixtures" / "folder_emails.json"
            count = load_folder_fixtures(loader, to_email, fixtures_path)
            print(f"Loaded {count} fixture emails into folders")
    finally:
        loader.close()


if __name__ == "__main__":
    main()
