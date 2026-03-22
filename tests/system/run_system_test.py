"""System test orchestrator: runs the full end-to-end test sequence.

Usage:
    # Full test sequence
    python tests/system/run_system_test.py --config config.test.yaml --to-email test@fastmail.com

    # Setup only (for interactive development)
    python tests/system/run_system_test.py --config config.test.yaml --to-email test@fastmail.com --setup-only

    # Cleanup
    python tests/system/run_system_test.py --config config.test.yaml --to-email test@fastmail.com --cleanup
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
    cmd = f"mailsort --config {config_abs} {command}"
    logger.info("Running: %s", cmd)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=str(_PROJECT_ROOT),
    )
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            logger.info("  stdout: %s", line)
    if result.returncode != 0:
        logger.error("Command failed (exit %d):\n%s", result.returncode, result.stderr)
    return result


def phase_setup(config: str, to_email: str) -> bool:
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
        # Ensure required folders exist (create if missing)
        required = ["Affairs/Banks", "Affairs/Stores", "People/Children"]
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


def phase_age_gate(config: str) -> tuple[bool, str]:
    """Phase 4: Verify age gate blocks too-new emails, then verify they move after waiting."""
    print("\n" + "=" * 60)
    print("Phase 4: Age Gate Test")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    min_age = cfg.get("scheduler", {}).get("min_age_minutes", 1)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations
    from tests.system.verify_results import verify_too_new_blocked, verify_age_gate

    # --- Step 1: Run live BEFORE the age window expires ---
    print("\n  Step 1: Running live pass (emails should be too new)...")
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


def phase_correction(config: str, to_email: str) -> bool:
    """Phase 6: Simulate user correction and verify detection."""
    print("\n" + "=" * 60)
    print("Phase 6: User Correction Simulation")
    print("=" * 60)

    import yaml
    with open(config) as f:
        cfg = yaml.safe_load(f)
    db_path = cfg.get("db_path", "data/test.db")

    from mailsort.db.database import Database
    from mailsort.db.migrations import run_migrations

    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        # Find a moved email to "correct"
        moved_row = db.execute(
            "SELECT email_id, target_folder, rule_id FROM audit_log "
            "WHERE moved = 1 AND classification_source = 'rule' AND rule_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not moved_row:
            print("  WARN: No rule-moved emails found to correct. Skipping correction test.")
            return True

        email_id = moved_row["email_id"]
        original_folder = moved_row["target_folder"]
        rule_id = moved_row["rule_id"]
        print(f"  Will correct email {email_id[:12]}... from {original_folder}")
    finally:
        db.close()

    # Move the email to a different folder via JMAP
    token = os.environ.get("FASTMAIL_API_TOKEN", "")
    session_url = cfg.get("fastmail", {}).get("session_url", "https://api.fastmail.com/jmap/session")

    from tests.system.load_fixtures import JMAPLoader
    loader = JMAPLoader(token, session_url)
    try:
        folder_map = loader.resolve_folder_paths()

        # Pick a different folder
        if "Stores" in original_folder or "stores" in original_folder.lower():
            correction_folder = "Affairs/Banks"
        else:
            correction_folder = "Affairs/Stores"

        correction_id = folder_map.get(correction_folder) or folder_map.get(f"INBOX/{correction_folder}")
        if not correction_id:
            print(f"  ERROR: Could not find correction folder {correction_folder}")
            return False

        # Move email via JMAP
        data = loader.call([
            ["Email/get", {
                "accountId": loader.account_id,
                "ids": [email_id],
                "properties": ["mailboxIds"],
            }, "g1"],
        ])
        email_data = data["methodResponses"][0][1].get("list", [])
        if not email_data:
            print(f"  WARN: Email {email_id[:12]} not found in JMAP (may have been deleted)")
            return True

        new_mailbox_ids = {correction_id: True}
        loader.call([
            ["Email/set", {
                "accountId": loader.account_id,
                "update": {
                    email_id: {"mailboxIds": new_mailbox_ids},
                },
            }, "s1"],
        ])
        print(f"  Moved email to {correction_folder} (simulating user correction)")
    finally:
        loader.close()

    # Run again to detect correction
    result = run_mailsort("run", config)
    if result.returncode != 0:
        print("  ERROR: Post-correction run failed")
        return False

    # Verify
    db = Database(db_path)
    db.connect()
    try:
        run_migrations(db)
        row = db.execute(
            "SELECT run_id FROM runs WHERE trigger != 'bootstrap' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        run_id = row["run_id"] if row else ""

        from tests.system.verify_results import verify_correction
        v = verify_correction(db, run_id, "")
        return v.failed == 0
    finally:
        db.close()


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
    parser.add_argument("--config", default="config.test.yaml", help="Path to test config")
    parser.add_argument("--to-email", required=True, help="Test account email address")
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
    age_ok, live_run_id = phase_age_gate(args.config)
    if not age_ok:
        print("\nAGE GATE TEST FAILED — continuing anyway")

    # Phase 5: Live Run Verification
    if live_run_id:
        phase_live_verify(args.config, live_run_id)

    # Phase 6: User Correction
    phase_correction(args.config, args.to_email)

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
