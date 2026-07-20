#!/usr/bin/env python3
"""amtr_turns.py — the amtr PER-TURN CAPTURE ("what happens in every turn").

Groups a fully-fed session's records by turn and emits, for each assistant
turn, a self-contained record of everything that happened:

  - turn index, timestamp, model, stop reason, duration
  - resident R, its growth Delta-R vs the previous turn, structural cost, hit%
  - the assistant's ACTION that turn: a trimmed one-line text summary plus the
    list of tool_uses (name + primary target, e.g. ``Read x.py``,
    ``Bash "cargo test"``, ``Edit main.rs``, ``Task <desc>``)
  - files touched (faccess), commands run (cmds), retrievals (rets), subagents
    launched (agents where turn0 == t), and events (compaction / rebuild /
    error) landing on that turn.

Two outputs land under ``turns/``: ``turns.jsonl`` (one machine-readable JSON
object per turn) and ``turns.md`` (a scannable per-turn timeline). Optionally a
``turns/frames/`` directory with the per-turn context-map PNG (reused from the
already-rendered GIF frames — no extra render cost).

The per-turn ASSISTANT TEXT and tool_use targets are NOT stored on Session (it
aggregates content into categories), so those are recovered by a light
re-parse of the transcript that segments on the engine's own
``Session.is_new_turn`` boundary test — guaranteeing the turn indices line up
exactly with ``sess.turns`` / ``sess.faccess`` / ``sess.cmds`` / ...

stdlib only (+ Pillow, transitively, only when saving frames). All LOCAL.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import amtr_engine as ce            # noqa: E402


# ---------------------------------------------------------------- formatting
def _kfmt(n):
    """Compact token count, e.g. 5300 -> '5.3k', 940 -> '940'."""
    n = int(n or 0)
    a = abs(n)
    if a < 1000:
        return str(n)
    return "%.1fk" % (n / 1000.0)


def _skfmt(n):
    """Signed compact token delta, e.g. +5.3k / -12k / 0."""
    n = int(n or 0)
    if n == 0:
        return "0"
    return ("+" if n > 0 else "-") + _kfmt(abs(n))


def _one_line(s, n=200):
    """Collapse whitespace to a single line and clip to n chars."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


_CD_SEG = re.compile(r'^\s*cd\s+("[^"]*"|\'[^\']*\'|\S+)\s*$')
_ASSIGN_SEG = re.compile(r'^\s*[A-Za-z_]\w*=("[^"]*"|\'[^\']*\'|\S+)\s*$')


_LEAD_NOISE = re.compile(
    r'^\s*(?:cd\s+("[^"]*"|\'[^\']*\'|\S+)'
    r'|[A-Za-z_]\w*=("[^"]*"|\'[^\']*\'|\S+))\s*;?\s+')


def _clean_shell(cmd):
    """Drop leading `cd <path>` and `VAR=val` prefixes so the operative command
    shows instead of the `cd "/very/long/path" &&` (or `S=/long/path;`)
    boilerplate that prefixes almost every Claude Code shell call."""
    cmd = str(cmd or "")
    parts = re.split(r'\s*&&\s*', cmd)
    while len(parts) > 1 and (_CD_SEG.match(parts[0]) or _ASSIGN_SEG.match(parts[0])):
        parts.pop(0)
    out = " && ".join(parts).strip()
    # peel leading cd/assignment prefixes glued by `;` or whitespace, too
    for _ in range(5):
        m = _LEAD_NOISE.match(out)
        if not m:
            break
        out = out[m.end():]
    return out or cmd.strip()


