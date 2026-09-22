# Apple Reminders MCP

A Claude Desktop extension that gives **Claude full access to Apple Reminders** on macOS via Apple's first-class **EventKit** framework. Read, create, update, complete, and delete reminders — including subtasks, recurrence rules, alarms, location-based geofences, real tags, and the Mail / Messages chip that links a reminder to its source email or chat.

Packaged as an [MCPB desktop extension](https://support.claude.com/en/articles/12922929-building-desktop-extensions-with-mcpb) with the Reminders.app icon and one-click install.

---

## What it does

| Tool | Description |
|------|-------------|
| `list_reminder_lists` | Every reminder list with id, title, color, source/account, and write-access flag |
| `create_reminder_list` | Create a new list (optional color and source) |
| `update_reminder_list` | Rename or recolor an existing list |
| `delete_reminder_list` | Delete a list and all its reminders (destructive) |
| `get_stats` | Aggregate counts: list count, total, incomplete, completed, overdue, due today |
| `list_reminders` | Reminders in one or more lists with filters: completed, priority, due date, has-subtasks, text, tags |
| `search_reminders` | Free-text search across title and notes, with the same filters |
| `get_reminder` | Full detail — notes, URL, recurrence, alarms, location, subtask ids, real tags, linked content, timestamps |
| `get_reminder_link` | `x-apple-reminderkit://` URL to open the reminder in Reminders.app |
| `list_subtasks` | Immediate children of a parent reminder |
| `list_tags` | Every real Apple Reminders tag, with how many reminders carry it |
| `add_reminder_tags` | Add real tags to a reminder (idempotent; creates new tags) |
| `remove_reminder_tags` | Remove real tags from a reminder |
| `set_reminder_tags` | Replace a reminder's tags outright; empty list clears them |
| `set_reminder_link` | Attach linked content — the Mail / Messages chip — from a `message://` URL, a Messages chat guid, or an `https://` URL |
| `clear_reminder_link` | Remove a reminder's linked content |
| `create_reminder` | Create with full property set: notes, URL, due/start dates, priority, recurrence, alarms, location, parent (for subtasks), real tags, linked content |
| `update_reminder` | Update any subset of properties; `clear_*` flags explicitly remove fields |
| `complete_reminder` | Mark complete or uncomplete (or toggle if `completed` is null) |
| `delete_reminder` | Delete a reminder; cascades to subtasks by default |

## How it works

Communication with Reminders happens through **EventKit** (`EKEventStore`, `EKReminder`, `EKCalendar`, `EKRecurrenceRule`, `EKAlarm`, `EKStructuredLocation`) via PyObjC.

This differs from the [companion Apple Mail connector](https://github.com/falconbradley/claude-connector-apple-mail), which uses JXA via `osascript`. Reminders.app's AppleScript dictionary is gap-filled — it has no first-class API for subtasks, tags, locations, or recurrence read-back. EventKit exposes the full data model and is dramatically faster (~50–200ms for 1000 reminders, vs 5–30s for AppleScript).

The async EventKit fetch API (`fetchRemindersMatchingPredicate:completion:`) is bridged to a synchronous Python interface using a `threading.Event`, so callers see plain return values.

---

## Tags

Reminders' real tags — the chips on a reminder, and the tag filters in the
Reminders.app sidebar — are **not exposed by any supported API**:

- `EKReminder` / `EKCalendarItem` have no tag property, and a symbol scan of
  EventKit turns up no private one either.
- The Reminders AppleScript dictionary has no tag property.

So this connector reads them straight out of the Reminders app's own Core
Data store, the same way the companion Apple Notes connector reads note
tags. The store lives at

```
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/
```

with one SQLite file per account. It is opened strictly read-only
(`mode=ro` plus `PRAGMA query_only`) and never written; Reminders.app stays
the only writer and the only sync engine.

**This needs Full Disk Access** for the host process (Claude Desktop), since
Group Containers are TCC-protected. Everything else in the connector works
without it. When the store cannot be read, `get_reminder` returns
`tags: null` with a `tags_unavailable_reason` explaining why — deliberately
not an empty list, which would read as "this reminder has no tags".

### `tags` vs `text_hashtags`

These are different things and are reported separately:

| Field | What it is |
|---|---|
| `tags` | Real Apple tags, read from the Reminders store. `null` if unreadable. |
| `text_hashtags` | `#tokens` scraped out of the title and notes. Just text. |

Writing `#foo` into a reminder's title **does not tag it**. Earlier versions
of this connector reported that scrape as `tags`, which was wrong in both
directions: real tags were invisible, and arbitrary `#tokens` looked like
tags that did not exist.

### Writing tags

Supported, via `add_reminder_tags`, `remove_reminder_tags`,
`set_reminder_tags`, and a `tags` argument on `create_reminder`.

**This uses private API, deliberately.** There is no alternative. EventKit
and AppleScript have no tag concept; Shortcuts/App Intents expose no tag
parameter; and hand-writing the Core Data store would mean maintaining
Reminders' CloudKit sync bookkeeping ourselves, which would corrupt it.

So writes go through `ReminderKit`, the framework Reminders.app itself
uses, driven exactly as the app drives it:

```
REMStore.fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:
REMSaveRequest(store).updateReminder:              -> REMReminderChangeItem
  .hashtagContext.addHashtagWithType:name:(0, "…") -> add
  .hashtagContext.removeHashtag:                   -> remove
REMSaveRequest.saveSynchronouslyWithError:
```

Apple performs the write, so sync, change tracking and validation stay
Apple's job. Reminder ids need no translation: ReminderKit's
`DACalendarItemUniqueIdentifier` is the same string EventKit reports as
`calendarItemIdentifier`.

**The trade-off:** Apple owes this interface nothing, and a macOS update
could rename or drop a selector. The writer therefore declares every class
and selector it uses and verifies them *before* touching anything — if the
interface has moved it refuses with a message naming exactly what went
missing, rather than half-applying a change. Reading tags is unaffected
either way; it needs no private API.

Writing needs **Reminders permission** (ReminderKit talks to `remindd`);
reading needs **Full Disk Access**. These are separate capabilities and
each reports its own reason when unavailable.

Behaviour worth knowing:

- Adding a tag already on the reminder is a no-op, including across case
  (`Buy` and `buy` are the same tag to Reminders). No duplicates.
- Removing a tag that is not present is a no-op, not an error.
- `set_reminder_tags` diffs rather than clear-and-re-add, so tags that
  should stay are never briefly removed.
- Removing a tag from its last reminder **does** delete the tag itself.
  Reminders garbage-collects a label once nothing references it — verified
  by watching the `ZREMCDHASHTAGLABEL` rows disappear after the last
  reminder carrying them was untagged. So `list_tags` will in practice
  never report a tag with `reminder_count: 0`, even though it is written
  to handle one.
- A no-change call does not save, so it will not bump the reminder's
  modification date or wake CloudKit.

---

## Linked content

Make a reminder with Siri ("remind me about this email") or from the
share sheet, and Reminders.app shows a chip under the title — the Mail
icon with the message's subject, or the Messages icon with the chat's
name. Tap it and the source opens. That chip is the reminder's **linked
content**, and this connector can read it and write it.

It is **not** the reminder's `url` field. EventKit's `URL` lands in the
store's `ZICSURL` column, which Reminders.app never draws — a `message://`
URL there opens Mail fine from a script, but nobody can see or click it.
`url` is left alone; linked content is a separate thing.

### What it is on disk

A `REMUserActivity`, archived with `NSKeyedArchiver` into
`ZREMCDREMINDER.ZUSERACTIVITY`. Two shapes, both decoded from real rows:

| Chip | Activity `type` | Payload |
|---|---|---|
| **Mail** | 1 (universal link) | the bare Message-ID URL, `message:%3C<id>%3E` — note `message:` with no `//` |
| **Messages** | 2 (user activity) | a nested archive of an `NSUserActivity`: `activityType` = `com.apple.Messages`, `title` = chat name, `targetContentIdentifier` = `messages://open?groupid=chat…` |

Even Apple's own Messages chip is **chat-level**. There is no per-message
deep link into Messages.app, so a reminder about an iMessage opens the
conversation, not the message.

### Reading

`get_reminder` reports `link` with `kind` (`mail`, `messages`, `web`, or
`other` for an activity from some other app, such as Notes), `url`, and
for Messages the chat `title`. Same convention as `tags`: `link: null`
with `link_unavailable_reason` set means the store could not be read
(Full Disk Access again); `null` with no reason means there is simply no
link. The reader is read-only and needs no private API.

### Writing

`set_reminder_link`, `clear_reminder_link`, and a `link` argument on
`create_reminder` (with `link_title` for Messages). Accepted values:

| You pass | It becomes |
|---|---|
| `message://<Message-ID>` or `message:<Message-ID>`, brackets literal or percent-encoded — the Mail connector's `mail_link` works as-is | the **Mail** chip. Canonicalised to Apple's own spelling, `message:%3C<id>%3E`; both spellings open the message in Mail.app |
| a chat guid as the Messages connector reports it: `any;-;+15551234567` (1:1) or `any;+;chat123…` (group); also `iMessage;…` / `SMS;…`, a bare `chat123…`, a bare `+1555…`, or a ready `messages://open?…` URL | the **Messages** chip. Groups → `messages://open?groupid=chat…` (the form Messages itself writes); 1:1 → `messages://open?addresses=<handle>`. Verified against Messages.app: `groupid=chat…` opens the group, `addresses=<handle>` (and `groupid=<handle>`) opens the 1:1 chat |
| `http://` or `https://` | a universal-link chip |

A numeric label — `title` here, `link_title` on `create_reminder` — is
accepted as a string, so an SMS shortcode chat such as `any;-;42878` can
be labelled `42878` without quoting it (quotes would end up in the chip).

A malformed `link` on `create_reminder` is rejected before anything is
created. A link that parses but fails to *write* leaves the reminder in
place and reports why in `link_unavailable_reason`, exactly like `tags`.
Setting the link a reminder already has is a no-op and does not save.

**This uses private API, deliberately** — the same `ReminderKit` route as
[writing tags](#writing-tags), for the same reason: nothing supported can
set it. Only Siri and the share sheet write linked content, and they go
through ReminderKit.

```
REMStore.fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:
REMUserActivity initWithUniversalLink:        (Mail, web)
REMUserActivity initWithUserActivity:         (Messages, wrapping an NSUserActivity)
REMSaveRequest(store).updateReminder:         -> REMReminderChangeItem
  .setUserActivity:                           (forwarded to its REMReminderStorage)
REMSaveRequest.saveSynchronouslyWithError:
```

Every class and selector is declared and verified before anything is
touched; if a macOS update moves the interface the tools refuse with a
message naming what went missing. Writing needs **Reminders permission**;
reading needs **Full Disk Access**. The archive the writer produces was
checked byte-for-shape against Apple's own rows, and the store reader
decodes it identically. The full round trip was verified live on
2026-09-22 through the installed connector: both chips render in
Reminders.app exactly like Siri's, clicking the Mail chip opens the
email in Mail.app, and clicking the Messages chip opens the chat.

---

## Requirements

- macOS 14 Sonoma or later (`requestFullAccessToReminders` was added in 14)
- Python 3.11+
- Claude Desktop with extension support
- Reminders permission granted to Claude Desktop (see below)
- Full Disk Access for Claude Desktop — **only** needed to read real
  tags and linked content; every other tool works without it (see
  [Tags](#tags) and [Linked content](#linked-content))

---

## Installation

### Option 1: Desktop Extension (recommended)

Download the latest `.mcpb` from [Releases](../../releases), then **double-click** to install.

Or build from source:

```bash
git clone https://github.com/falconbradley/claude-connector-apple-reminders.git
cd claude-connector-apple-reminders
./build.sh
```

Then double-click `dist/apple-reminders.mcpb` (or drag it into Claude Desktop).

The extension appears in **Settings > Extensions** with the Reminders icon.

### Option 2: Manual MCP config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-reminders": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/apple-reminders-mcp", "apple-reminders-mcp"]
    }
  }
}
```

### Permissions

The first time Claude calls a Reminders tool, macOS will prompt you to grant **Reminders** access. Click **OK**.

If the prompt doesn't appear (which can happen with unsigned interpreters launched as child processes):

1. Open **System Settings → Privacy & Security → Reminders**
2. Add Claude Desktop (or whichever process is running `uv`) and enable it
3. Quit and relaunch Claude Desktop

To verify access status, run:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT client, auth_value FROM access WHERE service='kTCCServiceReminders'"
```

(`auth_value` of `2` = full access, `0` = denied.)

---

## Usage examples

Once installed, just ask Claude naturally:

- *"What's on my reminders for today?"*
- *"Show me everything overdue across all lists."*
- *"Add a reminder to call the dentist next Tuesday at 9am with high priority."*
- *"Create a Groceries list and add milk, bread, and eggs as separate reminders."*
- *"Add a subtask to the 'Pack for trip' reminder for charging the laptop."*
- *"Remind me to take out the trash every Sunday at 8am."*
- *"Mark my 'Pay rent' reminder complete."*
- *"What's tagged #urgent that's still incomplete?"*
- *"Make a reminder to reply to that email, linked to it so I can open it from Reminders."*
- *"Add a reminder to call mom when I leave the office (geofence at 37.7749, -122.4194)."*

---

## Building from source

```bash
# Install mcpb CLI (one time)
npm install -g @anthropic-ai/mcpb

# Install uv (one time, if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Build the extension
./build.sh

# Or manually:
mcpb validate manifest.json
mcpb pack . dist/apple-reminders.mcpb
```

### Project layout

```
apple-reminders-mcp/
├── manifest.json                    # MCPB desktop extension manifest
├── icon.png                         # Reminders.app icon (512x512)
├── icons/                           # Multi-size icons
│   ├── icon-128.png
│   ├── icon-256.png
│   └── icon-512.png
├── pyproject.toml                   # Python package + dependencies
├── build.sh                         # Validate + pack build script
├── tests/
│   └── test_e2e.py                  # End-to-end tests
└── src/
    └── apple_reminders_mcp/
        ├── __init__.py
        ├── server.py                # MCP tools (MCPServer)
        ├── reminders.py             # EventKit-backed RemindersStore
        ├── tagstore.py              # Read-only reader for real tags
        ├── tagwriter.py             # Tag writes via private ReminderKit
        ├── linkstore.py             # Read-only reader + decoder for linked content
        ├── linkwriter.py            # Linked-content writes via private ReminderKit
        ├── permissions.py           # TCC grant helpers
        └── models.py                # Pydantic data models
```

### Tests

```bash
# Static tests — model shapes, validation, helpers, the tag reader against
# a synthetic fixture store, link parsing, and the linked-content decoder
# against archives shaped like Apple's. No permissions needed.
uv run python tests/test_e2e.py --skip-live

# Full suite (requires Reminders permission, and Full Disk Access for the
# tag and linked-content tests)
uv run python tests/test_e2e.py
```

Three groups:

- **A — static.** Always runs. Includes the tag store exercised against a
  generated fixture that reproduces the awkward parts of the real schema:
  a per-store entity id, the shared wide object table, soft-deleted rows.
  Also builds the linked-content activities the writer would save and
  checks the store reader decodes them to the same thing.
- **C — live local store.** Reads the real Reminders store; needs Full Disk
  Access but *not* Reminders permission. Skipped with a reason otherwise.
  Includes decoding every Mail / Messages chip already in the store.
- **B — live EventKit.** Operates against a dedicated `__claude_mcp_test__`
  list, created at setup and torn down at the end. Needs Reminders
  permission, which a plain shell does not have — run these through the
  installed connector.

---

## Performance notes

EventKit is fast: a fetch over all reminders in a 1000-item store typically returns in 50–200ms. The first call after process start incurs a one-time bootstrap (~50–500ms) while EventKit prepares the store.

| Operation | Approx. time |
|-----------|--------------|
| Initial store bootstrap | 50–500 ms |
| `list_reminder_lists` | < 50 ms |
| `list_reminders` (1000 items, single list) | 50–200 ms |
| `search_reminders` (full store) | 200–500 ms |
| `create_reminder` / `update_reminder` | 30–100 ms |
| `delete_reminder` | 30–100 ms |

For comparison, the same operations via AppleScript would take 5–30 seconds.

---

## Roadmap

**v1 — shipped**
- [x] List, create, rename, delete reminder lists
- [x] List, search, get reminders with filters
- [x] Create, update, complete, delete reminders
- [x] Recurrence (daily/weekly/monthly/yearly with intervals, by-day, by-month, end conditions)
- [x] Alarms (relative, absolute, location)
- [x] Location/geofence reminders (enter/leave proximity)
- [x] Subtasks (parent/child)
- [x] Real Apple Reminders tags — read, enumerate, filter by, and write them
- [x] `x-apple-reminderkit://` deep links
- [x] Linked content — the Mail / Messages chip — read and write

**v2 — under consideration**
- [ ] Smart lists (today, scheduled, all, completed, flagged)
- [ ] Shared list write-back (currently best-effort; depends on CalDAV permissions)
- [ ] Bulk operations (delete multiple, complete multiple, move multiple)
- [ ] Public tags / linked-content API if Apple ships one — would let both write paths drop private ReminderKit


---

## Security & privacy

- All data stays on your Mac — this is a local MCP server. EventKit reads from the same store Reminders.app uses, including iCloud-synced lists if iCloud Reminders is enabled.
- Operations gated by macOS TCC: nothing happens until you grant Reminders access.
- macOS-only (`"platforms": ["darwin"]` in manifest).
- The connector never reaches outside the Reminders entity — no Calendar events, contacts, or files.
- Destructive operations (`delete_reminder_list`, `delete_reminder`) are explicit tools the model must choose to call; they don't run as side effects of reads.

---

## Troubleshooting

**"Apple Reminders access was not granted"**
Open **System Settings → Privacy & Security → Reminders**, ensure Claude Desktop is in the list and toggled on, then restart Claude Desktop.

**Permission prompt doesn't appear**
Unsigned Python interpreters launched by Claude Desktop sometimes don't trigger the prompt automatically. Add Claude Desktop manually under **System Settings → Privacy & Security → Reminders**.

**Recurrence rule didn't take effect**
EventKit silently rejects some illegal combinations (e.g. `days_of_month` on a weekly recurrence). Check the returned `ReminderDetail.recurrence` to see what was actually stored.

**Subtask doesn't appear / list_subtasks returns empty**
`EKReminder.parentReminder` is best-effort and not formally part of EventKit's public API. On some macOS versions it may not be available. The `list_subtasks` tool returns an empty array rather than failing.

**Extension doesn't appear after install**
Make sure you're running a recent Claude Desktop that supports MCPB extensions. Restart Claude Desktop after installing.

---

## License

[MIT](LICENSE)
