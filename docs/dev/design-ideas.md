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

## ~~Forced Recomputation of Folder Descriptions~~

**Status:** Implemented (2026-04-05) — `mailsort describe` CLI + web UI regeneration.
See `docs/dev/decisions.md` § "Folder description regeneration via `mailsort describe`".
Design choices, alternatives, and rationale are preserved in the decision log.

---

## Google SSO for Web UI

**Status:** Not started (2026-04-06)

### Concept

Gate access to the web UI with Google OAuth 2.0 / OpenID Connect. Currently
the web UI is completely unauthenticated — anyone who can reach the server can
view audit logs, manage rules, and (indirectly via the scheduler) influence
email classification. This is acceptable when the server is only reachable on
localhost, but risky for Docker deployments exposed on a LAN or behind a
reverse proxy.

Single-user design: no `users` table, no per-user `user_id` on `audit_log` or
`rules`. Authentication is a gate, not an identity system. Multi-user support
can be layered on later by adding a `users` table and foreign keys.

### Authentication flow

Uses the standard OAuth 2.0 Authorization Code flow:

1. User visits any protected route (e.g. `/rules`).
2. Auth middleware finds no valid session → 302 redirect to `/auth/login`.
3. `/auth/login` redirects to Google's authorization endpoint with
   `client_id`, `redirect_uri`, `scope=openid email profile`, `state` (CSRF
   nonce), and `response_type=code`.
4. User consents on Google's screen → Google redirects to
   `/auth/callback?code=xxx&state=yyy`.
5. Backend validates `state`, exchanges `code` for tokens by POSTing to
   Google's token endpoint (server-side, via `httpx`).
6. Backend decodes the ID token (JWT) to extract `email`, `name`, `picture`.
7. Backend checks `email` against the configured allowlist. Rejects with 403
   if not allowed.
8. Backend creates a server-side session (row in `sessions` table), sets a
   session cookie with the session ID, and redirects to the originally
   requested page.
9. `/auth/logout` deletes the session row and clears the cookie.

### Configuration

**`config.yaml`** — new top-level `auth` block:

```yaml
auth:
  google_client_id: "123456789.apps.googleusercontent.com"
  allowed_emails:
    - wleggette@ocient.com
  session_lifetime_hours: 720   # 30 days, optional default
  redirect_uri: null            # auto-detected; override for reverse proxy
```

**Auth disabled by default.** When `auth.google_client_id` is absent or null,
the auth middleware is a complete no-op — no redirects, no session checks, no
403s. This keeps the dev/localhost experience frictionless and avoids a
breaking change for existing deployments.

**`redirect_uri`** — normally auto-detected from the request
(`{scheme}://{host}/auth/callback`). Must be explicitly set when the app runs
behind a reverse proxy with TLS termination, since the app sees `http://` but
Google expects the external `https://` URL. Expected values per deployment:

| Deployment | `redirect_uri` setting |
|------------|------------------------|
| Dev (localhost) | `null` (auto → `http://localhost:8080/auth/callback`) |
| Docker direct | `null` (auto → `http://<host>:8080/auth/callback`) |
| Behind reverse proxy | `https://mailsort.example.com/auth/callback` |

**Secrets** — `GOOGLE_CLIENT_SECRET` is loaded from `.env` (which is
`/etc/mailsort/mailsort.secrets` in the production Docker deployment), the
same mechanism used for `FASTMAIL_API_TOKEN` and `ANTHROPIC_API_KEY`.

```
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxx
```

**Setup instructions needed.** The docs must include a guide for creating a
Google OAuth 2.0 client:
- How to create a project in Google Cloud Console
- How to configure the OAuth consent screen (app name, authorized domains)
- How to create OAuth 2.0 credentials (Web application type)
- How to add authorized redirect URIs for each deployment target
- How to obtain the client ID and client secret
- Where to put them (`config.yaml` and `.env` respectively)

### Server-side sessions

Use a `sessions` table in SQLite rather than signed cookies. This adds a small
amount of complexity but provides meaningful benefits for a tool that controls
email routing:

