"""
Apple Reminders MCP Server
==========================
Exposes Apple Reminders to Claude Desktop via the Model Context Protocol.
Uses Apple's first-class EventKit framework (via PyObjC) for full coverage:
subtasks, recurrence, alarms, locations/geofences, all priorities, and
structured due dates.

Permission model
----------------
Reminders access is gated by macOS TCC. On first tool invocation the OS
will prompt the user to grant access; alternatively the user can pre-grant
under System Settings → Privacy & Security → Reminders for the parent
process (Claude Desktop).

Tools provided
--------------
List management
  list_reminder_lists      - All reminder lists
  create_reminder_list     - Create a new list
  update_reminder_list     - Rename or recolor a list
  delete_reminder_list     - Delete a list (destructive)

Reminders — read
  get_stats                - Totals, overdue, due-today
  list_reminders           - Reminders in one or more lists, with filters
  search_reminders         - Free-text search across title and notes
  get_reminder             - Full detail for one reminder
  get_reminder_link        - x-apple-reminderkit:// URL
  list_subtasks            - Children of a parent reminder
  list_tags                - Every real Apple Reminders tag, with usage counts

Reminders — tags (write)
  add_reminder_tags        - Add real tags to a reminder
  remove_reminder_tags     - Remove real tags from a reminder
  set_reminder_tags        - Replace a reminder's tags outright

Tags
----
Real tags — the chips you see on a reminder in Reminders.app — are NOT
exposed by EventKit or by Reminders' AppleScript dictionary. They are read
directly from the Reminders app's own Core Data store, which needs Full
Disk Access for the host process (Claude Desktop). Without it, `tags` comes
back as null with `tags_unavailable_reason` set, rather than as an empty
list that would falsely read as "no tags".

Note that `#foo` typed into a title or note is just text and tags nothing.
That scraped value is reported separately as `text_hashtags`.

Writing tags is a separate capability from reading them: reads come from
the store file (Full Disk Access), writes go through Apple's private
ReminderKit framework (Reminders permission). Either can be unavailable
on its own, and each reports its own reason.

Reminders — write
  create_reminder          - Create with full property set (recurrence, alarms, location, parent)
  update_reminder          - Update any subset of properties
  complete_reminder        - Mark complete or uncomplete
  delete_reminder          - Delete a reminder (optionally cascading subtasks)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Optional

from mcp.server import MCPServer

from . import __version__
from .models import (
    AlarmSpec,
    DeleteResult,
    ListResult,
    LocationSpec,
    Priority,
    RecurrenceRule,
    ReminderDetail,
    ReminderList,
    ReminderResult,
    ReminderSummary,
    RemindersStats,
    SearchResult,
    TagInfo,
)
from .permissions import PermissionDeniedError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("apple_reminders_mcp")

# ---------------------------------------------------------------------------
# Lazy-initialised store. EventKit bootstrap (and a possible TCC prompt) can
# take a few seconds — we MUST NOT run it at import time, the MCP client
# would time out waiting for the initialize response.
# ---------------------------------------------------------------------------

_store = None  # type: ignore[var-annotated]


# ---------------------------------------------------------------------------
# MCP server app
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "Apple Reminders",
    instructions=(
        "Access to Apple Reminders on this Mac via EventKit. "
        "You can list and manage reminder lists; create, search, update, "
        "complete, and delete reminders; and work with subtasks, "
        "recurrence, alarms, and locations."
    ),
    version=__version__,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_store():
    """Return the RemindersStore, initialising on first call.

    Re-attempts on every call if init previously failed (the user may have
    granted Reminders access since the last attempt).
    """
    global _store
    if _store is not None:
        return _store
    # Defer the heavy import until first use.
    from .reminders import RemindersStore
    try:
        _store = RemindersStore()
        logger.info("Apple Reminders MCP ready (EventKit).")
        return _store
    except PermissionDeniedError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Could not initialise the Reminders store: {exc}"
        ) from exc


def _require_tag_writes(store) -> None:
    """Fail early, and with a reason, when tags cannot be written.

    Writing tags depends on private ReminderKit; reading them does not.
    A caller that gets this message should know which half is broken.
    """
    reason = store.tag_writes_unavailable_reason()
    if reason is not None:
        raise RuntimeError(f"Cannot write Apple Reminders tags: {reason}")


def _parse_iso(s: Optional[str], field: str) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field} date: {s!r}. Use ISO-8601 format.") from exc


# ---------------------------------------------------------------------------
# Tools — list management
# ---------------------------------------------------------------------------

@mcp.tool()
def list_reminder_lists() -> list[ReminderList]:
    """List every reminder list (calendar) on this Mac.

    Returns id, title, color, source (account), and whether the list can be
    modified. Use the returned ids with other tools that take a `list_id`.
    """
    return _require_store().list_lists()


@mcp.tool()
def create_reminder_list(
    title: str,
    color: Optional[str] = None,
    source_name: Optional[str] = None,
) -> ListResult:
    """Create a new reminder list.

    Args:
        title:        Non-empty list title.
        color:        Optional '#RRGGBB' hex color.
        source_name:  Account name to host the list (e.g. 'iCloud', 'On My Mac').
                      If unspecified, the first writable source is chosen.
    """
    return _require_store().create_list(title=title, color=color, source_name=source_name)


@mcp.tool()
def update_reminder_list(
    list_id: str,
    title: Optional[str] = None,
    color: Optional[str] = None,
) -> ListResult:
    """Rename or recolor a reminder list.

    Args:
        list_id:  Identifier from list_reminder_lists.
        title:    New title; pass null to leave unchanged.
        color:    New '#RRGGBB' color; pass null to leave unchanged.
    """
    if title is None and color is None:
        raise ValueError("Provide at least one of: title, color.")
    return _require_store().update_list(list_id=list_id, title=title, color=color)


@mcp.tool()
def delete_reminder_list(list_id: str) -> ListResult:
    """Delete a reminder list and all its reminders.

    DESTRUCTIVE. The returned `deleted_reminder_count` is the number of
    reminders that lived in the list at the moment of deletion.

    Args:
        list_id:  Identifier from list_reminder_lists.
    """
    return _require_store().delete_list(list_id)


# ---------------------------------------------------------------------------
# Tools — reminders (read)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> RemindersStats:
    """Return aggregate counts: total reminders, incomplete, completed, overdue, due today, list count."""
    return _require_store().get_stats()


@mcp.tool()
def list_reminders(
    list_ids: Optional[list[str]] = None,
    completed: Optional[bool] = None,
    priority: Optional[int] = None,
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    has_subtasks: Optional[bool] = None,
    text: Optional[str] = None,
    tags: Optional[list[str]] = None,
    match_all_tags: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """List reminders, with optional filters.

    Args:
        list_ids:     Restrict to these list ids. Empty/null = all lists.
        completed:    Filter by completion (true=done, false=open, null=any).
        priority:     Filter by priority value (0=none, 1=high, 5=medium, 9=low).
        due_before:   ISO-8601 — only reminders due on/before this time.
        due_after:    ISO-8601 — only reminders due on/after this time.
        has_subtasks: If true, only reminders with subtasks. If false, only without.
        text:         Substring match on title or notes.
        tags:         Only reminders carrying these real Apple tags.
                      Case-insensitive; a leading "#" is optional. This
                      matches genuine tags, not "#foo" text in the title.
        match_all_tags: If true, require every tag in `tags`; otherwise any.
        limit:        Max results per page (default 50, max 500).
        offset:       Pagination offset.
    """
    store = _require_store()
    limit = max(1, min(int(limit), 500))
    total, rows = store.list_reminders(
        list_ids=list_ids,
        completed=completed,
        priority=priority,
        due_before=_parse_iso(due_before, "due_before"),
        due_after=_parse_iso(due_after, "due_after"),
        has_subtasks=has_subtasks,
        text=text,
        tags=tags,
        match_all_tags=match_all_tags,
        limit=limit,
        offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, reminders=rows)


@mcp.tool()
def search_reminders(
    query: str,
    list_ids: Optional[list[str]] = None,
    completed: Optional[bool] = None,
    priority: Optional[int] = None,
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    tags: Optional[list[str]] = None,
    match_all_tags: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Search reminders by free text across title and notes.

    Args:
        query:       Required substring; case-insensitive.
        list_ids:    Restrict to these list ids; null = all lists.
        completed:   Filter by completion (true/false/null).
        priority:    Filter by priority (0/1/5/9).
        due_before:  ISO-8601 upper bound on due date.
        due_after:   ISO-8601 lower bound on due date.
        tags:        Only reminders carrying these real Apple tags
                     (case-insensitive, leading "#" optional).
        match_all_tags: If true, require every tag in `tags`; otherwise any.
        limit:       Max results (default 50, max 500).
        offset:      Pagination offset.
    """
    if not query or not query.strip():
        raise ValueError("`query` must be a non-empty string.")
    store = _require_store()
    limit = max(1, min(int(limit), 500))
    total, rows = store.search_reminders(
        query=query.strip(),
        list_ids=list_ids,
        completed=completed,
        priority=priority,
        due_before=_parse_iso(due_before, "due_before"),
        due_after=_parse_iso(due_after, "due_after"),
        tags=tags,
        match_all_tags=match_all_tags,
        limit=limit,
        offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, reminders=rows)


