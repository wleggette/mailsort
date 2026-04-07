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
