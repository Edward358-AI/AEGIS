# M0 findings — measured, not estimated

M0 existed to retire the risky assumptions in the architecture plan before
building on them. Four of the five estimates turned out to be wrong. Measured on
the target machine (RTX 2060 Super 8 GB, driver 610.47, Win10, Python 3.12.10)
with `scripts/m0_check.py`.

## 1. Latency is well inside budget — the LLM is not the bottleneck

| Path | Budgeted | Measured |
|---|---|---|
| Text → LLM → execute | 1500–2500 ms | **581–712 ms** |
| Execute alone (`launch_app`) | — | 10–13 ms |
| First request after model load | — | 8573 ms (one-off) |

The 3B planner at 4096 context returns a complete structured plan in well under
a second. Two consequences:

* The Tier −1 router is still worth building, but its justification shifts from
  "the LLM is too slow" to "the LLM is unnecessary work for known commands."
  The urgency is lower than the plan assumed.
* The cold-start penalty is the real latency cliff, not steady-state inference.
  Keep the planner pinned with a long TTL; a 60-minute TTL is set.

## 2. VRAM — the plan underestimated both the desktop and the planner

| Component | Plan estimate | Measured |
|---|---|---|
| Desktop baseline | 1.5 – 2.5 GB | **3.2 – 3.4 GB** |
| Planner @ default (8192 ctx, parallel 4) | ~2.15 GB | **3.04 GB** |
| Planner @ 4096 ctx, parallel 1 | ~2.15 GB | **2.54 GB** |
| Total resident | ~4.7 GB | **~5.94 GB** |
| Headroom for the VLM | ~3.3 GB | **~2.25 GB** |

The desktop baseline is high because of the actual working set — Firefox, Steam,
Beeper, ShareX, PowerToys and Command Palette all hold GPU memory. That is the
honest number to design against, not an idle desktop.

LM Studio's default `parallel 4` was silently allocating KV cache for
4 × 8192 tokens. Loading with `-c 4096 --parallel 1` recovers ~500 MB:

```bash
lms load llama-3.2-3b-instruct -c 4096 --parallel 1 --ttl 3600 -y
```

**Consequence for Tier 2: co-residency is not possible on this machine.**
Qwen2.5-VL-3B needs ~4 GB and there is ~2.25 GB free. The adaptive policy in the
plan would therefore *always* select evict-then-reload. With a measured Llama
load time of 3.33 s, a Tier 2 round trip costs roughly 8–12 s.

This needs a decision at M4. Options, in the order they seem attractive:

1. **Accept it** and treat Tier 2 as a genuine last resort, which the tier design
   already intends. A rare 10-second fallback may be acceptable.
2. **Use a smaller grounding model** (Moondream2 / Florence-2-base, ~1.5–2 GB)
   so both stay resident. Weaker at complex UI reasoning, but ~10x faster to reach.
3. **Reduce the desktop baseline**, which is not really an engineering answer.

## 3. Prompt-injection: the fence does not hold, the grammar does

With an injected instruction inside fenced "observed data", the planner was asked
a benign question. Compliance across repeated runs of 4 trials each:

| Run | Obeyed the injection |
|---|---|
| 1 | 3 / 4 |
| 2 | 0 / 4 |
| 3 | 4 / 4 |

Identical input, wildly different outcomes. **Prompt-level fencing is a hint, not
a control**, and any test asserting the model resists will pass and fail at
random. This is not a tuning problem — a 3B model is not a security boundary.

Three controls replace it, none of which depend on the model's cooperation.

### 3a. Capability narrowing — the structural fix

The response schema is built per turn from a classified intent, and the verb enum
in the decoder grammar is narrowed to that capability set. On a `QUERY` turn
`launch_app` is simply not in the grammar, so it cannot be generated.

Measured with the identical injected payload: **0/4 leaked, against 4/4 in the
same run unnarrowed.** Not persuasion — the tokens are undecodable.

The intent classifier (`aegis/brain/intent.py`) sees **only the user's own
request**, never observations, so it has no channel to inject through. A hostile
request can at worst push classification toward a *narrower* capability set,
never a wider one. A rule-based fast path resolves common phrasings in under a
millisecond, falling through to a small constrained LLM call otherwise, and
failing closed to `UNKNOWN` (which grants only `ask_user`).

### 3b. Split read and act paths

Executors are constructed with an explicit verb whitelist, so the query-path
executor has no side-effecting handler wired into it at all. A `launch_app`
action arriving on the read path is refused because the handler does not exist,
not because a runtime check rejected it.

### 3c. The taint gate — backstop

For the residual case that genuinely needs both untrusted input and side effects
("click the download button on this page"), a plan built with untrusted content
in context is marked `tainted` and cannot take a side-effecting action without
explicit human confirmation. It fails closed.

