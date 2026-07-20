# amtr v2 — SPEC (normative)

A diagnostic instrument for Claude Code sessions: the context window as an address
space, file access as a seismograph, cache economics as a per-turn ledger.

Two processes, a split-process instrument (a Rust UI + a Python engine):

```
┌──────────────────────────┐  JSON lines over stdout/stdin  ┌──────────────────────────┐
│ amtr (Rust ratatui bin)  │ ── Control (UI→Engine) ──▶     │ amtr_engine.py           │
│ owns ONLY the terminal   │                                │ (python3 ≥3.9, stdlib)   │
│ UI: layout, keys, render │ ◀── Update (Engine→UI) ──      │ owns ALL data: discovery,│
└──────────────────────────┘                                │ tailing, accounting      │
                                                            └──────────────────────────┘
```

This document is normative and self-contained: `rust/src/*.rs` and `amtr_engine.py`
are each implemented against this spec alone and will link. Where this spec is
silent, copy this split-process discipline.

Wire rules: one JSON object per line, `\n`-terminated, UTF-8, discriminated by a
`"type"` string (serde internally-tagged enums, snake_case). Unknown message types
MUST be ignored (Rust: parse failure → `log` entry, never fatal; Python: unknown
Control types ignored with a forward-compatibility comment). Malformed lines MUST
be skipped. Engine stderr is inherited and is NOT part of the protocol.

---

## (a) Data model & vocabulary

- **R (resident)** — authoritative context size: `input_tokens + cache_read_input_tokens
  + cache_creation_input_tokens` of the newest non-synthetic assistant record
  (`requestId` present, model ≠ `"<synthetic>"`) in the MAIN transcript (sidechains
  live in separate files and never pollute it).
- **C (waterline)** — `cache_read_input_tokens` of that record: the exact end of the
  cache-served prefix.
- **B (budget)** — context budget. Rungs: `{200_000, 1_000_000}`. Initial: 1M if
  `~/.claude/settings.json` `model` contains `[1m]`, else 200k. Auto-bump to the next
  rung (with a `log`) if `R` or any compaction `preTokens` exceeds the current rung.
  `--budget N` overrides and pins.
- **turn** — one assistant API turn = one non-synthetic assistant record carrying
  usage with a NEW `requestId` (multiple assistant records may share a requestId —
  streamed content blocks; the LAST usage per requestId wins). Turn index is
  0-based over the session.
- **Categories** (`cat` enum, wire strings): `overhead`, `user`, `assistant`,
  `thinking`, `reasoning`, `file`, `bash`, `tool`, `attach`, `summary`.
- **Hidden reasoning (`reasoning`)** — extended-thinking models (Fable 5) write
  EMPTY `thinking` blocks with only an encrypted `signature`; the real
  reasoning tokens are resident (re-billed as cached input every turn) but
  invisible to any transcript walk. Measured per turn as
  `hid(t) = max(0, out(t) − est(visible assistant text + thinking +
  tool_use inputs that turn))` and allocated as one synthetic segment
  (`uuid "reasoning-t<N>"`, born t). Compaction evicts them naturally (their
  synthetic uuids never appear in preservedMessages). `peek` on one answers
  found:true, kind `"reasoning"`, with an explainer. Without this category
  the reasoning mass masquerades as overhead (measured 53% of R on a real
  Fable session).
- **Server context rebuild** — R can FALL >10k between turns with NO
  compact_boundary (observed: after long away gaps the server rebuilds the
  context — cr collapses to an old prefix, R drops, hidden reasoning is
  flushed). Detection: `R(t) < R(t−1) − 10_000` with no intervening
  compaction ⇒ evict all `reasoning` segments, re-base `overhead₀`
  (`max(0, R − Σest)` at that turn), rebuild the map (rev+1), and emit
  `event{kind:"rebuild", severity:warn}`. Without this the rubber-band is
  silently absorbed into overhead.
- **Estimator** — per-record token estimate: `ceil(chars / 3.8)` on the JSON-decoded
  text; image blocks 1200 tok flat; `chars_per_tok` settable via Control `set`.
- **Overhead & calibration (the honesty rule)** — system prompt / tool schemas /
  skills are resident but invisible in the transcript. On each turn compute
  `Σest` = sum of live-record estimates.
  - `overhead₀` is measured at the FIRST turn: `max(0, R₀ − Σest₀)` and re-measured
    (re-based) at each compaction boundary the same way.
  - If `overhead₀ + Σest ≤ R`: `alpha = 1.0`, overhead segment = `R − Σest`.
  - Else: `alpha = (R − overhead₀) / Σest` (clamped to (0,1]), overhead = `overhead₀`.
  - The MAP is laid out as: `[overhead][records in prompt order, est·alpha each]`,
    summing to exactly R. `alpha` ships on every `map` message and is displayed.
- **waste (per file)** — cumulative tokens ever loaded for the file − tokens of its
  latest live copy. The numeric cost of re-reads/re-writes.
- **hit (per turn)** — `cr / max(1, cr + cc + in)`.
- **cost_u (per turn)** — honest structural units, NO dollar table in v2:
  `cost_u = in·1.0 + cr·0.1 + cc_5m·1.25 + cc_1h·2.0 + out·5.0`, reported in
  kilo-units (`ku`, float, 1 decimal).
- **T_auto** — auto-compact threshold fraction, default `0.85`; refined to
  `max(seen, preTokens/B)` on every observed `trigger:"auto"` compaction. Ships in
  `meta`.
- **thrash** — `C_t < C_{t−1} − 1024` (prefix invalidation), or 3 consecutive turns
  with `cc/R > 0.2`.
- **health** — `busy | idle | stalled | dead | offline`. From
  `~/.claude/sessions/<pid>.json` roster (pid `kill -0`-verified) ⊕ transcript
  staleness (roster busy + no transcript growth for 120 s ⇒ `stalled`; pid dead ⇒
  `dead`; no roster entry ⇒ `offline` = not-running session viewed post-hoc).

