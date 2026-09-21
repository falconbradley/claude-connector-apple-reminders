"""
Write path for real Apple Reminders tags, via private ReminderKit.

Read this before changing anything here
---------------------------------------
This module uses **private Apple API**. That is a deliberate, considered
choice, not an oversight, because there is no alternative:

* EventKit has no tag concept at all — not public, not private. A symbol
  scan of the framework turns up nothing.
* The Reminders AppleScript dictionary has no tag property.
* Shortcuts / App Intents expose no tag parameter (checked the extracted
  action metadata in Reminders.app and RemindersAppIntents.framework).
* Writing the Core Data store directly would mean hand-maintaining
  ``ZCKDIRTYFLAGS``, the ``ACHANGE``/``ATRANSACTION`` history and the
  ``Z_PRIMARYKEY`` counters while Reminders.app holds the file open. That
  corrupts CloudKit sync bookkeeping. It is not on the table.

``ReminderKit`` is the framework Reminders.app itself uses, and it has a
first-class tag API. We drive it exactly as the app does: fetch the
reminder, open a save request, mutate the hashtag context, save. Apple
performs the write, so sync, change tracking and validation stay Apple's
job, not ours.

The cost is that Apple owes us nothing here. Any macOS update can rename
or drop a selector. The mitigation is `_check_capabilities()`, which
verifies every class and selector this module needs *before* touching
anything, and fails with a message naming exactly what went missing. It
is far better to refuse loudly on macOS 28 than to half-apply a change.

Reading tags does not go through here — that is tagstore.py, which reads
the store file directly and needs no private API.

The API we drive
----------------
::

    store = REMStore()
    reminder = store.fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:
    request  = REMSaveRequest(store)
    change   = request.updateReminder:(reminder)        -> REMReminderChangeItem
    context  = change.hashtagContext                    -> ...ContextChangeItem
    context.addHashtagWithType:name:(0, "autoreview")
    context.removeHashtag:(hashtag)
    request.saveSynchronouslyWithError:

``inList:`` is nil to search every list. The identifier is the same
``ZDACALENDARITEMUNIQUEIDENTIFIER`` that EventKit reports as
``calendarItemIdentifier``, so reminder ids need no translation.

Hashtag type 0 is a plain tag; every live row in the local store uses it.

PyObjC has no metadata for a private framework, so it cannot know that
``error:`` is an out-parameter. We register that ourselves — without it
the NSError is silently dropped and every failure looks identical.

Permission
----------
``REMStore`` talks to ``remindd`` over XPC, gated by the same Reminders
TCC grant EventKit uses. A process without it constructs a store happily
and then fails every call with ``NSCocoaError 4097 "connection to service
named com.apple.remindd"``. That is reported as a permission problem, not
as a mysterious nil.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("apple_reminders_mcp.tagwriter")

_FRAMEWORK = "/System/Library/PrivateFrameworks/ReminderKit.framework"

# REMHashtagType. 0 is a plain tag; every live hashtag row in the local
# store carries it. (1 appears only on soft-deleted rows.)
_HASHTAG_TYPE_TAG = 0

# Class → selectors this module calls. Checked up front so a macOS change
# produces one clear error instead of a half-applied write.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "REMStore": (
        "fetchReminderWithDACalendarItemUniqueIdentifier_inList_error_",
    ),
    "REMSaveRequest": (
        "initWithStore_",
        "updateReminder_",
        "saveSynchronouslyWithError_",
    ),
    "REMReminderChangeItem": ("hashtagContext",),
    "REMReminderHashtagContextChangeItem": (
        "addHashtagWithType_name_",
        "removeHashtag_",
        "hashtags",
    ),
    "REMHashtag": ("name",),
}

# NSCocoaErrorDomain code for a refused/unavailable XPC connection.
_XPC_CONNECTION_INVALID = 4097


class TagWriteError(RuntimeError):
    """A tag write could not be completed."""


class TagWriteUnavailable(TagWriteError):
    """The write path itself is unusable on this machine.

    Either ReminderKit could not be loaded / no longer matches what we
    call, or the host process lacks Reminders permission.
    """


def normalise_tag_name(name: str) -> str:
    """Strip a leading '#' and surrounding space from a user-supplied tag."""
    return name.strip().lstrip("#").strip()


class RemindersTagWriter:
    """Adds and removes real tags on reminders, through ReminderKit.

    One instance owns one ``REMStore``. Calls are serialised: a save
    request is bound to its store, and interleaving two of them on one
    store is not something the framework promises to tolerate.
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
        """Load ReminderKit, register out-error metadata, check selectors."""
        if self._loaded:
            if self._load_error:
                raise TagWriteUnavailable(self._load_error)
            return
        try:
            import objc  # type: ignore
            from Foundation import NSBundle  # type: ignore
        except ImportError as exc:  # pragma: no cover - PyObjC is a hard dep
            self._loaded = True
            self._load_error = f"PyObjC not available: {exc}"
            raise TagWriteUnavailable(self._load_error) from exc

        bundle = NSBundle.bundleWithPath_(_FRAMEWORK)
        if bundle is None or not bundle.load():
            self._loaded = True
            self._load_error = (
                f"Could not load {_FRAMEWORK}. Writing tags needs Apple's "
                "private ReminderKit framework, which this macOS release "
                "does not appear to provide."
            )
            raise TagWriteUnavailable(self._load_error)

        # PyObjC has no metadata for a private framework, so `error:` would
        # be treated as a plain input and the NSError thrown away. Declare
        # it as an out-parameter so failures arrive with a reason attached.
        for cls_name, selector in (
            (b"REMStore",
             b"fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:"),
            (b"REMSaveRequest", b"saveSynchronouslyWithError:"),
        ):
            index = selector.count(b":") - 1  # the trailing error: argument
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
                "tag writer expects — missing: " + ", ".join(missing) + ". "
                "Writing tags uses private API, so an OS update can break "
                "it; reading tags is unaffected. Please file an issue."
            )
            raise TagWriteUnavailable(self._load_error)

        self._loaded = True

    def _check_capabilities(self, objc: Any) -> list[str]:
        """Return a list of missing class/selector names, empty when fine."""
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
                raise TagWriteUnavailable("Could not create a REMStore.")
        return self._store

    def available(self) -> bool:
        """True when the write path can load. Says nothing about permission."""
        try:
            self._load()
            return True
        except TagWriteUnavailable:
            return False

    def unavailable_reason(self) -> Optional[str]:
        try:
            self._load()
            return None
        except TagWriteUnavailable as exc:
            return str(exc)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch(self, store: Any, reminder_id: str) -> Any:
        result = store.fetchReminderWithDACalendarItemUniqueIdentifier_inList_error_(
            reminder_id, None, None
        )
        # With our metadata this is (reminder, error); without it, bare nil.
        if isinstance(result, tuple):
            reminder, error = result
        else:  # pragma: no cover - only if metadata registration failed
            reminder, error = result, None

        if reminder is not None:
            return reminder
        if error is not None:
            domain = str(error.domain()) if hasattr(error, "domain") else ""
            code = int(error.code()) if hasattr(error, "code") else 0
            if code == _XPC_CONNECTION_INVALID:
                raise TagWriteUnavailable(
                    "Could not reach the Reminders daemon (remindd). The "
                    "host process almost certainly lacks Reminders "
                    "permission — grant it under System Settings → Privacy "
                    "& Security → Reminders. "
                    f"({domain} {code})"
                )
            raise TagWriteError(
                f"Could not load reminder {reminder_id}: {error}"
            )
        raise TagWriteError(f"Reminder not found: {reminder_id}")

    @staticmethod
    def _names(hashtags: Any) -> list[str]:
        out = []
        for h in (hashtags or []):
            try:
                name = h.name()
            except Exception:
                continue
            if name:
                out.append(str(name))
        return out

    def current_tags(self, reminder_id: str) -> list[str]:
        """Read a reminder's tags through ReminderKit.

        tagstore.py is the normal way to read tags — it needs no private
        API. This exists so a write can confirm its own result against the
        same source it wrote to, rather than against a file that may not
        have been flushed yet.
        """
        with self._lock:
            store = self._rem_store()
            reminder = self._fetch(store, reminder_id)
            context = reminder.hashtagContext()
            if context is None:
                return []
            return sorted(self._names(context.hashtags()), key=str.casefold)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _apply(
        self,
        reminder_id: str,
        add: list[str],
        remove: list[str],
        replace_with: Optional[list[str]] = None,
    ) -> list[str]:
        """Mutate one reminder's tags and save. Returns the resulting tags."""
        store = self._rem_store()
        reminder = self._fetch(store, reminder_id)

        request = self._classes["REMSaveRequest"].alloc().initWithStore_(store)
        if request is None:
            raise TagWriteError("Could not create a REMSaveRequest.")

        change = request.updateReminder_(reminder)
        if change is None:
            raise TagWriteError(
                f"ReminderKit refused to open {reminder_id} for editing."
            )
        context = change.hashtagContext()
        if context is None:
            raise TagWriteError(
                f"Reminder {reminder_id} has no hashtag context to edit."
            )

        existing = list(context.hashtags() or [])
        by_fold = {str(h.name()).casefold(): h for h in existing if h.name()}

        changed = False

        if replace_with is not None:
            # Diff rather than clear-and-re-add: removing a tag only to
            # put it straight back churns Reminders' change tracking and
            # CloudKit for no reason.
            wanted = {n.casefold() for n in replace_with}
            for fold, h in list(by_fold.items()):
                if fold not in wanted:
                    context.removeHashtag_(h)
                    del by_fold[fold]
                    changed = True
            add = replace_with
        else:
            for name in remove:
                h = by_fold.pop(normalise_tag_name(name).casefold(), None)
                if h is not None:
                    context.removeHashtag_(h)
                    changed = True

        for name in add:
            clean = normalise_tag_name(name)
            if not clean:
                continue
            # Sanitise *before* the duplicate check: ReminderKit may
            # rewrite the name, and the rewritten form is what would
            # collide with an existing tag.
            safe = self._sanitise(context, clean)
            if not safe:
                continue
            # Adding a tag that is already there would create a duplicate
            # row; Reminders.app never does. Match case-insensitively,
            # since Apple matches on a case-folded canonical name.
            if safe.casefold() in by_fold:
                continue
            context.addHashtagWithType_name_(_HASHTAG_TYPE_TAG, safe)
            by_fold[safe.casefold()] = None
            changed = True

        if not changed:
            # Nothing to do. Don't save — an empty save request still bumps
            # the reminder's modification date and wakes up CloudKit.
            return sorted(
                self._names(context.hashtags()), key=str.casefold
            )

        result = request.saveSynchronouslyWithError_(None)
        if isinstance(result, tuple):
            ok, error = result
        else:  # pragma: no cover - only if metadata registration failed
            ok, error = bool(result), None
        if not ok:
            if error is not None and getattr(error, "code", None) is not None:
                if int(error.code()) == _XPC_CONNECTION_INVALID:
                    raise TagWriteUnavailable(
                        "Could not reach the Reminders daemon (remindd) to "
                        "save. The host process lacks Reminders permission."
                    )
            raise TagWriteError(
                f"Saving tags for {reminder_id} failed: "
                f"{error if error is not None else 'unknown error'}"
            )

        # Read back from the store rather than trusting the change item:
        # the point of this feature is that the connector reports what is
        # really there.
        reminder = self._fetch(store, reminder_id)
        context = reminder.hashtagContext()
        names = self._names(context.hashtags()) if context is not None else []
        return sorted(names, key=str.casefold)

    @staticmethod
    def _sanitise(context: Any, name: str) -> str:
        """Let ReminderKit strip characters it will not accept in a tag."""
        fn = getattr(context, "nameWithDisallowedCharactersReplaced_", None)
        if fn is None:
            return name
        try:
            cleaned = fn(name)
        except Exception:
            return name
        return str(cleaned) if cleaned else name

    def add_tags(self, reminder_id: str, tags: list[str]) -> list[str]:
        """Add tags, creating any that do not exist yet. Idempotent."""
        names = [normalise_tag_name(t) for t in tags]
        names = [n for n in names if n]
        if not names:
            raise ValueError("No tag names given.")
        with self._lock:
            return self._apply(reminder_id, add=names, remove=[])

    def remove_tags(self, reminder_id: str, tags: list[str]) -> list[str]:
        """Remove tags. Names not present are ignored."""
        names = [normalise_tag_name(t) for t in tags]
        names = [n for n in names if n]
        if not names:
            raise ValueError("No tag names given.")
        with self._lock:
            return self._apply(reminder_id, add=[], remove=names)

    def set_tags(self, reminder_id: str, tags: list[str]) -> list[str]:
        """Replace a reminder's tags outright. An empty list clears them."""
        names = [normalise_tag_name(t) for t in tags]
        names = [n for n in names if n]
        with self._lock:
            return self._apply(reminder_id, add=[], remove=[], replace_with=names)
