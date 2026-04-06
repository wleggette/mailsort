"""System test orchestrator: runs the full end-to-end test sequence.

Usage:
    # Full test sequence (run from project root)
    python tests/system/run_system_test.py

    # Setup only (for interactive development)
    python tests/system/run_system_test.py --setup-only

    # Cleanup
    python tests/system/run_system_test.py --cleanup
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path so mailsort and tests.system are importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

SYSTEM_DIR = Path(__file__).parent


def run_mailsort(command: str, config: str) -> subprocess.CompletedProcess:
    """Run a mailsort CLI command and return the result."""
    config_abs = str(Path(config).resolve())
    mailsort_bin = str(Path(sys.executable).parent / "mailsort")
    cmd = [mailsort_bin, "--config", config_abs] + command.split()
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(_PROJECT_ROOT),
    )
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            logger.info("  stdout: %s", line)
    if result.returncode != 0:
        logger.error("Command failed (exit %d):\n%s", result.returncode, result.stderr)
    return result


def phase_setup(config: str, to_email: str | None) -> bool:
    """Phase 1: Load fixtures and inbox emails into test account."""
    print("\n" + "=" * 60)
    print("Phase 1: Setup — Loading test data")
    print("=" * 60)

    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    if not token:
        print("ERROR: FASTMAIL_API_TOKEN not set", file=sys.stderr)
        return False

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")

    from tests.system.load_fixtures import JMAPLoader, load_folder_fixtures, load_inbox_emails, TEST_CONTACTS
    from tests.system.generate_inbox_emails import generate_inbox_emails

    loader = JMAPLoader(token, session_url)

    try:
        # Auto-detect email from JMAP session if not provided
        to_email = to_email or loader.account_email
        print(f"  Using recipient address: {to_email}")
        # Ensure required folders exist (create if missing)
        required = ["Affairs/Banks", "Affairs/Stores", "Affairs/Medical", "People/Children"]
        for folder_path in required:
            loader.ensure_folder_path(folder_path)
        folder_map = loader.resolve_folder_paths()
        print(f"  Folders verified: {len(folder_map)} mailboxes")

        # Create test contacts (CI1)
        contacts_created = loader.create_contacts(TEST_CONTACTS)
        print(f"  Test contacts: {contacts_created} created")

        # Load static fixtures
        fixtures_path = SYSTEM_DIR / "fixtures" / "folder_emails.json"
        folder_count = load_folder_fixtures(loader, to_email, fixtures_path)
        print(f"  Folder fixtures: {folder_count} emails loaded")

        # Load dynamic inbox emails
        inbox_emails = generate_inbox_emails()
        inbox_count = load_inbox_emails(loader, to_email, inbox_emails)
        print(f"  Inbox emails: {inbox_count} emails loaded")

        return True
    finally:
        loader.close()


def phase_bootstrap(config: str) -> bool:
    """Phase 2: Run bootstrap and verify results.

    Sub-phases:
      1. No-LLM pre-flight (D3): bootstrap without API key, verify fallback descriptions
      2. Normal bootstrap: full bootstrap with LLM
      3. Idempotency re-run (F5): bootstrap again, verify 0 new rows
    """
    print("\n" + "=" * 60)
    print("Phase 2: Bootstrap")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.verify_results import verify_bootstrap, verify_bootstrap_idempotency

    # --- Step 1: No-LLM pre-flight (D3) ---
    print("\n  Step 1: No-LLM pre-flight (D3)...")
    if os.path.exists(db_path):
        os.remove(db_path)

    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        result = run_mailsort("bootstrap", config)
        if result.returncode != 0:
            print("  ERROR: No-LLM bootstrap failed")
            return False

        # Verify all descriptions are fallback (no LLM)
        db = Database(db_path)
        db.connect()
        try:
            run_migrations(db)
            descs = db.execute("SELECT * FROM folder_descriptions").fetchall()
            all_fallback = all(
                d["description"].startswith("Emails filed under ")
                for d in descs
                if d["source"] == "auto"
            )
            if all_fallback:
                print(f"  D3 PASS: all {len(descs)} auto descriptions use fallback")
            else:
                print(f"  D3 FAIL: some descriptions are not fallback")
                return False
        finally:
            db.close()
    finally:
        if saved_key:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    # Wipe DB for the real bootstrap
    if os.path.exists(db_path):
        os.remove(db_path)
        print("  Wiped DB after no-LLM pre-flight")

    # --- Step 2: Normal bootstrap ---
    print("\n  Step 2: Normal bootstrap...")
    result = run_mailsort("bootstrap", config)
    if result.returncode != 0:
        print("  ERROR: Bootstrap failed")
        return False

    # Verify
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        v = verify_bootstrap(db)
        if v.failed > 0:
            return False
    finally:
        db.close()

    # --- Step 3: Idempotency re-run (F5) ---
    print("\n  Step 3: Idempotency re-run (F5)...")
    result = run_mailsort("bootstrap", config)
    if result.returncode != 0:
        print("  ERROR: Second bootstrap failed")
        return False

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        v = verify_bootstrap_idempotency(db)
        return v.failed == 0
    finally:
        db.close()


def phase_dry_run(config: str) -> tuple[bool, str]:
    """Phase 3: Dry run and verify. Returns (success, run_id)."""
    print("\n" + "=" * 60)
    print("Phase 3: Dry Run")
    print("=" * 60)

    result = run_mailsort("dry-run", config)
    if result.returncode != 0:
        print("  ERROR: Dry run failed")
        return False, ""

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.verify_results import verify_dry_run

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        # Get the latest non-bootstrap run
        row = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("  ERROR: No run found in database")
            return False, ""
        run_id = row["run_id"]
        v = verify_dry_run(db, run_id)
        return v.failed == 0, run_id
    finally:
        db.close()


def phase_age_gate(config: str, to_email: str = "") -> tuple[bool, str]:
    """Phase 4: Verify age gate blocks too-new emails, then verify they move after waiting.

    Injects a fresh "age gate" email with received_at=now right before step 1.
    This avoids timing issues with fixture emails loaded minutes earlier at setup.
    """
    print("\n" + "=" * 60)
    print("Phase 4: Age Gate Test")
    print("=" * 60)

    import yaml
    from datetime import datetime, timezone
    from tests.system.load_fixtures import JMAPLoader, build_rfc5322

    with open(config) as f:
        cfg = yaml.safe_load(f)
    min_age = cfg.get("scheduler", {}).get("min_age_minutes", 1)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.verify_results import verify_too_new_blocked, verify_age_gate

    # --- Step 0: Inject a fresh age-gate email (received_at = now) ---
    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")
    loader = JMAPLoader(token, session_url)
    if not to_email:
        to_email = loader.account_email

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d%H%M")
    received_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rfc5322 = build_rfc5322(
        from_name="Bank of America",
        from_email="alerts@bankofamerica.com",
        to_email=to_email,
        subject=f"[TEST] Age gate BoA alert {ts}",
        body="Your account balance has changed.",
        received_at=received_at,
    )
    # Resolve inbox ID
    mailboxes = loader.get_mailboxes()
    inbox_id = None
    for mid, mbox in mailboxes.items():
        if mbox.get("role") == "inbox":
            inbox_id = mid
            break
    if not inbox_id:
        print("  ERROR: Could not find inbox mailbox")
        return False, ""

    blob_id = loader.upload_blob(rfc5322)
    email_id = loader.import_email(
        blob_id,
        inbox_id,
        keywords={"$seen": True, "$mailsort-test": True},
        received_at=received_at,
    )
    print(f"  Injected age-gate email (received_at={received_at}, id={email_id})")

    # --- Step 1: Run live BEFORE the age window expires ---
    print("\n  Step 1: Running live pass (age-gate email should be too new)...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Pre-timer live run failed")
        return False, ""

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        row = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        pre_run_id = row["run_id"] if row else ""
        v_blocked = verify_too_new_blocked(db, pre_run_id)
        v_blocked.print_report()
        if v_blocked.failed > 0:
            print("  FAIL: Too-new emails should NOT have been moved")
            return False, pre_run_id
    finally:
        db.close()

    # --- Step 2: Wait for min_age_minutes to elapse ---
    print(f"\n  Step 2: Waiting {min_age} minute(s) for age gate to expire...")
    time.sleep(min_age * 60 + 5)  # wait min_age + 5 seconds buffer
    print("  Wait complete.")

    # --- Step 3: Run live AFTER the age window expires ---
    print("\n  Step 3: Running live pass (emails should now be eligible)...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Post-timer live run failed")
        return False, ""

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        row = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        post_run_id = row["run_id"] if row else ""
        v_moved = verify_age_gate(db, post_run_id)
        return v_moved.failed == 0, post_run_id
    finally:
        db.close()


def phase_live_verify(config: str, run_id: str) -> bool:
    """Phase 5: Verify live run results."""
    print("\n" + "=" * 60)
    print("Phase 5: Live Run Verification")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.verify_results import verify_live_run

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        v = verify_live_run(db, run_id)
        return v.failed == 0
    finally:
        db.close()


def phase_learning(config: str, to_email: str) -> bool:
    """Phase 4: Learning & Feedback — L1, L3, L3a, L4, L5, L6, L9, L14, L17.

    Batch 1 (single correction + skipped sort):
      Step 1: Make 4 JMAP moves simulating user actions.
      Step 2: Run mailsort → learning detects the moves.
      Step 3: Verify L1, L3, L4, L5.
    Dedup pass:
      Step 4: Run mailsort again → tests L6 (dedup).
      Step 5: Verify L6.
    Batch 2 ("3 strikes"):
      Step 6: Make 2 more JMAP corrections (L3a).
      Step 7: Run mailsort.
      Step 8: Verify L3a, L9.
    Sort-back recovery:
      Step 9: Move chase L3a-1 back to Banks (L14 confirming sort).
      Step 10: Run mailsort.
      Step 11: Verify L14.
    Manual rule exemption:
      Step 12: Verify L17.
    """
    print("\n" + "=" * 60)
    print("Phase 4: Learning & Feedback (L1, L3, L3a, L4, L5, L6, L9, L14, L17)")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.load_fixtures import JMAPLoader

    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")

    # ------------------------------------------------------------------
    # Find the emails we need to move
    # ------------------------------------------------------------------
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)

        # L3: chase rule-moved email (for correction Banks → Stores)
        l3_row = db.execute(
            "SELECT email_id, target_folder, rule_id FROM audit_log "
            "WHERE moved = 1 AND from_address = 'noreply@chase.com' "
            "AND classification_source = 'rule' AND rule_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # L4: megastore alerts rule-moved email (for inbox return)
        l4_row = db.execute(
            "SELECT email_id, target_folder, rule_id FROM audit_log "
            "WHERE moved = 1 AND from_address = 'alerts@megastore.com' "
            "AND classification_source = 'rule' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # L5: megastore returns LLM-moved email (for LLM correction)
        l5_row = db.execute(
            "SELECT email_id, target_folder, rule_id FROM audit_log "
            "WHERE moved = 1 AND from_address = 'returns@megastore.com' "
            "AND classification_source = 'llm' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # L1: ambiguous-service skipped email (for skipped sort)
        l1_row = db.execute(
            "SELECT email_id, target_folder FROM audit_log "
            "WHERE moved = 0 AND from_address = 'info@ambiguous-service.com' "
            "AND skip_reason = 'below_threshold' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # Snapshot rule confidences before moves
        pre_rules = {
            r["id"]: {"confidence": r["confidence"], "active": r["active"]}
            for r in db.execute("SELECT id, confidence, active FROM rules").fetchall()
        }
    finally:
        db.close()

    # Check we found all needed emails
    missing = []
    if not l3_row:
        missing.append("L3 (chase rule-moved)")
    if not l4_row:
        missing.append("L4 (megastore alerts rule-moved)")
    if not l5_row:
        missing.append("L5 (megastore returns LLM-moved)")
    if not l1_row:
        missing.append("L1 (ambiguous-service skipped)")
    if missing:
        print(f"  WARN: Missing emails for: {', '.join(missing)}")
        print("  Continuing with available scenarios...")

    # ------------------------------------------------------------------
    # Step 1: Make JMAP moves
    # ------------------------------------------------------------------
    print("\n  Step 1: Simulating user actions via JMAP moves...")
    loader = JMAPLoader(token, session_url)
    try:
        folder_map = loader.resolve_folder_paths()

        def resolve_folder(name: str) -> str | None:
            return (folder_map.get(name)
                    or folder_map.get(f"INBOX/{name}")
                    or folder_map.get(f"Inbox/{name}"))

        stores_id = resolve_folder("Affairs/Stores")
        banks_id = resolve_folder("Affairs/Banks")
        inbox_id = resolve_folder("INBOX")
        # INBOX might not be in folder_map — get it from the tree
        if not inbox_id:
            mailboxes = loader.get_mailboxes()
            for mid, mbox in mailboxes.items():
                if mbox.get("role") == "inbox":
                    inbox_id = mid
                    break

        def move_email(email_id: str, target_id: str, label: str) -> bool:
            try:
                loader.call([
                    ["Email/set", {
                        "accountId": loader.account_id,
                        "update": {email_id: {"mailboxIds": {target_id: True}}},
                    }, "s1"],
                ])
                print(f"    {label}: moved {email_id[:12]}...")
                return True
            except Exception as e:
                print(f"    {label}: FAILED — {e}")
                return False

        if l3_row and stores_id:
            move_email(l3_row["email_id"], stores_id, "L3 chase → Stores")
        if l4_row and inbox_id:
            move_email(l4_row["email_id"], inbox_id, "L4 megastore alerts → INBOX")
        if l5_row and banks_id:
            move_email(l5_row["email_id"], banks_id, "L5 megastore returns → Banks")
        if l1_row and banks_id:
            move_email(l1_row["email_id"], banks_id, "L1 ambiguous → Banks")
    finally:
        loader.close()

    # ------------------------------------------------------------------
    # Step 2: Run mailsort to detect the moves
    # ------------------------------------------------------------------
    print("\n  Step 2: Running mailsort to detect learning...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Post-correction run failed")
        return False

    # ------------------------------------------------------------------
    # Step 3: Verify L1, L3, L4, L5
    # ------------------------------------------------------------------
    print("\n  Step 3: Verifying L1, L3, L4, L5...")
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        run1_id = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()["run_id"]

        from tests.system.verify_results import verify_learning_step1
        v1 = verify_learning_step1(
            db, run1_id,
            l1_email_id=l1_row["email_id"] if l1_row else None,
            l3_email_id=l3_row["email_id"] if l3_row else None,
            l3_rule_id=l3_row["rule_id"] if l3_row else None,
            l4_email_id=l4_row["email_id"] if l4_row else None,
            l4_rule_id=l4_row["rule_id"] if l4_row else None,
            l5_email_id=l5_row["email_id"] if l5_row else None,
            pre_rules=pre_rules,
        )
        if v1.failed > 0:
            print("  Step 3 had failures — continuing to step 4")
    finally:
        db.close()

    # ------------------------------------------------------------------
    # Step 4: Run again for L6 (dedup) and L9 (deactivated rule)
    # ------------------------------------------------------------------
    print("\n  Step 4: Running mailsort again (no new moves — tests dedup + deactivation)...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Second post-correction run failed")
        return False

    # ------------------------------------------------------------------
    # Step 5: Verify L6, L9
    # ------------------------------------------------------------------
    print("\n  Step 5: Verifying L6, L9...")
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        run2_id = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()["run_id"]

        from tests.system.verify_results import verify_learning_step2
        v2 = verify_learning_step2(
            db, run2_id,
            l3_email_id=l3_row["email_id"] if l3_row else None,
        )
    finally:
        db.close()

    # ------------------------------------------------------------------
    # Step 6: Batch 2 — two more chase corrections ("3 strikes", L3a)
    # ------------------------------------------------------------------
    # Find 2 more chase rule-moved emails (L3a-1, L3a-2)
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        l3a_rows = db.execute(
            "SELECT email_id, target_folder, rule_id FROM audit_log "
            "WHERE moved = 1 AND from_address = 'noreply@chase.com' "
            "AND classification_source = 'rule' AND rule_id IS NOT NULL "
            "AND email_id != ? "
            "ORDER BY created_at DESC LIMIT 2",
            (l3_row["email_id"] if l3_row else "",),
        ).fetchall()
        chase_rule_id = l3_row["rule_id"] if l3_row else None
    finally:
        db.close()

    if len(l3a_rows) < 2:
        print(f"  WARN: Only found {len(l3a_rows)} additional chase rule-moved emails for L3a (need 2)")
        print("  Skipping Batch 2, sort-back, and L17 checks")
        return v1.failed == 0 and v2.failed == 0

    l3a1_email_id = l3a_rows[0]["email_id"]
    l3a2_email_id = l3a_rows[1]["email_id"]

    print(f"\n  Step 6: Simulating 2 more chase corrections (L3a)...")
    loader = JMAPLoader(token, session_url)
    try:
        folder_map = loader.resolve_folder_paths()

        def resolve_folder(name: str) -> str | None:
            return (folder_map.get(name)
                    or folder_map.get(f"INBOX/{name}")
                    or folder_map.get(f"Inbox/{name}"))

        children_id = resolve_folder("People/Children")
        stores_id = resolve_folder("Affairs/Stores")

        def move_email(email_id: str, target_id: str, label: str) -> bool:
            try:
                loader.call([
                    ["Email/set", {
                        "accountId": loader.account_id,
                        "update": {email_id: {"mailboxIds": {target_id: True}}},
                    }, "s1"],
                ])
                print(f"    {label}: moved {email_id[:12]}...")
                return True
            except Exception as e:
                print(f"    {label}: FAILED — {e}")
                return False

        if children_id:
            move_email(l3a1_email_id, children_id, "L3a-1 chase → Children")
        if stores_id:
            move_email(l3a2_email_id, stores_id, "L3a-2 chase → Stores")
    finally:
        loader.close()

    # ------------------------------------------------------------------
    # Step 7: Run mailsort to detect batch 2 corrections
    # ------------------------------------------------------------------
    print("\n  Step 7: Running mailsort (detect batch 2 corrections)...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Batch 2 run failed")
        return False

    # ------------------------------------------------------------------
    # Step 8: Verify L3a, L9
    # ------------------------------------------------------------------
    print("\n  Step 8: Verifying L3a, L9...")
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        run3_id = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()["run_id"]

        from tests.system.verify_results import verify_learning_step3
        v3 = verify_learning_step3(
            db, run3_id,
            chase_rule_id=chase_rule_id,
            l3a1_email_id=l3a1_email_id,
            l3a2_email_id=l3a2_email_id,
        )
        if v3.failed > 0:
            print("  Step 8 had failures — continuing to step 9")
    finally:
        db.close()

    # ------------------------------------------------------------------
    # Step 9: Sort-back recovery (L14) — move L3a-1 back to Banks
    # ------------------------------------------------------------------
    print(f"\n  Step 9: Sort-back — moving L3a-1 chase email back to Banks (L14)...")
    loader = JMAPLoader(token, session_url)
    try:
        folder_map = loader.resolve_folder_paths()
        banks_id = (folder_map.get("Affairs/Banks")
                    or folder_map.get("INBOX/Affairs/Banks")
                    or folder_map.get("Inbox/Affairs/Banks"))
        if banks_id:
            try:
                loader.call([
                    ["Email/set", {
                        "accountId": loader.account_id,
                        "update": {l3a1_email_id: {"mailboxIds": {banks_id: True}}},
                    }, "s1"],
                ])
                print(f"    L14 chase L3a-1 → Banks: moved {l3a1_email_id[:12]}...")
            except Exception as e:
                print(f"    L14: FAILED — {e}")
        else:
            print("    L14: WARN — Banks folder not found")
    finally:
        loader.close()

    # ------------------------------------------------------------------
    # Step 10: Run mailsort to detect confirming sort
    # ------------------------------------------------------------------
    print("\n  Step 10: Running mailsort (detect confirming sort)...")
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Sort-back run failed")
        return False

    # ------------------------------------------------------------------
    # Step 11: Verify L14 (sort-back recovery)
    # ------------------------------------------------------------------
    print("\n  Step 11: Verifying L14 (sort-back recovery)...")
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        run4_id = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()["run_id"]

        from tests.system.verify_results import verify_learning_step4
        v4 = verify_learning_step4(
            db, run4_id,
            chase_rule_id=chase_rule_id,
            l3a1_email_id=l3a1_email_id,
            pre_l3a_confidence=v3.metadata.get("chase_confidence") if v3.metadata else None,
        )
    finally:
        db.close()

    # ------------------------------------------------------------------
    # Step 12: Verify L17 (manual rule exemption)
    # ------------------------------------------------------------------
    print("\n  Step 12: Verifying L17 (manual rule exemption)...")
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        from tests.system.verify_results import verify_learning_step5
        v5 = verify_learning_step5(db)
    finally:
        db.close()

    return (v1.failed == 0 and v2.failed == 0
            and v3.failed == 0 and v4.failed == 0 and v5.failed == 0)


def phase_cleanup(config: str) -> bool:
    """Phase 7: Delete test emails and remove test database."""
    print("\n" + "=" * 60)
    print("Phase 7: Cleanup")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)

    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")

    from tests.system.load_fixtures import JMAPLoader, cleanup_test_emails
    loader = JMAPLoader(token, session_url)
    try:
        count = cleanup_test_emails(loader)
        print(f"  Deleted {count} test emails from Fastmail")
    finally:
        loader.close()

    db_path = cfg.get("db_path", "data/test.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"  Removed test database: {db_path}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Run mailsort system tests against a Fastmail test account")
    parser.add_argument("--config", default="tests/system/config.test.yaml", help="Path to test config")
    parser.add_argument("--to-email", default=None, help="Test account email address (auto-detected from JMAP session if omitted)")
    parser.add_argument("--setup-only", action="store_true", help="Only setup (load fixtures + bootstrap), then stop")
    parser.add_argument("--cleanup", "--cleanup-only", action="store_true", help="Only cleanup test data")
    parser.add_argument("--skip-cleanup", action="store_true", help="Skip cleanup phase in full test run")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    if args.cleanup:
        phase_cleanup(args.config)
        return

    # Phase 1: Setup
    if not phase_setup(args.config, args.to_email):
        print("\nSETUP FAILED — aborting")
        sys.exit(1)

    # Phase 2: Bootstrap
    if not phase_bootstrap(args.config):
        print("\nBOOTSTRAP FAILED — aborting")
        sys.exit(1)

    if args.setup_only:
        print("\n" + "=" * 60)
        print("Setup complete. Test data loaded and bootstrap finished.")
        print("=" * 60)
        print(f"\nYou can now:")
        print(f"  mailsort web --config {args.config} --port 8081")
        print(f"  mailsort dry-run --config {args.config}")
        print(f"  mailsort run --config {args.config}")
        return

    # Phase 3: Dry Run
    dry_ok, dry_run_id = phase_dry_run(args.config)
    if not dry_ok:
        print("\nDRY RUN VERIFICATION FAILED — continuing anyway")

    # Phase 4: Age Gate Test
    age_ok, live_run_id = phase_age_gate(args.config, to_email=args.to_email or "")
    if not age_ok:
        print("\nAGE GATE TEST FAILED — continuing anyway")

    # Phase 5: Live Run Verification
    if live_run_id:
        phase_live_verify(args.config, live_run_id)

    # Phase 4: Learning & Feedback
    phase_learning(args.config, args.to_email or "")

    # Phase 7: Cleanup
    if not args.skip_cleanup:
        phase_cleanup(args.config)
    else:
        print("\nSkipping cleanup (--skip-cleanup)")

    print("\n" + "=" * 60)
    print("System test complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