**Pros:**
- Immediate revocation — delete the row and the session is dead.
- Visibility — a `/settings` or future admin page can show active sessions.
- "Log out everywhere" is trivial (`DELETE FROM sessions WHERE email = ?`).
- Session data (email, name, avatar URL) stays server-side; the cookie is just
  an opaque ID.

**Cons:**
- Every authenticated request does a SQLite lookup — but the existing
  middleware already opens a connection per request, so this is negligible.
- Needs periodic cleanup of expired rows — use lazy cleanup in the auth
  middleware itself (delete expired rows on every Nth request, e.g. 1-in-100).
  This works in both `mailsort start` (scheduler) and `mailsort web`
  (standalone web-only mode, which doesn't run APScheduler).
- One more migration (small).

On balance, server-side sessions are worth it. The revocation capability
matters for a tool with access to email.

**Schema (new migration):**

```sql
CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,   -- random UUID or token
    email         TEXT NOT NULL,
    name          TEXT,
    picture_url   TEXT,
    created_at    TEXT NOT NULL,      -- ISO 8601
    expires_at    TEXT NOT NULL
);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
```

### New routes — `web/routes/auth.py`

| Route | Method | Behavior |
|-------|--------|----------|
| `/auth/login` | GET | Build Google auth URL, store `state` nonce in the `sessions` table (as a pending row with short TTL), redirect to Google |
| `/auth/callback` | GET | Validate `state` against pending session row, exchange code for tokens, verify email against allowlist, promote session row to active, set session cookie, redirect to `/` |
| `/auth/logout` | POST | Delete session row, clear cookie, redirect to `/auth/login`. POST (not GET) to prevent logout via `<img>` or link prefetch. Requires CSRF token from the session. |

### Auth middleware

A FastAPI dependency (`get_current_user`) injected into all non-auth routes.
Reads the session cookie, looks up the session row, checks expiry. Returns
the session dict (email, name, picture_url) on success; redirects to
`/auth/login` on failure. This slots in next to the existing database
middleware in `web/app.py`.

**Excluded routes** (no auth check):
- `/auth/*` — login, callback, logout
- `/healthz` — health check endpoint (used by Docker/k8s probes)

**Cookie settings:**
- `HttpOnly` — not accessible from JavaScript
- `SameSite=Lax` — prevents CSRF from cross-origin requests
- `Secure` — only sent over HTTPS (skip in dev when scheme is `http`)
- `Path=/` — available to all routes

**No-op when auth is disabled.** If `google_client_id` is not configured, the
middleware passes through without any session check. Templates receive a null
session object — the avatar/logout UI is simply hidden.

### Template changes

- **`base.html`** — add user avatar + name in the header, plus a logout link.
  The avatar comes from the `picture` claim in Google's ID token (a public
  Google-hosted URL like `https://lh3.googleusercontent.com/a/...`). Render
  with `<img src="{{ session.picture_url }}" class="rounded-full w-8 h-8">`.
  The URL may go stale if the user changes their Google profile photo; it
  refreshes on next login.
- **New `login.html`** — minimal page with a "Sign in with Google" button
  linking to `/auth/login`.
- **Logout** — rendered as a `<form method="POST" action="/auth/logout">`
  with a hidden CSRF token, styled as a text link.

### Settings page — sessions panel

Add a **Sessions** card to the existing `/settings` page:

- **Active sessions table** — each row from `sessions`: device/browser
  (parsed from `User-Agent`, stored at login), created date, expires date.
  The session matching the current request cookie gets a "current" badge.
- **"Revoke" button** per row — deletes that session (`DELETE FROM sessions
  WHERE id = ?`). Grayed out on the current session (use logout instead).
- **"Revoke all other sessions" button** — deletes all sessions except the
  current one (`DELETE FROM sessions WHERE email = ? AND id != ?`). Keeps
  you logged in.
- **Hidden when auth is disabled** — the panel is only rendered when
  `google_client_id` is configured.

Requires storing `User-Agent` in the `sessions` table (add `user_agent TEXT`
column to the schema).

### Library choice

