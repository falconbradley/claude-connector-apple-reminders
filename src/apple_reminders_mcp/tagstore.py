"""
Read engine for *real* Apple Reminders tags.

Why this module exists
----------------------
EventKit has no tags. ``EKReminder``/``EKCalendarItem`` expose no tag or
hashtag property, and a symbol scan of the framework binary turns up no
private one either. The Reminders AppleScript dictionary is equally bare
(name, body, due date, priority, flagged, ... and nothing else). So a tag
a user adds in Reminders.app is invisible to every supported API.

It is, however, plainly visible in the app's own Core Data store, which
is where this module reads it from — the same approach the sibling Apple
Notes connector takes for note tags.

Where tags live
---------------
Reminders keeps one SQLite store per account under

    ~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/
        Data-<ACCOUNT-UUID>.sqlite      one per remote account (iCloud, ...)
        Data-local.sqlite               the on-device account

Tags are modelled as two entities:

* ``REMCDHashtagLabel`` → table ``ZREMCDHASHTAGLABEL``. One row per tag
  that exists in the account, with ``ZNAME`` (as typed, e.g. "autoreview")
  and ``ZCANONICALNAME`` (case-folded, used by the app for matching).

* ``REMCDHashtag`` → stored in the shared wide table ``ZREMCDOBJECT``,
  one row per *application* of a tag to a reminder. It carries
  ``ZHASHTAGLABEL`` (→ the label) and a reminder foreign key.

So a reminder's real tags are:

    ZREMCDOBJECT (Z_ENT = REMCDHashtag)
      → ZREMCDHASHTAGLABEL  via ZHASHTAGLABEL
      → ZREMCDREMINDER      via the reminder FK

Schema drift
------------
Two things move between macOS releases and even between stores on the
same Mac, so both are resolved at runtime rather than hardcoded:

* ``Z_ENT`` for ``REMCDHashtag`` — observed as 32 in the iCloud stores
  and 33 in ``Data-local.sqlite`` on the same machine. Read from
  ``Z_PRIMARYKEY``.

* The reminder foreign-key column. ``ZREMCDOBJECT`` is shared by ~20
  entities, so Core Data disambiguates same-named relationships with
  numeric suffixes: ``ZREMINDER``, ``ZREMINDER1`` ... ``ZREMINDER5`` all
  exist, and which one belongs to ``REMCDHashtag`` depends on model
  ordering. We probe for the one actually populated on hashtag rows.

Reminder identity
-----------------
``EKReminder.calendarItemIdentifier()`` is the reminder's UUID, which the
store holds twice: as raw bytes in ``ZIDENTIFIER`` (indexed, unique) and
as a dashed uppercase string in ``ZDACALENDARITEMUNIQUEIDENTIFIER``. On
this Mac all 3283 reminders agree across both, so we read the string
column and fall back to formatting the blob.

Safety
------
The store is opened strictly read-only (URI ``mode=ro`` plus
``PRAGMA query_only``) and never written. Reminders.app remains the only
writer, and the sole sync and auth engine. Nothing here is cached across
calls: a cached tag map is exactly how a stale read gets served, which is
the bug this feature was reported alongside.

Requires Full Disk Access for the host process, since Group Containers
are TCC-protected. Without it, callers get a clear error rather than a
silently empty tag list.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import TagInfo

logger = logging.getLogger("apple_reminders_mcp.tagstore")

_STORES_DIR = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.reminders"
    / "Container_v1" / "Stores"
)

# Core Data numbers same-named relationships on the shared object table.
# Ordered widest-first so the bare name is tried before the suffixed ones.
_REMINDER_FK_CANDIDATES = (
    "ZREMINDER", "ZREMINDER1", "ZREMINDER2",
    "ZREMINDER3", "ZREMINDER4", "ZREMINDER5",
)

_HASHTAG_ENTITY = "REMCDHashtag"
_LABEL_TABLE = "ZREMCDHASHTAGLABEL"
_OBJECT_TABLE = "ZREMCDOBJECT"
_REMINDER_TABLE = "ZREMCDREMINDER"

# Dashed-uppercase UUID rebuilt from the 16-byte ZIDENTIFIER blob, used
# when ZDACALENDARITEMUNIQUEIDENTIFIER is absent.
_UUID_FROM_HEX = (
    "upper("
    "substr(hex({c}),1,8)||'-'||substr(hex({c}),9,4)||'-'||"
    "substr(hex({c}),13,4)||'-'||substr(hex({c}),17,4)||'-'||"
    "substr(hex({c}),21,12))"
)


class TagStoreError(RuntimeError):
    """Raised when the local Reminders store cannot be read.

    Almost always missing Full Disk Access for the host process.
    """


def _normalise(name: str) -> str:
    """Case-fold a tag name and drop a leading '#' the caller may have typed."""
    return name.strip().lstrip("#").strip().casefold()


class RemindersTagStore:
    """Read-only view over the Reminders Core Data stores, for tags only.

    Everything else about a reminder comes from EventKit; this reads the
    one thing EventKit cannot see.
    """

    def __init__(self, stores_dir: Optional[Path] = None) -> None:
        self._dir = stores_dir or _STORES_DIR
        self._local = threading.local()
        # path → resolved schema, or None when this store has no readable
        # hashtag model. Schema shape is stable for a given file, so unlike
        # tag *data* it is safe to remember.
        self._schemas: dict[str, Optional[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Discovery / connection
    # ------------------------------------------------------------------

    def store_paths(self) -> list[Path]:
        """Every per-account store file, newest-looking first."""
        if not self._dir.is_dir():
            return []
        try:
            return sorted(self._dir.glob("Data-*.sqlite"))
        except OSError:
            return []

    def available(self) -> bool:
        """True when at least one store can be opened and understood."""
        try:
            return any(self._usable_stores())
        except TagStoreError:
            return False

    def unavailable_reason(self) -> Optional[str]:
        """Human-readable reason reads would fail, or None when they work."""
        try:
            if any(self._usable_stores()):
                return None
        except TagStoreError as exc:
            return str(exc)
        return (
            f"No readable Reminders store found under {self._dir}. Is "
            "Reminders set up on this Mac, and does the host process have "
            "Full Disk Access?"
        )

    def _usable_stores(self) -> list[tuple[sqlite3.Connection, dict[str, Any]]]:
        """Open every store that exposes a readable hashtag model.

        Raises TagStoreError only when *nothing* could be opened, so one
        odd account store cannot take the whole feature down.
        """
        paths = self.store_paths()
        if not paths:
            raise TagStoreError(
                f"No Reminders store found under {self._dir}. Is Reminders "
                "set up on this Mac, and does the host process have Full "
                "Disk Access?"
            )
        usable: list[tuple[sqlite3.Connection, dict[str, Any]]] = []
        errors: list[str] = []
        for path in paths:
            try:
                con = self._connect(path)
            except TagStoreError as exc:
                errors.append(str(exc))
                continue
            schema = self._schema(path, con)
            if schema is not None:
                usable.append((con, schema))
        if not usable and errors:
            raise TagStoreError("; ".join(errors))
        return usable

    def _connect(self, path: Path) -> sqlite3.Connection:
        cache: dict[str, sqlite3.Connection] = getattr(self._local, "cons", None)  # type: ignore[assignment]
        if cache is None:
            cache = {}
            self._local.cons = cache
        con = cache.get(str(path))
        if con is not None:
            return con
        try:
            con = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
            con.execute("PRAGMA query_only = 1")
            con.row_factory = sqlite3.Row
            # Cheap proof the file really opened (and that WAL recovery,
            # which needs the -shm sidecar, did not trip on permissions).
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise TagStoreError(
                f"Could not open {path.name} read-only: {exc}. This usually "
                "means the host process is missing Full Disk Access — see "
                "the README."
            ) from exc
        cache[str(path)] = con
        return con

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def _schema(self, path: Path, con: sqlite3.Connection) -> Optional[dict[str, Any]]:
        key = str(path)
        with self._lock:
            if key in self._schemas:
                return self._schemas[key]
        resolved = self._resolve_schema(path, con)
        with self._lock:
            self._schemas[key] = resolved
        return resolved

    def _resolve_schema(
        self, path: Path, con: sqlite3.Connection
    ) -> Optional[dict[str, Any]]:
        """Work out this store's hashtag entity id and reminder FK column."""
        tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for needed in (_LABEL_TABLE, _OBJECT_TABLE, _REMINDER_TABLE, "Z_PRIMARYKEY"):
            if needed not in tables:
                logger.debug("%s: no %s table, skipping", path.name, needed)
                return None

        row = con.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = ?", (_HASHTAG_ENTITY,)
        ).fetchone()
        if row is None:
            logger.debug("%s: entity %s not in model", path.name, _HASHTAG_ENTITY)
            return None
        ent = int(row["Z_ENT"])

        obj_cols = {r["name"] for r in con.execute(f"PRAGMA table_info({_OBJECT_TABLE})")}
        if "ZHASHTAGLABEL" not in obj_cols:
            logger.debug("%s: %s has no ZHASHTAGLABEL", path.name, _OBJECT_TABLE)
            return None

        fk = self._probe_reminder_fk(con, ent, obj_cols)

        rem_cols = {r["name"] for r in con.execute(f"PRAGMA table_info({_REMINDER_TABLE})")}
        if "ZDACALENDARITEMUNIQUEIDENTIFIER" in rem_cols:
            uuid_expr = "upper(r.ZDACALENDARITEMUNIQUEIDENTIFIER)"
        elif "ZIDENTIFIER" in rem_cols:
            uuid_expr = _UUID_FROM_HEX.format(c="r.ZIDENTIFIER")
        else:
            logger.debug("%s: no reminder identifier column", path.name)
            return None

        label_cols = {r["name"] for r in con.execute(f"PRAGMA table_info({_LABEL_TABLE})")}
        if "ZNAME" not in label_cols:
            return None

        return {
            "path": path,
            "ent": ent,
            "reminder_fk": fk,
            "uuid_expr": uuid_expr,
            # Not every release carries the soft-delete flag on every table.
            "obj_deleted": "ZMARKEDFORDELETION" in obj_cols,
            "rem_deleted": "ZMARKEDFORDELETION" in rem_cols,
        }

    def _probe_reminder_fk(
        self, con: sqlite3.Connection, ent: int, obj_cols: set[str]
    ) -> Optional[str]:
        """Find which ZREMINDER* column holds a hashtag row's reminder.

        Returns None when the store has no live hashtag rows to learn
        from — in which case it has no tags to report either, so the
        queries below correctly yield nothing.
        """
        for col in _REMINDER_FK_CANDIDATES:
            if col not in obj_cols:
                continue
            try:
                hit = con.execute(
                    f"SELECT 1 FROM {_OBJECT_TABLE} o "
                    f"JOIN {_REMINDER_TABLE} r ON r.Z_PK = o.{col} "
                    f"WHERE o.Z_ENT = ? AND o.ZHASHTAGLABEL IS NOT NULL LIMIT 1",
                    (ent,),
                ).fetchone()
            except sqlite3.Error:
                continue
            if hit is not None:
                return col
        return None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _rows(self) -> list[sqlite3.Row]:
        """Every live (tag name, reminder uuid) pair across all accounts."""
        out: list[sqlite3.Row] = []
        for con, schema in self._usable_stores():
            fk = schema["reminder_fk"]
            if not fk:
                continue
            where = [f"o.Z_ENT = {int(schema['ent'])}"]
            if schema["obj_deleted"]:
                where.append("(o.ZMARKEDFORDELETION IS NULL OR o.ZMARKEDFORDELETION = 0)")
            if schema["rem_deleted"]:
                where.append("(r.ZMARKEDFORDELETION IS NULL OR r.ZMARKEDFORDELETION = 0)")
            sql = (
                f"SELECT l.ZNAME AS name, {schema['uuid_expr']} AS uuid "
                f"FROM {_OBJECT_TABLE} o "
                f"JOIN {_LABEL_TABLE} l ON l.Z_PK = o.ZHASHTAGLABEL "
                f"JOIN {_REMINDER_TABLE} r ON r.Z_PK = o.{fk} "
                f"WHERE {' AND '.join(where)} "
                f"AND l.ZNAME IS NOT NULL AND {schema['uuid_expr']} IS NOT NULL"
            )
            try:
                out.extend(con.execute(sql).fetchall())
            except sqlite3.Error as exc:
                logger.warning(
                    "Tag read failed for %s: %s", schema["path"].name, exc
                )
        return out

    def tags_by_reminder(self) -> dict[str, list[str]]:
        """reminder uuid → its real tag names, sorted, deduplicated.

        One query per account store, so callers annotating a page of
        search results should call this once rather than per reminder.
        """
        acc: dict[str, set[str]] = {}
        for row in self._rows():
            acc.setdefault(str(row["uuid"]).upper(), set()).add(str(row["name"]))
        return {
            uuid: sorted(names, key=str.casefold) for uuid, names in acc.items()
        }

    def tags_for_reminder(self, reminder_id: str) -> list[str]:
        """Real tag names on one reminder. Empty list when it has none."""
        return self.tags_by_reminder().get(reminder_id.upper(), [])

    def list_tags(self) -> list[TagInfo]:
        """Every tag in every account, with how many reminders carry it.

        Includes tags the user created that are not currently applied to
        anything: Reminders keeps the label row, and the app still offers
        such a tag in its filter UI, so hiding it here would misrepresent
        what exists.
        """
        counts: dict[str, dict[str, Any]] = {}

        def entry(name: str) -> dict[str, Any]:
            return counts.setdefault(
                _normalise(name), {"name": name, "ids": set()}
            )

        for con, schema in self._usable_stores():
            try:
                for row in con.execute(f"SELECT ZNAME FROM {_LABEL_TABLE}"):
                    if row["ZNAME"]:
                        entry(str(row["ZNAME"]))
            except sqlite3.Error as exc:
                logger.warning(
                    "Label read failed for %s: %s", schema["path"].name, exc
                )

        for row in self._rows():
            entry(str(row["name"]))["ids"].add(str(row["uuid"]).upper())

        tags = [
            TagInfo(
                name=v["name"],
                reminder_count=len(v["ids"]),
                reminder_ids=sorted(v["ids"]),
            )
            for v in counts.values()
        ]
        tags.sort(key=lambda t: (-t.reminder_count, t.name.casefold()))
        return tags

    def reminders_with_tags(
        self, names: Iterable[str], match_all: bool = False
    ) -> set[str]:
        """Reminder ids carrying the given tags, matched case-insensitively.

        A leading '#' is accepted and ignored, so callers need not know
        whether the user typed the sigil.
        """
        wanted = {_normalise(n) for n in names}
        wanted.discard("")
        if not wanted:
            return set()
        by_reminder: dict[str, set[str]] = {}
        for row in self._rows():
            by_reminder.setdefault(str(row["uuid"]).upper(), set()).add(
                _normalise(str(row["name"]))
            )
        if match_all:
            return {rid for rid, have in by_reminder.items() if wanted <= have}
        return {rid for rid, have in by_reminder.items() if wanted & have}
