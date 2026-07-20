#!/usr/bin/env python3
"""amtr_phases.py — segment a fed amtr_engine.Session into workflow PHASES and
rank them by token expensiveness, and derive characterize-style "algorithm"
sections that map out WHAT THE SESSION'S ALGORITHM WAS (its phases, what each
did, its agent fan-outs and file/command activity) from ground truth.

Applies the `characterize` method (parse-never-draw, ground-truth-first,
stages read in execution order) to the SESSION rather than a repo: the
"system" is the session's algorithm; the "stages" are its phases.

Stdlib only. Public API:
    detect_phases(sess) -> list[Phase dict]  (chronological)
    ranked(phases)      -> same list sorted by cost desc
    algorithm_sections(sess, phases) -> list[Section dict] (chronological)
"""
import math


def _i(v):
    try:
        return int(v)
    except Exception:
        return 0


# activity → a short human role label (characterize "verb-phrase" stage names)
ROLE_LABEL = {
    "agent":     "AGENT FAN-OUT",
    "file":      "FILE-HEAVY EDITING",
    "bash":      "SHELL / BUILD",
    "tool":      "TOOL / RETRIEVAL",
    "reasoning": "DEEP REASONING",
    "user":      "USER STEERING",
    "mixed":     "MIXED WORK",
}


def _turn_activity(sess):
    """Per-turn activity buckets derived from ground truth: file ops, bash
    cmds, retrievals, agents launched, and the turn's category deltas."""
    n = len(sess.turns)
    fa_r = [0] * n           # file reads
    fa_w = [0] * n           # file writes/edits
    fa_tok = [0] * n
    cmd = [0] * n
    ret = [0] * n
    ag_launch = [0] * n
    files_by_turn = [set() for _ in range(n)]
    for fa in sess.faccess:
        t = fa["turn"]
        if 0 <= t < n:
            if fa["op"] == "r":
                fa_r[t] += 1
            else:
                fa_w[t] += 1
            fa_tok[t] += _i(fa["tok"])
            files_by_turn[t].add(fa["file"])
    for c in sess.cmds:
        t = c["turn"]
        if 0 <= t < n:
            cmd[t] += 1
    for r in sess.rets:
        t = r["turn"]
        if 0 <= t < n:
            ret[t] += 1
    for a in sess.agents.values():
        t = a.get("turn0", 0) or 0
        if 0 <= t < n:
            ag_launch[t] += 1
    return {"fa_r": fa_r, "fa_w": fa_w, "fa_tok": fa_tok, "cmd": cmd,
            "ret": ret, "ag_launch": ag_launch, "files": files_by_turn}


