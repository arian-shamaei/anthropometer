---
name: amtr
description: Debug the agent session you are in. Reads the session transcript with amtr (anthropometer) and answers what is filling the context window, what the session has cost, what a compaction dropped, which files are being re-read, and what subagents spent. Use when the user asks about context usage, tokens, cost, cache hits, compaction, or wants a report of a Claude Code, Codex CLI, or Gemini CLI session.
license: MIT
compatibility: Claude Code (also reads Codex CLI and Gemini CLI transcripts). Needs python3 3.9 or newer, standard library only. Works from a bare checkout; a system install of amtr is used when present.
metadata:
  author: arian-shamaei
  source: https://github.com/arian-shamaei/anthropometer
  version: "0.6.0"
---

# amtr

`amtr` is the agentic debugger for Claude Code. This skill is its headless form:
one script reads the current session's transcript and returns a ground-truth
report, so the agent can answer questions about its own context window, cost,
compactions, file traffic, and subagents with numbers instead of guesses.

Every figure is labeled the way amtr labels it: **authoritative** (read from
the API usage records in the transcript) or **estimated** (sized from text).
Keep that label when you repeat a number.

## When to use this skill

- "How full is my context?" / "What is eating my context window?"
- "What has this session cost so far?" / "Is the cache hitting?"
- "What did the last compaction drop?"
- "Which files keep getting re-read?" / "Where am I wasting tokens?"
- "What did the subagents spend?" / "How much did that agent return?"
- "Write me a report of this session" (or of another session by id or path).
- After a headless run (`claude -p`, Codex, Gemini) to see what it did.

## What this skill does

1. **Context map**: resident tokens against the budget, peak turn, waterline,
   composition by category (overhead, user, assistant, reasoning, file, bash,
   tool, attachments, summary).
2. **Economics**: input, cache-read, cache-create, output tokens, hit rate,
   cost per turn, thrash events (a cache prefix invalidated).
3. **Compaction autopsy**: for each compaction, what was dropped by category
   and by file, how many messages were preserved, how long it took.
4. **File traffic**: per-file tokens, reads, writes, edits, and waste (tokens
   spent re-reading content already in context).
5. **Shell, retrieval, agents, events**: failed commands, tool searches and
   MCP calls, subagent spend and amplification, API errors, a timeline sparkline.

The skill is read-only. It never edits a transcript, and it does not touch the
session it reads.

## How to use

Run the script. With no target it reports the newest session for the current
working directory, which is the session the agent is in.

```sh
bash <skill-dir>/scripts/amtr-report.sh --json
```

Targets:

```sh
--session <path or session id>    a specific transcript (Claude Code, Codex CLI, Gemini CLI)
--project <dir>                   the newest session under that project
--budget <tokens>                 pin the context budget when the model window is unknown
--watch                           wait for a running headless session to end, then report
```

Drop `--json` for the markdown report, which is what to write to a file when
the user asks for a report:

```sh
bash <skill-dir>/scripts/amtr-report.sh > session-report.md
```

Then answer the question from the JSON. Read `references/report-fields.md`
for the field guide and the thresholds below.

## Reading the report

- `context.pct_budget` at 80 or more: a compaction is close. Say so and
  suggest finishing the current step before it lands.
- `context.cats`: the largest category is the answer to "what is eating my
  context". `attach` is tool results and pasted content, `file` is file reads,
  `reasoning` is the model's own thinking.
- `economics.hit` under 0.9, or `economics.thrash` above 0: the cache prefix
  is being invalidated. Long edits to early context, system prompt changes,
  and model switches cause this.
- `context.compactions[].dropped_cats` and `dropped_files`: the autopsy. Name
  the top categories and files that were lost.
- `files.table[].waste`: tokens spent re-reading. A file with high waste and
  many reads should be read once and kept in mind, or read in ranges.
- `agents.top[]`: `own_tok` is what the subagent spent in its own window,
  `ret_tok` is what it returned to the main window, `amp` is the ratio.
- `diagnostics[]`: the engine's own findings. Repeat them; they are already
  ranked.

Report the two or three numbers that answer the question, then one action.
Do not paste the whole JSON.

## Example

**User**: "Why is my context at 70% already? I barely did anything."

**Agent** runs `bash <skill-dir>/scripts/amtr-report.sh --json` and answers:

```
Context: 172,435 of 1,000,000 tokens resident (17.2%), peak at turn 38. Authoritative.

  attach      72,200   41.9%   tool results and pasted content
  bash        35,427   20.5%   shell output
  overhead    31,391   18.2%   system prompt, tool schemas, skills
  reasoning   22,869   13.3%

The largest block is tool results. Three Bash calls returned over 3,000 tokens each
(turns 4, 23, 25). Pipe long outputs through head or grep before returning them.
```

**Inspired by:** [The autopsy](https://github.com/arian-shamaei/anthropometer/tree/main/docs/autopsy),
where amtr dissected the 153-hour Claude Code session that built it: 1,945 turns,
1.02 billion cache-read tokens, 3 compactions.

## Tips

- Run it before a long task to know the starting point, and again after, so
  the cost of the task is a difference of two reports.
- For a subagent's window, pass its transcript path with `--session`; the
  main report's `agents.top[]` lists them.
- If the model's window size is not known to the engine, `pct_budget` is
  computed against a default. Pass `--budget` with the real window.
- The full TUI (`amtr`) has everything here live, with replay and inspect
  keys. Install: `cargo install amtr`, `brew install arian-shamaei/anthropometer/amtr`,
  or the curl one-liner in the repository README.

## Common use cases

- Deciding whether to compact, start a fresh session, or keep going.
- Explaining a surprising bill for a session.
- Post-mortem of a headless run that produced a bad result.
- Finding the one file that a session keeps re-reading.
- Comparing two sessions that did the same task with different prompts.
