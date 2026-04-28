# Operations & Deployment

Docker deployment, operational concerns, error handling, and monitoring.

## Docker

### Dockerfile

Because this project uses a `src/` layout, the package source must be copied
into the image before `pip install .` is executed. Otherwise the package may
not be importable at runtime.

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached unless pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN mkdir -p src/mailsort && \
    touch src/mailsort/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src/mailsort

# Copy actual source (only this layer rebuilds on code changes)
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

EXPOSE 8025 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8025/health')" || exit 1

CMD ["mailsort", "start"]
```

The two-stage `pip install` separates dependency installation (cached) from
source installation (`--no-deps`, near-instant). Code-only rebuilds skip the
slow dependency resolution step.

The `start` command runs the scheduler, health check (port 8025), and web UI
(port 8080) in a single process. See `docs/dev/decisions.md` "Embed web UI in
scheduler process" for rationale.

On first start (no completed bootstrap in the database), the scheduler
automatically runs `run_bootstrap` on the first tick before any classification.
Classification begins on the next scheduled tick. If bootstrap fails or the
process is killed, the next tick retries. See `_run_auto_bootstrap` in
`scheduler.py`.

### docker-compose.yml

```yaml
services:
  mailsort:
    build: .
    container_name: mailsort
    restart: unless-stopped
    stop_grace_period: 180s
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    ports:
      - "8080:8080"
    environment:
      - FASTMAIL_API_TOKEN=${FASTMAIL_API_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}
      - TZ=America/Chicago
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

`stop_grace_period: 180s` gives in-flight runs up to 3 minutes to finish
before Docker force-kills the container during `docker compose up --build`.

### .env file (not committed)

```
FASTMAIL_API_TOKEN=fmu1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxx   # Optional: only if auth enabled
```

---

## Concurrent Run Protection

APScheduler may fire a new run while a previous one is still in progress (e.g.,
if a run takes longer than the scheduler interval). Two simultaneous runs would
process the same inbox emails and write conflicting audit records.

Three layers of protection:

1. **APScheduler `max_instances=1`** — prevents the scheduler from launching a
   second instance of the job while one is already running.
2. **`fcntl.flock`** on `data/mailsort.run.lock` — true mutual exclusion within
   a single kernel. Auto-releases on crash/SIGKILL. Only live runs acquire the
   lock; dry runs bypass it. Lock is acquired early (before JMAP setup) so a
   second instance fails fast.
3. **CLI Docker delegation** — when the local CLI detects a running `mailsort`
   Docker container, it delegates via `docker exec` to ensure all runs happen
   inside the same kernel where `flock` works.

### Stale run reconciliation

