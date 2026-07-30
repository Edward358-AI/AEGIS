"""Per-stage latency instrumentation.

Latency is a primary design goal, so it is measured from the first commit
rather than bolted on. Every command produces a Trace with a stage breakdown
that can be compared against the budgets in the architecture plan.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass
class Stage:
    name: str
    ms: float


@dataclass
class Trace:
    """Timing record for a single command, start to finish."""

    label: str
    stages: list[Stage] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(Stage(name, (time.perf_counter() - start) * 1000.0))

    def mark(self, name: str) -> None:
        """Record a zero-width checkpoint measured from the trace start."""
        self.stages.append(Stage(name, (time.perf_counter() - self._t0) * 1000.0))

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0

    def summary(self) -> str:
        parts = " ".join(f"{s.name}={s.ms:.0f}ms" for s in self.stages)
        return f"[{self.label}] total={self.total_ms:.0f}ms {parts}"

    def log(self) -> None:
        log.info(self.summary())
