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
    metadata: dict = field(default_factory=dict)

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

    # ------------------------------------------------------------------
    # Hit counts — bootstrap must not record hits (coverage check is read-only)
    # ------------------------------------------------------------------
    rules_with_hits = db.execute(
        "SELECT COUNT(*) FROM rules WHERE hit_count > 0"
    ).fetchone()[0]
    v.check(rules_with_hits == 0, f"All rules have hit_count=0 after bootstrap (violations: {rules_with_hits})")

    rules_with_last_hit = db.execute(
        "SELECT COUNT(*) FROM rules WHERE last_relevant_at IS NOT NULL"
    ).fetchone()[0]
    v.check(rules_with_last_hit == 0, f"All rules have last_relevant_at=NULL after bootstrap (violations: {rules_with_last_hit})")

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

        # --- S11: Ineligible email still gets LLM classification ---
        elif "newinsurance" in subject.lower():
            v.check(src == "llm", f"S11 NewInsurance flagged: source=llm (got {src})")
            v.check(skip == "flagged", f"S11 NewInsurance: skip_reason=flagged (got {skip})")
            v.check(folder and folder != "INBOX",
                     f"S11 NewInsurance: target_folder is non-INBOX (got {folder})")

    # ------------------------------------------------------------------
    # X26: Non-LLM rows always have cached=0
    # ------------------------------------------------------------------
    non_llm_cached = db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE run_id = ? "
        "AND classification_source != 'llm' AND cached != 0",
        (run_id,),
    ).fetchone()[0]
    v.check(non_llm_cached == 0,
            f"X26 Non-LLM rows have cached=0 (violations: {non_llm_cached})")

    # ------------------------------------------------------------------
    # Hit counts — dry run must not record hits
    # ------------------------------------------------------------------
    rules_with_hits = db.execute(
        "SELECT COUNT(*) FROM rules WHERE hit_count > 0"
    ).fetchone()[0]
    v.check(rules_with_hits == 0, f"All rules have hit_count=0 after dry run (violations: {rules_with_hits})")

    # compute_rule_confidence() runs during the learning step (even on dry runs)
    # and populates last_relevant_at from bootstrap audit_log evidence.
    rules_with_last_relevant = db.execute(
        "SELECT COUNT(*) FROM rules WHERE last_relevant_at IS NOT NULL"
    ).fetchone()[0]
    total_rules = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    v.check(rules_with_last_relevant == total_rules,
            f"All rules have last_relevant_at set after dry run "
            f"(set: {rules_with_last_relevant}, total: {total_rules})")

    v.print_report()
    return v


