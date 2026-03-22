# Learning & Auto-Rule Generation

The learning system detects user manual sorts and automatically generates
classification rules from repeated patterns. It also manages rule confidence
through correction penalties and staleness decay.

## Learning from Manual Sorts

The learner (`audit/learner.py`) runs at the start of each scan, before
classification, so that newly learned rules are available for the current batch.
It detects user sorts through four complementary categories:

| Category | What it catches | How it works |
|----------|----------------|--------------|
| **1. Skipped sorts** | Emails mailsort left in inbox that the user moved | Query `audit_log WHERE moved=0` in last 7 days, check current `mailboxIds` |
| **2. Correction sorts** | Emails mailsort moved that the user relocated | Query `audit_log WHERE moved=1`, compare current mailbox to expected |
| **3. Inbox departures** | Emails the user sorted before mailsort processed them | Diff previous inbox snapshot against current inbox IDs; fetch departed emails to see where they went |
| **4. Daily folder scan** | Emails sorted outside any scan window entirely | Sample recent emails from each folder, find ones absent from `audit_log` |

Categories 1–2 handle emails mailsort already knows about. Categories 3–4
close the gap for emails mailsort never saw — the most common case being mail
you read and sort within a few minutes of arrival, before the next poll.

### Inbox Snapshot (Category 3)

On each scan, the orchestrator queries ALL inbox email IDs (no read/flag/age
filter) and stores them in the `inbox_snapshot` table. On the next scan, it
diffs `previous - current - already_processed` to find departures. Snapshots
older than 2 days are cleaned up automatically.

### Daily Folder Scan (Category 4)

Once per 24 hours, the learner samples recent emails from each non-inbox folder
and checks if any are absent from `audit_log`. This catches emails that arrived
and were sorted between two consecutive scans (i.e., never appeared in any
snapshot). The last-scan timestamp is tracked in the `learner_state` table.

### Detection Logic

```python
def detect_manual_sorts(jmap_client, previously_skipped: list[str], run_id: str):
    """Detect user corrections in two categories:

    1. Skipped emails the user moved out of the inbox (we left them, they sorted them).
    2. Mailsort-moved emails the user relocated to a different folder (corrections).

    Both are logged as manual classifications and fed into auto-rule generation.
    """
    # --- Category 1: skipped emails the user moved out of inbox ---
    if previously_skipped:
        emails = jmap_client.get_emails(previously_skipped, ["id", "mailboxIds"])
        for email in emails:
            if inbox_id not in email["mailboxIds"]:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path[new_folder_id]
                _record_manual_sort(run_id, email["id"], new_folder_path)

    # --- Category 2: mailsort-moved emails the user relocated ---
    recent_moves = db.execute("""
        SELECT email_id, target_folder FROM audit_log
        WHERE moved = 1 AND classification_source != 'manual'
          AND created_at >= datetime('now', '-7 days')
    """).fetchall()

    if recent_moves:
        move_ids = [row["email_id"] for row in recent_moves]
        expected_folders = {row["email_id"]: row["target_folder"] for row in recent_moves}
        emails = jmap_client.get_emails(move_ids[:100], ["id", "mailboxIds"])

        for email in emails:
            current_folder_ids = set(email["mailboxIds"].keys())
            expected_path = expected_folders[email["id"]]
            expected_id = folder_path_to_id.get(expected_path)

            # If the email is no longer in the folder mailsort put it in,
            # the user moved it — record the correction
            if expected_id and expected_id not in current_folder_ids:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path.get(new_folder_id)
                if new_folder_path and new_folder_path != "INBOX":
                    _record_manual_sort(run_id, email["id"], new_folder_path)


def _record_manual_sort(run_id: str, email_id: str, folder_path: str):
    """Log a manual classification and consider auto-generating a rule."""
    audit_log.insert(
        run_id=run_id,
        email_id=email_id,
        target_folder=folder_path,
        confidence=1.0,
        classification_source="manual",
        moved=True,
    )
    maybe_create_rule(email_id, folder_path)
```

