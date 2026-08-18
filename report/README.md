# amtr-paper

The compiled-report plugin for [amtr](https://github.com/arian-shamaei/anthropometer)
(anthropometer). The core `amtr` TUI is stdlib-only; this package carries the
heavy half — matplotlib figures, GIF animations, and the compiled LaTeX PDF —
as a separate install so the monitor itself stays dependency-free.

```sh
pip install amtr-paper
brew install tectonic        # LaTeX -> PDF (any channel works; cargo too)
```

## What it adds

- `amtr-paper` on your PATH: builds the full report directory
  (`report.pdf`, `report.md`, `figures/`, `turns/`) from any Claude Code
  session transcript. Standalone — it does not need amtr installed.
- The `R` key inside `amtr` auto-discovers `amtr-paper` and uses it to build
  the PDF in the background (progress and errors land in the report
  directory's `paper.log`). Without this package, `R` still writes
  `report.md` (stdlib) and tells you the PDF needs the plugin.

Without `tectonic` on PATH, `amtr-paper` still writes the figures, the
markdown report, and the per-turn capture; only the compiled PDF is skipped.

The stdlib-only markdown report command, `amtr-report`, stays in core amtr —
this plugin is only the compiled paper.

## Usage

```sh
amtr-paper --session <transcript.jsonl | session-id>
amtr-paper --project <project-dir>          # newest session under it
amtr-paper --session X --dir OUTDIR         # choose the report directory
```

Default output: `~/.claude/amtr-reports/<name>-<id8>/`.

## Note on modules

The wheel vendors its own copies of the report modules and `amtr_engine.py`
(synced at build time from the repo root by `build.sh`) so it works
standalone. In a repo checkout, the `R` key of a dev-built `amtr` prefers the
checkout copies automatically whenever matplotlib and Pillow import.

## Releasing

```sh
./report/build.sh --publish     # vendors modules, builds, twine-uploads
```

Version lives in `report/pyproject.toml`, independent of the amtr version.
