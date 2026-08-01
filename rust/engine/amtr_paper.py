#!/usr/bin/env python3
"""amtr_paper.py — the amtr COMPILED PDF REPORT builder.

Feeds a Claude Code session transcript through the amtr engine, renders the
four professional TUI-faithful figures (amtr_figures), computes the ranked
phase table and the characterize-style "algorithm" sections (amtr_phases),
emits a LaTeX document and compiles it to a PDF with `tectonic`. The animated
GIF figures are written next to the PDF; the PDF embeds each animation's final
static frame plus a keyframe montage strip (PDF cannot animate) and references
the .gif paths in the captions.

    amtr-paper [--session X | --project P] [--dir OUTDIR]

The output is a self-contained DIRECTORY (default
~/.claude/amtr-reports/<name>-<id8>/) housing report.pdf, report.md, a
figures/ dir (the 3 GIFs + static PNGs), and a turns/ dir (the per-turn
capture: turns.jsonl, turns.md, and per-turn map frames). Legacy --out
FILE.pdf still works: its parent directory becomes the report directory.

Everything is LOCAL. stdlib + matplotlib + Pillow + tectonic only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import amtr_engine as ce            # noqa: E402
import amtr_figures as cf          # noqa: E402
import amtr_phases as cp           # noqa: E402
import amtr_turns as ct            # noqa: E402


# ------------------------------------------------------------------ loading
def load_session(path, budget=None):
    """Single forward feed capturing a per-turn MAP snapshot, then enrich
    agents from the subagents dir (so the report reconciles with the
    divergence figure). Returns (sess, snapshots) where snapshots[t] =
    build_map_segs() at the end of turn t."""
    b = budget or ce.default_budget()
    sess = ce.Session(path, budget=b, budget_pinned=bool(budget))
    snapshots = []
    off = 0
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", "replace")
            s = line.strip()
            if s:
                try:
                    d = json.loads(s)
                except Exception:
                    d = None
                if isinstance(d, dict) and sess.turns and sess.is_new_turn(d):
                    snapshots.append(sess.build_map_segs())
                sess.feed_line(line, off)
            off += len(raw)
    snapshots.append(sess.build_map_segs())     # final turn
    # enrich agents (workflow children + own-token tails) via the engine
    try:
        ns = argparse.Namespace(session=None, project=None, budget=budget,
                                watch=False, json=False, idle_secs=60)
        eng = ce.Engine(ns)
        eng.session = sess
        eng._scan_agents(sess)
    except Exception as e:
        sys.stderr.write("agent scan skipped: %s\n" % e)
    # align snapshot count to turn count defensively
    n = len(sess.turns)
    if len(snapshots) > n:
        snapshots = snapshots[:n]
    while len(snapshots) < n:
        snapshots.append(sess.build_map_segs())
    return sess, snapshots


# ------------------------------------------------------------------ LaTeX
_UNI = {
    "→": r"$\rightarrow$", "←": r"$\leftarrow$", "≈": r"$\approx$",
    "≡": r"$\equiv$", "×": r"$\times$", "·": r"$\cdot$", "…": "...",
    "▼": r"$\triangledown$", "▲": r"$\triangle$", "✝": "[evicted]",
    "≤": r"$\leq$", "≥": r"$\geq$", "Σ": r"$\Sigma$", "α": r"$\alpha$",
    "▸": ">", "▾": "v", "◆": r"$\diamond$", "●": r"$\bullet$",
    "○": r"$\circ$", "✖": "x", "»": ">>", "«": "<<", "▮": "|",
    "“": '"', "”": '"', "’": "'", "‘": "'", "–": "-", "—": "---",
}
_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(s):
    """Escape ANY dynamic string for LaTeX: normalize known unicode, escape
    the special set, drop remaining non-latin1 so tectonic never chokes."""
    if s is None:
        return ""
    s = str(s)
    for u, r in _UNI.items():
        s = s.replace(u, r)
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 128:
            out.append(ch)
        elif ord(ch) < 256:
            out.append(ch)                  # latin1 ok under xelatex
        else:
            out.append("")                  # drop exotic glyphs
    return "".join(out)


def mono(s):
    return r"\texttt{%s}" % tex(s)


def _fk(n):
    n = int(n or 0)
    return "{:,}".format(n)


PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=22mm]{geometry}
\usepackage{lmodern}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{graphicx}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{caption}
\usepackage{float}
\usepackage{needspace}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}

\captionsetup{font=small,labelfont=bf,labelsep=period,justification=justified}
\setlength{\parindent}{0pt}
\setlength{\parskip}{4pt}
\renewcommand{\arraystretch}{1.2}
\frenchspacing
"""