def verify_dry_run_cached(
    db: Database, first_run_id: str, second_run_id: str,
) -> VerificationResult:
    """S10: Verify second dry run uses LLM cache hits.

    The second dry run reuses the same DB with no config changes, so
    LLM-classified emails from the first run should be cache hits.
    """
    v = VerificationResult()
    print(f"\n=== Verifying Cached Dry Run ({second_run_id[:8]}) ===")

    # Get LLM rows from the first run (these are the cache sources)
    first_llm = db.execute(
        "SELECT email_id, target_folder, confidence FROM audit_log "
        "WHERE run_id = ? AND classification_source = 'llm'",
        (first_run_id,),
    ).fetchall()
    first_llm_by_email = {r["email_id"]: dict(r) for r in first_llm}

    v.check(len(first_llm_by_email) > 0,
            f"S10 First run has LLM rows to cache (got {len(first_llm_by_email)})")

    # Get all rows from the second run
    second_rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (second_run_id,),
    ).fetchall()

    v.check(len(second_rows) > 0,
            f"S10 Second run has audit rows (got {len(second_rows)})")

    # Check LLM rows in the second run
    second_llm = [r for r in second_rows if r["classification_source"] == "llm"]
    cached_count = sum(1 for r in second_llm if r["cached"])
    v.check(cached_count > 0,
            f"S10 Second run has LLM cache hits (cached=1: {cached_count})")

    for r in second_llm:
        eid = r["email_id"]
        if eid in first_llm_by_email:
            v.check(r["cached"] == 1,
                     f"S10 {eid}: cached=1 on second run (got {r['cached']})")
            first = first_llm_by_email[eid]
            v.check(r["target_folder"] == first["target_folder"],
                     f"S10 {eid}: same target_folder "
                     f"({r['target_folder']} vs {first['target_folder']})")

    # Non-LLM rows should NOT be cached
    non_llm_cached = sum(
        1 for r in second_rows
        if r["classification_source"] != "llm" and r["cached"]
    )
    v.check(non_llm_cached == 0,
            f"S10 Non-LLM rows have cached=0 (violations: {non_llm_cached})")

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
    """Verify that too-new emails were classified but NOT moved (skip_reason=too_new).

    Checks both the fixture E4 email ("too new" in subject) and the freshly-
    injected age-gate email ("age gate" in subject).  Both should be blocked
    as too_new at step 1 time.
    """
    v = VerificationResult()
    print(f"\n=== Verifying Too-New Blocked ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()

    found_too_new = False
    found_age_gate = False
    for r in rows:
        subject = r["subject"] or ""
        # E4: always-too-new fixture email
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
        # Age-gate email: injected fresh with received_at=now
        if "age gate" in subject.lower():
            found_age_gate = True
            v.check(
                r["skip_reason"] == "too_new",
                f"Age-gate email blocked (skip_reason={r['skip_reason']!r}, subject={subject[:50]})",
            )
            v.check(
                not r["moved"],
                f"Age-gate email was NOT moved (moved={r['moved']}, subject={subject[:50]})",
            )

    v.check(found_too_new, "At least one too-new email found in run")
    v.check(found_age_gate, "Age-gate email found in run")

    v.print_report()
    return v


def verify_age_gate(db: Database, run_id: str) -> VerificationResult:
    """Verify age-gate email was moved after waiting.

    The age-gate email is injected fresh by phase_age_gate with received_at=now.
    It has "age gate" in its subject (distinct from E4's "too new" subject).
    """
    v = VerificationResult()
    print(f"\n=== Verifying Age Gate ({run_id[:8]}) ===")

    rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
    ).fetchall()

    found_age_gate = False
    for r in rows:
        subject = r["subject"] or ""
        if "age gate" in subject.lower():
            found_age_gate = True
            v.check(
                r["skip_reason"] != "too_new",
                f"Age-gate email is now eligible (skip_reason={r['skip_reason']!r})",
            )
            v.check(
                bool(r["moved"]),
                f"Age-gate email was moved (moved={r['moved']})",
            )

    v.check(found_age_gate, "Age-gate email appeared in post-timer run")

    v.print_report()
    return v


