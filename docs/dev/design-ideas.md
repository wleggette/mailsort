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

## ~~Reduce Redundant LLM Calls~~ → Implemented

**Implemented 2026-04-08.** See `docs/dev/decisions.md` §"LLM classification
cache (cache only, no eligibility gate)" for the decision record.

### Problem

Every cycle, every inbox email goes through the full classification pipeline
(thread → rules → LLM) even when it was already classified with the same
result last cycle — e.g., the LLM returned confidence 0.65 (below threshold
0.80). Nothing changed, but the LLM is called again every cycle.

With 50 inbox emails and 5-minute cycles, this can generate hundreds of
wasted LLM API calls per hour.

### Design decision: classify everything, cache LLM results

An earlier draft considered gating LLM calls for ineligible emails (flagged,
unread, too_new) by checking eligibility *before* classification. This was
rejected because it loses audit visibility: a flagged email with no rule match
would show `source="system"` instead of what the LLM would have classified it
as. Seeing "the LLM thinks this flagged email belongs in Affairs/Stores" is
useful — you know what will happen when you unflag it.

The LLM cache alone handles the repeated-call problem. After the first LLM
classification of an email, subsequent runs reuse the cached result. The
eligibility gate would only have saved one LLM call per email — the cache
eliminates all the rest.

**Approach: classify everything (thread → rules → cache → LLM), gate moves
after.** The existing flow is preserved; only an LLM cache layer is inserted
between the rule check and the actual LLM API call.

### LLM classification cache

For emails where thread + rules miss, check whether a prior LLM
classification exists in `audit_log` before making a new API call.

**Cache lookup** — before calling the LLM, query:
```sql
SELECT target_folder, confidence, llm_reasoning
FROM audit_log
WHERE email_id = ?
  AND classification_source = 'llm'
  AND created_at >= ?  -- classification_version_changed_at
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
the last version change.

**On version change** (description regeneration, model change): all cached
LLM results are invalidated. The next cycle calls the LLM for every email
that falls through to the LLM tier — exactly the same cost as today. After
that one cycle, results are cached again. Zero regression in the worst case.

**No TTL needed.** Given the same email content, folder descriptions, and
model, the LLM will produce essentially the same classification. The version
hash handles the only scenarios that would produce a different result. Emails
naturally leave the cache when they leave the inbox (no longer fetched).

**Cache scope:** The `audit_log` table IS the cache. No new table needed,
no new columns on `audit_log`. Only the two `learner_state` keys are added.

### Pipeline refactor — split into two methods

Currently `pipeline.classify()` is a single method running all three tiers.
With the cache, the orchestrator needs to run thread + rules first, then
check the cache, then conditionally call the LLM. A single method with a
`skip_llm` flag would cause thread + rules to run twice.

**Split into two methods:**

```python
class ClassificationPipeline:
    def classify_without_llm(self, features) -> tuple[Classification | None, str | None]:
        """Thread context + rule engine only. No network calls."""
        ...

    def classify_llm(self, features) -> tuple[Classification | None, str | None]:
        """LLM classification only. Assumes thread + rules already missed."""
        ...
```

The existing `classify()` method remains as a convenience wrapper that calls
both, preserving backward compatibility.

### Revised per-email flow

```python
for features in all_emails:
    # 1. Cheap classification (thread + rules)
    classification, skip_reason = pipeline.classify_without_llm(features)

    if not classification:
        # 2. Check LLM cache before calling API
        cached = _get_cached_llm_result(db, features.email_id, version_changed_at)
        if cached:
            classification = cached
            skip_reason = None
            cache_hits += 1
        else:
            # 3. Fresh LLM call
            classification, skip_reason = pipeline.classify_llm(features)

    # 4. Build move decision (confidence gate)
    decision = build_move_decision(features, classification, contacts, thresholds, skip_reason)

    # 5. Eligibility gates (unread, flagged, too_new) — same as today
    # These are applied post-classification so the audit log shows
    # what the classification *would* be, even for ineligible emails.
