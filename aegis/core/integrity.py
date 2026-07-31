"""Process integrity levels, and whether we are allowed to type into a window.

Windows assigns every process an integrity level (Medium for normal apps, High
for anything elevated). UIPI - User Interface Privilege Isolation - blocks a
lower-integrity process from sending synthetic input to a higher-integrity
window. That is deliberate OS security: it is what stops a Medium-integrity
process from puppeting an elevated console or clicking "Yes" on a UAC prompt.

The dangerous part for us is the failure mode. ``SendInput`` still returns
success when UIPI swallows the input, so without this check Aegis would report
"Done, sir" having typed precisely nothing. An honest refusal is far better
than a silent lie.

We do not try to defeat this. The workarounds are all worse than the problem:
running Aegis elevated would hand a 3B model admin-level input, and uiAccess
requires a signed binary installed under Program Files.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25

# Well-known RIDs from the mandatory-label SID. Higher means more privileged.
SECURITY_MANDATORY_UNTRUSTED_RID = 0x0000
SECURITY_MANDATORY_LOW_RID = 0x1000
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
SECURITY_MANDATORY_HIGH_RID = 0x3000
SECURITY_MANDATORY_SYSTEM_RID = 0x4000

_LEVEL_NAMES = {
    SECURITY_MANDATORY_UNTRUSTED_RID: "untrusted",
    SECURITY_MANDATORY_LOW_RID: "low",
    SECURITY_MANDATORY_MEDIUM_RID: "medium",
    SECURITY_MANDATORY_HIGH_RID: "high (elevated)",
    SECURITY_MANDATORY_SYSTEM_RID: "system",
}


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


def level_name(rid: int) -> str:
    """Human-readable name for an integrity RID, including in-between values."""
    if rid in _LEVEL_NAMES:
        return _LEVEL_NAMES[rid]
    if rid > SECURITY_MANDATORY_SYSTEM_RID:
        return "system+"
    if rid > SECURITY_MANDATORY_HIGH_RID:
        return "above high"
    if rid > SECURITY_MANDATORY_MEDIUM_RID:
        return "above medium"
    return f"rid-{rid:#x}"


def process_integrity(pid: int) -> int | None:
    """Integrity RID for a process, or None if it cannot be queried.

    A None result usually means the target is *more* privileged than us (we
    could not even open it), so callers should treat it as "assume blocked"
    rather than "assume fine".
    """
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, TokenIntegrityLevel, buffer, size, ctypes.byref(size)
            ):
                return None
            label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
            count = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
            return advapi32.GetSidSubAuthority(label.Label.Sid, count.contents.value - 1).contents.value
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def own_integrity() -> int:
    rid = process_integrity(kernel32.GetCurrentProcessId())
    return SECURITY_MANDATORY_MEDIUM_RID if rid is None else rid


def foreground_pid() -> int | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def can_send_input_to_foreground() -> tuple[bool, str]:
    """Whether synthetic input will actually reach the focused window.

    Returns (allowed, human-readable reason). Fails closed: anything we cannot
    determine is reported as blocked, because a wrong "yes" here means
    silently typing into the void.
    """
    pid = foreground_pid()
    if pid is None:
        return False, "there is no focused window"

    target = process_integrity(pid)
    if target is None:
        return False, (
            "that window belongs to a more privileged process than I am "
            "(Windows will not let me query it, let alone type into it)"
        )

    mine = own_integrity()
    if target > mine:
        return False, (
            f"that window is running at {level_name(target)} integrity and I am at "
            f"{level_name(mine)} - Windows blocks synthetic input across that boundary"
        )
    return True, "ok"
