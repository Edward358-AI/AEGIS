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
    return True, f"planner loaded at {loaded_ctx} context"
