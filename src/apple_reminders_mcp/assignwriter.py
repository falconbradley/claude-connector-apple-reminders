"""
Write path for reminder assignments, via private ReminderKit.

Read tagwriter.py first
-----------------------
Everything said there about private API applies here unchanged. Nothing
supported can assign a reminder: EventKit has no assignee, AppleScript
has none, and Shortcuts exposes no parameter for it. Reminders.app
assigns through ReminderKit, so this module does the same, and every
class and selector it uses is declared in ``_REQUIRED`` and verified
before anything is touched.

The API we drive
----------------
::

    store    = REMStore()
    reminder = store.fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:
    sharees  = reminder.list.shareeContext.sharees          -> [REMSharee]
    request  = REMSaveRequest(store)
    change   = request.updateReminder:(reminder)            -> REMReminderChangeItem
    context  = change.assignmentContext                     -> ...ContextChangeItem
    context.removeAllAssignments
    context.addAssignmentWithAssignee:originator:status:(assignee, me, 1)
    request.saveSynchronouslyWithError:

A ``REMSharee``'s ``objectID.uuid`` is the same participant id
sharestore.py reads from the store, so ids need no translation. Who is
assigning (the originator) is you: sharestore.py works that out from the
list's CloudKit share and hands the id in. Status 1 is what all 17 real
assignment rows carry, so it is what is written.

Reminders keeps one assignment per reminder (every assigned reminder in
the real store has exactly one row), so reassigning clears the old one
before adding the new one, rather than stacking a second.

Assigning tells the assignee
----------------------------
This is not a private edit. Reminders notifies the person a reminder is
assigned to, on their own devices, the same as assigning it in the app.
The tool description says so.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("apple_reminders_mcp.assignwriter")

_FRAMEWORK = "/System/Library/PrivateFrameworks/ReminderKit.framework"

# Class → selectors this module calls. Checked up front so a macOS change
# produces one clear error instead of a half-applied write.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "REMStore": (
        "fetchReminderWithDACalendarItemUniqueIdentifier_inList_error_",
        "fetchListWithObjectID_error_",
    ),
    "REMSaveRequest": (
        "initWithStore_",
        "updateReminder_",
        "saveSynchronouslyWithError_",
    ),
    "REMReminder": ("list", "storage", "assignmentContext"),
    "REMReminderStorage": ("listID",),
    "REMList": ("shareeContext",),
    "REMListShareeContext": ("sharees",),
    "REMSharee": ("objectID",),
    "REMObjectID": ("uuid",),
    "REMReminderAssignmentContext": ("currentAssignment",),
    "REMAssignment": ("assigneeID",),
    "REMReminderChangeItem": ("assignmentContext",),
    "REMReminderAssignmentContextChangeItem": (
        "addAssignmentWithAssignee_originator_status_",
        "removeAllAssignments",
    ),
}

# REMAssignmentStatus carried by every live assignment row.
_ASSIGNMENT_STATUS = 1

# NSCocoaErrorDomain code for a refused/unavailable XPC connection.
_XPC_CONNECTION_INVALID = 4097


class AssignWriteError(RuntimeError):
    """An assignment write could not be completed."""


class AssignWriteUnavailable(AssignWriteError):
    """The write path itself is unusable on this machine.

    Either ReminderKit could not be loaded / no longer matches what we
    call, or the host process lacks Reminders permission.
    """


def _uuid(object_id: Any) -> Optional[str]:
    """A REMObjectID's UUID as an upper-case string."""
    if object_id is None:
        return None
    try:
        return str(object_id.uuid().UUIDString()).upper()
    except Exception:
        return None


