#!/usr/bin/env python3
"""Generator for tests/fixtures/showcase.jsonl — a RICH, fully-synthetic Claude
Code session transcript used only to showcase amtr's figures/screenshots.

Everything here is FICTIONAL: a made-up "webapp" REST-API project under
/Users/dev/code/webapp, generic filenames, generic prompts, generic shell
commands. NO real user data, paths, names, or content appears. Run:

    python3 tests/fixtures/gen_showcase.py > tests/fixtures/showcase.jsonl

The session is engineered so the derived figures look full. The engine defines
resident context R = input + cache_read + cache_creation per turn, and the map's
overhead band = R - (sum of visible content segments). So to get a COLORFUL map
(not a wall of "system overhead") the transcript must carry real content
(file reads, writes, thinking, shell output) whose token mass tracks R. We
mirror the engine's estimator here (ceil(len/3.8)) and drive each turn's
cache_read from the running live-content total, keeping overhead a small,
realistic sliver. Result:
  * ~40 turns spanning read / write / edit / bash / thinking / subagents
  * 10 distinct fake files touched repeatedly (files-roll marks, one map hue each)
  * a diverse category mix (file/reasoning/bash/tool/user) for a colorful map
  * 3 subagents with staggered starts + distinct durations (agents timeline)
  * resident context climbs to ~62% of a 1,000,000 budget with a single
    auto-compaction dip in the middle (a climbing EKG)
"""
import datetime
import json
import math
import sys

CWD = "/Users/dev/code/webapp"
SID = "5eadface-1000-4000-9000-abcdef012345"
VER = "2.1.205"
BRANCH = "main"
CHARS_PER_TOK = 3.8
OVERHEAD = 8000              # small, realistic system-prompt/tool-schema band
_ORIGIN = 1_760_000_000     # arbitrary fake epoch (generic, not "now")


def E(s):
    return int(math.ceil(len(s) / CHARS_PER_TOK)) if s else 0


# ---- generic filler content of an approximate token size --------------------
_LOREM = ("handle the request, validate the payload, look up the record, "
          "apply the business rule, write the response back to the client")


def blob(tok, tag):
    """A generic code/text blob of ~`tok` estimated tokens. Deterministic and
    obviously synthetic — no real data."""
    target = int(tok * CHARS_PER_TOK)
    out = []
    n = 0
    i = 0
    while n < target:
        line = "# %s L%04d: %s\n" % (tag, i, _LOREM)
        out.append(line)
        n += len(line)
        i += 1
    return "".join(out)


# ---- wall clock -------------------------------------------------------------
_T = [0]


def ts(step=7):
    _T[0] += step
    return (datetime.datetime.utcfromtimestamp(_ORIGIN + _T[0])
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))


# ---- record plumbing --------------------------------------------------------
_recs = []
_last_uuid = [None]
_uidn = [0]
LIVE = [0]                 # mirror of engine est_live (visible content tokens)
HIST = []                  # [(uuid, est_added)] for compaction survivor picking


def _uid(pfx):
    _uidn[0] += 1
    return "%s-%04d" % (pfx, _uidn[0])


def emit(rec):
    _recs.append(rec)


def base(uuid, typ, t, **extra):
    r = {"parentUuid": _last_uuid[0], "isSidechain": False,
         "userType": "external", "cwd": CWD, "sessionId": SID,
         "version": VER, "gitBranch": BRANCH, "entrypoint": "cli",
         "type": typ, "uuid": uuid, "timestamp": t}
    r.update(extra)
    _last_uuid[0] = uuid
    return r


def _grow(uuid, est):
    LIVE[0] += est
    HIST.append((uuid, est))


# ---- files (generic REST-API webapp) ----------------------------------------
FILE_PATHS = [
    "src/server.py", "src/routes/auth.py", "src/routes/users.py", "src/db.py",
    "src/models.py", "src/middleware.py", "tests/test_api.py",
    "tests/test_auth.py", "README.md", "config.yaml",
]


def tag_of(path):
    return path.split("/")[-1].split(".")[0]


# ---- turn / usage -----------------------------------------------------------
_turn = [0]


def _usage():
    """Resident = LIVE(before this turn's tool result) + overhead. cache_read
    carries the bulk; cache_creation is a small sliver (keeps cc/R < 0.2)."""
    t = _turn[0]
    inp = 6 + (t % 5)
    cc = 1500 + (t % 3) * 300
    cr = max(0, LIVE[0] + OVERHEAD - inp - cc)
    return {"input_tokens": inp, "output_tokens": 90 + (t % 6) * 12,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
            "service_tier": "standard", "inference_geo": "not_available",
            "cache_creation": {"ephemeral_5m_input_tokens": cc,
                               "ephemeral_1h_input_tokens": 0}}


