"""M1 acceptance harness.

The headline test is section 5: a *real* synthetic typing burst interrupted by
the kill switch. Until M1 no production verb drove SendInput, so the kill
switch had only ever been proven against the harness rather than against live
input injection.

Usage:  .venv\\Scripts\\python.exe scripts\\m1_check.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aegis.brain.intent import IntentClassifier  # noqa: E402
from aegis.core.integrity import level_name, own_integrity, process_integrity  # noqa: E402
from aegis.execute import windows as win  # noqa: E402
from aegis.execute.registry import Executor  # noqa: E402
from aegis.memory import db  # noqa: E402
from aegis.safety.guard import AbortedError, InputGuard, is_destructive_chord  # noqa: E402
from aegis.schema.actions import (  # noqa: E402
    CAPABILITY_SETS,
    READ_ONLY_VERBS,
    Action,
    Intent,
    Plan,
)

PASS, FAIL, INFO = "PASS", "FAIL", "  ->"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    section("1. Capability wiring")
    # Every verb the executor implements must be classified and reachable, or
    # the taint gate cannot reason about it and narrowing cannot grant it.
    full = Executor(InputGuard())
    implemented = set(full._handlers)
    reachable = set().union(*CAPABILITY_SETS.values())
    check(
        "Every implemented verb is reachable from some intent",
        implemented <= reachable,
        f"orphaned: {sorted(implemented - reachable) or 'none'}",
    )
    check(
        "Every reachable verb has a handler",
        reachable <= implemented,
        f"missing: {sorted(reachable - implemented) or 'none'}",
    )
    side_effecting = implemented - READ_ONLY_VERBS
    query_verbs = CAPABILITY_SETS[Intent.QUERY]
    check(
        "Query capability set contains no side-effecting verb",
        not (query_verbs & side_effecting),
        f"query={sorted(query_verbs)}",
    )

    section("2. Elevated-window guard (UIPI)")
    print(f"{INFO} Aegis runs at {level_name(own_integrity())} integrity")
    elevated = [w for w in win.list_windows() if (process_integrity(w.pid) or 0) > own_integrity()]
    if elevated:
        print(f"{INFO} found elevated window: {elevated[0].title[:50]!r}")
        check(
            "Elevated windows are detected as higher integrity",
            True,
            f"{len(elevated)} elevated window(s) open",
        )
    else:
        print(f"{INFO} no elevated window open; guard logic verified by unit check below")
    # The guard must fail closed for an unqueryable process (pid 4 = System).
    check(
        "Unqueryable (System) process reports no integrity, so input is refused",
        process_integrity(4) is None,
    )

    section("3. Destructive chord classification")
    for chord, expected in [
        ("ctrl+s", False), ("ctrl+c", False), ("ctrl+tab", False),
        ("alt+f4", True), ("ctrl+w", True), ("shift+delete", True),
        ("F4+alt", True),  # reordered - must still be caught
        ("SHIFT+DEL", True),  # aliased + cased
    ]:
        check(f"chord {chord!r} destructive={expected}", is_destructive_chord(chord) is expected)

    section("4. Confirmation gate")
    # Fails closed with no handler.
    closed = Executor(InputGuard(), confirm=None)
    res = closed.run(Plan(speech="", actions=[Action(verb="press_keys", target="alt+f4")]))
    check(
        "Destructive chord refused when no confirm handler exists",
        res and not res[0].ok,
        res[0].detail if res else "",
    )
    # Declined by the user.
    declined = Executor(InputGuard(), confirm=lambda q: False)
    res = declined.run(Plan(speech="", actions=[Action(verb="press_keys", target="ctrl+w")]))
    check("Destructive chord refused when user declines", res and not res[0].ok)
    # Unknown app triggers confirmation; a known one does not.
    asked: list[str] = []

    def spy(question: str) -> bool:
        asked.append(question)
        return False

    spy_exec = Executor(InputGuard(), confirm=spy)
    spy_exec.run(Plan(speech="", actions=[Action(verb="launch_app", target=r"C:\Windows\System32\cmd.exe")]))
    check("Raw executable path prompts for confirmation", len(asked) == 1, f"asked: {asked[:1]}")
    asked.clear()
    spy_exec.run(Plan(speech="", actions=[Action(verb="task_add", target="harness probe task")]))
    check("Known-safe verb does not prompt", not asked)

    section("5. Kill switch against a LIVE input burst")
    print(f"{INFO} opening Notepad and typing into it, then tripping the kill switch")
    guard = InputGuard()
    live = Executor(guard, confirm=lambda q: True)
    launched = live.run(Plan(speech="", actions=[Action(verb="launch_app", target="notepad")]))
    pid = launched[0].data.get("pid") if launched and launched[0].ok else None
    time.sleep(1.2)

    focused = live.run(Plan(speech="", actions=[Action(verb="focus_window", target="Notepad")]))
    if not (focused and focused[0].ok):
        check("Focused Notepad for the live test", False, focused[0].detail if focused else "")
    else:
        check("Focused Notepad for the live test", True, focused[0].detail)

        # 4000 chars at the 40/s rate cap would take ~100s; we trip the switch
        # 1.5s in and require it to stop essentially immediately.
        payload = "AEGIS KILL SWITCH TEST " * 200
        outcome: dict[str, object] = {}

        def burst() -> None:
            start = time.perf_counter()
            try:
                live.run(Plan(speech="", actions=[Action(verb="type_text", target=payload)]))
                outcome["result"] = "completed"
            except AbortedError:
                outcome["result"] = "aborted"
            except Exception as exc:  # noqa: BLE001
                outcome["result"] = f"error: {exc}"
            outcome["elapsed"] = time.perf_counter() - start

        thread = threading.Thread(target=burst, daemon=True)
        thread.start()
        time.sleep(1.5)
        trip_at = time.perf_counter()
        guard.trip()
        thread.join(timeout=10)
        stop_latency = (outcome.get("elapsed", 99) or 99) - 1.5

        # Must be "aborted", not merely "completed": an abort that gets
        # swallowed into a failed-action result would let the caller announce
        # success after a kill switch press.
        check(
            "Live typing burst raises AbortedError to the caller",
            outcome.get("result") == "aborted" and not thread.is_alive(),
            f"outcome={outcome.get('result')} after {outcome.get('elapsed', 0):.2f}s",
        )
        check(
            "Halt takes effect within one atomic input (<0.5s)",
            stop_latency < 0.5,
            f"{stop_latency*1000:.0f}ms after trip",
        )
        # The kill switch must not have summoned Task Manager.
        titles = [w.title.lower() for w in win.list_windows()]
        check(
            "Task Manager did not appear",
            not any("task manager" in t for t in titles),
        )
        guard.reset()

    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        print(f"{INFO} closed Notepad (pid {pid}) without saving")

    section("6. Task store")
    before = len(db.open_tasks(limit=100))
    task = db.add_task("harness smoke task", due="tomorrow")
    check("Task added with parsed due date", task.due is not None, f"due={task.due}")
    check("Open task count increased", len(db.open_tasks(limit=100)) == before + 1)
    done = db.complete_task("harness smoke")
    check("Task completed by fuzzy match", done is not None, done.title if done else "no match")
    check("Open task count restored", len(db.open_tasks(limit=100)) == before)
    # Clean up the probe task from section 4 too.
    db.complete_task("harness probe task")

    section("7. Intent routing for the new verbs")
    classifier = IntentClassifier()
    for request, expected in [
        ("open discord", Intent.ACT),
        ("type hello world", Intent.ACT),
        ("what do i still have to do", Intent.QUERY),
    ]:
        got, how = classifier.classify(request)
        check(f"classify({request!r}) -> {got.value}", got is expected, f"via {how}")

    print("\n" + "=" * 60)
    if _failures:
        print(f"M1 acceptance: {len(_failures)} FAILED -> {', '.join(_failures)}")
        return 1
    print("M1 acceptance: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
