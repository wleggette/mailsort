# JMAP Integration

Fastmail JMAP (RFC 8621) integration for email querying, folder management,
contact lookup, and email moves.

## Authentication

Fastmail uses Bearer token auth. Generate an API token at:
Settings → Privacy & Security → Manage API tokens

The token needs the scopes:
- `urn:ietf:params:jmap:core`
- `urn:ietf:params:jmap:mail`
- `urn:ietf:params:jmap:contacts` (for contact lookup — requires contacts synced to Fastmail)

## Session Discovery

```
GET https://api.fastmail.com/jmap/session
Authorization: Bearer {token}
```

Response provides:
- `accounts` → your account ID (e.g., `u12345678`)
- `apiUrl` → `https://api.fastmail.com/jmap/api/` (POST all method calls here)
- `primaryAccounts` → which account to use for mail

## Key JMAP Methods

All calls are POST to `apiUrl` with JSON body:

```json
{
  "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
  "methodCalls": [...]
}
```

### Mailbox/get — Discover folder tree

```json
["Mailbox/get", {
  "accountId": "u12345678",
  "properties": ["id", "name", "parentId", "role", "totalEmails", "unreadEmails"]
}, "m1"]
```

Returns all mailboxes. Build a tree from `parentId` relationships.
The inbox has `role: "inbox"`. Cache the mailbox ID → path mapping.

**`MailboxTree.build()`** constructs canonical folder paths by walking `parentId`
chains up to the inbox root (e.g. `INBOX/Affairs/Banks`). System folders with
roles like `trash`, `junk`, `drafts`, `sent` are excluded automatically.

**`exclude_folder_patterns`** (config) removes additional folders from the tree
using `fnmatch` glob matching against the full path. This is checked per-mailbox
*after* path construction, so excluding a parent folder does not affect its
children — e.g. excluding `INBOX/Affairs` still allows `INBOX/Affairs/Banks`:

```python
# Inside MailboxTree.build():
for mailbox in mailboxes:
    path = _build_path(mailbox, by_id)       # walks parentId chain
    if any(fnmatch.fnmatch(path, pat) for pat in exclude_patterns):
        continue                              # excluded — not in tree
    tree._id_to_path[mailbox.id] = path
    tree._path_to_id[path] = mailbox.id
```

Excluded folders are invisible to the rest of the system:
- Not in `tree.all_folder_paths()` → LLM never sees them as valid targets
- Not in folder descriptions → LLM prompt doesn't mention them
- Rules targeting them can't resolve a folder ID → `skip_reason="unknown_folder"`
- Bootstrap skips them when scanning for evidence

### Email/query — Find eligible inbox messages

```json
["Email/query", {
  "accountId": "u12345678",
  "filter": {
    "inMailbox": "INBOX_ID",
    "hasKeyword": "$seen",
    "notKeyword": "$flagged"
  },
  "sort": [{"property": "receivedAt", "isAscending": false}],
  "limit": 100
}, "q1"]
```

**Important:** JMAP's `Email/query` filter supports `hasKeyword` and `notKeyword`
for filtering by `$seen` (read) and `$flagged` status. This handles the
"only process read, unflagged emails" requirement at the query level.

The age filter (only emails older than N hours) is applied using the `before`
filter condition with a computed UTC datetime. Fastmail's JMAP implementation
supports `before` in `Email/query`, so this can be done server-side:

```python
cutoff = (datetime.now(timezone.utc) - timedelta(minutes=config.scheduler.min_age_minutes)).isoformat() + "Z"
filter["before"] = cutoff
```

This eliminates the need for client-side filtering on `receivedAt`.

### Email/get — Fetch metadata

```json
["Email/get", {
  "accountId": "u12345678",
  "#ids": {"resultOf": "q1", "name": "Email/query", "path": "/ids"},
  "properties": [
    "id", "threadId", "mailboxIds", "from", "to", "subject",
    "receivedAt", "keywords", "preview",
    "header:list-id:asText",
    "header:list-unsubscribe:asText"
  ]
}, "g1"]
```