@mcp.tool()
def get_reminder(reminder_id: str) -> ReminderDetail:
    """Fetch one reminder with all properties.

    Includes notes, recurrence, alarms, location, subtasks, and real tags.

    `tags` holds genuine Apple Reminders tags. It is null (not empty) when
    the local store could not be read, with the reason in
    `tags_unavailable_reason`. `text_hashtags` is a separate, purely
    textual scrape of "#tokens" from the title and notes — those are not
    tags and never were.

    Args:
        reminder_id: Identifier from list_reminders / search_reminders.
    """
    detail = _require_store().get_reminder(reminder_id)
    if detail is None:
        raise ValueError(f"Reminder not found: {reminder_id}")
    return detail


@mcp.tool()
def get_reminder_link(reminder_id: str) -> dict:
    """Return an x-apple-reminderkit:// URL that opens the reminder in Reminders.app.

    Args:
        reminder_id: Identifier from list_reminders / search_reminders.
    """
    link = _require_store().get_reminder_link(reminder_id)
    return {"reminder_id": reminder_id, "reminder_link": link}


@mcp.tool()
def list_tags() -> list[TagInfo]:
    """List every real Apple Reminders tag, with how many reminders use it.

    These are the tags shown as chips on a reminder and in the Reminders.app
    sidebar. Includes tags that exist but are not currently applied to any
    reminder (count 0), since the app still offers those when filtering.

    Reads the Reminders app's own store, which needs Full Disk Access for
    the host process; without it this raises rather than returning [].
    """
    store = _require_store()
    reason = store.tags_unavailable_reason()
    if reason is not None:
        raise RuntimeError(f"Cannot read Apple Reminders tags: {reason}")
    return store.list_tags()


