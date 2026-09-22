"""EventKit-backed bridge to Apple Reminders.

Wraps ``EKEventStore`` with a synchronous Python API.  The async fetch
calls (``fetchRemindersMatchingPredicate:completion:``) are bridged to a
threading.Event so callers see plain return values.

All methods raise ``PermissionDeniedError`` if Reminders access has not
been granted, and ``RuntimeError`` for other EventKit failures.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Optional
from urllib.parse import quote

import objc  # type: ignore
from EventKit import (  # type: ignore
    EKAlarm,
    EKCalendar,
    EKEntityTypeReminder,
    EKEventStore,
    EKRecurrenceDayOfWeek,
    EKRecurrenceEnd,
    EKRecurrenceRule,
    EKReminder,
    EKSourceTypeBirthdays,
    EKSourceTypeCalDAV,
    EKSourceTypeExchange,
    EKSourceTypeLocal,
    EKSourceTypeMobileMe,
    EKSourceTypeSubscribed,
    EKStructuredLocation,
)
from CoreLocation import CLLocation  # type: ignore
from Foundation import (  # type: ignore
    NSCalendar,
    NSCalendarUnitDay,
    NSCalendarUnitHour,
    NSCalendarUnitMinute,
    NSCalendarUnitMonth,
    NSCalendarUnitSecond,
    NSCalendarUnitYear,
    NSDate,
    NSDateComponents,
    NSPredicate,
    NSURL,
)

from .models import (
    AlarmSpec,
    DayOfWeek,
    DeleteResult,
    Frequency,
    LinkedContent,
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
from .tagstore import RemindersTagStore, TagStoreError
from .tagwriter import RemindersTagWriter, TagWriteError, TagWriteUnavailable
from .linkstore import RemindersLinkStore, LinkStoreError
from .linkwriter import RemindersLinkWriter, LinkWriteError, parse_link
from .permissions import (
    PermissionDeniedError,
    authorization_status_label,
    request_reminders_access,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants and lookup tables
# ---------------------------------------------------------------------------

# EKRecurrenceFrequency
EK_FREQ_DAILY = 0
EK_FREQ_WEEKLY = 1
EK_FREQ_MONTHLY = 2
EK_FREQ_YEARLY = 3

_FREQ_TO_EK = {
    "daily": EK_FREQ_DAILY,
    "weekly": EK_FREQ_WEEKLY,
    "monthly": EK_FREQ_MONTHLY,
    "yearly": EK_FREQ_YEARLY,
}
_EK_TO_FREQ: dict[int, Frequency] = {v: k for k, v in _FREQ_TO_EK.items()}  # type: ignore[misc]

# EKWeekday is 1=Sunday..7=Saturday in EventKit; map to Python-style names.
_DAY_TO_EK = {
    "sunday": 1, "monday": 2, "tuesday": 3, "wednesday": 4,
    "thursday": 5, "friday": 6, "saturday": 7,
}
_EK_TO_DAY: dict[int, DayOfWeek] = {v: k for k, v in _DAY_TO_EK.items()}  # type: ignore[misc]

# EKAlarmProximity
EK_PROX_NONE = 0
EK_PROX_ENTER = 1
EK_PROX_LEAVE = 2

_PROX_TO_EK = {"none": EK_PROX_NONE, "enter": EK_PROX_ENTER, "leave": EK_PROX_LEAVE}
_EK_TO_PROX = {v: k for k, v in _PROX_TO_EK.items()}

_SOURCE_TYPE_LABEL = {
    EKSourceTypeLocal: "local",
    EKSourceTypeExchange: "exchange",
    EKSourceTypeCalDAV: "calDAV",
    EKSourceTypeMobileMe: "mobileMe",
    EKSourceTypeSubscribed: "subscribed",
    EKSourceTypeBirthdays: "birthday",
}

# Matches a "#token" in free text. This finds *text*, not Apple tags — see
# _extract_text_hashtags.
_HASHTAG_RE = re.compile(r"(?<!\w)#([\w\-]+)")

_PRIORITY_VALID: set[int] = {0, 1, 5, 9}


# ---------------------------------------------------------------------------
# Date / NSDate / NSDateComponents helpers
# ---------------------------------------------------------------------------

def _ns_date_to_datetime(ns_date: Any) -> Optional[datetime]:
    if ns_date is None:
        return None
    try:
        ts = ns_date.timeIntervalSince1970()
    except Exception:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def _datetime_to_ns_date(dt: Optional[datetime]) -> Optional[Any]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _components_to_datetime(comp: Any) -> tuple[Optional[datetime], bool]:
    """Return (datetime, is_all_day)."""
    if comp is None:
        return None, False
    cal = NSCalendar.currentCalendar()
    ns_date = cal.dateFromComponents_(comp)
    if ns_date is None:
        return None, False
    dt = _ns_date_to_datetime(ns_date)
    has_time = comp.hour() != 0x7FFFFFFFFFFFFFFF or comp.minute() != 0x7FFFFFFFFFFFFFFF
    is_all_day = not has_time
    return dt, is_all_day


def _datetime_to_components(dt: datetime, all_day: bool) -> Any:
    comp = NSDateComponents.alloc().init()
    comp.setYear_(dt.year)
    comp.setMonth_(dt.month)
    comp.setDay_(dt.day)
    if not all_day:
        comp.setHour_(dt.hour)
        comp.setMinute_(dt.minute)
        comp.setSecond_(dt.second)
    return comp


# ---------------------------------------------------------------------------
# Conversion: EventKit objects ↔ Pydantic models
# ---------------------------------------------------------------------------

def _color_to_hex(color: Any) -> Optional[str]:
    """Convert a CGColorRef to '#RRGGBB' (best effort)."""
    if color is None:
        return None
    try:
        from Quartz import CGColorGetComponents, CGColorGetNumberOfComponents  # type: ignore
        n = CGColorGetNumberOfComponents(color)
        comps = CGColorGetComponents(color)
        if comps is None:
            return None
        if n >= 3:
            r, g, b = comps[0], comps[1], comps[2]
        elif n == 2:
            # Grayscale + alpha
            r = g = b = comps[0]
        else:
            return None
        return "#{:02X}{:02X}{:02X}".format(
            max(0, min(255, int(round(r * 255)))),
            max(0, min(255, int(round(g * 255)))),
            max(0, min(255, int(round(b * 255)))),
        )
    except Exception:
        return None


def _hex_to_cgcolor(hex_str: str) -> Optional[Any]:
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
    except ValueError:
        return None
    try:
        from Quartz import (  # type: ignore
            CGColorCreate,
            CGColorSpaceCreateDeviceRGB,
        )
        cs = CGColorSpaceCreateDeviceRGB()
        return CGColorCreate(cs, [r, g, b, 1.0])
    except Exception:
        return None


def _source_names(sources: Iterable[Any]) -> str:
    names = [str(s.title()) for s in sources]
    return ", ".join(repr(n) for n in names) if names else "none"


def _calendar_to_model(cal: Any) -> ReminderList:
    src = cal.source()
    return ReminderList(
        id=str(cal.calendarIdentifier()),
        title=str(cal.title()),
        color=_color_to_hex(cal.CGColor()),
        source_name=str(src.title()) if src else "",
        source_type=_SOURCE_TYPE_LABEL.get(src.sourceType() if src else -1, "unknown"),
        allows_modification=bool(cal.allowsContentModifications()),
    )


def _recurrence_to_model(rule: Any) -> Optional[RecurrenceRule]:
    if rule is None:
        return None
    freq = _EK_TO_FREQ.get(rule.frequency())
    if freq is None:
        return None
    end = rule.recurrenceEnd()
    end_date: Optional[datetime] = None
    end_count: Optional[int] = None
    if end is not None:
        if end.endDate() is not None:
            end_date = _ns_date_to_datetime(end.endDate())
        oc = end.occurrenceCount()
        if oc and oc > 0:
            end_count = int(oc)

    days_of_week: Optional[list[DayOfWeek]] = None
    raw_days = rule.daysOfTheWeek()
    if raw_days:
        days = []
        for d in raw_days:
            day_of_week = d.dayOfTheWeek() if hasattr(d, "dayOfTheWeek") else None
            if day_of_week and day_of_week in _EK_TO_DAY:
                days.append(_EK_TO_DAY[day_of_week])
        days_of_week = days or None

    def _to_int_list(arr: Any) -> Optional[list[int]]:
        if not arr:
            return None
        out = []
        for x in arr:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out or None

    return RecurrenceRule(
        frequency=freq,
        interval=int(rule.interval()),
        end_date=end_date,
        end_count=end_count,
        days_of_week=days_of_week,
        days_of_month=_to_int_list(rule.daysOfTheMonth()),
        months_of_year=_to_int_list(rule.monthsOfTheYear()),
        set_positions=_to_int_list(rule.setPositions()),
    )


def _model_to_recurrence(model: RecurrenceRule) -> Any:
    freq = _FREQ_TO_EK[model.frequency]
    end = None
    if model.end_date is not None:
        end = EKRecurrenceEnd.recurrenceEndWithEndDate_(_datetime_to_ns_date(model.end_date))
    elif model.end_count is not None:
        end = EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(model.end_count)

    days_of_week = None
    if model.days_of_week:
        days_of_week = [
            EKRecurrenceDayOfWeek.dayOfWeek_(_DAY_TO_EK[d])
            for d in model.days_of_week
        ]

    return EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
        freq,
        max(1, int(model.interval)),
        days_of_week,
        model.days_of_month,
        model.months_of_year,
        None,
        None,
        model.set_positions,
        end,
    )


def _alarm_to_model(alarm: Any) -> AlarmSpec:
    rel = alarm.relativeOffset()
    abs_date = alarm.absoluteDate()
    sloc = alarm.structuredLocation()
    location: Optional[LocationSpec] = None
    if sloc is not None:
        geo = sloc.geoLocation()
        if geo is not None:
            coord = geo.coordinate()
            location = LocationSpec(
                title=str(sloc.title()) if sloc.title() else "",
                latitude=float(coord.latitude),
                longitude=float(coord.longitude),
                radius_meters=float(sloc.radius()) if sloc.radius() > 0 else None,
            )

    proximity = _EK_TO_PROX.get(int(alarm.proximity()), "none")

    if location is not None and abs_date is None and rel == 0:
        kind = "location"
    elif abs_date is not None:
        kind = "absolute"
    else:
        kind = "relative"

    return AlarmSpec(
        kind=kind,  # type: ignore[arg-type]
        relative_offset_seconds=float(rel) if kind == "relative" else None,
        absolute_date=_ns_date_to_datetime(abs_date) if kind == "absolute" else None,
        location=location,
        proximity=proximity,  # type: ignore[arg-type]
    )


def _model_to_alarm(spec: AlarmSpec) -> Any:
    if spec.kind == "absolute":
        if spec.absolute_date is None:
            raise ValueError("Absolute alarm requires absolute_date.")
        alarm = EKAlarm.alarmWithAbsoluteDate_(_datetime_to_ns_date(spec.absolute_date))
    elif spec.kind == "relative":
        offset = float(spec.relative_offset_seconds or 0.0)
        alarm = EKAlarm.alarmWithRelativeOffset_(offset)
    elif spec.kind == "location":
        # Location alarms in EventKit use a 0-offset relative alarm + structuredLocation.
        alarm = EKAlarm.alarmWithRelativeOffset_(0.0)
    else:
        raise ValueError(f"Unknown alarm kind: {spec.kind}")

    if spec.location is not None:
        cl_loc = CLLocation.alloc().initWithLatitude_longitude_(
            spec.location.latitude, spec.location.longitude
        )
        sl = EKStructuredLocation.locationWithTitle_(spec.location.title or "")
        sl.setGeoLocation_(cl_loc)
        if spec.location.radius_meters is not None:
            sl.setRadius_(float(spec.location.radius_meters))
        alarm.setStructuredLocation_(sl)

    alarm.setProximity_(_PROX_TO_EK.get(spec.proximity, EK_PROX_NONE))
    return alarm


def _structured_location_to_model(sloc: Any) -> Optional[LocationSpec]:
    if sloc is None:
        return None
    geo = sloc.geoLocation()
    if geo is None:
        return None
    coord = geo.coordinate()
    return LocationSpec(
        title=str(sloc.title()) if sloc.title() else "",
        latitude=float(coord.latitude),
        longitude=float(coord.longitude),
        radius_meters=float(sloc.radius()) if sloc.radius() > 0 else None,
    )


def _extract_text_hashtags(text: Optional[str]) -> list[str]:
    """Scrape "#token" strings out of free text.

    These are NOT Apple Reminders tags. Typing "#foo" into a title or note
    creates no tag — the text just sits there. Real tags live in the app's
    own store and are read by tagstore.py. This helper exists only because
    some callers use text conventions of their own; the result is surfaced
    as ``text_hashtags`` so it can never pass for the real thing.
    """
    if not text:
        return []
    return _HASHTAG_RE.findall(text)


def _make_reminder_link(reminder_id: str) -> str:
    """Build an x-apple-reminderkit:// URL that opens a reminder in Reminders.app."""
    return f"x-apple-reminderkit://REMCDReminder/{quote(reminder_id, safe='')}"


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class RemindersStore:
    """Synchronous wrapper around EKEventStore for the Reminders entity."""

    def __init__(self) -> None:
        self._store = EKEventStore.alloc().init()
        self._access_granted = False
        self._lists_by_id: dict[str, Any] = {}  # cache: id → EKCalendar
        # Short-lived cache: id → EKReminder. EventKit hands out snapshots,
        # so anything left here across a refresh would be served stale.
        # _refresh_eventkit() clears it; never let it outlive a call chain.
        self._reminders_by_id: dict[str, Any] = {}
        # Real tags are invisible to EventKit; this reads them from the
        # Reminders app's own store. Constructed lazily-cheap (no I/O until
        # first query) so a missing store cannot break EventKit bootstrap.
        self._tags = RemindersTagStore()
        # Writing tags has no supported route at all, so it goes through
        # Apple's private ReminderKit — see tagwriter.py for why, and for
        # the capability check that keeps an OS change from half-applying
        # a write. Reading does not depend on this.
        self._tag_writer = RemindersTagWriter()
        # Linked content — the Mail / Messages chip — is the same story:
        # invisible to EventKit, read from the store file, written through
        # ReminderKit. See linkstore.py and linkwriter.py.
        self._links = RemindersLinkStore()
        self._link_writer = RemindersLinkWriter()

        self._ensure_access()
        self._refresh_lists()

    # ------------------------------------------------------------------
    # Permission gate
    # ------------------------------------------------------------------

    def _ensure_access(self) -> None:
        if self._access_granted:
            return
        # Best-effort: check current authorization status before prompting.
        try:
            cls = EKEventStore.authorizationStatusForEntityType_
            status = int(cls(EKEntityTypeReminder))
            logger.info(
                "Reminders authorization status: %s (%d)",
                authorization_status_label(status), status,
            )
            if status in (3, 5):  # authorized / fullAccess
                self._access_granted = True
                return
            if status in (1, 2):  # restricted / denied
                raise PermissionDeniedError(
                    f"Status: {authorization_status_label(status)}"
                )
        except AttributeError:
            pass

        granted = request_reminders_access(self._store)
        if not granted:
            raise PermissionDeniedError()
        self._access_granted = True

    # ------------------------------------------------------------------
    # Lists (calendars of type Reminder)
    # ------------------------------------------------------------------

    def _refresh_lists(self) -> None:
        cals = self._store.calendarsForEntityType_(EKEntityTypeReminder) or []
        self._lists_by_id = {str(c.calendarIdentifier()): c for c in cals}

    def _refresh_eventkit(self) -> None:
        """Make this process see edits made elsewhere (Reminders.app, iCloud).

        EKEventStore hands out object *snapshots* and keeps its own cache;
        neither notices external writes on its own. Without this, a
        long-lived MCP server answers get_reminder from whatever it first
        fetched — which is how an in-app tag edit could come back as a
        byte-identical record with an unchanged modification_date.

        refreshSourcesIfNecessary() pulls remote changes; reset() drops the
        cached graph. reset() invalidates every EKObject we hold, so the
        calendar map and the reminder cache are rebuilt straight after.
        """
        for call in ("refreshSourcesIfNecessary", "reset"):
            fn = getattr(self._store, call, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("EKEventStore.%s() failed: %s", call, exc)
        self._reminders_by_id.clear()
        self._refresh_lists()

    def list_lists(self) -> list[ReminderList]:
        self._refresh_lists()
        return [_calendar_to_model(c) for c in self._lists_by_id.values()]

    def _get_list(self, list_id: str) -> Any:
        cal = self._lists_by_id.get(list_id)
        if cal is None:
            self._refresh_lists()
            cal = self._lists_by_id.get(list_id)
        if cal is None:
            raise ValueError(f"Reminder list not found: {list_id}")
        return cal

    def _reminder_sources(self) -> list[Any]:
        """Sources that can actually host reminder lists, best first.

        EventKit exposes no "does this source support reminders?" flag, and
        source *type* is not a proxy for it: a Google or Exchange account
        shows up as calDAV alongside iCloud but serves calendars only, so
        saving a reminder list into it fails with "That account does not
        support reminders."  The observable signal is whether the source
        already vends calendars for ``EKEntityTypeReminder``.

        The source behind ``defaultCalendarForNewReminders()`` is the one
        Reminders.app itself writes to, so it leads the list.
        """
        ranked: list[Any] = []
        seen: set[str] = set()

        def add(src: Any) -> None:
            if src is None:
                return
            key = str(src.sourceIdentifier())
            if key in seen:
                return
            seen.add(key)
            ranked.append(src)

        try:
            default_cal = self._store.defaultCalendarForNewReminders()
        except Exception:  # pragma: no cover - defensive
            default_cal = None
        if default_cal is not None:
            add(default_cal.source())

        capable = []
        for src in self._store.sources() or []:
            cals = src.calendarsForEntityType_(EKEntityTypeReminder) or []
            if len(cals):
                capable.append(src)
        # Among the rest, keep the historical iCloud-before-local preference.
        preferred_order = (
            EKSourceTypeCalDAV,
            EKSourceTypeMobileMe,
            EKSourceTypeLocal,
            EKSourceTypeExchange,
        )
        for st in preferred_order:
            for src in capable:
                if src.sourceType() == st:
                    add(src)
        for src in capable:
            add(src)
        return ranked

    def _choose_list_source(self, source_name: Optional[str]) -> Any:
        """Pick the source a new reminder list should be saved into."""
        candidates = self._reminder_sources()
        if source_name:
            wanted = source_name.strip().lower()
            for src in candidates:
                if str(src.title()).lower() == wanted:
                    return src
            # Named a source that exists but cannot hold reminders.
            for src in self._store.sources() or []:
                if str(src.title()).lower() == wanted:
                    raise ValueError(
                        f"Source {source_name!r} does not support reminders. "
                        f"Available: {_source_names(candidates)}"
                    )
            raise ValueError(
                f"Unknown source: {source_name!r}. "
                f"Available: {_source_names(candidates)}"
            )
        if candidates:
            return candidates[0]
        # No source vends reminder calendars yet (a machine with none at
        # all); fall back to any writable source rather than refusing.
        for st in (EKSourceTypeCalDAV, EKSourceTypeMobileMe, EKSourceTypeLocal):
            for src in self._store.sources() or []:
                if src.sourceType() == st:
                    return src
        raise RuntimeError("No source available for new reminder list.")

    def create_list(
        self,
        title: str,
        color: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> ListResult:
        if not title or not title.strip():
            raise ValueError("List title must be a non-empty string.")
        cal = EKCalendar.calendarForEntityType_eventStore_(EKEntityTypeReminder, self._store)
        cal.setTitle_(title.strip())

        cal.setSource_(self._choose_list_source(source_name))

        if color:
            cgc = _hex_to_cgcolor(color)
            if cgc is not None:
                cal.setCGColor_(cgc)

        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not create list: {_nserror_str(err)}")

        self._refresh_lists()
        return ListResult(list=_calendar_to_model(cal), success=True)

    def update_list(
        self,
        list_id: str,
        title: Optional[str] = None,
        color: Optional[str] = None,
    ) -> ListResult:
        cal = self._get_list(list_id)
        if title is not None:
            if not title.strip():
                raise ValueError("List title must be a non-empty string.")
            cal.setTitle_(title.strip())
        if color is not None:
            cgc = _hex_to_cgcolor(color)
            if cgc is None:
                raise ValueError(f"Invalid color: {color!r}. Use '#RRGGBB'.")
            cal.setCGColor_(cgc)
        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not update list: {_nserror_str(err)}")
        self._refresh_lists()
        return ListResult(list=_calendar_to_model(cal), success=True)

    def delete_list(self, list_id: str) -> ListResult:
        cal = self._get_list(list_id)
        # Count reminders before deletion (purely informational).
        try:
            count = self._count_reminders_in_lists([cal])
        except Exception:
            count = None
        ok, err = self._store.removeCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not delete list: {_nserror_str(err)}")
        self._refresh_lists()
        return ListResult(list=None, success=True, deleted_reminder_count=count)

    # ------------------------------------------------------------------
    # Reminders — read
    # ------------------------------------------------------------------

    def _fetch(
        self,
        predicate: Any,
        timeout: float = 30.0,
    ) -> list[Any]:
        """Async EventKit fetch → blocking list of EKReminder."""
        result: dict[str, Any] = {"reminders": []}
        done = threading.Event()

        def callback(reminders) -> None:  # type: ignore[no-untyped-def]
            result["reminders"] = list(reminders) if reminders else []
            done.set()

        self._store.fetchRemindersMatchingPredicate_completion_(predicate, callback)
        if not done.wait(timeout=timeout):
            raise RuntimeError(f"EventKit fetch timed out after {timeout}s.")
        return result["reminders"]

    def _count_reminders_in_lists(self, lists: list[Any]) -> int:
        pred = self._store.predicateForRemindersInCalendars_(lists)
        return len(self._fetch(pred))

    def _build_predicate(
        self,
        lists: Optional[list[Any]],
        completed: Optional[bool],
        due_before: Optional[datetime],
        due_after: Optional[datetime],
    ) -> Any:
        # Apple EventKit offers three prebuilt predicate factories:
        #   predicateForIncompleteRemindersWithDueDateStarting:ending:calendars:
        #   predicateForCompletedRemindersWithCompletionDateStarting:ending:calendars:
        #   predicateForRemindersInCalendars:
        # Pick whichever fits and apply post-filtering for anything beyond.
        # Note the asymmetry in Apple's naming: "Incomplete" but "Completed".
        starting = _datetime_to_ns_date(due_after) if due_after else None
        ending = _datetime_to_ns_date(due_before) if due_before else None

        if completed is False:
            return self._store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                starting, ending, lists,
            )
        if completed is True:
            # This predicate bounds by *completion* date, but due_after/due_before
            # are due-date bounds — passing them here would silently drop items
            # completed outside the window. Fetch all completed rows and let
            # _post_filter apply the due-date filter.
            return self._store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
                None, None, lists,
            )
        return self._store.predicateForRemindersInCalendars_(lists)

    def _post_filter(
        self,
        rows: Iterable[Any],
        text: Optional[str],
        priority: Optional[int],
        due_before: Optional[datetime],
        due_after: Optional[datetime],
        completed: Optional[bool],
        has_subtasks: Optional[bool],
    ) -> list[Any]:
        text_lc = text.lower() if text else None
        out = []
        for r in rows:
            if text_lc is not None:
                title = (str(r.title() or "")).lower()
                notes = (str(r.notes() or "")).lower()
                if text_lc not in title and text_lc not in notes:
                    continue
            if priority is not None and int(r.priority()) != priority:
                continue
            if completed is not None and bool(r.isCompleted()) != completed:
                continue
            if due_before is not None or due_after is not None:
                d, _ = _components_to_datetime(r.dueDateComponents())
                if d is None:
                    if due_before is not None or due_after is not None:
                        continue
                else:
                    if due_after is not None and d < due_after:
                        continue
                    if due_before is not None and d > due_before:
                        continue
            if has_subtasks is not None:
                children = self._children_of(r)
                if has_subtasks and not children:
                    continue
                if not has_subtasks and children:
                    continue
            out.append(r)
        return out

    def _resolve_lists(self, list_ids: Optional[list[str]]) -> Optional[list[Any]]:
        if not list_ids:
            return None
        out = []
        for lid in list_ids:
            out.append(self._get_list(lid))
        return out

    def _to_summary(self, r: Any) -> ReminderSummary:
        cal = r.calendar()
        due, all_day = _components_to_datetime(r.dueDateComponents())
        parent = self._parent_of(r)
        ident = str(r.calendarItemIdentifier())
        # Use fast path only — the fallback would trigger a full-list fetch
        # per row, which is fine for one reminder but unacceptable when
        # building summaries for a whole list.
        fast_kids = self._children_of_fast(r)
        has_subtasks = bool(fast_kids) if fast_kids is not None else False
        return ReminderSummary(
            id=ident,
            list_id=str(cal.calendarIdentifier()) if cal else "",
            list_title=str(cal.title()) if cal else "",
            title=str(r.title() or ""),
            completed=bool(r.isCompleted()),
            due_date=due,
            is_all_day=all_day,
            priority=int(r.priority()),  # type: ignore[arg-type]
            has_subtasks=has_subtasks,
            parent_id=str(parent.calendarItemIdentifier()) if parent else None,
            reminder_link=_make_reminder_link(ident),
        )

    def _to_detail(self, r: Any) -> ReminderDetail:
        summary = self._to_summary(r)
        notes = str(r.notes()) if r.notes() else None
        url_obj = r.URL()
        url_str = str(url_obj.absoluteString()) if url_obj else None
        start, _start_all_day = _components_to_datetime(r.startDateComponents())
        completion = _ns_date_to_datetime(r.completionDate())

        rules = r.recurrenceRules() or []
        recurrence = _recurrence_to_model(rules[0]) if rules else None

        alarms = []
        for a in (r.alarms() or []):
            try:
                alarms.append(_alarm_to_model(a))
            except Exception:
                continue

        location: Optional[LocationSpec] = None
        # Reminder-level structured location (via EKCalendarItem)
        try:
            sloc = r.structuredLocation()
        except Exception:
            sloc = None
        if sloc is not None:
            location = _structured_location_to_model(sloc)
        # If no calendar-item-level location, fall back to first location alarm
        if location is None:
            for a in alarms:
                if a.location is not None:
                    location = a.location
                    break

        children = self._children_of(r)
        subtask_ids = [str(c.calendarItemIdentifier()) for c in children]

        text_hashtags = (
            _extract_text_hashtags(summary.title) + _extract_text_hashtags(notes)
        )
        # de-dup preserving order
        seen: set[str] = set()
        text_hashtags = [
            t for t in text_hashtags if not (t in seen or seen.add(t))
        ]

        tags, tags_reason = self._real_tags_for(summary.id)
        link, link_reason = self._link_for(summary.id)

        return ReminderDetail(
            **summary.model_dump(),
            notes=notes,
            url=url_str,
            start_date=start,
            completion_date=completion,
            recurrence=recurrence,
            alarms=alarms,
            location=location,
            subtask_ids=subtask_ids,
            tags=tags,
            tags_unavailable_reason=tags_reason,
            text_hashtags=text_hashtags,
            link=link,
            link_unavailable_reason=link_reason,
            creation_date=_ns_date_to_datetime(r.creationDate()),
            modification_date=_ns_date_to_datetime(r.lastModifiedDate()),
        )

    # Real tags --------------------------------------------------------

    def _real_tags_for(
        self, reminder_id: str
    ) -> tuple[Optional[list[str]], Optional[str]]:
        """Read one reminder's real tags.

        Returns (tags, None) on success or (None, reason) when the local
        store is unreadable. The distinction matters: an empty list means
        "this reminder has no tags", while None means "we could not tell" —
        collapsing the two is what let a fabricated tag list look
        authoritative for weeks.
        """
        try:
            return self._tags.tags_for_reminder(reminder_id), None
        except TagStoreError as exc:
            logger.warning("Real tags unavailable: %s", exc)
            return None, str(exc)

    def list_tags(self) -> list[TagInfo]:
        """Every real tag across every account, with usage counts."""
        return self._tags.list_tags()

    def tags_available(self) -> bool:
        return self._tags.available()

    def tags_unavailable_reason(self) -> Optional[str]:
        """Why real-tag reads would fail, or None when they work."""
        return self._tags.unavailable_reason()

    def tag_writes_unavailable_reason(self) -> Optional[str]:
        """Why real-tag writes would fail, or None when they work.

        Separate from the read path: reading uses the store file, writing
        uses ReminderKit, and either can be unavailable on its own.
        """
        return self._tag_writer.unavailable_reason()

    def _detail_after_tag_write(
        self, reminder_id: str, tags: list[str]
    ) -> ReminderDetail:
        """Rebuild a reminder's detail after ReminderKit changed its tags.

        `tags` comes from the writer's own read-back rather than from the
        store file, which remindd may not have flushed yet. EventKit is
        refreshed first, since the save also bumped the modification date.
        """
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        detail = self._to_detail(r)
        detail.tags = tags
        detail.tags_unavailable_reason = None
        return detail

    # Linked content -----------------------------------------------------

    def _link_for(
        self, reminder_id: str
    ) -> tuple[Optional[LinkedContent], Optional[str]]:
        """Read one reminder's linked content (the Mail / Messages chip).

        Returns (link, None) on success — link being None when the reminder
        simply has none — or (None, reason) when the store is unreadable.
        """
        try:
            return self._links.link_for_reminder(reminder_id), None
        except LinkStoreError as exc:
            logger.warning("Linked content unavailable: %s", exc)
            return None, str(exc)

    def link_writes_unavailable_reason(self) -> Optional[str]:
        """Why linked-content writes would fail, or None when they work."""
        return self._link_writer.unavailable_reason()

    def _detail_after_link_write(
        self, reminder_id: str, link: Optional[LinkedContent]
    ) -> ReminderDetail:
        """Rebuild a reminder's detail after ReminderKit changed its link.

        `link` comes from the writer's own read-back, for the same reason
        _detail_after_tag_write trusts the writer over the store file.
        """
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        detail = self._to_detail(r)
        detail.link = link
        detail.link_unavailable_reason = None
        return detail

    def set_link(
        self, reminder_id: str, link: str, title: Optional[str] = None
    ) -> ReminderDetail:
        """Attach linked content, replacing any existing chip."""
        result = self._link_writer.set_link(reminder_id, link, title)
        return self._detail_after_link_write(reminder_id, result)

    def clear_link(self, reminder_id: str) -> ReminderDetail:
        """Remove linked content. No-op when there is none."""
        self._link_writer.clear_link(reminder_id)
        return self._detail_after_link_write(reminder_id, None)

    def add_tags(self, reminder_id: str, tags: list[str]) -> ReminderDetail:
        """Add real tags to a reminder. Already-present tags are skipped."""
        result = self._tag_writer.add_tags(reminder_id, tags)
        return self._detail_after_tag_write(reminder_id, result)

    def remove_tags(self, reminder_id: str, tags: list[str]) -> ReminderDetail:
        """Remove real tags from a reminder. Absent tags are ignored."""
        result = self._tag_writer.remove_tags(reminder_id, tags)
        return self._detail_after_tag_write(reminder_id, result)

    def set_tags(self, reminder_id: str, tags: list[str]) -> ReminderDetail:
        """Replace a reminder's real tags. An empty list clears them."""
        result = self._tag_writer.set_tags(reminder_id, tags)
        return self._detail_after_tag_write(reminder_id, result)

    # Subtask helpers --------------------------------------------------

    def _parent_of(self, r: Any) -> Optional[Any]:
        try:
            p = r.parentReminder() if hasattr(r, "parentReminder") else None
        except Exception:
            p = None
        return p

    def _children_of_fast(self, r: Any) -> Optional[list[Any]]:
        """Fast path: returns children if EKReminder.subreminders() is available.

        Returns None if no fast-path API is exposed (caller should treat as
        unknown rather than empty), or [] / [child, ...] otherwise.
        """
        if hasattr(r, "subreminders"):
            try:
                kids = r.subreminders()
                return list(kids) if kids else []
            except Exception:
                return []
        return None

    def _children_of(self, r: Any) -> list[Any]:
        """Comprehensive children lookup. Use sparingly — falls back to a full fetch."""
        fast = self._children_of_fast(r)
        if fast is not None:
            return fast
        # Fallback: scan the same list for items whose parentReminder == r.
        cal = r.calendar()
        if cal is None:
            return []
        pred = self._store.predicateForRemindersInCalendars_([cal])
        try:
            rows = self._fetch(pred)
        except Exception:
            return []
        target_id = str(r.calendarItemIdentifier())
        kids = []
        for x in rows:
            try:
                p = x.parentReminder() if hasattr(x, "parentReminder") else None
            except Exception:
                p = None
            if p is not None and str(p.calendarItemIdentifier()) == target_id:
                kids.append(x)
        return kids

    # Public read methods ---------------------------------------------

    def list_reminders(
        self,
        list_ids: Optional[list[str]] = None,
        completed: Optional[bool] = None,
        priority: Optional[int] = None,
        due_before: Optional[datetime] = None,
        due_after: Optional[datetime] = None,
        has_subtasks: Optional[bool] = None,
        text: Optional[str] = None,
        tags: Optional[list[str]] = None,
        match_all_tags: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[ReminderSummary]]:
        if priority is not None and priority not in _PRIORITY_VALID:
            raise ValueError(f"priority must be one of {sorted(_PRIORITY_VALID)}")
        # Resolve the tag filter before any EventKit work: if the local
        # store is unreadable we must fail loudly, never quietly return
        # every reminder as though none were filtered out.
        tag_ids: Optional[set[str]] = None
        if tags:
            try:
                tag_ids = self._tags.reminders_with_tags(
                    tags, match_all=match_all_tags
                )
            except TagStoreError as exc:
                raise RuntimeError(
                    f"Cannot filter by tag: {exc}"
                ) from exc
            if not tag_ids:
                return 0, []
        self._refresh_eventkit()
        lists = self._resolve_lists(list_ids)
        pred = self._build_predicate(lists, completed, due_before, due_after)
        rows = self._fetch(pred)
        rows = self._post_filter(
            rows, text, priority, due_before, due_after, completed, has_subtasks
        )
        if tag_ids is not None:
            rows = [
                r for r in rows
                if str(r.calendarItemIdentifier()).upper() in tag_ids
            ]
        # Stable sort: incomplete first, then by due date (None last), then title.
        def sort_key(r: Any) -> tuple[int, float, str]:
            done = 1 if r.isCompleted() else 0
            d, _ = _components_to_datetime(r.dueDateComponents())
            ts = d.timestamp() if d else float("inf")
            return (done, ts, str(r.title() or ""))
        rows.sort(key=sort_key)

        total = len(rows)
        page = rows[offset : offset + limit]
        # Cache for subsequent get_reminder calls
        for r in page:
            self._reminders_by_id[str(r.calendarItemIdentifier())] = r
        return total, [self._to_summary(r) for r in page]

    def search_reminders(
        self,
        query: str,
        list_ids: Optional[list[str]] = None,
        completed: Optional[bool] = None,
        priority: Optional[int] = None,
        due_before: Optional[datetime] = None,
        due_after: Optional[datetime] = None,
        tags: Optional[list[str]] = None,
        match_all_tags: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[ReminderSummary]]:
        return self.list_reminders(
            list_ids=list_ids,
            completed=completed,
            priority=priority,
            due_before=due_before,
            due_after=due_after,
            text=query,
            tags=tags,
            match_all_tags=match_all_tags,
            limit=limit,
            offset=offset,
        )

    def get_reminder(self, reminder_id: str) -> Optional[ReminderDetail]:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            return None
        return self._to_detail(r)

    def list_subtasks(self, reminder_id: str) -> list[ReminderSummary]:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        return [self._to_summary(c) for c in self._children_of(r)]

    def get_stats(self) -> RemindersStats:
        self._refresh_eventkit()
        all_lists = list(self._lists_by_id.values())
        if not all_lists:
            return RemindersStats(
                list_count=0, total=0, incomplete=0, completed=0, overdue=0, due_today=0,
            )
        all_pred = self._store.predicateForRemindersInCalendars_(all_lists)
        rows = self._fetch(all_pred)
        total = len(rows)
        completed = sum(1 for r in rows if r.isCompleted())
        incomplete = total - completed

        now = datetime.now().astimezone()
        today_start = datetime.combine(now.date(), time.min).astimezone()
        today_end = datetime.combine(now.date(), time.max).astimezone()
        overdue = 0
        due_today = 0
        for r in rows:
            if r.isCompleted():
                continue
            d, _ = _components_to_datetime(r.dueDateComponents())
            if d is None:
                continue
            if d < now:
                overdue += 1
            if today_start <= d <= today_end:
                due_today += 1
        return RemindersStats(
            list_count=len(all_lists),
            total=total,
            incomplete=incomplete,
            completed=completed,
            overdue=overdue,
            due_today=due_today,
        )

    # ------------------------------------------------------------------
    # Reminders — write
    # ------------------------------------------------------------------

    def _lookup_reminder(self, reminder_id: str) -> Optional[Any]:
        # Only trust the cache within a call chain; _refresh_eventkit()
        # clears it, so a fresh read never sees a stale snapshot.
        cached = self._reminders_by_id.get(reminder_id)
        if cached is not None:
            return cached
        # EKEventStore.calendarItemWithIdentifier_ returns the item directly.
        item = self._store.calendarItemWithIdentifier_(reminder_id)
        if item is None:
            return None
        self._reminders_by_id[reminder_id] = item
        return item

    def create_reminder(
        self,
        title: str,
        list_id: Optional[str] = None,
        notes: Optional[str] = None,
        url: Optional[str] = None,
        due_date: Optional[datetime] = None,
        is_all_day: bool = False,
        start_date: Optional[datetime] = None,
        priority: Priority = 0,
        recurrence: Optional[RecurrenceRule] = None,
        alarms: Optional[list[AlarmSpec]] = None,
        location: Optional[LocationSpec] = None,
        parent_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        link: Optional[str] = None,
        link_title: Optional[str] = None,
    ) -> ReminderDetail:
        if not title or not title.strip():
            raise ValueError("Reminder title must be a non-empty string.")
        if link is not None and link.strip():
            # Fail on a malformed link *before* creating anything: this is
            # the one link error that is the caller's, not the machine's.
            parse_link(link, link_title)
        if priority not in _PRIORITY_VALID:
            raise ValueError(f"priority must be one of {sorted(_PRIORITY_VALID)}")

        # Refresh before allocating, not after: reset() invalidates every
        # EKObject, so the new reminder (and a parent looked up below) must
        # come from the store as it stands after the refresh.
        self._refresh_eventkit()

        r = EKReminder.reminderWithEventStore_(self._store)
        r.setTitle_(title.strip())

        cal: Any
        if list_id:
            cal = self._get_list(list_id)
        else:
            cal = self._store.defaultCalendarForNewReminders()
            if cal is None:
                raise RuntimeError("No default reminders list available.")
        r.setCalendar_(cal)

        if notes is not None:
            r.setNotes_(notes)
        if url is not None:
            ns_url = NSURL.URLWithString_(url)
            if ns_url is not None:
                r.setURL_(ns_url)
        if priority:
            r.setPriority_(int(priority))

        if due_date is not None:
            r.setDueDateComponents_(_datetime_to_components(due_date, is_all_day))
        if start_date is not None:
            r.setStartDateComponents_(_datetime_to_components(start_date, is_all_day))

        if recurrence is not None:
            r.setRecurrenceRules_([_model_to_recurrence(recurrence)])

        if alarms:
            for spec in alarms:
                r.addAlarm_(_model_to_alarm(spec))

        if location is not None and not alarms:
            # Attach location as a calendar-item-level structured location too,
            # but EventKit only stores it via alarms reliably — so add a 0-offset
            # location alarm to ensure persistence.
            loc_spec = AlarmSpec(kind="location", location=location, proximity="enter")
            r.addAlarm_(_model_to_alarm(loc_spec))

        if parent_id:
            parent = self._lookup_reminder(parent_id)
            if parent is None:
                raise ValueError(f"Parent reminder not found: {parent_id}")
            if hasattr(r, "setParentReminder_"):
                try:
                    r.setParentReminder_(parent)
                except Exception as exc:
                    logger.warning("setParentReminder_ failed: %s", exc)

        ok, err = self._store.saveReminder_commit_error_(r, True, None)
        if not ok:
            raise RuntimeError(f"Could not save reminder: {_nserror_str(err)}")

        ident = str(r.calendarItemIdentifier())
        self._reminders_by_id[ident] = r

        # The reminder exists at this point. Tags and linked content are
        # further, independent writes through ReminderKit, so if either
        # fails the reminder still stands — say so in the detail rather
        # than raising, which would imply nothing was created.
        tags_applied: Optional[list[str]] = None
        tags_reason: Optional[str] = None
        if tags:
            try:
                tags_applied = self._tag_writer.add_tags(ident, tags)
            except (TagWriteError, ValueError) as exc:
                logger.warning("Reminder %s created but not tagged: %s",
                               ident, exc)
                tags_reason = (
                    f"Reminder was created, but its tags were not applied: "
                    f"{exc}"
                )

        link_applied: Optional[LinkedContent] = None
        link_reason: Optional[str] = None
        if link is not None and link.strip():
            try:
                link_applied = self._link_writer.set_link(ident, link, link_title)
            except (LinkWriteError, ValueError) as exc:
                logger.warning("Reminder %s created but not linked: %s",
                               ident, exc)
                link_reason = (
                    f"Reminder was created, but its linked content was not "
                    f"applied: {exc}"
                )

        if tags_applied is None and link_applied is None:
            detail = self._to_detail(r)
        else:
            # A ReminderKit save bumped the modification date; rebuild
            # from a refreshed EventKit so the detail reflects it.
            self._refresh_eventkit()
            fresh = self._lookup_reminder(ident)
            detail = self._to_detail(fresh if fresh is not None else r)
        if tags_applied is not None:
            detail.tags = tags_applied
            detail.tags_unavailable_reason = None
        if tags_reason is not None:
            detail.tags_unavailable_reason = tags_reason
        if link_applied is not None:
            detail.link = link_applied
            detail.link_unavailable_reason = None
        if link_reason is not None:
            detail.link_unavailable_reason = link_reason
        return detail

    def update_reminder(
        self,
        reminder_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        url: Optional[str] = None,
        list_id: Optional[str] = None,
        due_date: Optional[datetime] = None,
        is_all_day: Optional[bool] = None,
        start_date: Optional[datetime] = None,
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
    ) -> ReminderDetail:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")

        if title is not None:
            if not title.strip():
                raise ValueError("Reminder title must be a non-empty string.")
            r.setTitle_(title.strip())
        if clear_notes:
            r.setNotes_(None)
        elif notes is not None:
            r.setNotes_(notes)
        if clear_url:
            r.setURL_(None)
        elif url is not None:
            ns_url = NSURL.URLWithString_(url)
            if ns_url is not None:
                r.setURL_(ns_url)
        if list_id is not None:
            r.setCalendar_(self._get_list(list_id))
        if priority is not None:
            if priority not in _PRIORITY_VALID:
                raise ValueError(f"priority must be one of {sorted(_PRIORITY_VALID)}")
            r.setPriority_(int(priority))

        if clear_due_date:
            r.setDueDateComponents_(None)
        elif due_date is not None:
            all_day = bool(is_all_day) if is_all_day is not None else False
            r.setDueDateComponents_(_datetime_to_components(due_date, all_day))

        if clear_start_date:
            r.setStartDateComponents_(None)
        elif start_date is not None:
            all_day = bool(is_all_day) if is_all_day is not None else False
            r.setStartDateComponents_(_datetime_to_components(start_date, all_day))

        if clear_recurrence:
            r.setRecurrenceRules_(None)
        elif recurrence is not None:
            r.setRecurrenceRules_([_model_to_recurrence(recurrence)])

        if clear_alarms:
            for a in list(r.alarms() or []):
                r.removeAlarm_(a)
        elif alarms is not None:
            for a in list(r.alarms() or []):
                r.removeAlarm_(a)
            for spec in alarms:
                r.addAlarm_(_model_to_alarm(spec))

        if clear_location:
            # Drop any 0-offset location alarms.
            for a in list(r.alarms() or []):
                if a.structuredLocation() is not None and a.relativeOffset() == 0 and a.absoluteDate() is None:
                    r.removeAlarm_(a)
        elif location is not None:
            r.addAlarm_(_model_to_alarm(AlarmSpec(kind="location", location=location, proximity="enter")))

        ok, err = self._store.saveReminder_commit_error_(r, True, None)
        if not ok:
            raise RuntimeError(f"Could not update reminder: {_nserror_str(err)}")
        return self._to_detail(r)

    def complete_reminder(
        self,
        reminder_id: str,
        completed: Optional[bool] = None,
    ) -> ReminderDetail:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        target = (not bool(r.isCompleted())) if completed is None else bool(completed)
        r.setCompleted_(target)
        if target and r.completionDate() is None:
            r.setCompletionDate_(NSDate.date())
        if not target:
            r.setCompletionDate_(None)
        ok, err = self._store.saveReminder_commit_error_(r, True, None)
        if not ok:
            raise RuntimeError(f"Could not change completion: {_nserror_str(err)}")
        return self._to_detail(r)

    def delete_reminder(
        self,
        reminder_id: str,
        cascade: bool = True,
    ) -> DeleteResult:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        deleted_subs = 0
        if cascade:
            for c in self._children_of(r):
                ok, _ = self._store.removeReminder_commit_error_(c, True, None)
                if ok:
                    deleted_subs += 1
                    self._reminders_by_id.pop(str(c.calendarItemIdentifier()), None)
        ok, err = self._store.removeReminder_commit_error_(r, True, None)
        if not ok:
            raise RuntimeError(f"Could not delete reminder: {_nserror_str(err)}")
        self._reminders_by_id.pop(reminder_id, None)
        return DeleteResult(id=reminder_id, success=True, deleted_subtask_count=deleted_subs)

    def get_reminder_link(self, reminder_id: str) -> str:
        self._refresh_eventkit()
        r = self._lookup_reminder(reminder_id)
        if r is None:
            raise ValueError(f"Reminder not found: {reminder_id}")
        return _make_reminder_link(reminder_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nserror_str(err: Any) -> str:
    if err is None:
        return "unknown error"
    try:
        return str(err.localizedDescription())
    except Exception:
        return repr(err)
