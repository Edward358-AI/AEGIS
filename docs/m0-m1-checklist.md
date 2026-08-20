# M0–M1 manual acceptance checklist

The automated layer is `scripts/m0_check.py` and `scripts/m1_check.py` — run
those first. This checklist is the human layer: the things a script cannot
judge (voice quality, focus feel, whether the butler is lying to you). Each
item is *say this → expect this*. Test on a quiet machine with LM Studio up.

## 0. Preflight

- [ ] `m0_check.py` passes (needs LM Studio + hands off the mouse for the cursor test)
- [ ] `m1_check.py` passes, including live sections 7–8 (hands off keyboard ~20s)
- [ ] Start Aegis: log shows `planner loaded at 4096 context` (or an auto-reload fixing it) — not a drifted 8192
- [ ] Launch Aegis a second time → "already running" box, no second instance

## 1. Hotkeys and the bar

- [ ] `Ctrl+Shift+Space` summons the bar; again hides it; `Esc` dismisses
- [ ] Bar appears on whichever monitor the cursor is on — check **both** (the second one especially, with its offset + 1.75 DPI)
- [ ] `Ctrl+Alt+Backspace` while idle → "Halted." status, no Task Manager popping up

## 2. Launching

- [ ] "open notepad" → opens, spoken confirmation *after* the ~100ms "Right away, sir" ack
- [ ] "open sptofiy" → **Spotify launches, no confirmation prompt** (typo repair)
- [ ] "open [a Start-Menu-only app you have — obsidian, whatever]" → launches via shortcut
- [ ] "open settings" → Windows Settings (URI scheme path)
- [ ] "open qzxvbn" → asks first ("not a known application"); press `Esc` → **"Very well, sir."** — no success line, no "that didn't work"
- [ ] Same, but press `Enter` → tries, fails honestly: "That didn't work, sir."
- [ ] "I need the calculator" → calc actually opens (the M0 empty-plan bug: speech with zero actions — must be impossible now)

## 3. Focus, typing, chords

- [ ] "switch to firefox" → Firefox actually foregrounds (not just a taskbar flash)
- [ ] "switch to discrod" → typo still finds Discord (window near-match)
- [ ] "open notepad and type hello world" → chains focus-then-type into Notepad
- [ ] "type café résumé 你好" into Notepad → exact Unicode arrives (layout-independent)
- [ ] "press ctrl+s" in Notepad with unsaved text → Save dialog, **no** confirmation (benign chord)

## 4. The confirmation gate (the M1-hardening headline)

With Notepad focused and some throwaway text in it:

- [ ] "press alt+f4" → bar pops the question **and has keyboard focus** — your `Enter` answers the bar, it does NOT type a newline into Notepad
- [ ] After `Enter` → **Notepad** closes (never the bar itself), focus handed back cleanly
- [ ] Again with "press ctrl+w" but answer `Esc` → nothing closes, status "Cancelled.", voice says "Very well, sir." — and *not* the pre-written success line first
- [ ] Let a confirmation sit unanswered → defaults to NO at ~25s: "Timed out - I did nothing."

## 5. Kill switch under fire

- [ ] "type [several sentences of anything]" then hit `Ctrl+Alt+Backspace` mid-burst → typing stops **within a keystroke**, status "Halted.", no success speech
- [ ] Trip the kill switch while a confirmation question is open → question dies **immediately** as declined (not after the 25s timeout), bar leaves confirm mode
- [ ] Next command after a kill works normally (guard resets per command)

## 6. Voice and honesty

- [ ] Every command: cached ack fires near-instantly, real reply follows; utterances never overlap
- [ ] It's the JARVIS Piper voice, not SAPI David
- [ ] Bluetooth (JBL): the **first word is not clipped** (lead-silence pad at 400ms — if it clips, bump `AEGIS_TTS_LEAD_SILENCE_MS`)
- [ ] Open an **admin** terminal, focus it, "type hello" → flat refusal naming the integrity boundary — never "Done, sir" over silently-swallowed input (UIPI honesty)
- [ ] Steady-state command → action lands well under a second; first command after a long idle may take ~8s once (model reload — expected, not a bug)

## Out of scope until later milestones

Don't test these; they're designed-out for now: reading the screen or reacting
to dialogs (M2/M4), clicking anything (M2 wires the mouse verbs to element
picking), voice input / wake word (M3), research-grade answers (3B model, no
internet), typing into elevated windows (deliberate OS boundary, stays).
Task tracking and reminders are gone on purpose — Aegis is a computer-control
agent; life tracking lives with your general-purpose assistant.