@mcp.tool()
def add_reminder_tags(reminder_id: str, tags: list[str]) -> ReminderResult:
    """Add real Apple tags to a reminder.

    These are genuine tags — the chips shown on the reminder in
    Reminders.app and filterable from its sidebar — not "#text" in the
    title. Tags that do not exist yet are created; tags already on the
    reminder are left alone, so calling this twice is safe.

    Writing tags uses Apple's private ReminderKit framework, because no
    supported API exposes tags at all. It can therefore stop working
    after a macOS update, in which case this raises with a clear reason
    rather than silently doing nothing.

    Args:
        reminder_id: Identifier from list_reminders / search_reminders.
        tags:        Tag names. A leading "#" is optional.
    """
    if not tags:
        raise ValueError("`tags` must contain at least one tag name.")
    store = _require_store()
    _require_tag_writes(store)
    return ReminderResult(reminder=store.add_tags(reminder_id, tags), success=True)


@mcp.tool()
def remove_reminder_tags(reminder_id: str, tags: list[str]) -> ReminderResult:
    """Remove real Apple tags from a reminder.

    Names not currently on the reminder are ignored. Removing a tag from
    its last reminder does not delete the tag itself — Reminders keeps
    the label, exactly as it does when you do this in the app.

    Args:
        reminder_id: Identifier from list_reminders / search_reminders.
        tags:        Tag names to remove. A leading "#" is optional.
    """
    if not tags:
        raise ValueError("`tags` must contain at least one tag name.")
    store = _require_store()
    _require_tag_writes(store)
    return ReminderResult(
        reminder=store.remove_tags(reminder_id, tags), success=True
    )


