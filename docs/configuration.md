# Configuration Reference

Mailsort is configured via `config.yaml` (gitignored) with secrets from
environment variables. The committed reference is `config.yaml.example`.

All intervals, thresholds, and tunable values are configurable — no hardcoded
magic numbers. Fields are defined as Pydantic models in `config.py` with
sensible defaults.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FASTMAIL_API_TOKEN` | Yes | Fastmail API token (Bearer auth) |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (for LLM classification). If unset, LLM classification is skipped gracefully |

## config.yaml

```yaml
# Fastmail settings
fastmail:
  api_url: "https://api.fastmail.com/jmap/api/"
  session_url: "https://api.fastmail.com/jmap/session"
  # Token is in FASTMAIL_API_TOKEN env var

# Scheduling
scheduler:
  interval_minutes: 15
  min_age_minutes: 240      # Don't move emails younger than this (4 hours)
  max_batch_size: 100       # Max emails to process per run

# Classification
classification:
  thresholds:
    rule_move: 0.85
    llm_move: 0.80
    llm_move_known_contact: 0.93   # Stricter for personal/family senders
  # Per-type thresholds for auto-rule creation.
  # Higher thresholds for broader rules; coherence required for domain rules.
  auto_rule_thresholds:
    list_id: 2              # list_id uniquely identifies one sender source
    exact_sender: 3         # Narrow scope, 3 confirmations is sufficient
    sender_domain: 5        # Broad scope — more evidence required
  auto_rule_domain_coherence: 0.80  # 80%+ of domain moves must go to same folder
  llm_model: "claude-haiku-4-5-20251001"
  llm_max_preview_chars: 500
  llm_use_preview: true             # Send email preview text to the LLM
  llm_allow_known_contacts: true    # If false, skip LLM for known contacts
  llm_redact_patterns:              # Regex patterns to redact before sending to LLM
    - "\\b\\d{3}-\\d{2}-\\d{4}\\b"             # SSN
    - "\\b(?:\\d[ -]*){13,16}\\b"              # Credit card numbers
  llm_suggest_rule_after_n: 5       # Suggest a subject regex rule after N consistent LLM classifications
  llm_skip_senders:                 # Never send these senders' emails to the LLM
    # - "spouse@example.com"
  llm_skip_domains:                 # Never send emails from these domains to the LLM
    # - "bank.example.com"
  correction_penalty: 0.05          # Confidence reduction per net user correction (3 typically drops below rule_move)
  learner_lookback_days: 7          # Days back to check for skipped/corrected emails
  coherence_lookback_days: 30       # Window for coherence and correction counting
  coherence_min_sample: 3           # Min emails in window before coherence adjusts confidence
  staleness_threshold_days: 365     # Days since last_relevant_at before staleness decay starts
  staleness_decay_days: 365         # Duration of linear decay from 1.0 to floor
  staleness_floor: 0.6              # Minimum staleness factor
  deactivation_threshold: 0.50      # Confidence below which a rule is set to active=0
  base_confidence:                  # Base confidence formula params (computed on the fly each cycle)
    list_id: 0.95
    exact_sender_floor: 0.80
    exact_sender_cap: 0.95
    exact_sender_per_evidence: 0.03
    sender_domain_floor: 0.75
    sender_domain_cap: 0.90
    sender_domain_per_evidence: 0.02

# Folder descriptions — OPTIONAL manual overrides only.
# Bootstrap auto-generates descriptions for all folders by scanning their contents.
# Only add entries here to correct a description the LLM got wrong.
folder_description_overrides:
  # "INBOX/Affairs/Legal": "Contracts, NDAs, and legal correspondence from attorneys"

# Manual rules (override auto-generated rules)
manual_rules:
  - type: exact_sender
    value: "important@example.com"
    folder: "INBOX/Affairs/Legal"
    confidence: 1.0

# Folders to exclude from classification, bootstrap, and learning.
# Uses glob patterns matched against folder paths.
# Also useful for parent/holding folders that only contain subfolders:
#   excluding "INBOX/Affairs" still allows "INBOX/Affairs/Banks" etc.
exclude_folder_patterns:
  # - "INBOX/Affairs"       # parent folder — sort into subfolders instead
  # - "INBOX/People"        # parent folder — sort into subfolders instead

# Skip list — never auto-move these senders
skip_senders:
  - "spouse@example.com"
  - "boss@company.com"

# Contacts are loaded automatically from Fastmail (synced from Apple Contacts).
# Use this section only to add relationship hints that CardDAV doesn't carry,
# or to annotate addresses not in your address book.
known_contact_overrides:
  # "husband@gmail.com":
  #   relationship: "spouse"   # Extra hint for the LLM beyond just the name

# Logging
logging_config:
  level: INFO
  file: "/app/data/mailsort.log"
  max_size_mb: 10
  backup_count: 3