### Transcript parsing rules (version-drift law, from the schema survey)

Never require `summary`/`slug`/`session_id` records or fields; accept user
`message.content` as string OR array; `usage.cache_creation` nested object may be
absent (fall back to `cache_creation_input_tokens` as 5m+1h combined, split
unknown → attribute to `cc_5m`); tolerate every unknown record type / attachment
subtype / tool name. Token sources per record:

- `assistant`: per content block — `text`→`assistant`, `thinking`→`thinking`,
  `tool_use`→ input est to the tool's bucket (`Read/Write/Edit/NotebookEdit` with a
  `file_path|path|notebook_path` → that file; `Bash`→`bash`; else `tool`). Remember
  `tool_use_id → (name, file)`.
- `user`: string content → `user`; `tool_result` blocks → matched via
  `tool_use_id`: Read→file(`r`), Write/Edit/NotebookEdit→file(`w`/`e`), Bash→`bash`,
  else `tool`. Prefer `toolUseResult.file.content` length when present (structured,
  exact); image results 1200 each. `text` blocks containing `<system-reminder>` →
  `attach`.
- `attachment`: → `attach`.
- `system`/`compact_boundary`: see compaction. Other system subtypes are events,
  not context (they cost ~0; do not allocate).
- The post-compaction `user` record with `isCompactSummary:true` → `summary`.
- Records from `isSidechain:true` files are never parsed into the main accounting.

**Compaction:** on `compact_boundary`, `preservedMessages.allUuids` (fallback:
`preservedSegment` head..tail, else keep nothing) defines survivors; all other
prior records are evicted (files keep stats, marked `resident:false`, `✝`).
Aggregate evicted estimates into `dropped_cats` / `dropped_files`; cross-check
against `preTokens − postTokens` (mismatch → `log`). Re-base `overhead₀`. Rebuild
and re-emit `map` (rev+1), emit `compaction` + `event{kind:compaction}`.

**Agents (subagents):** discovered from `<transcript-dir>/<session-id>/subagents/`
(`agent-*.jsonl` + `.meta.json`, workflow children under `workflows/wf_*/`), and
from `Agent`-tool `toolUseResult` in the parent (async launch + completion with
`totalTokens`, `toolStats`). Running agents are offset-tailed for their own last
usage (own-window resident). `ret_tok` = estimate of the tool_result content
injected into the parent. `amp = own_tok / max(1, ret_tok)`.

---

## (b) Update messages (Engine → UI)

Weight rules: no file contents ever cross the wire; paths sent once and interned
(`files` upserts introduce integer `id`; every other message references files by
id); `map` ≤ 1024 segs (merge smallest adjacent same-cat/same-file first);
per-event messages < 200 bytes typical. The engine MUST emit a fresh coalesced
`map` (rev+1) BEFORE the UI-side ring (1024+overhead) would overflow — i.e.
whenever `base_segs + map_add segs since the last rebuild ≥ 1024` — not only at
compaction; the UI's cap is a safety net, never the steady state. All `ts`
strings on the wire ("HH:MM:SS") are UTC, including engine-synthesized events.