All four categories log detected sorts as `classification_source='manual'` in
`audit_log` and feed them into auto-rule generation.

---

## Auto-Rule Generation

Auto-rule generation runs in two situations:
1. After detecting a manual sort (user moved an email we left in inbox)
2. After an LLM-classified move, once the same pattern has been seen N times

**Strategy: create all eligible rules.** Every rule type whose evidence
thresholds are met is created independently. A single sender can produce a
`list_id` rule *and* an `exact_sender` rule, or a `sender_domain` rule *and*
`exact_sender` rules for individual addresses within that domain. This keeps
the rule set complete so that:

- If a broader rule is later deactivated (confidence decay, correction
  penalty), the narrower rule still covers the sender.
- The rule detail UI shows all evidence-backed rules, giving full visibility
  into classification behaviour.
- Classification-time priority determines which rule actually fires —
  `list_id` beats `exact_sender` beats `sender_domain`.

Subject regex and body content are **not** auto-generated.

```python
def maybe_create_rule(email_features, target_folder: str) -> list[int]:
    """Create every rule type whose evidence thresholds are met.

    Evaluated independently (not short-circuited):
      1. list_id       — stable identifier for newsletters/mailing lists
      2. sender_domain — when domain history is coherent (most moves → same folder)
      3. exact_sender  — narrow scope for individual transactional senders

    Classification-time priority decides which rule fires; creation-time
    builds the full set so narrower rules survive if broader ones decay.

    Returns a list of created rule IDs (may be empty).
    """
    thresholds = config.classification.auto_rule_thresholds  # list_id: 2, exact_sender: 3, sender_domain: 5
    coherence_min = config.classification.auto_rule_domain_coherence  # 0.80
    created: list[int] = []

    # 1. List-Id rule — highest specificity, lowest threshold
    if email_features.list_id:
        count = db.execute("""
            SELECT COUNT(*) FROM audit_log
            WHERE list_id = ? AND target_folder = ? AND moved = 1
        """, (email_features.list_id, target_folder)).fetchone()[0]

        if count >= thresholds["list_id"]:
            existing = db.find_rule("list_id", email_features.list_id)
            if not existing:
                rule_id = db.create_rule(
                    rule_type="list_id",
                    condition_value=email_features.list_id,
                    target_folder_path=target_folder,
                    confidence=0.95,
                    source="auto",
                )
                logger.info(f"Auto-created list_id rule: {email_features.list_id} → {target_folder}")
                created.append(rule_id)

    # 2. Sender domain rule — requires volume AND coherence.
    #    Coherence: what fraction of this domain's total moves go to target_folder?
    #    A domain like amazon.com that splits across Orders/Receipts/Deals should
    #    never get a domain rule — it would misroute whichever folders are in the minority.
    domain_total = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_domain = ? AND moved = 1
    """, (email_features.from_domain,)).fetchone()[0]

    domain_to_target = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_domain = ? AND target_folder = ? AND moved = 1
    """, (email_features.from_domain, target_folder)).fetchone()[0]

    domain_distinct_senders = db.execute("""
        SELECT COUNT(DISTINCT from_address) FROM audit_log
        WHERE from_domain = ? AND target_folder = ? AND moved = 1
    """, (email_features.from_domain, target_folder)).fetchone()[0]

    domain_coherence = domain_to_target / domain_total if domain_total > 0 else 0.0

    if (domain_to_target >= thresholds["sender_domain"]
            and domain_distinct_senders >= 3
            and domain_coherence >= coherence_min):
        existing = db.find_rule("sender_domain", email_features.from_domain)
        if not existing:
            confidence = min(0.90, 0.75 + (domain_to_target * 0.02))
            rule_id = db.create_rule(
                rule_type="sender_domain",
                condition_value=email_features.from_domain,
                target_folder_path=target_folder,
                confidence=confidence,
                source="auto",
            )
            logger.info(
                f"Auto-created domain rule: {email_features.from_domain} → {target_folder} "
                f"(coherence={domain_coherence:.0%}, n={domain_to_target})"
            )
            created.append(rule_id)

    # 3. Exact sender — narrow scope, moderate threshold (always evaluated)
    count = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_address = ? AND target_folder = ? AND moved = 1
    """, (email_features.from_address, target_folder)).fetchone()[0]

    if count >= thresholds["exact_sender"]:
        existing = db.find_rule("exact_sender", email_features.from_address)
        if not existing:
            rule_id = db.create_rule(
                rule_type="exact_sender",
                condition_value=email_features.from_address,
                target_folder_path=target_folder,
                confidence=min(0.95, 0.80 + (count * 0.03)),
                source="auto",
            )
            logger.info(f"Auto-created sender rule: {email_features.from_address} → {target_folder}")
            created.append(rule_id)

    return created
```