On startup, `reconcile_stale_runs` marks leftover `running` rows as
`abandoned`. Live runs (`dry_run=0`) are abandoned unconditionally (the lock
guarantees they're stale). Dry runs (`dry_run=1`) are only abandoned after
`stale_dry_run_minutes` (default 60) to avoid interfering with a legitimately
running dry run.

### Auto-downgrade on read-only token

If a live run detects a read-only JMAP token (`jmap.is_read_only`), it
automatically downgrades to dry-run mode. `run_classification_pass` returns a
`RunResult` dataclass with `read_only_downgrade=True`. The CLI and scheduler
display the downgrade to the user.

---

## Error Handling Design

Every I/O boundary (JMAP API, Anthropic API, SQLite) is wrapped so that
failures are logged, partial progress is preserved, and one bad email never
kills the entire batch. Four principles govern error handling:

### 1. Guaranteed audit logging

Classification decisions are always written to `audit_log`, even when the
JMAP move call crashes. The orchestrator wraps the move step in `try/except`
and writes audit rows in a `finally` block:

```python
outcomes: dict[str, bool] = {}
try:
    if planned and not dry_run:
        outcomes = jmap.move_emails(moves)
except Exception:
    logger.exception("JMAP move_emails failed — decisions will still be logged")
finally:
    # Always log, even on move crash. When outcomes is empty,
    # planned decisions are recorded as moved=False — accurate
    # since nothing was confirmed moved.
    audit.log_decisions(run_id, decisions, outcomes)
```

The outer `run_classification_pass` also catches any exception from the full
run body and calls `finish_run(status="failed")`, so the `runs` table always
reflects what happened.

### 2. Per-email isolation

A classification failure for one email must not prevent the remaining emails
from being processed. The orchestrator wraps each `pipeline.classify()` call:

```python
for features in eligible:
    try:
        classification, skip_reason = pipeline.classify(features)
    except Exception:
        logger.exception("Classification failed for %s, skipping", features.email_id)
        classification, skip_reason = None, "classification_error"
```

Within the pipeline itself, the thread context DB lookup and JMAP fallback
are each individually wrapped so failures degrade to the next classification
tier (rules, then LLM) rather than crashing.

### 3. Defensive audit writes

`AuditWriter.finish_run()` is called from exception handlers and must never
mask the original error. It catches and logs its own DB failures internally:

```python
def finish_run(self, run_id, *, status, ...):
    try:
        self._db.execute("UPDATE runs SET status=? ...", ...)
        self._db.commit()
    except Exception:
        logger.exception("Failed to write finish_run for %s", run_id)
```

`AuditWriter.log_decisions()` uses per-row isolation — if one insert fails
(e.g., constraint violation), remaining rows are still written:

```python
for d in decisions:
    try:
        self.log_decision(run_id, d, moved=moved)
        logged += 1
    except Exception:
        logger.exception("Failed to log audit row for %s", d.email_id)
```

### 4. Graceful degradation across tiers

Each I/O tier degrades independently without blocking the others:

| Tier | Failure mode | Behavior |
|------|-------------|----------|
| **JMAP query/fetch** | Network error, HTTP 5xx | Run marked `failed`, exception propagated to caller |
| **Thread context DB** | SQLite error | Logged, returns `None` — falls through to rule engine |
| **Thread context JMAP** | `Thread/get` or `Email/get` failure | Logged, returns `None` — falls through to rule engine |
| **Rule engine** | Bad regex in `subject_regex` rule | Logged per-rule, continues to next rule |
| **LLM (Anthropic)** | API timeout, rate limit, parse error | Returns `Classification(confidence=0.0, reasoning="api_error")` — email is skipped, not crashed |
| **JMAP move** | `Email/set` network error | Logged, `outcomes` stays empty, all decisions logged as `moved=False` |
| **Audit DB** | Insert/commit failure | Logged per-row, remaining rows still attempted |

Anthropic API failures are handled at the LLM classifier level — `classify()`
catches all exceptions and returns a safe default. Since rules don't need the
LLM, rule-matched emails can still be moved in the same run. Only emails that
require LLM classification are skipped.

---

## Deleted Folder Handling

If a Fastmail folder is renamed or deleted, rules pointing to it will have a
stale `target_folder_path` that no longer exists in the mailbox tree.
`RuleEngine.reconcile_folders()` (`classifier/rules.py`) compares active rules
against the live mailbox tree and deactivates any with a missing target.

**When it runs:**

- **Every classification pass** — the orchestrator calls `reconcile_folders`
  at the start of `_execute_run`, before the learning step, so stale rules
  never match during classification.
- **Every bootstrap** — called before rule creation and coverage calculation,
  ensuring deleted-folder evidence is excluded from both.

**What it affects:**

- **Rule deactivation:** active rules targeting a deleted folder are set to
  `active=0`. They are retained (not deleted) so they appear in the rules UI
  for review. If the folder was renamed, the rule can be manually updated and
  re-activated.
- **Bootstrap rule creation:** `_create_rules_from_evidence` filters out
  audit_log evidence pointing to folders not in the live tree, preventing
  rules from being created for deleted folders.
- **Bootstrap coverage:** `_calculate_coverage` excludes deleted-folder
  evidence from both the matched count and the total, so coverage percentage
  reflects only reachable folders.
- **Classification fallback:** if a rule somehow matches but the folder ID
  can't be resolved (e.g., folder deleted between reconciliation and
  classification), the email gets `skip_reason = "unknown_folder"` and is
  not moved.

---

## Database Migration Versioning

Migrations are tracked via a `schema_version` table. On startup, `migrations.py`
checks the current version and applies any pending migrations in order.
Migrations are never skipped or re-applied. See [design/data-models.md](design/data-models.md)
for full schema details.

---

## Google OAuth Setup (Optional)