| type | fields | when |
|---|---|---|
| `init` | `engine_version:str, sessions:[Sess], default_session:str\|null` | once, first line |
| `meta` | `session_id, name, path, project, title?, model, budget:int, t_auto:float, cc_version?, started_at?` | on attach; re-emit on change. `name` = the distinct readable session handle: the live roster name (a custom name you set like `allboutRAG`, or derived `project-hash`) when running, else a stable memorable `adjective-noun` from the uuid — so amtr sessions never blur together |
| `backfill` | `turns:[Turn]≤512 (newest), faccess:[Faccess]≤4096 (newest), compactions:[Compaction] (all), agents:[Agent] (all), events:[Event]≤256, cmds:[Cmd]≤256 (newest), rets:[Ret]≤256 (newest)` | once per attach, after `meta`+`map`, before `ready` |
| `ready` | `session_id, turns:int, resident:int, budget:int` | backfill complete; UI un-gates views |
| `turn` | `turn:int, ts:str("HH:MM:SS"), model:str, in:int, cr:int, cc:int, cc_5m:int, cc_1h:int, out:int, resident:int, waterline:int, dur_ms:int\|null, stop:str\|null, tools:int, cost_u:float, hit:float` | one per new assistant turn |
| `map` | `rev:int, alpha:float, segs:[Seg]` | on attach + every rebuild (compaction, seek-base change); `segs[0]` is the overhead segment (`cat:"overhead"`) |
| `map_add` | `rev:int, segs:[Seg]` | appended segments between rebuilds (same rev) |
| `files` | `upserts:[File]` | change-detected batches, ≤ 1/tick |
| `faccess` | `turn:int, ts:str, file:int, op:"r"\|"w"\|"e", tok:int` | one per file access |
| `cats` | `totals:{cat:int}` | change-detected |
| `compaction` | `n:int, turn:int, ts, trigger:"auto"\|"manual", pre:int, post:int, dropped:int, cum_dropped:int, dur_ms:int, dropped_cats:{cat:int}, dropped_files:[{file:int,tok:int}]≤16, preserved_msgs:int` | per compact_boundary |
| `agent` | `id:str, state:"running"\|"done"\|"failed", agent_type?, desc?, wf?:str, path?:str, turn0:int, ts0:str, t0:float, ts_last:float, turn1?:int, own_tok:int, ret_tok?:int, tools?:{r:int,s:int,b:int,e:int}, dur_ms?:int` | upsert per lifecycle change (also on own_tok growth); `t0`/`ts_last` = epoch UTC of launch / newest own-transcript activity, 0 = unknown; `path` = the agent's own transcript (drill-in target), absent until dir-scanned |
| `cmd` | `turn:int, ts:str, epoch:float, cmd:str(≤240), desc:str\|null, out:str(≤600), err:str(≤300), ok:bool, interrupted:bool, bg:bool, tok_out:int` | one per completed Bash execution (the SHELL console feed). `cmd` is the command's head, `out`/`err` are TAILS of stdout/stderr with control characters stripped (ESC sequences, \r) — truncation is engine-side, marked with a leading `…` when cut; `tok_out` = estimated tokens of the FULL result as charged to context (ties the console to context cost); `bg` = backgrounded. Never anything but Bash — file tools stay in FILES |
| `ret` | `turn:int, ts:str, epoch:float, kind:"search"\|"fetch"\|"toolsearch"\|"mcp", src:str, q:str(≤160), n:int\|null, bytes:int\|null, dur_ms:int\|null, tok:int, ok:bool` | one per completed EXTERNAL retrieval — the agentic-retrieval feed (SHELL's second perspective). WebSearch → kind search, src "web", q = query, n = searchCount; WebFetch → fetch, src = host, q = url, bytes/dur_ms from the result; ToolSearch → toolsearch, src "tools", n = matches; `mcp__<server>__<tool>` → mcp, src = server, q = tool + primary arg. `tok` = estimated tokens the result injected into context. File tools NEVER appear here (FILES owns file retrieval) |
| `tasks` | `total:int, done:int, in_progress:int, active:str\|null` | change-detected |
| `health` | `status:str, last_activity_ts:float, api_errors:int, retry_in_ms?:int, stalled:bool` | on change + every 5 s |
| `event` | `kind:str, severity:"info"\|"warn"\|"error", ts:str, turn:int, msg:str` | ledger feed; kinds: `compaction, api_error, model_fallback, model_switch, hook_block, queued_prompt, pressure, thrash, stall, agent_failed, attach, rebuild` |
| `fleet` | `sessions:[Sess]` | change-detected roster refresh (~2 s scan) |
| `snapshot` | `turn:int, resident:int, waterline:int, cc:int, map:Map, files:[File], cats:{cat:int}, agents:[Agent], tasks:Tasks` | reply to `seek` (latest-wins); `resident/waterline/cc` are that turn's values so replay renders (cache mode, MAP fill) never fall back to live numbers |
| `report_done` | `ok:bool, path:str, msg:str` | reply to `report`: the report was written to `path` (the UI flashes it in the footer) |
| `peek` | `seg:int, found:bool, cat:str, kind:str\|null, uuid:str\|null, born:int, est:int, tok:int, file:int\|null, excerpt:str(≤2000), truncated:bool` | reply to `peek`: the segment's underlying record content, sanitized (control chars stripped) and clipped — the ONE sanctioned exception to the no-content wire rule, bounded and on-demand. `kind` = the record type (user/assistant/attachment/…); the overhead segment answers `kind:"overhead"` with an explainer excerpt (system prompt + tool schemas + skills are server-side and unmeasurable per-item); a segment that no longer exists (evicted/unknown) answers `found:false` |
| `log` | `msg:str` | anything unexpected |

Sub-objects:

- `Sess = {id:str, path:str, pid:int|null, name:str|null, project:str, status:str,
  mtime:float, live:bool, resident:int|null, budget:int|null, last_prompt:str|null}`
- `Seg  = {id:int, cat:str, tok:int, file:int|null, born:int (turn), ts:float (epoch
  of last access)}`
- `File = {id:int, path:str, tok:int, reads:int, writes:int, edits:int, waste:int,
  last_ts:str, last_epoch:float, resident:bool}` — `last_epoch` = epoch seconds
  UTC of the newest access, 0 = unknown (drives the FILES NOW view's live decay;
  UI arrival-stamping is wrong post-backfill, so the engine owns it)
- `Turn`, `Faccess`, `Compaction`, `Agent`, `Tasks` = the payloads of the
  corresponding messages minus the `type` tag.

Ordering per attach: `meta` → `map` → `backfill` → `ready` → incremental flow.

## (c) Control messages (UI → Engine)

| type | fields | meaning |
|---|---|---|
| `attach` | `session:str` (id or path; agent transcript paths under `subagents/` are valid — the engine parses them as MAIN despite `isSidechain:true` records, titling the session from the agent's meta description) | detach current, backfill new, re-`ready` |
| `seek` | `turn:int` | **latest-wins**: engine coalesces (only newest pending seek is answered); reply is one `snapshot`; live tailing never pauses |
| `peek` | `seg:int` | on-demand content inspection (MAP INSPECT mode): the engine re-reads the segment's record from disk and replies with one `peek`. Explicit-request-only (sent on Enter, never per-cursor-move), so no coalescing is needed |
| `live` | — | leave replay; engine stops answering stale seeks |
| `report` | — | write a ground-truth report (§f) of the ATTACHED session — the live engine already has it parsed, so it is instant — to `~/.claude/amtr-reports/<name>-<id8>.md`; replies `report_done`. Bound to `R` in the TUI (one-key, seamless) |
| `set` | `key:str, value` | `chars_per_tok:float`, `poll_ms:int`, `t_auto:float`; unknown keys silently ignored |
| `fleet_refresh` | — | force roster rescan |
| `quit` | — | cooperative shutdown; stdin EOF is the equivalent fallback |

---

## (d) Engine (`amtr_engine.py`) — process discipline & internals

**Copied verbatim from the proven server:** fd-1 hijack FIRST (`_PROTO_FD =
os.dup(1); os.dup2(2,1); sys.stdout = sys.stderr; _PROTO = os.fdopen(_PROTO_FD,'w',
buffering=1)`); single `send(obj)` under one lock, `json.dumps` + `\n` + flush,
swallowing `(BrokenPipeError, ValueError)`; `log(msg)` for every error path; main
thread = blocking `for line in sys.stdin` (EOF ≡ quit); one `threading.Event`
`_quitting`; per-handler try/except; field validation before use; all emission
change-detected with `object()` sentinels reset on re-attach.

**Threads:** main (stdin dispatch + seek handling) · **tail** daemon (250 ms tick:
`os.stat` cheap-check → offset-read new bytes → parse → emit deltas; a transcript
that SHRANK below the read offset means rewrite/rotation — perform a full
re-attach (fresh Session, meta→files→map→backfill→ready), never re-feed into
populated state; 1 s sub-cadence for `subagents/` discovery+tails and
`~/.claude/tasks/<sid>/`) ·
**fleet** daemon (2 s: `~/.claude/sessions/*.json` + `kill -0` + newest-transcript
scan per project + `history.jsonl` tail for last-prompt join; also drives `health`).

**Session accounting object** holds: record ring (uuid → {cat, est, file, born,
last_ts, evicted}), file table, turn list, segment builder, compactions, agents,
overhead₀/alpha, budget. **Checkpoints:** deep-copied every 200 turns, kept ≤ 16
(thin to power-of-two spacing). `seek{turn}` clones nearest checkpoint ≤ turn,
replays records forward to that turn, emits `snapshot`. Replay never disturbs the
live tail.

**Session discovery:** `--session FILE` > `--project PATH` (newest `.jsonl` under
its slug) > newest session anywhere under `~/.claude/projects`, cross-checked
against the live roster (prefer live sessions). The engine must EXCLUDE its own
monitoring session when auto-picking iff `AMTR_SELF_SESSION` env is set to that id.

**CLI (engine standalone, for tests & debugging):** `--selftest` (replay a fixture
transcript at full speed, emit all messages to the protocol stream, exit 0 —
doubles as the UI's demo feed via `amtr --engine-args`); `--validate` (print v1's
authoritative-vs-estimate report to stderr and exit).

**Fixture:** `tests/fixtures/golden.jsonl` — SYNTHETIC transcript (no private
data) exercising: ≥6 turns with realistic usage (growing cr, cc spikes), Read/
Write/Edit/Bash tool_use+tool_result pairs (with `toolUseResult.file` shapes),
an attachment, a `<system-reminder>` text block, one `compact_boundary` (with
`preservedMessages.allUuids` + post `isCompactSummary` user record), one Agent
launch+completion (`totalTokens`, `toolStats`), one `api_error` system record, a
string-content user prompt and an array-content one. `tests/test_engine.py`
(stdlib `unittest`): replays the fixture through the Session object and asserts —
R after each turn, waterline, overhead/alpha math, file waste, eviction set after
compaction, emitted-message type sequence of `--selftest`, checkpoint/seek
equivalence (seek(t) state == linear replay to t).

## (e) UI (`rust/`) — architecture

Crate layout: `main.rs` (App, run loop, layout, keys, overlays, tests) ·
`ipc.rs` (spawn + reader/writer threads + serde enums — a self-contained split-process module
wholesale: parse-error→`Update::Log`, writer `recv_timeout(100ms)` + AtomicBool
stop, EOF→drop→`Disconnected`) · `state.rs` (mirrored session state: capped rings —
turns 512, faccess 4096, log 200, events 256, segs 1024+overhead) · `viz.rs` (pure
renderers: `(state, Rect, &mut Frame)`; every one guards degenerate rects).

**Spawn:** `amtr [--session S|--project P] [--engine PATH] [--python PATH]` →
`Command::new(python).arg(engine).args(passthrough)`, cwd = repo root, piped
stdin/stdout, inherited stderr. Defaults: `python3`, `amtr_engine.py` next to the
binary's repo (compile-time default overridable by `AMTR_ENGINE` env).

**Run loop (split-process pattern, normative):** block on `rx.recv_timeout(deadline)`
where `deadline = min(30ms idle, pulse clock 80ms while any pulse>0, heat clock
500ms while MAP heat mode active and max heat>0.05, blink 500ms)`; then
`drain_updates()` (coalesce all queued); zero-timeout input drain (modal-first
dispatch, `KeyEventKind::Press` only); wall-clock catch-up with `+=` rescheduling;
dirty-flag `terminal.draw`. `Disconnected` → 10 ms sleep + `engine dead` banner.
`ratatui::init()/restore()`; `q` → `Control::Quit` then restore-first shutdown
(send quit again, join handle).

### Global chrome (every tab)

Row 0 — **status ribbon**: `amtr │ <name> <title> <model> │ R 591k/1000k 59% ▮ │
+8.2k/t │ compact≈9t │ 412ku │ ag 2● │ tasks 3/7 │ ●busy`. Elision rule: the
title is truncated (`…`) to whatever width keeps every field to its right
intact; fields are never cut mid-value. The tabs line likewise shortens its
LEFT content (drop `(f)FLEET (?)help` hints, then compact tab labels) before
ever clipping the LIVE/REPLAY indicator. Zone-colored block:
green R/B<0.60, amber <0.85, red ≥0.85 (fixed). Fill rate = EMA(ΔR per turn,
k=8). ETA = `(T_auto·B − R)/max(1,slope)` turns, slope = least-squares over last
16 turns (UI-computed from its turn ring).
Row 1 — **tabs line**: `[1]OVERVIEW [2]FILES [3]TURNS [4]AGENTS [5]EVENTS
(f)FLEET (?)help`, active tab inverse; right side `● LIVE` (green) or
`« REPLAY t=N/M` (amber). Glyph discipline: width-1 glyphs ONLY throughout the
UI (blocks, eighths, shade, braille, `▼◆▲✝«»·`); no emoji, no wide glyphs.
Row 2 — **timeline scrubber** (the whole session in one row): column c covers
turn bucket `[c·M/(W), (c+1)·M/W)`; glyph = shade ramp `·░▒▓█` of max resident/B
in the bucket; overlay markers win over shade: `◆` compaction (magenta),
`▲` thrash (red); playhead `┃` inverse (green live / amber replay). Keys ←/→ move
the cursor; the strip is the cursor's spatial home.
Last row — **alert/footer ribbon**: newest unacked alert (severity-colored), else
contextual key hints. Alerts: `PRESSURE_AMBER/RED, COMPACT_ETA≤3t, CACHE_THRASH,
STALLED, API_ERROR, MODEL_FALLBACK, AGENT_FAILED` (from `event` messages with
severity ≥ warn).

**Size tiers** (pure `layout(area) -> Panes{Option<Rect>…}`, pane-dropping, never
squishing): ≥110×30 full · ≥80×24 drop secondary columns, coarser MAP rung ·
≥50×15 one primary pane per tab, 1-line ribbon, no scrubber · <50×15 big-number
mode (R%, zone color, ETA, alert count) · <14×6 centered `amtr ≥14×6`.

### Tab 1 OVERVIEW — MAP + EKG

**Context GAUGE** (OVERVIEW headline, 2 rows): a big glanceable bar that warms
green → amber → red as the window fills (the zones you cross toward
compaction), with `CONTEXT R/B  N%`, compaction ETA and fill-rate inline. This
carries "how full", so the MAP below is free to be pure content.

**MAP** (fills the body between gauge and the compact EKG): row-major memory
map, half-block cells (2 logical rows per char row). On OVERVIEW it PACKS to
resident content — cell size chosen so R fills the pane edge-to-edge, no
free-space dot field, no blank gaps (the gauge shows headroom instead). Cell
size is therefore content-relative on OVERVIEW (it "breathes" as R grows);
within any frame area ∝ tokens still holds. The rung ladder (budget-relative,
fixed-scale, headroom-as-dots) is retained for any non-packed use. Cell size S from fixed ladder `{128,256,512,1k,2k,4k,8k,16k}` tok:
smallest rung where `B/S` fits the pane; rung labeled in gutter (`▪=2k`) —
large panes buy RESOLUTION, never blank space (the ladder floor is what lets
a tall terminal render a 1M budget at 128 tok/cell instead of leaving the
EKG a void). Cells
beyond R render dim `·` (free space visible). Cell owner = plurality segment.
**Color modes (`m` cycles, gutter-labeled):**
1. `class` (default) — fixed palette: overhead slate (110,120,140) · user blue
   (90,140,220) · assistant green (110,190,110) · thinking dim-green (80,130,90) ·
   bash orange (230,140,80) · tool gray (150,150,150) · attach purple (160,110,
   220) · summary white (235,235,235) · file → the file's accent hue: fixed 8-hue
   wheel `[(230,90,90),(90,200,200),(230,200,80),(200,110,230),(120,220,120),
   (240,150,60),(100,150,240),(220,120,170)]` assigned at first access, cycled.
2. `heat` — uniform orange, brightness `0.30 + 0.70·e^(−Δt/45s)` from seg `ts`;
   500 ms animation clock while any heat > 0.05; else static.
3. `age` — shade ramp `·░▒▓█` by log₄ buckets of `turns_now − born` ∈
   [0,1,4,16,64+), newest brightest.
4. `cache` — `[0,C)` steel (70,110,160), `[C, C+cc)` cyan (80,200,220), rest
   amber (230,170,60): the exact billing tri-coloring of last turn.
**Waterline marker** (all modes): bright cyan cell at address C; on thrash the
re-created span flashes red (6-frame pulse, 80 ms cadence). New segments pulse
white at the tail (write head). On compaction: 3-frame dim sweep, then re-layout.
Selected file (FILES tab selection) renders inverse in every mode.
**Legend row** under the map: `class:tokens` pairs in class colors (from `cats`).
**INSPECT mode (`i` on OVERVIEW)** — walk the prompt's segments like a memory
debugger: `←/→` (and `j/k`) move a segment cursor in prompt order, the selected
segment ANIMATES on the 500 ms blink clock as a BREATHING spotlight: white
blaze ↔ deep dim (`scale(color, 0.40)`). Never reverse-video — a fg/bg swap
is visually nil on a solid single-color chunk, which most walked segments
are (field-verified). The legend row is replaced by the
segment's identity
(`#id cat · file path? · born t · est N ×α = M tok`); `Enter` on a FILE-backed
chunk opens it in `$EDITOR` (suspend/resume), on any other chunk it requests
`peek`; `p` requests `peek` unconditionally (the in-context copy can differ
from disk). The peek overlay shows the record's actual text (excerpt, wrapped;
`…` marks the clip; `found:false` renders "evicted — no longer in the
transcript window"). Esc closes the overlay, then exits INSPECT; the turn
cursor keys are captured by INSPECT while it is active (`←/→` walk segments,
not turns). Works in replay (the snapshot's segs carry the same ids).

**EKG** (bottom): braille Canvas, x = last 512 turns, y = [0,B] FIXED. Traces:
resident R_t (zone-colored bright line), waterline C_t (dim cyan), dotted
least-squares projection (last 16 turns) to the `T_auto·B` horizontal rule, zone
rules at 0.60/0.85. `▼` printed above compaction cliffs. Turn-cursor as vertical
line. Two sparkline lanes under it: out/turn (fixed 0–16k), cost_u/turn (fixed
0–100ku).

### Tab 2 FILES — two perspectives: HISTORY (default) and NOW (`v` toggles)

**HISTORY = roll + table.** Roll (WEAVE idiom): rows = files (current sort
order, scrollable), x = turns (shared cursor); cell `▀`=read `▄`=write/edit
`█`=both, blank untouched; intensity by fixed access-size ramp (<1k dim, 1–4k
normal, 4–16k bright, >16k bold). Newest column pulses. Gutter: 22-cell
right-truncated path in the file's accent hue; `✝` prefix if evicted.
Table: `tok(est) %res rd/wr/ed waste last spark path` (`ed` = Edit-tool
patches, NOT execute — executions live in SHELL); spark = 8-slot access-size
history (`▁▂▃▄▅▆▇█`, fixed 0–16k). Sorts (`s`): size/recent/churn(waste)/name.
Selection ↔ roll row ↔ MAP highlight. `o` opens in `$EDITOR` (suspend/resume).
`Enter` = detail line (full path, alpha-calibrated vs raw est, access counts).

**NOW = live file activity, no turn history** (btop-process-list perspective).
- Hot ⟺ `now − last_epoch < 118.8 s` (the exact age where MAP heat goes static:
  `0.70·e^(−dt/45) ≤ 0.05` — one shared law). Order: `last_epoch` desc; files
  with `last_epoch == 0` sink to the cold tail. Evicted files stay (with `✝`)
  only while still hot, then drop from the view entirely.
- Row (Full tier): `AGE op LAST TOKBAR TOK PATH` — AGE = `now − last_epoch`
  ticking live (`2s/47s/7m`, `—` unknown); op = newest faccess op (`r` cyan,
  `w` orange, `e` yellow, `·` if the ring lost it); LAST = that access's tok;
  TOKBAR = resident est on a FIXED 0–64k eighth-block bar (≥64k full+bold);
  PATH two-tone (dir prefix dim, basename in the file's accent hue), tail-
  truncated only on overflow. Hot/cold separated by a dim `── cold ──` divider;
  header shows `hot N · cold M` + `v history` hint.
- **Decay animation**: every row's fg brightness = `0.30 + 0.70·e^(−age/45s)`
  on the 500 ms heat clock (static when nothing is hot). Entry: a live faccess
  (never backfill) pulses the row's AGE/op cells white for 6 frames (80 ms
  clock) as the file jumps to row 1.
- Tiers: Medium drops TOKBAR+TOK; Compact = hot zone only, header carries
  `+M cold`. Replay: NOW is live-only — render a centered dim
  `NOW is live-only — End/Esc → LIVE · v → history` notice, no rows.
- Keys: `v` toggles (default HISTORY, reset on re-attach); `j/k g/G Enter o`
  unchanged over the NOW order; `s` is a no-op in NOW. MAP selection
  cross-link follows whichever view is active.

### Tab 3 TURNS — cache & cost ledger ("the ledger becomes the chart")

Per-turn stacked columns, 1 cell wide, half-block vertical resolution, y FIXED
0–B (same scale as EKG), cumulative rounding; five fused channels, zero new
panes/keys/wire fields:
1. **Stack bands** bottom-up: `cr` steel + `cc_5m` cyan + `cc_1h` purple
   (160,110,220) + `in` red; stack top = R_t by identity, drift-proof: the
   purple/cyan boundary uses `cc_5m` but the `in` edge is computed from
   `cr + cc` (an old engine sending only `cc` renders all-cyan, total
   unchanged). Min-1-row rule for nonzero `in` kept.
2. **Marker rail** (chart row 0, shares the B gutter label): per turn column
   `▲` thrash (red, bold while pulsing) > `▼` compaction (magenta) > `◆` model
   switch (white).
3. **Prev-waterline tick**: one bright half-cell (the MAP waterline cyan
   (150,240,255)) at `C_{t−1}` inside each column — below the steel top =
   promotion into cache, floating above it = invalidation depth. Steel top
   itself ≡ C_t.
4. **Lane recoloring** (glyphs/scales unchanged: out 0–16k, dur 0–120 s, ku
   0–100ku): out green, red iff `stop=="max_tokens"`, amber for other abnormal
   stops; dur blue with brightness by tools count (0 dim → ≥6 white); ku amber
   iff `hit≥0.90`, orange 0.50–0.90, red < 0.50 (expensive-because-missed
   screams).
5. **Ledger line 2** appends `fa {r}r/{w}w[/{e}e]` for the cursor turn and
   `◆ prev→new` on model-switch turns; the 5m/1h sub-numbers take their band
   colors. New-turn columns pulse white-blended for 6 frames (80 ms clock;
   new indices only, never upserts/backfill).
Footer legend (colored swatches): ` █cr █5m █1h █in ▀wl ▼cmp ▲thr ◆mdl`.
Lanes degrade: 3 iff body ≥12 rows, 2 (drop dur) iff ≥8, else 0; rail iff
chart ≥4 rows.
```
turn 214  16:48:51   in 2 │ cr 512.3k │ cc 17.9k (5m 17.9k·1h 0) │ out 203
hit 96.6%  cost 21.4ku  dur 8.4s  stop tool_use  tools 3  fa 2r/1w
```

### Tab 4 AGENTS — load strip + unified ledger

At 60+ agents per session, per-agent gantt bars are the wrong projection;
the temporal truth is CONCURRENCY.
- **Header**: `fan-out 312k ≡ 0.31× main · 3● 54○ 3✖ · sort:<mode> ·
  filter:<mode>`.
- **LOAD strip** (2 rows, Full/Medium): x = shared turn axis, y = agents alive
  per turn on a FIXED 0–8 half-block scale (`alive(t) = |{a: turn0≤t≤end}|`,
  end = turn1 else last-if-running else turn0). Steel history; cyan when any
  agent alive there is currently running (blink-dimmed); alive>8 → top
  half-cell white (overload cap); a failed agent's end turn → top half-cell
  red (notch wins). Turn-cursor column REVERSED. Gutter `8+`/`0`.
- **Unified ledger** (ONE list — gantt and table may never desync): row =
  `glyph TOKBAR own-tok ret-tok amp tools(r/s/b/e) dur label`. Glyph: `●` cyan
  running (blink) · `○` green done · `✖` red failed · `▸/▾` wf rollup. TOKBAR:
  own_tok on a FIXED log scale `(log10(tok)−3)/3` (1k edge → 1M full),
  eighth-block, colored by state; brightness for running rows = heat law
  `0.30+0.70·e^(−(now−ts_last)/45s)` — producing glows, wedged fades; bar tip
  pulses white 6 frames on own_tok growth. `dur` = dur_ms when finished, live
  `now − t0` ticking while running (`—` when t0 unknown). Workflow rollup row
  `wf_<id> ×N k● j✖` with child sums; auto-expanded while any child runs or
  failed, auto-collapsed when all done; `Enter` sets a manual override.
  Ordering (`s`): recent (default: running first) → tok → launch. Filter
  (`a`): all → run → fail. **`Enter` on an agent row DRILLS INTO the agent**:
  the UI pushes the current session onto an attach breadcrumb stack and sends
  `attach` with the agent's own transcript path — the FULL instrument (MAP,
  TURNS, FILES, SHELL…) re-targets to the agent's own context window.
  `Backspace` (global) pops the stack and re-attaches the parent; the fleet
  picker clears the stack. Agents without a known `path` (never dir-scanned)
  don't drill; wf rollup rows keep `Enter` = expand/collapse. Selection with
  `j/k g/G`, kept visible. Detail line (Full, ≥12 rows): id · wf · desc ·
  t0→t1 · ts.
- Tiers: Medium drops detail + tools column, bar 12→8 cells; Compact drops
  strip/header-row/detail/footer — header + list only.
Footer: `tasks 3/7 ▸ "<active>"`.

### Tab 5 EVENTS — ledger + compaction post-mortem

Scrollable ledger (newest first): `▼ compaction · ✖ api_error (retry 3/10 in 8s) ·
⇄ model_fallback · ⚑ hook_block · ◇ queued_prompt · ⚠ pressure/thrash/stall`,
severity-colored. `Enter` on a compaction → post-mortem overlay (Clear + centered):
pre/post bars on one fixed 0–B scale, dropped-by-category (eighth-block bars),
top dropped files with `✝`, preserved count, `←/→` prev/next compaction, Esc.
`j` on any event jumps the turn cursor to it.

### Tab 6 SHELL — the command console

The Bash feed as the terminal Claude never shows you, priced in context tokens.
- **Newest at BOTTOM**, tail-pinned follow (`shell_follow`, default true, no
  selection bar while following). `j/k/g` break follow and anchor selection by
  SEQ IDENTITY (never index); `G` and `End` (live) restore follow; global
  `go_live` also restores it.
- **Entry**: `ts ○/✖/^ $ cmd [— desc] spark tok_out`, then output tail lines
  (stdout dim ×0.72, indent; stderr with a red `▎` gutter). Collapsed: 2 output
  lines Full / 1 Medium (stderr preferred) / 0 Compact. `Enter` expands the
  selection (full tails, `# desc` comment line, `^C` mark on interrupted);
  Enter while following = verbose follow. Marks: `○` ok green · `✖` failed
  red · `^` interrupted amber (wins) · ` &` bg cyan suffix. tok_out spark +
  number on the FIXED 0–16k ramp with the FILES size-color law (≥16k red
  bold — a command that dumps 16k tokens into context screams).
- Header: `$ N cmds · ✖E · ^I · &B · filter all|err`; right posture:
  `● FOLLOW` (blink) / `↑ +N newer` / `« console @ t=N`.
- **No heat-dimming** (transcripts must stay readable); arrival = 6-frame
  white pulse on the prompt line (80 ms clock, live only, never backfill).
- **Replay filters to `turn ≤ cursor`** UI-side (the ring is turn-tagged) —
  scrubbing visibly rewinds the console; pulses inherently invisible.
- Keys (contextual): `j/k` browse · `g/G` ends (G restores follow) · `Enter`
  expand · `a` filter all⇄err · **`v` toggles CONSOLE ↔ RETRIEVAL** (the
  FILES-view idiom). Tabs line gains `[6]SHELL` under the existing elision
  ladder; `1–6` selects tabs. UI ring cap 256 (= backfill cap); absent `ok`
  on the wire defaults TRUE (version-drift law).

**RETRIEVAL perspective** (`v` inside SHELL) — the agentic-retrieval feed:
every EXTERNAL pull the session's own retriever made, newest at bottom, the
console's follow/browse machinery (`shell_follow` shared; separate seq-anchored
selection). Row: `ts glyph src q → n · dur tok` — glyphs `⌕` search (cyan) ·
`⇣` fetch (blue) · `◆` mcp (per-server accent from the file-hue wheel keyed by
server name) · `#` toolsearch (dim); failed pulls `✖` red. `tok` on the same
fixed 0–16k ramp/color law as the console (a 16k-token retrieval screams).
Header: `⌕ N pulls · web X tok · mcp Y tok · tools Z tok · filter all|err` +
the shared FOLLOW/browse/replay posture. `Enter` expands: full query/url,
bytes, duration, result count. Replay filters `turn ≤ cursor` (same law).
Empty state: `no retrievals yet — web/MCP pulls will stream here`. Tier
degradation mirrors the console (Medium drops ts, Compact drops header).

### FLEET (`f` or `0`) — session picker overlay

Table: live roster first (●busy ◐stalled ○idle), then recent by mtime (✖dead /
offline). Columns: status glyph, name, project tail, resident 8-cell mini-bar
(0–B when known), age, last prompt (truncated). `Enter` attaches. `r` refresh.

### Help (`?`) — three pages, each fits 80×24

`?` opens; `?`/`→`/`j` page forward, `←`/`k` back; any other key closes.
1. **keys + legend** — full keymap, palette swatches, zone thresholds,
   waterline/thrash explanation, glyph dictionary (`▀▄█ ▼ ✝ ◆ ▲ «`, shell).
2. **the numbers** — glossary of every quantity on screen: R, hot/cold
   (recency of last touch, ~2 min window), AGE/O/LAST, spark, %res, waste
   (cumulative loaded − live copy), ku (the cost-unit formula), out/t ku/t,
   ag/amp, post-mortem.
3. **modes & anatomy** — the four MAP measures (class/heat/age/cache) and the
   TURNS column anatomy (cr/5m/1h/in bands, ▀wl tick, ▼▲◆ rail, lane color
   rules).

### Keybindings (dispatch: overlay > tab-contextual > global)

Global: `q` quit · `?` help · `1–5` tabs · `f`/`0` fleet · `p` pause render ·
`←/→` cursor ±1 · `Shift+←/→` ±10 · `Home` first · `End`/`Esc` LIVE · `m` MAP
mode · `c` latest post-mortem · `R` write report · `+/-` MAP rung override.
Contextual: `j/k` select (FILES rows, AGENTS rows, EVENTS entries) · `g/G`
ends · `Enter` drill (FILES detail · AGENTS wf-expand/agent-jump · EVENTS
post-mortem/jump) · `s` sort (FILES history view, AGENTS) · `v` FILES
history↔now · `a` AGENTS state filter · `o` `$EDITOR` · `r` (fleet) refresh.
Replay: cursor off tail sends `seek{turn}` (UI ALSO coalesces: at most one
in-flight seek, newest wins); `snapshot` re-renders MAP/FILES/cats/AGENTS/tasks
at that turn using the snapshot's own `resident/waterline/cc` (never live
values); EKG/roll/TURNS keep full traces + cursor line; live buffers keep
filling; `End` snaps back with no re-fetch. Because the engine deliberately
never answers a cancelled seek, `live`/`End` MUST clear the UI's in-flight
latch as well as its pending seek (liveness rule).

