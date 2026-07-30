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