@mcp.tool()
def set_reminder_tags(reminder_id: str, tags: list[str]) -> ReminderResult:
    """Replace a reminder's real Apple tags with exactly this set.

    Tags not in the list are removed, missing ones are added, and ones
    already correct are left untouched. Pass an empty list to clear every
    tag from the reminder.

    Args:
        reminder_id: Identifier from list_reminders / search_reminders.
        tags:        The complete desired tag set. Empty list clears all.
    """
    store = _require_store()
    _require_tag_writes(store)
    return ReminderResult(reminder=store.set_tags(reminder_id, tags), success=True)


@mcp.tool()
def list_subtasks(reminder_id: str) -> list[ReminderSummary]:
    """List the immediate subtasks (child reminders) of a parent reminder.

    Args:
        reminder_id: Identifier of the parent reminder.
    """
    return _require_store().list_subtasks(reminder_id)


# ---------------------------------------------------------------------------
# Tools — reminders (write)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_reminder(
    title: str,
    list_id: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    due_date: Optional[str] = None,
    is_all_day: bool = False,
    start_date: Optional[str] = None,
    priority: int = 0,
    recurrence: Optional[RecurrenceRule] = None,
    alarms: Optional[list[AlarmSpec]] = None,
    location: Optional[LocationSpec] = None,
    parent_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> ReminderResult:
    """Create a new reminder with rich properties.

    Args:
        title:       Non-empty title. Putting '#foo' in it does NOT tag the
                     reminder — use `tags` for that.
        list_id:     Target list id. If null, uses the default Reminders list.
        notes:       Optional body text.
        url:         Optional URL to attach.
        due_date:    ISO-8601 due date. If is_all_day=true, the time component is dropped.
        is_all_day:  When true, the due date is stored as a date-only component.
        start_date:  ISO-8601 start date (use when a reminder spans a window).
        priority:    0 (none), 1 (high), 5 (medium), 9 (low).
        recurrence:  Optional RecurrenceRule (frequency + interval + termination + by-rules).
        alarms:      Optional list of AlarmSpec (relative offset, absolute date, or location).
        location:    Optional geofence target. Stored as a location alarm.
        parent_id:   Identifier of a parent reminder to make this a subtask.
        tags:        Real Apple tags to apply, e.g. ["autoreview"]. A
                     leading "#" is optional. Tags are applied after the
                     reminder is created; if that step fails the reminder
                     still exists and the reason is reported in
                     `tags_unavailable_reason`.
    """
    if priority not in (0, 1, 5, 9):
        raise ValueError("priority must be one of 0, 1, 5, 9.")
    if not title or not title.strip():
        raise ValueError("`title` must be a non-empty string.")
    store = _require_store()
    detail = store.create_reminder(
        title=title,
        list_id=list_id,
        notes=notes,
        url=url,
        due_date=_parse_iso(due_date, "due_date"),
        is_all_day=is_all_day,
        start_date=_parse_iso(start_date, "start_date"),
        priority=priority,  # type: ignore[arg-type]
        recurrence=recurrence,
        alarms=alarms,
        location=location,
        parent_id=parent_id,
        tags=tags,
    )
    return ReminderResult(reminder=detail, success=True)


