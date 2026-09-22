"""
End-to-end tests for the Apple Reminders MCP connector.

Tests are split into two groups:

  Group A — Static tests (no Reminders permission required)
    Pydantic model shapes, input validation, hashtag parsing, the
    tag-store reader against a synthetic fixture store, link parsing,
    and the linked-content decoder against archives built the way
    Reminders builds them.
    Always run.

  Group C — Live local Reminders store (requires Full Disk Access)
    Reads the real Reminders Core Data store to check real-tag reading,
    the real-tag / text-hashtag split, and that every linked-content
    chip in the store decodes. Needs no Reminders TCC grant, only file
    access. Skipped with a clear message when unreadable.

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
        if skip_live and group in ("B", "C"):
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
    # `tags` defaults to None, NOT []. None means "real tags could not be
    # read"; [] means "this reminder genuinely has none". Collapsing the
    # two is what let a fabricated tag list pass for authoritative.
    eq(rd.tags, None)
    eq(rd.tags_unavailable_reason, None)
    eq(rd.text_hashtags, [])
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


@test("A", "text hashtag scraping")
def t_hashtag_parsing():
    """The text scraper, which finds text and nothing more.

    Renamed from _extract_tags deliberately: it never read Apple tags,
    and the old name is what made its output look authoritative.
    """
    from apple_reminders_mcp.reminders import _extract_text_hashtags as ex

    eq(ex(None), [])
    eq(ex(""), [])
    eq(ex("Buy milk"), [])
    eq(ex("Buy milk #shopping"), ["shopping"])
    eq(ex("#Work meeting #urgent #q2-plan"), ["Work", "urgent", "q2-plan"])
    # Don't pick up email-style # in URLs/strings (no leading word boundary)
    eq(ex("see foo#bar later"), [])

    # The old name must stay gone, so nothing can quietly go on calling it.
    import apple_reminders_mcp.reminders as rem
    truthy(
        not hasattr(rem, "_extract_tags"),
        "_extract_tags still exists — callers could keep treating scraped "
        "text as real tags",
    )


@test("A", "scraped hashtags never populate `tags`")
def t_scrape_not_in_tags():
    """Source guard for the bug this feature fixes.

    _to_detail must fill `tags` from the tag store and `text_hashtags`
    from the scraper — never the other way round.
    """
    src = (ROOT / "src" / "apple_reminders_mcp" / "reminders.py").read_text()
    detail = src.split("def _to_detail(")[1].split("\n    def ")[0]

    import re

    is_in("text_hashtags=text_hashtags", detail)
    # Word-boundary match: "text_hashtags=..." must not count as "tags=...".
    truthy(
        re.search(r"(?<![\w_])tags=tags\b", detail),
        "`tags` is not populated from the real tag store",
    )
    truthy(
        not re.search(r"(?<![\w_])tags=text_hashtags\b", detail),
        "`tags` is being populated from the text scrape",
    )
    truthy(
        "_real_tags_for" in detail,
        "_to_detail does not consult the real tag store",
    )


@test("A", "TagInfo shape")
def t_taginfo_shape():
    from apple_reminders_mcp.models import TagInfo

    t = TagInfo(name="autoreview")
    eq(t.reminder_count, 0)
    eq(t.reminder_ids, [])
    t2 = TagInfo(name="Buy", reminder_count=2, reminder_ids=["A", "B"])
    eq(t2.reminder_count, 2)


@test("A", "version strings agree")
def t_version_agreement():
    """manifest.json, pyproject.toml and __version__ must match.

    The package version is what the server reports to the MCP client over
    the wire, so a stale one makes an installed build lie about itself —
    it sat at 0.1.2 through two releases before anyone noticed.
    """
    import json
    import re

    manifest = json.loads((ROOT / "manifest.json").read_text())["version"]
    pyproject = re.search(
        r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M
    ).group(1)
    from apple_reminders_mcp import __version__

    eq(pyproject, manifest, "pyproject.toml disagrees with manifest.json")
    eq(__version__, manifest, "__init__.py disagrees with manifest.json")


@test("A", "every manifest tool exists on the server")
def t_manifest_matches_tools():
    """A tool listed in the manifest but missing from server.py (or the
    reverse) means the installed extension advertises a surface it does
    not have."""
    import json

    from apple_reminders_mcp import server

    listed = {t["name"] for t in json.loads((ROOT / "manifest.json").read_text())["tools"]}
    for name in listed:
        truthy(
            hasattr(server, name),
            f"manifest lists {name} but server.py has no such tool",
        )
    for name in ("add_reminder_tags", "remove_reminder_tags",
                 "set_reminder_tags", "list_tags",
                 "set_reminder_link", "clear_reminder_link"):
        is_in(name, listed, f"{name} is not advertised in manifest.json")


@test("A", "tool schema advertises optional params by type")
def t_optional_params_typed():
    """Every optional argument must carry a top-level `type`.

    Pydantic spells `Optional[str] = None` as `anyOf: [string, null]`, and
    Claude Desktop keeps only a property's top-level `type` and `default`
    when it hands the schema to the model. That shape therefore reached
    the model as a bare `{"default": null}`, and a digits-only label such
    as an SMS shortcode was sent as a JSON integer and refused.
    """
    import asyncio
    import json

    from apple_reminders_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    link_props = tools["set_reminder_link"].input_schema["properties"]
    eq(link_props["title"].get("type"), "string", "set_reminder_link.title has no type")
    eq(link_props["title"].get("default"), None)
    eq(link_props["link"].get("type"), "string")
    create_props = tools["create_reminder"].input_schema["properties"]
    for name in ("link_title", "notes", "url", "list_id", "due_date", "parent_id"):
        eq(create_props[name].get("type"), "string", f"create_reminder.{name} has no type")
    eq(create_props["alarms"].get("type"), "array")
    eq(create_props["tags"].get("type"), "array")
    # The pattern is fixed once, for every tool.
    for name, tool in tools.items():
        truthy("anyOf" not in json.dumps(tool.input_schema),
               f"{name} still advertises a nullable parameter as anyOf")


@test("A", "digits-only free text is accepted as a string")
def t_numeric_text_coerced():
    """A client that ignores the schema may still send 42878 as an integer.
    Free-text fields store its digits; a boolean is not text and stays
    refused."""
    from pydantic import ValidationError

    from apple_reminders_mcp import server

    def validate(tool: str, args: dict) -> dict:
        meta = server.mcp._tool_manager.get_tool(tool).fn_metadata
        return meta.arg_model.model_validate(meta.pre_parse_json(args)).model_dump()

    got = validate("set_reminder_link",
                   {"reminder_id": "r", "link": "any;-;42878", "title": 42878})
    eq(got["title"], "42878")
    got = validate("create_reminder",
                   {"title": 2026, "notes": 12.5, "link": "any;-;42878", "link_title": 42878})
    eq(got["title"], "2026")
    eq(got["notes"], "12.5")
    eq(got["link_title"], "42878")
    got = validate("update_reminder", {"reminder_id": "r", "title": 7, "notes": 8})
    eq((got["title"], got["notes"]), ("7", "8"))
    eq(validate("search_reminders", {"query": 42878})["query"], "42878")
    eq(validate("list_reminders", {"text": 42878})["text"], "42878")
    eq(validate("create_reminder_list", {"title": 2026})["title"], "2026")
    # A string that happens to be digits is stored verbatim, quotes and all
    # would have been the workaround; there must be none to strip.
    eq(validate("set_reminder_link",
                {"reminder_id": "r", "link": "any;-;42878", "title": "42878"})["title"],
       "42878")
    # Explicit null is still "no label".
    eq(validate("set_reminder_link",
                {"reminder_id": "r", "link": "any;-;42878", "title": None})["title"], None)
    for tool, args in (("set_reminder_link", {"reminder_id": "r", "link": "l", "title": True}),
                       ("create_reminder", {"title": False})):
        try:
            validate(tool, args)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{tool} accepted a boolean as text")


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
        # Used by _refresh_eventkit to stop the store serving stale
        # snapshots of reminders edited in Reminders.app.
        "refreshSourcesIfNecessary",
        "reset",
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
# Tag store — synthetic fixture
# ---------------------------------------------------------------------------

def _build_fixture_store(path: Path, hashtag_ent: int, fk_col: str) -> None:
    """Write a miniature Reminders store with the real schema shape.

    Only the columns the reader touches are recreated, but the awkward
    parts are faithful: the hashtag entity lives in the shared wide
    ZREMCDOBJECT table, its Z_ENT is parameterised (it really does differ
    between the iCloud stores and Data-local.sqlite on one Mac), and the
    reminder foreign key is one of several same-named ZREMINDER* columns.
    """
    import sqlite3

    con = sqlite3.connect(path)
    fk_cols = ", ".join(
        f"{c} INTEGER" for c in ("ZREMINDER", "ZREMINDER1", "ZREMINDER2",
                                 "ZREMINDER3", "ZREMINDER4", "ZREMINDER5")
    )
    con.executescript(
        f"""
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        CREATE TABLE ZREMCDHASHTAGLABEL (
            Z_PK INTEGER PRIMARY KEY, ZNAME VARCHAR, ZCANONICALNAME VARCHAR
        );
        CREATE TABLE ZREMCDOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZMARKEDFORDELETION INTEGER,
            ZHASHTAGLABEL INTEGER, {fk_cols}
        );
        CREATE TABLE ZREMCDREMINDER (
            Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZMARKEDFORDELETION INTEGER,
            ZDACALENDARITEMUNIQUEIDENTIFIER VARCHAR
        );
        """
    )
    con.execute(
        "INSERT INTO Z_PRIMARYKEY VALUES (?,?,0,0)", (hashtag_ent, "REMCDHashtag")
    )
    # A decoy entity sharing ZREMCDOBJECT, to prove we filter on Z_ENT.
    con.execute(
        "INSERT INTO Z_PRIMARYKEY VALUES (?,?,0,0)", (hashtag_ent + 5, "REMCDAlarm")
    )
    con.executemany(
        "INSERT INTO ZREMCDHASHTAGLABEL VALUES (?,?,?)",
        [(1, "Buy", "buy"), (2, "autoreview", "autoreview"), (3, "Unused", "unused")],
    )
    con.executemany(
        "INSERT INTO ZREMCDREMINDER VALUES (?,?,?,?)",
        [
            (10, "Install dishwasher", 0, FIX_A),
            (11, "Call NovoCare", 0, FIX_B),
            (12, "Plain reminder, no tags", 0, FIX_C),
            (13, "Deleted but still tagged", 1, FIX_DELETED),
        ],
    )
    rows = [
        # (pk, ent, deleted, label, reminder)
        (100, hashtag_ent, 0, 1, 10),            # Buy       -> A
        (101, hashtag_ent, 0, 2, 11),            # autoreview-> B
        (102, hashtag_ent, 0, 1, 11),            # Buy       -> B (two tags)
        (103, hashtag_ent, 1, 2, 10),            # soft-deleted application
        (104, hashtag_ent, 1, None, None),       # orphan, as seen in the wild
        (105, hashtag_ent, 0, 1, 13),            # on a deleted reminder
        (106, hashtag_ent + 5, 0, 1, 12),        # different entity entirely
    ]
    for pk, ent, deleted, label, rem in rows:
        cols = ["Z_PK", "Z_ENT", "ZMARKEDFORDELETION", "ZHASHTAGLABEL", fk_col]
        con.execute(
            f"INSERT INTO ZREMCDOBJECT ({', '.join(cols)}) VALUES (?,?,?,?,?)",
            (pk, ent, deleted, label, rem),
        )
    con.commit()
    con.close()


FIX_A = "AAAAAAAA-0000-0000-0000-000000000001"
FIX_B = "BBBBBBBB-0000-0000-0000-000000000002"
FIX_C = "CCCCCCCC-0000-0000-0000-000000000003"
FIX_DELETED = "DDDDDDDD-0000-0000-0000-000000000004"


@test("A", "reads refresh the EventKit store first")
def t_reads_refresh():
    """Guard against the stale-read bug returning.

    EKEventStore hands out object snapshots and caches them, so a
    long-lived MCP process will happily re-serve a reminder it fetched
    minutes ago — which is how an in-app tag edit came back with an
    unchanged modification_date. Every public entry point that reads or
    mutates a reminder must refresh before it looks anything up.
    """
    import ast

    src = (ROOT / "src" / "apple_reminders_mcp" / "reminders.py").read_text()

    # The cache must never be populated without a way to clear it.
    is_in("def _refresh_eventkit", src)
    is_in("self._reminders_by_id.clear()", src)

    tree = ast.parse(src)
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RemindersStore"
    )
    bodies = {
        n.name: ast.get_source_segment(src, n)
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
    }
    for name in (
        "get_reminder", "list_subtasks", "get_stats", "list_reminders",
        "create_reminder", "update_reminder", "complete_reminder",
        "delete_reminder",
    ):
        body = bodies.get(name)
        not_none(body, f"could not find {name} in reminders.py")
        truthy(
            "self._refresh_eventkit()" in body,
            f"{name} does not refresh before reading — it can serve a "
            "stale snapshot",
        )

    # create_reminder must refresh BEFORE it allocates, since reset()
    # invalidates every EKObject including one just created.
    create = bodies["create_reminder"]
    truthy(
        create.index("self._refresh_eventkit()")
        < create.index("EKReminder.reminderWithEventStore_"),
        "create_reminder refreshes after allocating, which would "
        "invalidate the new reminder",
    )


@test("A", "tag store reads a fixture store")
def t_tagstore_fixture():
    import tempfile

    from apple_reminders_mcp.tagstore import RemindersTagStore

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # Two accounts with *different* entity ids and FK columns, which is
        # the real situation: Z_ENT for REMCDHashtag was 32 in the iCloud
        # stores and 33 in Data-local.sqlite on the same machine.
        _build_fixture_store(d / "Data-ACCOUNT1.sqlite", 32, "ZREMINDER3")
        _build_fixture_store(d / "Data-local.sqlite", 33, "ZREMINDER1")

        ts = RemindersTagStore(stores_dir=d)
        truthy(ts.available(), "fixture store should be readable")
        eq(ts.unavailable_reason(), None)
        eq(len(ts.store_paths()), 2)

        eq(ts.tags_for_reminder(FIX_A), ["Buy"])
        # Sorted, case-insensitively.
        eq(ts.tags_for_reminder(FIX_B), ["autoreview", "Buy"])
        # A reminder with no tags reads as [] — definitely not None.
        eq(ts.tags_for_reminder(FIX_C), [])
        # Soft-deleted reminders are excluded.
        eq(ts.tags_for_reminder(FIX_DELETED), [])
        # Ids are matched case-insensitively.
        eq(ts.tags_for_reminder(FIX_A.lower()), ["Buy"])

        # A soft-deleted tag application must not resurface (row 103 put
        # `autoreview` on reminder A).
        truthy(
            "autoreview" not in ts.tags_for_reminder(FIX_A),
            "soft-deleted tag application leaked into results",
        )
        # And a row belonging to a different entity must not be read as a
        # tag (row 106 points at reminder C).
        eq(ts.tags_for_reminder(FIX_C), [])


@test("A", "tag store list_tags and filtering")
def t_tagstore_list_and_filter():
    import tempfile

    from apple_reminders_mcp.tagstore import RemindersTagStore

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _build_fixture_store(d / "Data-ACCOUNT1.sqlite", 32, "ZREMINDER3")
        ts = RemindersTagStore(stores_dir=d)

        tags = {t.name: t for t in ts.list_tags()}
        eq(set(tags), {"Buy", "autoreview", "Unused"})
        eq(tags["Buy"].reminder_count, 2)          # reminders A and B
        eq(tags["autoreview"].reminder_count, 1)   # reminder B
        # A tag that exists but is applied to nothing is still a tag.
        eq(tags["Unused"].reminder_count, 0)
        eq(tags["Unused"].reminder_ids, [])
        eq(sorted(tags["Buy"].reminder_ids), sorted([FIX_A, FIX_B]))
        # Most-used first.
        eq([t.name for t in ts.list_tags()][0], "Buy")

        # Matching is case-insensitive and the "#" sigil is optional.
        eq(ts.reminders_with_tags(["autoreview"]), {FIX_B})
        eq(ts.reminders_with_tags(["#AUTOREVIEW"]), {FIX_B})
        eq(ts.reminders_with_tags(["  #Autoreview "]), {FIX_B})
        eq(ts.reminders_with_tags(["nonexistent"]), set())
        eq(ts.reminders_with_tags([]), set())
        eq(ts.reminders_with_tags([""]), set())

        # any-of vs all-of
        eq(ts.reminders_with_tags(["buy", "autoreview"]), {FIX_A, FIX_B})
        eq(
            ts.reminders_with_tags(["buy", "autoreview"], match_all=True),
            {FIX_B},
        )


@test("A", "tag store reports an unreadable store instead of empty tags")
def t_tagstore_missing():
    import tempfile

    from apple_reminders_mcp.tagstore import RemindersTagStore, TagStoreError

    with tempfile.TemporaryDirectory() as tmp:
        ts = RemindersTagStore(stores_dir=Path(tmp) / "nope")
        truthy(not ts.available())
        reason = ts.unavailable_reason()
        not_none(reason, "an unreadable store must explain itself")
        is_in("Full Disk Access", reason)
        # The failure must be loud, not an empty list that reads as "no tags".
        try:
            ts.tags_for_reminder(FIX_A)
        except TagStoreError:
            pass
        else:
            raise AssertionError(
                "tags_for_reminder returned instead of raising on an "
                "unreadable store — callers would read that as 'no tags'"
            )


@test("A", "tag store never opens the real store for writing")
def t_tagstore_read_only():
    """The store belongs to Reminders.app. We only ever read it."""
    src = (ROOT / "src" / "apple_reminders_mcp" / "tagstore.py").read_text()
    is_in("mode=ro", src)
    is_in("PRAGMA query_only", src)
    for forbidden in ("INSERT ", "UPDATE ", "DELETE FROM", "DROP ", "mode=rw"):
        truthy(
            forbidden not in src,
            f"tagstore.py contains {forbidden!r} — it must never write",
        )


@test("A", "tag name normalisation")
def t_tag_name_normalise():
    from apple_reminders_mcp.tagwriter import normalise_tag_name as n

    eq(n("autoreview"), "autoreview")
    eq(n("#autoreview"), "autoreview")
    eq(n("  #Work "), "Work")
    eq(n("##double"), "double")
    eq(n(""), "")
    eq(n("   "), "")


@test("A", "tag writer declares what it needs before using it")
def t_tag_writer_capabilities():
    """A macOS update must break loudly, not half-apply a write.

    The writer drives private ReminderKit, so every class and selector it
    calls is listed up front and checked before anything is mutated.
    """
    import ast

    from apple_reminders_mcp import tagwriter

    src = (ROOT / "src" / "apple_reminders_mcp" / "tagwriter.py").read_text()

    # Every selector the module actually calls on a ReminderKit object
    # must appear in the declared capability table.
    declared = {
        f"{cls}.{sel}"
        for cls, sels in tagwriter._REQUIRED.items()
        for sel in sels
    }
    flat = {sel for sels in tagwriter._REQUIRED.values() for sel in sels}
    for selector in (
        "fetchReminderWithDACalendarItemUniqueIdentifier_inList_error_",
        "updateReminder_",
        "saveSynchronouslyWithError_",
        "addHashtagWithType_name_",
        "removeHashtag_",
        "hashtagContext",
    ):
        is_in(selector, flat, f"{selector} is called but not declared")
    truthy(declared, "capability table is empty")

    # The check must run before any mutation can happen: _apply goes
    # through _rem_store, which loads and verifies.
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RemindersTagWriter"
    )
    apply_fn = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_apply"
    )
    body = ast.get_source_segment(src, apply_fn)
    truthy(
        body.index("_rem_store()") < body.index("addHashtagWithType_name_"),
        "_apply mutates before the capability check has run",
    )


@test("A", "tag writer never falls back to writing the store file")
def t_tag_writer_no_sqlite():
    """Hand-editing the Core Data store would corrupt CloudKit sync."""
    src = (ROOT / "src" / "apple_reminders_mcp" / "tagwriter.py").read_text()
    for forbidden in ("sqlite3", "INSERT ", "UPDATE ", "DELETE FROM"):
        truthy(
            forbidden not in src,
            f"tagwriter.py references {forbidden!r} — writes must go "
            "through ReminderKit, never the store file",
        )


@test("A", "tag write tools refuse cleanly when unavailable")
def t_tag_write_guard():
    """An unavailable write path must raise, never silently no-op."""
    from apple_reminders_mcp import server

    class _Store:
        def tag_writes_unavailable_reason(self):
            return "ReminderKit did not load"

    try:
        server._require_tag_writes(_Store())
    except RuntimeError as exc:
        is_in("ReminderKit did not load", str(exc))
    else:
        raise AssertionError("_require_tag_writes did not raise")

    class _OkStore:
        def tag_writes_unavailable_reason(self):
            return None

    server._require_tag_writes(_OkStore())  # must not raise


@test("A", "tag writes are a separate capability from tag reads")
def t_tag_read_write_separate():
    """Reads use the store file; writes use ReminderKit.

    Conflating them would mean a machine without Full Disk Access
    reports that it cannot write either, which is false.
    """
    import ast

    src = (ROOT / "src" / "apple_reminders_mcp" / "reminders.py").read_text()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RemindersStore"
    )
    fns = {
        n.name: ast.get_source_segment(src, n)
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
    }
    for name in ("tags_unavailable_reason", "tag_writes_unavailable_reason"):
        not_none(fns.get(name), f"{name} is missing")
    is_in("self._tags.", fns["tags_unavailable_reason"])
    is_in("self._tag_writer.", fns["tag_writes_unavailable_reason"])

    # A failed tag write during create must not claim the reminder failed.
    create = fns["create_reminder"]
    is_in("tags_unavailable_reason", create)
    truthy(
        "raise" not in create.split("if tags:")[1].split("return self._to_detail(r)")[0],
        "a failed tag write raises out of create_reminder, which would "
        "imply the reminder was not created",
    )


@test("A", "linked content model shape")
def t_link_model_shape():
    from apple_reminders_mcp.models import LinkedContent, ReminderDetail

    rd = ReminderDetail(id="x", list_id="l", list_title="Errands", title="t")
    # Same convention as tags: None + no reason == "no link", not "unknown".
    eq(rd.link, None)
    eq(rd.link_unavailable_reason, None)
    lc = LinkedContent(kind="mail", url="message:%3Ca@b%3E")
    eq(lc.title, None)
    eq(lc.activity_type, None)
    try:
        LinkedContent(kind="carrier-pigeon", url="x")
    except Exception:
        pass
    else:
        raise AssertionError("LinkedContent accepted an unknown kind")


@test("A", "link parsing canonicalises Mail, Messages, and web links")
def t_link_parsing():
    from apple_reminders_mcp.linkwriter import parse_link

    # Every spelling of a Mail link lands on Apple's own: message:%3C…%3E
    apple = "message:%3Cabc+d_e=f@host.example%3E"
    for form in (
        "message:%3Cabc+d_e=f@host.example%3E",           # Apple's spelling
        "message://%3Cabc+d_e=f%40host.example%3E",       # Mail connector's mail_link
        "message://<abc+d_e=f@host.example>",
        "message:<abc+d_e=f@host.example>",
        "<abc+d_e=f@host.example>",
        "  message://<abc+d_e=f@host.example>  ",
    ):
        spec = parse_link(form)
        eq(spec.kind, "mail", form)
        eq(spec.url, apple, form)
        eq(spec.title, None, "Mail links carry no title")

    # Messages: chat guids as the Messages connector reports them.
    spec = parse_link("any;+;chat656905820623768966", "Arcadia Survivors")
    eq(spec.kind, "messages")
    eq(spec.url, "messages://open?groupid=chat656905820623768966")
    eq(spec.title, "Arcadia Survivors")
    spec = parse_link("iMessage;-;+15551234567")
    eq(spec.url, "messages://open?addresses=+15551234567")
    eq(spec.title, "+15551234567", "1:1 title defaults to the handle")
    eq(parse_link("SMS;-;+15551234567").url, "messages://open?addresses=+15551234567")
    eq(parse_link("any;-;someone@example.com").url,
       "messages://open?addresses=someone@example.com")
    eq(parse_link("chat123").url, "messages://open?groupid=chat123")
    eq(parse_link("+15551234567").url, "messages://open?addresses=+15551234567")
    eq(parse_link("imessage://+15551234567").url, "messages://open?addresses=+15551234567")
    eq(parse_link("messages://open?groupid=chat1").url, "messages://open?groupid=chat1")

    spec = parse_link("https://example.com/a?b=c", "ignored?")
    eq(spec.kind, "web")
    eq(spec.url, "https://example.com/a?b=c")

    for bad in ("", "   ", "hello", "mailto:a@b.c", "brad@example.com", "ftp://x"):
        try:
            parse_link(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_link accepted {bad!r}")


@test("A", "linked-content decoder reads Apple's archive shapes")
def t_link_decoder():
    """Two on-disk shapes, both seen in real stores on 2026-09-21.

    Apple archives `type` / `storage` / `flags` straight into $top; the
    storage is raw bytes on some rows and an NSMutableData object on
    others. The user-activity form nests a second keyed archive.
    """
    import plistlib

    from apple_reminders_mcp.linkstore import decode_user_activity

    # Universal link, raw-bytes storage, fields in $top (rows 105/108/3615).
    raw = plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"type": 1, "storage": plistlib.UID(1)},
        "$objects": ["$null", b"message:%3Cabc@host.example%3E"],
    }, fmt=plistlib.FMT_BINARY)
    link = decode_user_activity(raw)
    not_none(link)
    eq(link.kind, "mail")
    eq(link.url, "message:%3Cabc@host.example%3E")
    eq(link.title, None)

    # Universal link, NSMutableData storage (row 2080), plus flags.
    wrapped = plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"type": 1, "storage": plistlib.UID(1), "flags": 0},
        "$objects": [
            "$null",
            {"$class": plistlib.UID(2), "NS.data": b"https://example.com/x"},
            {"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]},
        ],
    }, fmt=plistlib.FMT_BINARY)
    eq(decode_user_activity(wrapped).kind, "web")
    eq(decode_user_activity(wrapped).url, "https://example.com/x")

    # NSUserActivity form (row 4935): a nested keyed archive.
    inner = plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            {"$class": plistlib.UID(5), "activityType": plistlib.UID(2),
             "title": plistlib.UID(3), "targetContentIdentifier": plistlib.UID(4),
             "type": 1, "version": 1},
            "com.apple.Messages",
            "Arcadia Survivors",
            "messages://open?groupid=chat656905820623768966",
            {"$classname": "UAUserActivityInfo", "$classes": ["UAUserActivityInfo", "NSObject"]},
        ],
    }, fmt=plistlib.FMT_BINARY)
    outer = plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"type": 2, "storage": plistlib.UID(1), "flags": 0},
        "$objects": ["$null", inner],
    }, fmt=plistlib.FMT_BINARY)
    link = decode_user_activity(outer)
    not_none(link)
    eq(link.kind, "messages")
    eq(link.url, "messages://open?groupid=chat656905820623768966")
    eq(link.title, "Arcadia Survivors")
    eq(link.activity_type, "com.apple.Messages")

    # XML-format archive (row 2080): references are {"CF$UID": n} dicts.
    # (plistlib's XML writer has no UID support, so spell them as Apple does.)
    xml = plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"type": 1, "storage": {"CF$UID": 1}},
        "$objects": [
            "$null",
            {"$class": {"CF$UID": 2}, "NS.data": b"message:%3Cxml@host.example%3E"},
            {"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]},
        ],
    }, fmt=plistlib.FMT_XML)
    is_in(b"CF$UID", xml, "fixture did not produce an XML-style reference")
    link = decode_user_activity(xml)
    not_none(link, "XML-format archive did not decode")
    eq(link.url, "message:%3Cxml@host.example%3E")

    # Garbage must read as "no link", never raise.
    eq(decode_user_activity(None), None)
    eq(decode_user_activity(b""), None)
    eq(decode_user_activity(b"not a plist"), None)


@test("A", "link writer's own archive decodes like Apple's")
def t_link_writer_archive_matches():
    """Build the activities the writer would save and decode them with the
    store reader — the two halves must agree before any live write."""
    from apple_reminders_mcp.linkstore import decode_user_activity
    from apple_reminders_mcp.linkwriter import RemindersLinkWriter, parse_link

    w = RemindersLinkWriter()
    reason = w.unavailable_reason()
    if reason is not None and "does not appear to provide" in reason:
        skip(f"ReminderKit unavailable on this macOS: {reason}")
    eq(reason, None, "ReminderKit no longer matches what the link writer calls")

    from Foundation import NSKeyedArchiver

    def archived(spec):
        activity = w._make_activity(spec)
        data, err = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(
            activity, True, None
        )
        not_none(data, f"could not archive {spec}: {err}")
        return activity, bytes(data)

    activity, data = archived(parse_link("message://%3Ca%40b.example%3E"))
    eq(int(activity.type()), 1, "a Mail link must be a universal-link activity")
    link = decode_user_activity(data)
    eq(link.kind, "mail")
    eq(link.url, "message:%3Ca@b.example%3E")
    eq(w._linked_from_activity(activity), link, "writer and reader disagree")

    activity, data = archived(parse_link("any;+;chat42", "Family"))
    eq(int(activity.type()), 2, "a Messages link must wrap an NSUserActivity")
    link = decode_user_activity(data)
    eq(link.kind, "messages")
    eq(link.url, "messages://open?groupid=chat42")
    eq(link.title, "Family")
    eq(link.activity_type, "com.apple.Messages")
    eq(w._linked_from_activity(activity), link, "writer and reader disagree")


@test("A", "link writer declares what it needs before using it")
def t_link_writer_capabilities():
    import ast

    from apple_reminders_mcp import linkwriter

    src = (ROOT / "src" / "apple_reminders_mcp" / "linkwriter.py").read_text()
    for cls, sels in linkwriter._REQUIRED.items():
        truthy(cls.startswith("REM"), f"{cls} is not a ReminderKit class")
        for sel in sels:
            is_in(sel, src, f"{cls}.{sel} declared but never called")
    flat = {sel for sels in linkwriter._REQUIRED.values() for sel in sels}
    for needed in (
        "initWithUniversalLink_", "initWithUserActivity_",
        "setUserActivity_", "userActivity", "updateReminder_",
        "saveSynchronouslyWithError_",
    ):
        is_in(needed, flat, f"{needed} is used but not declared in _REQUIRED")

    tree = ast.parse(src)
    cls_node = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RemindersLinkWriter"
    )
    body = ast.get_source_segment(src, cls_node)
    truthy(
        body.index("_rem_store()") < body.index("updateReminder_"),
        "the store (and so the capability check) must be reached before a write",
    )


@test("A", "link writer never falls back to writing the store file")
def t_link_writer_no_sqlite():
    src = (ROOT / "src" / "apple_reminders_mcp" / "linkwriter.py").read_text()
    for forbidden in ("sqlite3", "UPDATE ", "INSERT ", "ZUSERACTIVITY ="):
        truthy(
            forbidden not in src,
            f"linkwriter.py references {forbidden!r} — writes must go "
            "through ReminderKit only",
        )
    src = (ROOT / "src" / "apple_reminders_mcp" / "linkstore.py").read_text()
    for forbidden in ("INSERT", "UPDATE", "DELETE", "mode=rw", "mode=rwc"):
        truthy(forbidden not in src, f"linkstore.py contains {forbidden!r} — it must never write")
    is_in("mode=ro", src)
    is_in("query_only", src)


@test("A", "link write tools refuse cleanly when unavailable")
def t_link_write_guard():
    from apple_reminders_mcp import server

    class _Store:
        def link_writes_unavailable_reason(self):
            return "ReminderKit moved"

    try:
        server._require_link_writes(_Store())
    except RuntimeError as exc:
        is_in("ReminderKit moved", str(exc))
    else:
        raise AssertionError("_require_link_writes did not raise")

    class _OkStore:
        def link_writes_unavailable_reason(self):
            return None

    server._require_link_writes(_OkStore())


@test("A", "link reads and writes are separate; create survives a failed link")
def t_link_read_write_separate():
    import ast

    src = (ROOT / "src" / "apple_reminders_mcp" / "reminders.py").read_text()
    tree = ast.parse(src)
    cls_node = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RemindersStore"
    )
    fns = {
        n.name: ast.get_source_segment(src, n)
        for n in cls_node.body if isinstance(n, ast.FunctionDef)
    }
    is_in("self._links.", fns["_link_for"])
    is_in("self._link_writer.", fns["link_writes_unavailable_reason"])
    is_in("_link_for", fns["_to_detail"], "_to_detail does not consult the link store")

    create = fns["create_reminder"]
    is_in("link_unavailable_reason", create)
    # A malformed link is the caller's error and must fail before creation…
    truthy(create.index("parse_link(") < create.index("saveReminder_commit_error_"))
    # …but once the reminder exists, a failed *write* must not claim the
    # reminder failed. (EventKit's own save failure, just above, may raise.)
    after_save = create.split("ident = str(r.calendarItemIdentifier())")[1]
    truthy(
        "raise" not in after_save.split("return detail")[0],
        "a failed link write raises out of create_reminder, which would "
        "imply the reminder was not created",
    )


@test("A", "tag writer loads ReminderKit on this machine")
def t_tag_writer_loads():
    """Not a live write — just that the private API still matches.

    This runs without Reminders permission, so it is the earliest place a
    macOS update that renames a selector will show up.
    """
    from apple_reminders_mcp.tagwriter import RemindersTagWriter

    w = RemindersTagWriter()
    reason = w.unavailable_reason()
    if reason is not None and "does not appear to provide" in reason:
        skip(f"ReminderKit unavailable on this macOS: {reason}")
    eq(reason, None, "ReminderKit no longer matches what the writer calls")
    truthy(w.available())


class _FakeSource:
    """Stand-in for EKSource: a title, a type, and its reminder calendars."""

    def __init__(self, ident, title, source_type, reminder_calendars=0):
        self._ident = ident
        self._title = title
        self._type = source_type
        self._cals = [object()] * reminder_calendars

    def sourceIdentifier(self):
        return self._ident

    def title(self):
        return self._title

    def sourceType(self):
        return self._type

    def calendarsForEntityType_(self, entity_type):
        return self._cals


class _FakeCalendar:
    def __init__(self, source):
        self._source = source

    def source(self):
        return self._source


class _FakeEventStore:
    def __init__(self, sources, default_source=None):
        self._sources = sources
        self._default = _FakeCalendar(default_source) if default_source else None

    def sources(self):
        return self._sources

    def defaultCalendarForNewReminders(self):
        return self._default


def _store_with(sources, default_source=None):
    """A RemindersStore wired to a fake EKEventStore, with no TCC prompt."""
    from apple_reminders_mcp.reminders import RemindersStore

    store = object.__new__(RemindersStore)
    store._store = _FakeEventStore(sources, default_source)
    store._access_granted = True
    return store


def _brads_sources():
    """The topology that broke create_reminder_list on 2026-09-21.

    Two calDAV accounts: iCloud, which holds all six reminder lists, and a
    calendar-only account that sorts ahead of it. The old code picked the
    first calDAV source it saw and EventKit rejected the save with "That
    account does not support reminders."
    """
    from EventKit import EKSourceTypeCalDAV, EKSourceTypeLocal

    calendar_only = _FakeSource("s1", "Work Calendar", EKSourceTypeCalDAV, 0)
    icloud = _FakeSource("s2", "iCloud", EKSourceTypeCalDAV, 6)
    local = _FakeSource("s3", "On My Mac", EKSourceTypeLocal, 0)
    return calendar_only, icloud, local


@test("A", "new-list source skips accounts that cannot hold reminders")
def t_list_source_skips_incapable():
    calendar_only, icloud, local = _brads_sources()
    store = _store_with([calendar_only, icloud, local])

    chosen = store._choose_list_source(None)
    eq(str(chosen.title()), "iCloud", "must not pick the calendar-only calDAV account")

    # Ordering must not rescue it: reversing the list still lands on iCloud.
    store = _store_with([local, icloud, calendar_only])
    eq(str(store._choose_list_source(None).title()), "iCloud")


@test("A", "new-list source follows the default reminders list")
def t_list_source_prefers_default():
    calendar_only, icloud, local = _brads_sources()
    local_with_lists = _FakeSource("s3", "On My Mac", local.sourceType(), 2)
    # Both are capable; the account Reminders.app itself writes to wins,
    # even though calDAV outranks local by type.
    store = _store_with(
        [calendar_only, icloud, local_with_lists], default_source=local_with_lists
    )
    eq(str(store._choose_list_source(None).title()), "On My Mac")


@test("A", "explicit source name is validated against reminder support")
def t_list_source_explicit_name():
    calendar_only, icloud, local = _brads_sources()
    store = _store_with([calendar_only, icloud, local])

    eq(str(store._choose_list_source("iCloud").title()), "iCloud")
    eq(str(store._choose_list_source("  icloud ").title()), "iCloud", "case/space insensitive")

    # Naming the calendar-only account fails in Python with a message that
    # names the usable sources, rather than in EventKit with "That account
    # does not support reminders."
    try:
        store._choose_list_source("Work Calendar")
    except ValueError as exc:
        truthy("does not support reminders" in str(exc))
        truthy("'iCloud'" in str(exc), "error should list the usable sources")
    else:
        raise AssertionError("expected ValueError for a calendar-only source")

    try:
        store._choose_list_source("Nope")
    except ValueError as exc:
        truthy("Unknown source" in str(exc))
    else:
        raise AssertionError("expected ValueError for an unknown source")


@test("A", "new-list source falls back when no list exists yet")
def t_list_source_fallback():
    from EventKit import EKSourceTypeCalDAV, EKSourceTypeLocal

    # A machine with no reminder lists at all: nothing vends reminder
    # calendars, so capability cannot be observed. Still pick something
    # writable rather than refusing outright.
    icloud = _FakeSource("s1", "iCloud", EKSourceTypeCalDAV, 0)
    local = _FakeSource("s2", "On My Mac", EKSourceTypeLocal, 0)
    store = _store_with([local, icloud])
    eq(str(store._choose_list_source(None).title()), "iCloud")


# ---------------------------------------------------------------------------
# Group C — Live local Reminders store (needs Full Disk Access, not TCC)
# ---------------------------------------------------------------------------

_real_tagstore = None
_real_tagstore_skip: Optional[str] = None


def _real_tagstore_or_skip():
    global _real_tagstore, _real_tagstore_skip
    if _real_tagstore is not None:
        return _real_tagstore
    if _real_tagstore_skip is not None:
        skip(_real_tagstore_skip)
    from apple_reminders_mcp.tagstore import RemindersTagStore

    ts = RemindersTagStore()
    reason = ts.unavailable_reason()
    if reason is not None:
        _real_tagstore_skip = f"Local Reminders store unreadable: {reason}"
        skip(_real_tagstore_skip)
    _real_tagstore = ts
    return ts


@test("C", "real tags read from the live Reminders store")
def t_real_tags_live():
    ts = _real_tagstore_or_skip()
    tags = ts.list_tags()
    truthy(isinstance(tags, list), "list_tags must return a list")
    for t in tags:
        truthy(bool(t.name), "a tag with no name came back")
        truthy(
            not t.name.startswith("#"),
            f"tag {t.name!r} carries a '#' — names are stored without it",
        )
        eq(len(t.reminder_ids), t.reminder_count)
        # Every id must look like the EventKit calendarItemIdentifier.
        for rid in t.reminder_ids:
            eq(len(rid), 36, f"{rid!r} is not a dashed UUID")
            eq(rid, rid.upper(), f"{rid!r} should be upper-case")

    # Round-trip: each tag's members must report that tag back.
    for t in tags:
        for rid in t.reminder_ids[:5]:
            is_in(
                t.name, ts.tags_for_reminder(rid),
                f"{rid} is listed under {t.name!r} but does not report it",
            )


@test("C", "real tags are independent of '#' text")
def t_real_tags_vs_text():
    """The heart of the bug: text and tags are unrelated.

    A reminder with a real tag need not contain '#' anywhere, and '#text'
    in a title creates no tag. Asserted against whatever the live store
    actually holds, so it stays honest as the data changes.
    """
    ts = _real_tagstore_or_skip()
    from apple_reminders_mcp.reminders import _extract_text_hashtags

    import sqlite3

    titles: dict[str, str] = {}
    for path in ts.store_paths():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            for row in con.execute(
                "SELECT upper(ZDACALENDARITEMUNIQUEIDENTIFIER) AS uuid, "
                "ZTITLE AS title, ZNOTES AS notes FROM ZREMCDREMINDER "
                "WHERE ZDACALENDARITEMUNIQUEIDENTIFIER IS NOT NULL"
            ):
                titles[row["uuid"]] = f"{row['title'] or ''}\n{row['notes'] or ''}"
            con.close()
        except sqlite3.Error as exc:
            skip(f"Could not read reminder titles: {exc}")

    tagged = ts.tags_by_reminder()
    if not tagged:
        skip("No tagged reminders in the live store to compare against")

    # At least one real tag must be invisible to the text scrape — that is
    # the false negative the old code produced.
    false_negatives = [
        rid for rid, names in tagged.items()
        if names and not _extract_text_hashtags(titles.get(rid, ""))
    ]
    truthy(
        false_negatives,
        "expected at least one reminder whose real tags do not appear as "
        "'#text' — if this fails the comparison proves nothing",
    )

    # Conversely, text hashtags that correspond to no real tag are the
    # false positives the old code reported as `tags`.
    false_positives = [
        rid for rid, text in titles.items()
        if _extract_text_hashtags(text)
        and not {h.casefold() for h in _extract_text_hashtags(text)}
        <= {n.casefold() for n in tagged.get(rid, [])}
    ]
    print(
        f"      (live store: {len(tagged)} tagged reminders, "
        f"{len(false_negatives)} invisible to the text scrape, "
        f"{len(false_positives)} '#text' reminders with no matching real tag)"
    )


@test("C", "every linked-content chip in the live store decodes")
def t_real_links_live():
    """Whatever Siri and the share sheet have written on this Mac must
    read back as a Mail, Messages, or other link — never as garbage."""
    from apple_reminders_mcp.linkstore import RemindersLinkStore

    ls = RemindersLinkStore()
    reason = ls.unavailable_reason()
    if reason is not None:
        skip(f"Local Reminders store unreadable: {reason}")

    import sqlite3

    # Distinct *live* reminder ids, not rows: a reminder that moved between
    # accounts lingers, marked for deletion, in the old account's store.
    raw_ids: set[str] = set()
    for path in ls.store_paths():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cols = {r[1] for r in con.execute("PRAGMA table_info(ZREMCDREMINDER)")}
            live = (" AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION = 0)"
                    if "ZMARKEDFORDELETION" in cols else "")
            raw_ids.update(
                row[0] for row in con.execute(
                    "SELECT upper(ZDACALENDARITEMUNIQUEIDENTIFIER) FROM ZREMCDREMINDER "
                    "WHERE ZUSERACTIVITY IS NOT NULL "
                    "AND ZDACALENDARITEMUNIQUEIDENTIFIER IS NOT NULL" + live
                )
            )
            con.close()
        except sqlite3.Error as exc:
            skip(f"Could not count linked reminders: {exc}")

    links = ls.all_links()
    if not links:
        skip("No linked reminders in the live store to decode")
    eq(set(links), raw_ids, "some ZUSERACTIVITY blobs did not decode")
    kinds = {}
    for rid, link in links.items():
        eq(len(rid), 36, f"{rid!r} is not a dashed UUID")
        is_in(link.kind, ("mail", "messages", "web", "other"))
        kinds[link.kind] = kinds.get(link.kind, 0) + 1
        if link.kind == "mail":
            truthy(link.url.startswith("message:"), link.url)
        if link.kind == "messages":
            truthy(link.url.startswith("messages://"), link.url)
            eq(link.activity_type, "com.apple.Messages")
        # Round-trip through the single-reminder path.
        eq(ls.link_for_reminder(rid), link)
        eq(ls.link_for_reminder(rid.lower()), link, "id lookup must be case-insensitive")
    print(f"      (live store: {len(links)} linked reminders — {kinds})")


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
    # "#shopping" is text in the title, not a real Apple tag, so it belongs
    # to text_hashtags and `tags` must stay empty — same split the decoy in
    # "tag filter matches real tags" asserts from the other direction.
    eq(detail.tags, [])
    is_in("shopping", detail.text_hashtags)

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


@test("B", "real tag write round-trip")
def t_tag_write_round_trip():
    """Add / remove / set real tags on a throwaway reminder.

    Needs both halves: Reminders permission for the ReminderKit write,
    and Full Disk Access for the store read that independently confirms
    it. Asserting only against the writer's own return value would prove
    nothing — the whole point is that the tag really lands in Reminders.
    """
    store = _store_or_skip()
    list_id = _ensure_test_list()

    reason = store.tag_writes_unavailable_reason()
    if reason is not None:
        skip(f"Tag writes unavailable: {reason}")

    from apple_reminders_mcp.tagstore import RemindersTagStore

    fresh = RemindersTagStore()
    if fresh.unavailable_reason() is not None:
        skip("Full Disk Access needed to verify tag writes independently")

    item = store.create_reminder(title="zztagwrite subject", list_id=list_id)
    _track(item.id)
    eq(item.tags, [], "a new reminder should start with no tags")

    def from_store():
        # A fresh reader each time: a cached one could mask a write that
        # never actually reached the store.
        return RemindersTagStore().tags_for_reminder(item.id)

    # --- add -------------------------------------------------------------
    detail = store.add_tags(item.id, ["zztest-alpha"])
    eq(detail.tags, ["zztest-alpha"])
    eq(from_store(), ["zztest-alpha"], "tag did not reach the Reminders store")

    # Adding again must not duplicate.
    detail = store.add_tags(item.id, ["zztest-alpha"])
    eq(detail.tags, ["zztest-alpha"], "adding an existing tag duplicated it")
    # Nor should a different casing of the same tag.
    detail = store.add_tags(item.id, ["ZZTEST-ALPHA"])
    eq(len(detail.tags), 1, "case variant created a second tag")

    # A leading '#' is accepted and stripped.
    detail = store.add_tags(item.id, ["#zztest-beta"])
    eq(sorted(detail.tags), ["zztest-alpha", "zztest-beta"])
    eq(sorted(from_store()), ["zztest-alpha", "zztest-beta"])

    # --- the real tag must be independent of the title text --------------
    detail = store.get_reminder(item.id)
    eq(sorted(detail.tags), ["zztest-alpha", "zztest-beta"])
    eq(detail.text_hashtags, [], "title has no '#' yet text_hashtags is set")
    truthy("#" not in detail.title, "test reminder title should carry no '#'")

    # --- remove ----------------------------------------------------------
    detail = store.remove_tags(item.id, ["zztest-alpha"])
    eq(detail.tags, ["zztest-beta"])
    eq(from_store(), ["zztest-beta"])
    # Removing something absent is a no-op, not an error.
    detail = store.remove_tags(item.id, ["not-there-at-all"])
    eq(detail.tags, ["zztest-beta"])

    # --- set (replace) ---------------------------------------------------
    detail = store.set_tags(item.id, ["zztest-gamma", "zztest-beta"])
    eq(sorted(detail.tags), ["zztest-beta", "zztest-gamma"])
    eq(sorted(from_store()), ["zztest-beta", "zztest-gamma"])

    # --- clear -----------------------------------------------------------
    detail = store.set_tags(item.id, [])
    eq(detail.tags, [])
    eq(from_store(), [], "tags were not cleared in the Reminders store")


@test("B", "create_reminder applies tags")
def t_create_with_tags():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    if store.tag_writes_unavailable_reason() is not None:
        skip("Tag writes unavailable")

    item = store.create_reminder(
        title="zztagcreate subject", list_id=list_id, tags=["#zztest-delta"]
    )
    _track(item.id)
    eq(item.tags, ["zztest-delta"])
    eq(item.tags_unavailable_reason, None)

    from apple_reminders_mcp.tagstore import RemindersTagStore
    if RemindersTagStore().unavailable_reason() is None:
        eq(RemindersTagStore().tags_for_reminder(item.id), ["zztest-delta"])


@test("B", "tag filter matches real tags, not '#' text")
def t_tag_filter_live():
    """A '#token' in the title must not satisfy a tag filter."""
    store = _store_or_skip()
    list_id = _ensure_test_list()
    if store.tag_writes_unavailable_reason() is not None:
        skip("Tag writes unavailable")

    tagged = store.create_reminder(title="zzfilter really tagged", list_id=list_id)
    _track(tagged.id)
    store.add_tags(tagged.id, ["zztest-filter"])

    # Same word, but only as text in the title.
    decoy = store.create_reminder(
        title="zzfilter decoy #zztest-filter", list_id=list_id
    )
    _track(decoy.id)

    _, rows = store.list_reminders(
        list_ids=[list_id], tags=["zztest-filter"], limit=200
    )
    ids = {r.id for r in rows}
    is_in(tagged.id, ids, "the genuinely tagged reminder was not returned")
    truthy(
        decoy.id not in ids,
        "a '#token' in the title satisfied a real-tag filter",
    )

    # And the decoy reports the text separately, with no real tag.
    detail = store.get_reminder(decoy.id)
    eq(detail.tags, [])
    eq(detail.text_hashtags, ["zztest-filter"])


@test("B", "linked content write round-trip")
def t_link_write_round_trip():
    """Set / replace / clear the Mail and Messages chip on a throwaway
    reminder, confirming each step against the store file as well as
    against ReminderKit's own read-back."""
    store = _store_or_skip()
    list_id = _ensure_test_list()
    reason = store.link_writes_unavailable_reason()
    if reason is not None:
        skip(f"Link writes unavailable: {reason}")

    from apple_reminders_mcp.linkstore import RemindersLinkStore, decode_user_activity

    fresh = RemindersLinkStore()
    if fresh.unavailable_reason() is not None:
        skip("Full Disk Access needed to verify link writes independently")

    item = store.create_reminder(title="zzlinkwrite subject", list_id=list_id)
    _track(item.id)
    eq(item.link, None, "a new reminder should start with no linked content")
    eq(item.link_unavailable_reason, None)

    def from_store():
        # A new reader each time: nothing may be served from a cache.
        return RemindersLinkStore().link_for_reminder(item.id)

    # --- Mail chip, from the Mail connector's own mail_link spelling -------
    detail = store.set_link(item.id, "message://%3Czztest%40example.mail%3E")
    not_none(detail.link)
    eq(detail.link.kind, "mail")
    eq(detail.link.url, "message:%3Czztest@example.mail%3E")
    eq(detail.link_unavailable_reason, None)
    stored = from_store()
    eq(stored, detail.link, "Mail link did not reach the Reminders store")
    # And the on-disk archive has Apple's shape: type 1, storage = the URL.
    blob = RemindersLinkStore().raw_user_activity(item.id)
    not_none(blob)
    is_in(b"message:%3Czztest@example.mail%3E", blob)
    eq(decode_user_activity(blob), detail.link)

    # Setting the same link again is a no-op (no save, no churn).
    before_mod = store.get_reminder(item.id).modification_date
    detail = store.set_link(item.id, "message:%3Czztest@example.mail%3E")
    eq(detail.link.url, "message:%3Czztest@example.mail%3E")
    eq(store.get_reminder(item.id).modification_date, before_mod,
       "a no-change set_link saved anyway")

    # --- Replace with a Messages chip --------------------------------------
    detail = store.set_link(item.id, "any;+;chat1234567890", "zztest group")
    eq(detail.link.kind, "messages")
    eq(detail.link.url, "messages://open?groupid=chat1234567890")
    eq(detail.link.title, "zztest group")
    eq(detail.link.activity_type, "com.apple.Messages")
    eq(from_store(), detail.link, "Messages link did not reach the Reminders store")

    detail = store.set_link(item.id, "any;-;+15551234567", "zztest person")
    eq(detail.link.url, "messages://open?addresses=+15551234567")
    eq(from_store().title, "zztest person")

    # --- get_reminder reads it back from the store ---------------------------
    fetched = store.get_reminder(item.id)
    eq(fetched.link, detail.link)
    eq(fetched.link_unavailable_reason, None)

    # --- Clear -------------------------------------------------------------
    detail = store.clear_link(item.id)
    eq(detail.link, None)
    eq(detail.link_unavailable_reason, None)
    eq(from_store(), None, "linked content was not cleared in the Reminders store")
    # Clearing again is a no-op, not an error.
    eq(store.clear_link(item.id).link, None)


