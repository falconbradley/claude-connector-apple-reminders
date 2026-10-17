"""
Read engine for list sharing and reminder assignments.

Why this module exists
----------------------
A list shared through iCloud has members, and a reminder on such a list
can be assigned to one of them. EventKit sees none of it: ``EKCalendar``
reports no sharees for these lists and ``EKReminder`` has no assignee.
Both live in the Reminders app's own Core Data store, next to the tags
tagstore.py reads, so this module reads them the same way.

Where sharing lives
-------------------
Decoded from Brad's real iCloud store on 2026-10-16 (3 shared lists,
8 sharees, 17 assignments):

* ``ZREMCDBASELIST`` — one row per list. ``ZCKIDENTIFIER`` is the same
  UUID EventKit reports as ``calendarIdentifier``. ``ZSHARINGSTATUS`` is
  0 for a list nobody else is on, 1 for one you share, 2 for one shared
  with you. ``ZSHAREDOWNERIDENTIFIER`` is a 16-byte UUID equal to the
  owner's sharee ``ZIDENTIFIER``. ``ZCKSERVERSHAREDATA`` is the list's
  ``CKShare``, archived with its system fields.

* ``REMCDSharee`` → the shared table ``ZREMCDOBJECT``. One row per person
  per list, the owner and you included. ``ZCKIDENTIFIER`` is the CloudKit
  share-participant id. Names are ``ZFIRSTNAME`` / ``ZLASTNAME`` /
  ``ZNICKNAME``. The address is ``mailto:…`` or ``tel:…``.

* ``REMCDAssignment`` → also ``ZREMCDOBJECT``. ``ZCKASSIGNEEIDENTIFIER``
  and ``ZCKORIGINATORIDENTIFIER`` are sharee participant ids, and all 17
  real rows resolve through them. There is also a ``ZASSIGNEE`` foreign
  key, but only 6 of the 17 rows fill it in, so it is not used.
  ``ZASSIGNEDDATE`` is a Core Data timestamp.

Who "me" is
-----------
No sharee row says which one is you. The archived ``CKShare`` does:
each participant carries ``IsCurrentUser``, and the current user's
record name is CloudKit's ``__defaultOwner__``. Its ``ParticipantID`` is
the sharee's ``ZCKIDENTIFIER``. The archive is read with ``plistlib``,
exactly as linkstore.py reads chips, so nothing is unarchived into live
objects. When it cannot be read, a list you own still knows you are its
owner. A list shared with you still knows you are the only other member
when it has just two. Past that, nobody is marked as you.

Sharee status
-------------
``CKShareParticipant.AcceptanceStatus`` is public: 1 pending, 2 accepted,
3 removed. It is used when the share decodes. The sharee row's own
status column is the fallback, and its codes were matched against
CloudKit's: 1 for every accepted participant, 5 for both invitations
nobody has answered.

Schema drift
------------
``ZREMCDOBJECT`` is shared by about 20 entities, so Core Data numbers
same-named columns. The sharee status is in ``ZSTATUS1`` and its address
in ``ZADDRESS1``. An assignment's status is in ``ZSTATUS`` and its
reminder in ``ZREMINDER1``. Entity ids differ between stores on one Mac
too. All of it is resolved at runtime, as in tagstore.py. A probe that
finds no rows is not remembered, so the first assignment made in a
session is still found.

Safety
------
Exactly as tagstore.py: the store is opened read-only (URI ``mode=ro``
plus ``PRAGMA query_only``) and never written. Needs Full Disk Access.
Assigning goes through ReminderKit — see assignwriter.py. Changing who a
list is shared with is out of scope: that sends iCloud invitations.
"""

from __future__ import annotations

import logging
import plistlib
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .launcher import full_disk_access_steps
from .linkstore import _resolve
from .models import Assignment, ListSharing, Sharee, ShareeStatus
from .tagstore import _STORES_DIR, _UUID_FROM_HEX

logger = logging.getLogger("apple_reminders_mcp.sharestore")

