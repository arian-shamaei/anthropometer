# amtr

**A btop-style, real-time context debugger for AI coding agents — Claude Code, Codex CLI, Gemini CLI.**

`amtr` attaches to an agent session's transcript and renders — live — exactly what is
happening inside the model's context window: how the token budget is being spent, which
files and tools are resident, when compactions fire, what subagents are doing, the cache
economics of every turn, and the window each model actually runs under (200k or 1M, from
the CLI's own sources). INSPECT any turn, REPLAY the session, drill into a subagent, read a
compaction post-mortem.

```
cargo install amtr    # needs python3 ≥3.9 on PATH (engine is embedded, pure stdlib)
amtr                  # attach to your newest session
amtr --session ID     # or a specific one (Claude Code, Codex CLI, Gemini CLI)
```

Press `R` for the session report. Without anything else installed it writes `report.md`
instantly. The compiled PDF report is a separate plugin the TUI auto-discovers:

```
pip install amtr-paper    # figures + PDF builder (matplotlib, Pillow)
brew install tectonic     # LaTeX → PDF
```

The live TUI itself never needs the heavy dependencies. `man amtr` after install.

Full docs, screenshots, and the forensic autopsy of amtr's own 153-hour build session:
**https://github.com/arian-shamaei/anthropometer**

MIT.
