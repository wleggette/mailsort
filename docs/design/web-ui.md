# Mailsort Web UI — Planning Document

## Goals

1. **Full visibility** into mailsort's internal state — audit logs, rules, contacts,
   folder descriptions, classification decisions, and threshold analysis
2. **Bookmarkable URLs** — every page and filtered view has a real URL that works
   with browser back/forward and can be shared or bookmarked
3. **Clean, minimal design** — modern aesthetic inspired by shadcn/ui: good typography,
   subtle grays, card-based layouts, readable data tables
4. **No separate build tooling** — no Node, npm, or webpack; deploys in the same
   Docker container as the scheduler
5. **Read-mostly** — primarily a monitoring/inspection tool; limited write operations
   (rule activation/deactivation, manual rule creation, trigger actions)

---

## Pages & Features

### 1. Dashboard (`/`)

The landing page. At-a-glance overview of system health.

- **Last run card** — status badge (completed/dry run/failed/abandoned/running),
  timing, trigger type, emails seen/moved/skipped/failed
- **Run history** — compact table of the last 20 runs with status, timing, counts.
  Dry-run rows (explicit or auto-downgraded) show a blue "dry run" badge.
- **Quick stats row** — total active rules, total contacts, folders tracked,
  emails processed (lifetime), rules auto-created this week
- **System health** — scheduler status, last contact refresh, last folder scan,
  DB size

### 2. Audit Log (`/audit`)

The core inspection tool. Every classification decision mailsort has made.

- **Filterable table** with columns: timestamp, email_id, from, subject,
  classification source, target folder, confidence, moved, skip reason
- **Unique mode** (on by default): deduplicates by `(email_id, classification_source,
  moved, skip_reason)` — repeated identical outcomes collapse into one row with a
  `×N` badge. Different outcomes for the same email (flagged → moved → corrected)
  each show as separate rows. Disabled when filtering by `run_id`.
- **Filter bar** (query params, all bookmarkable):
  - `?unique=1|0` (default 1)
  - `?source=rule|llm|thread|manual|correction`
  - `?moved=1|0`
  - `?folder=INBOX/Affairs/Banks`
  - `?sender=noreply@chase.com`
  - `?days=7|30|90`
  - `?run_id=...`
  - `?rule_id=N` (numeric, filter by rule that matched)
- **Source badges** — color-coded by `classification_source`:
  - `rule` → blue, `llm` → purple, `thread` → teal, `manual` → gray,
    `correction` → orange
  - Rule and correction badges with a `rule_id` link to `/rules/{id}`
    (shows "correction #42" or "rule #42")
- **Click a row** → detail view at `/audit/{id}` showing full email metadata,
  classification reasoning, rule ID if applicable
- **Export CSV** button
- **Pagination** — 50 rows per page, page number in query params

### 3. Audit Detail (`/audit/{id}`)

Single audit log entry with full context.

- Email metadata: from, to, subject, date, list-id, preview
- Classification: source, confidence, reasoning, rule ID (linked)
- Decision: moved/skipped, skip reason
- Thread context: thread ID, sibling audit entries
- Run context: link to run, trigger type

### 4. Rules (`/rules`)

All classification rules with management capabilities.

- **Table** with columns: type, value, folder, confidence, source, hits, created, last relevant, active
- **Filter tabs**: All / Active / Inactive / Suggested
- **Sort** by any column (click header)
- **Search** by value or folder
- **Filter by creation date** — `?created_days=30` shows rules created in last N days
- **Actions per rule**:
  - Toggle active/inactive
  - Edit confidence
  - Delete (soft — deactivate)
- **Create rule form** — type, value, folder, confidence
- **Coherence indicator** — show current coherence for each rule's condition

### 5. Rules Detail (`/rules/{id}`)

Single rule with full history.

- **Details card** — Confidence, Active (badge), Rule #, Source, Created,
  Updated, Last Relevant. Confidence is the final computed value. Last Relevant
  shows when the most recent matching email was sorted to the target folder.
