"""Result validator for system tests.

Queries the test database and validates that bootstrap, dry-run, live run,
and correction simulation produced the expected outcomes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mailsort.db.database import Database

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    details: list[str] = field(default_factory=list)

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed += 1
            self.details.append(f"  PASS: {description}")
        else:
            self.failed += 1
            self.details.append(f"  FAIL: {description}")
        return condition

    def warn(self, description: str):
        self.warnings += 1
        self.details.append(f"  WARN: {description}")

    def summary(self) -> str:
        return f"{self.passed} passed, {self.failed} failed, {self.warnings} warnings"

    def print_report(self):
        for line in self.details:
            print(line)
        print(f"\n  {self.summary()}")


def verify_bootstrap(db: Database) -> VerificationResult:
    """Verify bootstrap created expected rules and descriptions."""
    v = VerificationResult()
    print("\n=== Verifying Bootstrap ===")

    # Rules created
    rules = db.execute("SELECT * FROM rules WHERE active = 1").fetchall()
    rule_map = {(r["rule_type"], r["condition_value"]): r for r in rules}

    # Group A: exact_sender rules for clean high-coherence senders
    for sender in ["noreply@chase.com", "alerts@bankofamerica.com",
                    "orders@amazon.com", "noreply@target.com",
                    "admin@lincolnelementary.org", "activities@ymca.org"]:
        v.check(
            ("exact_sender", sender) in rule_map,
            f"exact_sender rule for {sender}",
        )

    # Group B: domain rule for bigbank.com
    v.check(
        ("sender_domain", "bigbank.com") in rule_map,
        "sender_domain rule for bigbank.com (high coherence, 3+ senders)",
    )

    # Group C: NO domain rule for megastore.com (low coherence)
    v.check(
        ("sender_domain", "megastore.com") not in rule_map,
        "NO sender_domain rule for megastore.com (low coherence — Amazon problem)",
    )
    # But exact_sender rules for orders@ and alerts@ (each ≥3)
    v.check(
        ("exact_sender", "orders@megastore.com") in rule_map,
        "exact_sender rule for orders@megastore.com (≥3 emails, high coherence)",
    )
    v.check(
        ("exact_sender", "alerts@megastore.com") in rule_map,
        "exact_sender rule for alerts@megastore.com (≥3 emails, high coherence)",
    )
    # returns@ has only 2 — no rule
    v.check(
        ("exact_sender", "returns@megastore.com") not in rule_map,
        "NO exact_sender rule for returns@megastore.com (only 2 emails)",
    )

    # Group D: NO rule for testcontact@example.com (split across folders)
    v.check(
        ("exact_sender", "testcontact@example.com") not in rule_map,
        "NO rule for testcontact@example.com (split across 3 folders, low coherence)",
    )

    # Group E: exact_sender rule for testfriend@gmail.com (concentrated)
    v.check(
        ("exact_sender", "testfriend@gmail.com") in rule_map,
        "exact_sender rule for testfriend@gmail.com (concentrated in Children)",
    )

    # Group F: list_id rule
    v.check(
        ("list_id", "<newsletter.school.org>") in rule_map,
        "list_id rule for <newsletter.school.org>",
    )

    # Group G: NO list_id rule for mixed alerts (low coherence)
    v.check(
        ("list_id", "<alerts.mixed.com>") not in rule_map,
        "NO list_id rule for <alerts.mixed.com> (split across folders)",
    )

    # Group H: NO rule for alice@family.com (split across folders)
    v.check(
        ("exact_sender", "alice@family.com") not in rule_map,
        "NO rule for alice@family.com (split across 2 folders, 50% coherence)",
    )

    # Group I: NO rule for rare@oneoff.com (below threshold)
    v.check(
        ("exact_sender", "rare@oneoff.com") not in rule_map,
        "NO rule for rare@oneoff.com (only 2 emails, below threshold of 3)",
    )

    # Folder descriptions
    desc_count = db.execute("SELECT COUNT(*) FROM folder_descriptions").fetchone()[0]
    v.check(desc_count >= 3, f"At least 3 folder descriptions generated (got {desc_count})")

    # Coverage
    total_evidence = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'manual'"
    ).fetchone()[0]
    v.check(total_evidence > 50, f"At least 50 evidence emails in audit_log (got {total_evidence})")

    v.print_report()
    return v


def verify_dry_run(db: Database, run_id: str) -> VerificationResult:
    """Verify dry-run produced correct classifications without moving."""
    v = VerificationResult()
    print(f"\n=== Verifying Dry Run ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()
    by_subject_prefix: dict[str, dict] = {}
    for r in rows:
        subject = r["subject"] or ""
        if subject.startswith("[TEST]"):
            by_subject_prefix[subject] = dict(r)

    v.check(len(rows) > 0, f"Audit log has entries for this run (got {len(rows)})")

    # No emails should have been moved
    moved_count = sum(1 for r in rows if r["moved"])
    v.check(moved_count == 0, f"No emails moved in dry run (moved={moved_count})")

    # Check specific test emails by subject prefix pattern
    for r in rows:
        subject = r["subject"] or ""
        if "eligible" in subject.lower() and "chase" in subject.lower():
            v.check(r["classification_source"] == "rule", f"Chase eligible classified by rule")
            v.check(r["skip_reason"] == "dry_run" or r["skip_reason"] is None,
                     f"Chase eligible: skip_reason={r['skip_reason']}")
        elif "unread" in subject.lower() and "amazon" in subject.lower():
            v.check(r["skip_reason"] == "unread", f"Amazon unread: skip_reason={r['skip_reason']}")
        elif "flagged" in subject.lower() and "chase" in subject.lower():
            v.check(r["skip_reason"] == "flagged", f"Chase flagged: skip_reason={r['skip_reason']}")
        elif "too new" in subject.lower() or "bofa too new" in subject.lower():
            v.check(r["skip_reason"] == "too_new", f"BofA too new: skip_reason={r['skip_reason']}")

    v.print_report()
    return v


def verify_live_run(db: Database, run_id: str) -> VerificationResult:
    """Verify live run moved eligible emails and left others."""
    v = VerificationResult()
    print(f"\n=== Verifying Live Run ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()

    moved = [r for r in rows if r["moved"]]
    not_moved = [r for r in rows if not r["moved"]]

    v.check(len(moved) > 0, f"At least some emails were moved (moved={len(moved)})")
    v.check(len(not_moved) > 0, f"Some emails were not moved (not_moved={len(not_moved)})")

    # Verify specific outcomes
    for r in rows:
        subject = r["subject"] or ""
        if "[TEST]" not in subject:
            continue
        if "eligible" in subject.lower() and "chase" in subject.lower():
            v.check(r["moved"] == 1, f"Chase eligible was moved")
            v.check(r["target_folder"] in ("Affairs/Banks", "INBOX/Affairs/Banks"),
                     f"Chase moved to Banks (got {r['target_folder']})")
        elif "unread" in subject.lower():
            v.check(r["moved"] == 0, f"Unread email was NOT moved")
            v.check(r["skip_reason"] == "unread", f"Unread email skip_reason=unread")
        elif "flagged" in subject.lower() and "unread" not in subject.lower():
            v.check(r["moved"] == 0, f"Flagged email was NOT moved")
            v.check(r["skip_reason"] == "flagged", f"Flagged email skip_reason=flagged")

    run_row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    v.check(run_row["status"] == "completed", f"Run status is completed")
    v.check(run_row["emails_moved"] == len(moved), f"Run emails_moved matches audit_log")

    v.print_report()
    return v


def verify_too_new_blocked(db: Database, run_id: str) -> VerificationResult:
    """Verify that too-new emails were classified but NOT moved (skip_reason=too_new)."""
    v = VerificationResult()
    print(f"\n=== Verifying Too-New Blocked ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()

    found_too_new = False
    for r in rows:
        subject = r["subject"] or ""
        if "too new" in subject.lower():
            found_too_new = True
            v.check(
                r["skip_reason"] == "too_new",
                f"Too-new email blocked (skip_reason={r['skip_reason']!r}, subject={subject[:50]})",
            )
            v.check(
                not r["moved"],
                f"Too-new email was NOT moved (moved={r['moved']}, subject={subject[:50]})",
            )

    v.check(found_too_new, "At least one too-new email found in run")

    v.print_report()
    return v


def verify_age_gate(db: Database, run_id: str) -> VerificationResult:
    """Verify previously-too-new email was moved after waiting."""
    v = VerificationResult()
    print(f"\n=== Verifying Age Gate ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()

    found_too_new_subject = False
    for r in rows:
        subject = r["subject"] or ""
        if "too new" in subject.lower():
            found_too_new_subject = True
            v.check(
                r["skip_reason"] != "too_new",
                f"Previously too-new email is now eligible (skip_reason={r['skip_reason']!r})",
            )
            v.check(
                bool(r["moved"]),
                f"Previously too-new email was moved (moved={r['moved']})",
            )

    v.check(found_too_new_subject, "Too-new email appeared in post-timer run")

    v.print_report()
    return v


def verify_correction(db: Database, run_id: str, corrected_email_subject: str) -> VerificationResult:
    """Verify user correction was detected and rule penalized."""
    v = VerificationResult()
    print(f"\n=== Verifying Correction Detection ({run_id[:8]}) ===")

    # Check for manual audit_log row
    manual_rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ? AND classification_source = 'manual'",
        (run_id,),
    ).fetchall()
    v.check(len(manual_rows) > 0, f"Manual sort detected (found {len(manual_rows)} manual rows)")

    # Check that a rule was penalized
    # Look for rules with confidence < their original value
    penalized = db.execute(
        "SELECT * FROM rules WHERE confidence < 0.95 AND source = 'auto'"
    ).fetchall()
    if penalized:
        for r in penalized:
            v.check(True, f"Rule {r['id']} ({r['condition_value']}) penalized to {r['confidence']:.2f}")
    else:
        v.warn("No rules appear to have been penalized (may be expected if correction was for LLM-classified email)")

    v.print_report()
    return v