@test("B", "set_reminder_link stores a numeric label as its digits")
def t_link_numeric_title_live():
    """The 2026-09-22 failure: an SMS shortcode chat (`any;-;42878`) whose
    label is the shortcode itself. Goes through the MCP tool layer, not
    the store, because that is where the integer was refused."""
    import asyncio

    from apple_reminders_mcp import server

    store = _store_or_skip()
    list_id = _ensure_test_list()
    if store.link_writes_unavailable_reason() is not None:
        skip(f"Link writes unavailable: {store.link_writes_unavailable_reason()}")

    # Share the test's store: EventKit rations EKEventStore instances.
    server._store = store
    item = store.create_reminder(title="zzlink shortcode", list_id=list_id)
    _track(item.id)

    def via_tool(title):
        # The same entry point a client's request reaches, schema
        # validation included.
        return asyncio.run(server.mcp.call_tool(
            "set_reminder_link",
            {"reminder_id": item.id, "link": "any;-;42878", "title": title},
        ))

    # As a string, the way a schema-respecting client sends it.
    via_tool("42878")
    detail = store.get_reminder(item.id)
    not_none(detail.link)
    eq(detail.link.kind, "messages")
    eq(detail.link.url, "messages://open?addresses=42878")
    eq(detail.link.title, "42878", "label must be the digits, no quotes")

    # As an integer, the way the failing client sent it.
    store.clear_link(item.id)
    via_tool(42878)
    detail = store.get_reminder(item.id)
    not_none(detail.link)
    eq(detail.link.title, "42878", "integer label was not stored as its digits")

    from apple_reminders_mcp.linkstore import RemindersLinkStore
    if RemindersLinkStore().unavailable_reason() is None:
        eq(RemindersLinkStore().link_for_reminder(item.id).title, "42878",
           "numeric label did not reach the Reminders store")


