# amtr

**A btop-style, real-time diagnostic instrument for Claude Code sessions.**

`amtr` attaches to a Claude Code session's transcript and renders — live — exactly what is
happening inside the model's context window: how the token budget is being spent, which files
and tools are resident, when compactions fire, what subagents are doing, and the true cost of
every turn. Press `R` and it compiles a ground-truth PDF report of the whole session.

```
cargo install amtr    # needs python3 on PATH (engine is embedded, pure stdlib)
amtr                  # attach to your newest session
```

The R-key PDF report additionally wants `matplotlib`, `numpy`, `pillow`, and a LaTeX
installation; the live TUI needs only the Python standard library.

Full docs, screenshots, and the forensic autopsy of amtr's own 153-hour build session:
**https://github.com/arian-shamaei/anthropometer**

MIT.
