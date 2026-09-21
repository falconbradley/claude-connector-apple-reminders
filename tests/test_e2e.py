"""
End-to-end tests for the Apple Reminders MCP connector.

Tests are split into two groups:

  Group A — Static tests (no Reminders permission required)
    Pydantic model shapes, input validation, hashtag parsing.
    Always run.

  Group B — Live EventKit tests (requires Reminders access)
    Operate against a dedicated test list named ``__claude_mcp_test__``,
    which is created at setup and torn down at the end. Skipped with a
    clear message if Reminders access has not been granted.

Usage:
    uv run python tests/test_e2e.py
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

# Ensure src/ is on sys.path so the package imports cleanly when run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"

_registry: list[tuple[str, str, Callable]] = []   # (group, name, fn)
_results: list[tuple[str, str, str, str]] = []    # (status, group, name, detail)


def test(group: str, name: str):
    def decorator(fn):
        _registry.append((group, name, fn))
        return fn
    return decorator


class _SkipTest(Exception):
    """Raised by a test when a required fixture is unavailable."""


def skip(msg: str):
    raise _SkipTest(msg)


def run_all(skip_live: bool = False) -> None:
    for group, name, fn in _registry:
        if skip_live and group == "B":
            _results.append((_SKIP, group, name, "live tests skipped"))
            continue
        try:
            fn()
            _results.append((_PASS, group, name, ""))
        except _SkipTest as exc:
            _results.append((_SKIP, group, name, str(exc)))
        except AssertionError as exc:
            _results.append((_FAIL, group, name, str(exc)))
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            _results.append((_FAIL, group, name, f"{type(exc).__name__}: {exc}\n{tb}"))


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"Expected {b!r}, got {a!r}" + (f" — {msg}" if msg else ""))


def is_in(v, c, msg=""):
    if v not in c:
        raise AssertionError(f"{v!r} not in {c!r}" + (f" — {msg}" if msg else ""))


def truthy(v, msg=""):
    if not v:
        raise AssertionError(f"Expected truthy, got {v!r}" + (f" — {msg}" if msg else ""))


def not_none(v, msg=""):
    if v is None:
        raise AssertionError("Expected non-None" + (f" — {msg}" if msg else ""))


# ---------------------------------------------------------------------------
# Group A — Static (model + input validation + helper)
# ---------------------------------------------------------------------------

@test("A", "model shapes")
def t_model_shapes():
    from apple_reminders_mcp.models import (
        AlarmSpec,
        DeleteResult,
        ListResult,
        LocationSpec,
        RecurrenceRule,
        ReminderDetail,
        ReminderList,
        ReminderResult,
        ReminderSummary,
        RemindersStats,
        SearchResult,
    )

    rl = ReminderList(id="abc", title="Errands", source_name="iCloud", source_type="calDAV")
    eq(rl.allows_modification, True)

    rs = ReminderSummary(
        id="x", list_id="l", list_title="Errands", title="Buy milk",
    )
    eq(rs.priority, 0)
    eq(rs.completed, False)
    eq(rs.is_all_day, False)

    rd = ReminderDetail(**rs.model_dump())
    eq(rd.tags, [])
    eq(rd.alarms, [])

    stats = RemindersStats(list_count=1, total=2, incomplete=2, completed=0, overdue=0, due_today=1)
    eq(stats.due_today, 1)

    sr = SearchResult(total=0, offset=0, limit=10, reminders=[])
    eq(len(sr.reminders), 0)

    lr = ListResult(success=True)
    eq(lr.list, None)

    rr = ReminderResult(reminder=rd, success=True)
    truthy(rr.success)

    dr = DeleteResult(id="y", success=True, deleted_subtask_count=2)
    eq(dr.deleted_subtask_count, 2)


@test("A", "recurrence validation")
def t_recurrence_validation():
    from apple_reminders_mcp.models import RecurrenceRule

    r = RecurrenceRule(frequency="daily")
    eq(r.interval, 1)

    r = RecurrenceRule(frequency="weekly", interval=2, days_of_week=["monday", "wednesday"])
    eq(len(r.days_of_week or []), 2)

    try:
        RecurrenceRule(frequency="hourly")  # type: ignore[arg-type]
    except Exception as exc:
        # Pydantic Literal validation should reject this.
        is_in("hourly", str(exc))
    else:
        raise AssertionError("RecurrenceRule should reject unknown frequency")

    try:
        RecurrenceRule(frequency="daily", interval=0)
    except Exception:
        pass
    else:
        raise AssertionError("RecurrenceRule should reject interval=0")


@test("A", "alarm validation")
def t_alarm_validation():
    from apple_reminders_mcp.models import AlarmSpec, LocationSpec

    a1 = AlarmSpec(kind="relative", relative_offset_seconds=-3600.0)
    eq(a1.kind, "relative")

    a2 = AlarmSpec(kind="absolute", absolute_date=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc))
    not_none(a2.absolute_date)

    a3 = AlarmSpec(
        kind="location",
        location=LocationSpec(title="Home", latitude=37.0, longitude=-122.0, radius_meters=100),
        proximity="enter",
    )
    eq(a3.proximity, "enter")

    try:
        AlarmSpec(kind="relative", proximity="zigzag")  # type: ignore[arg-type]
    except Exception:
        pass
    else:
        raise AssertionError("AlarmSpec should reject unknown proximity")


@test("A", "hashtag parsing")
def t_hashtag_parsing():
    from apple_reminders_mcp.reminders import _extract_tags

    eq(_extract_tags(None), [])
    eq(_extract_tags(""), [])
    eq(_extract_tags("Buy milk"), [])
    eq(_extract_tags("Buy milk #shopping"), ["shopping"])
    eq(_extract_tags("#Work meeting #urgent #q2-plan"), ["Work", "urgent", "q2-plan"])
    # Don't pick up email-style # in URLs/strings (no leading word boundary)
    eq(_extract_tags("see foo#bar later"), [])


@test("A", "reminder link helper")
def t_reminder_link():
    from apple_reminders_mcp.reminders import _make_reminder_link
    link = _make_reminder_link("12345-abcd-EFGH/?")
    is_in("x-apple-reminderkit://REMCDReminder/", link)
    # Special chars should be percent-encoded.
    is_in("%3F", link)


@test("A", "frequency mapping")
def t_frequency_mapping():
    from apple_reminders_mcp.reminders import _EK_TO_FREQ, _FREQ_TO_EK

    for k, v in _FREQ_TO_EK.items():
        eq(_EK_TO_FREQ[v], k)

    eq(_FREQ_TO_EK["daily"], 0)
    eq(_FREQ_TO_EK["weekly"], 1)
    eq(_FREQ_TO_EK["monthly"], 2)
    eq(_FREQ_TO_EK["yearly"], 3)


@test("A", "iso parse helper")
def t_iso_parse():
    from apple_reminders_mcp.server import _parse_iso

    eq(_parse_iso(None, "x"), None)
    not_none(_parse_iso("2026-05-04T10:00:00Z", "x"))
    not_none(_parse_iso("2026-05-04T10:00:00+00:00", "x"))
    try:
        _parse_iso("not-a-date", "x")
    except ValueError:
        pass
    else:
        raise AssertionError("_parse_iso should reject malformed input")


@test("A", "EventKit predicate selectors exist")
def t_predicate_selectors_exist():
    """Guard against typos in the Objective-C selectors we call by name.

    PyObjC resolves selectors lazily, so a misspelled one only blows up when
    the matching code path is exercised against a live store. Assert up front
    that every selector _build_predicate reaches for is really there — note
    Apple's asymmetry: "Incomplete" but "Completed".
    """
    try:
        import EventKit  # noqa: F401
    except ImportError as exc:
        skip(f"PyObjC/EventKit not available: {exc}")

    for selector in (
        "predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_",
        "predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_",
        "predicateForRemindersInCalendars_",
        "fetchRemindersMatchingPredicate_completion_",
    ):
        truthy(
            hasattr(EventKit.EKEventStore, selector),
            f"EKEventStore has no selector {selector}",
        )

    # And that the module actually calls those names, not lookalikes.
    from pathlib import Path as _Path
    src = (_Path(__file__).resolve().parent.parent
           / "src" / "apple_reminders_mcp" / "reminders.py").read_text()
    for bad in ("predicateForCompleteRemindersWith", "predicateForCompleteReminders:"):
        if bad in src:
            raise AssertionError(
                f"reminders.py references non-existent selector prefix {bad!r} "
                "— the real one is predicateForCompletedReminders..."
            )


# ---------------------------------------------------------------------------
# Group B — Live EventKit
# ---------------------------------------------------------------------------

TEST_LIST_TITLE = "__claude_mcp_test__"

_store = None
_store_skip_reason: Optional[str] = None
_test_list_id: Optional[str] = None
_created_reminder_ids: list[str] = []


def _store_or_skip():
    """Return the shared RemindersStore, or skip.

    The failure reason is cached: EventKit refuses to hand out more than a
    handful of EKEventStore instances per process ("too many EKEventStore
    instances"), so retrying the constructor once per test both wastes the
    quota and buries the real first error.
    """
    global _store, _store_skip_reason
    if _store is not None:
        return _store
    if _store_skip_reason is not None:
        skip(_store_skip_reason)
    try:
        from apple_reminders_mcp.permissions import PermissionDeniedError
        from apple_reminders_mcp.reminders import RemindersStore
    except ImportError as exc:
        _store_skip_reason = f"PyObjC/EventKit not available: {exc}"
        skip(_store_skip_reason)
    try:
        _store = RemindersStore()
    except PermissionDeniedError as exc:
        _store_skip_reason = f"Reminders access not granted: {exc}"
        skip(_store_skip_reason)
    except Exception as exc:
        _store_skip_reason = f"Could not init RemindersStore: {exc}"
        skip(_store_skip_reason)
    return _store


def _ensure_test_list():
    global _test_list_id
    if _test_list_id is not None:
        return _test_list_id
    store = _store_or_skip()
    # Reuse existing test list if one happened to be left around.
    for lst in store.list_lists():
        if lst.title == TEST_LIST_TITLE:
            _test_list_id = lst.id
            return _test_list_id
    res = store.create_list(title=TEST_LIST_TITLE)
    _test_list_id = res.list.id  # type: ignore[union-attr]
    return _test_list_id


def _track(reminder_id: str) -> str:
    _created_reminder_ids.append(reminder_id)
    return reminder_id


@test("B", "list lists includes test list")
def t_list_lists():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    lists = store.list_lists()
    truthy(any(l.id == list_id for l in lists))


@test("B", "create + get + update + delete reminder")
def t_crud_reminder():
    store = _store_or_skip()
    list_id = _ensure_test_list()

    detail = store.create_reminder(
        title="Buy milk #shopping",
        list_id=list_id,
        notes="Whole milk preferred",
        priority=1,
    )
    rid = _track(detail.id)
    eq(detail.priority, 1)
    is_in("shopping", detail.tags)

    fetched = store.get_reminder(rid)
    not_none(fetched)
    eq(fetched.title, "Buy milk #shopping")  # type: ignore[union-attr]

    updated = store.update_reminder(rid, title="Buy oat milk", priority=5)
    eq(updated.title, "Buy oat milk")
    eq(updated.priority, 5)

    res = store.delete_reminder(rid)
    truthy(res.success)
    _created_reminder_ids.remove(rid)


@test("B", "complete and uncomplete")
def t_complete():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    detail = store.create_reminder(title="Take out trash", list_id=list_id)
    rid = _track(detail.id)
    completed = store.complete_reminder(rid, completed=True)
    eq(completed.completed, True)
    not_none(completed.completion_date)
    uncompleted = store.complete_reminder(rid, completed=False)
    eq(uncompleted.completed, False)


@test("B", "due date round-trip (timed)")
def t_due_date_timed():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    when = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0, second=0)
    detail = store.create_reminder(
        title="Timed reminder", list_id=list_id, due_date=when, is_all_day=False,
    )
    _track(detail.id)
    not_none(detail.due_date)
    eq(detail.is_all_day, False)


@test("B", "due date round-trip (all-day)")
def t_due_date_all_day():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    when = (datetime.now().astimezone() + timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    detail = store.create_reminder(
        title="All-day reminder", list_id=list_id, due_date=when, is_all_day=True,
    )
    _track(detail.id)
    not_none(detail.due_date)
    eq(detail.is_all_day, True)


@test("B", "recurrence round-trip")
def t_recurrence():
    from apple_reminders_mcp.models import RecurrenceRule

    store = _store_or_skip()
    list_id = _ensure_test_list()
    rule = RecurrenceRule(frequency="weekly", interval=2, days_of_week=["monday", "friday"])
    when = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0)
    detail = store.create_reminder(
        title="Recurring meeting", list_id=list_id, due_date=when, recurrence=rule,
    )
    _track(detail.id)
    not_none(detail.recurrence)
    eq(detail.recurrence.frequency, "weekly")  # type: ignore[union-attr]
    eq(detail.recurrence.interval, 2)          # type: ignore[union-attr]


@test("B", "alarm round-trip")
def t_alarm():
    from apple_reminders_mcp.models import AlarmSpec

    store = _store_or_skip()
    list_id = _ensure_test_list()
    when = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0)
    detail = store.create_reminder(
        title="Reminder with alarm",
        list_id=list_id,
        due_date=when,
        alarms=[AlarmSpec(kind="relative", relative_offset_seconds=-1800.0)],
    )
    _track(detail.id)
    truthy(len(detail.alarms) >= 1)
    eq(detail.alarms[0].kind, "relative")


@test("B", "subtasks: parent + child + cascade delete")
def t_subtasks():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    parent = store.create_reminder(title="Pack for trip", list_id=list_id)
    _track(parent.id)
    child = store.create_reminder(
        title="Pack toothbrush", list_id=list_id, parent_id=parent.id,
    )
    _track(child.id)

    children = store.list_subtasks(parent.id)
    if not children:
        # parentReminder is best-effort on some macOS releases; don't hard-fail.
        skip("subreminders not surfaced by EventKit on this macOS version")

    # Cascade delete should clean up the child too.
    res = store.delete_reminder(parent.id, cascade=True)
    truthy(res.success)
    if parent.id in _created_reminder_ids:
        _created_reminder_ids.remove(parent.id)
    if child.id in _created_reminder_ids:
        _created_reminder_ids.remove(child.id)


@test("B", "search by text + filters")
def t_search():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    a = store.create_reminder(title="Find me alpha bravo", list_id=list_id)
    _track(a.id)
    b = store.create_reminder(title="Unrelated charlie", list_id=list_id)
    _track(b.id)

    total, rows = store.search_reminders(query="bravo", list_ids=[list_id], limit=10)
    truthy(total >= 1)
    truthy(any(r.id == a.id for r in rows))
    truthy(not any(r.id == b.id for r in rows))


@test("B", "stats are sane")
def t_stats():
    store = _store_or_skip()
    s = store.get_stats()
    truthy(s.list_count >= 1)
    truthy(s.total >= s.completed)
    truthy(s.total == s.completed + s.incomplete)


@test("B", "reminder link round-trip")
def t_reminder_link_live():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    detail = store.create_reminder(title="Link target", list_id=list_id)
    _track(detail.id)
    link = store.get_reminder_link(detail.id)
    is_in("x-apple-reminderkit://REMCDReminder/", link)


@test("B", "completed filter sweep (list + search)")
def t_completed_filter_sweep():
    """Exercise every `completed` value against a real store.

    Regression guard for the crash where completed=True hit a misspelled
    EventKit selector: completed=False and completed=None took different
    predicate branches and passed, so the bug only surfaced here.
    """
    store = _store_or_skip()
    list_id = _ensure_test_list()

    marker = "zzsweep"
    open_item = store.create_reminder(title=f"{marker} still open", list_id=list_id)
    _track(open_item.id)
    done_item = store.create_reminder(title=f"{marker} already done", list_id=list_id)
    _track(done_item.id)
    store.complete_reminder(done_item.id, completed=True)

    def ids(rows):
        return {r.id for r in rows}

    # --- list_reminders ---------------------------------------------------
    _, rows = store.list_reminders(list_ids=[list_id], completed=True, limit=200)
    got = ids(rows)
    is_in(done_item.id, got, "completed=True must return the completed reminder")
    truthy(open_item.id not in got, "completed=True must not return open reminders")
    truthy(all(r.completed for r in rows), "completed=True returned an open row")

    _, rows = store.list_reminders(list_ids=[list_id], completed=False, limit=200)
    got = ids(rows)
    is_in(open_item.id, got, "completed=False must return the open reminder")
    truthy(done_item.id not in got, "completed=False must not return completed reminders")
    truthy(not any(r.completed for r in rows), "completed=False returned a completed row")

    _, rows = store.list_reminders(list_ids=[list_id], completed=None, limit=200)
    got = ids(rows)
    is_in(open_item.id, got, "completed=None must return open reminders")
    is_in(done_item.id, got, "completed=None must return completed reminders")

    # --- search_reminders (same parameter, shared code path) --------------
    _, rows = store.search_reminders(
        query=marker, list_ids=[list_id], completed=True, limit=200
    )
    got = ids(rows)
    is_in(done_item.id, got, "search completed=True must return the completed reminder")
    truthy(open_item.id not in got, "search completed=True must not return open reminders")

    _, rows = store.search_reminders(
        query=marker, list_ids=[list_id], completed=False, limit=200
    )
    got = ids(rows)
    is_in(open_item.id, got, "search completed=False must return the open reminder")
    truthy(done_item.id not in got, "search completed=False must not return completed reminders")

    _, rows = store.search_reminders(
        query=marker, list_ids=[list_id], completed=None, limit=200
    )
    got = ids(rows)
    is_in(open_item.id, got, "search completed=None must return open reminders")
    is_in(done_item.id, got, "search completed=None must return completed reminders")


@test("B", "completed filter combined with due-date bounds")
def t_completed_with_due_bounds():
    """completed=True + due_before/due_after must filter by DUE date.

    The EventKit predicate for completed reminders bounds by *completion*
    date; feeding due-date bounds into it would silently drop rows completed
    outside the window. The item below is completed now but due in the past,
    so it must survive a past due window and vanish from a future one.
    """
    store = _store_or_skip()
    list_id = _ensure_test_list()

    past_due = (datetime.now().astimezone() - timedelta(days=3)).replace(
        microsecond=0, second=0
    )
    item = store.create_reminder(
        title="zzbounds overdue but finished", list_id=list_id, due_date=past_due
    )
    _track(item.id)
    store.complete_reminder(item.id, completed=True)

    _, rows = store.list_reminders(
        list_ids=[list_id],
        completed=True,
        due_before=past_due + timedelta(days=1),
        limit=200,
    )
    is_in(item.id, {r.id for r in rows}, "due_before window should include the item")

    _, rows = store.list_reminders(
        list_ids=[list_id],
        completed=True,
        due_after=datetime.now().astimezone() + timedelta(days=1),
        limit=200,
    )
    truthy(
        item.id not in {r.id for r in rows},
        "due_after window in the future should exclude a past-due item",
    )


# ---------------------------------------------------------------------------
# Tear-down
# ---------------------------------------------------------------------------

def _cleanup() -> None:
    """Best-effort: delete created reminders and the test list."""
    if _store is None:
        return
    for rid in list(_created_reminder_ids):
        try:
            _store.delete_reminder(rid, cascade=True)
        except Exception:
            pass
    if _test_list_id is not None:
        try:
            _store.delete_list(_test_list_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

def _print_report() -> int:
    counts = {_PASS: 0, _FAIL: 0, _SKIP: 0}
    for status, _, _, _ in _results:
        counts[status] += 1

    print("")
    print("=" * 60)
    print(f"Apple Reminders MCP — test report")
    print("=" * 60)

    cur_group = None
    for status, group, name, detail in _results:
        if group != cur_group:
            cur_group = group
            label = "Static" if group == "A" else "Live (EventKit)"
            print(f"\n[Group {group}] {label}")
        marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}[status]
        line = f"  {marker} {name}"
        if status != _PASS and detail:
            line += f"\n      {detail.splitlines()[0]}"
        print(line)

    print("")
    print(f"  {counts[_PASS]} passed  {counts[_FAIL]} failed  {counts[_SKIP]} skipped")
    print("")
    return 1 if counts[_FAIL] else 0


def main() -> int:
    skip_live = "--skip-live" in sys.argv
    try:
        run_all(skip_live=skip_live)
    finally:
        _cleanup()
    return _print_report()


if __name__ == "__main__":
    sys.exit(main())