### Tests (day one, `#[cfg(test)]` in main.rs/viz.rs)

`demo_app()` fixture (dummy channel ends + hand-set state incl. a MAP with all
cats, 40 turns, 6 files, 1 compaction, 2 agents, 3 events). (1) TestBackend
semantic string assertions per tab + each overlay; (2) no-panic size sweep —
every tab × every overlay × sizes from 1×1 through 200×60; (3) MAP fixed-scale
invariance (gutter bytes identical across different data); (4) scrubber marker
placement; (5) `shots -- --nocapture` full-screen dump harness at ~12 sizes.

---

## (f) REPORT mode — the session as a ground-truth report

`python3 amtr_engine.py --report [--session X|--project P] [--json] [--watch
[--idle-secs N]]` — no TUI, no protocol stream: parse the transcript (the
recording Claude Code already made) and print a report to stdout, exit 0.
Headless runs (`claude -p`) write the same transcripts; `--watch` tails a LIVE
session and emits the report when the run ENDS (roster pid gone, or transcript
quiet ≥ idle-secs [default 60] with roster not busy) — start it beside a
headless process and collect the account at the end.

The report is markdown, ground truth first, sections in this order:
1. **HEADER** — title/session id, project, model(s) (+ switches), cc version,
   wall span (first→last ts, duration), turns, entrypoint.
