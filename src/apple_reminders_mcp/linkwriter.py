"""
Write path for a reminder's linked content — the Mail / Messages chip.

Read tagwriter.py first
-----------------------
Everything said there about private API applies here unchanged. This
module drives Apple's private ``ReminderKit`` because nothing supported
can set linked content: EventKit has no notion of it (its ``URL`` field
is a different, never-displayed column), AppleScript has none, and
Shortcuts exposes no parameter for it. Only Siri and the share sheet
write it, and they go through ReminderKit.

Every class and selector used is declared in ``_REQUIRED`` and verified
before anything is touched. If a macOS update moves the interface, the
tools refuse with a message naming what went missing.

The API we drive
----------------
::

    store    = REMStore()
    reminder = store.fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:
    request  = REMSaveRequest(store)
    change   = request.updateReminder:(reminder)          -> REMReminderChangeItem
    activity = REMUserActivity initWithUniversalLink:     (Mail, web)
             | REMUserActivity initWithUserActivity:      (Messages)
    change.setUserActivity:(activity)   # forwarded to its REMReminderStorage
    request.saveSynchronouslyWithError:

``REMReminderChangeItem`` does not declare ``setUserActivity:`` itself;
it forwards unknown selectors to its ``storage`` (a
``REMReminderStorage``, which does declare it). PyObjC cannot see
forwarded selectors, so the setter is tried on the change item first and
then on ``change.storage()`` directly. The read-back after the save goes
through a fresh fetch, so what the tool reports is what remindd holds.

What gets written
-----------------
Decoded from Apple's own rows (see linkstore.py):

* **Mail** — ``initWithUniversalLink:`` with the message's Message-ID URL
  in Apple's spelling, ``message:%3C<id>%3E``. ``type`` comes out 1 and
  the archive is byte-for-byte the shape Apple writes. Both this form and
  ``message://…`` open the right message in Mail.app; the connector
  canonicalises to Apple's.

* **Messages** — an ``NSUserActivity`` of type ``com.apple.Messages`` whose
  ``targetContentIdentifier`` (and ``__kIMChatRegistryContinuityURLKey``
  in ``userInfo``) is a ``messages://open?…`` URL, wrapped with
  ``initWithUserActivity:`` (``type`` 2). Verified with Messages.app on
  2026-09-21: ``groupid=chat<digits>`` opens a group chat,
  ``addresses=<handle>`` (and ``groupid=<handle>``) opens a 1:1 chat.
  There is no per-message link — Apple's own chip is chat-level too.

* **Web** — any ``http(s)://`` URL via ``initWithUniversalLink:``.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, unquote

from .linkstore import (
    MESSAGES_ACTIVITY_TYPE,
    TYPE_UNIVERSAL_LINK,
    TYPE_USER_ACTIVITY,
    kind_for_url,
)
from .models import LinkedContent

logger = logging.getLogger("apple_reminders_mcp.linkwriter")

_FRAMEWORK = "/System/Library/PrivateFrameworks/ReminderKit.framework"

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
    "REMReminder": ("storage",),
    "REMReminderChangeItem": ("storage",),
    "REMReminderStorage": ("userActivity", "setUserActivity_"),
    "REMUserActivity": (
        "initWithUniversalLink_",
        "initWithUserActivity_",
        "type",
        "storage",
        "universalLink",
        "userActivity",
    ),
}

# The key Messages.app itself puts in the activity's userInfo.
_MESSAGES_CONTINUITY_KEY = "__kIMChatRegistryContinuityURLKey"

# NSCocoaErrorDomain code for a refused/unavailable XPC connection.
_XPC_CONNECTION_INVALID = 4097

# A chat guid as the Messages connector (and chat.db) spells it:
# "<service>;<-|+>;<identifier>", '-' for a 1:1 chat, '+' for a group.
_CHAT_GUID = re.compile(r"^(?:any|imessage|sms|rcs|auto)\s*;\s*([-+])\s*;\s*(.+)$", re.I)
_GROUP_ID = re.compile(r"^chat\d+$", re.I)
_PHONE = re.compile(r"^\+\d{5,}$")

# Characters Apple leaves literal in a Message-ID URL (observed in its own
# rows: '@', '+', '=', '_', '-', '.'), plus the rest of RFC 3986's sub-delims.
_MESSAGE_ID_SAFE = "@+=!$&'()*,;:/~"


class LinkWriteError(RuntimeError):
    """A linked-content write could not be completed."""


class LinkWriteUnavailable(LinkWriteError):
    """The write path itself is unusable on this machine.

    Either ReminderKit could not be loaded / no longer matches what we
    call, or the host process lacks Reminders permission.
    """


@dataclass(frozen=True)
class LinkSpec:
    """A parsed, canonical link, ready to become a REMUserActivity."""
    kind: str                  # "mail" | "messages" | "web"
    url: str
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# Parsing — pure Python, no ObjC
# ---------------------------------------------------------------------------

def mail_url_for_message_id(message_id: str) -> str:
    """Apple's spelling of a Mail deep link: ``message:%3C<id>%3E``."""
    mid = message_id.strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1].strip()
    if not mid or "@" not in mid:
        raise ValueError(f"Not a Mail Message-ID: {message_id!r}")
    return "message:%3C" + quote(mid, safe=_MESSAGE_ID_SAFE) + "%3E"


