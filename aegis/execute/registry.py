"""The single executor.

Every action - whether it came from the LLM planner or (from M2) the Tier -1
deterministic router - is executed here. There is exactly one executor and one
whitelist, so the fast path cannot drift from the slow path or skip a guard.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import subprocess
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from aegis.core.integrity import can_send_input_to_foreground
from aegis.core.timing import Trace
from aegis.execute import windows as win
from aegis.execute.sendinput import InputSender
from aegis.memory import db
from aegis.safety.guard import (
    AbortedError,
    InputGuard,
    PlanBudget,
    is_destructive_chord,
    normalise_chord,
)
from aegis.schema.actions import ALL_VERBS, DESTRUCTIVE_VERBS, READ_ONLY_VERBS, Action, Plan

log = logging.getLogger(__name__)

# Targets are never passed to a shell, but keeping them to a boring character
# set removes any doubt about argument smuggling.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9 ._:\\/-]{1,120}$")

_APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "settings": "ms-settings:",
}

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]


@dataclass
class ExecutionResult:
    verb: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)
    # True when the confirmation gate said no (user declined, timed out, or no
    # handler was wired). Callers report that differently from a failure:
    # nothing went wrong, a human just wasn't convinced.
    declined: bool = False


class ExecutionError(RuntimeError):
    pass


def _app_paths_lookup(exe: str) -> str | None:
    """Resolve an executable through the Windows App Paths registry.

    This is how Chrome, Firefox and friends are found when they are not on
    PATH, which is the common case for user-installed applications.
    """
    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    return value.strip('"')
        except OSError:
            continue
    return None


def _start_menu_shortcuts() -> dict[str, str]:
    """Lowercased shortcut stem -> shortcut path, across both Start Menus."""
    found: dict[str, str] = {}
    for root in _START_MENU_DIRS:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            found.setdefault(lnk.stem.lower(), str(lnk))
    return found


def _start_menu_lookup(name: str) -> str | None:
    """Find a Start Menu shortcut whose name matches.

    This is what makes "open obsidian" work for apps that are neither on PATH
    nor registered in App Paths - which is most modern user-installed
    software. Shortcuts are launched via the shell, which resolves the .lnk.
    """
    needle = name.strip().lower()
    if not needle:
        return None
    shortcuts = _start_menu_shortcuts()
    if needle in shortcuts:
        return shortcuts[needle]
    best: tuple[int, str] | None = None
    for stem, path in shortcuts.items():
        if stem.startswith(needle) or needle in stem:
            score = len(stem)
            if best is None or score < best[0]:
                best = (score, path)
    return best[1] if best else None


def _fuzzy_app_correction(name: str) -> str | None:
    """Closest known application name for a near-miss, or None.

    Deterministic typo repair ("sptofiy" -> "spotify") with no model in the
    loop, matched only against names that are actually launchable here: the
    alias table plus the Start Menu. The worst case is therefore launching a
    real installed app and saying so, never conjuring an arbitrary target.
    Matching rules (ratio + transposition) live in windows.is_near_name.
    """
    candidates = sorted(set(_APP_ALIASES) | set(_start_menu_shortcuts()))
    best: tuple[float, str] | None = None
    for candidate in candidates:
        if not win.is_near_name(name, candidate):
            continue
        score = difflib.SequenceMatcher(None, name, candidate).ratio()
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


class Executor:
    def __init__(
        self,
        guard: InputGuard,
        allowed_verbs: frozenset[str] | None = None,
        confirm: Callable[[str], bool] | None = None,
        ask: Callable[[str], None] | None = None,
        answer: Callable[[str], None] | None = None,
    ) -> None:
        """Build an executor limited to ``allowed_verbs``.

        This is how the read path and the act path are kept apart: the query
        executor is constructed with only read-only verbs, so a side-effecting
        handler is not merely refused at runtime - it was never wired in.

        ``confirm`` receives a human-readable description of the pending
        action and returns True to proceed. With no handler wired up, anything
        needing confirmation is refused, so the gate fails closed.
        """
        self.guard = guard
        self._confirm = confirm
        self._ask = ask
        self._answer = answer
        self.sender = InputSender(guard)

        # The whitelist. A verb absent from this mapping cannot be executed,
        # regardless of what the model or an injected instruction asks for.
        every: dict[str, Callable[[Action], tuple[str, dict]]] = {
            "launch_app": self._launch_app,
            "focus_window": self._focus_window,
            "type_text": self._type_text,
            "press_keys": self._press_keys,
            "task_add": self._task_add,
            "task_complete": self._task_complete,
            "query_tasks": self._query_tasks,
            "answer": self._answer_user,
            "ask_user": self._ask_user,
        }
        permitted = allowed_verbs if allowed_verbs is not None else ALL_VERBS
        self._handlers = {verb: fn for verb, fn in every.items() if verb in permitted}

    # --- confirmation ---------------------------------------------------
    def _needs_confirmation(self, action: Action, tainted: bool) -> str | None:
        """Human-readable reason this action needs a yes, or None if it doesn't."""
        if action.verb in DESTRUCTIVE_VERBS:
            return f"{action.verb} is a destructive action"
        if tainted and action.verb not in READ_ONLY_VERBS:
            return "this plan was built from untrusted screen content"
        if action.verb == "press_keys" and is_destructive_chord(action.target):
            return f"{normalise_chord(action.target)} closes or discards something"
        if action.verb == "launch_app" and self._is_unrecognised_app(action.target):
            return f"{action.target!r} is not a known application"
        return None

    @staticmethod
    def _is_unrecognised_app(target: str) -> bool:
        name = target.strip().lower()
        if name in _APP_ALIASES:
            return False
        # A raw path or URI scheme is exactly the case worth confirming.
        if ":" in target or "\\" in target or "/" in target:
            return True
        exe = name if name.endswith(".exe") else f"{name}.exe"
        if shutil.which(exe) or _app_paths_lookup(exe) or _start_menu_lookup(name):
            return False
        # A repairable near-miss of a known app is recognised: prompting
        # "Launch sptofiy?" would just parrot the typo at the user.
        return _fuzzy_app_correction(name) is None

    def run(
        self,
        plan: Plan,
        trace: Trace | None = None,
        tainted: bool = False,
    ) -> list[ExecutionResult]:
        """Execute a plan.

        ``tainted`` must be True whenever untrusted observed content (screen
        text, window titles, page contents) was in the planner's context. M0
        measurements showed a 3B model will sometimes obey an injected
        instruction despite the prompt-level fence - it complied on one run and
        refused on the next with identical input. So the fence is treated as a
        hint, and this gate is the actual guarantee: a tainted plan cannot take
        a side-effecting action without explicit human confirmation.
        """
        budget = PlanBudget()
        results: list[ExecutionResult] = []

        for action in plan.actions:
            budget.consume()
            self.guard.check()

            handler = self._handlers.get(action.verb)
            if handler is None:
                reason = (
                    "verb not permitted on this path"
                    if action.verb in ALL_VERBS
                    else "unknown verb"
                )
                results.append(ExecutionResult(action.verb, False, f"{reason} - refused"))
                log.warning("refused %r: %s", action.verb, reason)
                continue

            reason = self._needs_confirmation(action, tainted)
            if reason is not None:
                # Confidence never bypasses this gate, and it fails closed:
                # with no confirmation handler wired up, the action is refused.
                question = self._describe(action)
                if self._confirm is None or not self._confirm(question):
                    results.append(
                        ExecutionResult(
                            action.verb, False, f"declined ({reason})", declined=True
                        )
                    )
                    log.warning("blocked %s: %s", action.verb, reason)
                    continue
                # The kill switch may trip while the question is open, and the
                # user may still answer yes a beat later. A yes that predates
                # the halt is stale - never act on it.
                self.guard.check()

            try:
                if trace is not None:
                    with trace.stage(f"exec:{action.verb}"):
                        detail, data = handler(action)
                else:
                    detail, data = handler(action)
                results.append(ExecutionResult(action.verb, True, detail, data))
            except AbortedError:
                # Must escape rather than be recorded as an ordinary failure.
                # Swallowing it here would let a kill switch pressed during the
                # final action of a plan look like success to the caller.
                log.warning("aborted during %s", action.verb)
                raise
            except Exception as exc:
                log.exception("action %s failed", action.verb)
                results.append(ExecutionResult(action.verb, False, str(exc)))

        return results

    @staticmethod
    def _describe(action: Action) -> str:
        """Plain-language description of a pending action, for the confirm prompt."""
        target = action.target
        phrasing = {
            "launch_app": f"Launch {target}",
            "focus_window": f"Switch to {target}",
            "type_text": f"Type {target!r} into the focused window",
            "press_keys": f"Press {normalise_chord(target)}",
            "task_add": f"Add task {target!r}",
            "task_complete": f"Mark {target!r} complete",
        }
        return phrasing.get(action.verb, f"{action.verb}: {target}")

    # --- handlers -------------------------------------------------------
    def _launch_app(self, action: Action) -> tuple[str, dict]:
        target = action.target.strip()
        if not _SAFE_TARGET.match(target):
            raise ExecutionError(f"unsafe application name: {target!r}")

        launched = self._try_launch(target)
        if launched is not None:
            return launched

        # Near-miss repair. The planner normalises most typos itself (it
        # writes the target string), but when one slips through verbatim the
        # lookup should not dead-end on it. The correction is reported in the
        # result rather than applied silently.
        corrected = _fuzzy_app_correction(target.lower())
        if corrected is not None:
            launched = self._try_launch(corrected)
            if launched is not None:
                detail, data = launched
                return (
                    f"{detail} (read {target!r} as {corrected!r})",
                    {**data, "corrected_from": target},
                )

        raise ExecutionError(f"could not find an application called {target!r}")

    def _try_launch(self, target: str) -> tuple[str, dict] | None:
        """One pass through the resolution chain, or None if nothing matched."""
        resolved = _APP_ALIASES.get(target.lower(), target)

        # A URI scheme (ms-settings:, http:) goes to the shell handler.
        if ":" in resolved and not resolved[1:2] == ":":
            proc = subprocess.Popen(["cmd", "/c", "start", "", resolved], shell=False)
            return f"opened {resolved}", {"pid": proc.pid, "shell": True}

        exe = resolved if resolved.lower().endswith(".exe") else f"{resolved}.exe"
        path = shutil.which(exe) or _app_paths_lookup(exe)
        if path is not None:
            proc = subprocess.Popen([path], shell=False)
            return f"launched {path}", {"pid": proc.pid, "path": path}

        shortcut = _start_menu_lookup(target)
        if shortcut is not None:
            # .lnk files must go through the shell to be resolved.
            proc = subprocess.Popen(["cmd", "/c", "start", "", shortcut], shell=False)
            return f"launched {Path(shortcut).stem}", {"pid": proc.pid, "shortcut": shortcut}

        return None

    def _focus_window(self, action: Action) -> tuple[str, dict]:
        match = win.find_window(action.target)
        if match is None:
            open_titles = ", ".join(w.title[:30] for w in win.list_windows()[:5])
            raise ExecutionError(
                f"no window matching {action.target!r} (open: {open_titles})"
            )
        if not win.focus_window(match.hwnd):
            raise ExecutionError(f"Windows refused to focus {match.title!r}")
        return f"focused {match.title}", {"hwnd": match.hwnd, "title": match.title}

    def _type_text(self, action: Action) -> tuple[str, dict]:
        allowed, reason = can_send_input_to_foreground()
        if not allowed:
            # UIPI would swallow the keystrokes while SendInput still reports
            # success, so refuse loudly rather than claim to have typed.
            raise ExecutionError(f"I can't type there - {reason}")
        self.sender.type_text(action.target)
        return f"typed {len(action.target)} characters", {"length": len(action.target)}

    def _press_keys(self, action: Action) -> tuple[str, dict]:
        allowed, reason = can_send_input_to_foreground()
        if not allowed:
            raise ExecutionError(f"I can't send keys there - {reason}")
        chord = normalise_chord(action.target)
        self.sender.press_keys(action.target)
        return f"pressed {chord}", {"chord": chord}

    def _task_add(self, action: Action) -> tuple[str, dict]:
        task = db.add_task(action.target)
        return f"logged task: {task.title}", {"id": task.id, "title": task.title}

    def _task_complete(self, action: Action) -> tuple[str, dict]:
        task = db.complete_task(action.target)
        if task is None:
            raise ExecutionError(f"no open task matching {action.target!r}")
        return f"completed: {task.title}", {"id": task.id, "title": task.title}

    def _query_tasks(self, action: Action) -> tuple[str, dict]:
        summary = db.summarise_open()
        if self._answer is not None:
            self._answer(summary)
        return summary, {"summary": summary}

    def _ask_user(self, action: Action) -> tuple[str, dict]:
        if self._ask is not None:
            self._ask(action.target)
        return f"asked: {action.target}", {"question": action.target}

    def _answer_user(self, action: Action) -> tuple[str, dict]:
        if self._answer is not None:
            self._answer(action.target)
        return f"answered: {action.target}", {"answer": action.target}