**Decision: Authlib.** First-class Starlette/FastAPI integration, handles the
OAuth dance, token exchange, and JWKS-based JWT validation. One new dependency.
Reduces the auth routes to ~50 lines. Less surface area for subtle OAuth bugs
(nonce validation, token expiry, JWKS rotation) compared to rolling it
manually with httpx + PyJWT.

### What stays the same

- Fastmail and Anthropic API tokens remain server-level secrets, unchanged.
- Scheduler, classifier pipeline, JMAP client, mover — all untouched.
- Database schema for `rules`, `audit_log`, `runs`, etc. — no changes.
- CLI commands (`mailsort run`, `mailsort start`, etc.) — no auth needed,
  these run server-side and don't go through the web UI.

### Testing strategy

#### 1. Unit tests — auth middleware and session logic

Standard pytest tests using FastAPI's `TestClient`, no network, no browser.

**Auth middleware (`test_auth_middleware`):**
- Valid session cookie → request passes through, `request.state.session`
  populated with email/name/picture_url.
- Expired session cookie → 302 redirect to `/auth/login`.
- Missing session cookie → 302 redirect to `/auth/login`.
- Invalid session ID (not in DB) → 302 redirect to `/auth/login`.
- Auth disabled (no `google_client_id`) → all requests pass through
  unconditionally, `request.state.session` is `None`.
- Excluded routes (`/auth/*`, `/healthz`) → no session check regardless of
  auth config.

**Session CRUD (`test_sessions`):**
- Create session → row exists with correct email, expires_at, user_agent.
- Look up session by ID → returns correct data.
- Look up expired session → returns None.
- Delete session by ID → row gone, cookie cleared.
- Revoke all other sessions → only current session survives.
- Lazy cleanup (1-in-N) → expired rows deleted, active rows untouched.

**Allowlist (`test_allowlist`):**
- Email in `allowed_emails` → session created, 302 to `/`.
- Email not in `allowed_emails` → 403, no session created.
- Empty `allowed_emails` list → 403 for everyone (fail-closed).

**Config parsing (`test_auth_config`):**
- `auth` block absent → `auth_enabled` is `False`.
- `auth.google_client_id` present → `auth_enabled` is `True`.
- `redirect_uri` absent → auto-detected from request.
- `redirect_uri` present → used as-is.
- `session_lifetime_hours` absent → defaults to 720.

#### 2. Integration tests — OAuth callback flow (mocked Authlib)

Patch Authlib's `oauth.google.authorize_access_token` to return a fake token
dict with known claims, then test the full callback handler logic:

**Happy path (`test_auth_callback_success`):**
- Pending session row exists with matching `state` nonce.
- `authorize_access_token` returns `{userinfo: {email, name, picture}}`.
- Email is in allowlist → session row promoted to active, session cookie set,
  302 to `/`.
- Subsequent request with session cookie → protected route accessible.

**Rejection (`test_auth_callback_rejected`):**
- Same setup but email is not in allowlist → 403, no active session created.

**Invalid state (`test_auth_callback_bad_state`):**
- `state` param doesn't match any pending session row → 400 or redirect to
  `/auth/login` with error.

**Logout (`test_auth_logout`):**
- POST `/auth/logout` with valid session cookie and CSRF token → session row
  deleted, cookie cleared, 302 to `/auth/login`.
- POST `/auth/logout` without CSRF token → 403.
- GET `/auth/logout` → 405 Method Not Allowed.

#### 3. System tests — auth disabled

The main system test suite (`run_system_test.py`) runs with auth disabled.
The test config omits `auth.google_client_id` entirely. All existing phases
(bootstrap, dry-run, age gate, live run, learning, cleanup) are unaffected.
No auth-related assertions in these phases.

#### 4. UI system tests — authentication (Playwright)

Dedicated Playwright-based test suite for the authentication UI. Requires:
- A Google test account (dedicated, 2FA disabled, no CAPTCHA).
- A real `google_client_id` and `google_client_secret` configured for
  `http://localhost:8080/auth/callback`.