```

### Cache hit tracking

Add a `cached BOOLEAN NOT NULL DEFAULT 0` column to `audit_log` (in migration
12, alongside the `'system'` source). When a cache hit is used, the
orchestrator sets a `cached=True` flag on the `MoveDecision`, which the
`AuditWriter` writes through to the column.

This enables future queries like:
```sql
-- LLM API calls vs cache hits over the last 7 days
SELECT cached, COUNT(*) FROM audit_log
WHERE classification_source = 'llm' AND created_at >= datetime('now', '-7 days')
GROUP BY cached
```

No UI changes needed now — the data is available for dashboards later.

### `build_move_decision` fallback — `source="system"`

Currently fabricates `source="llm"` when `classification is None` (LLM
unavailable, API error, privacy gate). This is misleading — no LLM call
occurred. Change to `source="system"` which honestly represents "the system
produced this decision, not the LLM."

This requires adding `'system'` to the `classification_source` CHECK
constraint (migration 12). The `source="system"` rows only appear when
classification truly fails — not for eligibility gating (since all emails
are now fully classified).

### Schema change

Add `'system'` to the `classification_source` CHECK constraint:

```sql
CHECK(classification_source IN ('thread','rule','llm','manual','correction','system'))
```

**No backfill needed.** Existing rows are all legitimate.

### Query impact analysis

`source="system"` rows are rare (only LLM-unavailable/error cases) and always
have `moved = 0`. Most existing queries use positive filters (`= 'llm'`,
`= 'rule'`, etc.) and naturally exclude them.

**Two places need code changes:**

1. **Dedup CTE** (`analyze.py` and `main.py`):
   `!= 'manual'` → `NOT IN ('manual', 'system')` to prevent a system row
   from hiding a prior real classification.

2. **Source breakdown**: system rows would appear as a bucket. Add exclusion.

**Safe as-is:** all learner queries are guarded by `moved = 1` (system rows
never satisfy this). All other queries use positive source filters.

### UI changes

- **Audit log page**: Add `"system"` to source filter dropdown and amber
  badge color in list, detail, and rule detail templates.
- **Analysis page**: No changes — system rows are excluded by existing
  positive filters.

### Impact estimate

The cache eliminates repeat LLM calls for **all** emails that remain in the
inbox across runs — flagged, unread, too_new, and below_threshold alike.
After the first classification of each email, no further LLM API calls are
made unless the classification version changes.

~60–80% reduction in LLM API calls per cycle for a typical inbox.

### Scope estimate

~50–80 lines of new/changed Python across orchestrator + pipeline + migration.
Half-day implementation including tests.

### Documentation changes required

**`docs/architecture.md`** — Per-Run Sequence diagram:
- Step 4 (Classify): show `classify_without_llm` then cache check then
  `classify_llm`. No eligibility change.

**`docs/design/classification.md`**:
- New section: LLM classification cache (`classification_version` mechanism,
  `learner_state` keys, cache lookup query).
- Pipeline steps: note cache check between rules and LLM.

**`docs/design/data-models.md`**:
- `audit_log` CHECK constraint: add `'system'`.
- `audit_log`: add `cached BOOLEAN NOT NULL DEFAULT 0` column.
- `MoveDecision` model: add `cached: bool = False`.
- `Classification` model: add `"system"` and `"correction"` to source comment.
- `learner_state` known keys: add `classification_version` and
  `classification_version_changed_at`.
- Migration list: add migration 12.

**`docs/design/audit.md`**:
- Outcome categories: add system row (LLM-unavailable/error only).
- Log format: add LLM cache hits to classification summary.

**`docs/planning/system-test-plan.md`**:
- §4.2: add scenario for LLM cache hit.
- §4.4: refine "LLM called only when no rule/thread match" →
  "…AND no valid cache."
- §8.3: consider scenario for cache version invalidation.

---

## Analysis Page Improvements — "Skipped Emails You Later Sorted"

**Status:** Investigating (2026-04-06)

### Bug: Inflated counts in skipped-then-sorted query

The "Skipped Emails You Later Sorted" section reports **1534 skipped LLM emails**
and **131 same-folder matches**. These are audit_log rows, not distinct emails.

**Root cause:** The query in `analyze.py` (lines 107-119) joins `audit_log a1`
(LLM skip rows) against `audit_log a2` (manual/correction rows) on `email_id`.
An email that sat in the inbox for N cycles has N `source='llm', moved=0` audit
rows. Each joins against the manual sort row, producing N output rows per email.

**Actual numbers (30-day window):**

| Metric | Reported (rows) | Actual (distinct emails) | Inflation |
|--------|-----------------|--------------------------|-----------|
| Total skipped-then-sorted | 1,534 | 41 | ~37× |
| Same-folder matches | 131 | 5 | ~26× |

**Fix:** Deduplicate per `email_id`, keeping only the most recent LLM audit row.
Use `ROW_NUMBER() OVER (PARTITION BY a1.email_id ORDER BY a1.id DESC)`.

### Full accounting: 173 LLM-source emails

The analysis page's dedup CTE shows 173 LLM emails, 110 moved, 63 skipped.
The "skipped-then-sorted" section only covers the 41 that the user later
manually sorted. The full breakdown of all 63 skipped:

| Category | Count | Notes |
|----------|-------|-------|
| User later sorted (skipped-then-sorted) | 41 | Shown on analysis page |
| Still in inbox — flagged | 13 | User intentionally keeping these |
| Still in inbox — below_threshold | 5 | LLM unsure, user hasn't acted |
| Still in inbox — known_contact threshold | 3 | Pending user action |
| Still in inbox — too_new | 1 | Waiting for age gate |
| **Total skipped** | **63** | |

Eligibility-gated emails (flagged/unread/too_new) should not count toward
actionable items since they'll auto-sort once the gate clears. However, the
totals should account for all 63 so numbers add up to the 173 shown at the
top of the page. Show eligibility-gated counts as a footnote or collapsed
"informational" section.

### Skip reason breakdown (41 later-sorted emails)

| Skip Reason | Count |
|-------------|-------|
| `below_threshold` | 21 |
| `below_threshold_known_contact` | 12 |
| `unread` | 3 |
| `too_new` | 3 |
| `flagged` | 2 |

### Confusion analysis

**Wrong-folder patterns (36 of 41 later-sorted had wrong LLM folder):**

| LLM said → User moved to | Count | Pattern |
|---------------------------|-------|---------|
| `INBOX` → `Affairs/Stores` | 14 | LLM returned conf 0.15–0.35. "I don't know." |
| `INBOX` → `Affairs/Medical` | 5 | Same — LLM doesn't recognize these senders/topics |
| `People/Friends` → `People/Family` | 4 | All `yzhuang1@gmail.com` — folder disambiguation |
| `INBOX` → `People/Family` | 2 | Low-confidence known contact emails |
| Various → `Affairs/Stores` | 5 | Alerts, Support, Gardening → all treated as "stores" |
| Various → `People/Family` | 4 | Uncommon, Stores, Gardening → actually family |
| Other one-offs | 2 | |

**Known-contact confusion is almost entirely one sender.**
Of 14 known-contact-threshold emails, 12 are `yzhuang1@gmail.com` (user's
husband). He writes on many topics (taxes, gardening, suction cups, hospital
updates, real estate, AI), so the LLM scatters his emails across
`People/Friends`, `People/Family`, `Affairs/Residence`, `Projects/*`,
`Affairs/Stores`, `Affairs/Uncommon`, and `INBOX`. This is an inherently hard
problem — the correct folder depends on *content*, but the contact threshold
gates the move. The `People/Friends` → `People/Family` confusion (4 emails)
is the most common specific error.

This is **expected behavior for a multi-topic contact** and will not improve
with threshold tuning alone. The folder descriptions for `People/Friends` vs
`People/Family` could be refined to better distinguish them. Over time, as
rules are created from evidence, many of these will be caught by thread-context
or sender rules before hitting the LLM.

**`Affairs/Stores` is functioning as a catch-all for misc commercial/civic
email.** 14 emails classified as `INBOX` (low confidence) were later moved
there. The fix is to improve the `Affairs/Stores` folder description so the
LLM understands its scope — newsletters, municipal notices, one-off commercial
emails, civic communications, etc.

### Redesigned analysis page — action-oriented cards

Replace the current flat "Skipped Emails You Later Sorted" section with
multiple cards, each answering a specific question and prompting a specific
action. Each card should cover one aspect of system performance.

**Principle:** Every card answers "what happened?", "why?", and "what should
I do about it?" The user shouldn't have to interpret raw data.

#### Card 1: "Folder Description Gaps"

**Question:** Which folders is the LLM failing to classify emails into?

**Data:** Emails where the LLM returned low confidence (< 0.60) or classified
to `INBOX` (meaning "I don't know"), but the user later moved them to a
specific folder. Grouped by **destination folder** (where the user moved them).

**Layout — folder group cards with inline links:**

Each folder group is a mini-card (white bg, border) with:

```
┌─ Affairs/Stores ─────────────────────────────────────────────────┐
│  14 emails the LLM couldn't classify  →  Review description      │
│                                                                   │
│  LLM said: INBOX (10), Affairs/Alerts (2), Projects/* (2)        │
│                                                                   │
│  From               Subject                        Confidence     │
│  chicagoelec…       Use Any of 52 Secure Drop…     0.15    [→]   │
│  chicagoelec…       Return Your Vote By Mail…      0.15    [→]   │
│  chicagoelec…       Last Chance to Return…          0.15    [→]   │
│  wordpress.com      Gap Week, March 27…            0.30    [→]   │
│  wordpress.com      Miscellanea: The War…          0.15    [→]   │
│  …                                                                │
│  ▸ Show 9 more                                                    │
└──────────────────────────────────────────────────────────────────┘
```

**Navigation links (reusing existing pages):**

- **"Review description"** → `/folders` with `?highlight=Affairs/Stores`
  (or anchor link `#folder-affairs-stores`). The folders page already shows
  descriptions with per-folder regenerate links.
- **`[→]` per row** → `/audit?sender=chicagoelections.gov&source=llm&days=30`
  — reuses the existing audit log filter page to show all LLM classifications
  for that sender. The audit list already supports sender/source/days filters.
- **Subject link** → If the audit_log row id is available, link directly to
  `/audit/{id}` for the full classification detail (LLM reasoning, confidence,
  thread context, email history across runs).
- **Sender link** → If a rule exists for that sender, link to `/rules/{id}`
  showing the rule's evidence and performance. If no rule exists, link to
  `/audit?sender=X` to show all history.

**Collapsible rows:** Show top 5 emails per folder by default. "Show N more"
expands the rest. Keeps the page scannable when multiple folders have gaps.

**Existing patterns reused:**
- Table styling from rules detail "Evidence Emails" section (same columns:
  From, Subject, source badge, confidence)
- Source badges from `audit/detail.html` (llm=purple, rule=blue, etc.)
- Link patterns from `audit/list.html` (filter by sender, source, days)
- Card layout from dashboard (white bg, border, header with action link)

#### Card 2: "Known Contact Sorting"

**Question:** How are emails from known contacts being handled — and what
mechanisms are working to sort them correctly?

**Data:** For each known contact that has `below_threshold_known_contact`
skips, show the full picture: threshold blocks, thread-context sorts that
succeeded, existing rules that cover them, and folder coherence.

**Layout — per-contact card:**

```
┌─ yzhuang1@gmail.com ─────────────────────────────────────────────┐
│  Known contact · Relationship: spouse                             │
│                                                                   │
│  ┌── How emails are being sorted (30d) ──────────────────────┐   │
│  │  Thread context:  8 emails auto-sorted                     │   │
│  │  Rules:           0 (no active rule for this sender)       │   │
│  │  LLM moved:       2 emails (above 0.93 threshold)         │   │
│  │  Threshold-blocked: 12 emails (conf 0.75–0.85, need 0.93) │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                   │
│  Folder coherence for this sender:                                │
│    People/Family   ████████████░░  72% (18 of 25 emails)         │
│    People/Friends  ███░░░░░░░░░░░  12% (3 emails)                │
│    Affairs/*       ██░░░░░░░░░░░░   8% (2 emails)                │
│    Other           ██░░░░░░░░░░░░   8% (2 emails)                │
│                                                                   │
│  ⚠ Coherence is below the auto-rule threshold (80%).              │
│    The LLM often picks People/Friends instead of People/Family.   │
│    Thread context is the most effective sorting mechanism for      │
│    this contact — it sorted 8 emails correctly this period.       │
│                                                                   │
│  Threshold-blocked emails:                                        │
│  Subject                           LLM said         User moved    │
│  Re: Johnny in hospital            People/Family    People/Family  │  [→]
│  when you have a moment…           People/Family    People/Family  │  [→]
│  Costco planter - on sale…         Gardening        People/Family  │  [→]
│  Re: Suction cup                   People/Friends   People/Family  │  [→]
│  ▸ Show 8 more                                                    │
│                                                                   │
│  What's working: Thread context sorts handle 8 of this contact's  │
│  emails automatically. The 0.93 threshold is preventing incorrect  │
│  moves — the LLM picks the wrong folder for 10 of 12 blocked     │
│  emails.                                                          │
│                                                                   │
│  Actions:                                                         │
│    → Compare People/Friends vs People/Family descriptions          │
│    → View all audit entries for this sender                        │
└──────────────────────────────────────────────────────────────────┘
```

**Key additions beyond the original design:**

1. **Thread context visibility** — show how many emails were successfully
   sorted by thread context. This is often the primary mechanism for
   multi-topic contacts. Query: `classification_source='thread'` for the
   sender in the period.

2. **Folder coherence bar** — shows where this sender's emails actually go.
   If coherence is below the auto-rule threshold (80%), explain why no rule
   exists. If above, note that a rule could/should exist.
   Uses the same coherence calculation as `maybe_create_rule` in the learner.

3. **Existing rules** — show any active rules that match (exact_sender,
   sender_domain). Link to `/rules/{id}`. If none exist and coherence is
   high, explain what threshold is missing.

4. **"What's working" summary** — synthesized text explaining the interplay:
   - Thread context sorted N emails (good — this works for reply chains)
   - Threshold blocked N emails, of which M were wrong-folder (good —
     threshold is protecting)
   - N emails were correctly classified but blocked (potential improvement)

**Navigation links:**
- **Contact name** → `/contacts?search=yzhuang1` (existing contacts page)
- **Folder names** → `/folders#folder-path` or `/audit?folder=X`
- **Per-email `[→]`** → `/audit/{id}` (existing audit detail)
- **"View all audit entries"** → `/audit?sender=yzhuang1@gmail.com&days=30`
- **"Compare descriptions"** → `/folders` (could add anchor links)

**When to show this card:** Only when there are ≥3 `below_threshold_known_contact`
skips for a sender in the period. If all known-contact blocks are from one
sender, show one card. Multiple senders get multiple cards.

#### Card 3: "Folder Disambiguation"

**Question:** Is the LLM confusing two similar folders?

**Data:** Emails where the LLM had moderate-to-high confidence (≥ 0.60) but
picked the wrong folder. Grouped by **(llm_folder → user_folder)** pairs.

**Display:**
```
People/Friends → People/Family (4 emails)
  All from yzhuang1@gmail.com
  The LLM can't distinguish these folders for this sender.
  → Compare folder descriptions (link to both)

Affairs/Uncommon/Support → Affairs/Stores (2 emails)
  → Compare folder descriptions
```

**Action:** "Compare folder descriptions" — link to `/folders` or show the
two descriptions side-by-side so the user can spot overlap.

#### Card 4: "Learning Effectiveness"

**Question:** How well is the system learning from your manual sorts?

The learner already auto-creates rules when evidence thresholds are met
(`maybe_create_rule` in `learner.py`). Suggesting rules manually would be
redundant. Instead, this card reports on how effective that learning loop is.

**Data:**
- Rules created post-bootstrap (learned from your manual sorts)
- How many emails those learned rules have subsequently sorted
- Bootstrap rules vs learned rules comparison
- Evidence sources that triggered rule creation

**Layout:**

```
┌─ Learning Effectiveness ─────────────────────────────────────────┐
│                                                                   │
│  ┌── Rule creation ────────────────────────────────────────────┐ │
│  │  Bootstrap rules:    120 rules → 1,182 emails sorted        │ │
│  │  Learned rules:        6 rules →   411 emails sorted        │ │
│  │  Manual rules:         0                                     │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  Rules learned from your manual sorts:                            │
│                                                                   │
│  Rule                          Folder             Hits  Status   │
│  wordpress.com                 Affairs/Stores      197  active   │  [→]
│  chicagopubliclibrary.org      Affairs/Stores      195  active   │  [→]
│  milla@besttherapies.org       Affairs/Medical      19  active   │  [→]
│  allycbentz@gmail.com          Affairs/Medical       0  active   │  [→]
│  HomeDepot@order.homedepot.com Affairs/Stores        0  active   │  [→]
│  chicagoelections.gov          Affairs/Stores        0  active   │  [→]
│                                                                   │
│  All 6 rules were created from manual sort evidence.              │
│  Top performers: wordpress.com and chicagopubliclibrary.org       │
│  alone account for 392 emails sorted automatically.               │
│                                                                   │
│  ▸ View all rules by source                                      │
└──────────────────────────────────────────────────────────────────┘
```

**What this shows the user:**
- The system is learning: your manual sorts create rules that subsequently
  automate classification for similar emails.
- Quantifies the payoff: "you sorted 3 wordpress.com emails → the system
  created a rule that has since sorted 197 emails for you."
- If no rules have been learned recently, it indicates either: (a) manual
  sorts don't repeat enough to meet thresholds, or (b) rules already cover
  the senders you sort. Both are okay — the card explains which.

**Navigation links:**
- **Rule name `[→]`** → `/rules/{id}` (existing rule detail with evidence,
  coherence, performance stats)
- **"View all rules by source"** → `/rules?filter=all&search=` with a new
  filter option for `source=auto` to separate bootstrap vs learned rules
  (requires adding a source filter to the rules page — minor enhancement)
- **Hit count** → `/audit?source=rule&days=30` filtered to that rule's
  matches

**Evidence source breakdown** (shown on hover or in expanded view):
For each learned rule, show what triggered its creation:
- `manual: 3` = you sorted 3 emails from this sender
- `rule: 2` = the rule has since sorted 2 more (confirming evidence)
- `thread: 5` = thread context added evidence

This reuses the same evidence query from the rules detail page.

#### Card 5: "Eligibility-Gated Emails" (informational/collapsed)

**Question:** How many skipped emails were held back by eligibility gates
rather than confidence?

**Data:** Count of emails skipped for `unread`, `flagged`, `too_new`. These
require no action — they'll auto-sort on the next eligible cycle.

**Display:** A single collapsed line:
```
▸ 20 emails held by eligibility gates (flagged: 13, unread: 3, too_new: 4)
  These will be sorted automatically once the gate condition clears.
```

**Action:** None. Purely informational to explain why the numbers add up.

#### Card 6: "LLM Accuracy Summary" (replaces current recommendation)

**Question:** Overall, how well is the LLM performing?

**Data:** Of 173 LLM-classified emails:
- 110 moved successfully (correction rate from existing card)
- 63 skipped: 20 eligibility-gated, 43 threshold-gated
- Of 41 later sorted by user: 5 same-folder (LLM was right), 36 wrong-folder

**Display:** Compact summary with key ratios. No "consider lowering threshold"
recommendation unless the same-folder count is significant (e.g., > 10 emails
AND > 20% of threshold-blocked emails).

**Action:** Links to the other cards for specific actions.

### Implementation approach

1. **Fix the dedup bug first** — straightforward query change in `analyze.py`.
   Add `skip_reason` to the query. This is a prerequisite for everything else.

2. **Restructure the template** — replace the single flat table with the card
   layout above. The backend query can be a single query that returns all
   skipped-then-sorted emails with skip_reason; the grouping/categorization
   happens in the route handler or template.

3. **Add the "still in inbox" accounting** — query for LLM-skipped emails
   that were NOT later sorted, to make the totals add up.

4. **Learning effectiveness card** — query rules by creation date relative
   to bootstrap, compute hit counts, and fetch evidence source breakdown
   per learned rule. Minor enhancement: add `source` filter to rules page.