Required only when enabling web UI authentication (`auth.google_client_id` in
`config.yaml`). When omitted, the web UI remains open.

### 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., "Mailsort") or select an existing one

No APIs need to be enabled — basic OAuth 2.0 / OpenID Connect login works with
just the project and OAuth credentials.

### 2. Configure the OAuth Consent Screen

Navigate to one of (Google shows different UIs depending on the account):
- **Google Auth Platform → Overview** (newer UI), or
- **APIs & Services → OAuth consent screen** (older UI)

1. Click **Get started** (or **Configure consent screen**) if not yet set up
2. Fill in app name ("Mailsort"), your support email, and select **External**
   user type (or Internal if using Google Workspace)
3. Add scopes — click **Add or remove scopes** and check these three:
   - `openid` — "Associate you with your personal info on Google"
   - `.../auth/userinfo.email` — "See your primary Google Account email address"
   - `.../auth/userinfo.profile` — "See your personal info…"

  
### 3. Publish the App

1. Navigate to **Google Auth Platform → Audience**
2. Click **Publish App** to move from "Testing" to "Production"

Since all three scopes are non-sensitive, publishing is instant with no Google
verification required. This removes the 7-day token expiry and the
test-user-only restriction that "Testing" mode has.

### 4. Create OAuth 2.0 Credentials

1. Navigate to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application**
4. Name: "Mailsort" (or any label)
5. **Authorized redirect URIs** — add one per deployment:

| Deployment | Redirect URI |
|------------|--------------|
| Local / development | `http://localhost:8080/auth/callback` |
| Behind reverse proxy | `https://mailsort.example.com/auth/callback` |

   Google rejects raw IP addresses in redirect URIs (except `localhost`).
   For LAN access without a reverse proxy, either access via `localhost`
   with SSH port forwarding, or use a wildcard DNS service like
   [nip.io](https://nip.io) (e.g., `http://192-168-1-50.nip.io:8080/auth/callback`).

6. Click **Create** — note the **Client ID** and **Client Secret**

### 5. Configure Mailsort

**`config.yaml`:**

```yaml
auth:
  google_client_id: "123456789-abc.apps.googleusercontent.com"
  allowed_emails:
    - you@gmail.com
  # session_lifetime_hours: 720   # 30 days (default)
  # redirect_uri: null            # auto-detected (set for reverse proxy)
```

**`.env`:**

```
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxx
```

For Docker deployments, add `GOOGLE_CLIENT_SECRET` to the `environment` section
of `docker-compose.yml` (already included in the template above).

### 6. Reverse Proxy Configuration

When running behind a reverse proxy (e.g., nginx, Caddy, Traefik) with TLS
termination:

1. Set `auth.redirect_uri` explicitly in `config.yaml`:
   ```yaml
   auth:
     redirect_uri: "https://mailsort.example.com/auth/callback"
   ```
2. Ensure the proxy forwards `X-Forwarded-For` and `X-Forwarded-Proto` headers
   (used for IP logging and scheme detection).

### Test Environment Setup

For running auth-related tests locally:

1. Create a **separate** OAuth client ID in the same Google Cloud project
   (or a dedicated test project)
2. Set the redirect URI to `http://localhost:8080/auth/callback`
3. Add the test email to `allowed_emails` in the test config
4. Set environment variables for the test runner:
   ```bash
   export GOOGLE_CLIENT_ID="test-client-id.apps.googleusercontent.com"
   export GOOGLE_CLIENT_SECRET="GOCSPX-test-secret"
   export GOOGLE_TEST_EMAIL="testuser@gmail.com"
   export GOOGLE_TEST_PASSWORD="test-password"  # Only for Playwright browser tests
   ```
5. Unit tests (mocked Authlib) don't need real credentials — only the
   Playwright browser tests require a real Google account

### Security Notes

- **Fail-closed:** Empty `allowed_emails` list rejects everyone (no open access)
- **Session cookie:** `HttpOnly`, `SameSite=Lax`, `Secure` (when HTTPS)
- **Server-side sessions:** Revocable, visible on `/settings`, cleaned up lazily
- **No CSRF token for logout:** `SameSite=Lax` prevents cross-site POST of the
  session cookie; worst case of bypass is forced logout (not destructive)