- **Performance card** — broken into **30 Days** and **All Time** columns
  (30 Days first, since it drives the formula):

  | Metric | 30 Days | All Time | Notes |
  |--------|---------|----------|-------|
  | **Coherence** | % (windowed) | % (all-time) | Windowed value is what the formula uses; all-time gives trend |
  | **Evidence** | N / M | N / M | Emails to target / total matching emails |
  | **Corrections** | N | N | Corrections against this rule |
  | **Confirming** | N | N | Manual sorts confirming the rule (excludes bootstrap runs) |
  | **Net Corrections** | N | N | max(0, corrections − confirming) |
  | **Hit Count** | — | Total | Display only (not used in formula) |

  Color coding for coherence: green ≥80%, amber ≥50%, red <50%.
  Color coding for net corrections: red if >0, gray otherwise.
  Color coding for confirming: green if >0, gray otherwise.

- **Manual rule warning** — if `source='manual'` and windowed coherence is below
  `auto_rule_domain_coherence` (default 0.80), show an amber warning badge:
  "Coherence below threshold — consider reviewing this rule."
- **Evidence Emails** — all emails matching the rule's condition across all
  folders, deduplicated by `email_id` (latest row per email). Folder column
  color-coded (green = target, amber = other). Includes `classification_source`
  badge. Subject links to `/audit/{id}`. "View in audit log →" links to
  `/audit?rule_id=N&days=365`.
- **Recent Matches** — emails classified by this rule, deduplicated by `email_id`.
  Subject links to `/audit/{id}`.
- **Emails Matched** metric — `COUNT(DISTINCT email_id)` for this rule, replacing
  the inflated `hit_count` which counted per-cycle, not per-email.

### 6. Threshold Analysis (`/analyze`)

Interactive version of `mailsort analyze`.

- **Date range picker** (default 30 days)
- **Summary cards** — Emails Classified, Moved, Skipped, User Corrections
- **Classification sources** — bar chart with counts and percentages.
  Bar colors: blue (rule), purple (llm), teal (thread), orange (correction).
  Excludes bootstrap runs, dry runs, and `manual` rows (user sorts).
  Deduplicated by `email_id` — each email counts once with its final outcome.
- **LLM confidence histogram** — bucketed distribution with current threshold marked
- **Rule confidence distribution** — bucketed by confidence range, linked to
  `/rules` with confidence filters
- **Classification Analysis section** — 5 action-oriented cards:
  - **LLM Accuracy Summary** — tree structure (LLM classified → moved/skipped →
    corrected/later-sorted → agreed/disagreed) + 3 precision metrics (System
    Effectiveness, Move Precision, Threshold Precision) with adaptive color
    thresholds (green ≥80%, amber 50–79%, red <50%)
  - **Folder Description Gaps** — wrong-folder skipped-then-sorted emails grouped
    by destination folder, with LLM guess + confidence per row, "Review description"
    links to `/folders?highlight=`, per-row `→` to `/audit/{id}`, collapsible rows
  - **Known Contact Sorting** — per-contact cards for senders with ≥N
    `below_threshold_known_contact` skips (configurable via
    `classification.min_known_contact_skips`). Shows sorting mechanism counts
    (thread/rules/LLM/blocked), folder coherence bars, conditional coherence note,
    threshold-blocked email table
  - **Learning Effectiveness** — total auto rules, emails sorted by rules, rules
    created in selected period (links to `/rules?filter=all&created_days=N`),
    recently created rules table with hit counts
  - **Eligibility-Gated Emails** — total/flagged/unread/too_new breakdown
- **Rule corrections table** — emails where a rule moved to folder A and the user
  relocated to folder B
- **Recommendations** — threshold adjustment suggestions

### 7. Contacts (`/contacts`)

Contacts synced from Fastmail.

- **Table**: email address, display name, relationship, fastmail UID, last refreshed
- **Count** and last refresh timestamp in header
- **Search** by name or email
- **Config overrides** highlighted (relationship column)
- **Refresh now** button (triggers contact refresh via API)

### 8. Folders (`/folders`)

Folder structure and descriptions with regeneration.