```

## Section Reference

### `fastmail`

| Field | Default | Description |
|-------|---------|-------------|
| `api_url` | `https://api.fastmail.com/jmap/api/` | JMAP API endpoint |
| `session_url` | `https://api.fastmail.com/jmap/session` | JMAP session discovery URL |

### `scheduler`

| Field | Default | Description |
|-------|---------|-------------|
| `interval_minutes` | `15` | Minutes between classification passes |
| `min_age_minutes` | `240` | Minimum email age before moving (4 hours) |
| `max_batch_size` | `100` | Max emails to process per run |
| `health_check_port` | `8025` | Port for the `/health` endpoint |
| `web_port` | `8080` | Port for the embedded web UI (0 to disable) |
| `contacts_refresh_hours` | `24` | Hours between Fastmail contact cache refreshes |
| `folder_scan_interval_hours` | `24` | Hours between Category 4 daily folder scans |
| `stale_dry_run_minutes` | `60` | Minutes before a stuck dry-run row is abandoned |

### `classification.thresholds`

| Field | Default | Description |
|-------|---------|-------------|
| `rule_move` | `0.85` | Minimum confidence for rule-based moves |
| `llm_move` | `0.80` | Minimum confidence for LLM-based moves |
| `llm_move_known_contact` | `0.93` | Stricter LLM threshold for known contacts |
| `rule_learn` | `0.70` | *(Currently unused)* Reserved for future rule-based learning signal threshold |

### `classification.auto_rule_thresholds`

| Field | Default | Description |
|-------|---------|-------------|
| `list_id` | `2` | Minimum emails to create a list_id rule |
| `exact_sender` | `3` | Minimum emails to create an exact_sender rule |
| `sender_domain` | `5` | Minimum emails to create a sender_domain rule |

### `classification` (other)

| Field | Default | Description |
|-------|---------|-------------|
| `auto_rule_domain_coherence` | `0.80` | Min coherence for domain rules |
| `llm_model` | `claude-haiku-4-5-20251001` | Anthropic model for classification |
| `llm_max_preview_chars` | `500` | Max preview chars sent to LLM |
| `llm_use_preview` | `true` | Send email preview to LLM |
| `llm_allow_known_contacts` | `true` | Allow LLM for known contacts |
| `llm_suggest_rule_after_n` | `5` | Suggest regex rule after N consistent LLM classifications |
| `correction_penalty` | `0.05` | Confidence reduction per net user correction (3 typically drops below `rule_move`) |
| `learner_lookback_days` | `7` | How many days back to check for skipped/corrected emails |
| `coherence_lookback_days` | `30` | Window (days) for coherence and correction counting |
| `coherence_min_sample` | `3` | Minimum emails in window before coherence adjusts confidence |
| `staleness_threshold_days` | `365` | Days since `last_relevant_at` before staleness decay starts |
| `staleness_decay_days` | `365` | Duration of linear decay from 1.0 to floor |
| `staleness_floor` | `0.6` | Minimum staleness factor |
| `deactivation_threshold` | `0.50` | Confidence below which a rule is set to `active=0` |
| `min_known_contact_skips` | `3` | Min threshold-blocked emails before showing a known contact card on `/analyze` |

### `classification.base_confidence`

Parameters for the base confidence formula, computed on the fly each cycle from
all-time evidence count in `audit_log`. Caps quickly (~5–8 emails depending on
rule type), so at scale the coherence and staleness factors dominate.

| Field | Default | Description |
|-------|---------|-------------|
| `list_id` | `0.95` | Fixed base confidence for list_id rules |
| `exact_sender_floor` | `0.80` | Starting confidence for exact_sender with minimum evidence |
| `exact_sender_cap` | `0.95` | Maximum confidence for exact_sender regardless of evidence count |
| `exact_sender_per_evidence` | `0.03` | Confidence increase per additional email for exact_sender |
| `sender_domain_floor` | `0.75` | Starting confidence for sender_domain with minimum evidence |
| `sender_domain_cap` | `0.90` | Maximum confidence for sender_domain regardless of evidence count |
| `sender_domain_per_evidence` | `0.02` | Confidence increase per additional email for sender_domain |

### `manual_rules`

Optional list of manually-defined rules that are exempt from computed confidence
adjustments. If an auto rule with the same type+condition already exists at
bootstrap time, it is upgraded to `source='manual'`.

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | Rule type: `exact_sender`, `sender_domain`, `list_id`, `subject_regex` |
| `value` | Yes | Condition value (email address, domain, list-id, regex) |
| `folder` | Yes | Target folder path (short form like `People/Children` or full `INBOX/People/Children`) |
| `confidence` | No | Confidence value (default `1.0`) — not recomputed by the confidence model |

### `logging_config`

| Field | Default | Description |
|-------|---------|-------------|
| `level` | `INFO` | Log level |
| `file` | `/app/data/mailsort.log` | Log file path |
| `max_size_mb` | `10` | Max log file size before rotation |
| `backup_count` | `3` | Number of rotated log files to keep |
| `format` | `text` | Log format: `text` (human-readable) or `json` (structured) |