_OBJECT_TABLE = "ZREMCDOBJECT"
_LIST_TABLE = "ZREMCDBASELIST"
_REMINDER_TABLE = "ZREMCDREMINDER"

_SHAREE_ENTITY = "REMCDSharee"
_ASSIGNMENT_ENTITY = "REMCDAssignment"
_LIST_ENTITY = "REMCDList"

# ZREMCDBASELIST.ZSHARINGSTATUS, as observed and as REMList's own class
# methods treat it (see is_owned_by_me / is_shared below).
SHARING_STATUS_SHARED_TO_ME = 2

# CKShareParticipantAcceptanceStatus (public CloudKit API).
_CK_ACCEPTANCE: dict[int, ShareeStatus] = {1: "pending", 2: "accepted", 3: "removed"}
# CKShareParticipantRole.owner (public CloudKit API).
_CK_ROLE_OWNER = 1
# CKCurrentUserDefaultName: the record name CloudKit gives the current user.
_CK_CURRENT_USER = "__defaultOwner__"

# The sharee row's own status column, matched against CloudKit's above.
_REM_SHAREE_STATUS: dict[int, ShareeStatus] = {1: "accepted", 5: "pending"}

# Core Data stores dates as seconds since this instant.
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_ME = "me"


class ShareStoreError(RuntimeError):
    """Raised when the local Reminders store cannot be read.

    Almost always missing Full Disk Access for the extension's ``uv``.
    """


# ---------------------------------------------------------------------------
# Sharing rules shared with ReminderKit
# ---------------------------------------------------------------------------

def is_owned_by_me(sharing_status: int) -> bool:
    """Mirror of ``+[REMList isOwnedByMeWithSharingStatus:]``.

    Checked against the real class method for every observed status in
    the test suite, so the two cannot drift apart unnoticed.
    """
    return sharing_status in (0, 1)


def is_shared(sharee_count: int, sharing_status: int) -> bool:
    """Mirror of ``+[REMList isSharedWithShareeCount:sharingStatus:]``."""
    return sharee_count > 0 or sharing_status == SHARING_STATUS_SHARED_TO_ME


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

@dataclass
class _Participant:
    is_me: bool
    is_owner: bool
    acceptance: Optional[int]


def decode_share_participants(blob: Optional[bytes]) -> dict[str, _Participant]:
    """Participant id → what the archived ``CKShare`` says about them.

    Never raises: a share that cannot be understood just leaves the store's
    own columns to answer, which they mostly can.
    """
    if not blob:
        return {}
    try:
        archive = plistlib.loads(blob)
        objects = archive["$objects"]
        top = archive["$top"]
        container = _resolve(objects, top.get("Participants"))
        if not isinstance(container, dict):
            return {}
        out: dict[str, _Participant] = {}
        for ref in container.get("NS.objects", []):
            p = _resolve(objects, ref)
            if not isinstance(p, dict):
                continue
            pid = _resolve(objects, p.get("ParticipantID"))
            if not isinstance(pid, str) or pid == "$null":
                continue
            record_name = None
            identity = _resolve(objects, p.get("UserIdentity"))
            if isinstance(identity, dict):
                record = _resolve(objects, identity.get("UserRecordID"))
                if isinstance(record, dict):
                    name = _resolve(objects, record.get("RecordName"))
                    record_name = name if isinstance(name, str) else None
            acceptance = p.get("AcceptanceStatus")
            out[pid.upper()] = _Participant(
                is_me=p.get("IsCurrentUser") is True or record_name == _CK_CURRENT_USER,
                is_owner=p.get("Type") == _CK_ROLE_OWNER,
                acceptance=acceptance if isinstance(acceptance, int) else None,
            )
        return out
    except Exception as exc:  # plistlib raises several different types
        logger.warning("Could not decode a list's CloudKit share: %s", exc)
        return {}


