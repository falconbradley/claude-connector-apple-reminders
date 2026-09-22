"""
Read engine for a reminder's *linked content* — the Mail / Messages chip.

What the chip is
----------------
When a reminder is made with Siri ("remind me about this") or from the
share sheet, Reminders.app shows a small chip under the title — the Mail
icon with the message's subject, or the Messages icon with the chat's
name — and tapping it opens the source. That chip is **not** the
reminder's URL field. EventKit's ``EKCalendarItem.URL`` lands in the
store's ``ZICSURL`` column and the list view never draws it.

The chip is a ``REMUserActivity`` archived with ``NSKeyedArchiver`` into
``ZREMCDREMINDER.ZUSERACTIVITY``. Two shapes exist in the wild, decoded
from real rows on 2026-09-21:

* **type 1 — universal link.** ``storage`` is the bare URL as bytes. The
  Mail chip is one of these, with the message's RFC 5322 Message-ID URL:
  ``message:%3C<id>%3E`` (note ``message:`` with no ``//``).

* **type 2 — user activity.** ``storage`` is a *nested* keyed archive of
  an ``NSUserActivity`` (``UAUserActivityInfo`` on disk). The Messages
  chip is one: ``activityType`` = ``com.apple.Messages``, ``title`` = the
  chat's display name, ``targetContentIdentifier`` =
  ``messages://open?groupid=chat…``. Even Apple's own link is chat-level;
  there is no per-message deep link into Messages.app.

Apple archives the top-level fields (``type``, ``storage``, ``flags``)
straight into ``$top``. An archive produced with
``+[NSKeyedArchiver archivedDataWithRootObject:…]`` instead puts them in
a ``root`` object. Most rows are binary plists; one real row was XML,
where references are ``{"CF$UID": n}`` dictionaries. The decoder accepts
all of these.

Safety
------
Exactly as tagstore.py: the store is opened read-only (URI ``mode=ro``
plus ``PRAGMA query_only``) and never written. Needs Full Disk Access.
Writing goes through ReminderKit — see linkwriter.py.
"""

from __future__ import annotations

import logging
import plistlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .models import LinkedContent
from .tagstore import _STORES_DIR

logger = logging.getLogger("apple_reminders_mcp.linkstore")

_REMINDER_TABLE = "ZREMCDREMINDER"
_ACTIVITY_COLUMN = "ZUSERACTIVITY"

# REMUserActivityType, as observed. 1 = universal link, 2 = NSUserActivity.
TYPE_UNIVERSAL_LINK = 1
TYPE_USER_ACTIVITY = 2

MESSAGES_ACTIVITY_TYPE = "com.apple.Messages"

# Last-resort URL scrape for a user activity whose target identifier is
# absent: something scheme-like followed by non-control characters.
_URL_IN_BYTES = re.compile(rb"[a-zA-Z][a-zA-Z0-9+.-]*://[^\x00-\x20\"'<>]+")


class LinkStoreError(RuntimeError):
    """Raised when the local Reminders store cannot be read.

    Almost always missing Full Disk Access for the host process.
    """


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def kind_for_url(url: Optional[str], activity_type: Optional[str] = None) -> str:
    """Classify a link the way the connector reports it."""
    if activity_type == MESSAGES_ACTIVITY_TYPE:
        return "messages"
    scheme = (url or "").split(":", 1)[0].lower()
    if scheme in ("messages", "imessage", "sms"):
        return "messages"
    if scheme == "message":
        return "mail"
    if scheme in ("http", "https"):
        return "web"
    return "other"


def _resolve(objects: list[Any], value: Any) -> Any:
    """Follow an archive reference to its object.

    Binary archives carry native UIDs; XML-format ones (Apple writes both —
    one of ten real rows was XML) spell the same reference as a
    ``{"CF$UID": n}`` dictionary.
    """
    index: Optional[int] = None
    if isinstance(value, plistlib.UID):
        index = value.data
    elif isinstance(value, dict) and set(value) == {"CF$UID"}:
        index = value["CF$UID"]
    if index is None:
        return value
    try:
        return objects[index]
    except (IndexError, TypeError):
        return None


def _as_bytes(obj: Any) -> Optional[bytes]:
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("NS.data"), bytes):
        return obj["NS.data"]
    return None


