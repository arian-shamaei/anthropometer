# amtr report fields

Output of `amtr-report.sh --json`: one JSON object with these top-level keys, in order.
Every block that carries a `label` is either `authoritative` (read from the API usage
records in the transcript) or `estimated` (sized from text with a chars-per-token fit).

## header
`session_id`, `title`, `name` (amtr's short name for the session), `project` (cwd),
`models[]`, `model_switches`, `cc_version`, `started_at`, `ended_at`, `duration_s`,
`turns`, `entrypoint` (cli, sdk, headless), `interrupted`.

## context (authoritative)
- `final_r` resident tokens at the end; `budget` the window; `pct_budget`.
- `peak_r`, `peak_turn`: the high-water mark and when.
- `waterline`: the cached prefix size at the end. A waterline that drops between turns
  is a cache prefix invalidation (see `economics.thrash` and `timeline.notes`).
- `compactions[]`: `turn`, `ts`, `trigger` (auto or manual), `pre`, `post`, `dropped`,
  `cum_dropped`, `dur_ms`, `dropped_cats{}`, `dropped_files[]` (`file` is an index into
  the files table order, `tok` the tokens lost), `preserved_msgs`.
- `rebuilds[]`: server-side context rebuilds.
- `cats{}`: tokens by category at the end: `overhead` (system prompt, tool schemas,
  skills: server-side, one number), `user`, `assistant`, `thinking`, `reasoning`,
  `file` (file reads), `bash` (shell output), `tool` (other tool results), `attach`
  (attachments and pasted content), `summary` (compaction summaries).
- `alpha`: share of the resident window the categories account for.
- `fit{}`: the chars-per-token fit. `active` false means a single global constant
  (`prior_cpt`) sized everything; `active` true means per-category ratios (`cpt{}`)
  with the hold-out error in `holdout_fit_pct`.

## economics (authoritative)
`in`, `cache_read`, `cc_5m`, `cc_1h` (cache creation, by TTL), `out`, `hit` (cache-read
share of input), `cost_total`, `cost_mean`, `cost_p95` (in list-price units, `u`),
`thrash` (count of prefix invalidations), `models[]` with the same fields per model.

## files (estimated)
`table[]`: `path`, `tok` resident now, `pct_r`, `reads`, `writes`, `edits`, `waste`
(tokens spent re-reading content already in context), `resident`. `totals{}` and
`total_waste` sum the table; `evicted` counts files dropped by compaction.

## shell
`n`, `ok`, `failed`, `interrupted`, `bg` (background), `tok_out` (tokens the outputs
cost), `failures[]` (`ts`, `turn`, `cmd`, `err`), `top[]` (the most expensive commands
by output tokens).

## retrieval
Tool searches and MCP calls: `n`, `tok`, `by_kind[]`, `by_src[]`, `failures[]`.

## agents
Subagents: `n`, `counts{}` by state, `own_tok` (spent inside their own windows),
`ret_tok` (returned to the main window), `x_main` (own spend as a multiple of the main
window), `amp_median`, `top[]` (`type`, `desc`, `state`, `own_tok`, `ret_tok`, `amp`,
`dur_ms`).

## events
`ts`, `kind` (api_error, rate_limit, model_switch, ...), `severity`, `turn`, `msg`.

## timeline
`spark`: one block character per slice of the session, resident tokens scaled to
`peak`. `marks`: the same slices with `▼` at compactions. `notes[]`: dated findings
(thrash, compaction, errors) with `turn`, `ts`, `kind`, `msg`.

## diagnostics
The engine's own ranked findings as strings: waste hot-spots, sub-50% cache-hit turns,
repeated failures. Repeat these before adding your own.