- The mailsort web server running locally with auth enabled.

These tests are **separate from the main system test suite** and run on
demand (not in CI initially — Google's login page is too fragile for
unattended automation).

##### 4a. Template rendering (BeautifulSoup, no browser)

Use FastAPI's `TestClient` to fetch rendered HTML pages with different session
states. Parse with BeautifulSoup to assert element presence/absence.

**Auth enabled, logged in (pre-seeded session):**
- `base.html` renders avatar `<img>` with `src` matching session `picture_url`.
- `base.html` renders user name text.
- `base.html` renders logout `<form>` with `method="POST"` and CSRF token.
- `/settings` renders Sessions card with active sessions table.
- Sessions table shows current session with "current" badge.
- Sessions table shows "Revoke" button (not on current session row).
- Sessions table shows "Revoke all other sessions" button.

**Auth enabled, not logged in:**
- Protected routes return 302 to `/auth/login`.
- `/auth/login` renders "Sign in with Google" button.

**Auth disabled:**
- `base.html` does **not** render avatar, name, or logout form.
- `/settings` does **not** render Sessions card.
- All routes accessible without session cookie.

##### 4b. Full browser flow (Playwright)

Automate the real Google OAuth flow end-to-end. Each test starts with a clean
session state (no cookies, no active sessions in DB).

**Login flow (`test_browser_google_login`):**
1. Navigate to `http://localhost:8080/rules`.
2. Assert redirected to `/auth/login`.
3. Click "Sign in with Google".
4. Assert redirected to Google's consent screen.
5. Fill in test account email → Next → password → Next.
6. (First time only) Click "Allow" on consent screen.
7. Assert redirected back to `http://localhost:8080/auth/callback`.
8. Assert final page is `/rules` (the originally requested page).
9. Assert avatar `<img>` is visible in header.
10. Assert session cookie is set (`HttpOnly`, `SameSite=Lax`).

**Protected route enforcement (`test_browser_protected_routes`):**
1. Without logging in, navigate to each protected route
   (`/`, `/rules`, `/audit`, `/analyze`, `/settings`, `/contacts`, `/folders`).
2. Assert each redirects to `/auth/login`.
3. Navigate to `/healthz` → assert 200 (no redirect).

**Logout flow (`test_browser_logout`):**
1. Log in via the full Google flow (reuse helper from login test).
2. Click the logout button in the header.
3. Assert redirected to `/auth/login`.
4. Navigate to `/rules` → assert redirected to `/auth/login` (session gone).
5. Assert session cookie is cleared.

**Session revocation from settings (`test_browser_session_revoke`):**
1. Log in from browser A (Playwright context 1).
2. Log in from browser B (Playwright context 2).
3. In browser A, navigate to `/settings`.
4. Assert Sessions card shows 2 active sessions.
5. Click "Revoke" on browser B's session row.
6. Assert Sessions card now shows 1 session.
7. In browser B, navigate to `/rules` → assert redirected to `/auth/login`.

**Revoke all other sessions (`test_browser_revoke_all_others`):**
1. Create 3 sessions (contexts A, B, C).
2. In context A, navigate to `/settings`, click "Revoke all other sessions".
3. Assert Sessions card shows 1 session (A only).
4. Contexts B and C → assert redirected to `/auth/login`.

**Rejected email (`test_browser_rejected_email`):**
1. Configure `allowed_emails` to exclude the test account.
2. Attempt the Google login flow.
3. Assert 403 page after callback (not a redirect loop).
4. Assert no session cookie set.

##### Playwright test infrastructure

```
tests/
  ui/
    conftest.py           ← Playwright fixtures, server startup, config
    test_auth_templates.py  ← 4a: BeautifulSoup template assertions
    test_auth_browser.py    ← 4b: Full Playwright browser tests
```

**Fixtures:**
- `auth_config` — generates a test `config.yaml` with auth enabled,
  `google_client_id`, `allowed_emails`, and `db_path` pointing to a temp DB.
- `web_server` — starts `mailsort web` in a subprocess with the test config,
  waits for `/healthz` to return 200, tears down after tests.
