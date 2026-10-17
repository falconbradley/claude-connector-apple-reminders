"""Pydantic models for Apple Reminders MCP server."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sharing and assignments
# ---------------------------------------------------------------------------

# "accepted" = on the list; "pending" = invited, has not joined yet;
# "removed" = left or was removed; "unknown" = a status code not yet seen.
ShareeStatus = Literal["accepted", "pending", "removed", "unknown"]


class Sharee(BaseModel):
    """One person on a shared list, including the owner and you.

    `id` is the CloudKit share-participant id, which is what an assignment
    points at. It is per list: the same person has a different id on each
    list they share. See sharestore.py.
    """
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    status: ShareeStatus = "unknown"
    is_owner: bool = False
    is_me: bool = False


class ListSharing(BaseModel):
    """Who a list is shared with. Invisible to EventKit; read from the store."""
    is_shared: bool = False
    owned_by_me: bool = True
    owner: Optional[Sharee] = None
    sharees: list[Sharee] = []                # everyone on the list, owner first


class Assignment(BaseModel):
    """Who a reminder on a shared list is assigned to, and by whom."""
    assignee: Sharee
    assigned_by: Optional[Sharee] = None      # None when Apple nullified the originator
    assigned_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Reminder lists (EKCalendar of type Reminder)
# ---------------------------------------------------------------------------

class ReminderList(BaseModel):
    id: str                                   # EKCalendar.calendarIdentifier
    title: str
    color: Optional[str] = None               # "#RRGGBB"
    source_name: str                          # account name (e.g. "iCloud", "Local")
    source_type: str                          # "local" | "calDAV" | "exchange" | "subscribed" | "birthday" | "mobileMe"
    allows_modification: bool = True
    # Same convention as ReminderDetail.tags: None with
    # `sharing_unavailable_reason` set means the store could not be read;
    # None with no reason means the list is not in the Reminders store at
    # all (e.g. a CalDAV account's list), so it has no iCloud sharing.
    sharing: Optional[ListSharing] = None
    sharing_unavailable_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Recurrence, alarms, locations
# ---------------------------------------------------------------------------

Frequency = Literal["daily", "weekly", "monthly", "yearly"]
DayOfWeek = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class RecurrenceRule(BaseModel):
    """Represents an EKRecurrenceRule for a reminder."""
    frequency: Frequency
    interval: int = Field(default=1, ge=1)
    end_date: Optional[datetime] = None       # mutually exclusive with end_count
    end_count: Optional[int] = Field(default=None, ge=1)
    days_of_week: Optional[list[DayOfWeek]] = None
    days_of_month: Optional[list[int]] = None  # 1..31, or -1..-31 from end of month
    months_of_year: Optional[list[int]] = None  # 1..12
    set_positions: Optional[list[int]] = None  # 1..366 or -1..-366


class LocationSpec(BaseModel):
    """A geofence target for a reminder alarm."""
    title: str = ""
    latitude: float
    longitude: float
    radius_meters: Optional[float] = None     # None = system default


AlarmKind = Literal["relative", "absolute", "location"]
Proximity = Literal["enter", "leave", "none"]


class AlarmSpec(BaseModel):
    """An EKAlarm, specified relative to due date OR absolute date OR a geofence."""
    kind: AlarmKind
    relative_offset_seconds: Optional[float] = None  # negative = before due
    absolute_date: Optional[datetime] = None
    location: Optional[LocationSpec] = None
    proximity: Proximity = "none"


# ---------------------------------------------------------------------------
# Linked content
# ---------------------------------------------------------------------------

# "mail" = a Mail message (message:<Message-ID> URL, renders the Mail chip);
# "messages" = a Messages chat (chat-level, renders the Messages chip);
# "web" = an http(s) universal link; "other" = an activity from some other
# app (e.g. Notes), reported as read but not something this connector writes.
LinkKind = Literal["mail", "messages", "web", "other"]


class LinkedContent(BaseModel):
    """The Mail / Messages chip on a reminder — its *linked content*.

    This is what Siri's "remind me about this" and the share sheet attach.
    It is NOT the reminder's `url` field, which Reminders.app never shows.
    Stored as a REMUserActivity in the app's own database; see
    linkstore.py for how it is read and linkwriter.py for how it is set.
    """
    kind: LinkKind
    url: Optional[str] = None                 # what opening the chip resolves
    title: Optional[str] = None               # chat name for Messages; None for Mail
    activity_type: Optional[str] = None       # NSUserActivity type, when there is one


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

Priority = Literal[0, 1, 5, 9]                # 0=none, 1=high, 5=medium, 9=low (Apple convention)


class ReminderSummary(BaseModel):
    id: str                                   # EKReminder.calendarItemIdentifier
    list_id: str
    list_title: str
    title: str
    completed: bool = False
    due_date: Optional[datetime] = None
    is_all_day: bool = False                  # true when due_date has no time component
    priority: Priority = 0
    has_subtasks: bool = False
    parent_id: Optional[str] = None
    reminder_link: Optional[str] = None       # x-apple-reminderkit:// URL


class ReminderDetail(ReminderSummary):
    """Full reminder with notes, recurrence, alarms, location, and timestamps."""
    notes: Optional[str] = None
    url: Optional[str] = None
    start_date: Optional[datetime] = None
    completion_date: Optional[datetime] = None
    recurrence: Optional[RecurrenceRule] = None
    alarms: list[AlarmSpec] = []
    location: Optional[LocationSpec] = None
    subtask_ids: list[str] = []
    # Real Apple Reminders tags — the ones shown as chips in Reminders.app
    # and filterable in its sidebar. EventKit cannot see these; they are
    # read from the app's own store (see tagstore.py). None (rather than
    # []) means "could not be read", which is NOT the same as "no tags" —
    # see tags_unavailable_reason.
    tags: Optional[list[str]] = None
    tags_unavailable_reason: Optional[str] = None
    # `#tokens` scraped out of the title and notes text. These are NOT
    # Apple tags and never were: writing "#foo" into a title tags nothing.
    # Kept because some callers key off text conventions of their own, but
    # named so it can never be mistaken for the real thing.
    text_hashtags: list[str] = []
    # Linked content — the Mail / Messages chip. Same convention as `tags`:
    # None with `link_unavailable_reason` set means the store could not be
    # read; None with no reason means the reminder simply has no link.
    link: Optional[LinkedContent] = None
    link_unavailable_reason: Optional[str] = None
    # Assignment on a shared list. None with `assignment_unavailable_reason`
    # set means the store could not be read; None with no reason means the
    # reminder is not assigned to anyone.
    assignment: Optional[Assignment] = None
    assignment_unavailable_reason: Optional[str] = None
    creation_date: Optional[datetime] = None
    modification_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------

class TagInfo(BaseModel):
    """A real Apple Reminders tag, with its usage across all accounts."""
    name: str                                 # as the user typed it, no leading "#"
    reminder_count: int = 0
    reminder_ids: list[str] = []


class SearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    reminders: list[ReminderSummary]


class RemindersStats(BaseModel):
    list_count: int
    total: int
    incomplete: int
    completed: int
    overdue: int
    due_today: int


class ListResult(BaseModel):
    """Returned by create/update/delete list operations."""
    list: Optional[ReminderList] = None
    success: bool
    deleted_reminder_count: Optional[int] = None  # populated only by delete_reminder_list


class ReminderResult(BaseModel):
    """Returned by create/update/complete operations."""
    reminder: ReminderDetail
    success: bool


class DeleteResult(BaseModel):
    id: str
    success: bool
    deleted_subtask_count: int = 0