@test("B", "create_reminder applies linked content")
def t_create_with_link():
    store = _store_or_skip()
    list_id = _ensure_test_list()
    if store.link_writes_unavailable_reason() is not None:
        skip("Link writes unavailable")

    item = store.create_reminder(
        title="zzlinkcreate subject", list_id=list_id,
        link="message://%3Czzcreate%40example.mail%3E",
    )
    _track(item.id)
    not_none(item.link)
    eq(item.link.kind, "mail")
    eq(item.link.url, "message:%3Czzcreate@example.mail%3E")
    eq(item.link_unavailable_reason, None)
    # `url` is a different field and must stay untouched.
    eq(item.url, None)

    from apple_reminders_mcp.linkstore import RemindersLinkStore
    if RemindersLinkStore().unavailable_reason() is None:
        eq(RemindersLinkStore().link_for_reminder(item.id), item.link)

    # Tags and link together, in one call.
    if store.tag_writes_unavailable_reason() is None:
        both = store.create_reminder(
            title="zzlinkcreate both", list_id=list_id,
            tags=["zztest-link"], link="any;+;chat42", link_title="zztest chat",
        )
        _track(both.id)
        eq(both.tags, ["zztest-link"])
        eq(both.link.kind, "messages")
        eq(both.link.title, "zztest chat")

    # A malformed link fails before anything is created.
    n_before = len(store.list_reminders(list_ids=[list_id], limit=500).reminders)
    try:
        store.create_reminder(title="zzlinkcreate bad", list_id=list_id, link="nonsense")
    except ValueError:
        pass
    else:
        raise AssertionError("create_reminder accepted a malformed link")
    eq(len(store.list_reminders(list_ids=[list_id], limit=500).reminders), n_before,
       "a reminder was created despite the malformed link")


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
            label = {
                "A": "Static",
                "B": "Live (EventKit)",
                "C": "Live (local Reminders store)",
            }.get(group, group)
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