- `seeded_session` — inserts a session row directly into the DB and returns
  the session cookie value (for template tests that skip the Google flow).
- `google_credentials` — reads `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_TEST_EMAIL`, `GOOGLE_TEST_PASSWORD` from environment. Skips tests
  if not set.

**Running:**
```bash
# Template tests only (no browser, no Google account needed)
pytest tests/ui/test_auth_templates.py

# Full browser tests (requires Google test account + env vars)
pytest tests/ui/test_auth_browser.py --headed  # visible browser for debugging
pytest tests/ui/test_auth_browser.py            # headless for CI
```

##### Known fragility risks

- **Google login page changes** — selectors for email/password fields may
  break. Mitigation: isolate Google interaction into a single helper function;
  when it breaks, fix in one place.
- **CAPTCHAs** — Google may challenge automated logins. Mitigation: use a
  dedicated test account with low activity, run from a consistent IP. If
  CAPTCHAs become persistent, fall back to the pre-seeded session approach
  (4a) and skip 4b.
- **2FA prompts** — test account must have 2FA disabled.
- **Consent screen re-prompts** — handle the "Allow" button conditionally
  (may not appear after first consent).

### Scope estimate

~150–250 lines of new Python (auth routes, middleware, config fields, one
migration), plus minor template tweaks. Roughly a day of work for the core
auth implementation. Playwright test suite is additional effort (~half day
setup, ongoing maintenance for Google page changes). The fiddliest part is
getting the Google Cloud Console redirect URIs right for each deployment
target (localhost dev, Docker, reverse proxy).

---

## Reduce Redundant LLM Calls

**Status:** Not started (2026-04-07)

### Problem

Every cycle, every inbox email goes through the full classification pipeline
(thread → rules → LLM) even when:

1. **It can't be moved** — flagged, unread, or too-new emails are rejected by
   eligibility gates *after* classification, so the LLM call was wasted.
2. **It was already classified** with the same result last cycle — e.g., the
   LLM returned confidence 0.65 (below threshold 0.80). Nothing changed, but
   the LLM is called again every cycle for the same email.

With 50 inbox emails and 5-minute cycles, this can generate hundreds of
wasted LLM API calls per hour.

### Root cause

In `orchestrator.py`, the per-email loop runs:

```
classify (thread → rules → LLM)  →  build_move_decision  →  eligibility gates
```

Eligibility gates (unread, flagged, too_new) are checked *after*
classification. The LLM — the only expensive step — runs unconditionally for
every email that doesn't match a thread or rule.

### Approach: two layers

#### Layer 1 — Check eligibility before LLM

Move the eligibility check earlier: run thread + rules (cheap DB lookups)
for every email, but only invoke the LLM for emails that are actually
eligible to move.

For ineligible emails (flagged, unread, too_new) where thread + rules miss:
the audit row is still logged, but the LLM is never called.

**What classification_source for ineligible-and-unclassified emails?**

Currently, when classification is `None`, `build_move_decision` fabricates a
Classification with `source="llm"` — this is already misleading (no LLM call
occurred). With the new design, we need an honest source for "we saw the
email but didn't classify it."

**Option A — Keep source="llm" with confidence=0.0, folder="INBOX":**
- Pro: No schema change.
- Con: Pollutes LLM confidence metrics. Misleading in audit log — suggests
  the LLM was called when it wasn't. Analysis queries that filter on
  `source='llm'` would include phantom rows.

**Option B — New source `"system"`:**
- Pro: Semantically accurate — answers "who made this decision?" with "the
  system's built-in eligibility rules." Consistent with the other source
  values (thread, rule, llm, manual, correction) which all name the actor.
  Clean separation from real classifications. Existing analysis queries that
  filter on specific sources (`= 'llm'`, `= 'rule'`, etc.) naturally exclude
  these rows. The `skip_reason` field captures the specific reason (flagged,
  unread, too_new).
- Con: Schema change — need to add `'system'` to the `classification_source`
  CHECK constraint (migration required). Queries using `!= 'manual'` in the
  dedup CTE would include system rows, but the dedup selects the latest row
  per email, so a real classification supersedes the system row if the email
  later becomes eligible.