@mcp.tool()
def update_reminder(
    reminder_id: str,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    list_id: Optional[str] = None,
    due_date: Optional[str] = None,
    is_all_day: Optional[bool] = None,
    start_date: Optional[str] = None,
    priority: Optional[int] = None,
    recurrence: Optional[RecurrenceRule] = None,
    alarms: Optional[list[AlarmSpec]] = None,
    location: Optional[LocationSpec] = None,
    clear_due_date: bool = False,
    clear_start_date: bool = False,
    clear_recurrence: bool = False,
    clear_alarms: bool = False,
    clear_location: bool = False,
    clear_url: bool = False,
    clear_notes: bool = False,
) -> ReminderResult:
    """Update any subset of a reminder's properties.

    Optional fields default to leaving the existing value untouched. Use the
    `clear_*` flags to explicitly remove a value (since passing null cannot
    distinguish "omit" from "set to null" in MCP tool calls).

    Args:
        reminder_id:        Identifier of the reminder to update.
        title:              New title (cannot be empty).
        notes:              New notes; or set clear_notes=true to remove.
        url:                New URL; or set clear_url=true to remove.
        list_id:            Move to a different list.
        due_date:           New ISO-8601 due date; or clear_due_date=true to remove.
        is_all_day:         When supplying due_date or start_date, marks them all-day.
        start_date:         New ISO-8601 start date; or clear_start_date=true to remove.
        priority:           New priority (0/1/5/9).
        recurrence:         New recurrence rule; or clear_recurrence=true to remove.
        alarms:             Replacement list of alarms; or clear_alarms=true to remove.
        location:           Add a location alarm; or clear_location=true to remove existing.
        clear_*:            Explicit removal flags.
    """
    if priority is not None and priority not in (0, 1, 5, 9):
        raise ValueError("priority must be one of 0, 1, 5, 9.")
    detail = _require_store().update_reminder(
        reminder_id=reminder_id,
        title=title,
        notes=notes,
        url=url,
        list_id=list_id,
        due_date=_parse_iso(due_date, "due_date"),
        is_all_day=is_all_day,
        start_date=_parse_iso(start_date, "start_date"),
        priority=priority,
        recurrence=recurrence,
        alarms=alarms,
        location=location,
        clear_due_date=clear_due_date,
        clear_start_date=clear_start_date,
        clear_recurrence=clear_recurrence,
        clear_alarms=clear_alarms,
        clear_location=clear_location,
        clear_url=clear_url,
        clear_notes=clear_notes,
    )
    return ReminderResult(reminder=detail, success=True)


@mcp.tool()
def complete_reminder(
    reminder_id: str,
    completed: Optional[bool] = None,
) -> ReminderResult:
    """Mark a reminder as complete or uncomplete (or toggle if `completed` is null).

    Args:
        reminder_id: Identifier of the reminder.
        completed:   True to complete, false to uncomplete, null to toggle.
    """
    detail = _require_store().complete_reminder(reminder_id, completed)
    return ReminderResult(reminder=detail, success=True)


@mcp.tool()
def delete_reminder(
    reminder_id: str,
    cascade: bool = True,
) -> DeleteResult:
    """Delete a reminder.

    Args:
        reminder_id: Identifier of the reminder.
        cascade:     If true (default), also delete its subtasks. If false,
                     subtasks are orphaned (their parent_id will become null).
    """
    return _require_store().delete_reminder(reminder_id, cascade=cascade)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