- **Tree view** or indented table showing folder hierarchy
- Columns: folder path, description, source (auto/manual), email count (from audit_log)
- **Highlight support** — `?highlight=FolderA,FolderB` scrolls to and highlights
  specified folders (yellow background). Used by analysis page cross-links.
- **Excluded folders** shown grayed out with the matching pattern
- **Per-folder regeneration** — "Regenerate" link on each row (hidden for manual
  overrides, stale, or excluded folders). POSTs to `/folders/regenerate`, fetches
  fresh email samples via JMAP, calls the LLM, and replaces the description.
- **Bulk regeneration** — "Regenerate All" button in the page header with JS
  confirmation dialog. POSTs to `/folders/regenerate-all`.
- **Flash messages** — success/error feedback after regeneration via query param.

### 9. Settings (`/settings`)

Read-only view of current configuration.

- **Fastmail** — session URL, account ID, permissions (read-only/read-write),
  capabilities, contacts scope available
- **Scheduler** — interval, min age, batch size, health check port,
  contacts refresh interval
- **Classification** — all thresholds, auto-rule thresholds, coherence min,
  LLM model, privacy settings
- **Skip senders** list
- **Exclude folder patterns** list
- **Known contact overrides** list
- **Sessions** — active sessions table (only visible when auth is enabled):
  - Each row: browser/device (from User-Agent), IP address, created, expires
  - Current session marked with "current" badge
  - "Revoke" button per row (grayed out on current session)
  - "Revoke all other sessions" button

### 10. Authentication

Optional Google SSO gate for the web UI. Disabled by default (no-op when
`auth.google_client_id` is not configured).

- **Login page** (`/auth/login`) — minimal page with "Sign in with Google"
  button. Shown when a user attempts to access a protected route without a
  valid session.
- **OAuth callback** (`/auth/callback`) — exchanges authorization code for
  tokens, validates email against allowlist, creates server-side session.
- **Logout** (`/auth/logout`) — POST endpoint, deletes session row, clears
  cookie, redirects to `/auth/login`.
- **Auth middleware** — reads session cookie, validates against `sessions`
  table. Redirects to login on failure. Excluded paths: `/auth/*`, `/static/*`.
  Complete no-op when auth is disabled.
- **Session storage** — server-side `sessions` table in SQLite. Enables
  immediate revocation, visibility into active sessions, and "log out
  everywhere" functionality.
- **Lazy session cleanup** — on 1-in-100 requests, delete expired session
  rows. Works in both `mailsort start` and `mailsort web` modes.

---

## Architecture

### Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Web framework** | FastAPI | Already in the Python ecosystem, async, auto-generates OpenAPI docs |
| **Templating** | Jinja2 | Server-rendered HTML, proper URL routing, no client-side router |
| **Styling** | Tailwind CSS (CDN) | Clean minimal aesthetic, no build step |
| **Interactivity** | htmx | Fragment swaps for filtering/sorting without full page reloads |
| **Icons** | Lucide (CDN) | Clean line icons, works well with Tailwind |
| **ASGI server** | Uvicorn | Standard FastAPI deployment |
| **Database** | Existing SQLite | Same DB file, read by the web server |

### New Dependencies

```
fastapi
uvicorn[standard]
jinja2
authlib           # OAuth 2.0 / OpenID Connect (Google SSO)
```

### URL Routing

All pages are server-rendered with proper HTTP routes. Filtered views use query
parameters so they're bookmarkable. htmx handles in-page updates (filter
changes, pagination, inline edits) by swapping HTML fragments from dedicated
API endpoints.