def verify_learning_step1(
    db: Database,
    run_id: str,
    *,
    l1_email_id: str | None,
    l3_email_id: str | None,
    l3_rule_id: int | None,
    l4_email_id: str | None,
    l4_rule_id: int | None,
    l5_email_id: str | None,
    pre_rules: dict[int, dict],
) -> VerificationResult:
    """Verify L1, L3, L4, L5 after the first learning run."""
    v = VerificationResult()
    print(f"\n=== Verifying Learning Step 1 ({run_id[:8]}) — L1, L3, L4, L5 ===")

    # Helper: check if a manual audit row exists for an email in this run
    def has_manual_row(email_id: str, expected_folder_fragment: str | None = None) -> bool:
        rows = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? AND classification_source = 'manual'",
            (run_id, email_id),
        ).fetchall()
        if not rows:
            return False
        if expected_folder_fragment:
            return any(expected_folder_fragment.lower() in (r["target_folder"] or "").lower() for r in rows)
        return True

    # --- L1: Skipped email sorted by user (Category 1) ---
    if l1_email_id:
        v.check(
            has_manual_row(l1_email_id, "Banks"),
            f"L1: manual audit row for ambiguous-service → Banks (email {l1_email_id[:12]})",
        )
    else:
        v.warn("L1: skipped (ambiguous-service email not found)")

    # --- L3: Rule-based correction (Category 2) ---
    # With computed confidence model: correction is recorded as classification_source='correction',
    # confidence is recomputed (not directly penalized), rule may stay active.
    if l3_email_id:
        # Check for correction row (not 'manual' — corrections now use 'correction' source)
        corr_rows = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? AND classification_source = 'correction'",
            (run_id, l3_email_id),
        ).fetchall()
        v.check(
            len(corr_rows) > 0,
            f"L3: correction audit row for chase (email {l3_email_id[:12]}, found {len(corr_rows)} rows)",
        )

        # After compute_rule_confidence: confidence should have dropped from original
        if l3_rule_id:
            rule = db.execute("SELECT * FROM rules WHERE id = ?", (l3_rule_id,)).fetchone()
            pre = pre_rules.get(l3_rule_id, {})
            if rule and pre:
                v.check(
                    rule["confidence"] < pre["confidence"],
                    f"L3: chase rule confidence dropped ({pre['confidence']:.2f} → {rule['confidence']:.2f})",
                )
                # Rule stays active (above deactivation_threshold 0.50) but may be below rule_move
                v.check(
                    rule["confidence"] > 0.50 or not rule["active"],
                    f"L3: chase rule above deactivation or deactivated (conf={rule['confidence']:.2f}, active={rule['active']})",
                )
            else:
                v.check(False, f"L3: chase rule {l3_rule_id} not found")
    else:
        v.warn("L3: skipped (chase email not found)")

    # --- L4: Inbox return ignored (Category 2 — negative) ---
    if l4_email_id:
        # Should NOT have a manual row — inbox returns are ignored
        has_row = has_manual_row(l4_email_id)
        v.check(
            not has_row,
            f"L4: NO manual row for megastore alerts inbox return (email {l4_email_id[:12]}, has_row={has_row})",
        )

        # Rule should be unchanged
        if l4_rule_id:
            rule = db.execute("SELECT * FROM rules WHERE id = ?", (l4_rule_id,)).fetchone()
            pre = pre_rules.get(l4_rule_id, {})
            if rule and pre:
                v.check(
                    rule["confidence"] == pre["confidence"],
                    f"L4: megastore alerts rule confidence unchanged ({rule['confidence']:.2f}, was {pre['confidence']:.2f})",
                )
                v.check(
                    bool(rule["active"]) == bool(pre["active"]),
                    f"L4: megastore alerts rule still active={rule['active']}",
                )
    else:
        v.warn("L4: skipped (megastore alerts email not found)")

    # --- L5: LLM-based correction (Category 2) ---
    # In the computed confidence model, _detect_correction_sorts catches all
    # non-manual/non-correction moves (including LLM). The relocation is
    # recorded as classification_source='correction' (not 'manual').
    if l5_email_id:
        corr_rows = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? AND classification_source = 'correction'",
            (run_id, l5_email_id),
        ).fetchall()
        has_corr = any("Banks" in (r["target_folder"] or "") for r in corr_rows) if corr_rows else False
        v.check(
            has_corr,
            f"L5: correction audit row for megastore returns → Banks (email {l5_email_id[:12]})",
        )

        # With computed confidence model, compute_rule_confidence updates all active rules.
        # We only verify that rules without corrections didn't drop significantly.
        for rule_id, pre in pre_rules.items():
            if rule_id == l3_rule_id:
                continue  # L3 intentionally changed this one
            current = db.execute("SELECT confidence FROM rules WHERE id = ?", (rule_id,)).fetchone()
            if current:
                drop = pre["confidence"] - current["confidence"]
                v.check(
                    drop < 0.10,
                    f"L5: rule {rule_id} confidence didn't drop significantly "
                    f"({pre['confidence']:.2f} → {current['confidence']:.2f})",
                )
    else:
        v.warn("L5: skipped (megastore returns email not found)")

    v.print_report()
    return v


