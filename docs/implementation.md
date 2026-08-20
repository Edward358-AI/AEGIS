# Aegis implementation plan

Recovered from the original architecture session and re-scoped 2026-08-19.

**The re-scope, in one paragraph:** Aegis is a computer-control agent, not a
personal assistant. General-purpose assistants already handle life tracking,
so the task store and its verbs (`task_add`, `task_complete`, `query_tasks`)
were removed at M1, M5's memory milestone is re-aimed at *agent* memory (what
Aegis has seen and done on the machine), and M6 keeps its machine-awareness
half while dropping the assistant half (proactive nudging, phone handoff).
Everything else — perception, control, voice, vision — is unchanged.

## Phases

### M0 — Thin vertical slice ✅ (2026-07-29)

Hotkey → command bar → 3B planner with constrained JSON → `launch_app` →
spoken result. Existed to retire risky assumptions; four of five estimates
were wrong. Measured numbers and forced decisions in
[m0-findings.md](m0-findings.md) — VRAM headroom (~2.25 GB, so no Tier-2
co-residency), latency (581–712 ms end-to-end), and the prompt-injection
result that shaped the whole security model: fencing is a hint, the decoder
grammar is the control.

### M1 — Execution & safety primitives ✅ (2026-07-30, hardened 2026-08-19)

Full input verb set (`focus_window`, `type_text`, `press_keys` + typo-tolerant
`launch_app`), the single gated executor, per-intent capability narrowing,
UIPI integrity guard, destructive-chord confirmation, kill switch proven
against a live typing burst. Hardening pass added: confirmation prompt that
wins and returns keyboard focus, kill-aware confirmations, and speech decided
from execution results rather than the plan's prediction.

*Re-scope note:* the original M1 sketch included `click_element` and
`read_screen` (they belong to M2, where elements exist) and the task verbs
(removed — see above).

### M2 — Perception (Tier 1) + Tier −1 router ← next

- `perception/` — COM-owning UIA thread, tree walk, pruning to lean JSON.
- `brain/router.py` — Tier −1 deterministic skill registry; no LLM for known
  commands. Justified by M0 as token economy and reliability, not latency.
- Element actions by **index into a trusted enumeration** — the model picks
  from a list built by code; it never invents coordinates or names
  (references-not-values). This is where the mouse verbs finally get wired.
- Escalation state machine in the planner; screen text enters the context
  fenced and *tainting* (side effects then require confirmation).
- **Validates:** whether UIA actually exposes Discord/Firefox on this
  machine — the answer sizes how much M4 matters.

### M3 — Voice loop

- `voice/stt.py` — faster-whisper small (int8, CPU).
- `voice/wake.py` — openwakeword, with push-to-talk as the reliable fallback.
- Streaming TTS off the token stream. (The original plan's Kokoro voice was
  overtaken by events: the Piper JARVIS voice landed at M0 and stays.)

### M4 — Vision fallback (Tier 2)

- Tier 2a first: Windows OCR + exact string match — zero VRAM, a *checkable*
  signal, sees text where UIA goes blind (canvas/Electron surfaces).
- Tier 2b: visual grounding (Qwen2.5-VL class) via JIT model swap. Decide
  here, with M2's data: accept the measured 8–12 s evict/reload round trip,
  or drop to a ~2 GB grounding model for co-residency.
- Coordinates map back through DPI-aware capture and are validated against
  **physical monitor rectangles**, never the virtual-desktop bounding box
  (the dead-zone lesson from m0-findings §6).

### M5 — Agent memory & HUD *(re-scoped)*

- ChromaDB recall over what Aegis has **seen and done** — actions taken,
  windows/elements observed, outcomes — with the clipboard exclusion list and
  pause toggle designed in from the start.
- `ui/hud.py` — ambient HUD: agent state, current action, thermals.
- ~~SQLite tasks/deadlines~~ — cut with the assistant role. "Memory" here
  means the agent remembering the machine, not the user's life.

### M6 — Machine awareness *(re-scoped, was "Ambient intelligence")*

- Game-Mode detection via LibreHardwareMonitor; VRAM eviction policy so the
  agent yields GPU memory when the machine needs it.
- ~~Context-aware nudging (cooldowns, snooze, per-app hard-off)~~ and
  ~~ntfy.sh mobile handoff~~ — cut: proactive assistant behaviour, not
  computer control.

## Execution tiers (unchanged)

| Tier | Mechanism | Milestone |
|---|---|---|
| −1 | Deterministic intent router (no LLM) | M2 |
| 0 | Direct API / IPC hooks | M6 |
| 1 | Windows UI Automation | M2 (window focus shipped at M1) |
| 2a | Windows OCR + string match | M4 |
| 2b | Visual grounding via JIT swap | M4 |
| 3 | Ask the user | shipped |

## Scope realism

The original estimate stands: the full plan is 6–12 months of solo work.
M0–M2 is the point where Aegis is usable daily; M2 remains the "v0.1"
checkpoint to reassess against measured Tier −1 hit rate and UIA coverage.
The re-scope shrinks M5 and M6, which only helps.