- **Downstream impact:** The learner's Cat 1 detection (`_detect_skipped_sorts`)
  queries `WHERE moved = 0` — system-source rows satisfy this, so the
  learner still detects when a user manually sorts an email that mailsort
  had skipped. No learner change needed.

**Recommendation: Option B (`"system"` source).** It's the only honest
representation, and the migration is trivial. "System" was chosen over
"skipped" because "skipped" conflicts with the existing skip_reason
vocabulary — many emails are "skipped" for various reasons (below_threshold,
llm_skip_senders, etc.) but still have a real classification source.

#### Layer 2 — Cache prior LLM classifications

For eligible emails where thread + rules miss, check whether a prior LLM
classification exists in `audit_log` before making a new API call.

**Lookup:** Before calling the LLM, query:
```sql
SELECT target_folder, confidence, llm_reasoning, skip_reason
FROM audit_log
WHERE email_id = ?
  AND classification_source = 'llm'
ORDER BY id DESC LIMIT 1
```

If a valid cached result exists, reuse it as the classification. The email
still gets a new MoveDecision and audit row — just without an API call.

**Invalidation — when to re-classify despite a cached result:**

1. **New rule covers this email** — not an issue. Thread → rules run first;
   if a rule now matches, the pipeline never reaches the LLM tier. The cache
   is only consulted when thread + rules both miss.
2. **Threshold config changed** — not an issue. The cached *confidence* is
   still valid; only the move decision threshold changes. The mover
   re-evaluates the cached confidence against the current threshold.
3. **Folder descriptions changed** — the LLM might classify differently with
   updated descriptions. This is a true invalidation signal.
4. **LLM model changed** — different model, different answer.

Signals 1–2 are naturally handled without any cache logic. Signals 3–4 are
rare (user-triggered). Handle via global version invalidation:

**Classification version** — a SHA-256 hash of (folder descriptions content +
LLM model name), computed once at the start of each run. Two keys in the
`learner_state` table (global key-value store):
- `classification_version` — the current hash.
- `classification_version_changed_at` — ISO timestamp of last change.

At each run start: compute the hash, compare to stored value. If different,
update both keys. The cache lookup only considers audit rows created after
the last version change:

```sql
SELECT target_folder, confidence, llm_reasoning, skip_reason
FROM audit_log
WHERE email_id = ?
  AND classification_source = 'llm'
  AND created_at >= ?  -- classification_version_changed_at
ORDER BY id DESC LIMIT 1
```

**On version change** (description regeneration, model change): all cached
LLM results are invalidated. The next cycle calls the LLM for every eligible
email — exactly the same cost as today's behavior. After that one cycle, all
results are cached again. Zero regression from today in the worst case.

**No TTL needed.** Given the same email content, folder descriptions, and
model, the LLM will produce essentially the same classification. The version
hash handles the only scenarios that would produce a different result. Emails
naturally leave the cache when they leave the inbox (no longer fetched).

**Cache scope:** The `audit_log` table IS the cache. No new table needed,
no new columns on `audit_log`. Only the two `learner_state` keys are added.

#### Pipeline refactor — split into two methods

Currently `pipeline.classify()` is a single method running all three tiers.
With this change, the orchestrator needs to run thread + rules first (for all
emails), then conditionally call the LLM (only for eligible, non-cached
emails). A single method with a `skip_llm` flag would cause thread + rules to
run twice — once to check, then again inside the full pipeline call.

**Split into two methods:**

```python
class ClassificationPipeline:
    def classify_without_llm(self, features) -> tuple[Classification | None, str | None]:
        """Thread context + rule engine only. No network calls."""
        clf = self._resolve_thread_context(features)
        if clf:
            return clf, None
        clf = self._rules.classify(features)
        if clf:
            return clf, None
        return None, None

    def classify_llm(self, features) -> tuple[Classification | None, str | None]:
        """LLM classification only. Assumes thread + rules already missed."""
        if self._llm is None:
            return None, "llm_unavailable"
        allowed, skip_reason = self._llm.should_call(features, self._contacts)
        if not allowed:
            return None, skip_reason
        contact = get_contact_for_sender(features, self._contacts)
        clf = self._llm.classify(features, self._folder_descriptions, contact=contact)
        if clf.reasoning == "api_error":
            return None, "llm_api_error"
        return clf, None
```