def _fig_block(L, static_path, montage_path, gif_path, caption, label):
    """A numbered academic figure float: the final static frame, an optional
    keyframe montage beneath it, and a numbered caption."""
    L.append(r"\begin{figure}[htbp]\centering")
    L.append(r"\includegraphics[width=\linewidth]{%s}" %
             static_path.replace("\\", "/"))
    if montage_path:
        L.append(r"\\[4pt]{\small\itshape Keyframes (animation sampled "
                 r"turn-by-turn, earliest to latest):}\\[2pt]")
        L.append(r"\includegraphics[width=\linewidth]{%s}" %
                 montage_path.replace("\\", "/"))
    L.append(r"\caption{%s}" % caption)
    L.append(r"\label{%s}" % label)
    L.append(r"\end{figure}")


def _turn_marks(r):
    """A compact raw-LaTeX suffix summarizing writes / commands / events on a
    turn. Glyphs are emitted as raw math ($\\checkmark$, $\\times$, ...) — they
    must NOT pass through tex(), which drops non-latin glyphs. Returns "" when
    the turn had no write/command/event activity."""
    bits = []
    nw = sum(1 for f in r["files"] if f["op"] in ("w", "e"))
    if nw:
        bits.append(r"\textbf{%dw}" % nw)          # files written / edited
    ok = sum(1 for c in r["commands"] if c["ok"] and not c["interrupted"])
    fail = sum(1 for c in r["commands"]
               if not c["ok"] and not c["interrupted"])
    intr = sum(1 for c in r["commands"] if c["interrupted"])
    ncmd = ok + fail + intr
    if ncmd:
        if ncmd <= 4:
            bits.append(ok * r"$\checkmark$" + intr * r"$\blacksquare$"
                        + fail * r"$\times$")
        else:
            parts = []
            if ok:
                parts.append(r"%d$\checkmark$" % ok)
            if fail:
                parts.append(r"%d$\times$" % fail)
            if intr:
                parts.append(r"%d$\blacksquare$" % intr)
            bits.append(r"\,".join(parts))
    ev = ""
    for e in r["events"]:
        k = e.get("kind")
        if k == "compaction":
            ev += r"$\triangledown$"
        elif k == "rebuild":
            ev += r"$\approx$"
        else:
            ev += r"\textbf{!}"
    if ev:
        bits.append(ev)
    if not bits:
        return ""
    return r" {\footnotesize[%s]}" % r"\;".join(bits)


def _turn_action_cell(r, maxlen=50):
    """The Action column for one turn: a condensed one-line summary of the
    assistant's tool calls with targets (falling back to its trimmed text when
    it used no tools), truncated BEFORE escaping, then a raw-LaTeX marks
    suffix. All free text routed through tex(); glyphs stay raw."""
    tools = r.get("tools") or []
    base = "; ".join(tools) if tools else (r.get("text") or "")
    base = ct._one_line(base, maxlen)
    body = tex(base) if base else r"\textit{---}"
    return body + _turn_marks(r)


