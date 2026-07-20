# Contributing to amtr

Thanks for your interest! `amtr` is a small, focused tool and contributions are welcome —
bug reports, feature ideas, and PRs.

## Architecture in one minute

Two processes over newline-delimited JSON (`SPEC.md` is the normative contract):

- **`rust/`** — the ratatui TUI. Owns *only* the terminal. Renders whatever the engine sends.
- **`amtr_engine.py`** — the Python engine (stdlib only). Owns *all* the data: transcript
  discovery, tailing, token accounting, checkpoints, and replay.

The report pipeline (`amtr_paper.py` + `amtr_figures.py` / `amtr_turns.py` / `amtr_phases.py`)
is separate and only needs `matplotlib`, `Pillow`, and `tectonic`.

Read `SPEC.md` before changing behavior on either side — both are implemented against it alone.

## Build & test

```sh
# Rust TUI
cd rust
cargo build --release
cargo test --release            # headless screenshot suite (no live session needed)
cargo install --path .          # → ~/.cargo/bin/amtr

# Python engine + report
python3 -m pytest tests/ -q
python3 amtr_engine.py --report --json --session tests/fixtures/golden.jsonl
```

`amtr --demo` runs a self-contained demo (no live Claude Code session required) — the fastest
way to see the whole instrument.

## Ground rules

- **Keep the two sides honest.** The engine labels every quantity *authoritative* (read from
  API usage records) vs *estimated*. Don't blur them.
- **Tests must pass** on both sides (`cargo test` + `pytest`) before a PR.
- **No new heavy deps** in the core TUI/engine path — the engine is stdlib-only on purpose.
- **Visual changes:** validate against the real terminal, not a mockup. `amtr --demo` +
  the capture harness under `tests/` is the source of truth.
- **Never commit real session data.** Fixtures and demo data are synthetic and generic.

## Submitting

1. Fork, branch, make focused changes with tests.
2. `cargo test --release` and `python3 -m pytest tests/ -q` green.
3. Open a PR describing the change and, for anything visual, include a before/after.

MIT-licensed — by contributing you agree your work is released under the same license.
