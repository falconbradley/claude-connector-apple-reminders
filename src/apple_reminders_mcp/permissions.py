"""TCC permission helpers for the Reminders entity.

Apple changed the access API in macOS 14 Sonoma:

- Pre-14: ``EKEventStore.requestAccessToEntityType:completion:``
- 14+:    ``EKEventStore.requestFullAccessToRemindersWithCompletion:``

The new call requires ``NSRemindersFullAccessUsageDescription`` in the
calling process's Info.plist to actually present a prompt.  When the
process is an unsigned, dynamically-launched Python interpreter (which is
what `uv run` produces under Claude Desktop), the system attaches the
prompt to the *responsible process* — typically Claude Desktop itself.

If the user has previously granted access via System Settings the call
returns ``True`` immediately; otherwise we surface a structured
``PermissionDeniedError`` with remediation steps.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class PermissionDeniedError(RuntimeError):
    """Raised when EventKit access for Reminders is not granted."""

    def __init__(self, underlying: Optional[str] = None) -> None:
        msg = (
            "Apple Reminders access was not granted to this process.\n\n"
            "To fix:\n"
            "  1. Open System Settings → Privacy & Security → Reminders\n"
            "  2. Enable 'Claude' (or the parent app, e.g. Terminal/iTerm)\n"
            "  3. Quit and relaunch Claude Desktop\n"
        )
        if underlying:
            msg += f"\nUnderlying error: {underlying}"
        super().__init__(msg)


def request_reminders_access(store) -> bool:
    """Synchronously request Reminders access on an EKEventStore.

    Returns True if access was granted, False otherwise.  Blocks until the
    OS callback fires (or 30 s timeout, after which we assume denial).

    Uses the macOS-14 API when available, falling back to the deprecated
    pre-14 method.  Both shapes are ``(BOOL granted, NSError* error)``.
    """
    granted = {"value": False, "error": None}
    done = threading.Event()

    def callback(ok: bool, err) -> None:  # type: ignore[no-untyped-def]
        granted["value"] = bool(ok)
        granted["error"] = err
        done.set()

    method = None
    if hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
        method = store.requestFullAccessToRemindersWithCompletion_
        logger.debug("Using requestFullAccessToRemindersWithCompletion_ (macOS 14+).")
    elif hasattr(store, "requestAccessToEntityType_completion_"):
        # EKEntityTypeReminder == 1
        EKEntityTypeReminder = 1

        def legacy_completion(ok: bool, err) -> None:  # type: ignore[no-untyped-def]
            callback(ok, err)

        method = lambda cb: store.requestAccessToEntityType_completion_(  # noqa: E731
            EKEntityTypeReminder, cb
        )
        logger.debug("Using legacy requestAccessToEntityType_completion_ (pre-macOS 14).")
    else:
        raise PermissionDeniedError("EKEventStore exposes no access-request method.")

    method(callback)

    if not done.wait(timeout=30.0):
        logger.warning("Reminders access prompt timed out after 30s.")
        return False

    err = granted["error"]
    if err is not None:
        # NSError; render its human-readable description if present.
        try:
            err_msg = str(err.localizedDescription())
        except Exception:
            err_msg = repr(err)
        logger.warning("Reminders access request returned error: %s", err_msg)

    return bool(granted["value"])


def authorization_status_label(status: int) -> str:
    """Map EKAuthorizationStatus integer to a friendly label."""
    return {
        0: "not determined",
        1: "restricted",
        2: "denied",
        3: "authorized",
        4: "writeOnly",       # macOS 14+ for events; not applicable to reminders
        5: "fullAccess",      # macOS 14+ value for full reminders access
    }.get(status, f"unknown ({status})")
