"""Execution guard rails.

Three mechanisms work together, per the architecture plan. Any one of them
alone is insufficient:

1. An abort Event checked between *every atomic input*, not once per plan.
2. A hard rate cap on injected inputs per second.
3. A ceiling on actions per plan, so a hallucinating loop terminates itself.
"""

from __future__ import annotations

import logging
import threading
import time

from aegis.config import settings

log = logging.getLogger(__name__)


# Chords that close, discard or destroy something. press_keys is gated on this
# rather than being blanket-destructive, since most chords (ctrl+s, ctrl+c,
# ctrl+tab) are entirely benign and prompting for them would train the user to
# approve without reading.
DESTRUCTIVE_CHORDS: frozenset[str] = frozenset(
    {
        "alt+f4",        # close window
        "ctrl+w",        # close tab/document
        "ctrl+shift+w",  # close all windows
        "ctrl+q",        # quit application
        "ctrl+shift+q",  # quit all
        "shift+delete",  # permanent delete, bypasses Recycle Bin
        "ctrl+shift+delete",  # clear browsing data
        "win+l",         # lock workstation
    }
)


def normalise_chord(combo: str) -> str:
    """Canonical form of a chord so ``Alt + F4`` matches ``alt+f4``.

    Modifiers are sorted so ordering cannot be used to slip a chord past the
    classifier; the final non-modifier key is kept last.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    aliases = {"control": "ctrl", "esc": "escape", "del": "delete", "super": "win", "meta": "win"}
    parts = [aliases.get(p, p) for p in parts]
    modifiers = sorted(p for p in parts if p in {"ctrl", "alt", "shift", "win"})
    keys = [p for p in parts if p not in {"ctrl", "alt", "shift", "win"}]
    return "+".join(modifiers + keys)


def is_destructive_chord(combo: str) -> bool:
    return normalise_chord(combo) in {normalise_chord(c) for c in DESTRUCTIVE_CHORDS}


class AbortedError(RuntimeError):
    """Raised when the kill switch trips mid-execution."""


class BudgetExceededError(RuntimeError):
    """Raised when a plan exceeds its action ceiling."""


class InputGuard:
    """Shared gate that every synthetic input must pass through."""

    def __init__(self, max_inputs_per_second: float | None = None) -> None:
        self.abort = threading.Event()
        self._rate = max_inputs_per_second or settings.max_inputs_per_second
        self._min_interval = 1.0 / self._rate if self._rate > 0 else 0.0
        self._last_input = 0.0
        self._lock = threading.Lock()

    # --- kill switch ----------------------------------------------------
    def trip(self) -> None:
        """Fire the kill switch. Safe to call from any thread."""
        self.abort.set()
        log.warning("KILL SWITCH TRIPPED - halting all synthetic input")

    def reset(self) -> None:
        self.abort.clear()

    @property
    def aborted(self) -> bool:
        return self.abort.is_set()

    # --- the gate -------------------------------------------------------
    def check(self) -> None:
        """Raise if execution has been aborted."""
        if self.abort.is_set():
            raise AbortedError("execution aborted by kill switch")

    def gate(self) -> None:
        """Call immediately before each atomic input event.

        Checks the abort flag and enforces the rate cap. The abort flag is
        re-checked after throttling, since the sleep is where a panicking
        user's keypress most likely lands.
        """
        self.check()
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.perf_counter()
            wait = self._min_interval - (now - self._last_input)
            if wait > 0:
                time.sleep(wait)
            self._last_input = time.perf_counter()
        self.check()


class PlanBudget:
    """Action ceiling for a single plan."""

    def __init__(self, max_actions: int | None = None) -> None:
        self.max_actions = max_actions or settings.max_actions_per_plan
        self.used = 0

    def consume(self) -> None:
        self.used += 1
        if self.used > self.max_actions:
            raise BudgetExceededError(
                f"plan exceeded action ceiling of {self.max_actions}"
            )
