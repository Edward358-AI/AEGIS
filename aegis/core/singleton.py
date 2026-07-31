"""Single-instance enforcement.

Two Aegis processes cannot both own the global hotkeys - the second one's
RegisterHotKey calls simply fail, leaving a live but unreachable process that
still holds the log file and a loaded TTS model. Easy to create by accident,
confusing to diagnose.

A named mutex is the standard Windows answer. The name is per-user so this
does not interfere across accounts on a shared machine.
"""

from __future__ import annotations

import ctypes
import getpass
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183

kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE


class InstanceLock:
    """Holds the mutex for the lifetime of the process."""

    def __init__(self) -> None:
        # Local\ scopes the mutex to this login session, which is what we want.
        self.name = f"Local\\Aegis-{getpass.getuser()}"
        self._handle: int | None = None

    def acquire(self) -> bool:
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            log.warning("could not create instance mutex; continuing unguarded")
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None


def signal_existing_instance() -> None:
    """Ask the running instance to show itself.

    Best effort: the running Aegis already listens for the global hotkey, so
    the friendliest thing a duplicate launch can do is tell the user which
    keys to press rather than silently vanishing.
    """
    from aegis.config import settings  # noqa: PLC0415

    message = (
        f"Aegis is already running.\n\n"
        f"Press {settings.hotkey_command_bar} to summon it."
    )
    user32.MessageBoxW(None, message, "Aegis", 0x40)  # MB_ICONINFORMATION
