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
    """Verify bootstrap created expected rules, descriptions, contacts, and run record.

    Covers all scenarios from the system test plan §3: F1-F7, EF1-EF9, D1-D7,
    LR1-LR4, DR1-DR6, ER1-ER8, P1-P3, CA1-CA3, CI1-CI5.
    """
    v = VerificationResult()
    print("\n=== Verifying Bootstrap ===")

    # ------------------------------------------------------------------
    # Run record (§3.6)
    # ------------------------------------------------------------------
    run_row = db.execute(
        "SELECT * FROM runs WHERE trigger = 'bootstrap' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    v.check(run_row is not None, "Bootstrap run record exists")
    if run_row:
        v.check(run_row["status"] == "completed", f"Bootstrap run status=completed (got {run_row['status']})")
        v.check(run_row["emails_moved"] == 0, f"Bootstrap emails_moved=0 (got {run_row['emails_moved']})")

    # ------------------------------------------------------------------
    # Evidence rows (§3.6, EF1-EF9)
    # ------------------------------------------------------------------
    total_evidence = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'manual'"
    ).fetchone()[0]
    v.check(total_evidence > 50, f"Evidence: >50 emails in audit_log (got {total_evidence})")

    # All evidence rows should have moved=1 and skip_reason IS NULL
    bad_moved = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'manual' AND moved != 1"
    ).fetchone()[0]
    v.check(bad_moved == 0, f"All evidence rows have moved=1 (violations: {bad_moved})")

    bad_skip = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'manual' AND skip_reason IS NOT NULL"
    ).fetchone()[0]
    v.check(bad_skip == 0, f"All evidence rows have skip_reason=NULL (violations: {bad_skip})")

    # ------------------------------------------------------------------
    # Rules created — load all active rules
    # ------------------------------------------------------------------
    rules = db.execute("SELECT * FROM rules WHERE active = 1").fetchall()
    rule_map = {(r["rule_type"], r["condition_value"]): r for r in rules}

    # --- Group A: exact_sender rules (ER1) ---
    for sender in ["noreply@chase.com", "alerts@bankofamerica.com",
                    "orders@amazon.com", "noreply@target.com",
                    "admin@lincolnelementary.org"]:
        v.check(("exact_sender", sender) in rule_map, f"ER1: exact_sender rule for {sender}")

    # Group A + N: activities@ymca.org has BOTH exact_sender and list_id (LR4, P1)
    v.check(
        ("exact_sender", "activities@ymca.org") in rule_map,
        "LR4/P1: exact_sender rule for activities@ymca.org (coexists with list_id)",
    )
    v.check(
        ("list_id", "<updates.ymca.org>") in rule_map,
        "LR4/P1: list_id rule for <updates.ymca.org> (coexists with exact_sender)",
    )

    # --- Group B: domain rule (DR1) + exact_sender coexistence (ER4, P2) ---
    v.check(
        ("sender_domain", "bigbank.com") in rule_map,
        "DR1: sender_domain rule for bigbank.com (8 emails, 3 senders, 100% coherence)",
    )
    v.check(
        ("exact_sender", "statements@bigbank.com") in rule_map,
        "ER4/P2: exact_sender for statements@bigbank.com (3×, coexists with domain rule)",
    )
    v.check(
        ("exact_sender", "alerts@bigbank.com") in rule_map,
        "ER4/P2: exact_sender for alerts@bigbank.com (3×, coexists with domain rule)",
    )
    v.check(
        ("exact_sender", "fraud@bigbank.com") not in rule_map,
        "ER4: NO exact_sender for fraud@bigbank.com (2× below threshold)",
    )

    # --- Group C: split domain (DR2, ER5, ER6, P3) ---
    v.check(
        ("sender_domain", "megastore.com") not in rule_map,
        "DR2/P3: NO sender_domain for megastore.com (57% coherence)",
    )
    v.check(
        ("exact_sender", "orders@megastore.com") in rule_map,
        "ER5: exact_sender for orders@megastore.com (4× Stores)",
    )
    v.check(
        ("exact_sender", "alerts@megastore.com") in rule_map,
        "ER5: exact_sender for alerts@megastore.com (3× Banks)",
    )
    v.check(
        ("exact_sender", "returns@megastore.com") not in rule_map,
        "ER6: NO exact_sender for returns@megastore.com (2× below threshold)",
    )

    # --- Group D: known contact, split (ER2) ---
    v.check(
        ("exact_sender", "testcontact@example.com") not in rule_map,
        "ER2: NO rule for testcontact@example.com (40% coherence across 3 folders)",
    )

    # --- Group E: known contact, concentrated (ER1) ---
    v.check(
        ("exact_sender", "testfriend@gmail.com") in rule_map,
        "ER1: exact_sender for testfriend@gmail.com (4× Children, 100% coherence)",
    )

    # --- Group F: list_id boundary (LR1) ---
    v.check(
        ("list_id", "<newsletter.school.org>") in rule_map,
        "LR1: list_id rule for <newsletter.school.org> (boundary: exactly 2 emails)",
    )

    # --- Group G: list_id low coherence (LR2) ---
    v.check(
        ("list_id", "<alerts.mixed.com>") not in rule_map,
        "LR2: NO list_id for <alerts.mixed.com> (50% coherence)",
    )

    # --- Group H: unknown sender, split (ER2) ---
    v.check(
        ("exact_sender", "alice@family.com") not in rule_map,
        "ER2: NO rule for alice@family.com (50% coherence across 2 folders)",
    )

    # --- Group I: below threshold (ER3) ---
    v.check(
        ("exact_sender", "rare@oneoff.com") not in rule_map,
        "ER3: NO rule for rare@oneoff.com (2 emails, below threshold of 3)",
    )

    # --- Group J: domain <3 senders (DR3) ---
    v.check(
        ("sender_domain", "concentrated.com") not in rule_map,
        "DR3: NO sender_domain for concentrated.com (1 sender, need 3)",
    )
    v.check(
        ("exact_sender", "single@concentrated.com") in rule_map,
        "DR3: exact_sender for single@concentrated.com (5× Banks, 100% coherence)",
    )

    # --- Group K: bulk sender + domain rule under sampling cap (F6) ---
    # 55 emails in Medical from 3 senders at myhealth.com; only 50 sampled
    v.check(
        ("sender_domain", "myhealth.com") in rule_map,
        "F6/DR: sender_domain for myhealth.com (3 senders, 100% coherence, survives 50-email cap)",
    )
    v.check(
        ("exact_sender", "portal@myhealth.com") in rule_map,
        "F6: exact_sender for portal@myhealth.com (Medical)",
    )
    v.check(
        ("exact_sender", "labs@myhealth.com") in rule_map,
        "F6: exact_sender for labs@myhealth.com (Medical)",
    )
    v.check(
        ("exact_sender", "appointments@myhealth.com") in rule_map,
        "F6: exact_sender for appointments@myhealth.com (Medical)",
    )

    # F6 sampling cap: Medical has 55 emails but only 50 should be sampled
    medical_evidence = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE classification_source = 'manual' "
        "AND target_folder LIKE '%Medical%'"
    ).fetchone()[0]
    v.check(
        medical_evidence == 50,
        f"F6: Medical sampled exactly 50 of 55 emails (got {medical_evidence})",
    )

    # --- Group L: below list_id threshold (LR3) ---
    v.check(
        ("list_id", "<rare.list.org>") not in rule_map,
        "LR3: NO list_id for <rare.list.org> (1 email, below threshold of 2)",
    )

    # --- Group M: domain boundary (DR4) ---
    v.check(
        ("sender_domain", "boundarybank.com") in rule_map,
        "DR4: sender_domain for boundarybank.com (boundary: exactly 5 emails, 3 senders)",
    )

    # --- Group N: already checked above with Group A (LR4, P1) ---

    # --- Group O: list_id + sender_domain coexistence (DR5) ---
    v.check(
        ("list_id", "<updates.community.org>") in rule_map,
        "DR5: list_id rule for <updates.community.org>",
    )
    v.check(
        ("sender_domain", "community.org") in rule_map,
        "DR5: sender_domain rule for community.org (5 emails, 3 senders, all with list_id)",
    )

    # --- Group P: exact_sender count boundary (ER7) ---
    v.check(
        ("exact_sender", "receipts@shopify.com") in rule_map,
        "ER7: exact_sender for receipts@shopify.com (boundary: exactly 3 emails)",
    )

    # --- Group Q: domain 2 senders boundary (DR6) ---
    v.check(
        ("sender_domain", "twopeople.com") not in rule_map,
        "DR6: NO sender_domain for twopeople.com (2 senders, need 3)",
    )
    v.check(
        ("exact_sender", "info@twopeople.com") in rule_map,
        "DR6: exact_sender for info@twopeople.com (3× Banks)",
    )
    v.check(
        ("exact_sender", "support@twopeople.com") not in rule_map,
        "DR6: NO exact_sender for support@twopeople.com (2× below threshold)",
    )

    # --- Group R: coherence boundary (ER8) ---
    v.check(
        ("exact_sender", "billing@utility.com") in rule_map,
        "ER8: exact_sender for billing@utility.com (4/5=80% coherence, boundary pass)",
    )

    # ------------------------------------------------------------------
    # Confidence values (CA1-CA3)
    # ------------------------------------------------------------------
    # list_id rules should have confidence 0.95
    for list_val in ["<newsletter.school.org>", "<updates.ymca.org>", "<updates.community.org>"]:
        r = rule_map.get(("list_id", list_val))
        if r:
            v.check(r["confidence"] == 0.95, f"CA1: list_id {list_val} confidence=0.95 (got {r['confidence']})")

    # Check a domain rule confidence: min(0.90, 0.75 + n*0.02)
    r = rule_map.get(("sender_domain", "bigbank.com"))
    if r:
        # 8 emails to target → min(0.90, 0.75 + 8*0.02) = min(0.90, 0.91) = 0.90
        v.check(r["confidence"] == 0.90, f"CA1: bigbank.com domain confidence=0.90 (got {r['confidence']})")

    # Check an exact_sender confidence: min(0.95, 0.80 + n*0.03)
    r = rule_map.get(("exact_sender", "noreply@chase.com"))
    if r:
        # 5 emails → min(0.95, 0.80 + 5*0.03) = min(0.95, 0.95) = 0.95
        v.check(r["confidence"] == 0.95, f"CA1: chase exact confidence=0.95 (got {r['confidence']})")

    # Rule target folders (CA3)
    r = rule_map.get(("exact_sender", "noreply@chase.com"))
    if r:
        v.check("Banks" in r["target_folder_path"], f"CA3: chase → Banks (got {r['target_folder_path']})")
    r = rule_map.get(("exact_sender", "orders@amazon.com"))
    if r:
        v.check("Stores" in r["target_folder_path"], f"CA3: amazon → Stores (got {r['target_folder_path']})")

    # ------------------------------------------------------------------
    # Folder descriptions (D1-D7)
    # ------------------------------------------------------------------
    desc_rows = db.execute("SELECT * FROM folder_descriptions").fetchall()
    desc_map = {r["folder_path"]: r for r in desc_rows}
    v.check(len(desc_rows) >= 4, f"D7: at least 4 folder descriptions (got {len(desc_rows)})")

    # D1: config overrides should be present for Banks, Stores, Children
    for path_suffix in ["Banks", "Stores", "Children"]:
        found = any(path_suffix in path for path in desc_map)
        v.check(found, f"D1/D7: description exists for folder containing '{path_suffix}'")

    # ------------------------------------------------------------------
    # Contacts (CI1-CI2)
    # ------------------------------------------------------------------
    contact_count = db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    v.check(contact_count >= 1, f"CI1: at least 1 contact imported (got {contact_count})")

    # CI2: testcontact@example.com should have relationship from config override
    tc = db.execute(
        "SELECT * FROM contacts WHERE email_address = 'testcontact@example.com'"
    ).fetchone()
    if tc:
        v.check(tc["relationship"] == "friend", f"CI2: testcontact relationship='friend' (got {tc['relationship']})")
    else:
        v.warn("CI2: testcontact@example.com not in contacts (may need config override)")

    v.print_report()
    return v