```
GET /                     → Dashboard
GET /audit                → Audit log (filterable via query params)
GET /audit/{id}           → Audit detail
GET /rules                → Rules list
GET /rules/{id}           → Rule detail
GET /analyze              → Threshold analysis
GET /contacts             → Contacts list
GET /folders              → Folder descriptions
GET /settings             → Config view

# Authentication (excluded from auth middleware)
GET  /auth/login          → Redirect to Google OAuth
GET  /auth/callback       → OAuth callback, create session
POST /auth/logout         → Delete session, clear cookie

# htmx fragment endpoints (partial HTML responses)
GET /api/audit/table      → Audit table rows (for htmx filter/paginate)
GET /api/rules/table      → Rules table rows
GET /api/contacts/table   → Contacts table rows

# Action endpoints
POST /api/rules/{id}/toggle    → Activate/deactivate rule
POST /api/rules/{id}/edit      → Update rule confidence/folder
POST /api/rules/create         → Create new rule
POST /api/contacts/refresh     → Trigger contact refresh
```

### File Structure

```
src/mailsort/
  web/
    __init__.py
    app.py              ← FastAPI app factory, route registration, auth middleware
    routes/
      __init__.py
      auth.py           ← GET /auth/login, /auth/callback, POST /auth/logout
      dashboard.py      ← GET /
      audit.py          ← GET /audit, /audit/{id}
      rules.py          ← GET /rules, /rules/{id}, POST actions
      analyze.py        ← GET /analyze
      contacts.py       ← GET /contacts, POST refresh
      folders.py        ← GET /folders
      settings.py       ← GET /settings
    templates/
      base.html         ← Layout: nav, head, Tailwind/htmx CDN links, avatar/logout
      login.html        ← "Sign in with Google" page
      dashboard.html
      audit/
        list.html
        detail.html
        _table.html     ← Partial for htmx swaps
      rules/
        list.html
        detail.html
        _table.html
        _create_form.html
      analyze.html
      contacts/
        list.html
        _table.html
      folders.html
      settings.html
      components/
        nav.html        ← Sidebar/top navigation + avatar
        pagination.html ← Reusable pagination
        filters.html    ← Reusable filter bar
        badge.html      ← Status badges
    static/
      style.css         ← Minimal custom CSS (if any beyond Tailwind)
```

### Integration with Existing System

- When running `mailsort start`, the web server is **embedded in the same
  process** as the scheduler — started in a background daemon thread using
  Uvicorn, the same pattern as the health check server.
- `mailsort web` is still available as a **standalone command** for development
  (runs the web UI without the scheduler).
- Reads the **same SQLite database** — no API layer between web and data.
- Write operations (rule toggle, contact refresh) use the existing
  `RuleEngine` and `refresh_contacts` functions.
- The scheduler continues running independently; the web UI is read-mostly.
- Configurable via `scheduler.web_port` (default 8080, set to 0 to disable).

### CLI Commands

```
mailsort start              # Scheduler + health check + web UI (all-in-one)
mailsort web [--port 8080]  # Standalone web UI (for development)
```

In Docker, `mailsort start` provides everything in one container. The web UI
is accessible on port 8080 (configurable), the health check on port 8025.

---

## Design Language

- **Colors**: Neutral grays for backgrounds/borders, blue for primary actions,
  green for success/moved, amber for warnings/skipped, red for failures/errors
- **Typography**: System font stack (Inter-like), monospace for email addresses,
  rule values, and IDs
- **Layout**: Max-width container (~1200px), left sidebar nav on desktop,
  top nav on mobile
- **Cards**: White background, subtle border, small border-radius, light shadow
- **Tables**: Alternating row backgrounds, sticky headers, compact row height
- **Badges**: Rounded pills for status (completed/failed/abandoned) and
  classification source (rule/llm/thread/manual)
- **Empty states**: Friendly message + suggested action when no data

### Table Header Conventions

All data tables follow these conventions for consistent column alignment:

- **`whitespace-nowrap`** on all `<th>` elements — prevents header text from
  wrapping and ensures headers stay on a single line.
- **Matching horizontal padding** — `<th>` and `<td>` in the same column must
  use the same `px-*` class (typically `px-4`). Never override cell padding
  with inline `style="padding-left"` as this breaks header-to-data alignment.