def user_prompt(text):
    u = _uid("u")
    emit(base(u, "user", ts(), message={"role": "user", "content": text},
              promptId="p-" + u, promptSource="typed"))
    _grow(u, E(text))


def assistant(blocks):
    _turn[0] += 1
    a = _uid("a")
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) \
        else "end_turn"
    emit(base(a, "assistant", ts(), requestId="req_%03d" % _turn[0],
              message={"id": "msg_" + a, "type": "message", "role": "assistant",
                       "model": "claude-fable-5", "content": blocks,
                       "stop_reason": stop, "stop_sequence": None,
                       "usage": _usage()}))
    # mirror the assistant-side allocations (text/thinking/tool_use input)
    est = 0
    for b in blocks:
        if b.get("type") == "text":
            est += E(b.get("text", ""))
        elif b.get("type") == "thinking":
            est += E(b.get("thinking", ""))
        elif b.get("type") == "tool_use":
            est += E(json.dumps(b["input"], ensure_ascii=False))
    _grow(a, est)
    return a


def tool_result(tuid, content, tur, grow_est):
    u = _uid("u")
    src = _last_uuid[0]
    emit(base(u, "user", ts(),
              message={"role": "user", "content": content if isinstance(
                  content, list) else [
                  {"tool_use_id": tuid, "type": "tool_result",
                   "content": content, "is_error": False}]},
              toolUseResult=tur, sourceToolAssistantUUID=src))
    _grow(u, grow_est)


# ---- action helpers (content sizes are the token curve's control knobs) -----
def do_read(path, tok, note=None):
    tuid = _uid("toolu")
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Read",
         "input": {"file_path": CWD + "/" + path}, "caller": {"type": "direct"}}]
    assistant(blocks)
    content = blob(tok, tag_of(path))
    nl = content.count("\n") + 1
    tur = {"type": "text", "file": {"filePath": CWD + "/" + path,
           "content": content, "numLines": nl, "startLine": 1,
           "totalLines": nl}}
    tool_result(tuid, "     1\t" + content[:200], tur, E(content))


def do_write(path, tok, note=None):
    tuid = _uid("toolu")
    content = blob(tok, tag_of(path))
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Write",
         "input": {"file_path": CWD + "/" + path, "content": content},
         "caller": {"type": "direct"}}]
    # Write allocates its input (which includes content) on the assistant side
    _grow_pre = LIVE[0]
    assistant(blocks)
    tur = {"type": "create", "filePath": CWD + "/" + path, "content": content,
           "structuredPatch": [], "originalFile": None, "userModified": False}
    tool_result(tuid, "File created successfully at: " + CWD + "/" + path,
                tur, 20)


def do_edit(path, old, new, note=None):
    tuid = _uid("toolu")
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Edit",
         "input": {"file_path": CWD + "/" + path, "old_string": old,
                   "new_string": new}, "caller": {"type": "direct"}}]
    assistant(blocks)
    tur = {"filePath": CWD + "/" + path, "oldString": old, "newString": new,
           "originalFile": "", "structuredPatch": [
               {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
                "lines": ["-" + old, "+" + new]}],
           "userModified": False, "replaceAll": False}
    tool_result(tuid, "The file " + CWD + "/" + path + " has been updated.",
                tur, 40)


def do_bash(cmd, desc, tok, stdout_head, note=None):
    tuid = _uid("toolu")
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Bash",
         "input": {"command": cmd, "description": desc},
         "caller": {"type": "direct"}}]
    assistant(blocks)
    body = stdout_head + "\n" + blob(tok, "log").replace("# log", "  ")
    tur = {"stdout": body, "stderr": "", "interrupted": False, "isImage": False}
    tool_result(tuid, body[:600], tur, E(body))


def do_grep(pattern, tok, note=None):
    tuid = _uid("toolu")
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Grep",
         "input": {"pattern": pattern, "output_mode": "content"},
         "caller": {"type": "direct"}}]
    assistant(blocks)
    out = blob(tok, "match")
    tool_result(tuid, out[:400],
                {"mode": "content", "numLines": out.count("\n")}, E(out))


