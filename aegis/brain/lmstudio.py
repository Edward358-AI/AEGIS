"""LM Studio health checks via its native REST API.

The OpenAI-compatible ``/v1`` endpoint says nothing about *how* a model was
loaded. LM Studio's own ``/api/v0`` endpoint does, which matters here: JIT
auto-load ignores whatever parameters a previous explicit ``lms load`` used and
falls back to defaults. On this hardware that silently costs ~500 MB of VRAM
(8192 context and parallel 4 allocate KV cache for 32k tokens), which is the
difference between headroom and none.

It is a warning rather than an error - Aegis works fine either way, it just has
less room for the vision model.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from aegis.config import settings

log = logging.getLogger(__name__)


def _native_api_url(path: str) -> str:
    base = settings.lms_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/api/v0/{path.lstrip('/')}"


def loaded_models(timeout: float = 5.0) -> list[dict]:
    """Every model LM Studio knows about, with load state and parameters."""
    try:
        with urllib.request.urlopen(_native_api_url("models"), timeout=timeout) as response:
            return json.loads(response.read()).get("data", [])
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        log.debug("LM Studio native API unavailable", exc_info=True)
        return []


def _lms_cli() -> str | None:
    """Path to the `lms` CLI, which is how load parameters are actually set."""
    from pathlib import Path  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    if candidate.exists():
        return str(candidate)
    candidate = Path.home() / ".lmstudio" / "bin" / "lms"
    return str(candidate) if candidate.exists() else None


def reload_planner_tuned(timeout: float = 180.0) -> tuple[bool, str]:
    """Reload the planner at the budgeted context, replacing a drifted load.

    JIT auto-load ignores previous parameters and comes back at LM Studio's
    defaults, which costs ~500 MB here. Rather than nagging the user to run
    the command by hand every time, Aegis issues it.
    """
    import subprocess  # noqa: PLC0415

    cli = _lms_cli()
    if cli is None:
        return False, "the `lms` CLI was not found; reload manually"

    model = settings.planner_model
    try:
        subprocess.run([cli, "unload", model], capture_output=True, timeout=timeout)
        result = subprocess.run(
            [
                cli, "load", model,
                "-c", str(settings.planner_context),
                "--parallel", "1",
                "--ttl", "3600",
                "-y",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"reload failed: {exc}"

    if result.returncode != 0:
        return False, f"reload failed: {(result.stderr or result.stdout)[:160]}"
    return True, f"reloaded {model} at {settings.planner_context} context"


def ensure_planner_tuned() -> str:
    """Check the planner's load parameters and fix them if they have drifted."""
    ok, detail = check_planner_load()
    if ok:
        return detail
    if "not present" in detail:
        return detail  # a missing model is not something a reload can fix
    log.warning("%s", detail)
    fixed, fix_detail = reload_planner_tuned()
    return fix_detail if fixed else f"{detail} (auto-reload failed: {fix_detail})"


def check_planner_load() -> tuple[bool, str]:
    """Verify the planner is loaded with the context length we budgeted for."""
    models = loaded_models()
    if not models:
        return True, "could not query LM Studio load parameters"

    entry = next((m for m in models if m.get("id") == settings.planner_model), None)
    if entry is None:
        return False, f"planner model '{settings.planner_model}' is not present"
    if entry.get("state") != "loaded":
        return True, f"'{settings.planner_model}' not loaded yet; it will JIT-load on first use"

    loaded_ctx = entry.get("loaded_context_length")
    if loaded_ctx and loaded_ctx > settings.planner_context:
        return False, (
            f"'{settings.planner_model}' is loaded at {loaded_ctx} context, "
            f"not the budgeted {settings.planner_context}. This wastes VRAM needed "
            f"for the vision model. Reload with:\n"
            f"  lms load {settings.planner_model} "
            f"-c {settings.planner_context} --parallel 1 --ttl 3600 -y"
        )
    if loaded_ctx:
        return True, f"planner loaded at {loaded_ctx} context"
    return True, "planner loaded (context length unreported)"