def _turn_table(L, turns, t0, t1, maxlen=50):
    """Append a per-turn longtable for the turns in [t0, t1] (a phase's turn
    range). One row per turn: turn index, per-turn context growth Delta-R,
    structural cost, and a condensed action summary. Paginates via longtable."""
    sl = [r for r in turns if t0 <= r["turn"] <= t1]
    if not sl:
        return
    L.append(r"\par\needspace{6\baselineskip}")
    L.append(r"\begingroup\small")
    L.append(r"\begin{longtable}{@{}r r r "
             r">{\raggedright\arraybackslash}p{0.60\linewidth}@{}}")
    hdr = r"Turn & $\Delta$R & Cost & Action\\"
    L.append(r"\toprule " + hdr + r"\midrule\endfirsthead")
    L.append(r"\toprule " + hdr + r"\midrule\endhead")
    for r in sl:
        L.append(r"%d & $%s$ & %.1f & %s\\" % (
            r["turn"], _sk(r["dr"]), r["cost_u"],
            _turn_action_cell(r, maxlen)))
    L.append(r"\bottomrule")
    L.append(r"\end{longtable}\endgroup")


def _sk(n):
    """Signed compact token delta as LaTeX-math-safe text (e.g. +9.0k, -12.2k,
    0). Wrapped in $...$ by the caller so the sign renders as a math minus."""
    return ct._skfmt(n)


