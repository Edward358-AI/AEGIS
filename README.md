# Aegis

A local, low-latency AI desktop agent for Windows — a JARVIS-style assistant that
takes natural-language commands and drives the OS. Everything runs on-device.

**Status: M0 complete.** A full vertical slice works end to end: global hotkey →
command bar → local LLM planner (constrained JSON) → executor → speech. The
safety primitives (kill switch, verb whitelist, action ceiling, rate cap, taint
gate) are in place and tested.

## Requirements

- Windows 10, Python 3.12 (not 3.13 — `openwakeword` is unreliable there)
- [LM Studio](https://lmstudio.ai/) with `llama-3.2-3b-instruct`
- NVIDIA GPU (developed against an RTX 2060 Super, 8 GB)

## Setup

```bash
py -3.12 -m venv .venv && .venv/Scripts/python.exe -m pip install -e .
```

Load the planner with an explicitly capped context. LM Studio's defaults
(8192 context, parallel 4) cost ~500 MB more VRAM for no benefit here:

```bash
lms server start && lms load llama-3.2-3b-instruct -c 4096 --parallel 1 --ttl 3600 -y
```

## Run

```bash
.venv/Scripts/python.exe -m aegis
```

- **`Ctrl+Shift+Space`** — summon the command bar (`Alt+Space` is taken by
  PowerToys / Command Palette on most systems)
- **`Ctrl+Alt+Backspace`** — kill switch; halts all synthetic input immediately
- **`Esc`** — dismiss the bar

Try: *"open notepad"*, *"I need the calculator"*, *"fire up chrome"*.

**Voice:** speaks with a JARVIS-styled Piper voice (`var/voices/jarvis-high.onnx`,
MIT licensed, from [jgkawell/jarvis](https://huggingface.co/jgkawell/jarvis)),
falling back to Windows SAPI5 if that file is absent. Speed and the leading
silence padding (see below) are tunable via `AEGIS_TTS_SPEED` and
`AEGIS_TTS_LEAD_SILENCE_MS`.

If your output device is Bluetooth, the first ~1s of a fresh clip can get
dropped while the link wakes from idle — every rendered clip is padded with
900ms of silence to absorb that (`aegis/voice/tts.py::_prepend_silence`).
Harmless on wired output.

## Verify

```bash
.venv/Scripts/python.exe scripts/m0_check.py
```

Checks VRAM headroom, planner latency and schema validity, prompt-injection
handling, and all four safety mechanisms. Measured results and what they changed
about the design are in [docs/m0-findings.md](docs/m0-findings.md).

## Architecture

Execution is tiered, cheapest first, so slow probabilistic vision is a last
resort rather than the default:

| Tier | Mechanism | Status |
|---|---|---|
| −1 | Deterministic intent router (no LLM) | M2 |
| 0 | Direct API / IPC hooks | M6 |
| 1 | Windows UI Automation | M2 |
| 2a | Windows OCR + string match (0 GB, hard confidence signal) | M4 |
| 2b | Visual grounding, Qwen2.5-VL via JIT swap | M4 |
| 3 | Ask the user | done |

Tier 2 is split because escalation needs a *checkable* first stage. A small VLM
returns confident coordinates with no signal about whether they are right, so
"escalate on failure" degenerates into never escalating. OCR gives an exact
string match or nothing, costs no VRAM, and sees the text in canvas- and
Electron-rendered apps where UIA goes blind.

```
aegis/
  main.py          entrypoint; sets DPI awareness before Qt exists
  config.py        every tunable, overridable via AEGIS_* env vars
  core/            DPI, latency tracing, hotkeys + kill switch
  safety/          abort event, rate cap, action ceiling
  schema/          pydantic actions -> JSON Schema for constrained decoding
  brain/           persona prompt, untrusted-content fence, LM Studio planner
  execute/         SendInput primitives, the single gated executor
  ui/              command bar
  voice/           TTS with pre-rendered acknowledgement cache
```

### Two things worth knowing before changing anything

**The planner is not trusted.** Screen text entering its context is untrusted
input, and the model obeys injected instructions at a rate that swings between
0/4 and 4/4 across runs — prompt fencing is a hint, not a control. Three things
enforce the boundary instead, none relying on the model's cooperation:

1. **Capability narrowing** — the decoder grammar is built per turn from a
   classified intent, so out-of-scope verbs are undecodable (measured 0/4 leaked
   against 4/4 unnarrowed). The classifier sees only the user's request.
2. **Split executors** — the query path has no side-effecting handler wired in.
3. **The taint gate** — plans built from untrusted context need confirmation for
   side effects, and fail closed.

Adding a verb means registering it in three places: the executor whitelist,
`READ_ONLY_VERBS`, and `CAPABILITY_SETS`. See
[docs/m0-findings.md](docs/m0-findings.md) §3 for the measurements.

**DPI awareness must be set before Qt loads.** `main.py` calls
`set_dpi_awareness()` at import time, before `QApplication` exists. Move it and
every coordinate silently breaks on scaled displays.
