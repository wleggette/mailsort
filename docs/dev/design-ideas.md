# Design Ideas

Captured ideas for future features with enough context to pick them up later
without re-investigating from scratch.

---

## ~~Correction Penalty Tuning — One-Strike Deactivation Problem~~

**Status:** Implemented (2026-04-05) — resolved by the computed confidence model.
See `docs/dev/decisions.md` § "Computed confidence model replaces static penalties".

---

## ~~Web UI Threshold Analysis Page (`/analyze`)~~ — Implemented

**Status:** Implemented (2026-03-27)

Implemented as `web/routes/analyze.py` + `web/templates/analyze.html`.
Includes classification sources bar chart, LLM confidence distribution table,
skipped-then-sorted table, rule corrections table, and recommendations.
Date range picker (7d / 30d / 90d).

---

## List-Unsubscribe Combined Rule

**Status:** Not prioritized (2026-03-21)

### Concept

A new rule type that combines `sender_domain` + presence of the `List-Unsubscribe`
header to classify bulk/marketing emails that lack a `List-Id` header. This would
fill the gap between `list_id` rules (which require a `List-Id` header) and
`sender_domain` rules (which require ≥5 emails from ≥3 distinct senders).

Example: `domain=substack.com + has_unsubscribe=True → Social/Newsletters`

### Analysis (2026-03-21)