def _tool_target(name, inp):
    """The primary target of a tool_use, formatted for a one-line summary.

    file tools -> basename, Bash -> the quoted command head, Agent/Task ->
    the delegated description, retrieval/other -> the most descriptive input
    field (query/pattern/url/...)."""
    if not isinstance(inp, dict):
        return ""
    if name in ce.AGENT_TOOLS:
        return _one_line(inp.get("description") or inp.get("prompt") or "", 80)
    if name == "Bash":
        cmd = _one_line(_clean_shell(inp.get("command") or ""), 80)
        return ('"%s"' % cmd) if cmd else ""
    fp = ce.tool_file(inp)
    if fp:
        return os.path.basename(fp.rstrip("/")) or fp
    for k in ("query", "pattern", "url", "q", "prompt", "path", "filePattern"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return _one_line(v, 80)
    return ""


def _tool_str(name, inp):
    tgt = _tool_target(name, inp)
    return ("%s %s" % (name, tgt)).strip()


# ---------------------------------------------------------------- re-parse
def _reparse_actions(path, budget=None):
    """turn_idx -> {"text": [str,...], "tools": [str,...]} recovered from the
    transcript. Segments on the engine's own is_new_turn (fed to a fresh probe
    Session so req_last advances exactly as it does for the real feed) — turn
    indices therefore match sess.turns one-for-one."""
    probe = ce.Session(path, budget=budget or ce.default_budget())
    actions = {}
    turn_idx = -1
    try:
        fh = open(path, "rb")
    except OSError:
        return actions
    with fh:
        for raw in fh:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                d = json.loads(s)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            new = probe.is_new_turn(d)      # BEFORE feed (feed advances req_last)
            probe.feed_line(s)
            if new:
                turn_idx += 1
            if turn_idx < 0:
                continue
            if d.get("type") != "assistant":
                continue
            if d.get("isSidechain") and not probe.sidechain_ok:
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            if (m.get("model") or "") == "<synthetic>":
                continue
            if d.get("isApiErrorMessage"):
                continue
            a = actions.setdefault(turn_idx, {"text": [], "tools": []})
            content = m.get("content")
            if isinstance(content, str):
                if content.strip():
                    a["text"].append(content.strip())
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        tx = b.get("text") or ""
                        if tx.strip():
                            a["text"].append(tx.strip())
                    elif bt == "tool_use":
                        a["tools"].append(
                            _tool_str(b.get("name") or "?", b.get("input")))
    return actions


# ---------------------------------------------------------------- grouping
def _events_by_turn(sess):
    """Merge every event landing on a turn. The sess.events feed
    (thrash/api_error/model_switch AND compaction/rebuild) is the primary
    source, but it is a capped deque — so the UNCAPPED compaction/rebuild
    ledgers backfill any that were evicted. A ledger entry is only added when
    the events feed has no event of that kind on that turn already, so a
    compaction that survives in both sources is reported once."""
    by = {}
    seen = set()               # (turn, kind, msg) exact dedup
    kinds = set()              # (turn, kind) presence, for ledger backfill

    def add(turn, kind, severity, msg):
        turn = int(turn)
        key = (turn, kind, msg)
        if key in seen:
            return
        seen.add(key)
        kinds.add((turn, kind))
        by.setdefault(turn, []).append(
            {"kind": kind, "severity": severity, "msg": msg})

    for ev in list(sess.events):
        add(ev.get("turn", 0), ev.get("kind") or "event",
            ev.get("severity") or "info", ev.get("msg") or "")
    for c in sess.compactions:
        turn = int(c.get("turn", 0))
        if (turn, "compaction") in kinds:
            continue           # already reported via the events feed
        add(turn, "compaction", "warn",
            "compaction #%d: %dk -> %dk (dropped %dk)"
            % (c.get("n", 0), int(c.get("pre", 0)) // 1000,
               int(c.get("post", 0)) // 1000, int(c.get("dropped", 0)) // 1000))
    for rb in sess.rebuilds:
        turn = int(rb.get("turn", 0))
        if (turn, "rebuild") in kinds:
            continue
        add(turn, "rebuild", "warn",
            "server context rebuild: %dk -> %dk (%dk reasoning flushed)"
            % (int(rb.get("pre", 0)) // 1000, int(rb.get("post", 0)) // 1000,
               int(rb.get("flushed", 0)) // 1000))
    return by


def build_turn_records(sess, path):
    """Return a list of per-turn dicts (index == turn), each capturing
    everything that happened that turn."""
    actions = _reparse_actions(path, budget=sess.budget)

    fa_by, cmd_by, ret_by, ag_by = {}, {}, {}, {}
    for fa in sess.faccess:
        fa_by.setdefault(fa["turn"], []).append(fa)
    for c in sess.cmds:
        cmd_by.setdefault(c["turn"], []).append(c)
    for r in sess.rets:
        ret_by.setdefault(r["turn"], []).append(r)
    for a in sess.agents.values():
        ag_by.setdefault(a.get("turn0", 0), []).append(a)
    ev_by = _events_by_turn(sess)

    recs = []
    prev_r = 0
    for i in range(len(sess.turns)):
        tp = sess.turn_payload(i)
        R = int(tp["resident"])
        act = actions.get(i, {"text": [], "tools": []})
        text = _one_line(" ".join(act["text"]), 200)

        files = []
        for fa in fa_by.get(i, []):
            f = sess.files.get(fa["file"])
            files.append({"path": f["path"] if f else str(fa["file"]),
                          "op": fa["op"], "tok": int(fa["tok"])})
        commands = [{"cmd": c["cmd"], "ok": bool(c["ok"]),
                     "interrupted": bool(c.get("interrupted")),
                     "bg": bool(c.get("bg")), "desc": c.get("desc")}
                    for c in cmd_by.get(i, [])]
        retrievals = [{"kind": r["kind"], "src": r["src"], "q": r["q"],
                       "n": r.get("n"), "tok": int(r["tok"]), "ok": bool(r["ok"])}
                      for r in ret_by.get(i, [])]
        agents = [{"agent_type": a.get("agent_type"), "desc": a.get("desc"),
                   "state": a.get("state"), "wf": a.get("wf"),
                   "own_tok": int(a.get("own_tok") or 0),
                   "turn1": a.get("turn1")}
                  for a in ag_by.get(i, [])]
        events = ev_by.get(i, [])

        recs.append({
            "turn": i,
            "ts": tp["ts"],
            "model": tp["model"],
            "stop": tp["stop"],
            "dur_ms": tp["dur_ms"],
            "resident": R,
            "dr": R - prev_r,
            "cost_u": tp["cost_u"],
            "hit_pct": round(tp["hit"] * 100.0, 1),
            "text": text,
            "tools": act["tools"],
            "files": files,
            "commands": commands,
            "retrievals": retrievals,
            "agents": agents,
            "events": events,
        })
        prev_r = R
    return recs


# ---------------------------------------------------------------- rendering
def render_turns_md(sess, recs):
    name = ce.session_name(sess.session_id)
    L = []
    L.append("# Per-turn timeline — %s" % name)
    L.append("")
    L.append("session `%s` · %d turns · one section per turn: what the "
             "assistant did, what it touched, and what happened."
             % (sess.session_id, len(recs)))
    L.append("")
    for r in recs:
        head = "### turn %d · %s · R %s (%s) · %s ku" % (
            r["turn"], r["ts"] or "?", _kfmt(r["resident"]),
            _skfmt(r["dr"]), r["cost_u"])
        L.append(head)
        meta = "model `%s`" % (r["model"] or "?")
        if r["stop"]:
            meta += " · stop `%s`" % r["stop"]
        if r["dur_ms"]:
            meta += " · %.1fs" % (r["dur_ms"] / 1000.0)
        meta += " · hit %.0f%%" % r["hit_pct"]
        L.append(meta)
        if r["text"]:
            L.append("> %s" % r["text"])
        if r["tools"]:
            L.append("- tools: %s" % ", ".join("`%s`" % t for t in r["tools"]))
        if r["files"]:
            parts = ["%s (%s%s)" % (os.path.basename(f["path"]) or f["path"],
                                    f["op"], "" if not f["tok"]
                                    else " %s" % _kfmt(f["tok"]))
                     for f in r["files"]]
            L.append("- files: %s" % ", ".join(parts))
        if r["commands"]:
            parts = []
            for c in r["commands"]:
                mark = "✓" if c["ok"] else ("⏹" if c["interrupted"]
                                                 else "✗")
                parts.append("`%s` %s" % (_one_line(c["cmd"], 60), mark))
            L.append("- commands: %s" % ", ".join(parts))
        if r["retrievals"]:
            parts = ["%s/%s %s" % (rr["kind"], rr["src"],
                                   _one_line(rr["q"], 50))
                     for rr in r["retrievals"]]
            L.append("- retrievals: %s" % ", ".join(parts))
        if r["agents"]:
            parts = []
            for a in r["agents"]:
                wf = " [%s]" % a["wf"] if a["wf"] else ""
                parts.append("%s%s (%s, %s tok) — %s" % (
                    a["agent_type"] or "agent", wf, a["state"] or "?",
                    _kfmt(a["own_tok"]),
                    _one_line(a["desc"] or "no description", 60)))
            L.append("- subagents: %s" % "; ".join(parts))
        if r["events"]:
            parts = ["**%s** %s" % (e["kind"], _one_line(e["msg"], 80))
                     for e in r["events"]]
            L.append("- events: %s" % "; ".join(parts))
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- write
def write_turns(sess, path, turnsdir, frames=None, recs=None):
    """Write turns/turns.jsonl and turns/turns.md (and turns/frames/turn-NNN.png
    when frames are supplied). Returns the number of turn records written.

    ``recs`` may be a pre-computed list from ``build_turn_records`` to avoid a
    second transcript re-parse when the caller already has them."""
    os.makedirs(turnsdir, exist_ok=True)
    if recs is None:
        recs = build_turn_records(sess, path)

    with open(os.path.join(turnsdir, "turns.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(turnsdir, "turns.md"), "w", encoding="utf-8") as fh:
        fh.write(render_turns_md(sess, recs))

    if frames:
        fdir = os.path.join(turnsdir, "frames")
        os.makedirs(fdir, exist_ok=True)
        for i, fr in enumerate(frames):
            try:
                fr.save(os.path.join(fdir, "turn-%03d.png" % i))
            except Exception as e:
                sys.stderr.write("frame %d save skipped: %s\n" % (i, e))
                break
    return len(recs)
