"""Aegis entrypoint.

Wiring order matters: DPI awareness is set before Qt is constructed, and the
hotkey thread is started last so nothing can fire into a half-built app.
"""

from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from aegis.core.dpi import set_dpi_awareness

# Must happen before QApplication exists, or every coordinate is wrong on a
# scaled display.
_DPI_MODE = set_dpi_awareness()

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aegis.brain.intent import IntentClassifier  # noqa: E402
from aegis.brain.lmstudio import ensure_planner_tuned  # noqa: E402
from aegis.brain.planner import Planner, PlannerError  # noqa: E402
from aegis.config import settings  # noqa: E402
from aegis.core.killswitch import HotkeyManager  # noqa: E402
from aegis.core.singleton import InstanceLock, signal_existing_instance  # noqa: E402
from aegis.core.timing import Trace  # noqa: E402
from aegis.execute.registry import Executor  # noqa: E402
from aegis.safety.guard import AbortedError, BudgetExceededError, InputGuard  # noqa: E402
from aegis.schema.actions import CAPABILITY_SETS  # noqa: E402
from aegis.voice.tts import TtsEngine  # noqa: E402

log = logging.getLogger("aegis")


class Bridge(QObject):
    """Marshals hotkey-thread events onto the Qt main thread."""

    toggle_bar = Signal()
    killed = Signal()
    status = Signal(str)
    confirm = Signal(str)
    confirm_cancelled = Signal()


def configure_logging() -> None:
    settings.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_dir / "aegis.log", encoding="utf-8"),
        ],
    )


class Aegis:
    def __init__(self) -> None:
        self.guard = InputGuard()
        self.planner = Planner()
        self.classifier = IntentClassifier()
        self.tts = TtsEngine()
        self.bridge = Bridge()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aegis-pipeline")

        # One executor per capability set. The query executor has no
        # side-effecting handler wired into it at all, so the read path cannot
        # touch the machine even if the planner is talked into asking.
        self.executors = {
            intent: Executor(
                self.guard,
                allowed_verbs=verbs,
                confirm=self._request_confirm,
                ask=self._on_ask,
                answer=self._on_answer,
            )
            for intent, verbs in CAPABILITY_SETS.items()
        }

        # Cross-thread confirmation handshake: the pipeline thread blocks on
        # this Event while the Qt main thread collects the answer.
        self._confirm_event = threading.Event()
        self._confirm_result = False

        from aegis.ui.commandbar import CommandBar

        self.bar = CommandBar()
        self.bar.submitted.connect(self._on_submit)
        self.bar.confirm_answered.connect(self._on_confirm_answered)
        self.bridge.toggle_bar.connect(self.bar.toggle, Qt.ConnectionType.QueuedConnection)
        self.bridge.status.connect(self.bar.set_status, Qt.ConnectionType.QueuedConnection)
        self.bridge.killed.connect(self._on_killed, Qt.ConnectionType.QueuedConnection)
        self.bridge.confirm.connect(self.bar.ask_confirm, Qt.ConnectionType.QueuedConnection)
        self.bridge.confirm_cancelled.connect(
            self.bar.cancel_confirm, Qt.ConnectionType.QueuedConnection
        )

        self.hotkeys = HotkeyManager()
        self.hotkeys.bind("command_bar", settings.hotkey_command_bar, self.bridge.toggle_bar.emit)
        self.hotkeys.bind("kill_switch", settings.hotkey_kill, self._trip_kill)

    # --- lifecycle ------------------------------------------------------
    def start(self) -> None:
        log.info("LM Studio: %s", ensure_planner_tuned())

        self.tts.start()
        self.hotkeys.start()
        log.info(
            "Aegis ready (dpi=%s). %s to summon, %s to halt.",
            _DPI_MODE, settings.hotkey_command_bar, settings.hotkey_kill,
        )

    def shutdown(self) -> None:
        self.hotkeys.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.tts.stop()

    # --- handlers -------------------------------------------------------
    def _trip_kill(self) -> None:
        self.guard.trip()
        self.bridge.killed.emit()

    def _on_killed(self) -> None:
        self.bar.set_status("Halted.")

    def _on_ask(self, question: str) -> None:
        self.bridge.status.emit(question)

    def _on_answer(self, answer: str) -> None:
        self.bridge.status.emit(answer)

    def _on_confirm_answered(self, approved: bool) -> None:
        self._confirm_result = approved
        self._confirm_event.set()

    def _request_confirm(self, question: str) -> bool:
        """Ask the user to approve an action. Called from the pipeline thread.

        Blocks until the Qt thread reports an answer, or until the timeout -
        which defaults to NO, so a question the user never sees or never
        answers can never become an action.
        """
        self._confirm_event.clear()
        self._confirm_result = False
        self.bridge.confirm.emit(question)
        self.tts.say(f"{question}? Please confirm, sir.")

        if not self._confirm_event.wait(timeout=settings.confirm_timeout_s):
            log.warning("confirmation timed out: %s", question)
            self.bridge.confirm_cancelled.emit()
            self.bridge.status.emit("Timed out - I did nothing.")
            return False

        log.info("confirmation %s: %s", "approved" if self._confirm_result else "declined", question)
        return self._confirm_result

    def _on_submit(self, text: str) -> None:
        self.bar.set_status("Thinking…")
        self.pool.submit(self._pipeline, text)

    def _pipeline(self, text: str) -> None:
        """Runs off the UI thread: ack -> plan -> execute -> speak."""
        trace = Trace("command")
        # A new user-initiated command clears any previous halt.
        self.guard.reset()
        # Fires before planning, so the user hears a response in ~100ms rather
        # than waiting on the model.
        self.tts.ack()

        # Classified from the user's request alone - no screen text, nothing
        # untrusted. That is what makes it safe to derive capabilities from.
        with trace.stage("intent"):
            intent, how = self.classifier.classify(text)
        allowed = CAPABILITY_SETS[intent]
        log.info("intent=%s (%s) capabilities=%s", intent.value, how, sorted(allowed))

        # From M2 this carries pruned UIA / screen text, which is untrusted and
        # taints the resulting plan. At M0 there are no observations yet.
        observations: str | None = None

        try:
            plan = self.planner.plan(
                text, allowed_verbs=allowed, observations=observations, trace=trace
            )
        except PlannerError as exc:
            log.error("%s", exc)
            self.bridge.status.emit(str(exc))
            self.tts.say("I'm afraid I can't reach my reasoning core, sir.")
            return

        self.bridge.status.emit(plan.speech)

        try:
            results = self.executors[intent].run(
                plan, trace=trace, tainted=observations is not None
            )
        except AbortedError:
            self.bridge.status.emit("Halted.")
            return
        except BudgetExceededError as exc:
            log.warning("%s", exc)
            self.bridge.status.emit("Action limit reached.")
            self.tts.say("I've stopped, sir. That was going nowhere.")
            return

        self.tts.say(plan.speech)

        failures = [r for r in results if not r.ok]
        if failures:
            detail = failures[0].detail
            log.warning("action failed: %s", detail)
            self.bridge.status.emit(f"Failed: {detail}")
            self.tts.say("That didn't work, sir.")

        trace.log()


def main() -> int:
    configure_logging()

    lock = InstanceLock()
    if not lock.acquire():
        log.warning("another Aegis instance is already running")
        signal_existing_instance()
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # the bar hides rather than exits

    aegis = Aegis()
    aegis.start()
    aegis.bar.summon()

    try:
        return app.exec()
    finally:
        aegis.shutdown()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
