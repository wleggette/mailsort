#!/usr/bin/env python3
"""Analyze List-Unsubscribe header prevalence across Fastmail folders.

Scans INBOX and all subfolders under People/ and Affairs/ to report:
- How many emails have List-Unsubscribe (with and without List-Id)
- Breakdown by folder and sender domain
- Emails that have List-Unsubscribe but NO List-Id (the gap a combined rule would fill)

Usage:
    python scripts/analyze_list_unsubscribe.py
    # Uses FASTMAIL_API_TOKEN from environment or .env file
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

# Load .env if present
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip()

# Add src to path so we can import mailsort
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mailsort.jmap.client import JMAPClient
from mailsort.jmap.mailbox_tree import MailboxTree

# Fastmail accepts header:List-Unsubscribe (case-sensitive, no :asText suffix)
ANALYSIS_PROPERTIES = [
    "id", "from", "subject", "header:list-id:asText", "header:List-Unsubscribe",
]


def main() -> None:
    token = os.environ.get("FASTMAIL_API_TOKEN")
    if not token:
        print("Error: FASTMAIL_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    session_url = "https://api.fastmail.com/jmap/session"

    with JMAPClient(token, session_url) as client:
        # Build mailbox tree
        mailboxes = client.get_all_mailboxes()
        tree = MailboxTree.build(mailboxes, exclude_patterns=[])

        # Find target folders: INBOX + anything under People/ or Affairs/
        target_folders: list[tuple[str, str]] = []  # (path, mailbox_id)

        # Add INBOX itself
        target_folders.append(("INBOX", tree.inbox_id))

        # Add subfolders matching People or Affairs (with or without INBOX/ prefix)
        for path in sorted(tree.all_folder_paths()):
            normalized = path.replace("INBOX/", "", 1) if path.startswith("INBOX/") else path
            if normalized.startswith("People") or normalized.startswith("Affairs"):
                mid = tree.id_for(path)
                if mid:
                    target_folders.append((path, mid))

        print(f"Found {len(target_folders)} folders:")
        for f, _ in target_folders:
            print(f"  {f}")

        print(f"\nScanning {len(target_folders)} folders (up to 200 emails each)...\n")
        session = client.get_session()

        # Stats
        total_emails = 0
        total_with_unsub = 0
        total_with_listid = 0
        total_unsub_no_listid = 0  # The interesting gap

        folder_stats: dict[str, dict] = {}
        domain_unsub_no_listid: dict[str, int] = defaultdict(int)
        domain_unsub_no_listid_folders: dict[str, set] = defaultdict(set)

        for folder_path, mailbox_id in target_folders:
            email_ids = client.query_folder_emails(mailbox_id, limit=200)
            if not email_ids:
                continue

            # Fetch in batches of 50
            folder_total = 0
            folder_unsub = 0
            folder_listid = 0
            folder_unsub_no_listid = 0

            for i in range(0, len(email_ids), 50):
                batch_ids = email_ids[i : i + 50]
                data = client.call([[
                    "Email/get", {
                        "accountId": session.account_id,
                        "ids": batch_ids,
                        "properties": ANALYSIS_PROPERTIES,
                    }, "g1",
                ]])
                raw_emails = data["methodResponses"][0][1].get("list", [])

                for email in raw_emails:
                    folder_total += 1
                    has_unsub = bool(email.get("header:List-Unsubscribe"))
                    has_listid = bool(email.get("header:list-id:asText"))

                    if has_unsub:
                        folder_unsub += 1
                    if has_listid:
                        folder_listid += 1
                    if has_unsub and not has_listid:
                        folder_unsub_no_listid += 1
                        from_list = email.get("from") or [{}]
                        addr = from_list[0].get("email", "") if from_list else ""
                        domain = addr.split("@", 1)[1].lower() if "@" in addr else addr
                        domain_unsub_no_listid[domain] += 1
                        domain_unsub_no_listid_folders[domain].add(folder_path)

            total_emails += folder_total
            total_with_unsub += folder_unsub
            total_with_listid += folder_listid
            total_unsub_no_listid += folder_unsub_no_listid

            folder_stats[folder_path] = {
                "total": folder_total,
                "unsub": folder_unsub,
                "listid": folder_listid,
                "unsub_no_listid": folder_unsub_no_listid,
            }

            pct = (folder_unsub / folder_total * 100) if folder_total else 0
            gap_pct = (folder_unsub_no_listid / folder_total * 100) if folder_total else 0
            print(
                f"  {folder_path:40s}  {folder_total:4d} emails  "
                f"{folder_unsub:3d} unsub ({pct:4.1f}%)  "
                f"{folder_unsub_no_listid:3d} unsub-only ({gap_pct:4.1f}%)"
            )

        # Summary
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"  Total emails scanned:              {total_emails}")
        print(f"  With List-Id:                      {total_with_listid} ({total_with_listid / total_emails * 100:.1f}%)" if total_emails else "")
        print(f"  With List-Unsubscribe:             {total_with_unsub} ({total_with_unsub / total_emails * 100:.1f}%)" if total_emails else "")
        print(f"  With List-Unsubscribe but NO Id:   {total_unsub_no_listid} ({total_unsub_no_listid / total_emails * 100:.1f}%)" if total_emails else "")

        if total_unsub_no_listid == 0:
            print("\n  → No emails found with List-Unsubscribe but missing List-Id.")
            print("    The combined rule feature would have zero impact.")
        else:
            print(f"\n  → {total_unsub_no_listid} emails have List-Unsubscribe but no List-Id.")
            print("    These are the emails a combined rule could help classify.")

            # Top domains in the gap
            print(f"\n  Top domains with List-Unsubscribe but no List-Id:")
            sorted_domains = sorted(domain_unsub_no_listid.items(), key=lambda x: -x[1])
            for domain, count in sorted_domains[:20]:
                folders = ", ".join(
                    p.replace("INBOX/", "") for p in sorted(domain_unsub_no_listid_folders[domain])
                )
                coherent = len(domain_unsub_no_listid_folders[domain]) == 1
                marker = "✓" if coherent else "✗"
                print(f"    {marker} {domain:35s} {count:3d} emails  → {folders}")

            coherent_count = sum(
                1 for d in domain_unsub_no_listid_folders.values() if len(d) == 1
            )
            split_count = len(domain_unsub_no_listid_folders) - coherent_count
            print(f"\n  Domains with coherent folder (✓): {coherent_count}")
            print(f"  Domains split across folders (✗): {split_count}")

            if coherent_count > 0:
                coherent_emails = sum(
                    domain_unsub_no_listid[d]
                    for d, folders in domain_unsub_no_listid_folders.items()
                    if len(folders) == 1
                )
                print(
                    f"\n  → {coherent_emails} emails from {coherent_count} domains could be "
                    f"covered by a domain+unsub combined rule."
                )


if __name__ == "__main__":
    main()
