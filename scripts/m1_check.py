"""M1 acceptance harness.

The headline tests are the live ones. Section 7 interrupts a *real* synthetic
typing burst with the kill switch - until M1 no production verb drove
SendInput, so the switch had only ever been proven against the harness.
Section 8 drives the *real* confirmation UI and proves it wins keyboard focus
from the target app and hands it back afterwards - the gap that let a
confirmed alt+f4 close the bar itself instead of the window the plan aimed at.

Usage:  .venv\\Scripts\\python.exe scripts\\m1_check.py [--headless]

--headless skips the live sections (7 and 8), so the rest can run on a busy
machine without stealing focus.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aegis.brain.intent import IntentClassifier  # noqa: E402
from aegis.brain.outcome import speech_for_outcome  # noqa: E402
from aegis.core.integrity import level_name, own_integrity, process_integrity  # noqa: E402
from aegis.execute import windows as win  # noqa: E402
from aegis.execute.registry import ExecutionResult, Executor  # noqa: E402
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
    headless = "--headless" in sys.argv

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
    check(
        "Declined result is flagged declined, not failed",
        res and res[0].declined,
        "outcome speech relies on this to acknowledge rather than apologise",
    )
    # A yes that predates the kill switch is stale. If the switch trips while
    # the question is open, an approval arriving afterwards must not execute.
    stale_guard = InputGuard()

    def approve_after_kill(question: str) -> bool:
        stale_guard.trip()
        return True

    stale_exec = Executor(stale_guard, confirm=approve_after_kill)
    try:
        stale_exec.run(
            Plan(speech="", actions=[Action(verb="launch_app", target="no-such-app-xyz")])
        )
        stale_outcome = "handler ran (or was recorded as an ordinary failure)"
        stale_ok = False
    except AbortedError:
        stale_outcome = "AbortedError"
        stale_ok = True
    check("Approval after a kill-switch trip does not execute", stale_ok, stale_outcome)
    # Unknown app triggers confirmation; a known one does not.
    asked: list[str] = []

    def spy(question: str) -> bool:
        asked.append(question)
        return False

    spy_exec = Executor(InputGuard(), confirm=spy)
    spy_exec.run(Plan(speech="", actions=[Action(verb="launch_app", target=r"C:\Windows\System32\cmd.exe")]))
    check("Raw executable path prompts for confirmation", len(asked) == 1, f"asked: {asked[:1]}")
    asked.clear()
    spy_exec.run(Plan(speech="", actions=[Action(verb="answer", target="harness probe")]))
    check("Known-safe verb does not prompt", not asked)

    section("5. Near-miss app and window names")
    from aegis.execute.registry import _fuzzy_app_correction  # noqa: PLC0415

    for typo, expected in [
        ("sptofiy", "spotify"),   # transposition - difflib alone scores 0.71
        ("notepda", "notepad"),
        ("discrod", "discord"),
        ("crome", "chrome"),
    ]:
        check(
            f"correction {typo!r} -> {expected!r}",
            _fuzzy_app_correction(typo) == expected,
            f"got {_fuzzy_app_correction(typo)!r}",
        )
    check(
        "'teams' is not anagram-corrected to 'steam'",
        _fuzzy_app_correction("teams") != "steam",
        f"got {_fuzzy_app_correction('teams')!r}",
    )
    check("Gibberish stays uncorrected", _fuzzy_app_correction("qzxvbn") is None)
    check(
        "Near-miss of a known app skips the unknown-app prompt",
        not Executor._is_unrecognised_app("sptofiy"),
    )
    check("Genuinely unknown app still prompts", Executor._is_unrecognised_app("qzxvbn"))
    check(
        "Window word near-match accepts a transposition",
        win.is_near_name("discrod", "discord"),
    )
    check(
        "Window word near-match rejects a different app",
        not win.is_near_name("slack", "steam"),
    )

    section("6. Outcome speech is honest")
    plan = Plan(speech="Notepad is closed, sir.", actions=[Action(verb="press_keys", target="alt+f4")])
    ok_r = ExecutionResult("press_keys", True, "pressed alt+f4")
    fail_r = ExecutionResult("press_keys", False, "Windows refused")
    dec_r = ExecutionResult("press_keys", False, "declined (destructive)", declined=True)
    check(
        "All-success speaks the plan's line",
        speech_for_outcome(plan, [ok_r]).speech == plan.speech,
    )
    o = speech_for_outcome(plan, [fail_r])
    check(
        "Failure never speaks the pre-written success line",
        plan.speech not in o.speech and "Failed" in (o.status or ""),
        f"speech={o.speech!r}",
    )
    o = speech_for_outcome(plan, [dec_r])
    check(
        "Decline acknowledges without claiming success or failure",
        plan.speech not in o.speech and "didn't work" not in o.speech,
        f"speech={o.speech!r}",
    )
    check(
        "Failure outranks a decline in a mixed plan",
        "Failed" in (speech_for_outcome(plan, [dec_r, fail_r]).status or ""),
    )
    qplan = Plan(speech="One moment, sir.", actions=[Action(verb="answer", target="")])
    reply = "The focused window is Notepad, sir."
    ans_r = ExecutionResult("answer", True, reply, {"answer": reply})
    check(
        "A query speaks the actual answer, not the plan's filler line",
        speech_for_outcome(qplan, [ans_r]).speech == reply,
    )

    section("7. Kill switch against a LIVE input burst")
    if headless:
        print(f"{INFO} skipped (--headless)")
    else:
        run_live_burst()

    section("8. Confirmation focus round-trip (live)")
    if headless:
        print(f"{INFO} skipped (--headless)")
    else:
        run_confirm_focus_test()

    section("9. Intent routing")
    classifier = IntentClassifier()
    for request, expected in [
        ("open discord", Intent.ACT),
        ("type hello world", Intent.ACT),
        ("what's on my screen", Intent.QUERY),
    ]:
        got, how = classifier.classify(request)
        check(f"classify({request!r}) -> {got.value}", got is expected, f"via {how}")

    print("\n" + "=" * 60)
    if _failures:
        print(f"M1 acceptance: {len(_failures)} FAILED -> {', '.join(_failures)}")
        return 1
    print("M1 acceptance: all checks passed")
    return 0


def run_live_burst() -> None:
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


def run_confirm_focus_test() -> None:
    """Drive the REAL confirmation UI against a real foreground app.

    This is the seam the lambda-based gate tests in section 4 cannot see: the
    bar must win keyboard focus from the app the plan just focused (or Enter
    lands in that app and the question times out), and must hand focus back
    once answered (or the approved action lands on the bar itself).
    """
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from aegis.config import settings  # noqa: PLC0415
    from aegis.main import Aegis  # noqa: PLC0415

    print(f"{INFO} driving the real command bar; hands off the keyboard and mouse")
    settings.tts_enabled = False  # keep the harness silent
    app = QApplication.instance() or QApplication(sys.argv)
    assistant = Aegis()

    def pump(condition, timeout: float = 5.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    note = subprocess.Popen(["notepad.exe"])
    try:
        target = None
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and target is None:
            target = win.find_window("Notepad")
            time.sleep(0.1)
        if target is None or not win.focus_window(target.hwnd):
            check("Focused Notepad for the confirmation focus test", False)
            return

        near = win.find_window("notepda")
        check(
            "find_window('notepda') fuzzy-matches the live Notepad window",
            near is not None and near.hwnd == target.hwnd,
            near.title if near else "no match",
        )

        bar_hwnd = int(assistant.bar.winId())
        outcome: dict[str, bool] = {}
        worker = threading.Thread(
            target=lambda: outcome.update(
                approved=assistant._request_confirm("Harness focus probe")
            ),
            daemon=True,
        )
        worker.start()
        check(
            "Confirmation prompt takes keyboard focus from the target app",
            pump(lambda: win.foreground_hwnd() == bar_hwnd),
            f"foreground={win.foreground_hwnd()} bar={bar_hwnd}",
        )
        assistant.bar._resolve_confirm(True)  # exactly what pressing Enter does
        worker.join(timeout=5)
        check("Approval reaches the pipeline thread", outcome.get("approved") is True)
        check(
            "Focus is handed back to the target window after the answer",
            pump(lambda: win.foreground_hwnd() == target.hwnd),
            f"foreground={win.foreground_hwnd()} target={target.hwnd}",
        )

        # Kill switch during a pending confirmation: the wait must resolve NOW
        # as a decline, not sit out the 25s timeout, and the bar must drop out
        # of its confirm state.
        outcome.clear()
        worker = threading.Thread(
            target=lambda: outcome.update(
                approved=assistant._request_confirm("Harness kill probe")
            ),
            daemon=True,
        )
        worker.start()
        pump(lambda: assistant.bar._pending_confirm)
        tripped_at = time.perf_counter()
        assistant._trip_kill()
        worker.join(timeout=5)
        elapsed = time.perf_counter() - tripped_at
        check(
            "Kill switch unparks a pending confirmation as declined",
            not worker.is_alive() and outcome.get("approved") is False and elapsed < 2.0,
            f"resolved in {elapsed:.2f}s (timeout would be {settings.confirm_timeout_s:.0f}s)",
        )
        check(
            "Bar leaves its confirm state on kill",
            pump(lambda: not assistant.bar._pending_confirm),
        )
    finally:
        assistant.bar.hide()
        app.processEvents()
        assistant.pool.shutdown(wait=False, cancel_futures=True)
        if note.pid:
            subprocess.run(["taskkill", "/PID", str(note.pid), "/F"], capture_output=True)
            print(f"{INFO} closed Notepad (pid {note.pid}) without saving")


if __name__ == "__main__":
    raise SystemExit(main())