def _as_str(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        return obj if obj != "$null" else None
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return None


def decode_user_activity(blob: Optional[bytes]) -> Optional[LinkedContent]:
    """Decode a ``ZUSERACTIVITY`` blob. None when there is no link in it.

    Never raises on malformed input: a reminder whose chip cannot be
    understood should read as "no readable link", logged, not as a
    failed tool call.
    """
    if not blob:
        return None
    try:
        archive = plistlib.loads(blob)
        objects = archive["$objects"]
        top = archive["$top"]
        fields = _resolve(objects, top["root"]) if "root" in top else top
        if not isinstance(fields, dict):
            return None
        kind_code = fields.get("type")
        storage = _as_bytes(_resolve(objects, fields.get("storage")))
        if storage is None:
            return None
        if kind_code == TYPE_UNIVERSAL_LINK:
            url = storage.decode("utf-8", "replace")
            return LinkedContent(kind=kind_for_url(url), url=url)
        if kind_code == TYPE_USER_ACTIVITY:
            return _decode_activity_archive(storage)
        logger.debug("Unknown REMUserActivity type %r", kind_code)
        return None
    except Exception as exc:  # plistlib raises several different types
        logger.warning("Could not decode a reminder's user activity: %s", exc)
        return None


def _decode_activity_archive(data: bytes) -> Optional[LinkedContent]:
    inner = plistlib.loads(data)
    objects = inner["$objects"]
    info = next(
        (o for o in objects if isinstance(o, dict) and "activityType" in o),
        None,
    )
    if info is None:
        return None
    activity_type = _as_str(_resolve(objects, info.get("activityType")))
    title = _as_str(_resolve(objects, info.get("title")))
    url = _as_str(_resolve(objects, info.get("targetContentIdentifier")))
    if not url:
        url = _as_str(_resolve(objects, info.get("webpageURL")))
    if not url:
        hit = _URL_IN_BYTES.search(data)
        url = hit.group(0).decode("utf-8", "replace") if hit else None
    return LinkedContent(
        kind=kind_for_url(url, activity_type),
        url=url,
        title=title,
        activity_type=activity_type,
    )


# ---------------------------------------------------------------------------
# Store reader
# ---------------------------------------------------------------------------

class RemindersLinkStore:
    """Read-only view over the Reminders stores, for linked content only."""

    def __init__(self, stores_dir: Optional[Path] = None) -> None:
        self._dir = stores_dir or _STORES_DIR
        self._local = threading.local()
        self._lock = threading.Lock()
        # path → whether this store's reminder table carries the column.
        # Schema is stable per file; the data never is, so only this is kept.
        self._has_column: dict[str, bool] = {}

    def store_paths(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        try:
            return sorted(self._dir.glob("Data-*.sqlite"))
        except OSError:
            return []

    def available(self) -> bool:
        try:
            return any(self._open_all())
        except LinkStoreError:
            return False

    def unavailable_reason(self) -> Optional[str]:
        try:
            if any(self._open_all()):
                return None
        except LinkStoreError as exc:
            return str(exc)
        return (
            f"No readable Reminders store found under {self._dir}. Is "
            "Reminders set up on this Mac, and does the host process have "
            "Full Disk Access?"
        )

    def _connect(self, path: Path) -> sqlite3.Connection:
        cache: Optional[dict[str, sqlite3.Connection]] = getattr(self._local, "cons", None)
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
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise LinkStoreError(
                f"Could not open {path.name} read-only: {exc}. This usually "
                "means the host process is missing Full Disk Access — see "
                "the README."
            ) from exc
        cache[str(path)] = con
        return con

    def _usable(self, path: Path, con: sqlite3.Connection) -> bool:
        key = str(path)
        with self._lock:
            if key in self._has_column:
                return self._has_column[key]
        try:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({_REMINDER_TABLE})")}
            ok = _ACTIVITY_COLUMN in cols and "ZDACALENDARITEMUNIQUEIDENTIFIER" in cols
        except sqlite3.Error:
            ok = False
        with self._lock:
            self._has_column[key] = ok
        return ok

    def _open_all(self) -> list[tuple[Path, sqlite3.Connection]]:
        paths = self.store_paths()
        if not paths:
            raise LinkStoreError(
                f"No Reminders store found under {self._dir}. Is Reminders "
                "set up on this Mac, and does the host process have Full "
                "Disk Access?"
            )
        usable: list[tuple[Path, sqlite3.Connection]] = []
        errors: list[str] = []
        for path in paths:
            try:
                con = self._connect(path)
            except LinkStoreError as exc:
                errors.append(str(exc))
                continue
            if self._usable(path, con):
                usable.append((path, con))
        if not usable and errors:
            raise LinkStoreError("; ".join(errors))
        return usable

    @staticmethod
    def _live_filter(con: sqlite3.Connection) -> str:
        """SQL excluding soft-deleted reminders, when the store has the flag.

        A reminder that moved between accounts can linger, marked for
        deletion, in the old account's store with its chip still attached.
        Every read applies this so the two lookups below agree.
        """
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({_REMINDER_TABLE})")}
        if "ZMARKEDFORDELETION" in cols:
            return " AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION = 0)"
        return ""

    def raw_user_activity(self, reminder_id: str) -> Optional[bytes]:
        """The archived ``REMUserActivity`` for a reminder, or None."""
        wanted = reminder_id.strip().upper()
        for path, con in self._open_all():
            try:
                row = con.execute(
                    f"SELECT {_ACTIVITY_COLUMN} AS blob FROM {_REMINDER_TABLE} "
                    f"WHERE upper(ZDACALENDARITEMUNIQUEIDENTIFIER) = ?"
                    f"{self._live_filter(con)} LIMIT 1",
                    (wanted,),
                ).fetchone()
            except sqlite3.Error as exc:
                logger.warning("Link read failed for %s: %s", path.name, exc)
                continue
            if row is not None:
                blob = row["blob"]
                return bytes(blob) if blob is not None else None
        return None

    def link_for_reminder(self, reminder_id: str) -> Optional[LinkedContent]:
        """The linked content on one reminder, or None when it has none.

        Raises LinkStoreError when no store could be read — callers must
        keep "no link" and "could not tell" apart.
        """
        return decode_user_activity(self.raw_user_activity(reminder_id))

    def all_links(self) -> dict[str, LinkedContent]:
        """reminder uuid → linked content, for every reminder carrying one."""
        out: dict[str, LinkedContent] = {}
        for path, con in self._open_all():
            try:
                rows = con.execute(
                    f"SELECT upper(ZDACALENDARITEMUNIQUEIDENTIFIER) AS uuid, "
                    f"{_ACTIVITY_COLUMN} AS blob FROM {_REMINDER_TABLE} "
                    f"WHERE {_ACTIVITY_COLUMN} IS NOT NULL "
                    f"AND ZDACALENDARITEMUNIQUEIDENTIFIER IS NOT NULL"
                    f"{self._live_filter(con)}"
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Link scan failed for %s: %s", path.name, exc)
                continue
            for row in rows:
                link = decode_user_activity(bytes(row["blob"]))
                if link is not None:
                    out[str(row["uuid"])] = link
        return out
