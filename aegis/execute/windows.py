"""Top-level window enumeration and focus.

``SetForegroundWindow`` is deliberately unreliable: Windows enforces a
foreground lock so background processes cannot steal focus while you are
typing. A plain call from a background process usually flashes the taskbar
button instead of raising the window.

The standard workaround is to attach our input queue to the current foreground
thread first, which puts us inside the same input context and makes the call
legal. It is not a hack around security so much as the documented way to hand
focus over deliberately - the same mechanism the command bar already uses.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_RESTORE = 9
GW_OWNER = 4

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetWindow.restype = wintypes.HWND


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int


def list_windows() -> list[Window]:
    """Visible, titled, top-level windows - roughly what Alt+Tab shows."""
    found: list[Window] = []

    def callback(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        # Owned windows are dialogs/popups belonging to another window; the
        # parent is what a user means by "the Discord window".
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(Window(hwnd=hwnd, title=title, pid=pid.value))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def find_window(target: str) -> Window | None:
    """Best window match for a user's phrasing, or None.

    Ranked: exact title, then prefix, then substring, then all query words
    present. Ties break toward the shorter title, which is almost always the
    main window rather than a document-specific one.
    """
    needle = target.strip().lower()
    if not needle:
        return None
    windows = list_windows()

    def rank(window: Window) -> tuple[int, int] | None:
        title = window.title.lower()
        if title == needle:
            return (0, len(title))
        if title.startswith(needle):
            return (1, len(title))
        if needle in title:
            return (2, len(title))
        if all(word in title for word in needle.split()):
            return (3, len(title))
        return None

    scored = [(rank(w), w) for w in windows]
    candidates = [(r, w) for r, w in scored if r is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda pair: pair[0])[1]


def foreground_hwnd() -> int:
    """The current foreground window's handle, or 0 if there is none."""
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    """Whether the handle still names a live window."""
    return bool(user32.IsWindow(hwnd))


def focus_window(hwnd: int) -> bool:
    """Raise and focus a window, working around the foreground lock."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    our_thread = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    their_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    attached = False
    if their_thread and their_thread != our_thread:
        attached = bool(user32.AttachThreadInput(our_thread, their_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, their_thread, False)

    if not ok:
        log.warning("SetForegroundWindow refused for hwnd %s", hwnd)
    return bool(user32.GetForegroundWindow() == hwnd)