2. **CONTEXT (authoritative)** — final R vs budget, PEAK R (max over turns),
   compactions (count, tokens dropped, per-event pre→post), server rebuilds
   (count, flushed), final composition by category (incl. overhead/reasoning
   split, α), waterline at end.
3. **ECONOMICS (authoritative)** — Σ input / cache-read / cc_5m / cc_1h /
   output tokens across ALL turns; overall hit rate; total cost_u; cost/turn
   mean & p95; thrash events; per-model breakdown when models switched.
4. **FILES (estimated)** — top 15 by live tokens: tok/%R/rd/wr/ed/waste/path;
   totals row; total waste; evicted count.
5. **SHELL** — commands run, ok/failed/interrupted/bg counts, total tok_out,
   the failures listed verbatim (cmd head + err tail), top 5 by tok_out.
6. **RETRIEVAL** — pulls by kind and by src with token totals; failures.
7. **AGENTS** — counts by state, fan-out Σown_tok, ≡×main ratio, Σret_tok,
   median amp; top 5 by own_tok (type · desc · own/ret/amp/dur).
8. **EVENTS** — the ledger verbatim (ts · kind · msg), errors first.
9. **TIMELINE** — R per turn as a text sparkline (▁▂▃▄▅▆▇█) scaled to the
   session's OWN peak R (a report is about THIS run; budget-scaling floors
   every headless run to a flat ▁ row), labeled with that peak and the budget,
   `▼` compaction / `≈` rebuild markers beneath (omitted when none), plus one
   line per notable event with its turn.
