"""Central configuration. Every tunable in the system lands here.

Values can be overridden by environment variables prefixed with ``AEGIS_``
(e.g. ``AEGIS_PLANNER_MODEL=...``) or by a ``.env`` file at the repo root.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=REPO_ROOT / ".env",
        extra="ignore",
    )

    # --- LM Studio -------------------------------------------------------
    lms_base_url: str = "http://localhost:1234/v1"
    lms_api_key: str = "lm-studio"  # LM Studio ignores the value but the SDK requires one
    planner_model: str = "llama-3.2-3b-instruct"
    vision_model: str = "qwen2.5-vl-3b-instruct"
    # JIT auto-load ignores this and uses LM Studio's defaults, so Aegis checks
    # the real loaded value at startup and warns. See brain/lmstudio.py.
    planner_context: int = 4096
    planner_timeout_s: float = 30.0
    planner_temperature: float = 0.2
    planner_max_tokens: int = 256

    # --- Hotkeys ---------------------------------------------------------
    # The kill switch deliberately avoids Ctrl+Shift+Esc, which Windows reserves
    # for Task Manager and which a user-mode hook cannot suppress.
    #
    # Alt+Space and Ctrl+Alt+Space are both already claimed on this machine
    # (PowerToys / Command Palette), so the command bar uses Ctrl+Shift+Space.
    # Ctrl+Space is free globally but would shadow autocomplete in editors.
    hotkey_command_bar: str = "ctrl+shift+space"
    hotkey_kill: str = "ctrl+alt+backspace"

    # --- Safety ----------------------------------------------------------
    max_actions_per_plan: int = 12
    max_inputs_per_second: float = 40.0

    # --- Vision (M4) -----------------------------------------------------
    vision_max_edge: int = 1024
    vision_min_free_vram_mb: int = 4096

    # --- Persona ---------------------------------------------------------
    persona_name: str = "Aegis"
    address_user_as: str = "sir"
    tts_enabled: bool = True
    tts_voice_hint: str = "en-GB"

    # "piper" (preferred - a JARVIS-styled en_GB voice), "sapi5" (built-in
    # Windows fallback, no download needed), or "auto" (piper if the model
    # files below are present, else sapi5).
    tts_backend: str = "auto"
    tts_piper_model: Path = REPO_ROOT / "var" / "voices" / "jarvis-high.onnx"
    tts_piper_config: Path = REPO_ROOT / "var" / "voices" / "jarvis-high.onnx.json"

    # Piper's length_scale: lower = faster speech. 1.0 is the model's natural
    # pace; 0.8 measured ~18% shorter with no audible quality loss, 0.65 and
    # below starts to garble.
    tts_speed: float = 0.8

    # Leading silence prepended to every rendered clip, including cached
    # acknowledgements. This is a workaround for Bluetooth/wireless output
    # devices: the link often needs to wake from an idle low-power state
    # before it actually carries audio, and anything sent during that window
    # is dropped rather than buffered - it sounds like the first ~1s of
    # speech is missing. Padding with disposable silence spends that wake-up
    # time on nothing rather than on real words. Irrelevant, but harmless, on
    # wired output.
    tts_lead_silence_ms: int = 900

    # --- Paths -----------------------------------------------------------
    data_dir: Path = REPO_ROOT / "var"

    @property
    def tts_cache_dir(self) -> Path:
        return self.data_dir / "tts_cache"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.tts_cache_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