The existing `classify()` method can remain as a convenience wrapper that
calls both, preserving backward compatibility for any callers.

### Revised per-email flow

```python
for features in eligible:
    # 1. Check eligibility once (reused in steps 3 and 6)
    ineligible_reason = _check_eligibility(features, cfg)

    # 2. Always run thread + rules (cheap)
    classification, skip_reason = pipeline.classify_without_llm(features)

    if not classification:
        if ineligible_reason:
            # 3. Ineligible, no cheap match — record as system gate
            classification = Classification(
                folder_path="INBOX", confidence=0.0,
                source="system", reasoning=ineligible_reason,
            )
            skip_reason = ineligible_reason
        else:
            # 4. Eligible — check LLM cache before calling API
            cached = _get_cached_llm_result(db, features.email_id, version)
            if cached:
                classification = cached
                skip_reason = None  # build_move_decision re-derives from confidence
            else:
                classification, skip_reason = pipeline.classify_llm(features)

    # 5. Build move decision (confidence gate applied here)
    decision = build_move_decision(
        features=features,
        classification=classification,
        contacts=contacts,
        thresholds=cfg.classification.thresholds,
        skip_reason=skip_reason,
    )

    # 6. Eligibility gate — apply to ALL paths (thread/rule/cached/fresh)
    if decision.should_move and ineligible_reason:
        decision.should_move = False
        decision.skip_reason = ineligible_reason
```

**Note on `build_move_decision`:** Currently fabricates `source="llm"` when
`classification is None`. With the new flow, classification should never be
`None` by step 4 (the system gate always constructs one). The fallback should
still be updated to `source="system"` for safety.

### Configuration

No new config fields. The cache is always active and invalidated by
`classification_version` changes. No TTL to configure.

### What stays the same

- Thread and rule classification — always runs, unchanged.
- Audit logging — every email gets an audit row, including system-gated ones.
- Learner Cat 1 detection — still finds system-gated emails that the user moved.
- Move execution — unchanged.
- Dry run — same optimization applies (classification cost, not move cost).

### Schema change

Add `'system'` to the `classification_source` CHECK constraint:

```sql
CHECK(classification_source IN ('thread','rule','llm','manual','correction','system'))
```

**No backfill needed.** Existing audit rows with `skip_reason` of flagged,
unread, or too_new have real LLM classifications (the LLM was called under
the old flow). Those rows are honest `source='llm'` and should stay as-is.
The `'system'` source only applies to new rows going forward where the LLM
is never called.

### Query impact analysis

Most existing queries use positive filters (`= 'llm'`, `= 'rule'`, etc.)
and naturally exclude `'system'` rows. Learner queries that use `!= 'manual'`
or `NOT IN ('manual','correction')` also pair with `moved = 1`, which
excludes system rows since they always have `moved = 0`.

**Two places need code changes:**

1. **Dedup CTE** (`analyze.py` lines 28,35 and `main.py` lines 526,533):
   Currently `classification_source != 'manual'`. A system row could become
   the "latest" row for an email_id, hiding a prior real classification.
   Change to `NOT IN ('manual', 'system')`.

2. **Source breakdown** (`analyze.py` line 53 and `main.py` equivalent):
   Currently `GROUP BY classification_source` with no filter. System rows
   would appear as a "system" bucket in the breakdown. Add
   `WHERE classification_source != 'system'` (or `NOT IN`).

**Safe as-is (no changes needed):**

- `analyze.py` / `main.py`: LLM confidence distribution (`= 'llm'`),
  corrections count (`= 'correction'`), skipped-then-sorted
  (`= 'llm' AND moved = 0`), rule corrections (`= 'rule' AND moved = 1`)
  — all positive filters.