This ordering matters. If the taint gate were the only control, every screen-aware
action would prompt, the user would click through reflexively, and the gate would
become theatre. 3a and 3b keep the prompt rate low enough that a prompt still
means something.

**Residual risk, stated plainly:** on a `QUERY` turn the model can still be
induced to *answer wrongly* from poisoned screen content. That is an information
integrity problem rather than an execution one, and it is not currently
mitigated.

## 4. `Alt+Space` is unavailable

`RegisterHotKey` fails with error 1409 (`ERROR_HOTKEY_ALREADY_REGISTERED`) for
both `alt+space` and `ctrl+alt+space` — PowerToys and/or Microsoft Command
Palette own them.

Probe results: `ctrl+space`, `ctrl+alt+a`, `ctrl+shift+space`, `ctrl+alt+enter`
and `ctrl+alt+backspace` are all free. The command bar now defaults to
**`ctrl+shift+space`**; `ctrl+space` was rejected because it would shadow
autocomplete inside editors.

The kill switch binding (`ctrl+alt+backspace`) registers cleanly, confirming the
plan's correction away from `ctrl+shift+esc` was necessary and sufficient.

## 5. No British voice is installed

SAPI5 exposes only *Microsoft David* (en-US male), *Zira* (en-US female) and
*Huihui* (zh-CN). The persona currently speaks with an American accent.

M0 ships the SAPI5 backend deliberately — it needs no model download, and the
acknowledgement cache mechanism is what actually matters at this stage (measured
warm: 4/4 phrases pre-rendered). Kokoro at M3 supplies the British male voice
(`bm_*`) behind the same `TtsBackend` interface. Installing the Windows en-GB
speech pack would be a stopgap, but Kokoro is the real fix.

## 6. The virtual desktop has dead zones

Measured layout:

```
virtual desktop bounding box:  4242 x 1703  at (0, -551)
  monitor 0 (primary):  (0, 0)     -> (2048, 1152)
  monitor 1:            (2048, -551) -> (4242, 683)
```

The monitors differ in size and vertical offset, so the bounding box is
substantially larger than the union of the two screens. Points such as
`(8, -543)` or `(4233, 1143)` are inside the bounding box but on no physical
monitor, and Windows silently clamps the cursor to the nearest valid position —
producing a 2186 px discrepancy that looks exactly like a normalisation bug.

The normalisation itself is correct: worst error is **1 px across 6 points**
spanning both monitors, including negative Y.

This matters beyond the test. At M4 the vision model returns coordinates in
screenshot space that must be mapped back to true screen space; any such
coordinate needs validating against the *monitor* rectangles, not the virtual
desktop bounding box, or Tier 2 will occasionally click somewhere unintended.
`win32api.EnumDisplayMonitors()` is the source of truth.

## 7. JIT auto-load silently discards tuned load parameters

After the model idles out and LM Studio JIT-reloads it on the next request, it
comes back at **8192 context / parallel 4** regardless of how it was loaded
before — costing back the ~500 MB that section 2 recovered. This was caught only
because the harness re-measured; it produces no error and no log line.

LM Studio's native API (`/api/v0/models`, distinct from the OpenAI-compatible
`/v1`) reports `loaded_context_length`, so Aegis now checks at startup and warns
with the exact command to fix it (`aegis/brain/lmstudio.py`). The harness asserts
it too.

The load must be applied explicitly and re-applied after any unload:

```bash
lms load llama-3.2-3b-instruct -c 4096 --parallel 1 --ttl 3600 -y
```

## 8. Without `minItems`, the model returns plans that do nothing

Observed: for *"I need the calculator"* the planner returned
`speech="Calculator is open, sir."` with an **empty actions array**. It announces
success and does nothing — the worst possible failure mode, because it looks like
it worked.

The schema now sets `min_length=1` on `actions` (`minItems` in the grammar), so
an empty plan cannot be generated. Every capability set includes `ask_user`, so
there is always a valid action available. `max_length` mirrors the executor's
action ceiling at the decoder, so an overlong plan cannot be generated either.

General lesson: constrained decoding removes malformed output, but only the
constraints you actually express. Cardinality needed stating.

## What carries forward

* Load the planner with `-c 4096 --parallel 1`; do not accept LM Studio defaults.
* Tier −1 is a token-economy and reliability win, not a latency rescue.
* Decide the Tier 2 model question at M4 — co-residency is off the table.
* Every new verb needs three registrations, not one: the executor whitelist,
  `READ_ONLY_VERBS`, and the relevant entries in `CAPABILITY_SETS`. Miss the
  second and the taint gate cannot classify it; miss the third and it is either
  unreachable or reachable from the read path.
* Validate any computed click coordinate against physical monitor rectangles,
  never against the virtual desktop bounding box.
* Re-apply the tuned `lms load` after any unload, and trust the startup warning
  over memory.
* Express cardinality in the schema, not just types.
