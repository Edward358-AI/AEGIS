"""The single executor.

Every action - whether it came from the LLM planner or (from M2) the Tier -1
deterministic router - is executed here. There is exactly one executor and one
whitelist, so the fast path cannot drift from the slow path or skip a guard.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import winreg
from dataclasses import dataclass, field
from typing import Callable

from aegis.core.timing import Trace
from aegis.safety.guard import InputGuard, PlanBudget
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


@dataclass
class ExecutionResult:
    verb: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)


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


class Executor:
    def __init__(
        self,
        guard: InputGuard,
        allowed_verbs: frozenset[str] | None = None,
        confirm: Callable[[Action], bool] | None = None,
        ask: Callable[[str], None] | None = None,
        answer: Callable[[str], None] | None = None,
    ) -> None:
        """Build an executor limited to ``allowed_verbs``.

        This is how the read path and the act path are kept apart: the query
        executor is constructed with only read-only verbs, so a side-effecting
        handler is not merely refused at runtime - it was never wired in.
        """
        self.guard = guard
        self._confirm = confirm
        self._ask = ask
        self._answer = answer

        # The whitelist. A verb absent from this mapping cannot be executed,
        # regardless of what the model or an injected instruction asks for.
        every: dict[str, Callable[[Action], tuple[str, dict]]] = {
            "launch_app": self._launch_app,
            "answer": self._answer_user,
            "ask_user": self._ask_user,
        }
        permitted = allowed_verbs if allowed_verbs is not None else ALL_VERBS
        self._handlers = {verb: fn for verb, fn in every.items() if verb in permitted}

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

            needs_confirm = action.verb in DESTRUCTIVE_VERBS or (
                tainted and action.verb not in READ_ONLY_VERBS
            )
            if needs_confirm:
                # Confidence never bypasses this gate, and it fails closed:
                # with no confirmation handler wired up, the action is refused.
                reason = "tainted context" if tainted else "destructive action"
                if self._confirm is None or not self._confirm(action):
                    results.append(
                        ExecutionResult(action.verb, False, f"blocked ({reason}), not confirmed")
                    )
                    log.warning("blocked %s (%s)", action.verb, reason)
                    continue

            try:
                if trace is not None:
                    with trace.stage(f"exec:{action.verb}"):
                        detail, data = handler(action)
                else:
                    detail, data = handler(action)
                results.append(ExecutionResult(action.verb, True, detail, data))
            except Exception as exc:
                log.exception("action %s failed", action.verb)
                results.append(ExecutionResult(action.verb, False, str(exc)))

        return results

    # --- handlers -------------------------------------------------------
    def _launch_app(self, action: Action) -> tuple[str, dict]:
        target = action.target.strip()
        if not _SAFE_TARGET.match(target):
            raise ExecutionError(f"unsafe application name: {target!r}")

        resolved = _APP_ALIASES.get(target.lower(), target)

        # A URI scheme (ms-settings:, http:) goes to the shell handler.
        if ":" in resolved and not resolved[1:2] == ":":
            proc = subprocess.Popen(["cmd", "/c", "start", "", resolved], shell=False)
            return f"opened {resolved}", {"pid": proc.pid, "shell": True}

        exe = resolved if resolved.lower().endswith(".exe") else f"{resolved}.exe"
        path = shutil.which(exe) or _app_paths_lookup(exe)
        if path is None:
            raise ExecutionError(f"could not find an application called {target!r}")

        proc = subprocess.Popen([path], shell=False)
        return f"launched {path}", {"pid": proc.pid, "path": path}

    def _ask_user(self, action: Action) -> tuple[str, dict]:
        if self._ask is not None:
            self._ask(action.target)
        return f"asked: {action.target}", {"question": action.target}

    def _answer_user(self, action: Action) -> tuple[str, dict]:
        if self._answer is not None:
            self._answer(action.target)
        return f"answered: {action.target}", {"answer": action.target}