def _boundaries(sess, act):
    """Structural change-points (characterize: parse phases, never guess):
    session start, compactions, server rebuilds, agent-launch bursts, large
    idle gaps between turn epochs, and model switches."""
    n = len(sess.turns)
    bset = {0, n}
    for c in sess.compactions:
        if 0 <= c["turn"] < n:
            bset.add(c["turn"])
    for rb in sess.rebuilds:
        if 0 <= rb["turn"] < n:
            bset.add(rb["turn"])
    for t in range(n):
        if act["ag_launch"][t] > 0:
            bset.add(t)
    # idle gaps: a wall-clock gap >> the local median marks a topic shift
    eps = sess.turn_epochs
    if len(eps) == n and n > 4:
        gaps = [eps[i] - eps[i - 1] for i in range(1, n)
                if eps[i] and eps[i - 1]]
        if gaps:
            gaps_sorted = sorted(gaps)
            med = gaps_sorted[len(gaps_sorted) // 2] or 1.0
            thr = max(med * 6.0, 180.0)
            for i in range(1, n):
                if eps[i] and eps[i - 1] and eps[i] - eps[i - 1] > thr:
                    bset.add(i)
    # model switches
    prev = None
    for t in sess.turns:
        m = t["model"] or "?"
        if prev is not None and m != prev:
            bset.add(t["turn"])
        prev = m
    return sorted(b for b in bset if 0 <= b <= n)


def _classify(sess, act, a, b):
    """Dominant activity of the contiguous turn range [a,b)."""
    # structural rule (deliverable 2: phases "dominated by a workflow"): a
    # range that launched a workflow or a burst of >=2 subagents IS an agent
    # fan-out, regardless of the file/shell volume around it.
    wf_ids = {ag.get("wf") for ag in sess.agents.values()
              if a <= (ag.get("turn0", 0) or 0) < b and ag.get("wf")}
    n_launch = sum(act["ag_launch"][a:b])
    if wf_ids or n_launch >= 2:
        return "agent"
    r = sum(act["fa_r"][a:b])
    w = sum(act["fa_w"][a:b])
    cmd = sum(act["cmd"][a:b])
    ret = sum(act["ret"][a:b])
    agl = sum(act["ag_launch"][a:b])
    # per-range category mass from the turns' out (reasoning proxy)
    out = sum(_i(sess.turns[t]["out"]) for t in range(a, b))
    scores = {
        "agent": agl * 3.0,
        "file": (r + w * 1.5),
        "bash": cmd * 1.2,
        "tool": ret * 1.5,
        "reasoning": out / 4000.0,
    }
    best = max(scores, key=scores.get)
    if scores[best] <= 0.01:
        return "mixed"
    # if two are close, call it mixed unless one clearly dominates
    ranked = sorted(scores.values(), reverse=True)
    if len(ranked) > 1 and ranked[1] > 0.66 * ranked[0] and ranked[0] > 0:
        # still prefer agent/bash/file signals over reasoning ties
        if best == "reasoning":
            return "mixed"
    return best


def _phase_metrics(sess, act, a, b, kind):
    cost = round(sum(sess.turn_payload(t)["cost_u"]
                     for t in range(a, b)), 1)
    out = sum(_i(sess.turns[t]["out"]) for t in range(a, b))
    files = set()
    for t in range(a, b):
        files |= act["files"][t]
    agents = [ag for ag in sess.agents.values()
              if a <= (ag.get("turn0", 0) or 0) < b]
    cmds = sum(act["cmd"][a:b])
    rets = sum(act["ret"][a:b])
    reads = sum(act["fa_r"][a:b])
    writes = sum(act["fa_w"][a:b])
    eps = sess.turn_epochs
    dur = 0.0
    if len(eps) == len(sess.turns) and b - 1 < len(eps) and eps[b - 1] and eps[a]:
        dur = max(0.0, eps[b - 1] - eps[a])
    r0 = sess.turns[a]["resident"]
    r1 = sess.turns[b - 1]["resident"]
    return {
        "t0": a, "t1": b - 1, "turns": b - a, "kind": kind,
        "label": ROLE_LABEL.get(kind, kind.upper()),
        "cost_u": cost, "out_tok": out, "files": sorted(files),
        "n_files": len(files), "reads": reads, "writes": writes,
        "agents": agents, "n_agents": len(agents), "cmds": cmds, "rets": rets,
        "dur_s": round(dur, 1), "r_in": r0, "r_out": r1,
        "dr": r1 - r0,
    }


def detect_phases(sess, min_turns=2, max_phases=12):
    """Contiguous phases from structural boundaries, tiny ones merged into the
    neighbour they most resemble, capped at max_phases."""
    n = len(sess.turns)
    if n == 0:
        return []
    act = _turn_activity(sess)
    bnds = _boundaries(sess, act)
    # raw contiguous ranges
    ranges = [(bnds[i], bnds[i + 1]) for i in range(len(bnds) - 1)
              if bnds[i + 1] > bnds[i]]
    if not ranges:
        ranges = [(0, n)]
    phases = []
    for a, b in ranges:
        kind = _classify(sess, act, a, b)
        phases.append(_phase_metrics(sess, act, a, b, kind))
    # merge tiny phases into the adjacent phase of the same kind, else the
    # cheaper neighbour (keeps the expensive stages legible)
    changed = True
    while changed and len(phases) > 1:
        changed = False
        for i, p in enumerate(phases):
            if p["turns"] >= min_turns:
                continue
            # choose merge target
            left = phases[i - 1] if i > 0 else None
            right = phases[i + 1] if i < len(phases) - 1 else None
            tgt = None
            if left and right:
                tgt = left if left["kind"] == p["kind"] else \
                    (right if right["kind"] == p["kind"] else
                     (left if left["cost_u"] <= right["cost_u"] else right))
            else:
                tgt = left or right
            if tgt is None:
                continue
            j = phases.index(tgt)
            a = min(p["t0"], tgt["t0"])
            b = max(p["t1"], tgt["t1"]) + 1
            kind = _classify(sess, act, a, b)
            merged = _phase_metrics(sess, act, a, b, kind)
            lo, hi = min(i, j), max(i, j)
            phases[lo:hi + 1] = [merged]
            changed = True
            break
    # cap: repeatedly merge the cheapest adjacent pair
    while len(phases) > max_phases:
        # find cheapest phase, merge into cheaper neighbour
        idx = min(range(len(phases)), key=lambda k: phases[k]["cost_u"])
        left = phases[idx - 1] if idx > 0 else None
        right = phases[idx + 1] if idx < len(phases) - 1 else None
        tgt = left if (right is None or (left and left["cost_u"] <=
                                         right["cost_u"])) else right
        j = phases.index(tgt)
        a = min(phases[idx]["t0"], tgt["t0"])
        b = max(phases[idx]["t1"], tgt["t1"]) + 1
        kind = _classify(sess, act, a, b)
        merged = _phase_metrics(sess, act, a, b, kind)
        lo, hi = min(idx, j), max(idx, j)
        phases[lo:hi + 1] = [merged]
    for i, p in enumerate(phases):
        p["idx"] = i + 1
    return phases


def ranked(phases):
    return sorted(phases, key=lambda p: -p["cost_u"])


# ------------------------------------------------------------------ sections
def _short(path, n=48):
    return path if len(path) <= n else "…" + path[-(n - 1):]


def algorithm_sections(sess, phases):
    """Characterize-style stages, in EXECUTION ORDER — each phase mapped to
    what it did (its role, inputs = files read, outputs = files written,
    fan-outs = agents, and its cost), derived entirely from ground truth."""
    secs = []
    total_cost = sum(p["cost_u"] for p in phases) or 1.0
    id2path = {fid: f["path"] for fid, f in sess.files.items()}
    for p in phases:
        reads = [id2path.get(fid, str(fid)) for fid in p["files"]]
        agents = p["agents"]
        ag_desc = []
        for a in sorted(agents, key=lambda a: -_i(a.get("own_tok"))):
            ag_desc.append({
                "type": a.get("agent_type") or "agent",
                "desc": a.get("desc") or "",
                "state": a.get("state"),
                "own_tok": _i(a.get("own_tok")),
                "wf": a.get("wf"),
            })
        # a one-line role caption, characterize-style
        cap = _caption(p)
        secs.append({
            "idx": p["idx"], "label": p["label"], "kind": p["kind"],
            "t0": p["t0"], "t1": p["t1"], "turns": p["turns"],
            "cost_u": p["cost_u"], "cost_pct": round(100 * p["cost_u"]
                                                     / total_cost, 1),
            "out_tok": p["out_tok"], "dur_s": p["dur_s"],
            "reads": reads[:12], "n_files": p["n_files"],
            "n_reads": p["reads"], "n_writes": p["writes"],
            "cmds": p["cmds"], "rets": p["rets"],
            "agents": ag_desc, "dr": p["dr"],
            "caption": cap,
        })
    return secs


def _caption(p):
    bits = []
    if p["n_agents"]:
        bits.append("spawned %d subagent%s" % (p["n_agents"],
                    "" if p["n_agents"] == 1 else "s"))
    if p["writes"]:
        bits.append("%d write%s" % (p["writes"],
                    "" if p["writes"] == 1 else "s"))
    if p["reads"]:
        bits.append("%d read%s" % (p["reads"],
                    "" if p["reads"] == 1 else "s"))
    if p["cmds"]:
        bits.append("%d shell cmd%s" % (p["cmds"],
                    "" if p["cmds"] == 1 else "s"))
    if p["rets"]:
        bits.append("%d retrieval%s" % (p["rets"],
                    "" if p["rets"] == 1 else "s"))
    drk = p["dr"]
    grow = ("grew context +%dk" % (drk // 1000)) if drk >= 1000 else \
        ("released %dk" % (-drk // 1000) if drk <= -1000 else "flat context")
    role = {
        "agent": "delegated work to subagents",
        "file": "read and edited source files",
        "bash": "ran the build / shell",
        "tool": "pulled external context",
        "reasoning": "did extended reasoning",
        "user": "took user steering",
        "mixed": "mixed activity",
    }.get(p["kind"], "worked")
    tail = ", ".join(bits) if bits else "no tool activity"
    return "%s; %s; %s over %d turn%s" % (role, tail, grow, p["turns"],
                                          "" if p["turns"] == 1 else "s")