Use result references to chain query → get in a single HTTP request.
The `preview` property gives a short text snippet without fetching the full body.
For richer classification, fetch `bodyValues` with `fetchTextBodyValues: true`.

### ContactCard/get — Fetch contacts for sender enrichment

```json
["ContactCard/get", {
  "accountId": "u12345678",
  "properties": ["uid", "name", "emails"]
}, "c1"]
```

Fastmail implements JMAP Contacts (`urn:ietf:params:jmap:contacts`). Each
`ContactCard` has a `name` map and an `emails` map keyed by arbitrary IDs.
Query all contacts once at startup (and refresh daily), then build an in-memory
lookup from email address → `{name, groups}`.

**Graceful degradation:** If the `urn:ietf:params:jmap:contacts` scope is not
present in the session capabilities (e.g., token was created without it, or
contacts have not been synced to Fastmail), the system logs a warning and
continues without contact enrichment. The `llm_move_known_contact` stricter
threshold will not apply — all LLM classifications use `llm_move` instead.
The contacts table remains empty until the scope becomes available.

```python
def refresh_contacts_cache(jmap_client):
    """Load contacts from Fastmail. No-op if contacts scope is unavailable."""
    if "urn:ietf:params:jmap:contacts" not in jmap_client.session_capabilities:
        logger.warning(
            "Contacts scope not available — contact enrichment disabled. "
            "Grant the contacts scope and sync Apple Contacts via CardDAV to enable."
        )
        return
    # ... fetch and cache ContactCard objects
```

**Prerequisite:** Sync Apple Contacts to Fastmail via CardDAV. In macOS:
System Settings → Internet Accounts → Add Account → CardDAV → Fastmail.
After that, Apple Contacts syncs bidirectionally with Fastmail and mailsort
reads them automatically — no separate credentials or client needed.

**Contact-based prompt enrichment:**
When the sender's email address matches a contact, the LLM prompt is enriched
with the contact's name so it can classify by relationship context rather than
treating the sender as an unknown address.

```
From: husband@gmail.com [known contact: "John Smith"]
Subject: Can you look at this?
```

vs.

```
From: husband@gmail.com
Subject: Can you look at this?
```

The `known_contacts` config key accepts optional manual overrides for contacts
you want to annotate beyond what's in your address book (e.g., adding a
`relationship` hint like "spouse" or "parent" that CardDAV doesn't carry).

### Email/set — Move to folder

`Email/set` replaces the full `mailboxIds` set for a message. To avoid stripping
unrelated mailbox memberships, mailsort must construct a new mailbox set from the
message's current `mailboxIds`: remove the inbox mailbox ID, add the target
folder ID, and preserve any other existing mailbox memberships.

```python
def build_updated_mailbox_ids(current_mailbox_ids: dict[str, bool],
                              inbox_id: str,
                              target_folder_id: str) -> dict[str, bool]:
    """Preserve existing mailbox memberships while removing inbox and adding target."""
    updated = dict(current_mailbox_ids)
    updated.pop(inbox_id, None)
    updated[target_folder_id] = True
    return updated
```

```json
["Email/set", {
  "accountId": "u12345678",
  "update": {
    "MSG_ID": {
      "mailboxIds": {"TARGET_FOLDER_ID": true, "OTHER_EXISTING_ID": true}
    }
  }
}, "s1"]
```

**Batch moves:** You can update multiple emails in a single `Email/set` call
by including multiple entries in the `update` object. Do this to minimize
API round trips.

## Rate Limiting & Best Practices

- Fastmail does not publish explicit rate limits for JMAP, but be respectful.
  Polling every 10-15 minutes is reasonable.
- Use result references to batch query + get into a single request.
- Batch Email/set updates (move multiple emails in one call).
- Cache the mailbox tree; only refresh it periodically (e.g., once per hour)
  or when a move fails with an unknown mailbox ID.
