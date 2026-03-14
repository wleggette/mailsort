"""Dynamic inbox email generator for system tests.

Generates inbox emails with timestamps relative to now, testing all
classification and eligibility scenarios. See SYSTEM_TEST_PLAN.md §2.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def generate_inbox_emails() -> list[dict]:
    """Generate test emails for inbox with dynamic receivedAt times.

    Returns a list of dicts with keys: from_email, from_name, subject, body,
    keywords, received_at, list_id, expected_outcome, description.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d%H%M")

    return [
        # E1: Rule match (chase), read, old — should be moved to Banks
        {
            "from_email": "noreply@chase.com",
            "from_name": "Chase Bank",
            "subject": f"[TEST] Chase alert - eligible {ts}",
            "body": "Your Chase account has a new notification. Please review.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "moved",
            "expected_folder": "Affairs/Banks",
            "description": "E1: Rule match, eligible — moved to Banks",
        },

        # E2: Rule match (amazon), unread — classified but not moved
        {
            "from_email": "orders@amazon.com",
            "from_name": "Amazon",
            "subject": f"[TEST] Amazon order unread {ts}",
            "body": "Your Amazon order has shipped.",
            "keywords": {},  # unread
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "unread",
            "description": "E2: Rule match, unread — classified but not moved",
        },

        # E3: Rule match (chase), read + flagged — classified but not moved
        {
            "from_email": "noreply@chase.com",
            "from_name": "Chase Bank",
            "subject": f"[TEST] Chase flagged {ts}",
            "body": "Important: your Chase credit limit has been increased.",
            "keywords": {"$seen": True, "$flagged": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "flagged",
            "description": "E3: Rule match, flagged — classified but not moved",
        },

        # E4: Rule match (BoA), read, too new — classified but not moved
        {
            "from_email": "alerts@bankofamerica.com",
            "from_name": "Bank of America",
            "subject": f"[TEST] BofA too new {ts}",
            "body": "A new transaction was posted to your account.",
            "keywords": {"$seen": True},
            "received_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),  # just now
            "expected_outcome": "too_new",
            "description": "E4: Rule match, too new — receivedAt is now, under min_age_minutes",
        },

        # E5: Unread + flagged + new — unread takes priority
        {
            "from_email": "noreply@target.com",
            "from_name": "Target",
            "subject": f"[TEST] Target unread+flagged {ts}",
            "body": "Your Target order is ready for pickup.",
            "keywords": {"$flagged": True},  # unread (no $seen) + flagged
            "received_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "unread",
            "description": "E5: Unread + flagged + new — unread checked first",
        },

        # S5: LLM classification — unknown sender, should go to LLM
        {
            "from_email": "newsletter@newbank.com",
            "from_name": "NewBank Newsletter",
            "subject": f"[TEST] NewBank investment tips {ts}",
            "body": "Here are this week's top investment tips and market analysis from NewBank financial advisors.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "llm_classifies",
            "description": "S5: No rule match, LLM classifies — content suggests Banks",
        },

        # S8: Known contact, ambiguous — LLM with stricter threshold
        {
            "from_email": "testcontact@example.com",
            "from_name": "Test Contact",
            "subject": f"[TEST] Contact ambiguous msg {ts}",
            "body": "Hey, just wanted to check in about a few things. Let me know when you're free.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "llm_known_contact",
            "description": "S8: Known contact, split history — LLM with known-contact threshold",
        },

        # C4: Unknown exact sender, split across folders — LLM
        {
            "from_email": "alice@family.com",
            "from_name": "Alice Family",
            "subject": f"[TEST] Alice ambiguous {ts}",
            "body": "Can you pick up the kids from school today? Also, did you see the bank statement?",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "llm_no_rule",
            "description": "C4: Unknown sender with split history — no rule, LLM classifies",
        },

        # C5: Completely unknown sender — no rule, no history
        {
            "from_email": "random@unknown.com",
            "from_name": "Random Person",
            "subject": f"[TEST] Unknown sender {ts}",
            "body": "This is a completely random email with no prior history in the system.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "llm_or_no_classification",
            "description": "C5: Unknown sender, no evidence — LLM or no_classification",
        },

        # R5: megastore.com address below threshold — no rule
        {
            "from_email": "returns@megastore.com",
            "from_name": "MegaStore Returns",
            "subject": f"[TEST] MegaStore return status {ts}",
            "body": "Your return has been received at our warehouse. Refund processing in 3-5 business days.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "llm_no_domain_rule",
            "description": "R5: megastore.com address with <3 emails — no rule, LLM classifies",
        },

        # S2: Domain rule match (bigbank.com) — new address at ruled domain
        {
            "from_email": "support@bigbank.com",
            "from_name": "BigBank Support",
            "subject": f"[TEST] BigBank support ticket {ts}",
            "body": "Your support ticket #12345 has been updated. A representative will contact you shortly.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_outcome": "moved",
            "expected_folder": "Affairs/Banks",
            "description": "S2: Domain rule match — new address at bigbank.com, domain rule applies",
        },

        # S3: List-Id rule match
        {
            "from_email": "newsletter@lincolnelementary.org",
            "from_name": "School Newsletter",
            "subject": f"[TEST] School weekly update {ts}",
            "body": "This week at Lincoln Elementary: spring concert rehearsals begin.",
            "keywords": {"$seen": True},
            "received_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "list_id": "<newsletter.school.org>",
            "expected_outcome": "moved",
            "expected_folder": "People/Children",
            "description": "S3: List-Id rule match — newsletter.school.org list",
        },
    ]
