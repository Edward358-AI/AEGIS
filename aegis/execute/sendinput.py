"""Win32 SendInput primitives.

Two details here are load-bearing and easy to get wrong:

* Absolute mouse coordinates are normalised to 0-65535 across the *virtual
  desktop* and sent with MOUSEEVENTF_VIRTUALDESK. Normalising against the
  primary monitor instead is the classic multi-monitor bug.
* Key presses use scancodes (KEYEVENTF_SCANCODE) so they register with
  DirectInput applications, which ignore virtual-key events. Text typing
  instead uses KEYEVENTF_UNICODE, which handles arbitrary characters without
  caring about the keyboard layout.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from aegis.safety.guard import InputGuard

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT

# Keys that live on the extended half of the keyboard and need the extended
# flag set, or they land as their numpad twins.
_EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24,  # PgUp PgDn End Home
    0x25, 0x26, 0x27, 0x28,  # Left Up Right Down
    0x2D, 0x2E,              # Insert Delete
    0x5B, 0x5C, 0x5D,        # LWin RWin Apps
    0xA3,                    # RControl
    0xA5,                    # RMenu (right Alt)
    0x6F,                    # Numpad divide
    0x90,                    # NumLock
    0x2C,                    # PrintScreen
}

VK_NAMES: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B, "super": 0x5B, "meta": 0x5B,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "back": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "grave": 0xC0, "tilde": 0xC0, "minus": 0xBD, "equals": 0xBB,
    "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "backslash": 0xDC,
    "semicolon": 0xBA, "quote": 0xDE,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def _vk_for(name: str) -> int:
    key = name.strip().lower()
    if key in VK_NAMES:
        return VK_NAMES[key]
    if len(key) == 1:
        # Digits and A-Z map directly onto their ASCII code as virtual keys.
        return ord(key.upper())
    raise ValueError(f"unknown key name: {name!r}")


def virtual_desktop_rect() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the whole virtual desktop, in pixels."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def _normalise(x: int, y: int) -> tuple[int, int]:
    left, top, width, height = virtual_desktop_rect()
    # Guard against a 1px desktop; the -1 is what makes the far edge reachable.
    nx = round((x - left) * 65535 / max(width - 1, 1))
    ny = round((y - top) * 65535 / max(height - 1, 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


class InputSender:
    """Sends synthetic input, gated by an InputGuard.

    Every atomic event passes through ``guard.gate()`` first, so a kill switch
    press interrupts a burst partway through rather than after it completes.
    """

    def __init__(self, guard: InputGuard) -> None:
        self.guard = guard

    def _send(self, *inputs: INPUT) -> None:
        for item in inputs:
            self.guard.gate()
            array = (INPUT * 1)(item)
            sent = user32.SendInput(1, array, ctypes.sizeof(INPUT))
            if sent != 1:
                err = ctypes.get_last_error()
                raise OSError(err, f"SendInput failed (error {err})")

    # --- mouse ----------------------------------------------------------
    def move_to(self, x: int, y: int) -> None:
        nx, ny = _normalise(x, y)
        item = INPUT(
            type=INPUT_MOUSE,
            u=_InputUnion(
                mi=MOUSEINPUT(
                    dx=nx,
                    dy=ny,
                    mouseData=0,
                    dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        self._send(item)

    def click(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        if x is not None and y is not None:
            self.move_to(x, y)
        down, up = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]
        for flag in (down, up):
            self._send(
                INPUT(
                    type=INPUT_MOUSE,
                    u=_InputUnion(
                        mi=MOUSEINPUT(0, 0, 0, flag, 0, 0)
                    ),
                )
            )

    def scroll(self, clicks: int) -> None:
        self._send(
            INPUT(
                type=INPUT_MOUSE,
                u=_InputUnion(
                    mi=MOUSEINPUT(0, 0, ctypes.c_uint32(clicks * 120).value, MOUSEEVENTF_WHEEL, 0, 0)
                ),
            )
        )

    # --- keyboard -------------------------------------------------------
    def _key_event(self, vk: int, keyup: bool) -> INPUT:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        flags = KEYEVENTF_SCANCODE
        if vk in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        if keyup:
            flags |= KEYEVENTF_KEYUP
        return INPUT(
            type=INPUT_KEYBOARD,
            u=_InputUnion(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)),
        )

    def press_keys(self, combo: str) -> None:
        """Press a chord such as ``ctrl+shift+t``, then release in reverse."""
        vks = [_vk_for(part) for part in combo.split("+") if part.strip()]
        if not vks:
            raise ValueError(f"empty key combo: {combo!r}")
        for vk in vks:
            self._send(self._key_event(vk, keyup=False))
        for vk in reversed(vks):
            self._send(self._key_event(vk, keyup=True))

    def type_text(self, text: str) -> None:
        """Type arbitrary text as Unicode, independent of keyboard layout."""
        for code_unit in _utf16_units(text):
            for keyup in (False, True):
                flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
                self._send(
                    INPUT(
                        type=INPUT_KEYBOARD,
                        u=_InputUnion(
                            ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
                        ),
                    )
                )


def _utf16_units(text: str) -> list[int]:
    """Split text into UTF-16 code units, so astral characters still work."""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