- **Depth indentation via inner element** — when rows need depth-based
  indentation (e.g., folder tree), apply `margin-left` on an inner `<span>`
  rather than overriding the `<td>` padding. This keeps the cell padding
  consistent with the `<th>` above it.
  ```html
  <!-- Correct: inner span for indentation -->
  <td class="px-4 py-2 ...">
    <span style="margin-left: {{ depth * 16 }}px">{{ text }}</span>
  </td>

  <!-- Wrong: overriding cell padding breaks column alignment -->
  <td class="px-4 py-2 ..." style="padding-left: {{ 16 + depth * 16 }}px">
  ```
- **`align-top`** on all `<td>` elements in tables with multi-line content
  (e.g., descriptions). This prevents cell content from centering vertically
  when adjacent cells wrap to multiple lines.
- **Text alignment** — numeric columns (counts, emails) must use `text-right`
  on **both** the `<th>` header and every `<td>` in that column. Mismatched
  alignment between header and data is a common bug. All other columns default
  to `text-left`.
- **Date/time columns** — use `whitespace-nowrap` on the `<td>` and
  `min-w-[120px]` on the `<th>` to prevent dates from wrapping mid-value.

### Filter Bar Patterns

All filterable pages (audit log, rules) follow these conventions:

- **Consistent input height**: All selects, text inputs, and buttons use
  `h-[34px]` for visual alignment.
- **Bookmarkable filters**: All filter values are query params in the URL
  (e.g., `/rules?filter=all&search=chase&conf_min=0.85`).
- **Auto-search on typing**: Text inputs submit the form after a 500ms
  debounce (`setTimeout`). No need to press Filter.
- **Select auto-submit**: Dropdowns submit immediately on `change` event.
- **Focus restoration**: Before submit, the focused input's `name` is saved
  to `sessionStorage`. After page reload, focus is restored to that input
  with cursor at end of text (`setSelectionRange`).
- **Filter bar layout**: Use `flex gap-2 items-end flex-wrap` on the form.
  Give each input a fixed or min width (`w-32`, `w-40`, `w-20`) so they
  don't stretch unevenly. Use `flex-1 min-w-[120px]` for the primary
  search field so it fills remaining space.
- **Filter + Clear buttons**: Filter and ✕ sit in a `flex gap-1` container
  with no fixed width (shrink-wraps to content). Both buttons are **fixed
  width** (no `flex-1`). Filter: `px-3 h-[34px] bg-blue-600 text-white
  hover:bg-blue-700`. ✕: `px-2 h-[34px]` bordered gray. ✕ uses Tailwind
  `invisible` class when no filters are active (preserves layout). ✕ links
  to the page with filters cleared but tab state preserved. All filter
  buttons across the app use **blue** (`bg-blue-600`).
- **Results count**: When filters are active, show "Showing N rule(s)
  matching filters" above the table.

```javascript
// Standard filter bar script pattern
(function() {
  const form = document.querySelector('form[action="/PAGE"]');
  let timer;
  function debounceSubmit(inputName) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      sessionStorage.setItem('PAGE_focus', inputName);
      form.submit();
    }, 500);
  }
  form.querySelectorAll('input[type="text"]').forEach(el => {
    el.addEventListener('input', () => debounceSubmit(el.name));
  });
  form.querySelectorAll('select').forEach(el => {
    el.addEventListener('change', () => {
      sessionStorage.setItem('PAGE_focus', el.name);
      form.submit();
    });
  });
  const savedFocus = sessionStorage.getItem('PAGE_focus');
  if (savedFocus) {
    sessionStorage.removeItem('PAGE_focus');
    const el = form.querySelector('[name="' + savedFocus + '"]');
    if (el) {
      el.focus();
      if (el.type === 'text') {
        const len = el.value.length;
        el.setSelectionRange(len, len);
      }
    }
  }
})();
```

---

## Implementation Checklist

### Phase 1: Skeleton ✅
- [x] Add `fastapi`, `uvicorn`, `jinja2` to `pyproject.toml`
- [x] Create `web/app.py` — FastAPI app factory with DB dependency
- [x] Create `web/templates/base.html` — layout with Tailwind CDN, htmx CDN, nav
- [x] Create `web/templates/components/nav.html` — sidebar navigation
- [x] Add `mailsort web` CLI command (`main.py`)
- [x] Verify skeleton serves a page at `http://localhost:8080/`