Ran `scripts/analyze_list_unsubscribe.py` against 2,628 emails across INBOX,
Affairs/*, and People/* folders. Findings:

| Metric | Count | % |
|--------|-------|---|
| Total emails scanned | 2,628 | 100% |
| Have `List-Unsubscribe` header | 192 | 7.3% |
| Have `List-Unsubscribe` but NO `List-Id` | 156 | 5.9% |
| ↳ Coherent (all go to single folder) | 108 | 4.1% |
| ↳↳ Already covered by `sender_domain` rule | 34 | 1.3% |
| ↳↳ Already covered by `exact_sender` rule | 29 | 1.1% |
| **↳↳ True gap (no existing rule covers)** | **45** | **1.7%** |

The 45 true-gap emails come from 37 domains, almost all single-sender with
1–2 emails each — below the `exact_sender` threshold of 3. They'll naturally
get covered as more email arrives.

Top coherent domains (all go to one folder):

| Domain | Emails | Covered by |
|--------|--------|-----------|
| linkedin.com | 34 | sender_domain (7 senders, 97% coherence) |
| facebookmail.com | 6 | exact_sender (6/6) |
| e.progressive.com | 5 | exact_sender (5/5) |
| lmco.com | 5 | exact_sender (4/5) |

6 domains were split across folders (e.g., `citi.com` across Banks + INBOX)
and wouldn't qualify for any combined rule due to low coherence.

### Why not prioritized

1. **Small incremental value** — only 1.7% of emails would benefit, all from
   low-volume senders that will qualify for `exact_sender` rules over time.
2. **Existing rules cover most of the gap** — 58% of the coherent unsub-only
   emails are already handled by `sender_domain` or `exact_sender` rules.
3. **The combined rule would only help sooner** — it would classify emails at
   1–2 occurrences instead of waiting for 3 (exact_sender threshold). This is
   a marginal timing improvement, not a coverage improvement.

### Implementation notes (from building the analysis script)

**JMAP header property naming:**
- Fastmail's JMAP rejects `header:list-unsubscribe:asText` (the lowercase
  `:asText` variant used for `list-id`). It returns `invalidArguments`.
- The working property name is `header:List-Unsubscribe` (case-sensitive,
  no `:asText` suffix). Returns `null` when the header is absent.
- The existing `JMAPClient` falls back from `EMAIL_PROPERTIES` (which includes
  `header:list-unsubscribe:asText`) to `_EMAIL_PROPERTIES_NO_UNSUB` when it
  gets an error — so the client silently drops the header. If implementing
  this feature, the property name in `EMAIL_PROPERTIES` needs to be fixed to
  `header:List-Unsubscribe`.

**Where the header is already modeled:**
- `JMAPEmail.list_unsubscribe` field exists in `jmap/models.py` (aliased to
  `header:list-unsubscribe:asText`) — would need the alias updated.
- `EmailFeatures.list_unsubscribe` field exists — already carries the value
  through the pipeline.
- The `_EMAIL_PROPERTIES_NO_UNSUB` fallback in `jmap/client.py` would need
  updating if the property name changes.

**Rule engine changes needed:**
- New rule type `domain_unsubscribe` (or extend `sender_domain` with a flag).
- Auto-rule generation: evaluate domain coherence for emails where
  `list_unsubscribe IS NOT NULL AND list_id IS NULL`.
- Classification priority: would slot between `list_id` and `exact_sender`
  (since it's broader than exact_sender but more specific than plain
  sender_domain).

### Analysis script

`scripts/analyze_list_unsubscribe.py` — scans Fastmail folders and reports
List-Unsubscribe prevalence, coverage gaps, and domain coherence. Can be re-run
to reassess if this feature becomes worth implementing as email volume grows.

```bash
.venv/bin/python scripts/analyze_list_unsubscribe.py
```

Requires `FASTMAIL_API_TOKEN` in `.env` (read-write token works; read-only
token also works but the header property may fail on some token configurations).

---

## ~~Dry-Run Aware Stale Run Reconciliation~~

**Status:** Implemented (2026-04-03) — M10 migration added `dry_run` column
to `runs` table. `reconcile_stale_runs` now only abandons `dry_run=0` rows.
Dashboard shows "dry run" badge for dry-run runs.
---

## ~~Coherence Drift on Active Rules~~

**Status:** Implemented (2026-04-05) — computed confidence model.
See `docs/dev/decisions.md` § "Computed confidence model replaces static penalties".
Formula, scenarios, alternatives, and rationale are preserved in the decision log.

---

## Forced Recomputation of Folder Descriptions

**Status:** Not yet implemented (2026-04-05)

### Problem

Folder descriptions are generated once — during bootstrap or when a new folder is first
discovered by the orchestrator. The `generate_folder_description` function explicitly
skips folders that already have a description in the `folder_descriptions` table. Once
written, a description is never updated automatically.

This becomes a problem when:

- **Bootstrap descriptions are poor.** Early descriptions may be based on a small or
  unrepresentative sample of emails (up to 15). As more mail accumulates, the description
  may no longer reflect the folder's actual contents.
- **Folder purpose evolves.** A folder originally used for one kind of email may shift
  over time (e.g., "Projects" starts receiving vendor invoices too).
- **Fallback descriptions persist.** If the LLM was unavailable during bootstrap (no API
  key, transient error), the folder gets a generic "Emails filed under X" fallback that
  never gets replaced.

Since folder descriptions are used in the LLM classification prompt, stale or inaccurate
descriptions degrade classification quality for all emails.

### Proposed Solution

Allow the user to force recomputation of folder descriptions at three granularity levels:

1. **All folders** — regenerate descriptions for every folder in the mailbox tree.
2. **Specific folders** — regenerate for folders matching a path pattern (e.g.,
   `INBOX/Affairs/*`).
3. **Individual folder** — regenerate for a single folder by exact path.

Recomputation should:

- Delete the existing row from `folder_descriptions` for the target folder(s).
- Fetch a fresh sample of emails from each folder via JMAP.
- Call the LLM to generate a new description from the fresh sample.
- Insert the new description with `source='auto'` and updated timestamps.
- Respect `folder_description_overrides` from config — folders with manual overrides
  are skipped (the override takes precedence).

#### CLI Interface

New subcommand under the `mailsort` CLI group:

```
mailsort regenerate-descriptions [OPTIONS]

Options:
  --folder TEXT    Regenerate for a specific folder path (repeatable)
  --pattern TEXT   Regenerate for folders matching a glob pattern (repeatable)
  --all            Regenerate for all folders
  --dry-run        Show which folders would be regenerated without doing it
```

Examples:

```bash
# Regenerate all descriptions
mailsort regenerate-descriptions --all

# Regenerate a single folder
mailsort regenerate-descriptions --folder "INBOX/Affairs/Banks"

# Regenerate folders matching a pattern
mailsort regenerate-descriptions --pattern "INBOX/Affairs/*"

# Preview what would be regenerated
mailsort regenerate-descriptions --all --dry-run
```

At least one of `--folder`, `--pattern`, or `--all` is required.

#### Web UI Interface

On the `/folders` page:

- **Per-folder action:** A "Regenerate" button or icon on each folder row. Triggers an
  async POST to regenerate the description for that single folder. Shows a spinner while
  in progress, then updates the description text in place.
- **Bulk action:** A "Regenerate All Descriptions" button (with confirmation dialog).
  Triggers regeneration for all non-overridden folders. Shows progress (e.g.,
  "Regenerating 12/45 folders…").

Folders with `source='manual'` (from config overrides) should show the override badge
and disable the regenerate button.

### Implementation Notes

**Core function changes (`classifier/descriptions.py`):**

- New function `regenerate_folder_description(db, folder_path, emails, ...)` — like
  `generate_folder_description` but deletes any existing description first. Alternatively,
  use `INSERT OR REPLACE` / `UPDATE` instead of the current `INSERT`.
- New function `regenerate_descriptions_for_folders(db, folder_paths, ...)` — batch
  version that iterates over a set of folder paths, fetching emails and regenerating each.
- The existing `generate_folder_description` stays unchanged for the normal flow (only
  generates if missing).

**JMAP interaction:**

- Regeneration needs to fetch sample emails for each target folder, which requires
  a `JMAPClient` instance and the `MailboxTree` (to resolve folder path → mailbox ID).
- The CLI command needs to set up JMAP (similar to `bootstrap` command) and the web UI
  already has access to config but would need a JMAP client for the email fetch.

**Sample quality:**

- Consider fetching more recent emails rather than the oldest (current bootstrap fetches
  up to `max_per_folder` without ordering preference). For regeneration, prefer
  `sort: receivedAt desc` to get the most recent emails, which better represent the
  folder's current purpose.
- The sample size (currently 15 emails sent to the LLM) could be configurable for
  regeneration, but the default should remain 15 for consistency.

**Web UI route (`web/routes/folders.py`):**

- New POST endpoint: `/folders/{path:path}/regenerate` — regenerate a single folder.
- New POST endpoint: `/folders/regenerate-all` — regenerate all folders.
- Both need access to a `JMAPClient`, which means either creating one per request or
  sharing a client via app state (the latter is preferable for connection reuse).

### Open Questions

1. **Should regeneration update `updated_at` vs. `generated_at`?** Currently
   `generated_at` uses a SQLite default. Regeneration should update both to reflect
   when the new description was created.
2. **Rate limiting for bulk regeneration.** Regenerating 50+ folders means 50+ LLM API
   calls. Should there be a concurrency limit or delay between calls? Haiku is fast and
   cheap, so this may not matter in practice.
3. **Should the web UI show the old vs. new description?** A diff or "previous
   description" tooltip could help the user confirm the regeneration improved things.