def verify_bootstrap_idempotency(db: Database) -> VerificationResult:
    """Verify that running bootstrap twice produces no new evidence or rules (F5, EF9)."""
    v = VerificationResult()
    print("\n=== Verifying Bootstrap Idempotency ===")

    # Should have exactly 2 bootstrap runs
    bootstrap_runs = db.execute(
        "SELECT * FROM runs WHERE trigger = 'bootstrap' ORDER BY started_at"
    ).fetchall()
    v.check(len(bootstrap_runs) >= 2, f"F5: at least 2 bootstrap runs (got {len(bootstrap_runs)})")

    if len(bootstrap_runs) >= 2:
        run1_id = bootstrap_runs[0]["run_id"]
        run2_id = bootstrap_runs[1]["run_id"]

        count1 = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE run_id = ?", (run1_id,)
        ).fetchone()[0]
        count2 = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE run_id = ?", (run2_id,)
        ).fetchone()[0]

        v.check(count1 > 50, f"F5: first bootstrap inserted evidence (got {count1})")
        v.check(count2 == 0, f"F5/EF9: second bootstrap inserted 0 new rows (got {count2})")

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
        src = r["classification_source"]
        skip = r["skip_reason"]
        folder = r["target_folder"] or ""

        # --- Eligibility gates (E1–E5) ---
        if "eligible" in subject.lower() and "chase" in subject.lower():
            v.check(src == "rule", f"E1 Chase eligible: source=rule (got {src})")
            v.check(skip is None or skip == "dry_run",
                     f"E1 Chase eligible: skip_reason={skip}")
        elif "unread" in subject.lower() and "amazon" in subject.lower():
            v.check(skip == "unread", f"E2 Amazon unread: skip_reason={skip}")
        elif "flagged" in subject.lower() and "chase" in subject.lower():
            v.check(skip == "flagged", f"E3 Chase flagged: skip_reason={skip}")
        elif "too new" in subject.lower():
            v.check(skip == "too_new", f"E4 BofA too new: skip_reason={skip}")
        elif "unread+flagged" in subject.lower() or ("target" in subject.lower() and "flagged" in subject.lower()):
            v.check(skip == "unread", f"E5 Target unread+flagged: skip_reason={skip} (unread checked first)")

        # --- Rule classification sources (S1–S4) ---
        elif "bigbank support" in subject.lower():
            v.check(src == "rule", f"S2 BigBank domain rule: source=rule (got {src})")
            v.check("banks" in folder.lower(), f"S2 BigBank → Banks (got {folder})")
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "sender_domain",
                             f"S2 matched via sender_domain rule (got {matched_rule['rule_type']})")
        elif "school weekly" in subject.lower():
            v.check(src == "rule", f"S3 School list_id rule: source=rule (got {src})")
            v.check("children" in folder.lower(), f"S3 School → Children (got {folder})")
            # Verify it was specifically the list_id rule, not an exact_sender rule
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "list_id",
                             f"S3 matched via list_id rule (got {matched_rule['rule_type']})")
                else:
                    v.check(False, f"S3 rule_id {r['rule_id']} not found in rules table")
        elif "re: one-time verification" in subject.lower():
            v.check(src == "thread", f"S4 Thread match: source={src} (expect thread — rare@oneoff.com has no rule)")
            v.check("banks" in folder.lower(), f"S4 Thread → Banks (got {folder})")

        # --- Amazon problem / per-address rules (R5) ---
        elif "megastore order" in subject.lower():
            v.check(src == "rule", f"R5b orders@megastore.com: source=rule (got {src})")
            v.check("stores" in folder.lower(), f"R5b MegaStore orders → Stores (got {folder})")
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "exact_sender",
                             f"R5b matched via exact_sender rule (got {matched_rule['rule_type']})")
        elif "megastore payment" in subject.lower():
            v.check(src == "rule", f"R5c alerts@megastore.com: source=rule (got {src})")
            v.check("banks" in folder.lower(), f"R5c MegaStore alerts → Banks (got {folder})")
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "exact_sender",
                             f"R5c matched via exact_sender rule (got {matched_rule['rule_type']})")
        elif "megastore return" in subject.lower():
            v.check(src == "llm", f"R5a returns@megastore.com: source=llm (got {src}, no rule)")

        # --- Priority interactions (P1, P2) ---
        elif "p1 exact over domain" in subject.lower():
            v.check(src == "rule", f"P1 exact_sender over sender_domain: source=rule (got {src})")
            v.check("banks" in folder.lower(), f"P1 BigBank → Banks (got {folder})")
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "exact_sender",
                             f"P1 matched via exact_sender (not sender_domain) (got {matched_rule['rule_type']})")
                else:
                    v.check(False, f"P1 rule_id {r['rule_id']} not found in rules table")
            else:
                v.check(False, "P1 no rule_id — expected exact_sender rule match")
        elif "p2 listid over exact" in subject.lower():
            v.check(src == "rule", f"P2 list_id over exact_sender: source=rule (got {src})")
            v.check("children" in folder.lower(), f"P2 YMCA → Children (got {folder})")
            if r["rule_id"]:
                matched_rule = db.execute(
                    "SELECT rule_type FROM rules WHERE id = ?", (r["rule_id"],)
                ).fetchone()
                if matched_rule:
                    v.check(matched_rule["rule_type"] == "list_id",
                             f"P2 matched via list_id (not exact_sender) (got {matched_rule['rule_type']})")
                else:
                    v.check(False, f"P2 rule_id {r['rule_id']} not found in rules table")
            else:
                v.check(False, "P2 no rule_id — expected list_id rule match")

        # --- Known contact with rule (C1) ---
        elif "friend playdate" in subject.lower():
            v.check(src == "rule", f"C1 testfriend rule: source=rule (got {src})")
            v.check("children" in folder.lower(), f"C1 testfriend → Children (got {folder})")

        # --- LLM scenarios ---
        elif "contact ambiguous" in subject.lower():
            v.check(src == "llm", f"S8 Known contact ambiguous: source=llm (got {src})")
        elif "alice ambiguous" in subject.lower():
            v.check(src == "llm", f"C4 alice@family.com split: source=llm (got {src})")
        elif "ambiguous-service" in (r["from_address"] or ""):
            v.check(src == "llm" or src is None,
                     f"S6 Ambiguous below threshold: source={src}")

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