10. **DIAGNOSTICS** — verdict bullets computed from the data: waste hot-spots
    (files with waste > 25% of their traffic), truncation stops
    (`max_tokens`), sub-50% hit turns, >16k-token single retrievals/commands,
    failed agents, unanswered pressure (R ended in the red zone).
`--json` emits the same content as one JSON object (sections as keys, tables
as arrays) for automation. Authoritative vs estimated labeling is preserved
in both formats. All numbers must reconcile with the live instrument's
(same Session accounting — the report is a RENDERING, never a re-derivation).

## (g) Demo / visual-testbench mode

`amtr --demo` runs a deterministic, fully-populated session (all categories,
40 turns, a compaction, a 12-child workflow, retrievals, a shell log) with NO
engine and the animation clocks LIVE (timestamps anchored to real `now`) — the
reproducible scene source for visual/animation validation and a zero-setup
tour of the instrument. It answers its own `peek` requests locally (no engine)
so INSPECT is fully exercised offline. The INSPECT spotlight FREEZES to a
steady blaze while a peek overlay is open (`Ui.spotlight_static`) — the reader
is on the text, a breathing map behind it is noise. Scenes are enumerated in
`tests/scenes.json` and driven by the `tui-visual-validation` skill (tmux
`capture-pane -e` is the source of truth, never a reconstruction).

## (h) Non-goals (v2.0)

Dollar cost tables (units only) · pixel/Kitty graphics (cell renderers only;
the pane structure must not preclude adding a gfx layer later) · mouse ·
multi-session simultaneous attach (fleet is a picker, one attach at a time) ·
editing anything (read-only instrument; `o` opens `$EDITOR` and that is all).
