# anthropometer &nbsp;·&nbsp; `amtr`

**A btop-style, real-time diagnostic instrument for Claude Code sessions.**

`amtr` attaches to a Claude Code session's transcript and renders — live — exactly
what is happening inside the model's context window: how the token budget is being
spent, which files and tools are resident, when compactions fire, what subagents
are doing, and the true cost of every turn. Press **`R`** and it compiles a
ground-truth PDF report of the whole session.

![amtr — the context window filling up over a session](docs/assets/context-fill.gif)

<sub>↑ replaying a session turn-by-turn: the context map fills, the composition shifts, the trend climbs.</sub>

> **▶ [The autopsy](docs/autopsy/):** amtr was built in one 153-hour Claude Code conversation —
> so we pointed it at its own transcripts. 1,945 turns, 1.02 billion cache-read tokens,
> 3 compactions, $1,472 at API list price. The instrument dissects its own birth.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
&nbsp;·&nbsp; [![vibe coded](https://img.shields.io/badge/AI%20usage-vibe%20coded-ff2d78)](#-ai-usage)
&nbsp;·&nbsp; Rust + ratatui TUI · Python engine · terminal-only

---

## AI usage

![the vibe meter — needle pegged](docs/assets/vibemeter.png)

---

## Why

`/context` gives you one number. `amtr` gives you the whole picture, continuously:
the context window as a **live memory map**, file access as a **traffic seismograph**,
cache economics as a **per-turn ledger**, compactions as **forensic events**, and
subagents as an **economics table** — every quantity labeled *authoritative*
(read straight from the API usage records) or *estimated*, never blurred together.

It works on any Claude Code session — interactive or headless (`claude -p`) — because
every session already writes a complete transcript. `amtr` just reads it in real time.

---

## The live TUI

A fast, keyboard-driven terminal UI. Tabs `1`–`6`, `f` for the session picker
(`tab` there for the system-wide wall), `i` to inspect, `R` to build a report,
`?` for help, `q` to quit.

### Context map — where your budget actually goes

![context map, composition legend, and resident trend](docs/assets/tui-overview.png)

The resident context as a **fixed-scale grid**: the whole box *is* the budget, and
every cell is colored by what occupies it — system overhead, file content, hidden
reasoning, shell output, tool results, and more. At a glance you see how full you
are and what's filling you up. `m` cycles four lenses (category · access-heat ·
turn-age · cache billing); `i` walks the segments like a memory debugger and reads
back the **actual text** occupying any region.

### Block themes — colorblind-verified

![t cycles the block themes: terminal, ukiyo-e, bauhaus, mono](docs/assets/themes.gif)

`t` cycles four identity palettes for the map (`--theme` pins one): **terminal**
(gruvbox-flavored default) · **ukiyo-e** · **bauhaus** · **mono** (luminance-only).
Every theme is accessibility-gated in the test suite: the build fails unless all
category/file color pairs stay distinguishable under normal vision, protanopia,
and deuteranopia (simulated with Machado 2009 matrices, CIE76 floors).

### Files & subagents

| Live file traffic (`2`) | Subagent fan-out (`4`) |
|---|---|
| ![files](docs/assets/tui-files.png) | ![agents](docs/assets/tui-agents.png) |

**FILES** shows every file's read/write/edit history and a "now" view of what's
being touched this instant (fading on a heat law), with a **waste** column that
prices re-reads. **AGENTS** is a concurrency load-strip over a ledger of each
subagent's own-tokens, return-tokens, amplification, and live duration.

### Session picker — find any session by name or path

Press **`f`** for a searchable, scrollable list of *every* session on your machine
(live ones first). Type to filter by name or project; paste a `.jsonl` path or a
session id to jump straight to it.

![sessions](docs/assets/tui-sessions.png)

### SYSTEM-WIDE — every session on your machine, one wall

Press **`f` then `tab`** for the wall: every *active* session as a live gradient
tank (each session keeps its own deterministic palette), all draining in real
time. On any tile: **`space`** quicklooks the session's actual conversation —
who's working on what, read straight from its transcript — **`⏎`** attaches,
and **`x`** ends the session (confirmed, then a polite SIGTERM).

![system-wide wall](docs/assets/tui-wall.png)

![quicklook preview](docs/assets/tui-wall-preview.png)

Other tabs: **TURNS** (per-turn stacked cache/input columns with the 5m/1h billing
split), **SHELL** (the command console Claude never shows you + the external-retrieval
feed), and **EVENTS** (compactions, API errors, model fallbacks — with a compaction
post-mortem on `Enter`). A timeline scrubber holds the whole session; `←/→` rewinds
every view to any past turn.

---

## The report — press `R`

`amtr` turns a session's transcript into a compiled, ground-truth **PDF report**:
a self-contained directory with `report.pdf`, animated GIFs + static figures, and a
per-turn capture (`turns.jsonl` / `turns.md`). Everything is rendered locally.

![report page](docs/assets/report-page1.png)

The figures reconstruct the session faithfully:

| Context map | File traffic roll |
|---|---|
| ![map](docs/assets/fig-context-map.png) | ![files](docs/assets/fig-files-roll.png) |

| Subagent branch tree | Agent fan-out timeline |
|---|---|
| ![tree](docs/assets/fig-branch-tree.png) | ![timeline](docs/assets/fig-agents-timeline.png) |

![ekg](docs/assets/fig-ekg.png)

Plus a cost-ranked phase table and a stage-by-stage, turn-by-turn account of what
the session actually did.

---

## Install

### Quick install (prebuilt binary)

No Rust, no Homebrew — one line downloads the binary + engine for your platform
(macOS arm64/x86_64, Linux x86_64/arm64) and installs it under `~/.local`:

```sh
curl -fsSL https://raw.githubusercontent.com/arian-shamaei/anthropometer/main/install.sh | sh
```

If `~/.local/bin` isn't on your `PATH`, the installer tells you how to add it.
Override the install prefix with `AMTR_PREFIX` or pin a version with `AMTR_VERSION`.
The live TUI needs only `python3` (≥3.9, stdlib); the **report** extras (`R`) still
need `pip install matplotlib pillow` plus `brew install tectonic`.

### Homebrew (recommended)

```sh
brew tap arian-shamaei/anthropometer
brew trust arian-shamaei/anthropometer   # newer Homebrew asks you to trust third-party taps
brew install amtr
```

This installs the live TUI (a small Rust binary + a stdlib Python engine — no
heavy dependencies). To enable the **report** feature (`R` / `amtr-paper`):

```sh
pip install matplotlib pillow      # figures
brew install tectonic              # LaTeX → PDF
```

### From source

Needs Rust and Python 3.9+.

```sh
git clone https://github.com/arian-shamaei/anthropometer
cd anthropometer/rust
cargo install --path .             # → ~/.cargo/bin/amtr
```

The engine path is baked in at build time (overridable with `$AMTR_ENGINE`), so
`amtr` runs from any directory.

---

## Usage

```sh
amtr                       # newest/active session, from anywhere
amtr --session S.jsonl     # a specific transcript
amtr --project ~/my/repo   # newest session for that project
amtr --demo                # a self-contained demo (no live session needed)
```

Arm it beside a headless run to get a report the moment it finishes:

```sh
claude -p "do the thing" &
amtr-report --watch        # tails the live session; prints the report when it ends
```

**Keys:** `1`–`6` tabs · `f` sessions (`tab` wall) · `i` inspect · `m` map mode ·
`←/→` scrub · `R` report · `?` help · `q` quit.

---

## Authoritative vs. estimated

`amtr` is careful about what it *knows* versus what it *estimates* — every number
on screen belongs to one of these rows, and the two are never blurred:

| quantity | status | where it comes from |
|---|---|---|
| **R** — resident context | **exact** | `input + cache_read + cache_creation` of the newest assistant `usage` record — the same quantity `/context` reports |
| **cache waterline** | **exact** | `cache_read_input_tokens`; a backward jump is a real prefix invalidation (thrash) |
| **compaction attribution** | derived | `compact_boundary` set-difference, cross-checked against pre/post token counts |
| **per-item allocations** | *estimated* | chars-per-token ratios (per-category calibrated), laid out in true prompt order and force-fit to sum exactly to R; the invisible server-side context (system prompt, tool schemas) is carried as an honest **overhead** segment with a displayed calibration factor `α` |

---

## How it works

Two processes over newline-delimited JSON: a **Rust/ratatui UI** that owns only the
terminal, and a **Python engine** that owns all the data (transcript discovery,
tailing, accounting, checkpoints, replay). `SPEC.md` is the normative contract;
both sides are implemented against it alone, and cross-process contract tests spawn
the real engine and require every emitted line to parse.

```
        ~/.claude/projects/<project>/<session-id>.jsonl
╔═══════════════════════════════════════════════════════════════╗
║   session transcripts — already written by Claude Code itself ║
╚═══════════════════════════════╤═══════════════════════════════╝
                                │  tailed live (~250 ms); no
                                ▼  instrumentation, ever
┌───────────────────────────────────────────────────────────────┐
│  amtr_engine.py   (python3 ≥ 3.9, stdlib only)                │
│  owns ALL data — discovery · token accounting · checkpoints   ├────▶ report.pdf
│  · replay · fleet scan · compaction forensics                 │      (press R)
└───────────────┬───────────────────────────────┬───────────────┘
                │                               ▲
                │ Update  (JSON lines, fd 1)    │ Control  (JSON lines, stdin)
                │ map · turn · files · fleet …  │ attach · seek · peek · kill …
                ▼                               │
┌───────────────────────────────────────────────┴───────────────┐
│  amtr   (Rust + ratatui)   owns ONLY the terminal             │
└───────────────────────────────────────────────────────────────┘
```

## Repository layout

```
SPEC.md            the normative protocol + view contract
amtr_engine.py     the engine: discovery, tailing, accounting, checkpoints, replay
amtr_paper.py      the PDF report builder (amtr_figures/_turns/_phases support it)
rust/              the TUI (cargo test runs a headless screenshot suite)
tests/             engine test suite + synthetic fixtures
packaging/homebrew the Homebrew formula + tap runbook
docs/assets/       screenshots and figures for this README
```

## License

[MIT](LICENSE) © Arian Shamaei