def verify_learning_step2(
    db: Database,
    run_id: str,
    *,
    l3_email_id: str | None,
) -> VerificationResult:
    """Verify L6 (dedup) and L9 (deactivated rule) after the second learning run."""
    v = VerificationResult()
    print(f"\n=== Verifying Learning Step 2 ({run_id[:8]}) — L6, L9 ===")

    # --- L6: Dedup — same correction not double-counted ---
    if l3_email_id:
        # No new correction rows for the same email in the second run
        corr_rows = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? "
            "AND classification_source IN ('manual', 'correction')",
            (run_id, l3_email_id),
        ).fetchall()
        v.check(
            len(corr_rows) == 0,
            f"L6: no new correction rows for chase in second run (got {len(corr_rows)})",
        )

        # Chase rule confidence should be stable (idempotent — compute doesn't change on re-run)
        chase_rule = db.execute(
            "SELECT * FROM rules WHERE condition_value = 'noreply@chase.com' AND rule_type = 'exact_sender'"
        ).fetchone()
        if chase_rule:
            # Confidence was already computed in step 1; should not change further
            v.check(
                chase_rule["confidence"] < 0.95,
                f"L6: chase rule confidence reduced from original (got {chase_rule['confidence']:.2f})",
            )
    else:
        v.warn("L6: skipped (chase email not available)")

    # --- L9: Rule with low confidence doesn't fire ---
    # With computed confidence model, the chase rule may still be active but with
    # confidence below rule_move threshold (0.85). If active, it shouldn't be used
    # as classification source because its confidence is too low.
    chase_rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'noreply@chase.com' AND rule_type = 'exact_sender'"
    ).fetchone()
    if chase_rule:
        if chase_rule["active"]:
            v.check(
                chase_rule["confidence"] < 0.85,
                f"L9: chase rule confidence below rule_move threshold "
                f"(conf={chase_rule['confidence']:.2f}, threshold=0.85)",
            )
        else:
            v.check(
                chase_rule["confidence"] < 0.50,
                f"L9: chase rule deactivated below threshold (conf={chase_rule['confidence']:.2f})",
            )
    else:
        v.warn("L9: chase rule not found")

    v.print_report()
    return v


def verify_learning_step3(
    db: Database,
    run_id: str,
    *,
    chase_rule_id: int | None,
    l3a1_email_id: str,
    l3a2_email_id: str,
) -> VerificationResult:
    """Verify L3a ('3 strikes') and L9 after batch 2 corrections."""
    v = VerificationResult()
    print(f"\n=== Verifying Learning Step 3 ({run_id[:8]}) — L3a, L9 ===")

    # --- L3a: 3 total correction rows for chase rule ---
    if chase_rule_id:
        total_corrections = db.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE classification_source = 'correction' AND rule_id = ?",
            (chase_rule_id,),
        ).fetchone()[0]
        v.check(
            total_corrections >= 3,
            f"L3a: 3+ total correction rows for chase rule (got {total_corrections})",
        )

        # New correction rows for L3a-1 and L3a-2 in this run
        l3a1_corr = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? "
            "AND classification_source = 'correction'",
            (run_id, l3a1_email_id),
        ).fetchall()
        v.check(
            len(l3a1_corr) > 0,
            f"L3a: correction row for L3a-1 chase email (email {l3a1_email_id[:12]})",
        )

        l3a2_corr = db.execute(
            "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? "
            "AND classification_source = 'correction'",
            (run_id, l3a2_email_id),
        ).fetchall()
        v.check(
            len(l3a2_corr) > 0,
            f"L3a: correction row for L3a-2 chase email (email {l3a2_email_id[:12]})",
        )

        # Chase rule confidence should be well below rule_move (0.85)
        chase_rule = db.execute(
            "SELECT * FROM rules WHERE id = ?", (chase_rule_id,)
        ).fetchone()
        if chase_rule:
            conf = chase_rule["confidence"]
            v.check(
                conf < 0.85,
                f"L3a: chase confidence below rule_move "
                f"(conf={conf:.2f}, threshold=0.85)",
            )
            v.check(
                bool(chase_rule["active"]),
                f"L3a: chase rule still active (conf={conf:.2f} > deactivation 0.50)",
            )
            # Store for L14 comparison
            v.metadata["chase_confidence"] = conf
        else:
            v.check(False, f"L3a: chase rule {chase_rule_id} not found")
    else:
        v.warn("L3a: skipped (chase rule_id not available)")

    # --- L9: chase rule below rule_move means it won't fire ---
    chase_rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'noreply@chase.com' "
        "AND rule_type = 'exact_sender'"
    ).fetchone()
    if chase_rule:
        v.check(
            chase_rule["confidence"] < 0.85,
            f"L9: chase rule won't fire — confidence below rule_move "
            f"(conf={chase_rule['confidence']:.2f}, threshold=0.85)",
        )
    else:
        v.warn("L9: chase rule not found")

    v.print_report()
    return v