### Phase 2: Dashboard (`/`) ✅
- [x] `web/routes/dashboard.py` — query runs, stats, system health
- [x] `web/templates/dashboard.html` — last run card, run history table, quick stats
- [x] Status badges component (completed/failed/abandoned)

### Phase 3: Rules (`/rules`, `/rules/{id}`) ✅
- [x] `web/routes/rules.py` — list, detail, toggle, create endpoints
- [x] `web/templates/rules/list.html` — filterable table with active/inactive/suggested tabs
- [x] `web/templates/rules/detail.html` — single rule with coherence stats + audit history
- [x] Evidence Emails section — all emails matching the rule's condition across all
      folders, with folder column color-coded (green = target, amber = other)
- [x] Rule create form (inline, toggled via button)
- [x] Toggle active/inactive via POST + redirect

### Phase 4: Audit Log (`/audit`, `/audit/{id}`) ✅
- [x] `web/routes/audit.py` — list with filters, detail view, pagination
- [x] `web/templates/audit/list.html` — filterable/paginated table, sortable columns
- [x] `web/templates/audit/detail.html` — full email classification detail with
      linked rule, LLM reasoning, thread context
- [x] Filter bar: source, moved, folder, sender, days (all bookmarkable via query params)
- [x] Pagination with bookmarkable page numbers
- [ ] CSV export (deferred)

### Phase 5: Threshold Analysis (`/analyze`) ✅
- [x] `web/routes/analyze.py` — query analysis data
- [x] `web/templates/analyze.html` — source breakdown, confidence histogram,
      skipped-then-sorted table, rule corrections, recommendations
- [x] Date range picker (7d / 30d / 90d query param)

### Phase 6: Contacts (`/contacts`) ✅
- [x] `web/routes/contacts.py` — list with search
- [x] `web/templates/contacts/list.html` — searchable sortable table
- [x] Relationship badges for config overrides

### Phase 7: Folders (`/folders`) ✅
- [x] `web/routes/folders.py` — folder list with descriptions and email counts
- [x] `web/templates/folders.html` — indented table with depth-based padding
- [x] Excluded folders shown grayed out with "excluded" badge
- [x] Per-folder "Regenerate" link (POST `/folders/regenerate`)
- [x] Bulk "Regenerate All" button with confirmation (POST `/folders/regenerate-all`)
- [x] Flash-style success/error messages after regeneration

### Phase 8: Settings (`/settings`) ✅
- [x] `web/routes/settings.py` — read-only config view
- [x] `web/templates/settings.html` — organized cards: Fastmail, Scheduler,
      Thresholds, Auto-Rule, LLM, Filters & Exclusions, Logging

### Phase 9: Authentication (Google SSO)
- [ ] Add `authlib` to `pyproject.toml` dependencies
- [ ] Add `AuthConfig` model to `config.py` (google_client_id, allowed_emails,
      session_lifetime_hours, redirect_uri) + GOOGLE_CLIENT_SECRET env loading
- [ ] Migration 13: `sessions` table (id, email, name, picture_url, user_agent,
      ip_address, created_at, expires_at)
- [ ] `web/routes/auth.py` — login, callback, logout routes (Authlib)
- [ ] Auth middleware in `web/app.py` — session validation, redirect to login,
      no-op when disabled, excluded paths (/auth/*, /static/*)
- [ ] `web/templates/login.html` — "Sign in with Google" button
- [ ] `web/templates/base.html` — avatar + name + logout form when session exists
- [ ] `web/templates/components/nav.html` — avatar in sidebar bottom
- [ ] `web/templates/settings.html` — Sessions panel (active sessions, revoke)
- [ ] Lazy session cleanup (1-in-100 requests: delete expired rows)
- [ ] `config.yaml.example` — commented `auth` block
- [ ] Tests: middleware, session CRUD, allowlist, config parsing, OAuth callback
      (mocked Authlib), logout, template rendering