def build_tex(sess, rep, phases, secs, figs, gif_paths, turns=None):
    h = rep["header"]
    c = rep["context"]
    e = rep["economics"]
    a = rep["agents"]
    L = [PREAMBLE, r"\begin{document}"]

    # ---- title block (academic)
    name = h.get("name") or h["session_id"]
    subtitle = h.get("title") or "Claude Code Session Diagnostic"
    L.append(r"\begin{center}")
    L.append(r"{\LARGE\bfseries A Diagnostic Report on Claude Code "
             r"Session \emph{%s}}\\[6pt]" % tex(name))
    L.append(r"{\large %s}\\[10pt]" % tex(subtitle))
    meta = []
    meta.append(r"session \texttt{%s}" % tex(h["session_id"][:16]))
    if h["project"]:
        meta.append(r"project \texttt{%s}" %
                    tex(os.path.basename(h["project"].rstrip("/")) or
                        h["project"]))
    meta.append(r"model \texttt{%s}" % tex(" / ".join(h["models"]) or "?"))
    L.append(r"{\normalsize %s}\\[3pt]" % r" \quad\textbar\quad ".join(meta))
    L.append(r"{\small %d turns \quad\textbar\quad %s wall-clock \quad\textbar"
             r"\quad compiled by \texttt{amtr-paper}}" %
             (h["turns"], ce._fmt_span(h["duration_s"])))
    L.append(r"\end{center}")
    L.append(r"\vspace{2pt}\hrule\vspace{8pt}")

    # ---- abstract
    R, B = c["final_r"], c["budget"]
    frac = 100.0 * R / max(1, B)
    L.append(r"\begin{abstract}\noindent")
    L.append(tex("This report characterizes a single Claude Code session as a "
                 "diagnostic subject. The session ran %d assistant turns, "
                 "ending at a resident context of %s tokens (%.1f%% of a %s "
                 "budget) at a total structural cost of %s kilo-units. Its "
                 "work is decomposed into %d execution phases, spanning %d "
                 "tracked files and %d subagent spawns; the most expensive "
                 "phase alone accounted for %s ku. The sections below present "
                 "the authoritative context and cache economics, four figures "
                 "reconstructing the session's context evolution, file access, "
                 "and agent fan-out, a phase decomposition ranked by cost, and "
                 "a stage-by-stage map of the algorithm the session executed."
                 % (h["turns"], _fk(R), frac, _fk(B), e["cost_total"],
                    len(phases), rep["files"]["totals"]["files"], a["n"],
                    (cp.ranked(phases)[0]["cost_u"] if phases else 0))))
    L.append(r"\end{abstract}")

    # ---- 1 Session Summary (Table)
    L.append(r"\section{Session Summary}")
    L.append(tex("All quantities in this section are authoritative — read "
                 "directly from the transcript's API usage records, not "
                 "estimated. R denotes resident context (input + cache-read + "
                 "cache-create tokens of the newest turn); the cost unit (ku) "
                 "is the structural weighting ") +
             r"$\text{in}\cdot1 + \text{cr}\cdot0.1 + \text{cc}_{5m}\cdot1.25 "
             r"+ \text{cc}_{1h}\cdot2 + \text{out}\cdot5$, in kilo-units.")
    L.append(r"\begin{table}[htbp]\centering\small")
    L.append(r"\caption{Authoritative context and cache economics for the "
             r"session.}\label{tab:summary}")
    L.append(r"\begin{tabular}{@{}l r @{\qquad} l r@{}}")
    L.append(r"\toprule")
    L.append(r"\multicolumn{2}{c}{\textbf{Context}} & "
             r"\multicolumn{2}{c}{\textbf{Economics}}\\")
    L.append(r"\cmidrule(r){1-2}\cmidrule(l){3-4}")
    L.append(r"final R & %s (%.1f\%%) & $\Sigma$ input & %s\\" %
             (_fk(R), frac, _fk(e["in"])))
    L.append(r"peak R & %s (t%d) & $\Sigma$ cache-read & %s\\" %
             (_fk(c["peak_r"]), c["peak_turn"], _fk(e["cache_read"])))
    L.append(r"budget & %s & $\Sigma$ cache-create & %s\\" %
             (_fk(B), _fk(e["cc_5m"] + e["cc_1h"])))
    L.append(r"waterline & %s & $\Sigma$ output & %s\\" %
             (_fk(c["waterline"]), _fk(e["out"])))
    L.append(r"overhead & %s & cache hit rate & %.1f\%%\\" %
             (_fk(c["overhead"]), 100.0 * e["hit"]))
    L.append(r"compactions & %d & total cost & %s ku\\" %
             (len(c["compactions"]), e["cost_total"]))
    L.append(r"server rebuilds & %d & cost/turn (mean) & %s ku\\" %
             (len(c["rebuilds"]), e["cost_mean"]))
    L.append(r"$\alpha$ calibration & %.3f & cost/turn (p95) & %s ku\\" %
             (c["alpha"], e["cost_p95"]))
    L.append(r"\bottomrule")
    L.append(r"\end{tabular}")
    L.append(r"\end{table}")
    # composition sentence
    comp = [(k, v) for k, v in c["cats"].items() if v]
    comp.sort(key=lambda kv: -kv[1])
    L.append(r"\noindent The final context composition, by category, was: %s." %
             (", ".join("%s (%s)" % (tex(k), ce._fc(v)) for k, v in comp)))

    # ---- 2 Resident Trend (EKG)
    L.append(r"\section{Resident Context Trend}")
    L.append(tex("Figure ") + r"\ref{fig:ekg} " +
             tex("traces resident context R across every turn against the "
                 "budget ceiling, with the 60 and 85 percent pressure zones "
                 "marked. The lower lanes give per-turn output tokens and "
                 "per-turn cost."))
    _fig_block(L, figs["ekg"], None, None,
               tex("Resident context R over %d turns against the %s budget, "
                   "with 60/85%% pressure-zone bands and the cache waterline. "
                   "Lower lanes: output tokens per turn (0--16k) and cost per "
                   "turn (0--100 ku). Markers: " % (len(sess.turns), _fk(B))) +
               r"$\triangledown$ " + tex("compaction, ") + r"$\approx$ " +
               tex("server rebuild."), "fig:ekg")

    # ---- 3 Context-Space Map
    L.append(r"\section{Context-Space Map}")
    L.append(tex("Figure ") + r"\ref{fig:map} " +
             tex("lays the resident context out as an address space: a "
                 "fixed-scale grid in which each cell is a fixed number of "
                 "tokens, coloured by the category occupying it. The animation "
                 "(written to disk, referenced in the caption) replays this "
                 "map growing turn by turn."))
    _fig_block(L, figs["map_static"], figs.get("map_montage"),
               gif_paths.get("map"),
               tex("Context-space map. Resident tokens laid out as a "
                   "fixed-scale, row-major grid coloured by category (files "
                   "carry distinct accent hues); light gray is free headroom "
                   "to the budget. Animation (one frame per turn): ") +
               mono(gif_paths.get("map", "")) + ".", "fig:map")

    # ---- 4 File Access
    L.append(r"\section{File Access}")
    L.append(tex("Figure ") + r"\ref{fig:files} " +
             tex("renders file activity as a traffic roll: one row per file, "
                 "one column per turn, each access marked by operation and "
                 "shaded by size."))
    _fig_block(L, figs["files_static"], figs.get("files_montage"),
               gif_paths.get("files"),
               tex("File traffic roll. Rows are files (labelled in their "
                   "accent hue), columns are turns; each cell marks a read, "
                   "write, or edit with brightness proportional to access "
                   "size. Animation: ") + mono(gif_paths.get("files", "")) +
               ".", "fig:files")

    # ---- 5 Subagent Divergence
    L.append(r"\section{Subagent Divergence}")
    L.append(tex("This section views the session's delegation from two angles. "
                 "Figure ") + r"\ref{fig:div} " +
             tex("depicts delegation structure as a branch graph: the main "
                 "session is a horizontal spine, and each subagent spawn "
                 "diverges from it at its launch turn and rejoins at "
                 "completion. Figure ") + r"\ref{fig:agents} " +
             tex("recasts the same subagents as a fan-out timeline mirroring "
                 "the live monitor's AGENTS view: a concurrency strip over the "
                 "turn axis above a per-agent Gantt annotated with each agent's "
                 "own and returned tokens, amplification, and duration."))
    _fig_block(L, figs["divergence_static"], figs.get("divergence_montage"),
               gif_paths.get("divergence"),
               tex("Subagent branch tree. The main session is the trunk; each "
                   "subagent forks at its launch turn into its own lane and "
                   "merges back at completion, coloured by terminal state "
                   "(running/done/failed) and labelled by agent type. Branch "
                   "length is scaled to the agent's own work (tokens); the merge "
                   "node is annotated with the tokens it returned to the parent. "
                   "Animation: ") +
               mono(gif_paths.get("divergence", "")) + ".", "fig:div")
    _fig_block(L, figs["agents_timeline"], None, None,
               tex("Agent fan-out timeline (mirroring the monitor's AGENTS "
                   "tab). Top: the fan-out strip counts subagents concurrently "
                   "active at each turn. Bottom: a per-agent Gantt, one row per "
                   "subagent sorted by launch turn, each bar spanning its "
                   "launch-to-return turn range and coloured by terminal state "
                   "(running/done/failed); a dot marks the launch, a diamond "
                   "(or open arrow, if still running) the return. The right "
                   "gutter reproduces the ledger columns: a log-scaled own(log) "
                   "bar, own tokens, tokens returned to the parent, the "
                   "amplification ratio (own/returned), and wall-clock "
                   "duration, followed by the agent type and its task "
                   "description."), "fig:agents")

    # ---- 6 Phase Decomposition (ranked table)
    L.append(r"\section{Phase Decomposition}")
    L.append(tex("The session is segmented into contiguous phases at "
                 "structural boundaries — server rebuilds, subagent launches, "
                 "long idle gaps, and model switches — and each phase is "
                 "classified by its dominant activity. Table ") +
             r"\ref{tab:phases} " +
             tex("ranks the phases by cost, most expensive first. Here ") +
             r"$\Delta$R " +
             tex("is the change in resident context across the phase."))
    L.append(r"\begin{center}\small")
    L.append(r"\begin{longtable}{@{}r l r r r r r r r@{}}")
    L.append(r"\caption{Session phases ranked by structural cost.}"
             r"\label{tab:phases}\\")
    L.append(r"\toprule")
    L.append(r"\# & Phase & Turns & Cost (ku) & \% & Output & Files & Agents "
             r"& $\Delta$R\\")
    L.append(r"\midrule\endfirsthead")
    L.append(r"\toprule \# & Phase & Turns & Cost (ku) & \% & Output & Files & "
             r"Agents & $\Delta$R\\ \midrule\endhead")
    total_cost = sum(p["cost_u"] for p in phases) or 1.0
    for p in cp.ranked(phases):
        L.append(r"%d & %s \textit{\footnotesize (t%d--%d)} & %d & %s & "
                 r"%.0f & %s & %d & %d & $%+d$k\\" % (
                     p["idx"], tex(p["label"].title()), p["t0"], p["t1"],
                     p["turns"], p["cost_u"], 100 * p["cost_u"] / total_cost,
                     _fk(p["out_tok"]), p["n_files"], p["n_agents"],
                     p["dr"] // 1000))
    L.append(r"\bottomrule")
    L.append(r"\end{longtable}\end{center}")

    # ---- 7 Stage Map (characterize)
    L.append(r"\section{The Session Algorithm: A Stage Map}")
    L.append(tex("Following the characterize method — mapping a system into "
                 "stages parsed from ground truth — this section reads the "
                 "phases in execution order and describes, for each, the "
                 "algorithm it carried out: its role, the files it read and "
                 "wrote, the subagents it delegated to, and its cost."))
    if turns:
        L.append(tex("Each stage closes with a turn-by-turn table of its "
                     "constituent turns: the per-turn context growth ") +
                 r"$\Delta$R" +
                 tex(", the structural cost in kilo-units, and a condensed "
                     "\"action\" — the assistant's tool calls with their "
                     "targets (or its text when it used no tools). A trailing "
                     "bracket flags files written or edited (") +
                 r"\textbf{n}\textbf{w}" +
                 tex("), shell commands run (") + r"$\checkmark$" +
                 tex(" ok, ") + r"$\times$" + tex(" failed), and events (") +
                 r"$\triangledown$" + tex(" compaction, ") + r"$\approx$" +
                 tex(" rebuild, ") + r"\textbf{!}" + tex(" error).") + r"\par")
    for s in secs:
        L.append(r"\needspace{5\baselineskip}")
        L.append(r"\subsection{Stage %d --- %s}" % (s["idx"],
                 tex(s["label"].title())))
        L.append(r"\noindent\textit{Turns %d--%d \textbullet\ %s ku "
                 r"(%.0f\%% of session cost).}\par" %
                 (s["t0"], s["t1"], s["cost_u"], s["cost_pct"]))
        cap = s["caption"]
        cap = cap[0].upper() + cap[1:] if cap else cap
        L.append(r"%s." % tex(cap))
        if s["reads"]:
            paths = ", ".join(mono(cp._short(os.path.basename(p) or p, 40))
                              for p in s["reads"][:8])
            more = "" if s["n_files"] <= 8 else (r" \textit{(and %d more)}" %
                                                 (s["n_files"] - 8))
            L.append(r"\par\noindent\textit{Files touched:} %s%s." %
                     (paths, more))
        if s["agents"]:
            L.append(r"\par\noindent\textit{Subagents spawned:}")
            L.append(r"\begin{itemize}[leftmargin=1.4em,itemsep=1pt,topsep=2pt]")
            for ag in s["agents"][:6]:
                wf = (r" \texttt{[%s]}" % tex(ag["wf"])) if ag["wf"] else ""
                L.append(r"\item \textbf{%s}%s (%s) --- %s "
                         r"\textit{(own %s tok)}" %
                         (tex(ag["type"]), wf, tex(ag["state"]),
                          tex(ag["desc"] or "no description"),
                          _fk(ag["own_tok"])))
            L.append(r"\end{itemize}")
        if turns:
            _turn_table(L, turns, s["t0"], s["t1"])

    # ---- 8 Diagnostics
    if rep["diagnostics"]:
        L.append(r"\section{Diagnostics}")
        L.append(tex("Automated findings computed from the session data:"))
        L.append(r"\begin{itemize}[leftmargin=1.4em,itemsep=2pt,topsep=2pt]")
        for d in rep["diagnostics"]:
            L.append(r"\item %s" % tex(d))
        L.append(r"\end{itemize}")

    L.append(r"\end{document}")
    return "\n".join(L)


# ------------------------------------------------------------------ build
def default_report_dir(sess):
    """The canonical self-contained report directory for a session:
    ~/.claude/amtr-reports/<name>-<id8>/."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", ce.session_name(sess.session_id))
    return os.path.join(ce.CLAUDE_DIR, "amtr-reports",
                        "%s-%s" % (name, sess.session_id[:8]))


def build(session_arg=None, project_arg=None, out=None, outdir_arg=None,
          budget=None):
    # resolve transcript path
    ns = argparse.Namespace(session=session_arg, project=project_arg,
                            budget=budget, watch=False, json=False,
                            idle_secs=60)
    eng = ce.Engine(ns)
    path = eng.pick_default()
    if not path or not os.path.isfile(path):
        sys.stderr.write("no session transcript found (try --session PATH)\n")
        return None
    sys.stderr.write("feeding %s ...\n" % path)
    sess, snapshots = load_session(path, budget=budget)
    if not sess.turns:
        sys.stderr.write("session has no turns; nothing to report\n")
        return None
    rep = ce.build_report(sess)
    phases = cp.detect_phases(sess)
    secs = cp.algorithm_sections(sess, phases)
    turn_recs = ct.build_turn_records(sess, path)

    # ---- output layout: a self-contained DIRECTORY housing all material.
    #   <outdir>/report.pdf  report.md  figures/  turns/
    # --dir wins; else legacy --out FILE.pdf keeps that exact PDF path (its
    # dir becomes the container); else the canonical ~/.claude/amtr-reports
    # directory named after the session.
    if outdir_arg:
        outdir = os.path.abspath(outdir_arg)
        pdf = os.path.join(outdir, "report.pdf")
    elif out:
        pdf = os.path.abspath(out)
        outdir = os.path.dirname(pdf) or os.getcwd()
    else:
        outdir = default_report_dir(sess)
        pdf = os.path.join(outdir, "report.pdf")
    figdir = os.path.join(outdir, "figures")
    turnsdir = os.path.join(outdir, "turns")
    for d in (outdir, figdir, turnsdir):
        os.makedirs(d, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="amtr-paper-")
    figs = {}
    gif_paths = {}
    sys.stderr.write("rendering figures ...\n")

    # Statics render once (not per-turn): embed the crisp VECTOR pdf in the
    # paper, and also emit the raster png into figures/ for the directory.
    # EKG
    figs["ekg"] = cf.ekg_static(sess, os.path.join(tmp, "ekg.pdf"))
    cf.ekg_static(sess, os.path.join(figdir, "ekg.png"))

    # MAP: static + animation GIF + keyframe montage
    figs["map_static"] = cf.map_static(sess, os.path.join(tmp, "map.pdf"))
    cf.map_static(sess, os.path.join(figdir, "map.png"))
    map_frames = cf.map_animation_frames(sess, snapshots)
    gif_paths["map"] = os.path.join(figdir, "map.gif")
    cf._write_gif(map_frames, gif_paths["map"])
    figs["map_montage"] = cf.montage(map_frames,
                                     os.path.join(tmp, "map_montage.png"))

    # FILES
    figs["files_static"] = cf.files_static(sess, os.path.join(tmp, "files.pdf"))
    cf.files_static(sess, os.path.join(figdir, "files.png"))
    file_frames = cf.files_animation_frames(sess)
    gif_paths["files"] = os.path.join(figdir, "files.gif")
    cf._write_gif(file_frames, gif_paths["files"])
    figs["files_montage"] = cf.montage(file_frames,
                                       os.path.join(tmp, "files_montage.png"))

    # DIVERGENCE
    figs["divergence_static"] = cf.divergence_static(
        sess, os.path.join(tmp, "divergence.pdf"))
    cf.divergence_static(sess, os.path.join(figdir, "divergence.png"))
    div_frames = cf.divergence_animation_frames(sess)
    gif_paths["divergence"] = os.path.join(figdir, "divergence.gif")
    cf._write_gif(div_frames, gif_paths["divergence"])
    figs["divergence_montage"] = cf.montage(
        div_frames, os.path.join(tmp, "divergence_montage.png"))

    # AGENTS TIMELINE — static only (the branch tree carries the animated view)
    figs["agents_timeline"] = cf.agents_timeline_static(
        sess, os.path.join(tmp, "agents_timeline.pdf"))
    cf.agents_timeline_static(sess, os.path.join(figdir, "agents_timeline.png"))

    # ---- markdown report (instant, human-readable companion to the PDF)
    md_path = os.path.join(outdir, "report.md")
    try:
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(ce.render_report_md(rep))
    except Exception as e:
        sys.stderr.write("report.md skipped: %s\n" % e)
        md_path = None

    # ---- per-turn capture (turns.jsonl + turns.md + per-turn map frames)
    sys.stderr.write("writing per-turn capture ...\n")
    n_turns = 0
    try:
        n_turns = ct.write_turns(sess, path, turnsdir, frames=map_frames,
                                 recs=turn_recs)
    except Exception as e:
        sys.stderr.write("per-turn capture skipped: %s\n" % e)

    # ---- LaTeX
    sys.stderr.write("compiling LaTeX ...\n")
    base = "report"
    texsrc = build_tex(sess, rep, phases, secs, figs, gif_paths,
                       turns=turn_recs)
    texpath = os.path.join(tmp, "%s.tex" % base)
    with open(texpath, "w", encoding="utf-8") as fh:
        fh.write(texsrc)

    tectonic = shutil.which("tectonic")
    if not tectonic:
        sys.stderr.write("tectonic not found on PATH\n")
        return None
    r = subprocess.run(
        [tectonic, "--outdir", tmp, "--keep-logs", "--chatter", "minimal",
         texpath],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("tectonic FAILED (exit %d):\n%s\n%s\n"
                         % (r.returncode, r.stdout[-3000:], r.stderr[-3000:]))
        sys.stderr.write("tex kept at %s\n" % texpath)
        return None
    pdf_tmp = os.path.join(tmp, "%s.pdf" % base)
    if not os.path.isfile(pdf_tmp):
        sys.stderr.write("tectonic exited 0 but no PDF produced\n")
        return None
    shutil.copyfile(pdf_tmp, pdf)
    return {"dir": outdir, "pdf": pdf, "md": md_path, "gifs": gif_paths,
            "turns": len(sess.turns), "turn_records": n_turns,
            "phases": len(phases), "agents": len(sess.agents)}


def main():
    ap = argparse.ArgumentParser(
        description="amtr compiled PDF report builder")
    ap.add_argument("--session", help="transcript path or session id")
    ap.add_argument("--project", help="project dir (newest session under it)")
    ap.add_argument("--dir", dest="outdir",
                    help="output DIRECTORY (default ~/.claude/amtr-reports/"
                         "<name>-<id8>/); houses report.pdf, report.md, "
                         "figures/, turns/")
    ap.add_argument("--out", help="legacy: exact output PDF path (its parent "
                                  "dir becomes the report directory)")
    ap.add_argument("--budget", type=int, help="pin the context budget")
    args = ap.parse_args()
    t0 = time.time()
    res = build(session_arg=args.session, project_arg=args.project,
                out=args.out, outdir_arg=args.outdir, budget=args.budget)
    if not res:
        return 1
    print("REPORT DIR: %s" % res["dir"])
    print("  report.pdf, report.md, figures/, turns/turns.{jsonl,md}")
    print("PDF:  %s" % res["pdf"])
    if res.get("md"):
        print("MD:   %s" % res["md"])
    for k in ("map", "files", "divergence"):
        if k in res["gifs"]:
            print("GIF:  %s" % res["gifs"][k])
    print("(%d turns, %d turn records, %d phases, %d agents, %.1fs)"
          % (res["turns"], res["turn_records"], res["phases"], res["agents"],
             time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