---

## Feedback Loop: Confidence Penalty on Corrections

When Category 2 (correction sorts) detects that a user relocated a
mailsort-moved email to a **different non-inbox folder**, the originating rule
receives a confidence penalty. This creates a feedback loop: rules that
consistently misroute emails lose confidence and eventually deactivate.

**Design decisions:**

- **Inbox returns are ignored.** Moving an email back to inbox is ambiguous —
  the user may just want to re-read it. Only a definitive sort to a different
  folder counts as a correction.
- **Penalty per correction:** `correction_penalty` (default 0.15) confidence
  reduction on the rule that caused the original move. This is steeper than
  the −0.10 staleness decay because a correction is a stronger signal than
  mere inactivity.
- **Deduplication:** Each email can only penalize a rule once. The learner
  tracks which `(email_id, rule_id)` pairs have already been penalized by
  checking for a subsequent `manual` audit_log row for the same email. If one
  exists, the correction was already processed.
- **Auto-deactivation:** If a rule's confidence drops below the `rule_move`
  threshold after a penalty, it is automatically deactivated. This prevents
  rules with accumulated negative signal from continuing to misroute.
- **Floor:** Confidence never drops below 0.0 (though deactivation at the
  threshold makes the floor largely academic).

```
Correction detected:
  audit_log row: email X moved to Receipts by rule #42 (conf 0.85)
  current state: email X is now in Finance

  → Record manual sort: email X → Finance (already done by Cat 2)
  → Penalize rule #42: 0.85 − 0.15 = 0.70
  → If 0.70 < rule_move threshold (e.g. 0.75): deactivate rule #42
```

The penalty is applied inside `_detect_correction_sorts` immediately after
recording the manual sort, ensuring the rule's confidence is updated before
the classification phase of the same run.

---

## Bootstrapping

Bootstrap is **idempotent** — safe to run multiple times. On each run it skips
emails already present in `audit_log`, so evidence is never duplicated. New
emails that appeared in folders since the last bootstrap are added. Rules,
contacts, and folder descriptions all use upsert/dedup logic. This means you
can re-bootstrap after adding new folders or to pick up recent mail without
corrupting existing data.

Bootstrap does not use a separate, looser rule-creation strategy. Historical
emails are treated as evidence inputs to the same candidate evaluation logic
used during live learning. All eligible rules are created — a sender with a
`list_id` may produce both a `list_id` rule and an `exact_sender` rule if both
thresholds are met. Classification-time priority determines which rule fires.

