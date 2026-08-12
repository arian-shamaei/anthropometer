---
title: "Watching an LLM think, in ratatui"
description: "What building amtr taught me about terminal rendering"
---

# Watching an LLM think, in ratatui: what building amtr taught me about terminal rendering

*Arian Shamaei · August 2026*

[amtr](https://github.com/arian-shamaei/anthropometer) is a btop-style
monitor for Claude Code sessions: a live map of the model's context window,
per-file token accounting, and cache economics, drawn entirely in the
terminal. The interesting problems turned out to be Rust problems, and
mostly ratatui problems.

**A fixed-scale map that never lies.** The centerpiece is a block map where
every cell is a fixed quantum of context space. Rendering it means bucketing
a few hundred segments into a `Rect` that resizes under you, without the map
ever stretching: the cell "rung" (tokens per cell) climbs a ladder until the
budget fits the pane. The invariant — bright cells + dim cells = the whole
budget, always — is enforced by a `TestBackend` test, not by eye.

**Color-vision safety as a unit test.** Themes are gated by a test that
simulates protanopia and deuteranopia (Machado 2009 matrices) and fails the
build if any two semantic colors land within a CIE76 ΔE floor of each other.
Shipping a theme means passing an accessibility proof, not a vibe check.

**The terminal as source of truth.** Visual regressions are caught by
driving the real binary in tmux and asserting on `capture-pane -e` output —
per-cell char+SGR signatures, sampled across time to prove animations
actually animate. A typescript replay will lie to you; the cell grid won't.

**Hybrid architecture.** The TUI is a single Rust binary (crossterm +
ratatui); the transcript parser is a stdlib-only Python engine the binary
spawns and talks to over JSON lines. Cargo ships the engine inside the
crate so `cargo install amtr` is self-contained.

*Disclosure: amtr is substantially built with LLM assistance (Claude),
which is fitting — it is a tool for watching that assistance spend your
tokens.*

Repo: [github.com/arian-shamaei/anthropometer](https://github.com/arian-shamaei/anthropometer)