def do_task(desc, prompt, summary, dur_ms, own_tok, ret_tok, tools, note=None):
    tuid = _uid("toolu")
    blocks = ([{"type": "text", "text": note}] if note else []) + [
        {"type": "tool_use", "id": tuid, "name": "Task",
         "input": {"description": desc, "prompt": prompt,
                   "subagent_type": "general-purpose"},
         "caller": {"type": "direct"}}]
    assistant(blocks)
    aid = _uid("agent") + "cafe"
    ret_text = summary + " " + blob(ret_tok, "agent")
    tur = {"status": "completed", "agentId": aid,
           "agentType": "general-purpose", "prompt": prompt,
           "content": [{"type": "text", "text": ret_text}],
           "totalDurationMs": dur_ms, "totalTokens": own_tok,
           "totalToolUseCount": sum(tools.values()),
           "usage": {"input_tokens": 14, "output_tokens": 900,
                     "cache_read_input_tokens": int(own_tok * 0.7),
                     "cache_creation_input_tokens": int(own_tok * 0.2)},
           "toolStats": {"readCount": tools.get("r", 0),
                         "searchCount": tools.get("s", 0),
                         "bashCount": tools.get("b", 0),
                         "editFileCount": tools.get("e", 0),
                         "linesAdded": tools.get("e", 0) * 12,
                         "linesRemoved": tools.get("e", 0) * 4,
                         "otherToolCount": 1}}
    tool_result(tuid, [{"tool_use_id": tuid, "type": "tool_result",
                        "content": [{"type": "text", "text": summary}],
                        "is_error": False}], tur, E(ret_text))


def thinking(text_tok, tag):
    txt = blob(text_tok, "think-" + tag)
    assistant([{"type": "thinking", "thinking": txt, "signature": "sig"},
               {"type": "text", "text": "Working through that now."}])


def turn_duration(ms):
    emit(base(_uid("s"), "system", ts(), subtype="turn_duration",
              durationMs=ms, messageCount=2))


def compact_boundary(target_survivor):
    """Evict everything except a recent tail of records whose content sums to
    ~target_survivor tokens (a realistic auto-compaction). Reset the mirror."""
    keep = []
    acc = 0
    for uuid, est in reversed(HIST):
        keep.append(uuid)
        acc += est
        if acc >= target_survivor:
            break
    keep = list(reversed(keep))
    pre = LIVE[0] + OVERHEAD
    LIVE[0] = acc
    HIST[:] = [(u, e) for (u, e) in HIST if u in set(keep)]
    anchor = keep[0]
    emit(base(_uid("s"), "system", ts(), subtype="compact_boundary",
              content="Conversation compacted", level="info",
              logicalParentUuid=keep[-1],
              compactMetadata={"trigger": "auto", "preTokens": pre,
                               "postTokens": acc + OVERHEAD, "durationMs": 9200,
                               "cumulativeDroppedTokens": pre - acc,
                               "preCompactDiscoveredTools": [],
                               "preservedSegment": {"headUuid": anchor,
                                                    "anchorUuid": anchor,
                                                    "tailUuid": keep[-1]},
                               "preservedMessages": {"anchorUuid": anchor,
                                                     "uuids": keep,
                                                     "allUuids": keep}}))


# ============================ THE SESSION =====================================
emit({"type": "ai-title", "aiTitle": "webapp: build the REST API",
      "sessionId": SID})

# --- phase 1: survey the codebase (climb from ~0) ---------------------------
user_prompt("Let's build out the webapp REST API. Start by surveying the "
            "existing server and routes so you understand the layout, then we "
            "will add rate limiting and expand the tests.")
thinking(6_000, "survey")
do_read("src/server.py", 20_000, "Reading the app entrypoint.")
do_read("src/routes/auth.py", 24_000)
do_read("src/routes/users.py", 16_000)
do_grep("def handler", 5_000, "Grepping for the handler entry points.")
do_read("src/db.py", 15_000)
do_read("src/models.py", 13_000)

# --- phase 2: first subagent audits auth ------------------------------------
do_task("audit auth routes",
        "Review src/routes/auth.py for security issues and report findings.",
        "Audit complete: token refresh path is missing a rate limit; add "
        "middleware.", dur_ms=48_000, own_tok=61_000, ret_tok=4_000,
        tools={"r": 4, "s": 2, "b": 1, "e": 0},
        note="Launching a subagent to audit the auth routes in parallel.")
do_bash("cd /Users/dev/code/webapp && python3 -m pytest -q",
        "run the test suite", 5_000, "18 passed, 2 warnings in 1.84s",
        note="Running the current test suite to get a baseline.")

# --- phase 3: implement middleware + wire it --------------------------------
do_read("src/middleware.py", 10_000)
do_write("src/middleware.py", 14_000,
         "Adding a rate-limit guard to the middleware as the audit suggested.")
do_edit("src/server.py", "def handler(req):", "def handler(req):  # guarded",
        "Wiring the middleware into the server entrypoint.")
do_edit("src/routes/auth.py", "refresh()", "refresh()  # rate-limited",
        "Guarding the token refresh path.")
do_bash("cd /Users/dev/code/webapp && git status --porcelain",
        "check working tree", 4_000,
        " M src/server.py\n M src/routes/auth.py\n M src/middleware.py")