```python
def bootstrap_rules(jmap_client, max_per_folder: int = 50):
    """Scan existing folders and feed historical evidence into the normal rule evaluator."""
    mailboxes = jmap_client.get_all_mailboxes()
    report = {"created": [], "rejected": [], "multi_folder_domains": []}

    # Phase 1: Collect evidence from all folders into audit_log
    for mailbox in mailboxes:
        if mailbox["role"] == "inbox" or is_system_folder(mailbox):
            continue

        folder_path = build_folder_path(mailbox, mailboxes)

        email_ids = jmap_client.query_emails(
            in_mailbox=mailbox["id"],
            limit=max_per_folder,
            sort=[{"property": "receivedAt", "isAscending": False}]
        )

        emails = jmap_client.get_emails(
            email_ids, ["id", "threadId", "from", "subject", "header:list-id:asText"]
        )

        # Record each email as bootstrap evidence in audit_log
        for email in emails:
            features = extract_features(email)
            audit_log.insert(
                run_id=bootstrap_run_id,
                email_id=email["id"],
                thread_id=email.get("threadId"),
                from_address=features.from_address,
                from_domain=features.from_domain,
                list_id=features.list_id,
                target_folder=folder_path,
                confidence=1.0,
                classification_source="manual",
                decision_status="moved",
            )

        # Generate folder description if one doesn't already exist
        if not db.get_folder_description(folder_path):
            description = generate_folder_description(folder_path, emails)
            if description:
                db.upsert_folder_description(folder_path, description, source="auto")
                logger.info(f"Generated description for {folder_path}: {description}")

    # Phase 2: Evaluate candidate rules using the same thresholds and coherence
    # checks as maybe_create_rule — now that audit_log has cross-folder history
    evidence = collect_bootstrap_candidates()
    for candidate in evidence:
        result = maybe_create_rule_from_evidence(
            candidate=candidate,
            source="auto",
        )
        if result.created:
            report["created"].append(result)
        elif result.reason == "low_coherence":
            report["rejected"].append(result)
            if result.rule_type == "sender_domain":
                report["multi_folder_domains"].append(result)

    log_bootstrap_report(report)

    # Load contacts after folder/rule seeding so first classification run
    # has full contact enrichment available
    refresh_contacts_cache(jmap_client)
```

### Folder Description Generation

```python
FOLDER_DESCRIPTION_PROMPT = """You are helping configure an email classifier.
Given a folder name and a sample of email subjects and senders stored in it,
write a single concise sentence (under 20 words) describing what kind of emails
belong in this folder.

Folder path: {folder_path}

Sample emails (sender — subject):
{samples}

Respond with ONLY the description sentence, no quotes, no punctuation at the end."""


def generate_folder_description(folder_path: str, emails: list) -> Optional[str]:
    """Ask the LLM to describe a folder based on sample emails inside it."""
    if not emails:
        return None

    samples = "\n".join(
        f"  {e['from'][0]['email'] if e.get('from') else '?'} — {e.get('subject', '(no subject)')}"
        for e in emails[:15]  # Cap at 15 samples; enough signal, minimal cost
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": FOLDER_DESCRIPTION_PROMPT.format(
                folder_path=folder_path,
                samples=samples,
            )
        }]
    )
    return response.content[0].text.strip()
```

Descriptions are generated automatically via `classifier/descriptions.py`:

- **During bootstrap:** each scanned folder gets a description generated from
  sample emails (LLM-based when `anthropic_api_key` is configured, name-based
  fallback otherwise)
- **During scheduled runs:** the orchestrator calls
  `generate_descriptions_for_new_folders()` to handle any folders added since
  the last run
- **Never overwritten:** existing descriptions are never replaced automatically.
  Change them manually via config overrides if needed.
- **Config overrides win:** `folder_description_overrides` in config.yaml
  takes precedence when loading descriptions for the LLM prompt

Descriptions are loaded at runtime by merging DB + config overrides, filtered
to only include folders that exist in the mailbox tree. Config override paths
are normalised (an `INBOX/` prefix is tried if the path doesn't match directly).
Overrides for non-existent or excluded folders are silently dropped so the LLM
never sees folders it can't classify into.

```python
def _load_folder_descriptions(cfg: Config, db: Database, valid_paths: set[str]) -> str:
    """Load folder descriptions, filtered to valid tree paths."""
    descriptions: dict[str, str] = {}

    # DB descriptions (already INBOX/-prefixed)
    for row in db.execute("SELECT folder_path, description FROM folder_descriptions"):
        if row["folder_path"] in valid_paths:
            descriptions[row["folder_path"]] = row["description"]

    # Config overrides — normalise path format (try INBOX/ prefix)
    for path, desc in (cfg.folder_description_overrides or {}).items():
        normalised = _normalise_folder_path(path, valid_paths)
        if normalised:
            descriptions[normalised] = desc

    return "\n".join(f"- {p}: {d}" for p, d in sorted(descriptions.items()))
```
