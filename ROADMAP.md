# Roadmap

`amtr` is usable today. Rough direction, newest priorities first — subject to change,
and issues/PRs are welcome.

## Near term
- **Prebuilt binaries + one-line installer** — `curl … | sh` and release archives for
  macOS/Linux, so trying it needs no `cargo` or tap.
- **Broader terminal coverage** — verify glyph rendering (blocks/braille) across common
  terminals; document recommended fonts.
- **Report polish** — dark-theme report figures option; faster figure rendering.

## Later
- **Homebrew core** — submit once the project is notable/maintained enough (until then, the
  [tap](https://github.com/arian-shamaei/homebrew-anthropometer) is the install path).
- **Live retrieval/RAG detail** — richer view of external pulls (web, MCP connectors).
- **More diagnostics** — additional automated findings in the report.
- **Config** — user-tunable budgets, palettes, and default views.

## Non-goals
- A vector store or its own retrieval — Claude Code *is* the retriever; `amtr` observes it.
- Modifying sessions — `amtr` is read-only; it never writes to a transcript.

Have an idea? Open an issue.
