"""Per-monitor DPI awareness.

This MUST run before Qt or UI Automation initialise. Without it, every screen
coordinate is silently wrong on a scaled display - which presents as a model
accuracy problem and is miserable to diagnose.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def set_dpi_awareness() -> str:
    """Opt into per-monitor DPI awareness, falling back through older APIs.

    Returns the name of the mechanism that succeeded, for logging.
    """
    # Win10 1703+ - the one we actually want.
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2):
            return "PerMonitorAwareV2"
    except (AttributeError, OSError):
        pass

    # Win8.1+
    try:
        # S_OK == 0; E_ACCESSDENIED means it was already set, which is fine.
        hresult = ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        if hresult in (0, -2147024891):
            return "PerMonitorAware"
    except (AttributeError, OSError):
        pass

    # Vista+ system-wide awareness. Better than nothing.
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "SystemAware"
    except (AttributeError, OSError):
        pass

    log.warning("Could not set DPI awareness; coordinates may be wrong on scaled displays")
    return "none"