def verify_learning_step4(
    db: Database,
    run_id: str,
    *,
    chase_rule_id: int | None,
    l3a1_email_id: str,
    pre_l3a_confidence: float | None,
) -> VerificationResult:
    """Verify L14 (sort-back recovery) — confirming sort detected, confidence recovers."""
    v = VerificationResult()
    print(f"\n=== Verifying Learning Step 4 ({run_id[:8]}) — L14 ===")

    # --- L14: Confirming sort detected ---
    # The L3a-1 email was moved back to Banks. It could be detected as:
    # - Cat 2 correction (if it had a prior rule/LLM move to Children)
    # - Cat 1 skipped sort (if it was in a non-inbox folder and user moved it)
    # Either way, a manual or correction row should exist for Banks.
    manual_rows = db.execute(
        "SELECT * FROM audit_log WHERE run_id = ? AND email_id = ? "
        "AND classification_source IN ('manual', 'correction')",
        (run_id, l3a1_email_id),
    ).fetchall()
    banks_rows = [r for r in manual_rows if "Banks" in (r["target_folder"] or "")]
    v.check(
        len(banks_rows) > 0,
        f"L14: confirming sort detected for L3a-1 → Banks "
        f"(email {l3a1_email_id[:12]}, found {len(banks_rows)} rows)",
    )

    # --- L14: Net corrections decreased, confidence partially recovered ---
    if chase_rule_id:
        chase_rule = db.execute(
            "SELECT * FROM rules WHERE id = ?", (chase_rule_id,)
        ).fetchone()
        if chase_rule:
            conf = chase_rule["confidence"]
            if pre_l3a_confidence is not None:
                v.check(
                    conf > pre_l3a_confidence,
                    f"L14: chase confidence recovered from L3a "
                    f"({pre_l3a_confidence:.2f} → {conf:.2f})",
                )
            else:
                v.warn("L14: no pre-L3a confidence available for comparison")

            # Should still be below rule_move (recovery is partial: 3 corr - 1 confirm = 2 net)
            v.check(
                conf < 0.85,
                f"L14: chase still below rule_move after partial recovery "
                f"(conf={conf:.2f})",
            )
            v.check(
                bool(chase_rule["active"]),
                f"L14: chase rule still active (conf={conf:.2f})",
            )
        else:
            v.check(False, f"L14: chase rule {chase_rule_id} not found")
    else:
        v.warn("L14: skipped (chase rule_id not available)")

    v.print_report()
    return v


def verify_learning_step5(db: Database) -> VerificationResult:
    """Verify L17 — manual rule exemption from computed confidence."""
    v = VerificationResult()
    print("\n=== Verifying Learning Step 5 — L17 (manual rule exemption) ===")

    manual_rule = db.execute(
        "SELECT * FROM rules WHERE condition_value = 'admin@lincolnelementary.org' "
        "AND source = 'manual'"
    ).fetchone()
    if manual_rule:
        v.check(
            manual_rule["confidence"] == 1.0,
            f"L17: manual rule confidence unchanged "
            f"(got {manual_rule['confidence']:.2f}, expected 1.0)",
        )
        v.check(
            bool(manual_rule["active"]),
            "L17: manual rule still active",
        )
        v.check(
            manual_rule["source"] == "manual",
            f"L17: rule source is 'manual' (got '{manual_rule['source']}')",
        )
    else:
        v.check(False, "L17: manual rule for admin@lincolnelementary.org not found")

    v.print_report()
    return v
