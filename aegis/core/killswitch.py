"""Global hotkeys, including the kill switch.

RegisterHotKey binds a hotkey to the *calling thread* when the window handle is
NULL, and delivers WM_HOTKEY to that thread's message queue. So registration
and the message pump both have to live on the same dedicated thread - that is
why this class does the registering inside ``_run`` rather than in ``bind``.

Callbacks fire on the hotkey thread. Keep them short and marshal anything
UI-related onto the Qt main thread.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

from aegis.execute.sendinput import _vk_for

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}

user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def parse_combo(combo: str) -> tuple[int, int]:
    """``"ctrl+alt+backspace"`` -> (modifier mask, virtual key code)."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey combo: {combo!r}")
    mods = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise ValueError(f"more than one non-modifier key in {combo!r}")
    if key is None:
        raise ValueError(f"hotkey {combo!r} has no non-modifier key")
    return mods | MOD_NOREPEAT, _vk_for(key)


class HotkeyManager:
    """Owns the hotkey thread and its message pump."""

    def __init__(self) -> None:
        self._bindings: list[tuple[str, str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()

    def bind(self, name: str, combo: str, callback: Callable[[], None]) -> None:
        """Queue a hotkey. Must be called before ``start()``."""
        if self._thread is not None:
            raise RuntimeError("bind() must be called before start()")
        parse_combo(combo)  # fail fast on a bad combo string
        self._bindings.append((name, combo, callback))

    def start(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._run, name="aegis-hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            log.error("hotkey thread failed to become ready within %.1fs", timeout)

    def stop(self) -> None:
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        handlers: dict[int, tuple[str, Callable[[], None]]] = {}

        for index, (name, combo, callback) in enumerate(self._bindings, start=1):
            mods, vk = parse_combo(combo)
            if user32.RegisterHotKey(None, index, mods, vk):
                handlers[index] = (name, callback)
                log.info("hotkey registered: %s -> %s", combo, name)
            else:
                err = ctypes.get_last_error()
                log.error(
                    "failed to register hotkey %s for %s (error %d) - "
                    "another application probably owns it",
                    combo, name, err,
                )

        self._ready.set()

        msg = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT, or an error we cannot recover from
                    break
                if msg.message == WM_HOTKEY:
                    entry = handlers.get(int(msg.wParam))
                    if entry is None:
                        continue
                    name, callback = entry
                    try:
                        callback()
                    except Exception:
                        log.exception("hotkey handler %s raised", name)
        finally:
            for index in handlers:
                user32.UnregisterHotKey(None, index)
            log.info("hotkey thread stopped")