def _split_address(address: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(email, phone) from a sharee address such as ``mailto:…`` / ``tel:…``."""
    if not address:
        return None, None
    value = address.strip()
    scheme, _, rest = value.partition(":")
    if scheme.lower() == "mailto" and rest:
        return rest, None
    if scheme.lower() == "tel" and rest:
        return None, rest
    if "@" in value:
        return value, None
    return None, value


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _core_data_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return _CORE_DATA_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _uuid_from_blob(blob: Any) -> Optional[str]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) != 16:
        return None
    h = bytes(blob).hex().upper()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass
class _ShareeRow:
    model: Sharee
    list_id: str
    first_name: Optional[str]
    nickname: Optional[str]

    def matches(self, who: str) -> bool:
        """True when `who` names this person: "me", id, email, phone or name."""
        wanted = who.strip()
        if not wanted:
            return False
        folded = wanted.casefold()
        if folded == _ME:
            return self.model.is_me
        m = self.model
        if wanted.upper() == m.id:
            return True
        if m.email and folded == m.email.casefold():
            return True
        if m.phone:
            a, b = _digits(wanted), _digits(m.phone)
            # Allow a missing country code: "+1 555…" vs "555…".
            if len(a) >= 7 and len(b) >= 7 and (a.endswith(b) or b.endswith(a)):
                return True
        names = {m.name, self.first_name, self.nickname}
        return any(n and folded == n.casefold() for n in names)


@dataclass
class _Snapshot:
    lists: dict[str, ListSharing] = field(default_factory=dict)
    sharees: dict[str, _ShareeRow] = field(default_factory=dict)
    # reminder uuid → current assignment
    assignments: dict[str, Assignment] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Store reader
# ---------------------------------------------------------------------------

class RemindersShareStore:
    """Read-only view over the Reminders stores, for sharing and assignments."""

    def __init__(self, stores_dir: Optional[Path] = None) -> None:
        self._dir = stores_dir or _STORES_DIR
        self._local = threading.local()
        self._lock = threading.Lock()
        # path → resolved schema. Only complete schemas are kept: a probe
        # that found no rows to learn from is retried on the next read.
        self._schemas: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Discovery / connection
    # ------------------------------------------------------------------

    def store_paths(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        try:
            return sorted(self._dir.glob("Data-*.sqlite"))
        except OSError:
            return []

    def available(self) -> bool:
        try:
            return any(self._usable_stores())
        except ShareStoreError:
            return False

    def unavailable_reason(self) -> Optional[str]:
        try:
            if any(self._usable_stores()):
                return None
        except ShareStoreError as exc:
            return str(exc)
        return (
            f"No readable Reminders store found under {self._dir}. Either "
            "Reminders is not set up on this Mac, or the extension lacks "
            "Full Disk Access.\n\n" + full_disk_access_steps()
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
            raise ShareStoreError(
                f"Could not open {path.name} read-only: {exc}. This usually "
                "means the extension is missing Full Disk Access.\n\n"
                + full_disk_access_steps()
            ) from exc
        cache[str(path)] = con
        return con

    def _usable_stores(self) -> list[tuple[sqlite3.Connection, dict[str, Any]]]:
        """Open every store with a readable sharing model.

        Raises ShareStoreError only when *nothing* could be opened, so one
        odd account store cannot take the whole feature down.
        """
        paths = self.store_paths()
        if not paths:
            raise ShareStoreError(
                f"No Reminders store found under {self._dir}. Either "
                "Reminders is not set up on this Mac, or the extension lacks "
                "Full Disk Access.\n\n" + full_disk_access_steps()
            )
        usable: list[tuple[sqlite3.Connection, dict[str, Any]]] = []
        errors: list[str] = []
        for path in paths:
            try:
                con = self._connect(path)
            except ShareStoreError as exc:
                errors.append(str(exc))
                continue
            schema = self._schema(path, con)
            if schema is not None:
                usable.append((con, schema))
        if not usable and errors:
            raise ShareStoreError("; ".join(errors))
        return usable

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def _schema(self, path: Path, con: sqlite3.Connection) -> Optional[dict[str, Any]]:
        key = str(path)
        with self._lock:
            cached = self._schemas.get(key)
        if cached is not None:
            return cached
        try:
            resolved = self._resolve_schema(path, con)
        except sqlite3.Error as exc:
            logger.warning("Sharing schema unreadable in %s: %s", path.name, exc)
            return None
        if resolved is not None and resolved["complete"]:
            with self._lock:
                self._schemas[key] = resolved
        return resolved

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _entity(con: sqlite3.Connection, name: str) -> Optional[int]:
        row = con.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = ?", (name,)
        ).fetchone()
        return int(row["Z_ENT"]) if row is not None else None

    @staticmethod
    def _probe(
        con: sqlite3.Connection,
        ent: int,
        cols: set[str],
        base: str,
        join_table: Optional[str] = None,
    ) -> Optional[str]:
        """Which of ``base``, ``base1`` … ``base5`` this entity's rows fill in.

        With `join_table`, the column must also point at a row there. None
        when the entity has no rows to learn from.
        """
        for col in (base, *(f"{base}{i}" for i in range(1, 6))):
            if col not in cols:
                continue
            if join_table:
                sql = (
                    f"SELECT 1 FROM {_OBJECT_TABLE} o JOIN {join_table} t "
                    f"ON t.Z_PK = o.{col} WHERE o.Z_ENT = ? LIMIT 1"
                )
            else:
                sql = (
                    f"SELECT 1 FROM {_OBJECT_TABLE} o "
                    f"WHERE o.Z_ENT = ? AND o.{col} IS NOT NULL LIMIT 1"
                )
            try:
                if con.execute(sql, (ent,)).fetchone() is not None:
                    return col
            except sqlite3.Error:
                continue
        return None

    def _resolve_schema(
        self, path: Path, con: sqlite3.Connection
    ) -> Optional[dict[str, Any]]:
        tables = {
            row["name"]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for needed in (_OBJECT_TABLE, _LIST_TABLE, _REMINDER_TABLE, "Z_PRIMARYKEY"):
            if needed not in tables:
                logger.debug("%s: no %s table, skipping", path.name, needed)
                return None

        list_cols = self._columns(con, _LIST_TABLE)
        if "ZCKIDENTIFIER" not in list_cols and "ZIDENTIFIER" not in list_cols:
            return None
        obj_cols = self._columns(con, _OBJECT_TABLE)
        rem_cols = self._columns(con, _REMINDER_TABLE)

        sharee_ent = self._entity(con, _SHAREE_ENTITY)
        assignment_ent = self._entity(con, _ASSIGNMENT_ENTITY)

        sharee: Optional[dict[str, Any]] = None
        if sharee_ent is not None and "ZCKIDENTIFIER" in obj_cols:
            sharee = {
                "ent": sharee_ent,
                "list_fk": self._probe(con, sharee_ent, obj_cols, "ZLIST", _LIST_TABLE),
                "status": self._probe(con, sharee_ent, obj_cols, "ZSTATUS"),
                "address": self._probe(con, sharee_ent, obj_cols, "ZADDRESS"),
            }

        if "ZDACALENDARITEMUNIQUEIDENTIFIER" in rem_cols:
            uuid_expr = "upper(r.ZDACALENDARITEMUNIQUEIDENTIFIER)"
        elif "ZIDENTIFIER" in rem_cols:
            uuid_expr = _UUID_FROM_HEX.format(c="r.ZIDENTIFIER")
        else:
            uuid_expr = None

        assignment: Optional[dict[str, Any]] = None
        if (
            assignment_ent is not None
            and uuid_expr is not None
            and "ZCKASSIGNEEIDENTIFIER" in obj_cols
        ):
            assignment = {
                "ent": assignment_ent,
                "reminder_fk": self._probe(
                    con, assignment_ent, obj_cols, "ZREMINDER", _REMINDER_TABLE
                ),
            }

        return {
            "path": path,
            "list_ent": self._entity(con, _LIST_ENTITY),
            "list_cols": list_cols,
            "obj_cols": obj_cols,
            "sharee": sharee,
            "assignment": assignment,
            "uuid_expr": uuid_expr,
            "obj_deleted": "ZMARKEDFORDELETION" in obj_cols,
            "rem_deleted": "ZMARKEDFORDELETION" in rem_cols,
            "list_deleted": "ZMARKEDFORDELETION" in list_cols,
            "complete": bool(
                sharee and sharee["list_fk"]
                and assignment and assignment["reminder_fk"]
            ),
        }

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @staticmethod
    def _col(cols: set[str], name: Optional[str], alias: str, prefix: str = "o") -> str:
        """`prefix.name AS alias`, or `NULL AS alias` when the column is absent."""
        if name and name in cols:
            return f"{prefix}.{name} AS {alias}"
        return f"NULL AS {alias}"

    def _read_store(
        self, con: sqlite3.Connection, schema: dict[str, Any], snap: _Snapshot
    ) -> None:
        lc, oc = schema["list_cols"], schema["obj_cols"]
        name = schema["path"].name

        # Lists ----------------------------------------------------------
        where = []
        if schema["list_ent"] is not None:
            where.append(f"l.Z_ENT = {int(schema['list_ent'])}")
        if schema["list_deleted"]:
            where.append("(l.ZMARKEDFORDELETION IS NULL OR l.ZMARKEDFORDELETION = 0)")
        list_sql = (
            f"SELECT l.Z_PK AS pk, "
            f"{self._col(lc, 'ZCKIDENTIFIER', 'ck_id', 'l')}, "
            f"{self._col(lc, 'ZIDENTIFIER', 'blob_id', 'l')}, "
            f"{self._col(lc, 'ZSHARINGSTATUS', 'sharing_status', 'l')}, "
            f"{self._col(lc, 'ZSHAREDOWNERIDENTIFIER', 'owner_id', 'l')}, "
            f"{self._col(lc, 'ZCKSERVERSHAREDATA', 'share', 'l')} "
            f"FROM {_LIST_TABLE} l"
            + (f" WHERE {' AND '.join(where)}" if where else "")
        )
        try:
            list_rows = con.execute(list_sql).fetchall()
        except sqlite3.Error as exc:
            logger.warning("List read failed for %s: %s", name, exc)
            return

        by_pk: dict[int, dict[str, Any]] = {}
        for row in list_rows:
            list_id = (str(row["ck_id"]).upper() if row["ck_id"]
                       else _uuid_from_blob(row["blob_id"]))
            if not list_id:
                continue
            by_pk[int(row["pk"])] = {
                "id": list_id,
                "status": int(row["sharing_status"] or 0),
                "owner_id": _uuid_from_blob(row["owner_id"]),
                "participants": decode_share_participants(
                    bytes(row["share"]) if row["share"] is not None else None
                ),
                "sharees": [],
            }

        # Sharees --------------------------------------------------------
        sh = schema["sharee"]
        if sh and sh["list_fk"]:
            where = [f"o.Z_ENT = {int(sh['ent'])}"]
            if schema["obj_deleted"]:
                where.append("(o.ZMARKEDFORDELETION IS NULL OR o.ZMARKEDFORDELETION = 0)")
            sql = (
                f"SELECT o.{sh['list_fk']} AS list_pk, o.ZCKIDENTIFIER AS ck_id, "
                f"{self._col(oc, 'ZIDENTIFIER', 'blob_id')}, "
                f"{self._col(oc, 'ZFIRSTNAME', 'first')}, "
                f"{self._col(oc, 'ZLASTNAME', 'last')}, "
                f"{self._col(oc, 'ZNICKNAME', 'nick')}, "
                f"{self._col(oc, 'ZDISPLAYNAME', 'display')}, "
                f"{self._col(oc, sh['address'], 'address')}, "
                f"{self._col(oc, sh['status'], 'status')} "
                f"FROM {_OBJECT_TABLE} o WHERE {' AND '.join(where)}"
            )
            try:
                for row in con.execute(sql):
                    info = by_pk.get(row["list_pk"])
                    if info is None:
                        continue
                    sharee_id = (str(row["ck_id"]).upper() if row["ck_id"]
                                 else _uuid_from_blob(row["blob_id"]))
                    if not sharee_id:
                        continue
                    info["sharees"].append((sharee_id, row))
            except sqlite3.Error as exc:
                logger.warning("Sharee read failed for %s: %s", name, exc)

        for info in by_pk.values():
            self._build_list(info, snap)

        # Assignments ----------------------------------------------------
        asg = schema["assignment"]
        if not (asg and asg["reminder_fk"] and schema["uuid_expr"]):
            return
        where = [f"o.Z_ENT = {int(asg['ent'])}"]
        if schema["obj_deleted"]:
            where.append("(o.ZMARKEDFORDELETION IS NULL OR o.ZMARKEDFORDELETION = 0)")
        if schema["rem_deleted"]:
            where.append("(r.ZMARKEDFORDELETION IS NULL OR r.ZMARKEDFORDELETION = 0)")
        sql = (
            f"SELECT {schema['uuid_expr']} AS uuid, "
            f"o.ZCKASSIGNEEIDENTIFIER AS assignee, "
            f"{self._col(oc, 'ZCKORIGINATORIDENTIFIER', 'originator')}, "
            f"{self._col(oc, 'ZASSIGNEDDATE', 'assigned')} "
            f"FROM {_OBJECT_TABLE} o "
            f"JOIN {_REMINDER_TABLE} r ON r.Z_PK = o.{asg['reminder_fk']} "
            f"WHERE {' AND '.join(where)} AND {schema['uuid_expr']} IS NOT NULL"
        )
        try:
            rows = con.execute(sql).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Assignment read failed for %s: %s", name, exc)
            return
        for row in rows:
            if not row["assignee"]:
                continue
            uuid = str(row["uuid"]).upper()
            when = _core_data_date(row["assigned"])
            current = snap.assignments.get(uuid)
            # Reminders keeps one assignment per reminder in practice, but
            # its API speaks of a "current" one; the newest wins.
            if current is not None and (
                when is None
                or (current.assigned_date is not None and current.assigned_date >= when)
            ):
                continue
            snap.assignments[uuid] = Assignment(
                assignee=self._person(snap, str(row["assignee"])),
                assigned_by=(self._person(snap, str(row["originator"]))
                             if row["originator"] else None),
                assigned_date=when,
            )

    @staticmethod
    def _person(snap: _Snapshot, sharee_id: str) -> Sharee:
        """The sharee with this id, or a bare record when it is not on file."""
        row = snap.sharees.get(sharee_id.upper())
        return row.model if row is not None else Sharee(id=sharee_id.upper())

    @staticmethod
    def _build_list(info: dict[str, Any], snap: _Snapshot) -> None:
        participants: dict[str, _Participant] = info["participants"]
        status: int = info["status"]
        owned = is_owned_by_me(status)
        rows: list[_ShareeRow] = []
        for sharee_id, row in info["sharees"]:
            first = row["first"] or None
            last = row["last"] or None
            nick = row["nick"] or None
            email, phone = _split_address(row["address"])
            name = (" ".join(p for p in (first, last) if p) or nick
                    or row["display"] or email or phone)
            part = participants.get(sharee_id)
            is_owner = (sharee_id == info["owner_id"]) if info["owner_id"] else bool(
                part and part.is_owner
            )
            state: ShareeStatus = "unknown"
            if part is not None and part.acceptance in _CK_ACCEPTANCE:
                state = _CK_ACCEPTANCE[part.acceptance]
            elif row["status"] is not None:
                state = _REM_SHAREE_STATUS.get(int(row["status"]), "unknown")
            rows.append(_ShareeRow(
                model=Sharee(
                    id=sharee_id, name=name, email=email, phone=phone,
                    status=state, is_owner=is_owner,
                    is_me=bool(part and part.is_me),
                ),
                list_id=info["id"],
                first_name=first,
                nickname=nick,
            ))

        # Without a readable share, fall back to what the list alone says.
        if rows and not any(r.model.is_me for r in rows):
            if owned:
                for r in rows:
                    r.model.is_me = r.model.is_owner
            elif status == SHARING_STATUS_SHARED_TO_ME:
                others = [r for r in rows if not r.model.is_owner]
                if len(others) == 1:
                    others[0].model.is_me = True

        rows.sort(key=lambda r: (not r.model.is_owner, (r.model.name or "").casefold()))
        for r in rows:
            snap.sharees[r.model.id] = r
        owner = next((r.model for r in rows if r.model.is_owner), None)
        snap.lists[info["id"]] = ListSharing(
            is_shared=is_shared(len(rows), status),
            owned_by_me=owned,
            owner=owner,
            sharees=[r.model for r in rows],
        )

    def _snapshot(self) -> _Snapshot:
        """Everything this module knows, read fresh from every store.

        Nothing is cached across calls: shared lists change from other
        people's devices, and a stale answer is the bug to avoid.
        """
        snap = _Snapshot()
        for con, schema in self._usable_stores():
            self._read_store(con, schema, snap)
        return snap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sharing_by_list(self) -> dict[str, ListSharing]:
        """list id (upper case) → its sharing, for every list in the store."""
        return self._snapshot().lists

    def sharing_for_list(self, list_id: str) -> Optional[ListSharing]:
        """One list's sharing. None when the list is not in the store."""
        return self.sharing_by_list().get(list_id.strip().upper())

    def assignments(self) -> dict[str, Assignment]:
        """reminder id (upper case) → its current assignment."""
        return self._snapshot().assignments

    def assignment_for_reminder(self, reminder_id: str) -> Optional[Assignment]:
        """One reminder's assignment, or None when it is not assigned."""
        return self.assignments().get(reminder_id.strip().upper())

    def reminders_assigned(self, who: Optional[str] = None) -> set[str]:
        """Ids of assigned reminders; with `who`, only those assigned to them.

        `who` is "me", a sharee id, an email address, a phone number, or a
        name, matched case-insensitively against every shared list. The
        same person is a different sharee on each list, so a name matches
        across all of them.
        """
        snap = self._snapshot()
        if who is None:
            return set(snap.assignments)
        ids = {sid for sid, row in snap.sharees.items() if row.matches(who)}
        return {
            rid for rid, a in snap.assignments.items() if a.assignee.id in ids
        }

    def resolve_sharee(self, list_id: str, who: str) -> Sharee:
        """The one member of a shared list that `who` names.

        Raises ValueError when the list is not shared, or when `who`
        matches nobody or more than one person.
        """
        snap = self._snapshot()
        lid = list_id.strip().upper()
        sharing = snap.lists.get(lid)
        if sharing is None or not sharing.is_shared:
            raise ValueError(
                "This reminder's list is not shared, and only reminders on "
                "a shared iCloud list can be assigned."
            )
        rows = [r for r in snap.sharees.values() if r.list_id == lid]
        hits = [r.model for r in rows if r.matches(who)]
        if len(hits) == 1:
            return hits[0]
        people = "; ".join(
            f"{m.name or '(no name)'} [{m.id}]"
            + (" (you)" if m.is_me else "")
            + ("" if m.status == "accepted" else f" ({m.status})")
            for m in (r.model for r in rows)
        )
        if not hits:
            raise ValueError(
                f"Nobody on this list matches {who!r}. Members: {people}."
            )
        raise ValueError(
            f"{who!r} matches more than one member of this list; pass a "
            f"sharee id instead. Members: {people}."
        )

    def me_on_list(self, list_id: str) -> Optional[Sharee]:
        """Your own sharee record on a shared list, when it can be told."""
        sharing = self.sharing_for_list(list_id)
        if sharing is None:
            return None
        return next((s for s in sharing.sharees if s.is_me), None)