def messages_url_for_chat(identifier: str, is_group: bool) -> str:
    """The URL Messages.app opens for a chat.

    Groups use the ``groupid=chat…`` form Messages itself writes; 1:1
    chats use ``addresses=<handle>``. Both were checked against
    Messages.app on 2026-09-21.
    """
    ident = identifier.strip()
    if not ident:
        raise ValueError("Empty chat identifier.")
    if is_group:
        return f"messages://open?groupid={ident}"
    return f"messages://open?addresses={ident}"


def parse_link(link: str, title: Optional[str] = None) -> LinkSpec:
    """Turn what a caller hands us into a canonical LinkSpec.

    Accepted forms:

    * Mail: ``message:<id>``, ``message://<id>``, either with the angle
      brackets literal or percent-encoded (the Mail connector's
      ``mail_link`` is ``message://%3C…%40…%3E``), or a bare ``<id@host>``.
    * Messages: a chat guid as the Messages connector reports it
      (``any;-;+15551234567``, ``iMessage;+;chat123…``), a bare
      ``chat<digits>`` group id, a bare ``+<digits>`` phone handle, an
      ``imessage:`` / ``sms:`` handle, or a ready ``messages://`` URL.
    * Web: any ``http://`` or ``https://`` URL.
    """
    s = (link or "").strip()
    clean_title = title.strip() if title and title.strip() else None
    if not s:
        raise ValueError("`link` must be a non-empty string.")
    lower = s.lower()

    # --- Mail ------------------------------------------------------------
    if lower.startswith("message:"):
        rest = s[len("message:"):]
        if rest.startswith("//"):
            rest = rest[2:]
        return LinkSpec("mail", mail_url_for_message_id(unquote(rest)), clean_title)
    if s.startswith("<") and s.endswith(">") and "@" in s:
        return LinkSpec("mail", mail_url_for_message_id(s), clean_title)

    # --- Messages --------------------------------------------------------
    if lower.startswith("messages://"):
        return LinkSpec("messages", s, clean_title)
    m = _CHAT_GUID.match(s)
    if m:
        ident = m.group(2).strip()
        return LinkSpec(
            "messages",
            messages_url_for_chat(ident, is_group=(m.group(1) == "+")),
            clean_title or ident,
        )
    if _GROUP_ID.match(s):
        return LinkSpec("messages", messages_url_for_chat(s, True), clean_title or s)
    if _PHONE.match(s):
        return LinkSpec("messages", messages_url_for_chat(s, False), clean_title or s)
    for scheme in ("imessage:", "sms:"):
        if lower.startswith(scheme):
            handle = s[len(scheme):].lstrip("/").strip()
            return LinkSpec(
                "messages", messages_url_for_chat(handle, False), clean_title or handle
            )

    # --- Web -------------------------------------------------------------
    if lower.startswith(("http://", "https://")):
        return LinkSpec("web", s, clean_title)

    raise ValueError(
        f"Unrecognised link {link!r}. Accepted: a Mail message URL "
        "(message:<Message-ID> or message://<Message-ID>), a Messages chat "
        "guid (any;-;+15551234567 or any;+;chat123…) or messages:// URL, "
        "or an http(s):// URL."
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class RemindersLinkWriter:
    """Sets and clears linked content on reminders, through ReminderKit.

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
                raise LinkWriteUnavailable(self._load_error)
            return
        try:
            import objc  # type: ignore
            from Foundation import NSBundle  # type: ignore
        except ImportError as exc:  # pragma: no cover - PyObjC is a hard dep
            self._loaded = True
            self._load_error = f"PyObjC not available: {exc}"
            raise LinkWriteUnavailable(self._load_error) from exc

        bundle = NSBundle.bundleWithPath_(_FRAMEWORK)
        if bundle is None or not bundle.load():
            self._loaded = True
            self._load_error = (
                f"Could not load {_FRAMEWORK}. Setting linked content needs "
                "Apple's private ReminderKit framework, which this macOS "
                "release does not appear to provide."
            )
            raise LinkWriteUnavailable(self._load_error)

        # Declare the trailing error: arguments as out-parameters, or the
        # NSError is dropped and every failure looks identical.
        for cls_name, selector in (
            (b"REMStore",
             b"fetchReminderWithDACalendarItemUniqueIdentifier:inList:error:"),
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
                "link writer expects — missing: " + ", ".join(missing) + ". "
                "Setting linked content uses private API, so an OS update "
                "can break it; reading it is unaffected. Please file an issue."
            )
            raise LinkWriteUnavailable(self._load_error)

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
                raise LinkWriteUnavailable("Could not create a REMStore.")
        return self._store

    def available(self) -> bool:
        try:
            self._load()
            return True
        except LinkWriteUnavailable:
            return False

    def unavailable_reason(self) -> Optional[str]:
        try:
            self._load()
            return None
        except LinkWriteUnavailable as exc:
            return str(exc)

    # ------------------------------------------------------------------
    # Fetch / read-back
    # ------------------------------------------------------------------

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
            domain = str(error.domain()) if hasattr(error, "domain") else ""
            code = int(error.code()) if hasattr(error, "code") else 0
            if code == _XPC_CONNECTION_INVALID:
                raise LinkWriteUnavailable(
                    "Could not reach the Reminders daemon (remindd). The "
                    "host process almost certainly lacks Reminders "
                    "permission — grant it under System Settings → Privacy "
                    f"& Security → Reminders. ({domain} {code})"
                )
            raise LinkWriteError(f"Could not load reminder {reminder_id}: {error}")
        raise LinkWriteError(f"Reminder not found: {reminder_id}")

    @staticmethod
    def _linked_from_activity(activity: Any) -> Optional[LinkedContent]:
        """Describe a live REMUserActivity the same way linkstore does."""
        if activity is None:
            return None
        try:
            kind_code = int(activity.type())
        except Exception:
            return None
        if kind_code == TYPE_UNIVERSAL_LINK:
            url_obj = activity.universalLink()
            url = str(url_obj.absoluteString()) if url_obj is not None else None
            if url is None:
                storage = activity.storage()
                url = bytes(storage).decode("utf-8", "replace") if storage else None
            return LinkedContent(kind=kind_for_url(url), url=url)
        if kind_code == TYPE_USER_ACTIVITY:
            ua = activity.userActivity()
            if ua is None:
                return None
            activity_type = str(ua.activityType()) if ua.activityType() else None
            title = str(ua.title()) if ua.title() else None
            target = ua.targetContentIdentifier()
            url = str(target) if target else None
            if url is None and ua.webpageURL() is not None:
                url = str(ua.webpageURL().absoluteString())
            return LinkedContent(
                kind=kind_for_url(url, activity_type), url=url,
                title=title, activity_type=activity_type,
            )
        return None

    def current_link(self, reminder_id: str) -> Optional[LinkedContent]:
        """Read a reminder's linked content through ReminderKit.

        linkstore.py is the normal read path. This exists so a write can
        confirm its result against the source it wrote to, rather than
        against a file remindd may not have flushed yet.
        """
        with self._lock:
            store = self._rem_store()
            reminder = self._fetch(store, reminder_id)
            storage = reminder.storage()
            return self._linked_from_activity(
                storage.userActivity() if storage is not None else None
            )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _make_activity(self, spec: LinkSpec) -> Any:
        from Foundation import NSURL, NSUserActivity  # type: ignore

        REMUserActivity = self._classes["REMUserActivity"]
        if spec.kind in ("mail", "web"):
            url = NSURL.URLWithString_(spec.url)
            if url is None:
                raise LinkWriteError(f"Not a valid URL: {spec.url!r}")
            activity = REMUserActivity.alloc().initWithUniversalLink_(url)
        elif spec.kind == "messages":
            ns_activity = NSUserActivity.alloc().initWithActivityType_(
                MESSAGES_ACTIVITY_TYPE
            )
            if spec.title:
                ns_activity.setTitle_(spec.title)
            ns_activity.setTargetContentIdentifier_(spec.url)
            ns_activity.setUserInfo_({_MESSAGES_CONTINUITY_KEY: spec.url})
            activity = REMUserActivity.alloc().initWithUserActivity_(ns_activity)
        else:  # pragma: no cover - parse_link never yields anything else
            raise LinkWriteError(f"Unsupported link kind {spec.kind!r}")
        if activity is None:
            raise LinkWriteError(
                f"ReminderKit refused to build a user activity for {spec.url!r}."
            )
        return activity

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _apply(self, reminder_id: str, activity: Any) -> Optional[LinkedContent]:
        """Set (or, with None, clear) the linked content and read it back."""
        store = self._rem_store()
        reminder = self._fetch(store, reminder_id)

        before = self._linked_from_activity(
            reminder.storage().userActivity() if reminder.storage() is not None else None
        )
        after_wanted = self._linked_from_activity(activity)
        if before == after_wanted:
            # Nothing to do. Don't save — an empty save request still bumps
            # the modification date and wakes CloudKit.
            return before

        request = self._classes["REMSaveRequest"].alloc().initWithStore_(store)
        if request is None:
            raise LinkWriteError("Could not create a REMSaveRequest.")
        change = request.updateReminder_(reminder)
        if change is None:
            raise LinkWriteError(
                f"ReminderKit refused to open reminder {reminder_id} for update."
            )

        # The change item forwards setUserActivity: to its storage, but
        # PyObjC cannot see forwarded selectors. Try the item, then go to
        # the storage it forwards to.
        setter = getattr(change, "setUserActivity_", None)
        applied = False
        if setter is not None:
            try:
                setter(activity)
                applied = True
            except Exception as exc:
                logger.debug("change.setUserActivity_ failed, using storage: %s", exc)
        if not applied:
            storage = change.storage()
            if storage is None:
                raise LinkWriteError(
                    f"Reminder {reminder_id} has no storage to set linked content on."
                )
            storage.setUserActivity_(activity)

        result = request.saveSynchronouslyWithError_(None)
        if isinstance(result, tuple):
            ok, error = result
        else:  # pragma: no cover - only if metadata registration failed
            ok, error = bool(result), None
        if not ok:
            if error is not None and getattr(error, "code", None) is not None:
                if int(error.code()) == _XPC_CONNECTION_INVALID:
                    raise LinkWriteUnavailable(
                        "Could not reach the Reminders daemon (remindd) to "
                        "save. The host process lacks Reminders permission."
                    )
            raise LinkWriteError(
                f"Saving linked content for {reminder_id} failed: "
                f"{error if error is not None else 'unknown error'}"
            )

        # Read back from the store rather than trusting the change item.
        reminder = self._fetch(store, reminder_id)
        storage = reminder.storage()
        return self._linked_from_activity(
            storage.userActivity() if storage is not None else None
        )

    def set_link(
        self, reminder_id: str, link: str, title: Optional[str] = None
    ) -> LinkedContent:
        """Attach linked content, replacing whatever was there."""
        spec = parse_link(link, title)
        with self._lock:
            self._load()
            activity = self._make_activity(spec)
            result = self._apply(reminder_id, activity)
        if result is None:
            raise LinkWriteError(
                f"ReminderKit saved without error but reminder {reminder_id} "
                "reports no linked content afterwards."
            )
        return result

    def clear_link(self, reminder_id: str) -> None:
        """Remove linked content. A reminder with none is left alone."""
        with self._lock:
            result = self._apply(reminder_id, None)
        if result is not None:
            raise LinkWriteError(
                f"ReminderKit saved without error but reminder {reminder_id} "
                f"still reports linked content: {result.url!r}."
            )
