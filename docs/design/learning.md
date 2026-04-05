# Learning & Auto-Rule Generation

The learning system detects user manual sorts and automatically generates
classification rules from repeated patterns. It manages rule confidence
through a **computed confidence model** that derives confidence from live
state each cycle — incorporating coherence, staleness, and user corrections.

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

    Both are logged and fed into auto-rule generation.
    """
    # --- Category 1: skipped emails the user moved out of inbox ---
    if previously_skipped:
        already = _already_handled_email_ids(previously_skipped)
        emails = jmap_client.get_emails(
            [eid for eid in previously_skipped if eid not in already],
            ["id", "mailboxIds"],
        )
        for email in emails:
            if inbox_id not in email["mailboxIds"]:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path[new_folder_id]
                _record_manual_sort(run_id, email["id"], new_folder_path)

    # --- Category 2: mailsort-moved emails the user relocated ---
    # ORDER BY created_at ASC ensures the most recent move wins in the dict
    # (Python dict overwrites earlier keys with later ones)
    recent_moves = db.execute("""
        SELECT email_id, target_folder, rule_id FROM audit_log
        WHERE moved = 1
          AND classification_source NOT IN ('manual', 'correction')
          AND created_at >= datetime('now', '-7 days')
        ORDER BY created_at ASC
    """).fetchall()

    if recent_moves:
        expected_folders = {row["email_id"]: row["target_folder"] for row in recent_moves}
        rule_ids = {row["email_id"]: row["rule_id"] for row in recent_moves}
        email_ids = list(expected_folders.keys())

        already = _already_handled_email_ids(email_ids)
        emails = jmap_client.get_emails(
            [eid for eid in email_ids if eid not in already],
            ["id", "mailboxIds"],
        )

        for email in emails:
            expected_path = expected_folders[email["id"]]
            expected_id = folder_path_to_id.get(expected_path)

            if expected_id and expected_id not in email["mailboxIds"]:
                new_folder_id = list(email["mailboxIds"].keys())[0]
                new_folder_path = mailbox_id_to_path.get(new_folder_id)
                if new_folder_path and new_folder_path != "INBOX":
                    _record_correction(
                        run_id, email["id"], new_folder_path,
                        rule_id=rule_ids.get(email["id"]),
                    )


def _record_manual_sort(run_id, email_id, folder_path):
    """Log a manual classification (Cat 1, 3, 4) and consider auto-rule."""
    audit_log.insert(
        ..., classification_source="manual", rule_id=None, moved=True,
    )
    maybe_create_rule(email_id, folder_path)


def _record_correction(run_id, email_id, folder_path, rule_id):
    """Log a correction (Cat 2) with the rule that fired."""
    audit_log.insert(
        ..., classification_source="correction", rule_id=rule_id, moved=True,
    )
    maybe_create_rule(email_id, folder_path)
```

Categories 1, 3, and 4 log detected sorts as `classification_source='manual'`.
Category 2 (corrections) uses `classification_source='correction'` with `rule_id`
set to the rule that fired (from the original audit_log move row).

#### Dedup: Preventing Duplicate Detections

Each detection category uses `_already_handled_email_ids` to skip emails that
have already been recorded. The dedup must account for the **move-correct-move-correct**
cycle: if a rule moves an email, the user corrects it, the email returns to inbox,
and the rule moves it again, the second correction **must** be detected.

The fix: only exclude an email if its most recent manual/correction row is
**newer than** its most recent rule move:

```sql
-- Emails where a correction/manual row already exists AND no newer rule move followed
SELECT DISTINCT a.email_id FROM audit_log a
WHERE a.email_id IN (...)
  AND a.classification_source IN ('manual', 'correction')
  AND NOT EXISTS (
      SELECT 1 FROM audit_log b
      WHERE b.email_id = a.email_id
        AND b.classification_source NOT IN ('manual', 'correction')
        AND b.moved = 1
        AND b.created_at > a.created_at
  )
```

If a new rule move exists after the last correction, the email is eligible for
re-detection. This prevents both double-counting within a cycle and missed
re-corrections across cycles.

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

### Reactivation Over Duplication

When evidence supports creating a rule, `maybe_create_rule` searches for an existing
rule with the same type+condition in **any status** (active or inactive) using
`find_rule_any_status`. If an inactive rule exists, it is reactivated with its
`confidence` set from the `BaseConfidenceConfig` formula using current evidence count,
rather than creating a duplicate row. This ensures one row per type+condition in the
database.

### `base_confidence` from `BaseConfidenceConfig`

Base confidence is **computed on the fly** each cycle from the all-time evidence count
for the rule's condition+target in `audit_log`. It is not stored in a column — the
`BaseConfidenceConfig` formula and the current evidence count are sufficient. This
means base confidence grows naturally as more evidence accumulates, capping quickly
(~5–8 emails depending on rule type). All values are configurable via
`BaseConfidenceConfig` (nested in `ClassificationConfig`):

| Rule type | Formula | Defaults |
|-----------|---------|----------|
| `list_id` | Fixed value | 0.95 |
| `exact_sender` | `min(cap, floor + evidence_count × per_evidence)` | floor=0.80, cap=0.95, per_evidence=0.03 |
| `sender_domain` | `min(cap, floor + evidence_count × per_evidence)` | floor=0.75, cap=0.90, per_evidence=0.02 |

```python
def maybe_create_rule(email_features, target_folder: str) -> list[int]:
    """Create every rule type whose evidence thresholds are met.

    Evaluated independently (not short-circuited):
      1. list_id       — stable identifier for newsletters/mailing lists
      2. sender_domain — when domain history is coherent (most moves → same folder)
      3. exact_sender  — narrow scope for individual transactional senders

    Classification-time priority decides which rule fires; creation-time
    builds the full set so narrower rules survive if broader ones decay.

    If an inactive rule with the same type+condition exists, it is
    reactivated instead of creating a duplicate.

    Returns a list of created rule IDs (may be empty).
    """
    thresholds = config.classification.auto_rule_thresholds  # list_id: 2, exact_sender: 3, sender_domain: 5
    coherence_min = config.classification.auto_rule_domain_coherence  # 0.80
    base_conf = config.classification.base_confidence  # BaseConfidenceConfig
    created: list[int] = []

    # 1. List-Id rule — highest specificity, lowest threshold
    if email_features.list_id:
        count = db.execute("""
            SELECT COUNT(*) FROM audit_log
            WHERE list_id = ? AND target_folder = ? AND moved = 1
        """, (email_features.list_id, target_folder)).fetchone()[0]

        if count >= thresholds["list_id"]:
            conf = base_conf.list_id  # fixed value (0.95)
            existing = find_rule_any_status("list_id", email_features.list_id)
            if existing and not existing["active"]:
                reactivate_rule(existing, confidence=conf)
                created.append(existing["id"])
            elif not existing:
                rule_id = db.create_rule(
                    rule_type="list_id",
                    condition_value=email_features.list_id,
                    target_folder_path=target_folder,
                    confidence=conf,
                    source="auto",
                )
                created.append(rule_id)

    # 2. Sender domain rule — requires volume AND coherence.
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
        conf = min(base_conf.sender_domain_cap,
                   base_conf.sender_domain_floor + domain_to_target * base_conf.sender_domain_per_evidence)
        existing = find_rule_any_status("sender_domain", email_features.from_domain)
        if existing and not existing["active"]:
            reactivate_rule(existing, confidence=conf)
            created.append(existing["id"])
        elif not existing:
            rule_id = db.create_rule(
                rule_type="sender_domain",
                condition_value=email_features.from_domain,
                target_folder_path=target_folder,
                confidence=conf,
                source="auto",
            )
            created.append(rule_id)

    # 3. Exact sender — narrow scope, moderate threshold (always evaluated)
    count = db.execute("""
        SELECT COUNT(*) FROM audit_log
        WHERE from_address = ? AND target_folder = ? AND moved = 1
    """, (email_features.from_address, target_folder)).fetchone()[0]

    if count >= thresholds["exact_sender"]:
        conf = min(base_conf.exact_sender_cap,
                   base_conf.exact_sender_floor + count * base_conf.exact_sender_per_evidence)
        existing = find_rule_any_status("exact_sender", email_features.from_address)
        if existing and not existing["active"]:
            reactivate_rule(existing, confidence=conf)
            created.append(existing["id"])
        elif not existing:
            rule_id = db.create_rule(
                rule_type="exact_sender",
                condition_value=email_features.from_address,
                target_folder_path=target_folder,
                confidence=conf,
                source="auto",
            )
            created.append(rule_id)

    return created
```

---

## Computed Confidence Model

Rule confidence is **computed from live state** every cycle rather than being
set once at creation and modified by one-way penalties. The `compute_rule_confidence()`
method runs in the learning step for all active auto rules (`source != 'manual'`),
writing the updated confidence to the `confidence` column. The rule engine at
classification time reads the stored value as before — no changes to the
classification hot path.

### Formula

```
confidence = max(0, base_confidence × coherence_factor × staleness_factor
                    − net_corrections_in_window × correction_penalty)
```

The formula is **idempotent** — running twice with the same inputs produces the
same result. It is **bidirectional** — if conditions improve, confidence goes up;
if conditions worsen, confidence drops. There are no one-way ratchets.

### Coherence Factor

Live coherence computed from `audit_log` within a configurable lookback window
(`coherence_lookback_days`, default 30):

```
coherence_factor = (emails matching condition → rule's target folder)
                   / (all emails matching condition that were moved)
```

Ranges 0.0–1.0. A **minimum sample guard** applies: if fewer than
`coherence_min_sample` (default 3) emails exist in the window, `coherence_factor`
defaults to 1.0 (benefit of the doubt).

This catches **coherence drift** — a rule created when 95% of a sender's mail
went to one folder will lose confidence if the distribution shifts. When coherence
recovers, confidence recovers automatically.

### Staleness Factor

Based on `last_relevant_at` — the most recent email matching the rule's condition
that was sorted to the rule's target folder, regardless of who performed the sort
(rule, LLM, thread context, or user).

- If `days_since_last_relevant ≤ staleness_threshold_days` (default 365): **1.0**
- Otherwise: `max(staleness_floor, 1.0 − (days_past_threshold / staleness_decay_days) × 0.4)`
  where `staleness_floor` = 0.6 and `staleness_decay_days` = 365

The 365-day threshold ensures quarterly newsletters, annual statements, and
seasonal senders never decay. Only senders silent for 13+ months start losing
confidence, and the decay is gentle (90 days past threshold to stop firing).

**Why `last_relevant_at` instead of `last_hit_at`:** If staleness were based on
when the *rule itself* last fired, a stale rule that drops below the confidence
gate enters a dead zone — it can't fire, so it can't get a hit, so it stays stale.
`last_relevant_at` breaks this cycle: if any matching email is sorted to the
correct folder by any means, staleness resets and the rule recovers.

`last_relevant_at` is maintained as a side effect of the coherence query —
`MAX(moved_at)` from the same audit_log results used for the coherence calculation.

### Correction Counting

User corrections have **immediate, volume-independent** impact via a flat
per-correction penalty (`correction_penalty`, default 0.05):

```
net_corrections = max(0, corrections_against_rule − confirming_manual_sorts)
penalty_total = net_corrections × correction_penalty
```

Three corrections stop any rule (3 × 0.05 = 0.15, enough to drop below the
`rule_move` threshold of 0.85). This is independent of the coherence factor,
so high-volume rules don't absorb corrections through sheer volume.

#### Correction Identification

Category 2 corrections are recorded with `classification_source='correction'` and
the existing `rule_id` column set to the **rule that fired** (from the original
`audit_log` move row). Only the firing rule receives a correction row — broader
rules that match the same email but didn't fire are not explicitly penalized.

Instead, **coherence handles the cascade naturally.** When a user corrects an email
away from Banks, that email now lives in a different folder. This shows up in the
coherence calculation for any broader rule (e.g., `sender_domain`) because matching
emails are now split across folders. If the corrected sender represents a large
fraction of the domain's traffic, the domain rule's coherence drops proportionally.

This avoids over-penalizing broad rules from a single sender's correction while
still being self-correcting: if the `exact_sender` rule decays and the `sender_domain`
rule fires next, the user corrects once more, and that rule gets its own correction row.
Worst case is 2–3 corrections (one per rule tier), after which all tiers stop firing.

Audit log rows for corrections vs. manual sorts:
- **Cat 1, 3, 4** (manual sorts): `classification_source='manual'`, `rule_id=NULL`
- **Cat 2** (corrections): `classification_source='correction'`, `rule_id=<corrected rule>`

#### Corrections Query

```sql
-- Corrections against this rule in the lookback window
SELECT COUNT(*) FROM audit_log
WHERE classification_source = 'correction'
  AND rule_id = ?
  AND created_at >= datetime('now', ? || ' days')
```

#### Confirming Sorts Query

A confirming sort is a Cat 1/3/4 manual sort where the email matches the rule's
condition AND was sorted to the rule's target folder. Query varies by rule type:

```sql
-- For exact_sender rule (condition_value = from_address)
SELECT COUNT(*) FROM audit_log
WHERE classification_source = 'manual'
  AND from_address = ?          -- rule's condition_value
  AND target_folder = ?         -- rule's target_folder_path
  AND created_at >= datetime('now', ? || ' days')

-- For sender_domain: use from_domain = ?
-- For list_id: use list_id = ?
```

Then: `net_corrections = max(0, corrections − confirming)`.

**Corrections are recoverable:** confirming sorts cancel corrections 1:1.
Corrections also age out of the lookback window (default 30 days) automatically.
**Inbox returns are ignored** — moving an email back to inbox is ambiguous.

### Deactivation and Recovery

**Confidence gate handles most cases.** Between `deactivation_threshold` (0.50) and
`rule_move` (0.85), the rule stays `active=1` but doesn't fire — the rule engine's
existing confidence gate filters it out. If conditions improve, confidence recovers
and the rule resumes firing without any deactivation/reactivation cycle.

**Deactivation at threshold.** When computed confidence drops below
`deactivation_threshold` (default 0.50), the rule is set to `active=0`. The
threshold of 0.50 approximates `staleness_floor` (0.6) × minimum `base_confidence`
(0.80) = 0.48 ≈ 0.50: the lowest confidence a merely-stale rule with good coherence
would reach. Anything below that reflects genuinely bad evidence.

**Reactivation:** Deactivated rules are reactivated by `maybe_create_rule` when new
evidence supports the pattern (see "Reactivation Over Duplication" above). No
duplicate rows are created.

### Manual Rule Exemption

Rules with `source = 'manual'` are **exempt** from computed confidence adjustments.
The user explicitly created them; modifying confidence silently would be surprising.
Instead, the web UI shows a warning badge when live coherence drops below
`auto_rule_domain_coherence` (default 0.80), and the user decides whether to adjust
or deactivate.

### `compute_rule_confidence()` Method

Runs every cycle in the learning step. For each active auto rule:

```python
def compute_rule_confidence(db, config):
    """Recompute confidence for all active auto rules from live state."""
    rules = db.execute(
        "SELECT * FROM rules WHERE active = 1 AND source != 'manual'"
    ).fetchall()

    base_conf = config.base_confidence  # BaseConfidenceConfig

    for rule in rules:
        # 1. base_confidence — computed on the fly from all-time evidence
        evidence_count = _count_all_time_evidence(db, rule)
        base = _compute_base_confidence(rule["rule_type"], evidence_count, base_conf)

        # 2. coherence_factor — from audit_log within lookback window
        coherence, sample_count, last_relevant = _compute_coherence(
            db, rule, config.coherence_lookback_days,
        )
        if sample_count < config.coherence_min_sample:
            coherence = 1.0  # benefit of the doubt

        # 3. staleness_factor — from last_relevant_at
        staleness = _compute_staleness(
            last_relevant or rule["last_relevant_at"],
            config.staleness_threshold_days,
            config.staleness_decay_days,
            config.staleness_floor,
        )

        # 4. net_corrections — in lookback window
        net_corrections = _count_net_corrections(
            db, rule, config.coherence_lookback_days,
        )

        # 5. confidence formula
        confidence = max(0, base * coherence * staleness
                         - net_corrections * config.correction_penalty)

        # 6. deactivation check
        if confidence < config.deactivation_threshold:
            db.execute(
                "UPDATE rules SET active = 0, confidence = ? WHERE id = ?",
                (confidence, rule["id"]),
            )
        else:
            db.execute(
                "UPDATE rules SET confidence = ?, last_relevant_at = ? WHERE id = ?",
                (confidence, last_relevant, rule["id"]),
            )

    db.commit()


def _count_all_time_evidence(db, rule) -> int:
    """Count all-time evidence for base_confidence. Uses LIMIT to cap scan."""
    col = {"exact_sender": "from_address", "sender_domain": "from_domain",
           "list_id": "list_id"}[rule["rule_type"]]
    max_needed = 20  # well past any cap — avoids full table scan
    return db.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM audit_log
            WHERE {col} = ? AND target_folder = ? AND moved = 1
            LIMIT ?
        )
    """, (rule["condition_value"], rule["target_folder_path"], max_needed)).fetchone()[0]


def _compute_base_confidence(rule_type, evidence_count, base_conf) -> float:
    """Compute base confidence from rule type and evidence count."""
    if rule_type == "list_id":
        return base_conf.list_id
    elif rule_type == "exact_sender":
        return min(base_conf.exact_sender_cap,
                   base_conf.exact_sender_floor + evidence_count * base_conf.exact_sender_per_evidence)
    elif rule_type == "sender_domain":
        return min(base_conf.sender_domain_cap,
                   base_conf.sender_domain_floor + evidence_count * base_conf.sender_domain_per_evidence)
```

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