do_read("tests/test_auth.py", 11_000)
do_edit("tests/test_auth.py", "def test_login():",
        "def test_login():  # + rate-limit case",
        "Adding a test for the new rate-limit behavior.")
do_bash("cd /Users/dev/code/webapp && python3 -m pytest tests/test_auth.py -q",
        "run auth tests", 3_000, "6 passed in 0.42s")

# --- phase 4: second subagent surveys coverage, expand tests ----------------
do_task("survey test coverage",
        "Map which route handlers lack test coverage and summarize gaps.",
        "Coverage survey: users routes at 40%, auth now 85%, wiring untested.",
        dur_ms=33_000, own_tok=44_000, ret_tok=4_000,
        tools={"r": 6, "s": 3, "b": 1, "e": 0},
        note="Delegating a coverage survey while I keep editing.")
thinking(9_000, "coverage")
do_read("tests/test_api.py", 14_000)
do_write("tests/test_api.py", 45_000,
         "Expanding the integration tests for the users routes.")
do_bash("cd /Users/dev/code/webapp && python3 -m pytest -q",
        "full suite after new tests", 5_000, "27 passed in 2.10s")

# --- a single auto-compaction (context got large) ---------------------------
turn_duration(4200)
compact_boundary(target_survivor=120_000)
u = _uid("u")
emit(base(u, "user", ts(),
          message={"role": "user",
                   "content": "This session is being continued from a previous "
                   "conversation. Summary: surveyed the webapp API, added "
                   "rate-limit middleware, expanded tests."},
          promptId="p-cs", promptSource="typed",
          isCompactSummary=True, isVisibleInTranscriptOnly=True))
_grow(u, 40)
assistant([{"type": "text", "text": "Caught up after compaction — continuing "
            "with the db refactor and docs."}])

# --- phase 5: db refactor via third subagent + edits ------------------------
do_read("src/db.py", 16_000)
do_task("refactor db layer",
        "Refactor src/db.py to a context-managed connection pool; return diff.",
        "Refactor done: pool is context-managed; 3 call sites updated, tests "
        "green.", dur_ms=61_000, own_tok=72_000, ret_tok=5_000,
        tools={"r": 5, "s": 2, "b": 3, "e": 4},
        note="Handing the db refactor to a subagent with edit access.")
do_edit("src/db.py", "connect()", "connect()  # pooled",
        "Applying the pooled-connection change to db.py.")
do_edit("src/models.py", "class User:", "class User:  # typed",
        "Tightening the model types to match.")
do_read("src/routes/users.py", 40_000,
        "Re-reading the users routes to point them at the pool.")
do_edit("src/routes/users.py", "get_db()", "get_db()  # pool",
        "Pointing the users routes at the pooled db helper.")
do_bash("cd /Users/dev/code/webapp && python3 -m pytest -q",
        "full suite after refactor", 6_000, "31 passed in 2.44s")
do_bash("cd /Users/dev/code/webapp && ruff check src tests",
        "lint the tree", 3_000, "All checks passed!")

# --- phase 6: docs + final verification (climb to peak) ---------------------
do_read("README.md", 60_000)
do_write("README.md", 70_000, "Updating the README with run + auth steps.")
do_edit("config.yaml", "port: 8080", "port: 8080\n  rate_limit: 100",
        "Documenting the rate-limit default in config.")
do_bash("cd /Users/dev/code/webapp && npm install",
        "install js deps for the docs site", 30_000, "added 214 packages in 6s")
do_bash("cd /Users/dev/code/webapp && npm test",
        "run the js doc-site tests", 20_000,
        "Test Suites: 3 passed, 3 total\nTests: 12 passed, 12 total")
do_read("src/server.py", 100_000,
        "Final full read of the server to confirm the wiring.")
do_read("src/routes/auth.py", 110_000,
        "Final full read of the auth routes.")
do_bash("cd /Users/dev/code/webapp && git add -A && git commit -m "
        "'add rate limiting, pool db, expand tests'",
        "commit the work", 8_000,
        "[main 9f3a1c2] add rate limiting, pool db, expand tests\n"
        " 9 files changed, 148 insertions(+), 22 deletions(-)")
thinking(12_000, "wrap")
assistant([{"type": "text", "text": "Done. Added rate-limit middleware, "
            "refactored the db to a pooled connection, expanded API + auth "
            "tests (31 passing), updated the README and config, and committed "
            "the work. Three subagents handled the auth audit, coverage "
            "survey, and db refactor."}])
turn_duration(5100)

# ------------------------------------------------------------------ dump
for r in _recs:
    sys.stdout.write(json.dumps(r) + "\n")