- `rules.py`: rule detail stats (`= 'correction'`, `= 'manual'`) — positive
  filters.
- `audit.py`: source filter (`= ?`) — user-selected, just add to dropdown.
- `learner.py`: Cat 1 (`!= 'manual' AND moved = 1`), Cat 2
  (`NOT IN ('manual','correction') AND moved = 1`), `_already_handled`
  (`NOT IN ('manual','correction') AND moved = 1`), re-sort checks — all
  guarded by `moved = 1`, which system rows never satisfy.

### UI changes

**Audit log page** (`audit/list.html`): Add `"system"` to the source filter
dropdown. Add a badge color for system rows (e.g., `bg-amber-50
text-amber-700`). Same badge addition in `audit/detail.html` and
`rules/detail.html` source badges.

**Analysis page:** No changes needed. System-gated emails represent
transient states (flagged, unread, too_new) — the email will eventually
become eligible and get a real classification, or the user will act on it.
They are not meaningful for classification accuracy metrics. The existing
analysis queries filter on specific sources (`'llm'`, `'rule'`, etc.) and
naturally exclude system rows.

### Impact estimate

- **Layer 1** eliminates LLM calls for flagged + unread + too_new emails.
- **Layer 2** eliminates repeat LLM calls for below_threshold emails.
- **Combined:** ~60–80% reduction in LLM API calls per cycle for a typical
  inbox with long-lived flagged/unread messages.

### Scope estimate

~50–80 lines of new/changed Python across orchestrator + pipeline + config +
migration. Half-day implementation including tests.

### Documentation changes required

**`docs/architecture.md`** — Per-Run Sequence diagram:
- Step 4 (Classify): restructure to show `classify_without_llm` for all
  emails, then eligibility gate before LLM, then cache check before API call.
  Add `source="system"` outcome path.
- Step 5 (Build Move Decision): eligibility gates section (lines 367-372)
  needs to reflect that eligibility is checked once at the top and reused in
  two places (gate LLM + prevent move).

**`docs/design/classification.md`**:
- Pipeline Steps (lines 1-18): reorder to reflect eligibility before LLM.
- Eligibility & Gating (lines 39-56): update the claim "all remaining inbox
  emails are classified (thread → rules → LLM)" — LLM is now conditional.
- New section: LLM classification cache (`classification_version` mechanism,
  `learner_state` keys, cache lookup query).
- New section: `classify_without_llm` / `classify_llm` method split.

**`docs/design/data-models.md`**:
- `audit_log` CHECK constraint (line 65): add `'system'`.
- `skip_reason` comment (lines 69-73): note that system source uses
  flagged/unread/too_new.
- `Classification` model (line 200): add `"system"` and `"correction"` to
  source comment.
- `learner_state` known keys (lines 157-160): add
  `classification_version` and `classification_version_changed_at`.
- Migration list (lines 223-236): add migration 12 for `'system'` CHECK.

**`docs/design/audit.md`**:
- Outcome Categories table (lines 38-51): note system-gated emails appear
  with `source=system` and skip_reason of flagged/unread/too_new.
- Log format (lines 56-75): add "System gate:" line to classification
  breakdown, or note how system-gated counts are reported.

**`docs/configuration.md`**: No changes (no new config fields).

**`docs/planning/system-test-plan.md`**:
- §4.1 Eligibility Gate Scenarios (lines 531-542): E2/E3/E4/E5 should
  show `classification_source='system'` for ineligible emails with no
  rule match. Add scenario: ineligible email WITH rule match still gets
  `source=rule`.
- §4.2 Classification Source Scenarios (lines 544-559): add scenarios for
  LLM cache hit and system gate.
- §4.4 Dry Run Checklist (line 582): refine "LLM called only when no
  rule/thread match" → "…AND email is eligible AND no valid cache."
- §4.5 Dynamic Inbox Emails (lines 584-617): E2/E3/E4/E5 expected
  outcomes should show `source=system` for no-rule-match cases.
- §8.3: consider adding scenario for cache version invalidation.