class RemindersAssignWriter:
    """Assigns and unassigns reminders on shared lists, through ReminderKit.

    One instance owns one ``REMStore``; calls are serialised for the same
    reason tagwriter.py serialises its own.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._classes: dict[str, Any] = {}
        self._store: Any = None
        self._load_error: Optional[str] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Framework bootstrap
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            if self._load_error:
                raise AssignWriteUnavailable(self._load_error)
            return
        try:
            import objc  # type: ignore
            from Foundation import NSBundle  # type: ignore
        except ImportError as exc:  # pragma: no cover - PyObjC is a hard dep
            self._loaded = True
            self._load_error = f"PyObjC not available: {exc}"
            raise AssignWriteUnavailable(self._load_error) from exc

        bundle = NSBundle.bundleWithPath_(_FRAMEWORK)
        if bundle is None or not bundle.load():
            self._loaded = True
            self._load_error = (
                f"Could not load {_FRAMEWORK}. Assigning reminders needs "
                "Apple's private ReminderKit framework, which this macOS "
                "release does not appear to provide."
            )
            raise AssignWriteUnavailable(self._load_error)

        # Declare the trailing error: arguments as out-parameters, or the
        # NSError is dropped and every failure looks identical.
        for cls_name, selector in (
            (b"REMStore",
             b"fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:"),
            (b"REMStore", b"fetchListWithObjectID:error:"),
            (b"REMSaveRequest", b"saveSynchronouslyWithError:"),
        ):
            index = selector.count(b":") - 1
            try:
                objc.registerMetaDataForSelector(
                    cls_name, selector,
                    {"arguments": {2 + index: {
                        "type_modifier": objc._C_OUT, "null_accepted": True,
                    }}},
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("metadata registration failed for %s: %s",
                             selector, exc)

        missing = self._check_capabilities(objc)
        if missing:
            self._loaded = True
            self._load_error = (
                "This macOS release's ReminderKit does not match what the "
                "assignment writer expects — missing: " + ", ".join(missing)
                + ". Assigning uses private API, so an OS update can break "
                "it; reading assignments is unaffected. Please file an issue."
            )
            raise AssignWriteUnavailable(self._load_error)

        self._loaded = True

    def _check_capabilities(self, objc: Any) -> list[str]:
        missing: list[str] = []
        for cls_name, selectors in _REQUIRED.items():
            try:
                cls = objc.lookUpClass(cls_name)
            except Exception:
                missing.append(cls_name)
                continue
            self._classes[cls_name] = cls
            for sel in selectors:
                if not hasattr(cls, sel):
                    missing.append(f"{cls_name}.{sel}")
        return missing

    def _rem_store(self) -> Any:
        self._load()
        if self._store is None:
            self._store = self._classes["REMStore"].alloc().init()
            if self._store is None:
                raise AssignWriteUnavailable("Could not create a REMStore.")
        return self._store

    def available(self) -> bool:
        try:
            self._load()
            return True
        except AssignWriteUnavailable:
            return False

    def unavailable_reason(self) -> Optional[str]:
        try:
            self._load()
            return None
        except AssignWriteUnavailable as exc:
            return str(exc)

    # ------------------------------------------------------------------
    # Fetch / read-back
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for(error: Any, what: str) -> None:
        domain = str(error.domain()) if hasattr(error, "domain") else ""
        code = int(error.code()) if hasattr(error, "code") else 0
        if code == _XPC_CONNECTION_INVALID:
            raise AssignWriteUnavailable(
                "Could not reach the Reminders daemon (remindd). The host "
                "process almost certainly lacks Reminders permission — grant "
                "it under System Settings → Privacy & Security → Reminders. "
                f"({domain} {code})"
            )
        raise AssignWriteError(f"Could not load {what}: {error}")

    def _fetch(self, store: Any, reminder_id: str) -> Any:
        result = store.fetchReminderWithDACalendarItemUniqueIdentifier_inList_error_(
            reminder_id, None, None
        )
        if isinstance(result, tuple):
            reminder, error = result
        else:  # pragma: no cover - only if metadata registration failed
            reminder, error = result, None
        if reminder is not None:
            return reminder
        if error is not None:
            self._raise_for(error, f"reminder {reminder_id}")
        raise AssignWriteError(f"Reminder not found: {reminder_id}")

    def _list_of(self, store: Any, reminder: Any) -> Any:
        """The reminder's REMList, fetched by id when it is not attached."""
        lst = reminder.list()
        if lst is not None:
            return lst
        storage = reminder.storage()
        list_id = storage.listID() if storage is not None else None
        if list_id is None:
            raise AssignWriteError("ReminderKit reports no list for this reminder.")
        result = store.fetchListWithObjectID_error_(list_id, None)
        if isinstance(result, tuple):
            lst, error = result
        else:  # pragma: no cover - only if metadata registration failed
            lst, error = result, None
        if lst is None:
            if error is not None:
                self._raise_for(error, "the reminder's list")
            raise AssignWriteError("Could not load the reminder's list.")
        return lst

    @staticmethod
    def _current(reminder: Any) -> Optional[str]:
        context = reminder.assignmentContext()
        if context is None:
            return None
        current = context.currentAssignment()
        return _uuid(current.assigneeID()) if current is not None else None

    def current_assignee(self, reminder_id: str) -> Optional[str]:
        """The assignee's sharee id, read through ReminderKit.

        sharestore.py is the normal read path. This exists so a write can
        confirm its result against the source it wrote to, rather than
        against a file remindd may not have flushed yet.
        """
        with self._lock:
            store = self._rem_store()
            return self._current(self._fetch(store, reminder_id))

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _apply(
        self,
        reminder_id: str,
        assignee_id: Optional[str],
        originator_id: Optional[str],
    ) -> Optional[str]:
        """Assign (or, with None, unassign) and read the result back."""
        store = self._rem_store()
        reminder = self._fetch(store, reminder_id)

        if self._current(reminder) == assignee_id:
            # Nothing to do. Don't save — an empty save request still bumps
            # the modification date, wakes CloudKit and can re-notify.
            return assignee_id

        assignee = originator = None
        if assignee_id is not None:
            # ReminderKit is the authority on who is on the list: the store
            # file may lag behind a membership change.
            context = self._list_of(store, reminder).shareeContext()
            sharees = list(context.sharees() or []) if context is not None else []
            by_id = {_uuid(s.objectID()): s for s in sharees}
            assignee = by_id.get(assignee_id)
            if assignee is None:
                raise AssignWriteError(
                    f"ReminderKit does not list sharee {assignee_id} as a "
                    "member of this reminder's list."
                )
            originator = by_id.get(originator_id) if originator_id else None
            if originator is None:
                raise AssignWriteError(
                    "Could not tell which member of this list is you, so the "
                    "assignment would have no sender. Nothing was changed."
                )

        request = self._classes["REMSaveRequest"].alloc().initWithStore_(store)
        if request is None:
            raise AssignWriteError("Could not create a REMSaveRequest.")
        change = request.updateReminder_(reminder)
        if change is None:
            raise AssignWriteError(
                f"ReminderKit refused to open reminder {reminder_id} for update."
            )
        context = change.assignmentContext()
        if context is None:
            raise AssignWriteError(
                f"Reminder {reminder_id} has no assignment context to edit."
            )
        context.removeAllAssignments()
        if assignee is not None:
            context.addAssignmentWithAssignee_originator_status_(
                assignee, originator, _ASSIGNMENT_STATUS
            )

        result = request.saveSynchronouslyWithError_(None)
        if isinstance(result, tuple):
            ok, error = result
        else:  # pragma: no cover - only if metadata registration failed
            ok, error = bool(result), None
        if not ok:
            if error is not None and getattr(error, "code", None) is not None:
                if int(error.code()) == _XPC_CONNECTION_INVALID:
                    raise AssignWriteUnavailable(
                        "Could not reach the Reminders daemon (remindd) to "
                        "save. The host process lacks Reminders permission."
                    )
            raise AssignWriteError(
                f"Saving the assignment for {reminder_id} failed: "
                f"{error if error is not None else 'unknown error'}"
            )

        # Read back from the store rather than trusting the change item.
        return self._current(self._fetch(store, reminder_id))

    def assign(self, reminder_id: str, assignee_id: str, originator_id: str) -> str:
        """Assign to one list member, replacing any current assignee."""
        wanted = assignee_id.strip().upper()
        with self._lock:
            result = self._apply(reminder_id, wanted, originator_id.strip().upper())
        if result != wanted:
            raise AssignWriteError(
                f"ReminderKit saved without error but reminder {reminder_id} "
                f"reports assignee {result!r} afterwards, not {wanted!r}."
            )
        return result

    def unassign(self, reminder_id: str) -> None:
        """Remove the assignment. An unassigned reminder is left alone."""
        with self._lock:
            result = self._apply(reminder_id, None, None)
        if result is not None:
            raise AssignWriteError(
                f"ReminderKit saved without error but reminder {reminder_id} "
                f"is still assigned to {result!r}."
            )
