#!/usr/bin/env python3
"""amtr_engine.py — data engine for amtr v2 (SPEC.md sections a-d, f).

Owns ALL data: session discovery, transcript tailing, token accounting.
Speaks JSON-lines on fd 1 (Update messages), reads Control messages on stdin.
Standalone modes (--validate, --report [--json] [--watch]) print human/JSON
output to the real stdout instead. Python >= 3.9, stdlib only.
"""
import os, sys

# fd-1 hijack FIRST (split-process discipline): fd 1 is reserved for protocol;
# any stray print or C-level chatter lands on stderr instead.
_PROTO_FD = os.dup(1)
os.dup2(2, 1)
sys.stdout = sys.stderr
_PROTO = os.fdopen(_PROTO_FD, "w", buffering=1, encoding="utf-8")

import json, time, math, copy, glob, re, argparse, threading, zlib, subprocess, signal, hashlib
from collections import deque
from datetime import datetime, timezone

ENGINE_VERSION = "0.1.6"
_PROTO_LOCK = threading.Lock()
_STANDALONE = False   # --validate/--report: fd 1 is the report, log() -> stderr

def _use_real_stdout():
    """Standalone modes (--validate/--report) speak to the HUMAN on fd 1: the
    hijack above already preserved the real stdout as _PROTO, so print() is
    rebound to it and log() falls back to stderr. Protocol modes never call
    this — their fd 1 stays reserved for Update messages."""
    global _STANDALONE
    _STANDALONE = True
    sys.stdout = _PROTO

def send(obj):
    try:
        with _PROTO_LOCK:
            _PROTO.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")
            _PROTO.flush()
    except (BrokenPipeError, ValueError):
        pass

def log(msg):
    if _STANDALONE:
        sys.stderr.write(str(msg) + "\n")
        return
    send({"type": "log", "msg": str(msg)})

# ---------------------------------------------------------------- estimator
IMG_TOK = 1200
MAP_CAP = 1024               # max segs in a full map == UI ring budget (SPEC b)
BUDGET_RUNGS = (200_000, 1_000_000)
CATS = ("overhead", "user", "assistant", "thinking", "reasoning", "file",
        "bash", "tool", "attach", "summary")

class Est:
    chars_per_tok = 3.8

def est_text(s):
    if not s:
        return 0
    return int(math.ceil(len(s) / Est.chars_per_tok))

def est_pair(o):
    """(tokens, chars) for an object under the estimator's rules. `tokens` is
    exactly what est_obj() returns; `chars` is the RAW character weight behind
    it — the design matrix the per-category fit (`fit_cats`) regresses against
    R. Token-priced blocks with no text (images) contribute their prior-implied
    chars, so a fit regularized toward the prior leaves them where they are."""
    if o is None:
        return 0, 0.0
    if isinstance(o, str):
        return est_text(o), float(len(o))
    if isinstance(o, list):
        t, c = 0, 0.0
        for b in o:
            bt, bc = est_pair(b)
            t += bt
            c += bc
        return t, c
    if isinstance(o, dict):
        if o.get("type") == "image":
            return IMG_TOK, IMG_TOK * Est.chars_per_tok
        try:
            s = json.dumps(o, ensure_ascii=False)
        except Exception:
            return 0, 0.0
        return est_text(s), float(len(s))
    s = str(o)
    return est_text(s), float(len(s))

def est_obj(o):
    return est_pair(o)[0]

# ------------------------------------------------------- per-category fit
# One global chars/token constant is a lie: JSON and code tokenize near 3
# chars/token, English prose near 4.5. The session already carries the ground
# truth to do better — R (in + cache_read + cache_creation) is authoritative on
# every turn and the engine knows how many CHARS of each category were resident
# at that turn. Regressing R on those char counts recovers this model's actual
# per-category rates, entirely offline (no tokenizer, no API, no network).
FIT_CATS = tuple(c for c in CATS if c != "overhead")
FIT_ON = True            # --no-fit / `set fit 0` turns the whole thing off
FIT_MIN_TURNS = 24       # fewer rows than this: never leave the prior
FIT_LAMBDA = 16.0        # ridge toward the prior, in pseudo-observations
FIT_LAMBDA_B = 2.0       # ridge on the intercept (weaker: it is identifiable)
FIT_MIN_CPT = 1.5        # plausible chars/token bounds; clamp, never trust
FIT_MAX_CPT = 8.0
FIT_MAX_ROWS = 4096      # newest rows only — the fit tracks the current model
FIT_REFIT_EVERY = 16     # refit cadence floor (grows as n//8 with the session)
FIT_HOLDOUT = 0.6        # train on the first 60% of turns, gate on the rest
FIT_MIN_GAIN = 0.02      # out-of-sample gain a fit must show over the prior
FIT_CATS_GAIN = 0.0      # extra gain per-category must show over one ratio

def _solve(A, b):
    """Gaussian elimination with partial pivoting on a small dense system.
    Returns the solution vector, or None if the system is singular/ill-
    conditioned (the caller then falls back to the global prior)."""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    scale = [max(abs(x) for x in M[i][:n]) or 1.0 for i in range(n)]
    for c in range(n):
        piv, pr = 0.0, c
        for r in range(c, n):
            v = abs(M[r][c]) / scale[r]
            if v > piv:
                piv, pr = v, r
        if piv < 1e-10:
            return None
        M[c], M[pr] = M[pr], M[c]
        scale[c], scale[pr] = scale[pr], scale[c]
        d = M[c][c]
        for r in range(n):
            if r == c or M[r][c] == 0.0:
                continue
            f = M[r][c] / d
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]

def _scale_fit(rows, prior_cpt):
    """Stage 1: ONE ratio for everything, plus an intercept — the smallest
    honest model (2 params, always identifiable, no ridge needed). It is both
    a control ('does per-category actually earn its keep?') and the shrinkage
    target for stage 2: a category the data cannot speak to should fall back
    to THIS session's own measured rate, not to a hardcoded constant."""
    n = len(rows)
    sx = sy = sxx = sxy = 0.0
    for R, ch in rows:
        x = sum(ch.values())
        sx += x
        sy += R
        sxx += x * x
        sxy += x * R
    d = n * sxx - sx * sx
    inv0 = 1.0 / max(0.5, prior_cpt)
    meta = {"n": n, "cols": [], "clamped": [], "prior_cpt": prior_cpt}
    if abs(d) < 1e-9 or sxx <= 0:
        return dict(meta, inv={c: inv0 for c in FIT_CATS}, intercept=0.0,
                    k=inv0, fitted=False)
    k = (n * sxy - sx * sy) / d
    b = (sy - k * sx) / n
    lo, hi = 1.0 / FIT_MAX_CPT, 1.0 / FIT_MIN_CPT
    ok = k == k and lo <= k <= hi and b >= 0
    if not ok:
        k, b = inv0, max(0.0, (sy - inv0 * sx) / n)
    return dict(meta, inv={c: k for c in FIT_CATS}, intercept=b, k=k,
                fitted=ok)

def _fit_rows(rows, prior_cpt, base_hint, lam=None, sc=None):
    """Stage 2: ridge-regress R on per-category resident chars.

    rows = [(R:int, {cat: chars})]. Solves
        R_t ≈ intercept + Σ_c chars[c,t] · inv[c]
    for `inv` (tokens per char) with a Tikhonov pull toward the SESSION'S OWN
    global rate (stage 1) — falling back to 1/chars_per_tok only when even that
    is unidentifiable. Shrinking toward the session's measured rate rather than
    a hardcoded 3.8 is what makes a barely-present category land somewhere
    sensible instead of importing a constant this model may not obey. Columns
    are RMS-scaled first, which both conditions the normal equations and makes
    one λ mean the same thing for every category.
    Returns {"inv","intercept",...} or None."""
    n = len(rows)
    if n < 4:
        return None
    lam = FIT_LAMBDA if lam is None else lam
    if sc is None:
        sc = _scale_fit(rows, prior_cpt)
    inv0 = sc["k"]
    base_hint = sc["intercept"] if sc["fitted"] else base_hint
    scale = {}
    for c in FIT_CATS:
        s = math.sqrt(sum(r[1].get(c, 0.0) ** 2 for r in rows) / n)
        scale[c] = s if s > 1.0 else 0.0     # 0 -> column carries no signal
    cols = [c for c in FIT_CATS if scale[c]]
    m = len(cols) + 1                        # + intercept
    G = [[0.0] * m for _ in range(m)]
    v = [0.0] * m
    for R, ch in rows:
        x = [1.0] + [ch.get(c, 0.0) / scale[c] for c in cols]
        for a in range(m):
            xa = x[a]
            if xa == 0.0:
                continue
            Ga = G[a]
            for b in range(a, m):
                Ga[b] += xa * x[b]
            v[a] += xa * R
    for a in range(m):                       # symmetrize
        for b in range(a):
            G[a][b] = G[b][a]
    beta0 = [float(base_hint)] + [inv0 * scale[c] for c in cols]
    # ridge weight per category: λ scaled by how SMALL the category's share of
    # the resident text is. A category holding 1% of the chars can shift R by
    # at most ~1% whatever its true ratio, so it is unidentifiable and belongs
    # on the prior; the dominant categories are the ones the data can speak to.
    stot = math.sqrt(sum(sum(r[1].values()) ** 2 for r in rows) / n) or 1.0
    lams = [FIT_LAMBDA_B] + [lam * max(1.0, stot / scale[c]) for c in cols]
    for a in range(m):
        G[a][a] += lams[a]
        v[a] += lams[a] * beta0[a]
    sol = _solve(G, v)
    if sol is None or any(x != x for x in sol):   # singular / NaN
        return None
    # keep every ratio inside a plausible chars/token band. Post-hoc clamping
    # would leave the other coefficients at a now-wrong optimum, so when the
    # unconstrained solution leaves the box, re-minimize the SAME ridge
    # objective subject to it (cyclic coordinate descent on the quadratic —
    # exact for a box-constrained convex QP, ~20k flops).
    lo, hi = 1.0 / FIT_MAX_CPT, 1.0 / FIT_MIN_CPT
    box = [(lo * scale[c], hi * scale[c]) for c in cols]
    bound = [i for i, (a, b) in enumerate(box)
             if not (a <= sol[i + 1] <= b)]
    if bound:
        beta = [sol[0]] + [min(b, max(a, sol[i + 1]))
                           for i, (a, b) in enumerate(box)]
        for _ in range(400):
            delta = 0.0
            for a in range(m):
                r = v[a] - sum(G[a][b] * beta[b] for b in range(m) if b != a)
                x = r / G[a][a] if G[a][a] > 0 else beta[a]
                if a:
                    x = min(box[a - 1][1], max(box[a - 1][0], x))
                delta = max(delta, abs(x - beta[a]))
                beta[a] = x
            if delta <= 1e-9 * (1.0 + abs(beta[0])):
                break
        sol = beta
        bound = [i for i, (a, b) in enumerate(box)
                 if sol[i + 1] <= a * (1 + 1e-9) or sol[i + 1] >= b * (1 - 1e-9)]
    inv = {c: inv0 for c in FIT_CATS}
    for i, c in enumerate(cols):
        inv[c] = sol[i + 1] / scale[c]
    return {"inv": inv, "intercept": sol[0], "n": n, "cols": cols,
            "clamped": [cols[i] for i in bound], "prior_cpt": prior_cpt}

def fit_predict(f, ch):
    inv = f["inv"]
    t = f["intercept"]
    for c, v in ch.items():
        t += v * inv.get(c, 0.0)
    return t

def _errs(rows, f):
    return sorted(abs(fit_predict(f, ch) - R) / max(1.0, float(R))
                  for R, ch in rows)

def _mae_pct(rows, f):
    """Mean |error| as a fraction of R — the honest score for a predictor
    whose target is authoritative."""
    if not rows:
        return 0.0
    e = _errs(rows, f)
    return sum(e) / len(e)

def _mdae_pct(rows, f):
    """MEDIAN |error| / R. The gate scores on this, not the mean: a handful of
    turns right after a server context rebuild are unpredictable for every
    model and would otherwise drown out the typical turn."""
    if not rows:
        return 0.0
    e = _errs(rows, f)
    n = len(e)
    return e[n // 2] if n % 2 else 0.5 * (e[n // 2 - 1] + e[n // 2])

def _prior_fit(rows, prior_cpt):
    """The OLD model as a frozen predictor: one global chars/token constant
    plus a least-squares intercept (the invisible server-side overhead)."""
    inv0 = 1.0 / max(0.5, prior_cpt)
    b = sum(R - sum(v * inv0 for v in ch.values()) for R, ch in rows) / len(rows)
    return {"inv": {c: inv0 for c in FIT_CATS}, "intercept": b, "n": len(rows),
            "cols": [], "clamped": [], "prior_cpt": prior_cpt}

def fit_cats(rows, prior_cpt=None, base_hint=0.0, min_turns=None):
    """Fit + GATE. Always returns a state dict; `mode` says which model earned
    its keep on turns it was NOT fitted to:

        cats   per-category ratios (stage 2)
        scale  one fitted ratio + intercept (stage 1) — the per-category split
               did not beat it, but the session's own scale still beats 3.8
        prior  neither did: the global constant, exactly as before

    The gate is out-of-sample by construction — fit on the first FIT_HOLDOUT of
    the turns, score on the rest — because in-sample a 10-parameter model always
    'wins'. Only on held-out turns does a better SPLIT show up as a better
    prediction of authoritative R."""
    prior_cpt = prior_cpt or Est.chars_per_tok
    mt = FIT_MIN_TURNS if min_turns is None else min_turns
    st = {"active": False, "mode": "prior", "reason": "", "n": len(rows),
          "prior_cpt": round(prior_cpt, 3)}
    if len(rows) < mt:
        st["reason"] = "too few turns (%d < %d)" % (len(rows), mt)
        return st
    rows = rows[-FIT_MAX_ROWS:]
    k = max(4, int(len(rows) * FIT_HOLDOUT))
    train, test = rows[:k], rows[k:]
    if len(test) < 4:
        st["reason"] = "too few holdout turns"
        return st
    sc_tr = _scale_fit(train, prior_cpt)
    ftr = _fit_rows(train, prior_cpt, base_hint, sc=sc_tr)
    h_prior = _mdae_pct(test, _prior_fit(train, prior_cpt))
    h_scale = _mdae_pct(test, sc_tr)
    h_cats = _mdae_pct(test, ftr) if ftr is not None else None
    st["holdout_prior_pct"] = round(100.0 * h_prior, 3)
    st["holdout_scale_pct"] = round(100.0 * h_scale, 3)
    if h_cats is not None:
        st["holdout_cats_pct"] = round(100.0 * h_cats, 3)
    def beats(a, b, m=FIT_MIN_GAIN):
        return a < b * (1.0 - m)
    if (h_cats is not None and beats(h_cats, h_scale, FIT_CATS_GAIN)
            and beats(h_cats, h_prior)):
        mode, hold = "cats", h_cats
    elif beats(h_scale, h_prior) and sc_tr["fitted"]:
        mode, hold = "scale", h_scale
    else:
        st["reason"] = ("no out-of-sample gain (per-category %s, global scale "
                        "%.2f%%, prior %.2f%%, median |err|/R)"
                        % ("%.2f%%" % (100.0 * h_cats) if h_cats is not None
                           else "n/a", 100.0 * h_scale, 100.0 * h_prior))
        return st
    sc = _scale_fit(rows, prior_cpt)
    full = sc if mode == "scale" else _fit_rows(rows, prior_cpt, base_hint,
                                                sc=sc)
    if full is None:
        st["reason"] = "singular system"
        return st
    if full["intercept"] < 0:
        st["reason"] = "negative overhead intercept"
        return st
    ybar = sum(R for R, _ in rows) / len(rows)
    ss_t = sum((R - ybar) ** 2 for R, _ in rows)
    ss_r = sum((fit_predict(full, ch) - R) ** 2 for R, ch in rows)
    rms = math.sqrt(ss_r / len(rows))
    st.update(full)
    st["active"] = True
    st["mode"] = mode
    st["holdout_fit_pct"] = round(100.0 * hold, 3)
    st["gain"] = round((h_prior - hold) / h_prior if h_prior > 0 else 0.0, 4)
    st["reason"] = ("fitted per category" if mode == "cats"
                    else "one fitted global ratio (per-category split did not "
                         "beat it out of sample)")
    st["rms"] = rms
    st["rms_pct"] = round(100.0 * rms / max(1.0, ybar), 3)
    st["r2"] = round(1.0 - ss_r / ss_t, 4) if ss_t > 0 else 0.0
    st["mae_pct"] = round(100.0 * _mae_pct(rows, full), 3)
    return st

def fit_report(st):
    """Wire/report view of a fit state: chars/token per category (the human
    unit), the fitted overhead intercept, and how well it actually did."""
    out = {"active": bool(st.get("active")), "mode": st.get("mode", "prior"),
           "reason": st.get("reason", ""), "turns": int(st.get("n", 0)),
           "prior_cpt": st.get("prior_cpt", Est.chars_per_tok)}
    for k in ("rms_pct", "r2", "mae_pct", "holdout_prior_pct",
              "holdout_scale_pct", "holdout_cats_pct", "holdout_fit_pct",
              "gain"):
        if k in st:
            out[k] = st[k]
    if st.get("active"):
        out["overhead"] = int(st["intercept"])
        out["cpt"] = {c: round(1.0 / r, 2)
                      for c, r in sorted(st["inv"].items()) if r > 0}
        out["fitted_cats"] = list(st.get("cols") or ())
        if st.get("clamped"):
            out["clamped"] = list(st["clamped"])
    return out

def hhmmss(ts):
    return ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else ""

def now_hhmmss():
    # every ts on the wire is UTC (SPEC b): transcript stamps are Zulu ISO
    # sliced by hhmmss(), so engine-synthesized stamps must be gmtime too
    return time.strftime("%H:%M:%S", time.gmtime())

def ts_epoch(ts):
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.rstrip("Z")).replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0

def _i(v):
    try:
        return int(v)
    except Exception:
        return 0

WRITE_TOOLS = {"Write": "w", "Edit": "e", "NotebookEdit": "e", "MultiEdit": "e"}
AGENT_TOOLS = ("Agent", "Task")

def tool_file(inp):
    if not isinstance(inp, dict):
        return None
    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    return fp if isinstance(fp, str) and fp else None

_CTRL_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)"
    r"|[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(s):
    """Strip ANSI escapes and control chars; normalize newlines (SPEC b: the
    cmd feed carries clean text only)."""
    if not isinstance(s, str):
        return ""
    return _CTRL_RE.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def head_clip(s, n):
    s = clean_text(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def tail_clip(s, n):
    s = clean_text(s).strip("\n")
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _blocks_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _ret_classify(name, inp):
    """External-retrieval tools only (SPEC b `ret`); file tools never."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "WebSearch":
        return {"kind": "search", "src": "web", "q": inp.get("query") or ""}
    if name == "WebFetch":
        url = inp.get("url") or ""
        m = re.match(r"https?://([^/]+)", url)
        return {"kind": "fetch", "src": m.group(1) if m else "web", "q": url}
    if name == "ToolSearch":
        return {"kind": "toolsearch", "src": "tools",
                "q": inp.get("query") or ""}
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "mcp"
        tool = parts[2] if len(parts) > 2 else name
        arg = next((v for k, v in inp.items()
                    if isinstance(v, str) and v
                    and k in ("query", "path", "url", "name", "fileId",
                              "id", "q", "document_id", "title")), "")
        return {"kind": "mcp", "src": server,
                "q": (tool + (" " + arg if arg else ""))}
    return None


def _fresh_pending():
    return {"turns": set(), "faccess": [], "segs": [], "files": set(),
            "compactions": [], "events": [], "agents": set(), "logs": [],
            "cmds": [], "rets": [], "map_rebuild": False}

# ---------------------------------------------------------------- providers
# Codex CLI and Gemini CLI transcripts → the engine's record model. Each
# adapter is a stateful, deep-copyable translator: one raw transcript line in,
# zero or more CLAUDE-SHAPED records out (user / assistant / system
# compact_boundary / provider_meta), which Session.feed_obj digests exactly as
# it does a Claude Code transcript. That is the whole design: one accounting
# core (turns, ring, files, cache economics, compactions, agents, replay,
# report), three front formats. Anything a provider cannot say (Codex has no
# cache-write tier; Gemini has no compaction pre/post sizes) is left absent
# and stays labeled estimated downstream — never invented.
#
#   provider  transcript                                          turn = 1 API request
#   claude    ~/.claude/projects/<slug>/<sid>.jsonl               requestId
#   codex     ~/.codex/sessions/Y/M/D/rollout-<ts>-<id>.jsonl     one event_msg token_count
#   gemini    ~/.gemini/tmp/<proj>/chats/session-<ts>-<id8>.jsonl one `gemini` message
#
# Turn ordering law (both adapters): the usage record OPENS the turn first,
# then the response's content records (own uuids — compaction survivors are
# matched by uuid), then the tool_result records. Content is therefore held
# until the request's usage is known (Codex: the token_count that follows the
# tool outputs; Gemini: tokens ride on the message itself), so a turn's
# allocations always land in the turn that paid for them.

def detect_provider(path, first_line=None):
    """Which CLI wrote this transcript. Path first (cheap, exact for the two
    known layouts), then the first line's shape."""
    p = path or ""
    if "/.codex/" in p or os.path.basename(p).startswith("rollout-"):
        return "codex"
    if "/.gemini/" in p and os.path.basename(p).startswith("session-"):
        return "gemini"
    if first_line is None:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                first_line = fh.readline()
        except OSError:
            first_line = ""
    try:
        d = json.loads(first_line or "{}")
    except Exception:
        d = {}
    if isinstance(d, dict):
        if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
            return "codex"
        if isinstance(d.get("sessionId"), str) and isinstance(d.get("projectHash"), str) \
                and "type" not in d:
            return "gemini"
    return "claude"


_CODEX_INJECTED = ("# AGENTS.md", "<environment_context>", "<turn_aborted>",
                   "<permissions", "<skills_instructions>", "<multi_agent_mode>",
                   "<user_instructions>", "<INSTRUCTIONS>")


def codex_user_text(payload):
    """The human's text in a Codex response_item message role:user — None
    when the record is the harness's own injected context (AGENTS.md,
    environment, permissions…), which also arrives as role:user."""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    text = "\n".join(
        c.get("text") or "" for c in (payload.get("content") or [])
        if isinstance(c, dict) and isinstance(c.get("text"), str))
    if text.lstrip().startswith(_CODEX_INJECTED):
        return None
    return text


def _shell_ok_from_output(out):
    """Codex/Gemini put the exit status inside the tool output text."""
    m = re.search(r"(?:Process exited with code|Exit [Cc]ode:?)\s*(-?\d+)", out or "")
    return (m is None) or m.group(1) == "0"


def _codex_output_body(out):
    """The stdout part of a Codex exec_command output ("Chunk ID…\nOutput:\n…")."""
    if not isinstance(out, str):
        return ""
    i = out.find("Output:\n")
    return out[i + 8:] if i >= 0 else out


def _norm_blocks(o):
    """Tool output → tool_result content the estimator prices honestly: text
    stays text, images (base64 payloads that are NOT tokens) become the
    engine's flat-priced image block, anything else is JSON."""
    if o is None:
        return ""
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        o = [o]
    if isinstance(o, list):
        blocks = []
        for b in o:
            if isinstance(b, dict):
                bt = b.get("type") or ""
                if bt in ("input_image", "image", "output_image") or \
                        isinstance(b.get("inlineData"), dict) or \
                        isinstance(b.get("image_url"), str):
                    blocks.append({"type": "image"})
                    continue
                if isinstance(b.get("text"), str):
                    blocks.append({"type": "text", "text": b["text"]})
                    continue
                blocks.append({"type": "text", "text": json.dumps(b)})
            elif isinstance(b, str):
                blocks.append({"type": "text", "text": b})
            else:
                blocks.append({"type": "text", "text": json.dumps(b)})
        return blocks
    return json.dumps(o)


_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)


def _codex_patch_files(text):
    """(op, path) per file in an apply_patch input; op ∈ w (add) · e (update)
    · d (delete)."""
    out = []
    for m in _PATCH_FILE_RE.finditer(text or ""):
        op = {"Add": "w", "Update": "e", "Delete": "d"}[m.group(1)]
        out.append((op, m.group(2).strip()))
    return out


class CodexAdapter:
    """Codex CLI rollout → records. Buffers a request's response_items until
    its token_count arrives (see the ordering law), then emits usage → content
    → tool results. Every field is plain data so Session.clone() deep-copies
    the adapter with the checkpoint (replay resumes mid-stream)."""
    provider = "codex"

    def __init__(self):
        self.req_n = 0             # requests seen (turn counter)
        self.items = []            # buffered content records for the open request
        self.results = []          # buffered tool_result records
        self.last_usage = None     # last token_count usage (dedupe refreshes)
        self.model = ""
        self.budget = None
        self.tools = {}            # call_id -> (mapped name, file paths, kind)
        self.meta_sent = False
        self.agents = {}           # thread id -> agent_path

    # -- helpers ------------------------------------------------------------
    def _req_id(self, offset=0):
        return "cx-req-%d" % (self.req_n + offset)

    @staticmethod
    def _rec(kind, uuid, ts, **kw):
        d = {"type": kind, "uuid": uuid, "timestamp": ts}
        d.update(kw)
        return d

    def _assistant(self, uuid, ts, blocks, usage=None, req=None):
        msg = {"role": "assistant", "model": self.model, "content": blocks}
        if usage is not None:
            msg["usage"] = usage
        return self._rec("assistant", uuid, ts, requestId=req or self._req_id(1),
                         message=msg)

    def _tool_result_rec(self, uuid, ts, call_id, out, ok, tur=None):
        b = {"type": "tool_result", "tool_use_id": call_id,
             "content": _norm_blocks(out)}
        if not ok:
            b["is_error"] = True
        d = self._rec("user", uuid, ts,
                      message={"role": "user", "content": [b]})
        if tur is not None:
            d["toolUseResult"] = tur
        return d

    def _map_call(self, name, args_raw, call_id):
        """Codex tool → the engine's tool vocabulary; returns tool_use blocks
        (apply_patch fans out per file) and remembers the mapping."""
        args = args_raw
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {"raw": args_raw}
        if not isinstance(args, dict):
            args = {"raw": args}
        if name in ("exec_command", "shell", "shell_command", "local_shell"):
            cmd = args.get("cmd") or args.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            self.tools[call_id] = ("Bash", [], "cmd")
            return [{"type": "tool_use", "id": call_id, "name": "Bash",
                     "input": {"command": cmd or "", "workdir": args.get("workdir")}}]
        if name == "apply_patch":
            files = _codex_patch_files(args.get("raw") if "raw" in args
                                       else args.get("input") or args.get("patch") or "")
            blocks = []
            paths = []
            for i, (op, fp) in enumerate(files):
                if op == "d":
                    continue
                tool = "Write" if op == "w" else "Edit"
                bid = call_id if not blocks else "%s#%d" % (call_id, i)
                blocks.append({"type": "tool_use", "id": bid, "name": tool,
                               "input": {"file_path": fp,
                                         "patch": args.get("raw") or ""}})
                paths.append(fp)
            if not blocks:
                blocks = [{"type": "tool_use", "id": call_id, "name": "apply_patch",
                           "input": args}]
            self.tools[call_id] = ("Edit", paths, "patch")
            return blocks
        if name in ("web_search", "web_search_call"):
            self.tools[call_id] = ("WebSearch", [], "ret")
            return [{"type": "tool_use", "id": call_id, "name": "WebSearch",
                     "input": {"query": args.get("query") or args.get("q") or ""}}]
        if name in ("spawn_agent", "spawn_agents", "run_agent"):
            self.tools[call_id] = ("Task", [], "agent")
            return [{"type": "tool_use", "id": call_id, "name": "Task",
                     "input": {"description": args.get("agent_path")
                               or args.get("name") or name}}]
        self.tools[call_id] = (name, [], "tool")
        return [{"type": "tool_use", "id": call_id, "name": name, "input": args}]

    # -- the translator ------------------------------------------------------
    def translate(self, d):
        t = d.get("type")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        ts = d.get("timestamp") or ""
        out = []
        if t == "session_meta":
            self.model = p.get("model") or self.model
            src = p.get("source")
            sub = p.get("thread_source") == "subagent" or (
                isinstance(src, dict) and "subagent" in src)
            out.append({"type": "provider_meta", "provider": "codex",
                        "sessionId": p.get("id") or p.get("session_id"),
                        "timestamp": p.get("timestamp") or ts,
                        "version": p.get("cli_version"), "cwd": p.get("cwd"),
                        "subagent": bool(sub),
                        "title": (p.get("agent_nickname") or p.get("agent_path"))
                        if sub else None})
            self.meta_sent = True
            return out
        if t == "turn_context":
            m = p.get("model")
            if isinstance(m, str) and m:
                self.model = m
            return out
        if t == "response_item":
            pt = p.get("type")
            iid = p.get("id") or ("cx-item-%d" % (len(self.items) + 1))
            if pt == "message":
                role = p.get("role")
                text = "\n".join(
                    c.get("text") or "" for c in (p.get("content") or [])
                    if isinstance(c, dict) and isinstance(c.get("text"), str))
                if role == "user":
                    # user prompts and Codex's own injected context (AGENTS.md,
                    # environment) both arrive as role:user; the injected ones
                    # are the harness talking, not the human — attach
                    if codex_user_text(p) is None:
                        out.append(self._rec("attachment", iid, ts,
                                             attachment={"type": "codex_context",
                                                         "text": text}))
                    else:
                        out.append(self._rec("user", iid, ts, message={
                            "role": "user",
                            "content": [{"type": "text", "text": text}]}))
                elif role == "developer" or role == "system":
                    out.append(self._rec("attachment", iid, ts,
                                         attachment={"type": "codex_developer",
                                                     "text": text}))
                elif role == "assistant":
                    self.items.append(self._assistant(
                        iid, ts, [{"type": "text", "text": text}]))
            elif pt == "reasoning":
                # summaries are the only visible reasoning; the encrypted body
                # is resident too but its size is only knowable through usage
                # (hidden reasoning = out − visible, the Claude rule)
                summ = "\n".join(
                    c.get("text") or "" for c in (p.get("summary") or [])
                    if isinstance(c, dict) and isinstance(c.get("text"), str))
                if summ:
                    self.items.append(self._assistant(
                        iid, ts, [{"type": "thinking", "thinking": summ}]))
            elif pt in ("function_call", "custom_tool_call"):
                cid = p.get("call_id") or iid
                raw = p.get("arguments") if pt == "function_call" else p.get("input")
                blocks = self._map_call(p.get("name") or "?", raw, cid)
                self.items.append(self._assistant(iid, ts, blocks))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                cid = p.get("call_id") or ""
                outp = p.get("output")
                name, paths, kind = self.tools.get(cid, ("?", [], "tool"))
                text = outp if isinstance(outp, str) else ""
                ok = _shell_ok_from_output(text)
                tur = None
                if kind == "cmd":
                    tur = {"stdout": _codex_output_body(text), "stderr": "",
                           "interrupted": "interrupted" in text[:200].lower()}
                self.results.append(self._tool_result_rec(iid, ts, cid, outp, ok, tur))
                if kind == "patch" and len(paths) > 1:
                    # the fan-out ids share one output; the extras get an
                    # empty ack so their file segments still resolve
                    for i in range(1, len(paths)):
                        self.results.append(self._tool_result_rec(
                            "%s#%d" % (iid, i), ts, "%s#%d" % (cid, i), "", True))
            elif pt == "agent_message":
                # inter-agent mail: resident context, harness-injected
                text = "\n".join(
                    c.get("text") or "" for c in (p.get("content") or [])
                    if isinstance(c, dict) and isinstance(c.get("text"), str))
                out.append(self._rec("attachment", iid, ts,
                                     attachment={"type": "agent_message",
                                                 "text": text}))
            return out
        if t == "event_msg":
            pt = p.get("type")
            if pt == "task_started":
                w = p.get("model_context_window")
                if isinstance(w, int) and w > 0 and w != self.budget:
                    self.budget = w
                    out.append({"type": "provider_meta", "provider": "codex",
                                "budget": w})
                return out
            if pt == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else {}
                last = info.get("last_token_usage") \
                    if isinstance(info.get("last_token_usage"), dict) else None
                w = info.get("model_context_window")
                if isinstance(w, int) and w > 0 and w != self.budget:
                    self.budget = w
                    out.append({"type": "provider_meta", "provider": "codex",
                                "budget": w})
                if last is None:
                    return out
                key = (last.get("input_tokens"), last.get("cached_input_tokens"),
                       last.get("output_tokens"), last.get("cache_write_input_tokens"))
                if key == self.last_usage and not self.items and not self.results:
                    return out           # a rate-limit refresh, not a request
                self.last_usage = key
                self.req_n += 1
                inp = _i(last.get("input_tokens"))
                cached = min(_i(last.get("cached_input_tokens")), inp)
                usage = {"input_tokens": inp - cached,
                         "cache_read_input_tokens": cached,
                         "cache_creation_input_tokens": _i(last.get("cache_write_input_tokens")),
                         "output_tokens": _i(last.get("output_tokens"))}
                out.append(self._assistant(self._req_id(), ts, [], usage=usage,
                                           req=self._req_id()))
                for it in self.items:
                    it["requestId"] = self._req_id()
                    out.append(it)
                out.extend(self.results)
                self.items, self.results = [], []
                return out
            if pt in ("task_complete", "turn_aborted"):
                # a request that ended without a token_count (aborted, or the
                # final assistant message that came after the last count):
                # its content still landed in context — allocate it under
                # the last request
                if self.items or self.results:
                    for it in self.items:
                        it["requestId"] = self._req_id() if self.req_n else "cx-req-1"
                        out.append(it)
                    out.extend(self.results)
                    self.items, self.results = [], []
                if pt == "turn_aborted":
                    out.append({"type": "provider_event", "kind": "interrupt",
                                "severity": "info", "timestamp": ts,
                                "msg": "turn aborted"})
                return out
            if pt == "sub_agent_activity":
                aid = p.get("agent_thread_id") or ""
                kind = p.get("kind")
                path = p.get("agent_path") or aid[:8]
                if not aid:
                    return out
                if kind == "started" and aid not in self.agents:
                    self.agents[aid] = path
                    cid = "cx-ag-" + aid
                    self.tools[cid] = ("Task", [], "agent")
                    self.items.append(self._assistant(
                        "cx-agl-" + aid, ts,
                        [{"type": "tool_use", "id": cid, "name": "Task",
                          "input": {"description": path}}]))
                    self.results.append(self._tool_result_rec(
                        "cx-agr-" + aid, ts, cid, "", True,
                        tur={"agentId": aid, "description": path,
                             "agentType": os.path.basename(path.rstrip("/"))
                             or "codex"}))
                elif kind == "interrupted" and aid in self.agents:
                    self.results.append(self._tool_result_rec(
                        "cx-agx-" + aid, ts, "cx-ag-" + aid, "interrupted", False,
                        tur={"agentId": aid, "status": "completed",
                             "description": self.agents[aid]}))
                return out
            if pt == "web_search_end":
                cid = p.get("call_id") or ("cx-ws-%d" % self.req_n)
                q = p.get("query") or ""
                res = p.get("results") if isinstance(p.get("results"), list) else []
                self.tools[cid] = ("WebSearch", [], "ret")
                self.items.append(self._assistant(
                    "cx-wsl-" + cid, ts,
                    [{"type": "tool_use", "id": cid, "name": "WebSearch",
                      "input": {"query": q}}]))
                text = "\n".join(
                    (r.get("title") or r.get("url") or r.get("domain") or "")
                    for r in res if isinstance(r, dict))
                self.results.append(self._tool_result_rec(
                    "cx-wsr-" + cid, ts, cid, text, True,
                    tur={"searchCount": len(res)}))
                return out
            if pt == "context_compacted":
                return out       # the `compacted` record carries the data
            return out
        if t == "compacted":
            hist = p.get("replacement_history")
            hist = hist if isinstance(hist, list) else []
            keep = [h.get("id") for h in hist if isinstance(h, dict) and h.get("id")]
            post_tok, _ = est_pair([h.get("content") for h in hist
                                    if isinstance(h, dict)])
            out.append({"type": "system", "subtype": "compact_boundary",
                        "uuid": "cx-compact-%d" % self.req_n, "timestamp": ts,
                        "compactMetadata": {"trigger": "auto",
                                            "preTokens": 0,   # engine fills R
                                            "postTokens": int(post_tok),
                                            "preservedMessages": {"allUuids": keep}}})
            return out
        return out


def gemini_token_limit(model):
    """The CLI's own tokenLimit(): 1,048,576 for every Gemini model it ships,
    256k for Gemma 4 — the context window IS the budget."""
    m = (model or "").lower()
    if m.startswith("gemma-4"):
        return 256_000
    return 1_048_576


def _gemini_parts_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content
                         if isinstance(p, dict) and isinstance(p.get("text"), str)
                         and not p.get("thought"))
    return ""


def _gemini_fr_text(result):
    """The text of a toolCall.result (functionResponse parts). Binary parts
    (inlineData images) become the literal marker "[image]" — callers price
    images through _norm_blocks, not through this text."""
    if isinstance(result, str):
        return result
    texts = []
    for part in result if isinstance(result, list) else [result]:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("inlineData"), dict):
            texts.append("[image]")
            continue
        fr = part.get("functionResponse")
        if isinstance(fr, dict):
            resp = fr.get("response")
            if isinstance(resp, dict):
                for k in ("output", "content", "result", "error"):
                    v = resp.get(k)
                    if isinstance(v, str):
                        texts.append(v)
                        break
                else:
                    texts.append(json.dumps(resp))
            elif isinstance(resp, str):
                texts.append(resp)
        elif isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(texts)


class GeminiAdapter:
    """Gemini CLI session recording → records. The file is an upsert log:
    a message may be re-appended with more fields (tokens, more toolCalls),
    `$set` patches metadata (and `$set.messages` REPLACES the history — the
    compaction), `$rewindTo` truncates. The adapter emits only the delta each
    time a message reappears."""
    provider = "gemini"
    _FILE_TOOLS = {"read_file": "Read", "read_many_files": "Read",
                   "write_file": "Write", "replace": "Edit", "edit": "Edit"}
    _SHELL_TOOLS = ("run_shell_command", "shell", "execute_command")
    _RET_TOOLS = {"google_web_search": "WebSearch", "web_search": "WebSearch",
                  "web_fetch": "WebFetch"}

    def __init__(self):
        self.seen = {}          # msg id -> {"text": bool, "tools": set(), "usage": bool}
        self.order = []         # message ids in history order (rewind law)
        self.model = ""
        self.budget = None
        self.session_id = None
        self.meta_sent = False
        self.hold = None        # a gemini message waiting for tokens (id, rec)
        self.n_compact = 0

    @staticmethod
    def _rec(kind, uuid, ts, **kw):
        d = {"type": kind, "uuid": uuid, "timestamp": ts}
        d.update(kw)
        return d

    def _tool_block(self, tc):
        name = tc.get("name") or "?"
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        cid = tc.get("id") or ""
        if name in self._FILE_TOOLS:
            fp = args.get("file_path") or args.get("absolute_path") or args.get("path")
            inp = dict(args)
            if isinstance(fp, str):
                inp["file_path"] = fp
            return {"type": "tool_use", "id": cid, "name": self._FILE_TOOLS[name],
                    "input": inp}, "file"
        if name in self._SHELL_TOOLS:
            return {"type": "tool_use", "id": cid, "name": "Bash",
                    "input": {"command": args.get("command") or "",
                              "description": args.get("description")}}, "cmd"
        if name in self._RET_TOOLS:
            mapped = self._RET_TOOLS[name]
            inp = {"query": args.get("query") or ""} if mapped == "WebSearch" \
                else {"url": args.get("url") or args.get("prompt") or ""}
            return {"type": "tool_use", "id": cid, "name": mapped, "input": inp}, "ret"
        if tc.get("agentId") or name in ("subagent", "run_subagent", "delegate"):
            return {"type": "tool_use", "id": cid, "name": "Task",
                    "input": {"description": args.get("description")
                              or args.get("prompt") or name}}, "agent"
        return {"type": "tool_use", "id": cid, "name": name, "input": args}, "tool"

    def _emit_message(self, m, ts, out):
        mid = m.get("id")
        mtype = m.get("type")
        st = self.seen.get(mid)
        if st is None:
            st = {"text": False, "tools": set(), "usage": False}
            self.seen[mid] = st
            self.order.append(mid)
        if mtype == "user":
            if not st["text"]:
                st["text"] = True
                text = _gemini_parts_text(m.get("content"))
                # binary/info/context injections vs the human's prompt: the
                # CLI marks nothing, so a leading @-file/context header is
                # the only tell — keep it simple: everything is `user`
                out.append(self._rec("user", mid, ts, message={
                    "role": "user", "content": [{"type": "text", "text": text}]}))
            return
        if mtype in ("info", "error", "warning"):
            if not st["text"]:
                st["text"] = True
                out.append(self._rec("attachment", mid, ts, attachment={
                    "type": "gemini_" + mtype,
                    "text": _gemini_parts_text(m.get("content"))}))
            return
        if mtype != "gemini":
            return
        model = m.get("model") or self.model
        if model and model != self.model:
            self.model = model
            lim = gemini_token_limit(model)
            if lim != self.budget:
                self.budget = lim
                out.append({"type": "provider_meta", "provider": "gemini",
                            "budget": lim})
        tokens = m.get("tokens") if isinstance(m.get("tokens"), dict) else None
        blocks = []
        if not st["text"]:
            st["text"] = True
            text = _gemini_parts_text(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for th in m.get("thoughts") or []:
                if isinstance(th, dict):
                    tt = " ".join(x for x in (th.get("subject"), th.get("description"))
                                  if isinstance(x, str))
                    if tt:
                        blocks.append({"type": "thinking", "thinking": tt})
        results = []
        for tc in m.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            cid = tc.get("id") or ""
            if cid in st["tools"]:
                continue
            status = tc.get("status")
            # emit a call only once it has finished (its result rides along)
            if status not in ("success", "error", "cancelled", None) and \
                    tc.get("result") is None:
                continue
            st["tools"].add(cid)
            blk, kind = self._tool_block(tc)
            blocks.append(blk)
            rtext = _gemini_fr_text(tc.get("result"))
            ok = status != "error" and (kind != "cmd" or _shell_ok_from_output(rtext))
            tur = None
            if kind == "cmd":
                body = rtext
                mm = re.search(r"\nOutput:\s*\n?(.*?)(?:\nExit Code:|\Z)", rtext, re.S)
                if mm:
                    body = mm.group(1)
                tur = {"stdout": body, "stderr": "", "interrupted": status == "cancelled"}
            elif kind == "file" and blk["name"] == "Read":
                tur = {"file": {"content": rtext}}
            elif kind == "agent":
                tur = {"agentId": tc.get("agentId") or cid,
                       "status": "completed" if status in ("success", "error") else None,
                       "description": (tc.get("args") or {}).get("description")
                       if isinstance(tc.get("args"), dict) else None,
                       "content": rtext}
            elif kind == "ret":
                tur = {"searchCount": None}
            n_img = sum(1 for part in (tc.get("result") or [])
                        if isinstance(part, dict) and isinstance(part.get("inlineData"), dict))
            content = rtext if not n_img else \
                [{"type": "text", "text": rtext}] + [{"type": "image"}] * n_img
            b = {"type": "tool_result", "tool_use_id": cid, "content": content}
            if not ok:
                b["is_error"] = True
            rd = self._rec("user", "%s#r%s" % (mid, cid[-8:]), ts,
                           message={"role": "user", "content": [b]})
            if tur is not None:
                rd["toolUseResult"] = tur
            results.append(rd)
        usage = None
        if tokens is not None and not st["usage"]:
            st["usage"] = True
            inp = _i(tokens.get("input"))
            cached = min(_i(tokens.get("cached")), inp)
            usage = {"input_tokens": inp - cached,
                     "cache_read_input_tokens": cached,
                     "cache_creation_input_tokens": 0,
                     "output_tokens": _i(tokens.get("output")) + _i(tokens.get("thoughts"))}
        if usage is None and not blocks and not results:
            return
        msg = {"role": "assistant", "model": model, "content": blocks}
        if usage is not None:
            msg["usage"] = usage
        out.append(self._rec("assistant", mid, ts, requestId=mid, message=msg))
        out.extend(results)

    def _compact(self, ts, keep, trigger="auto"):
        self.n_compact += 1
        return {"type": "system", "subtype": "compact_boundary",
                "uuid": "gm-compact-%d" % self.n_compact, "timestamp": ts,
                "compactMetadata": {"trigger": trigger, "preTokens": 0,
                                    "postTokens": 0,
                                    "preservedMessages": {"allUuids": list(keep)}}}

    def translate(self, d):
        out = []
        ts = d.get("timestamp") or d.get("lastUpdated") or d.get("startTime") or ""
        if isinstance(d.get("$rewindTo"), str):
            rid = d["$rewindTo"]
            keep = []
            for mid in self.order:
                if mid == rid:
                    break
                keep.append(mid)
            self.order = keep
            for mid in list(self.seen):
                if mid not in keep:
                    self.seen.pop(mid, None)
            out.append(self._compact(ts, keep, trigger="manual"))
            return out
        if isinstance(d.get("$set"), dict):
            st = d["$set"]
            if isinstance(st.get("messages"), list):
                # history REPLACED (chat compression): everything not in the
                # new set is gone; new synthetic entries (the summary) follow
                new_ids = [m.get("id") for m in st["messages"]
                           if isinstance(m, dict) and m.get("id")]
                keep = [i for i in new_ids if i in self.seen]
                for mid in list(self.seen):
                    if mid not in new_ids:
                        self.seen.pop(mid, None)
                self.order = [i for i in self.order if i in new_ids]
                out.append(self._compact(ts, keep))
                for m in st["messages"]:
                    if isinstance(m, dict) and m.get("id"):
                        self._emit_message(m, m.get("timestamp") or ts, out)
            if isinstance(st.get("summary"), str) and st["summary"]:
                out.append({"type": "custom-title", "customTitle": st["summary"][:120]})
            if isinstance(st.get("sessionId"), str) and not self.session_id:
                self.session_id = st["sessionId"]
            return out
        if isinstance(d.get("id"), str) and "type" in d:
            self._emit_message(d, d.get("timestamp") or ts, out)
            return out
        if isinstance(d.get("sessionId"), str) and isinstance(d.get("projectHash"), str):
            self.session_id = d["sessionId"]
            dirs = d.get("directories") if isinstance(d.get("directories"), list) else []
            out.append({"type": "provider_meta", "provider": "gemini",
                        "sessionId": d["sessionId"],
                        "timestamp": d.get("startTime") or ts,
                        "cwd": dirs[0] if dirs and isinstance(dirs[0], str) else None,
                        "projectHash": d.get("projectHash"),
                        "subagent": d.get("kind") == "subagent",
                        "title": d.get("summary")})
            self.meta_sent = True
            return out
        return out


def make_adapter(provider):
    return {"codex": CodexAdapter, "gemini": GeminiAdapter}.get(provider, lambda: None)()


# ---------------------------------------------------------------- session
class Session:
    """Pure accounting for one transcript. No I/O emission of its own —
    everything lands in self.pending for the caller to drain."""

    _SKIP_CLONE = ("checkpoints", "rec_offsets", "pending", "_no_ckpt")

    def __init__(self, path, budget=None, budget_pinned=False, t_auto=0.85,
                 ckpt_every=200, sidechain_ok=False, provider=None):
        # an explicitly attached agent transcript IS the main conversation
        # from this Session's point of view (SPEC c `attach`)
        self.sidechain_ok = sidechain_ok
        self.path = path
        # which CLI wrote the transcript; non-Claude providers feed through a
        # translating adapter (see the providers section) — plain data, so
        # clone() carries it into checkpoints and replay resumes mid-stream
        self.provider = provider or detect_provider(path)
        self.adapter = make_adapter(self.provider)
        self.session_id = os.path.basename(path)[:-6] if path.endswith(".jsonl") \
            else os.path.basename(path)
        # authoritative per-turn ledger
        self.turns = []          # list of turn dicts (payload minus cost/hit)
        self.req_last = None
        # record ring: seg_id -> seg (insertion-ordered dict); evicted removed
        self.ring = {}
        self.by_uuid = {}        # uuid -> [seg_ids]
        self.seg_next = 1        # 0 reserved for the overhead segment
        # files
        self.files = {}          # id -> file dict
        self.path2id = {}
        self.file_next = 0
        self.faccess = deque(maxlen=4096)
        self.cmds = deque(maxlen=256)      # SHELL console feed (SPEC b `cmd`)
        self.turn_epochs = []              # open-epoch per turn (turn_at_epoch)
        self.rets = deque(maxlen=256)      # agentic-retrieval feed (SPEC b `ret`)
        self.tu2ret = {}                   # tool_use_id -> {kind, src, q}
        self.tu2cmd = {}                   # tool_use_id -> {cmd, desc}
        # live category estimates (raw, unscaled)
        self.cat_est = {c: 0 for c in CATS if c != "overhead"}
        self.est_live = 0
        # resident CHARS per category + the per-turn snapshot of it: the
        # design matrix the per-category ratio fit regresses against R
        self.cat_chars = {c: 0.0 for c in CATS if c != "overhead"}
        self.turn_chars = []     # parallel to self.turns (never on the wire)
        self.fit = None          # active fit (see fit_cats) or None = prior
        self.fit_state = {"active": False, "reason": "warming up", "n": 0,
                          "prior_cpt": Est.chars_per_tok}
        self.fit_on = FIT_ON
        self.fit_next = FIT_MIN_TURNS
        # hidden reasoning (SPEC a): per-turn visible-assistant accumulator
        # and the ISO ts of the record that OPENED the current turn (gives
        # the synthetic reasoning seg a real epoch)
        self._vis_acc = 0
        self._turn_ts = ""
        # rebuild guard: True while a compact_boundary has run since the
        # last true turn open (its R drop must not read as a rebuild)
        self._compact_between = False
        # calibration
        self.overhead0 = None
        self.rebase_pending = False
        self.alpha = 1.0
        self.overhead = 0
        # between a compact_boundary and the next usage record turns[-1]
        # still holds the PRE-cut R; the boundary's post size is the honest
        # interim resident for map sizing (cleared at the next usage)
        self.interim_R = None
        # config / meta
        self.budget = budget if budget else BUDGET_RUNGS[0]
        self.budget_pinned = budget_pinned
        self.provider_budget = False    # a provider adapter set the window
        self.t_auto = t_auto
        self.model = ""
        self.backend = None    # local-backend identity (§ backend probe)
        self.cc_version = None
        self.started_at = None
        self.started_epoch = 0.0
        self.title = None
        self._ai_title = None
        self.project = None
        self.entrypoint = None
        self.last_ts = None      # newest record timestamp (report wall span)
        # compactions / events / agents
        self.compactions = []
        self.cum_dropped = 0
        self.rebuilds = []       # server context rebuilds (SPEC a; report f)
        self.events = deque(maxlen=256)
        self.agents = {}         # agent id -> agent dict
        self.tu2agent = {}       # tool_use_id -> {turn, ts}
        self.tu = {}             # tool_use_id -> (name, file_path)
        # diagnostics
        self.api_errors = 0
        self.last_retry_ms = None
        self.malformed = 0
        # thrash / pressure
        self.cc_hi_run = 0
        self.post_compact_grace = False
        self._sig_turn = -1      # last turn index the thrash signals ran for
        self._sid_seen = False
        self.zone = 0
        self._trunc_warned = False
        # map
        self.map_rev = 0
        self.map_base_n = 0      # segs in the last coalesced map emission
        self.map_adds_since = 0  # raw segs streamed via map_add since then
        # every parsed record uuid -> arrival index; anchors the
        # preservedSegment compaction fallback even for records that
        # produced no allocation
        self.uuid_order = {}
        # replay machinery
        self.rec_count = 0
        self.rec_offsets = []
        self.checkpoints = []    # (turn_count, rec_count, Session clone)
        self.ckpt_every = max(1, int(ckpt_every))
        self.next_ckpt_turn = self.ckpt_every
        self._no_ckpt = False
        self.pending = _fresh_pending()

    # ---- cloning / checkpoints ------------------------------------------
    def clone(self):
        new = Session.__new__(Session)
        for k, v in self.__dict__.items():
            if k in self._SKIP_CLONE:
                continue
            new.__dict__[k] = copy.deepcopy(v)
        new.checkpoints = []
        new.rec_offsets = []
        new.pending = _fresh_pending()
        new._no_ckpt = True
        return new

    def _maybe_checkpoint(self):
        if self._no_ckpt:
            return
        if len(self.turns) >= self.next_ckpt_turn:
            self.checkpoints.append((len(self.turns), self.rec_count, self.clone()))
            self.next_ckpt_turn = len(self.turns) + self.ckpt_every
            while len(self.checkpoints) > 16:
                # thin toward power-of-two spacing: halve the older entries
                keep = self.checkpoints[::2]
                if keep[-1] is not self.checkpoints[-1]:
                    keep.append(self.checkpoints[-1])
                self.checkpoints = keep

    # ---- feeding ---------------------------------------------------------
    def feed_line(self, line, offset=0):
        line = line.strip()
        if not line:
            return
        try:
            d = json.loads(line)
        except Exception:
            self.malformed += 1
            return
        if not isinstance(d, dict):
            return
        self.rec_offsets.append(offset)
        self.rec_count += 1
        for rec in self.translate(d):
            u = rec.get("uuid")
            if isinstance(u, str) and u not in self.uuid_order:
                self.uuid_order[u] = self.rec_count
            try:
                self.feed_obj(rec)
            except Exception as e:
                self.pending["logs"].append("record parse error: %s" % e)
        self._maybe_checkpoint()

    def translate(self, d):
        """Raw transcript object → the records feed_obj digests: identity
        for Claude Code, the provider adapter's output otherwise."""
        if self.adapter is None:
            return [d]
        try:
            return self.adapter.translate(d)
        except Exception as e:
            self.pending["logs"].append("%s adapter error: %s" % (self.provider, e))
            return []

    def is_new_turn(self, d):
        """Would this record open a new turn? Must mirror _feed_assistant."""
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return False
        if d.get("isSidechain") and not self.sidechain_ok:
            return False
        m = d.get("message")
        if not isinstance(m, dict):
            return False
        rid = d.get("requestId") or m.get("id")
        if (m.get("model") or "") == "<synthetic>" or not rid:
            return False
        if d.get("isApiErrorMessage"):
            return False
        if not isinstance(m.get("usage"), dict):
            return False
        return rid != self.req_last

    def feed_obj(self, d):
        if d.get("isSidechain") and not self.sidechain_ok:
            return
        t = d.get("type")
        if t == "assistant":
            self._feed_assistant(d)
        elif t == "user":
            self._feed_user(d)
        elif t == "attachment":
            self._feed_attachment(d)
        elif t == "system":
            self._feed_system(d)
        elif t == "custom-title":
            v = d.get("customTitle")
            if isinstance(v, str):
                self.title = v
        elif t == "ai-title":
            v = d.get("aiTitle")
            if isinstance(v, str):
                self._ai_title = v
                if self.title is None:
                    self.title = v
        elif t == "provider_meta":
            # a provider adapter's session facts (SPEC f2 → attach): the
            # model context window is the budget, authoritative for that
            # provider (a --budget pin still wins)
            w = d.get("budget")
            if isinstance(w, int) and w > 0 and not self.budget_pinned:
                self.budget = w
                self.provider_budget = True
            if isinstance(d.get("title"), str) and d["title"] and self.title is None:
                self.title = d["title"]
        elif t == "provider_event":
            self._event(d.get("kind") or "info", d.get("severity") or "info",
                        d.get("timestamp") or "", d.get("msg") or "")
        # every other record type: metadata, tolerated and ignored
        if not self._sid_seen and isinstance(d.get("sessionId"), str):
            self.session_id = d["sessionId"]     # records outrank the filename
            self._sid_seen = True
        if self.started_at is None and isinstance(d.get("timestamp"), str):
            self.started_at = d["timestamp"]
            self.started_epoch = ts_epoch(self.started_at)
        if self.cc_version is None and isinstance(d.get("version"), str):
            self.cc_version = d["version"]
        if self.project is None and isinstance(d.get("cwd"), str):
            self.project = d["cwd"]
        if self.entrypoint is None and isinstance(d.get("entrypoint"), str):
            self.entrypoint = d["entrypoint"]
        if isinstance(d.get("timestamp"), str):
            self.last_ts = d["timestamp"]

    # ---- allocation helpers ----------------------------------------------
    def _born(self):
        return max(0, len(self.turns) - 1)

    def _alloc(self, cat, tok, uuid, ts, file_id=None, chars=None):
        """`chars` = the raw character weight behind `tok`. Segments whose
        tokens are NOT char-derived (hidden reasoning, images) pass None and
        get the prior-implied chars, so the fit — regularized toward that same
        prior — leaves their pricing alone."""
        if tok <= 0:
            return
        if chars is None:
            chars = tok * Est.chars_per_tok
        sid = self.seg_next
        self.seg_next += 1
        seg = {"id": sid, "uuid": uuid, "cat": cat, "est": tok,
               "chars": float(chars),
               "file": file_id, "born": self._born(), "ts": ts_epoch(ts)}
        self.ring[sid] = seg
        self.by_uuid.setdefault(uuid, []).append(sid)
        self.cat_est[cat] = self.cat_est.get(cat, 0) + tok
        self.cat_chars[cat] = self.cat_chars.get(cat, 0.0) + seg["chars"]
        self.est_live += tok
        self.pending["segs"].append(seg)
        # SPEC (b) weight rule: a fresh coalesced map must land BEFORE the UI
        # ring (base + adds) would overflow; rev bumps so stale map_adds from
        # the old base can never append to the new one.
        self.map_adds_since += 1
        if (not self.pending["map_rebuild"]
                and self.map_base_n + self.map_adds_since >= MAP_CAP):
            self.map_rev += 1
            self.pending["map_rebuild"] = True

    def _file_id(self, fp):
        fid = self.path2id.get(fp)
        if fid is None:
            fid = self.file_next
            self.file_next += 1
            self.path2id[fp] = fid
            self.files[fid] = {"id": fid, "path": fp, "tok": 0, "cum": 0,
                               "reads": 0, "writes": 0, "edits": 0, "waste": 0,
                               "last_ts": "", "last_epoch": 0.0, "resident": True}
            self.pending["files"].add(fid)
        return fid

    def _faccess(self, fid, op, tok, ts):
        fa = {"turn": self._born(), "ts": hhmmss(ts), "file": fid,
              "op": op, "tok": int(tok)}
        self.faccess.append(fa)
        self.pending["faccess"].append(fa)

    def _file_touch(self, fid, ts):
        f = self.files[fid]
        f["last_ts"] = hhmmss(ts) or f["last_ts"]
        f["last_epoch"] = ts_epoch(ts) or f["last_epoch"]
        f["resident"] = True
        f["waste"] = max(0, f["cum"] - f["tok"])
        self.pending["files"].add(fid)

    def _mk_event(self, kind, severity, ts, msg):
        return {"kind": kind, "severity": severity, "ts": hhmmss(ts),
                "turn": self._born(), "msg": msg}

    def _event(self, kind, severity, ts, msg):
        ev = self._mk_event(kind, severity, ts, msg)
        self.events.append(ev)
        self.pending["events"].append(ev)

    # ---- assistant --------------------------------------------------------
    def _feed_assistant(self, d):
        m = d.get("message")
        if not isinstance(m, dict):
            return
        ts = d.get("timestamp") or ""
        model = m.get("model") or ""
        uuid = d.get("uuid") or ("anon-%d" % self.rec_count)
        if d.get("isApiErrorMessage"):
            self.api_errors += 1
            self._event("api_error", "error", ts,
                        str(d.get("error") or "api error (synthetic turn)"))
            return
        # requestId is absent when a non-Anthropic backend serves the session
        # (Ollama et al.); the streamed message id groups
        # same-response upserts exactly like requestId, so it is the fallback
        # turn key. True synthetics always carry model "<synthetic>".
        rid = d.get("requestId") or m.get("id")
        if model == "<synthetic>" or not rid:
            return  # synthetic: never a turn, never resident
        usage = m.get("usage") if isinstance(m.get("usage"), dict) else None
        content = m.get("content")
        # --- turn bookkeeping (LAST usage per requestId wins) ---
        if usage is not None:
            new_turn = rid != self.req_last
            if new_turn:
                # a TRUE turn boundary closes the previous turn: charge its
                # hidden reasoning BEFORE the new turn is appended (born =
                # the closed turn). Never on same-requestId usage upserts.
                self._close_turn_reasoning()
                self._vis_acc = 0
                self._turn_ts = ts
                self.req_last = rid
                if self.model and model and model != self.model:
                    self._event("model_switch", "info", ts,
                                "model %s -> %s" % (self.model, model))
                self.model = model or self.model
                self.turn_epochs.append(ts_epoch(ts))
                # design-matrix row: what was resident (in chars, per
                # category) when the server priced THIS request's R
                self.turn_chars.append(dict(self.cat_chars))
                self.turns.append({"turn": len(self.turns), "ts": hhmmss(ts),
                                   "model": model, "in": 0, "cr": 0, "cc": 0,
                                   "cc_5m": 0, "cc_1h": 0, "out": 0,
                                   "resident": 0, "waterline": 0,
                                   "dur_ms": None, "stop": None, "tools": 0})
            tr = self.turns[-1]
            tr["in"] = _i(usage.get("input_tokens"))
            tr["cr"] = _i(usage.get("cache_read_input_tokens"))
            cc = _i(usage.get("cache_creation_input_tokens"))
            tr["cc"] = cc
            nest = usage.get("cache_creation")
            if isinstance(nest, dict):
                tr["cc_5m"] = _i(nest.get("ephemeral_5m_input_tokens"))
                tr["cc_1h"] = _i(nest.get("ephemeral_1h_input_tokens"))
            else:
                tr["cc_5m"], tr["cc_1h"] = cc, 0   # split unknown -> cc_5m
            tr["out"] = _i(usage.get("output_tokens"))
            tr["model"] = model or tr["model"]
            if m.get("stop_reason"):
                tr["stop"] = m.get("stop_reason")
            tr["resident"] = tr["in"] + tr["cr"] + tr["cc"]
            tr["waterline"] = tr["cr"]
            self.pending["turns"].add(tr["turn"])
            self._on_turn_usage(tr, ts, new_turn)
        # --- content allocation (once per record) ---
        # every assistant-record allocation est also feeds the per-turn
        # visible accumulator (hidden reasoning = out - this sum)
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text") or ""
                    e = est_text(txt)
                    self._alloc("assistant", e, uuid, ts, chars=len(txt))
                elif bt == "thinking":
                    txt = b.get("thinking") or ""
                    e = est_text(txt)
                    self._alloc("thinking", e, uuid, ts, chars=len(txt))
                elif bt == "tool_use":
                    e = self._tool_use(b, uuid, ts)
                else:
                    e, ch = est_pair(b)
                    self._alloc("tool", e, uuid, ts, chars=ch)
                self._vis_acc += e
        elif isinstance(content, str):
            e = est_text(content)
            self._alloc("assistant", e, uuid, ts, chars=len(content))
            self._vis_acc += e

    def _close_turn_reasoning(self):
        """Hidden reasoning (SPEC a): extended-thinking models write EMPTY
        thinking blocks with only an encrypted signature; the real reasoning
        tokens are resident (re-billed as cached input every turn) but
        invisible to any transcript walk. When turn t+1 OPENS, charge turn t
        one synthetic segment hid = max(0, out(t) - visible assistant est of
        t). The LAST turn of a session gets its reasoning seg only when the
        next turn opens — acceptable, live sessions always advance."""
        if not self.turns:
            return
        t = len(self.turns) - 1
        hid = max(0, _i(self.turns[-1]["out"]) - self._vis_acc)
        if hid > 0:
            self._alloc("reasoning", hid, "reasoning-t%d" % t, self._turn_ts)

    def _tool_use(self, b, uuid, ts):
        name = b.get("name") or "?"
        inp = b.get("input")
        tid = b.get("id")
        fp = tool_file(inp)
        itok, ichars = est_pair(inp)
        if tid:
            self.tu[tid] = (name, fp)
        if name in AGENT_TOOLS:
            desc = inp.get("description") if isinstance(inp, dict) else None
            if tid:
                self.tu2agent[tid] = {"turn": self._born(), "ts": hhmmss(ts),
                                      "t0": ts_epoch(ts), "desc": desc}
            self._alloc("tool", itok, uuid, ts, chars=ichars)
        elif name == "Read" and fp:
            # addressing only: rings as file context, no file-stat change
            self._alloc("file", itok, uuid, ts, self._file_id(fp), chars=ichars)
        elif name in WRITE_TOOLS and fp:
            fid = self._file_id(fp)
            f = self.files[fid]
            op = WRITE_TOOLS[name]
            f["cum"] += itok
            if op == "w":
                f["tok"] = itok          # full copy replaces the live copy
                f["writes"] += 1
            else:
                f["tok"] += itok         # edit amends the live copy
                f["edits"] += 1
            self._file_touch(fid, ts)
            self._faccess(fid, op, itok, ts)
            self._alloc("file", itok, uuid, ts, fid, chars=ichars)
        elif name == "Bash":
            if tid and isinstance(inp, dict):
                self.tu2cmd[tid] = {"cmd": inp.get("command"),
                                    "desc": inp.get("description")}
            self._alloc("bash", itok, uuid, ts, chars=ichars)
        else:
            if tid:
                r = _ret_classify(name, inp)
                if r is not None:
                    self.tu2ret[tid] = r
            self._alloc("tool", itok, uuid, ts, chars=ichars)
        if self.turns:
            self.turns[-1]["tools"] += 1
            self.pending["turns"].add(self.turns[-1]["turn"])
        return itok

    # ---- per-category calibration ----------------------------------------
    def live_est(self):
        """Resident token estimate BEFORE alpha. With a fit active this is the
        per-category sum Σ_c chars[c]·inv[c]; otherwise the single-constant
        Σ est — the exact pre-fit number."""
        if self.fit is None:
            return self.est_live
        inv = self.fit["inv"]
        return sum(v * inv[c] for c, v in self.cat_chars.items())

    def seg_est(self, s):
        """Pre-alpha token estimate of ONE segment (the fit changes the split,
        never the total: alpha still force-fits the sum to authoritative R)."""
        if self.fit is None:
            return s["est"]
        return s["chars"] * self.fit["inv"].get(s["cat"], 0.0)

    def _maybe_refit(self):
        """Refit the per-category chars/token ratios against this session's own
        authoritative R. Cadenced (never per record, never per turn) and a pure
        function of (turns, turn_chars) so replay/seek reproduces it exactly."""
        n = min(len(self.turns) - 1, len(self.turn_chars))   # in-flight turn out
        if n < self.fit_next:
            return
        self.fit_next = n + max(FIT_REFIT_EVERY, n // 8)
        if not self.fit_on:
            self.fit_state = {"active": False, "reason": "disabled", "n": n,
                              "prior_cpt": Est.chars_per_tok}
            self.fit = None
            return
        rows = [(self.turns[i]["resident"], self.turn_chars[i])
                for i in range(n) if self.turns[i]["resident"] > 0]
        st = fit_cats(rows, base_hint=float(self.overhead0 or 0))
        self.fit_state = st
        self.fit = st if st.get("active") else None

    def fit_payload(self):
        return fit_report(self.fit_state)

    def _on_turn_usage(self, tr, ts, new_turn=False):
        R = tr["resident"]
        self.interim_R = None    # authoritative usage supersedes the boundary
        # budget auto-bump
        if not self.budget_pinned and R > self.budget:
            self._bump_budget(R, ts)
        # server context rebuild (SPEC a): R FELL >10k between turns with no
        # intervening compact_boundary — after long away gaps the server
        # rebuilds the context (cr collapses to an old prefix, accumulated
        # hidden reasoning is flushed). Only at a TRUE turn open, never on
        # same-requestId usage upserts, never right after a compaction
        # (its R drop is accounted by the compaction path) and never at
        # session start (needs a previous turn).
        if new_turn:
            if (len(self.turns) >= 2 and not self._compact_between
                    and R < self.turns[-2]["resident"] - 10_000):
                self._server_rebuild(R, self.turns[-2]["resident"], ts)
            self._compact_between = False
        # per-category ratio refit (cheap, cadenced): the split only
        if new_turn:
            self._maybe_refit()
        # overhead calibration (the honesty rule)
        if self.overhead0 is None or self.rebase_pending:
            self.overhead0 = max(0, R - self.est_live)
            if self.rebase_pending and not self.pending["map_rebuild"]:
                # the compaction-time map went out sized to interim numbers;
                # re-emit it now that the split is measured, or the interim
                # map (field-found: a giant overhead slab) sticks until the
                # next full rebuild
                self.map_rev += 1
                self.pending["map_rebuild"] = True
            self.rebase_pending = False
        E = self.live_est()
        # with a fit active the invisible overhead is the FITTED intercept
        # (bounded, so a stale intercept can never eat the whole context);
        # without one it stays the re-based overhead0 — bit-identical to
        # pre-fit amtr.
        # ...capped by overhead₀, the DIRECT measurement at the last rebase:
        # a fitted intercept is a regression parameter, not an observation, and
        # after a compaction it can go stale. Never let it claim more invisible
        # context than the honest R − Σest allowed.
        base = self.overhead0 if self.fit is None \
            else int(max(0, min(self.fit["intercept"], self.overhead0 or 0,
                                0.9 * R)))
        if base + E <= R:
            self.alpha = 1.0
            self.overhead = int(round(R - E))
        else:
            a = (R - base) / max(1, E)
            self.alpha = min(1.0, max(1e-6, a))
            self.overhead = int(base)
        # thrash signals run once per turn (streamed same-requestId records
        # must not re-trigger them after the post-compaction grace is spent)
        if tr["turn"] != self._sig_turn:
            self._sig_turn = tr["turn"]
            # thrash: prefix invalidation
            if len(self.turns) >= 2:
                prev_c = self.turns[-2]["waterline"]
                if self.post_compact_grace:
                    self.post_compact_grace = False
                elif tr["waterline"] < prev_c - 1024:
                    self._event("thrash", "warn", ts,
                                "waterline dropped %dk -> %dk (prefix invalidated)"
                                % (prev_c // 1000, tr["waterline"] // 1000))
            # thrash: sustained cache churn
            if R > 0 and tr["cc"] / R > 0.2:
                self.cc_hi_run += 1
                if self.cc_hi_run == 3:
                    self._event("thrash", "warn", ts,
                                "3 consecutive turns with cc/R > 0.2")
                    self.cc_hi_run = 0
            else:
                self.cc_hi_run = 0
        # pressure zones (0.60 amber, 0.85 red), upward transitions only
        frac = R / max(1, self.budget)
        z = 2 if frac >= 0.85 else (1 if frac >= 0.60 else 0)
        if z > self.zone:
            if z == 2:
                self._event("pressure", "error", ts,
                            "context %.0f%% of budget (red)" % (frac * 100))
            else:
                self._event("pressure", "warn", ts,
                            "context %.0f%% of budget (amber)" % (frac * 100))
        self.zone = z
        # local-backend truncation: the served window is a hard ceiling the
        # CLI does not know about — pressure on Anthropic ends in a compaction,
        # here it ends in the server silently dropping the OLDEST context.
        # Warn on the upward crossing into the margin; re-arm only after real
        # relief (compaction or rebuild pulls R back under 90%).
        ctx = (self.backend or {}).get("ctx") if isinstance(self.backend, dict) \
            else None
        if ctx and (self.backend or {}).get("loaded"):
            if R >= ctx - max(1024, ctx // 50):
                if not self._trunc_warned:
                    self._trunc_warned = True
                    self._event("truncation", "error", ts,
                                "R %dk at served window %dk — server will "
                                "truncate oldest context"
                                % (R // 1000, ctx // 1000))
            elif R < int(ctx * 0.90):
                self._trunc_warned = False

    def _bump_budget(self, need, ts):
        for rung in BUDGET_RUNGS:
            if rung >= need:
                if rung != self.budget:
                    self.pending["logs"].append(
                        "budget bumped %d -> %d" % (self.budget, rung))
                self.budget = rung
                return
        self.budget = BUDGET_RUNGS[-1]

    def _server_rebuild(self, R, prev_R, ts):
        """Server context rebuild (SPEC a): evict every reasoning segment
        (the flush is what the R drop IS), re-base overhead via the
        compaction rebase machinery, rebuild the map, emit a warn event.
        Files are untouched — reasoning segs never carry a file id."""
        gone = [sid for sid, s in self.ring.items()
                if s["cat"] == "reasoning"]
        flushed = 0
        for sid in gone:
            s = self.ring[sid]
            self.cat_est["reasoning"] -= s["est"]
            self.cat_chars["reasoning"] -= s["chars"]
            self.est_live -= s["est"]
            flushed += s["est"]
            ids = self.by_uuid.get(s["uuid"])
            if ids:
                try:
                    ids.remove(sid)
                except ValueError:
                    pass
                if not ids:
                    self.by_uuid.pop(s["uuid"], None)
            del self.ring[sid]
        self.rebase_pending = True       # overhead0 = max(0, R - Σest) next
        self.map_rev += 1
        self.pending["map_rebuild"] = True
        self.rebuilds.append({"turn": self._born(), "ts": hhmmss(ts),
                              "pre": int(prev_R), "post": int(R),
                              "flushed": int(flushed)})
        self._event("rebuild", "warn", ts,
                    "server context rebuild: R fell %dk -> %dk "
                    "(no compaction; %dk est reasoning flushed)"
                    % (prev_R // 1000, R // 1000, flushed // 1000))

    # ---- user --------------------------------------------------------------
    def _feed_user(self, d):
        m = d.get("message")
        if not isinstance(m, dict):
            return
        ts = d.get("timestamp") or ""
        uuid = d.get("uuid") or ("anon-%d" % self.rec_count)
        content = m.get("content")
        if d.get("isCompactSummary"):
            e, ch = est_pair(content)
            self._alloc("summary", e, uuid, ts, chars=ch)
            return
        if isinstance(content, str):
            self._alloc("user", est_text(content), uuid, ts,
                        chars=len(content))
            return
        if not isinstance(content, list):
            return
        tur = d.get("toolUseResult")
        for b in content:
            if isinstance(b, str):
                self._alloc("user", est_text(b), uuid, ts, chars=len(b))
                continue
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text") or ""
                cat = "attach" if "<system-reminder>" in txt else "user"
                self._alloc(cat, est_text(txt), uuid, ts, chars=len(txt))
            elif bt == "image":
                self._alloc("user", IMG_TOK, uuid, ts)
            elif bt == "tool_result":
                self._tool_result(b, tur, uuid, ts)
            else:
                e, ch = est_pair(b)
                self._alloc("user", e, uuid, ts, chars=ch)

    def _tool_result(self, b, tur, uuid, ts):
        tuid = b.get("tool_use_id")
        name, fp = self.tu.get(tuid, ("?", None))
        # Agent lifecycle first (launch/completion piggyback on tool_results)
        if name in AGENT_TOOLS and isinstance(tur, dict):
            self._agent_result(tuid, b, tur, ts)
        # token estimate: the API prompt carries the DECORATED tool_result
        # block (line-numbered for Read); toolUseResult.file.content is the
        # raw structured copy. When both exist, the LARGER is what context
        # actually pays for (audit: preferring the raw copy ran ~3.6k low
        # on a real session).
        rtok, rchars = est_pair(b.get("content"))
        if isinstance(tur, dict):
            f = tur.get("file")
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                ftok = est_text(f["content"])
                if ftok > rtok:
                    rtok, rchars = ftok, float(len(f["content"]))
        if name == "Read" and fp:
            fid = self._file_id(fp)
            f = self.files[fid]
            f["cum"] += rtok
            f["tok"] = rtok             # a read resets the live copy
            f["reads"] += 1
            self._file_touch(fid, ts)
            self._faccess(fid, "r", rtok, ts)
            self._alloc("file", rtok, uuid, ts, fid, chars=rchars)
        elif name in WRITE_TOOLS and fp:
            # ack/patch echo: resident context but not a new file copy
            self._alloc("file", rtok, uuid, ts, self._file_id(fp),
                        chars=rchars)
        elif name == "Bash":
            self._alloc("bash", rtok, uuid, ts, chars=rchars)
            self._cmd_result(tuid, b, tur, rtok, ts)
        else:
            self._alloc("tool", rtok, uuid, ts, chars=rchars)
            if tuid in self.tu2ret:
                self._ret_result(tuid, b, tur, rtok, ts)

    def _cmd_result(self, tuid, b, tur, rtok, ts):
        src = self.tu2cmd.pop(tuid, {})
        out = err = ""
        interrupted = bg = False
        if isinstance(tur, dict):
            out = tur.get("stdout") or ""
            err = tur.get("stderr") or ""
            interrupted = bool(tur.get("interrupted"))
            bg = bool(tur.get("backgroundTaskId")
                      or tur.get("assistantAutoBackgrounded"))
        elif isinstance(tur, str):
            out = tur
        else:
            out = _blocks_text(b.get("content"))
        desc = clean_text(src.get("desc") or "")[:120] or None
        entry = {"turn": self._born(), "ts": hhmmss(ts),
                 "epoch": ts_epoch(ts),
                 "cmd": head_clip(src.get("cmd") or "", 240),
                 "desc": desc,
                 "out": tail_clip(out, 600), "err": tail_clip(err, 300),
                 "ok": not b.get("is_error") and not interrupted,
                 "interrupted": interrupted, "bg": bg,
                 "tok_out": int(rtok)}
        self.cmds.append(entry)
        self.pending["cmds"].append(entry)

    def _ret_result(self, tuid, b, tur, rtok, ts):
        r = self.tu2ret.pop(tuid, {})
        n = bts = dur = None
        if isinstance(tur, dict):
            if r.get("kind") == "search":
                n = _i(tur.get("searchCount")) or None
                d = tur.get("durationSeconds")
                dur = int(d * 1000) if isinstance(d, (int, float)) else None
            elif r.get("kind") == "fetch":
                bts = _i(tur.get("bytes")) or None
                dur = _i(tur.get("durationMs")) or None
            elif r.get("kind") == "toolsearch":
                m = tur.get("matches")
                n = len(m) if isinstance(m, list) else None
        entry = {"turn": self._born(), "ts": hhmmss(ts),
                 "epoch": ts_epoch(ts),
                 "kind": r.get("kind") or "mcp",
                 "src": r.get("src") or "?",
                 "q": head_clip(r.get("q") or "", 160),
                 "n": n, "bytes": bts, "dur_ms": dur,
                 "tok": int(rtok), "ok": not b.get("is_error")}
        self.rets.append(entry)
        self.pending["rets"].append(entry)

    def _agent_result(self, tuid, b, tur, ts):
        aid = tur.get("agentId") or tuid or ("agent-%d" % self.rec_count)
        status = tur.get("status")
        launch = self.tu2agent.get(tuid, {})
        ag = self.agents.get(aid)
        if ag is None:
            ag = {"id": aid, "state": "running",
                  "agent_type": tur.get("agentType"),
                  "desc": launch.get("desc") or tur.get("description"),
                  "wf": None,
                  "turn0": launch.get("turn", self._born()),
                  "ts0": launch.get("ts", hhmmss(ts)),
                  "t0": launch.get("t0") or ts_epoch(ts),
                  "ts_last": ts_epoch(ts),
                  "turn1": None, "own_tok": 0, "ret_tok": None,
                  "tools": None, "dur_ms": None}
            self.agents[aid] = ag
        if tur.get("agentType"):
            ag["agent_type"] = tur.get("agentType")
        if status == "completed" or (status is None and tur.get("totalTokens") is not None):
            ag["state"] = "failed" if b.get("is_error") else "done"
            ag["turn1"] = self._born()
            ag["own_tok"] = _i(tur.get("totalTokens")) or ag["own_tok"]
            ag["ret_tok"] = est_obj(tur.get("content") if tur.get("content")
                                    is not None else b.get("content"))
            ag["dur_ms"] = _i(tur.get("totalDurationMs")) or None
            st = tur.get("toolStats")
            if isinstance(st, dict):
                ag["tools"] = {"r": _i(st.get("readCount")),
                               "s": _i(st.get("searchCount")),
                               "b": _i(st.get("bashCount")),
                               "e": _i(st.get("editFileCount"))}
        elif b.get("is_error"):
            ag["state"] = "failed"
            ag["turn1"] = self._born()
            ag["ret_tok"] = est_obj(b.get("content"))
        if ag["state"] in ("done", "failed") and ag.get("turn1") is not None:
            ag["ts_last"] = ts_epoch(ts) or ag.get("ts_last") or 0.0
            # a completed agent with a known duration pins its true launch time
            if ag.get("dur_ms") and ag.get("ts_last"):
                ag["t0"] = ag["ts_last"] - ag["dur_ms"] / 1000.0
        if ag["state"] == "failed":
            self._event("agent_failed", "warn", ts,
                        "agent %s failed" % (ag.get("desc") or aid))
        self.pending["agents"].add(aid)

    # ---- attachment / system ------------------------------------------------
    def _feed_attachment(self, d):
        ts = d.get("timestamp") or ""
        uuid = d.get("uuid") or ("anon-%d" % self.rec_count)
        att = d.get("attachment")
        e, ch = est_pair(att)
        self._alloc("attach", e, uuid, ts, chars=ch)
        if isinstance(att, dict) and att.get("type") == "queued_command":
            self._event("queued_prompt", "info", ts,
                        "queued: %s" % str(att.get("prompt") or "")[:80])

    def _feed_system(self, d):
        sub = d.get("subtype")
        ts = d.get("timestamp") or ""
        if sub == "compact_boundary":
            self._compact(d)
        elif sub == "api_error":
            self.api_errors += 1
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            msg = err.get("formatted") or err.get("message") or "api error"
            retry = d.get("retryInMs")
            if retry is not None:
                self.last_retry_ms = _i(retry)
                att = d.get("retryAttempt")
                mx = d.get("maxRetries")
                if att is not None and mx is not None:
                    msg = "%s (retry %s/%s in %ss)" % (
                        msg, att, mx, round(_i(retry) / 1000))
            self._event("api_error", "error", ts, str(msg)[:160])
        elif sub == "turn_duration":
            if self.turns:
                self.turns[-1]["dur_ms"] = _i(d.get("durationMs"))
                self.pending["turns"].add(self.turns[-1]["turn"])
        elif sub == "model_refusal_fallback":
            self._event("model_fallback", "warn", ts,
                        "fallback %s -> %s" % (d.get("originalModel"),
                                               d.get("fallbackModel")))
        elif sub == "stop_hook_summary":
            if d.get("preventedContinuation"):
                self._event("hook_block", "warn", ts,
                            "hook blocked continuation: %s"
                            % str(d.get("stopReason") or "")[:80])
        # other system subtypes are events, not context (~0 tokens): ignore

    # ---- compaction ----------------------------------------------------------
    def _compact(self, d):
        ts = d.get("timestamp") or ""
        cm = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}
        trigger = cm.get("trigger") if cm.get("trigger") in ("auto", "manual") else "manual"
        pre = _i(cm.get("preTokens")) or self.resident()   # providers: R at the cut
        post = _i(cm.get("postTokens"))
        dur = _i(cm.get("durationMs"))
        # survivors
        surv = None
        pm = cm.get("preservedMessages")
        if isinstance(pm, dict) and isinstance(pm.get("allUuids"), list):
            surv = set(u for u in pm["allUuids"] if isinstance(u, str))
        if surv is None:
            seg = cm.get("preservedSegment")
            if isinstance(seg, dict) and (seg.get("headUuid") or seg.get("tailUuid")):
                # anchor on record ARRIVAL order, not the seg ring — the head
                # record may have produced no allocation and must still anchor
                hi = self.uuid_order.get(seg.get("headUuid"))
                ti = self.uuid_order.get(seg.get("tailUuid"))
                if hi is None and ti is None:
                    self.pending["logs"].append(
                        "compaction preservedSegment anchors unknown; keeping nothing")
                else:
                    lo = hi if hi is not None else 0
                    hi2 = ti if ti is not None else max(self.uuid_order.values())
                    if lo > hi2:
                        lo, hi2 = hi2, lo
                    surv = set(u for u, i in self.uuid_order.items()
                               if lo <= i <= hi2)
        if surv is None:
            surv = set()  # keep nothing
        # evict
        dropped_cats = {}
        dropped_files = {}
        evicted_est = 0
        gone = []
        for sid, s in self.ring.items():
            if s["uuid"] in surv:
                continue
            gone.append(sid)
            dropped_cats[s["cat"]] = dropped_cats.get(s["cat"], 0) + s["est"]
            if s["file"] is not None:
                dropped_files[s["file"]] = dropped_files.get(s["file"], 0) + s["est"]
            self.cat_est[s["cat"]] -= s["est"]
            self.cat_chars[s["cat"]] -= s["chars"]
            self.est_live -= s["est"]
            evicted_est += s["est"]
        for sid in gone:
            uuid = self.ring[sid]["uuid"]
            ids = self.by_uuid.get(uuid)
            if ids:
                try:
                    ids.remove(sid)
                except ValueError:
                    pass
                if not ids:
                    self.by_uuid.pop(uuid, None)
            del self.ring[sid]
        # files: resident iff any live seg still references them
        live_files = set(s["file"] for s in self.ring.values() if s["file"] is not None)
        for fid, f in self.files.items():
            was = f["resident"]
            f["resident"] = fid in live_files
            if was != f["resident"]:
                self.pending["files"].add(fid)
        if post <= 0 and pre > 0:
            # no authoritative post size (Gemini says nothing; Codex only the
            # kept history): the evicted estimate is the best honest number
            post = max(0, pre - int(evicted_est * self.alpha))
        dropped = max(0, pre - post)
        cum = _i(cm.get("cumulativeDroppedTokens"))
        self.cum_dropped = cum if cum else self.cum_dropped + dropped
        # cross-check estimate vs authority
        scaled = int(evicted_est * self.alpha)
        if dropped > 0 and abs(scaled - dropped) > 0.25 * dropped:
            self.pending["logs"].append(
                "compaction cross-check: est dropped %d vs authoritative %d"
                % (scaled, dropped))
        # budget bump first so T_auto is refined against the new rung
        if not self.budget_pinned and pre > self.budget:
            self._bump_budget(pre, ts)
        if trigger == "auto" and self.budget > 0:
            frac = pre / self.budget
            if frac > self.t_auto:
                self.t_auto = min(0.99, frac)
        self.rebase_pending = True
        self.interim_R = post if post > 0 else None
        if self.interim_R is not None:
            # interim overhead by the same rule the rebase will apply
            # (overhead = R − E), so the cut-time map/legend split stays
            # sensible until the next usage record re-measures
            self.overhead = max(0, self.interim_R
                                - int(self.est_live * self.alpha))
        self.post_compact_grace = True
        self._compact_between = True     # this R drop is NOT a server rebuild
        self.zone = 0
        self.map_rev += 1
        self.pending["map_rebuild"] = True
        top = sorted(dropped_files.items(), key=lambda kv: -kv[1])[:16]
        comp = {"n": len(self.compactions) + 1, "turn": self._born(),
                "ts": hhmmss(ts), "trigger": trigger, "pre": pre, "post": post,
                "dropped": dropped, "cum_dropped": self.cum_dropped,
                "dur_ms": dur,
                "dropped_cats": {k: int(v) for k, v in dropped_cats.items()},
                "dropped_files": [{"file": f, "tok": int(t)} for f, t in top],
                "preserved_msgs": len(surv)}
        self.compactions.append(comp)
        self.pending["compactions"].append(comp)
        self._event("compaction", "warn", ts,
                    "%s compaction: %dk -> %dk (dropped %dk)"
                    % (trigger, pre // 1000, post // 1000, dropped // 1000))

    # ---- map ------------------------------------------------------------------
    def resident(self):
        return self.turns[-1]["resident"] if self.turns else 0

    def build_map_segs(self, cap=1024):
        R = self.resident()
        if self.interim_R is not None:
            # pre-cut R would dump the whole dropped span into the overhead
            # seg via the sum-to-R correction below
            R = min(R, self.interim_R)
        oh = {"id": 0, "cat": "overhead", "tok": int(self.overhead),
              "file": None, "born": 0, "ts": self.started_epoch}
        segs = [oh]
        total = oh["tok"]
        for s in self.ring.values():
            tok = int(self.seg_est(s) * self.alpha)
            if tok <= 0:
                continue
            segs.append({"id": s["id"], "cat": s["cat"], "tok": tok,
                         "file": s["file"], "born": s["born"], "ts": s["ts"]})
            total += tok
        diff = R - total          # rounding correction: sum to exactly R
        if diff >= 0:
            oh["tok"] += diff
        else:
            i = len(segs) - 1
            while diff < 0 and i >= 0:
                take = min(segs[i]["tok"], -diff)
                segs[i]["tok"] -= take
                diff += take
                i -= 1
            segs = [s for s in segs if s["tok"] > 0 or s["cat"] == "overhead"]
        return self._merge_segs(segs, cap)

    @staticmethod
    def _merge_segs(segs, cap):
        if len(segs) <= cap:
            return segs
        out = [segs[0]]
        for s in segs[1:]:      # pass 1: coalesce adjacent same-cat/same-file
            p = out[-1]
            if p["cat"] == s["cat"] and p["file"] == s["file"] and p["cat"] != "overhead":
                p["tok"] += s["tok"]
                p["ts"] = max(p["ts"], s["ts"])
                p["born"] = min(p["born"], s["born"])
            else:
                out.append(s)
        thr = max(1, sum(x["tok"] for x in out) // cap)
        while len(out) > cap:   # threshold passes: merge small neighbours
            nxt = [out[0]]
            for s in out[1:]:
                p = nxt[-1]
                if p["cat"] != "overhead" and p["tok"] + s["tok"] <= thr:
                    if s["tok"] > p["tok"]:          # plurality owner wins
                        p["cat"], p["file"] = s["cat"], s["file"]
                    p["tok"] += s["tok"]
                    p["ts"] = max(p["ts"], s["ts"])
                    p["born"] = min(p["born"], s["born"])
                else:
                    nxt.append(s)
            if len(nxt) == len(out):
                thr *= 2
            out = nxt
        return out

    # ---- payload builders -------------------------------------------------------
    def _excerpt_for(self, d, cat):
        """Best-effort extraction of the text a segment's tokens represent."""
        t = d.get("type")
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = m.get("content")
        parts = []
        if t == "attachment":
            parts.append(json.dumps(d.get("attachment"), ensure_ascii=False,
                                    indent=1))
        elif isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if cat == "assistant" and bt == "text":
                    parts.append(b.get("text") or "")
                elif cat == "thinking" and bt == "thinking":
                    parts.append(b.get("thinking") or "")
                elif bt == "tool_use":
                    parts.append("%s %s" % (b.get("name"),
                                            json.dumps(b.get("input"),
                                                       ensure_ascii=False)))
                elif bt == "tool_result":
                    c = b.get("content")
                    parts.append(c if isinstance(c, str) else _blocks_text(c))
                elif bt == "text" and cat in ("user", "summary", "attach"):
                    parts.append(b.get("text") or "")
        # file segments: the structured toolUseResult carries the exact copy
        tur = d.get("toolUseResult")
        if cat == "file" and isinstance(tur, dict):
            f = tur.get("file")
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                parts = [f["content"]]
        txt = "\n".join(x for x in parts if x)
        return txt or json.dumps(d, ensure_ascii=False)

    def peek_payload(self, sid):
        """INSPECT-mode content lookup (SPEC b/c `peek`): re-read the
        segment's record from disk; the one sanctioned, bounded exception
        to the no-content wire rule."""
        base = {"seg": int(sid), "found": False, "cat": "?", "kind": None,
                "uuid": None, "born": 0, "est": 0, "tok": 0, "file": None,
                "excerpt": "", "truncated": False}
        if sid == 0:
            txt = ("Request preamble the transcript omits: the system "
                   "prompt, tool schemas, skill listings and MCP "
                   "instructions. Measured honestly as R minus everything "
                   "visible (re-based at each compaction).")
            if self.backend:
                # local backend: the preamble IS observable — on the wire
                item = None
                rec = latest_proxy_record(self.model)
                if rec:
                    item = proxy_itemization(rec)
                txt += "\n\n" + (item or (
                    "The wire has it: run `amtr_engine.py --proxy "
                    "--upstream %s` and point ANTHROPIC_BASE_URL at "
                    "127.0.0.1:11435 to itemize this from the real "
                    "request bytes." % self.backend.get("url", "...")))
            base.update({
                "found": True, "cat": "overhead", "kind": "overhead",
                "tok": int(self.overhead), "excerpt": txt})
            return base
        seg = self.ring.get(sid)
        if seg is None:
            return base
        base.update({"cat": seg["cat"], "uuid": seg["uuid"],
                     "born": int(seg["born"]), "est": int(seg["est"]),
                     "tok": int(self.seg_est(seg) * self.alpha),
                     "file": seg["file"]})
        if seg["cat"] == "reasoning":
            # synthetic segment: its uuid names no transcript record, so
            # answer BEFORE the disk lookup with the explainer
            base.update({
                "found": True, "kind": "reasoning",
                "excerpt": ("Hidden reasoning generated at turn %d. "
                            "Extended-thinking models emit encrypted "
                            "signature-only thinking blocks: the reasoning "
                            "tokens stay resident server-side and are "
                            "re-billed as cached input every turn, but "
                            "never appear in the transcript. Measured as "
                            "that turn's output_tokens minus its visible "
                            "content (text + thinking + tool inputs)."
                            % int(seg["born"]))})
            return base
        idx = self.uuid_order.get(seg["uuid"])
        if idx is None or idx - 1 >= len(self.rec_offsets):
            return base
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.rec_offsets[idx - 1])
                line = fh.readline()
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return base
        if not isinstance(d, dict):
            return base
        raw = clean_text(self._excerpt_for(d, seg["cat"]))
        base.update({"found": True, "kind": d.get("type"),
                     "excerpt": raw[:2000],
                     "truncated": len(raw) > 2000})
        return base

    def turn_at_epoch(self, e):
        """Turn index active at epoch e — anchors dir-discovered agents to
        their real launch turn instead of the attach turn."""
        if not e or not self.turn_epochs:
            return max(0, len(self.turns) - 1)
        import bisect
        i = bisect.bisect_right(self.turn_epochs, e) - 1
        return max(0, min(i if i >= 0 else 0, len(self.turns) - 1))

    def turn_payload(self, i):
        t = self.turns[i]
        cost = (t["in"] * 1.0 + t["cr"] * 0.1 + t["cc_5m"] * 1.25
                + t["cc_1h"] * 2.0 + t["out"] * 5.0) / 1000.0
        hit = t["cr"] / max(1, t["cr"] + t["cc"] + t["in"])
        p = dict(t)
        p["cost_u"] = round(cost, 1)
        p["hit"] = round(hit, 4)
        return p

    def file_payload(self, fid):
        f = self.files[fid]
        return {"id": f["id"], "path": f["path"], "tok": int(f["tok"]),
                "reads": f["reads"], "writes": f["writes"], "edits": f["edits"],
                "waste": int(f["waste"]), "last_ts": f["last_ts"],
                "last_epoch": round(float(f.get("last_epoch") or 0.0), 3),
                "resident": f["resident"]}

    def cats_payload(self):
        out = {"overhead": int(self.overhead)}
        if self.fit is None:
            for c, v in self.cat_est.items():
                out[c] = int(v * self.alpha)
        else:
            inv = self.fit["inv"]
            for c, v in self.cat_chars.items():
                out[c] = int(v * inv[c] * self.alpha)
        return out

    def agent_payload(self, aid, a=None):
        if a is None:
            a = self.agents[aid]
        p = {"id": a["id"], "state": a["state"], "turn0": a["turn0"],
             "ts0": a["ts0"], "own_tok": int(a["own_tok"]),
             "t0": round(float(a.get("t0") or 0.0), 3),
             "ts_last": round(float(a.get("ts_last") or 0.0), 3)}
        for k in ("agent_type", "desc", "wf", "path", "turn1", "ret_tok", "tools",
                  "dur_ms", "map"):
            if a.get(k) is not None:
                p[k] = a[k]
        return p

    def map_payload(self):
        segs = self.build_map_segs()
        self.map_base_n = len(segs)          # rebuild resets the cadence counter
        self.map_adds_since = 0
        return {"rev": self.map_rev, "alpha": round(self.alpha, 4),
                "fit": self.fit_payload(), "segs": segs}

    def display_name(self):
        """The distinct handle: roster/memorable name for Claude Code; the
        project's basename + provider tag for other CLIs (matches the fleet
        rows, so the picker and the ribbon agree)."""
        if self.provider == "claude":
            return session_name(self.session_id)
        tag = {"codex": "cx", "gemini": "gm"}.get(self.provider, self.provider)
        base = os.path.basename((self.project or "").rstrip("/"))
        return "%s-%s" % (base or memorable_name(self.session_id), tag)

    def meta_payload(self):
        return {"session_id": self.session_id, "path": self.path,
                "attach_gen": getattr(self, "attach_gen", 0),
                "name": self.display_name(),
                "provider": self.provider,
                "project": self.project or "", "title": self.title,
                "model": self.model or "?", "budget": int(self.budget),
                "backend": self.backend,
                "t_auto": round(self.t_auto, 4), "cc_version": self.cc_version,
                "started_at": self.started_at}

    def backfill_payload(self):
        turns = [self.turn_payload(i) for i in
                 range(max(0, len(self.turns) - 512), len(self.turns))]
        return {"turns": turns,
                "faccess": list(self.faccess),
                "cmds": list(self.cmds),
                "rets": list(self.rets),
                "compactions": list(self.compactions),
                "agents": [self.agent_payload(a) for a in self.agents],
                "events": list(self.events)}

    # ---- report aggregations (SPEC f) -------------------------------------
    # Sums over the accounting above. The report is a RENDERING of the same
    # Session state the live instrument streams — nothing here re-derives
    # anything from the transcript. self.turns is the FULL ledger (only the
    # backfill wire copy is capped), so these totals cover every turn.

    def peak_resident(self):
        """(peak R, turn it happened on) across the whole session."""
        best_r, best_t = 0, 0
        for t in self.turns:
            if t["resident"] > best_r:
                best_r, best_t = t["resident"], t["turn"]
        return best_r, best_t

    def usage_totals(self):
        tot = {k: 0 for k in ("in", "cr", "cc", "cc_5m", "cc_1h", "out")}
        for t in self.turns:
            for k in tot:
                tot[k] += _i(t[k])
        # same law as the per-turn hit: cr / (cr + cc + in)
        tot["hit"] = round(tot["cr"] / max(1, tot["cr"] + tot["cc"]
                                           + tot["in"]), 4)
        return tot

    def cost_stats(self):
        costs = [self.turn_payload(i)["cost_u"]
                 for i in range(len(self.turns))]
        if not costs:
            return {"total": 0.0, "mean": 0.0, "p95": 0.0}
        total = round(sum(costs), 1)
        p95 = sorted(costs)[min(len(costs) - 1,
                                max(0, int(math.ceil(0.95 * len(costs))) - 1))]
        return {"total": total, "mean": round(total / len(costs), 1),
                "p95": p95}

    def model_totals(self):
        rows, by = [], {}
        for i, t in enumerate(self.turns):
            m = t["model"] or "?"
            d = by.get(m)
            if d is None:
                d = by[m] = {"model": m, "turns": 0, "in": 0, "cr": 0,
                             "cc": 0, "out": 0, "cost_u": 0.0}
                rows.append(d)
            d["turns"] += 1
            for k in ("in", "cr", "cc", "out"):
                d[k] += _i(t[k])
            d["cost_u"] += self.turn_payload(i)["cost_u"]
        for d in rows:
            d["cost_u"] = round(d["cost_u"], 1)
        return rows

    def cmd_totals(self):
        cs = list(self.cmds)
        return {"n": len(cs),
                "ok": sum(1 for c in cs if c["ok"]),
                "failed": sum(1 for c in cs
                              if not c["ok"] and not c["interrupted"]),
                "interrupted": sum(1 for c in cs if c["interrupted"]),
                "bg": sum(1 for c in cs if c["bg"]),
                "tok_out": sum(_i(c["tok_out"]) for c in cs)}

    def ret_totals(self):
        by_kind, by_src, fails = {}, {}, []
        for r in self.rets:
            for key, bucket in (("kind", by_kind), ("src", by_src)):
                d = bucket.setdefault(r[key], {key: r[key], "n": 0, "tok": 0})
                d["n"] += 1
                d["tok"] += _i(r["tok"])
            if not r.get("ok", True):
                fails.append(r)
        return {"n": len(self.rets),
                "tok": sum(_i(r["tok"]) for r in self.rets),
                "by_kind": list(by_kind.values()),
                "by_src": list(by_src.values()),
                "failures": fails}

    def agent_totals(self):
        ags = list(self.agents.values())
        counts = {}
        for a in ags:
            counts[a["state"]] = counts.get(a["state"], 0) + 1
        own = sum(_i(a["own_tok"]) for a in ags)
        rets = sum(_i(a["ret_tok"]) for a in ags
                   if a.get("ret_tok") is not None)
        amps = sorted(_i(a["own_tok"]) / max(1, _i(a["ret_tok"]))
                      for a in ags if a.get("ret_tok") is not None)
        med = 0.0
        if amps:
            mid = len(amps) // 2
            med = amps[mid] if len(amps) % 2 else (amps[mid - 1]
                                                   + amps[mid]) / 2
        # ×main is the AGENTS-tab header law: Σown_tok vs current resident R
        return {"n": len(ags), "counts": counts, "own_tok": int(own),
                "ret_tok": int(rets),
                "x_main": round(own / max(1, self.resident()), 2),
                "amp_median": round(med, 1)}

    # ---- seek / replay ------------------------------------------------------------
    def state_at_turn(self, t, lock=None):
        """Clone of this session's state at the end of 0-based turn t, built
        from the nearest checkpoint <= t and a forward replay from disk.
        If `lock` is given, it is held only for the checkpoint pick + clone,
        never during the disk replay (live tail is never paused)."""
        target = max(0, int(t)) + 1     # turn_count to reach
        if lock is not None:
            lock.acquire()
        try:
            base = None
            for tc, rc, snap in self.checkpoints:
                if tc <= target and (base is None or tc > base[0]):
                    base = (tc, rc, snap)
            if base is not None:
                clone = base[2].clone()
                start_rec = base[1]
            else:
                clone = Session(self.path, budget=self.budget,
                                budget_pinned=self.budget_pinned,
                                t_auto=0.85, ckpt_every=self.ckpt_every,
                                sidechain_ok=self.sidechain_ok,
                                provider=self.provider)
                clone._no_ckpt = True
                start_rec = 0
            rec_total = self.rec_count
            off = self.rec_offsets[start_rec] \
                if start_rec < len(self.rec_offsets) else None
        finally:
            if lock is not None:
                lock.release()
        if off is None:
            return clone
        try:
            with open(self.path, "rb") as fh:
                fh.seek(off)
                buf = b""
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = buf[:nl]
                        buf = buf[nl + 1:]
                        if not line.strip():
                            continue
                        try:
                            dd = json.loads(line.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if not isinstance(dd, dict):
                            continue
                        clone.rec_offsets.append(0)
                        clone.rec_count += 1
                        for rec in clone.translate(dd):
                            if len(clone.turns) >= target and clone.is_new_turn(rec):
                                clone.pending = _fresh_pending()
                                return clone
                            u = rec.get("uuid")
                            if isinstance(u, str) and u not in clone.uuid_order:
                                clone.uuid_order[u] = clone.rec_count
                            try:
                                clone.feed_obj(rec)
                            except Exception:
                                pass
                        if clone.rec_count >= rec_total and \
                                len(clone.turns) >= target:
                            clone.pending = _fresh_pending()
                            return clone
        except OSError as e:
            log("seek replay read failed: %s" % e)
        clone.pending = _fresh_pending()
        return clone

# ---------------------------------------------------------------- discovery
CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")
TASKS_DIR = os.path.join(CLAUDE_DIR, "tasks")
HISTORY_PATH = os.path.join(CLAUDE_DIR, "history.jsonl")
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")

def project_slug(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")

# a stable, readable, distinct handle per session — always present (offline or
# live) so amtr sessions never blur together. adjective-noun from the uuid.
_NAME_ADJ = ("amber azure brisk calm coral crisp dusky eager fleet gilded hazel "
             "ivory jade keen lunar mossy nimble ochre plush quiet russet slate "
             "teal umber vivid warm zesty bold clay dawn ember frost").split()
_NAME_NOUN = ("otter canyon ember falcon grove harbor inlet jetty kestrel lagoon "
              "meadow nimbus onyx pier quartz ridge summit tarn vale willow yarrow "
              "zephyr arch beacon cove delta fern glade heron isle koi lark").split()


def memorable_name(sid):
    h = zlib.crc32((sid or "").encode())
    return "%s-%s" % (_NAME_ADJ[h % len(_NAME_ADJ)],
                      _NAME_NOUN[(h // len(_NAME_ADJ)) % len(_NAME_NOUN)])


def session_name(session_id):
    """The distinct display name for a session: the live roster name (a custom
    name you set, like 'allboutRAG', or a derived 'project-hash') when running,
    else a stable memorable handle from the uuid."""
    e = _roster_entry(session_id)
    if e and isinstance(e.get("name"), str) and e["name"]:
        return e["name"]
    return memorable_name(session_id)

def find_transcript(session_id):
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    return hits[0] if hits else None

def newest_transcript(project=None):
    roots = []
    if project:
        # realpath (not abspath) so /tmp/x resolves to /private/tmp/x — the
        # real path Claude Code slugs the transcript under (macOS symlink)
        roots = [os.path.join(PROJECTS_DIR, project_slug(os.path.realpath(project)))]
    elif os.path.isdir(PROJECTS_DIR):
        roots = [os.path.join(PROJECTS_DIR, d) for d in os.listdir(PROJECTS_DIR)]
    best, bt = None, 0.0
    for r in roots:
        if not os.path.isdir(r):
            continue
        try:
            names = os.listdir(r)
        except OSError:
            continue
        for f in names:
            if f.endswith(".jsonl"):
                p = os.path.join(r, f)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > bt:
                    best, bt = p, mt
    return best

def default_budget():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            model = str((json.load(fh) or {}).get("model") or "")
        return BUDGET_RUNGS[1] if "[1m]" in model else BUDGET_RUNGS[0]
    except Exception:
        return BUDGET_RUNGS[0]

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False

def build_agent_map(path, budget):
    """Build the OVERVIEW context-map for a subagent's OWN sidechain
    transcript (SPEC b `agent.map`). The agent's records ARE its main
    conversation from its own point of view, so parse them through a fresh
    Session with `sidechain_ok=True` and lay out `build_map_segs()`.

    Returns `{"resident": int, "budget": int, "segs": [{cat, tok, file}]}`
    or None for a missing / empty / unparseable transcript (the caller then
    omits `map`, wire-null). Pure (no protocol emission)."""
    try:
        sess = Session(path, budget=budget, sidechain_ok=True)
        off = 0
        with open(path, "rb") as fh:
            for raw in fh:
                sess.feed_line(raw.decode("utf-8", "replace"), off)
                off += len(raw)
    except Exception:
        return None
    if not sess.turns or sess.resident() <= 0:
        return None
    segs = [{"cat": s["cat"], "tok": int(s["tok"]), "file": s["file"]}
            for s in sess.build_map_segs() if int(s["tok"]) > 0]
    if not segs:
        return None
    return {"resident": int(sess.resident()), "budget": int(sess.budget),
            "segs": segs}


def tail_usage(path, span=65536):
    """(resident tokens, model) of the newest non-synthetic assistant usage,
    from a bounded backward read. Returns (int, str) or (None, "")."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - span))
            data = fh.read(span + 1)
    except OSError:
        return None, ""
    best, model = None, ""
    for line in data.split(b"\n"):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        m = d.get("message")
        if not isinstance(m, dict) or (m.get("model") or "") == "<synthetic>":
            continue
        # message.id fallback: local backends write no requestId (same rule
        # as _feed_assistant)
        if not (d.get("requestId") or m.get("id")):
            continue
        u = m.get("usage")
        if isinstance(u, dict):
            best = (_i(u.get("input_tokens")) + _i(u.get("cache_read_input_tokens"))
                    + _i(u.get("cache_creation_input_tokens")))
            model = m.get("model") or model
    return best, model

def history_last_prompts(span=65536):
    """sessionId -> last prompt display, from the tail of ~/.claude/history.jsonl."""
    out = {}
    try:
        size = os.path.getsize(HISTORY_PATH)
        with open(HISTORY_PATH, "rb") as fh:
            fh.seek(max(0, size - span))
            data = fh.read(span + 1)
    except OSError:
        return out
    for line in data.split(b"\n"):
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("sessionId"), str):
            disp = d.get("display")
            if isinstance(disp, str):
                out[d["sessionId"]] = disp[:120]
    return out

_PREVIEW_SKIP = ("<command-", "<local-command", "<system-reminder", "<task-notification")

def _provider_tail_msgs(provider, lines, max_msgs):
    """Quicklook tail for Codex (event_msg user_message / agent_message) and
    Gemini (message records, upsert by id — the last version wins)."""
    out = []
    if provider == "codex":
        # response_item messages are the version-stable source: role:user
        # minus the harness's injected context, role:assistant prose
        for line in lines:
            if b'"response_item"' not in line:
                continue
            try:
                d = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            pl = d.get("payload") if isinstance(d, dict) else None
            if not isinstance(pl, dict) or pl.get("type") != "message":
                continue
            role = txt = None
            if pl.get("role") == "user":
                t = codex_user_text(pl)
                if t is not None:
                    role, txt = "user", clean_text(t).strip()
            elif pl.get("role") == "assistant":
                role = "assistant"
                txt = clean_text("\n".join(
                    c.get("text") or "" for c in (pl.get("content") or [])
                    if isinstance(c, dict))).strip()
            if not txt:
                continue
            if out and out[-1]["role"] == role:
                out[-1]["text"] = (out[-1]["text"] + "\n" + txt)[:700]
            else:
                out.append({"role": role, "text": txt[:700]})
        return out[-max_msgs:]
    if provider == "gemini":
        seen = {}
        order = []
        for line in lines:
            if b'"id"' not in line:
                continue
            try:
                d = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(d, dict) or not isinstance(d.get("id"), str):
                continue
            role = {"user": "user", "gemini": "assistant"}.get(d.get("type"))
            if not role:
                continue
            txt = clean_text(_gemini_parts_text(d.get("content"))).strip()
            if d["id"] not in seen:
                order.append(d["id"])
            seen[d["id"]] = (role, txt)
        for mid in order:
            role, txt = seen[mid]
            if not txt:
                continue
            if out and out[-1]["role"] == role:
                out[-1]["text"] = (out[-1]["text"] + "\n" + txt)[:700]
            else:
                out.append({"role": role, "text": txt[:700]})
        return out[-max_msgs:]
    return out


def transcript_tail_msgs(path, max_msgs=12, span=262144):
    """The conversation tail of a transcript, for the fleet quicklook
    (`fleet_peek`): the last user/assistant TEXT messages, oldest first,
    [{"role","text"}]. Tool results, meta records and harness wrappers
    (command echoes, system reminders) are skipped; consecutive same-role
    records (streamed assistant chunks) merge into one message. None on an
    unreadable file. Provider transcripts (Codex, Gemini) go through
    _provider_tail_msgs."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - span))
            data = fh.read(span + 1)
    except OSError:
        return None
    lines = data.split(b"\n")
    if size > span and lines:
        lines = lines[1:]                      # drop the partial first line
    prov = detect_provider(path, first_line="" if size > span else
                           lines[0].decode("utf-8", "replace") if lines else "")
    if prov != "claude":
        return _provider_tail_msgs(prov, lines, max_msgs)
    out = []
    for line in lines:
        if b'"type"' not in line:
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("isMeta"):
            continue
        role = d.get("type")
        if role not in ("user", "assistant"):
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        blocks = [c] if isinstance(c, str) else [
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        ] if isinstance(c, list) else []
        # harness wrappers (command echoes, system reminders) are filtered
        # per BLOCK — a real prompt often shares its record with a reminder
        parts = [t for t in (clean_text(b).strip() for b in blocks)
                 if t and not (role == "user" and t.startswith(_PREVIEW_SKIP))]
        txt = "\n".join(parts).strip()
        if not txt:
            continue                           # tool-result / tool-use / wrapper only
        if out and out[-1]["role"] == role:
            out[-1]["text"] = (out[-1]["text"] + "\n" + txt)[:700]
        else:
            out.append({"role": role, "text": txt[:700]})
    return out[-max_msgs:]


def scan_roster():
    """~/.claude/sessions/<pid>.json entries, pid-verified."""
    entries = []
    for p in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json"))):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("sessionId"), str):
            continue
        d["_alive"] = pid_alive(d.get("pid"))
        entries.append(d)
    return entries

def codex_tail_parse(lines):
    """Status/usage from the tail lines of a Codex CLI rollout (SPEC f2
    providers). Codex writes explicit task_started/task_complete events, so
    busy/idle is event-precise; token_count carries the last request's
    input size (the resident analog) and the model context window."""
    status = "idle"
    res = bud = prompt = total = None
    last_start = last_done = ""
    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        p = d.get("payload") or {}
        if d.get("type") == "response_item":
            # the human's prompt (0.146 also mirrored it as event_msg
            # user_message; 0.147 as item_completed — response_item is the
            # version-stable source)
            t = codex_user_text(p) if isinstance(p, dict) else None
            if t and t.strip():
                prompt = t.strip()[:200]
            continue
        if d.get("type") != "event_msg":
            continue
        pt = p.get("type")
        ts = d.get("timestamp") or ""
        if pt == "task_started":
            last_start = ts
        elif pt in ("task_complete", "turn_aborted", "error"):
            last_done = ts
        elif pt == "token_count":
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            if isinstance(last.get("input_tokens"), int):
                res = last["input_tokens"]
            if isinstance(info.get("model_context_window"), int):
                bud = info["model_context_window"]
            tot = info.get("total_token_usage") or {}
            if isinstance(tot.get("total_tokens"), int):
                total = tot["total_tokens"]
        elif pt == "user_message":
            m = p.get("message")
            if isinstance(m, str) and m.strip():
                prompt = m.strip()[:200]
    if last_start and last_start > last_done:
        status = "busy"
    return {"status": status, "resident": res, "budget": bud,
            "last_prompt": prompt, "total": total,
            "ended": bool(last_done and last_done >= last_start)}


def gemini_tail_parse(lines):
    """Status/usage from the tail of a Gemini CLI session recording. Gemini
    writes no task events, so busy/idle is inferred from the last message:
    a `user` record (or a `gemini` record without its tokens yet) means the
    model is working; tokens.input is the resident analog; the model names
    the context window."""
    last_type = None
    last_tokens = None
    res = prompt = model = None
    seen_tokens = set()
    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            continue
        t = d.get("type")
        if t == "user":
            last_type = "user"
            last_tokens = None
            txt = _gemini_parts_text(d.get("content"))
            if txt.strip():
                prompt = txt.strip()[:200]
        elif t == "gemini":
            last_type = "gemini"
            tok = d.get("tokens") if isinstance(d.get("tokens"), dict) else None
            last_tokens = tok
            if tok is not None:
                seen_tokens.add(d["id"])
                if isinstance(tok.get("input"), int):
                    res = tok["input"]
            if isinstance(d.get("model"), str) and d["model"]:
                model = d["model"]
    busy = last_type == "user" or (last_type == "gemini" and last_tokens is None)
    return {"status": "busy" if busy else "idle", "resident": res,
            "budget": gemini_token_limit(model) if model else None,
            "last_prompt": prompt, "model": model}


def fleet_row_status(raw, alive, mtime, now):
    """Status of one fleet row. Roster value verbatim, except: a dead pid is
    `dead`, and `busy` with no transcript growth for >120 s is `stalled`
    (SPEC a health rule applied fleet-wide — for unattached sessions mtime is
    the only growth signal we have). mtime 0 (transcript unknown) never
    stalls: absence of evidence is not staleness."""
    if not alive:
        return "dead"
    st = raw or "idle"
    if st == "busy" and mtime and now - mtime > 120:
        return "stalled"
    return st

def fleet_budget(base, resident):
    """Per-session budget for a fleet row: the engine's base budget, bumped to
    the next rung that fits the session's OWN resident (the SPEC a auto-bump
    applied per row). Without this a 1M session in a mixed fleet renders
    against the 200k global window."""
    if not resident or resident <= base:
        return base
    for r in BUDGET_RUNGS:
        if r >= resident:
            return max(base, r)
    return max(base, BUDGET_RUNGS[-1])

# ---------------------------------------------------- local-backend probe
# A session whose model is not an Anthropic name (e.g. `ollama launch
# claude --model qwen3.8`) is served by a local/proxied backend. The
# transcript cannot say WHAT serves it, but the machine can: the claude
# process's env carries ANTHROPIC_BASE_URL, and an Ollama server answers
# /api/ps with the loaded model's parameter size, quantization, and its
# EFFECTIVE context window — which is the session's true budget. Everything
# here is asked, never guessed: no answer -> no backend shown.

def _env_kv(tokens):
    """NAME=VALUE pairs from ps/environ token streams (values are URLs or
    model names — never contain spaces, so token-wise parsing is safe)."""
    out = {}
    for t in tokens:
        if "=" in t and re.match(r"^[A-Z][A-Z0-9_]*=", t):
            k, _, v = t.partition("=")
            out[k] = v
    return out

_MODEL_ENV_KEYS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL")

def _proc_env(pid):
    """Env of a same-user process: /proc on Linux, `ps -wwE` on macOS."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            return _env_kv(fh.read().decode("utf-8", "replace").split("\0"))
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-wwE", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return _env_kv(out.split())
    except Exception:
        return {}

def _base_url_for_model(model):
    """The ANTHROPIC_BASE_URL of the claude process that serves `model`.
    Joined on the model NAME (env hints or --model arg), not the session id:
    the transcript is the only place the session id lives, and no process
    advertises it. A lone base_url-bearing claude process wins by default."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    weak = None
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        head = cmd.split()[0] if cmd.split() else ""
        if os.path.basename(head) != "claude" or " --bg-" in cmd:
            continue
        env = _proc_env(pid)
        url = env.get("ANTHROPIC_BASE_URL")
        if not url:
            continue
        hints = [env[k] for k in _MODEL_ENV_KEYS if env.get(k)]
        m = re.search(r"--model[ =](\S+)", cmd)
        if m:
            hints.append(m.group(1))
        if model in hints:
            return url                      # strong join: named our model
        weak = weak or url
    return weak

def _http_json(url, payload=None, timeout=2.5):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _ollama_pick(models, model):
    """The entry serving `model`: exact name, then tag-stripped name."""
    want = model.split(":")[0]
    for m in models or []:
        name = m.get("name") or m.get("model") or ""
        if name == model or name.split(":")[0] == want:
            return m
    return None

def _backend_from_entry(url, entry, loaded):
    d = entry.get("details") or {}
    info = {"kind": "ollama", "url": url,
            "params": d.get("parameter_size") or "",
            "quant": d.get("quantization_level") or "",
            "ctx": None, "loaded": loaded}
    for k in ("size", "size_vram"):
        v = entry.get(k)
        if isinstance(v, int) and v > 0:
            info[k] = v
    ctx = entry.get("context_length")
    if not ctx:
        # /api/show: model_info carries "<family>.context_length" — the
        # model's MAX window, not the served one; better than nothing
        mi = entry.get("model_info") or {}
        for k, v in mi.items():
            if k.endswith(".context_length"):
                ctx = v
                break
    if isinstance(ctx, int) and ctx > 0:
        info["ctx"] = ctx
    return info

def probe_local_backend(model, url=None):
    """Identity of the local backend serving `model`, or None. Only the
    /api/ps `context_length` is the served (budget-true) window."""
    url = (url or _base_url_for_model(model)
           or "http://localhost:11434").rstrip("/")
    try:
        if "version" not in _http_json(url + "/api/version"):
            return None
        entry = _ollama_pick(_http_json(url + "/api/ps").get("models"), model)
        if entry:
            return _backend_from_entry(url, entry, True)
        entry = _http_json(url + "/api/show", {"model": model})
        return _backend_from_entry(url, entry, False)
    except Exception:
        return None

# ---------------------------------------------------- request-wire proxy
# The transcript never carries the request preamble (system prompt, tool
# schemas, MCP instructions) — that is a property of the CLIENT, not the
# backend. But with a local backend the full request is on the wire, on the
# user's own machine. `--proxy` is a recording passthrough: point
# ANTHROPIC_BASE_URL at it, it forwards untouched and records each
# request's COMPOSITION, so the overhead slab can be itemized from the real
# bytes the model received instead of inferred as R minus visible.

PROXY_DIR = os.path.expanduser("~/.claude/amtr-proxy")
PROXY_LOG = os.path.join(PROXY_DIR, "requests.jsonl")

def _jchars(v):
    """Chars of a request part as serialized — the size the wire carried."""
    if v is None:
        return 0
    if isinstance(v, str):
        return len(v)
    try:
        return len(json.dumps(v, ensure_ascii=False))
    except Exception:
        return 0

def proxy_compose(body):
    """One composition record from a /v1/messages request body (bytes)."""
    try:
        d = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(d, dict) or "messages" not in d:
        return None
    tools = d.get("tools") if isinstance(d.get("tools"), list) else []
    msgs = d.get("messages") if isinstance(d.get("messages"), list) else []
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "model": str(d.get("model") or ""),
           "system_chars": _jchars(d.get("system")),
           "tools_n": len(tools), "tools_chars": _jchars(tools),
           "msgs_n": len(msgs), "msgs_chars": _jchars(msgs),
           "input_tokens": None}
    rec["total_chars"] = (rec["system_chars"] + rec["tools_chars"]
                          + rec["msgs_chars"])
    return rec

_IN_TOK_RE = re.compile(rb'"input_tokens"\s*:\s*(\d+)')

def latest_proxy_record(model, max_age=900, span=65536):
    """Newest proxy composition record for `model` (fresh within max_age s),
    or None. Bounded backward read, same discipline as tail_usage."""
    try:
        size = os.path.getsize(PROXY_LOG)
        with open(PROXY_LOG, "rb") as fh:
            fh.seek(max(0, size - span))
            data = fh.read(span + 1)
    except OSError:
        return None
    best = None
    for line in data.split(b"\n"):
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("model") == model:
            best = d
    if best is None:
        return None
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(best["ts"])).total_seconds()
    except Exception:
        return None
    return best if 0 <= age <= max_age else None

def proxy_itemization(rec):
    """Human lines itemizing overhead from a composition record. The server
    reported input_tokens for the WHOLE request; each part's share is its
    serialized chars scaled by the one measured chars/token ratio — every
    byte visible, only the ratio shared."""
    tot_tok, tot_ch = rec.get("input_tokens"), rec.get("total_chars") or 0
    if not tot_tok or not tot_ch:
        return None
    cpt = tot_ch / tot_tok
    part = lambda ch: int(round(ch / cpt))
    hh = (rec.get("ts") or "")[11:16]
    return ("Itemized from the wire (amtr proxy, %sZ): system prompt "
            "≈%s tok · %d tool schemas ≈%s tok · history ≈%s tok "
            "(server-reported total %s tok)."
            % (hh, fmt_tok(part(rec["system_chars"])), rec["tools_n"],
               fmt_tok(part(rec["tools_chars"])),
               fmt_tok(part(rec["msgs_chars"])), fmt_tok(tot_tok)))

def fmt_tok(n):
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)

def run_proxy(args):
    import urllib.request
    import urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    upstream = args.upstream.rstrip("/")
    os.makedirs(PROXY_DIR, exist_ok=True)
    wlock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 + Connection: close — close-delimited responses let SSE
        # stream through without re-implementing chunked framing
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _relay(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            rec = None
            if self.command == "POST" and "/messages" in self.path:
                rec = proxy_compose(body)
            req = urllib.request.Request(upstream + self.path,
                                         data=body or None,
                                         method=self.command)
            skip = {"host", "content-length", "connection",
                    "accept-encoding"}
            for k, v in self.headers.items():
                if k.lower() not in skip:
                    req.add_header(k, v)
            try:
                resp = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                resp = e
            except Exception as e:
                self.send_error(502, str(e))
                return
            self.send_response(resp.getcode())
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection",
                                     "content-length"):
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            # relay as the bytes arrive (SSE stays live); sniff the server's
            # input_tokens for the composition record
            sniff = b""
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                if rec is not None and rec["input_tokens"] is None \
                        and len(sniff) < 262144:
                    sniff += chunk
                    m = _IN_TOK_RE.search(sniff)
                    if m:
                        rec["input_tokens"] = int(m.group(1))
            if rec is not None:
                with wlock:
                    with open(PROXY_LOG, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")

        do_GET = do_POST = do_DELETE = do_PUT = _relay

    srv = ThreadingHTTPServer(("127.0.0.1", args.listen), Handler)
    print("amtr proxy: 127.0.0.1:%d -> %s" % (args.listen, upstream),
          file=sys.stderr)
    print("recording request composition to %s" % PROXY_LOG,
          file=sys.stderr)
    print("point the client at it, e.g.:\n"
          "  ANTHROPIC_BASE_URL=http://127.0.0.1:%d claude --model <name>"
          % args.listen, file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0

# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, args):
        self.args = args
        self.poll_ms = 250
        self.budget = args.budget if args.budget else default_budget()
        self.budget_pinned = bool(args.budget)
        self._quitting = threading.Event()
        self.lock = threading.RLock()          # guards self.session mutation
        self.session = None
        self.tail_off = 0
        self.tail_buf = b""
        self.agent_tails = {}                  # path -> {off, buf, aid, wf, meta}
        self.wf_journals = {}                  # journal path -> {off, buf}
        # change-detection sentinels (reset on attach)
        self._never = object()
        self._last_cats = self._never
        self._last_meta = self._never
        self._last_tasks = self._never
        self._last_health = self._never
        self._last_fleet = self._never
        self._last_health_emit = 0.0
        self._last_growth = time.time()
        self._roster_cache = []
        self._resident_cache = {}      # path -> (mtime, (resident, model))
        self._backend_ctx = {}         # model -> served window (probe result)
        self._agent_map_cache = {}             # agent path -> (mtime, map|None)
        # seek coalescing (latest wins)
        self._seek_cond = threading.Condition()
        self._seek_pending = None
        self._seek_gen = 0
        self._fleet_force = threading.Event()
        # provider caches (SPEC f2 providers): Codex CLI and Gemini CLI
        # sessions join the roster (picker + wall + headless feed) and can be
        # attached — the adapters translate their transcripts
        self._gemini_cwd = {}        # pid -> cwd | None
        self._gemini_tail = {}       # path -> (mtime, parsed)
        self._gemini_reg = (0.0, {}) # (ts, project root -> short id)
        self._codex_files = {}       # pid -> rollout path | None
        self._codex_meta = {}        # path -> meta dict | None
        self._codex_tail = {}        # path -> (mtime, parsed)
        self._codex_tmux = (0.0, {})  # (ts, tty -> locator)

    # ---- reading -------------------------------------------------------------
    def _pump(self, path, off, buf, cb, reset_on_shrink=True):
        """Incremental buffered read of complete lines from `off`; calls
        cb(line_bytes, byte_offset) per line. Never holds more than one chunk
        plus a partial line in memory. Returns (new_off, new_buf, grew).
        A shrink with reset_on_shrink=False is left for the caller to handle
        (SPEC d: the main transcript must full-re-attach, never re-feed)."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return off, buf, False
        if size < off:                          # truncated: transcript replaced
            if not reset_on_shrink:
                return off, buf, False
            log("transcript shrank (%d -> %d); re-reading" % (off, size))
            off, buf = 0, b""
        if size == off:
            return off, buf, False
        grew = False
        try:
            with open(path, "rb") as fh:
                fh.seek(off)
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    start = off - len(buf)
                    buf += chunk
                    off = fh.tell()
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        cb(buf[:nl], start)
                        grew = True
                        start += nl + 1
                        buf = buf[nl + 1:]
        except OSError as e:
            log("read failed: %s" % e)
        return off, buf, grew

    # ---- attach ---------------------------------------------------------------
    def resolve_session(self, arg):
        if arg:
            # expand ~ and $VARS so a hand-typed path like
            # ~/.claude/projects/<slug>/<uuid>.jsonl resolves
            expanded = os.path.expanduser(os.path.expandvars(arg))
            if os.path.isfile(expanded):
                return os.path.realpath(expanded)
            # a bare session id (or a full path whose basename is the uuid)
            p = find_transcript(arg) or find_transcript(
                os.path.splitext(os.path.basename(expanded))[0])
            if p:
                return p
            # a provider session id (Codex thread / Gemini session): the
            # roster row carries its path
            if isinstance(self._last_fleet, list):
                for e in self._last_fleet:
                    if e.get("id") == arg and e.get("path") and \
                            os.path.isfile(e["path"]):
                        return e["path"]
            for _mt, path, meta in self._codex_recent_rollouts():
                if meta.get("id") == arg:
                    return path
        return None

    def pick_default(self):
        if self.args.session:
            p = self.resolve_session(self.args.session)
            if p:
                return p
            log("session %r not found" % self.args.session)
        if self.args.project:
            p = newest_transcript(self.args.project)
            if p:
                return p
            log("no transcript under project %r" % self.args.project)
        # An EXPLICIT --session/--project that didn't resolve must NOT silently
        # fall through to roster/global discovery — that reports the wrong
        # session. Only the no-args default case discovers.
        if self.args.session or self.args.project:
            return None
        self_sid = os.environ.get("AMTR_SELF_SESSION")
        live = []
        for e in scan_roster():
            if not e["_alive"]:
                continue
            if self_sid and e["sessionId"] == self_sid:
                continue                      # never auto-pick our own session
            tp = find_transcript(e["sessionId"])
            if tp:
                try:
                    live.append((os.path.getmtime(tp), tp))
                except OSError:
                    pass
        if live:
            return max(live)[1]
        p = newest_transcript()
        if p and self_sid and os.path.basename(p) == self_sid + ".jsonl":
            alt = [q for q in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
                   if os.path.basename(q) != self_sid + ".jsonl"]
            if alt:
                p = max(alt, key=lambda q: os.path.getmtime(q))
        return p

    def attach(self, path):
        sidechain = os.sep + "subagents" + os.sep in path
        self.attach_gen = getattr(self, "attach_gen", 0) + 1
        sess = Session(path, budget=self.budget, budget_pinned=self.budget_pinned,
                       sidechain_ok=sidechain)
        sess.attach_gen = self.attach_gen
        if sess.provider == "gemini":
            # the recording never names its project; the CLI's tmp dir does
            root = self._gemini_root_of(path)
            if root:
                sess.project = root
        if sidechain:
            try:
                with open(path[:-6] + ".meta.json", "r", encoding="utf-8") as fh:
                    m = json.load(fh) or {}
                desc = m.get("description") or m.get("agentType") or ""
                sess.title = ("agent: %s" % desc)[:60] if desc else "agent"
            except Exception:
                sess.title = "agent"
        t0 = time.time()
        off, buf, _ = self._pump(
            path, 0, b"",
            lambda raw, o: sess.feed_line(raw.decode("utf-8", "replace"), o))
        if sess.malformed:
            log("skipped %d malformed lines during backfill" % sess.malformed)
        sess.pending = _fresh_pending()        # backfill supersedes increments
        with self.lock:
            self.session = sess
            self.tail_off, self.tail_buf = off, buf
            self.agent_tails = {}
            self.wf_journals = {}
            self._last_cats = self._never
            self._last_meta = self._never
            self._last_tasks = self._never
            self._last_health = self._never
            self._last_growth = time.time()
            meta = sess.meta_payload()
            send(dict({"type": "meta"}, **meta))
            self._last_meta = meta
            if sess.files:
                send({"type": "files",
                      "upserts": [sess.file_payload(f) for f in sess.files]})
            send(dict({"type": "map"}, **sess.map_payload()))
            send(dict({"type": "backfill"}, **sess.backfill_payload()))
            send({"type": "ready", "session_id": sess.session_id,
                  "turns": len(sess.turns), "resident": sess.resident(),
                  "budget": sess.budget})
            send({"type": "event", "kind": "attach", "severity": "info",
                  "ts": now_hhmmss(),
                  "turn": max(0, len(sess.turns) - 1),
                  "msg": "attached %s (%d turns, %.2fs parse)"
                         % (sess.session_id[:8], len(sess.turns),
                            time.time() - t0)})
            self._scan_agents(sess)
            self._scan_tasks(sess, force=True)
            self.drain(sess)

    # ---- pending drain ----------------------------------------------------------
    def drain(self, sess):
        p, sess.pending = sess.pending, _fresh_pending()
        for msg in p["logs"]:
            log(msg)
        for ev in p["events"]:
            send(dict({"type": "event"}, **ev))
        if p["files"]:
            send({"type": "files",
                  "upserts": [sess.file_payload(f) for f in sorted(p["files"])]})
        for c in p["compactions"]:
            send(dict({"type": "compaction"}, **c))
        if p["map_rebuild"]:
            send(dict({"type": "map"}, **sess.map_payload()))
        elif p["segs"]:
            segs = [{"id": s["id"], "cat": s["cat"],
                     "tok": int(sess.seg_est(s) * sess.alpha),
                     "file": s["file"],
                     "born": s["born"], "ts": s["ts"]} for s in p["segs"]]
            segs = [s for s in segs if s["tok"] > 0]
            if segs:
                send({"type": "map_add", "rev": sess.map_rev, "segs": segs})
        for fa in p["faccess"]:
            send(dict({"type": "faccess"}, **fa))
        for c in p["cmds"]:
            send(dict({"type": "cmd"}, **c))
        for r in p["rets"]:
            send(dict({"type": "ret"}, **r))
        for i in sorted(p["turns"]):
            send(dict({"type": "turn"}, **sess.turn_payload(i)))
        for aid in sorted(p["agents"]):
            send(dict({"type": "agent"}, **sess.agent_payload(aid)))
        cats = sess.cats_payload()
        if cats != self._last_cats:
            self._last_cats = cats
            send({"type": "cats", "totals": cats})
        meta = sess.meta_payload()
        if meta != self._last_meta:
            self._last_meta = meta
            send(dict({"type": "meta"}, **meta))
        self._maybe_probe_backend(sess)

    # ---- local-backend probe ----------------------------------------------------
    def _maybe_probe_backend(self, sess):
        """Fire the probe once per (session, model), off-thread — the tail
        loop must never wait on a network answer."""
        m = sess.model
        if (not m or m == "?" or sess.provider != "claude"
                or m.startswith("claude-")):
            return
        if getattr(sess, "_probed_model", None) == m:
            return
        sess._probed_model = m
        threading.Thread(target=self._probe_backend, args=(sess, m),
                         daemon=True).start()

    def _backend_event(self, sess, msg, severity="info"):
        send({"type": "event", "kind": "backend", "severity": severity,
              "ts": now_hhmmss(), "turn": max(0, len(sess.turns) - 1),
              "msg": msg})

    def _probe_backend(self, sess, model):
        info = probe_local_backend(model)
        if info is None:
            return
        with self.lock:
            if self.session is not sess or sess.model != model:
                return
            sess.backend = info
            ctx = info.get("ctx")
            if info.get("loaded") and ctx:
                self._backend_ctx[model] = int(ctx)   # fleet rows inherit it
            if info.get("loaded") and ctx and not sess.budget_pinned \
                    and ctx != sess.budget:
                # /api/ps ctx is the SERVED window — authoritative, pin it
                old, sess.budget = sess.budget, int(ctx)
                sess.budget_pinned = True
                self._backend_event(
                    sess, "local backend %s (%s %s): budget %d -> %d"
                    % (model, info.get("params") or "?",
                       info.get("quant") or "?", old, sess.budget))
            # partial CPU offload: the standing explanation for every slow
            # turn that follows — said once, where the user looks when slow
            size, vram = info.get("size"), info.get("size_vram")
            if size and vram and vram < size * 0.95:
                self._backend_event(
                    sess, "vram %.1fG/%.1fG — partial CPU offload, "
                    "slow decode expected" % (vram / 1e9, size / 1e9))
            meta = sess.meta_payload()
            if meta != self._last_meta:
                self._last_meta = meta
                send(dict({"type": "meta"}, **meta))
        self._backend_watch(sess, model, info["url"])

    def _backend_watch(self, sess, model, url):
        """Slow /api/ps poll: model lifecycle as MOMENTS in the events feed —
        unload (cold load ahead), reload (window may change). Transitions
        only; a quiet server stays quiet. Runs until the session detaches."""
        loaded = True
        while not self._quitting.wait(15):
            with self.lock:
                if self.session is not sess or sess.model != model:
                    return
            try:
                entry = _ollama_pick(
                    _http_json(url + "/api/ps").get("models"), model)
            except Exception:
                continue                 # server hiccup: silent, keep watching
            now_loaded = entry is not None
            if now_loaded == loaded:
                continue
            loaded = now_loaded
            with self.lock:
                if self.session is not sess or sess.model != model:
                    return
                if not now_loaded:
                    self._backend_event(
                        sess, "model %s unloaded — cold load on next turn"
                        % model)
                    continue
                info = _backend_from_entry(url, entry, True)
                self._backend_event(sess, "model %s loaded" % model)
                sess.backend = info
                ctx = info.get("ctx")
                if ctx:
                    self._backend_ctx[model] = int(ctx)
                if ctx and ctx != sess.budget:
                    old, sess.budget = sess.budget, int(ctx)
                    sess.budget_pinned = True
                    self._backend_event(
                        sess, "served window changed: budget %d -> %d"
                        % (old, sess.budget))
                meta = sess.meta_payload()
                if meta != self._last_meta:
                    self._last_meta = meta
                    send(dict({"type": "meta"}, **meta))

    # ---- tail thread -------------------------------------------------------------
    def tail_loop(self):
        sub_next = 0.0
        while not self._quitting.is_set():
            time.sleep(max(0.02, self.poll_ms / 1000.0))
            try:
                reattach = None
                with self.lock:
                    sess = self.session
                    if sess is None:
                        continue
                    try:
                        if os.path.getsize(sess.path) < self.tail_off:
                            reattach = sess.path
                    except OSError:
                        pass
                if reattach is not None:
                    # SPEC d: a shrunk transcript means rewrite/rotation —
                    # full re-attach (fresh Session), never re-feed into
                    # populated state. attach() takes the lock itself.
                    log("transcript shrank; re-attaching %s"
                        % os.path.basename(reattach))
                    self.attach(reattach)
                    continue
                with self.lock:
                    sess = self.session
                    if sess is None:
                        continue
                    self.tail_off, self.tail_buf, grew = self._pump(
                        sess.path, self.tail_off, self.tail_buf,
                        lambda raw, o: sess.feed_line(
                            raw.decode("utf-8", "replace"), o),
                        reset_on_shrink=False)
                    if grew:
                        self._last_growth = time.time()
                        sess.last_retry_ms = None    # progress => retry cleared
                    self.drain(sess)
                    if time.time() >= sub_next:      # 1 s sub-cadence
                        sub_next = time.time() + 1.0
                        self._scan_agents(sess)
                        self._scan_tasks(sess)
                        self.drain(sess)
            except Exception as e:
                log("tail error: %s" % e)

    # ---- subagents ------------------------------------------------------------------
    def _scan_agents(self, sess):
        if sess.provider != "claude":
            return self._scan_provider_agents(sess)
        base = sess.path[:-6] if sess.path.endswith(".jsonl") else sess.path
        subdir = os.path.join(base, "subagents")
        if not os.path.isdir(subdir):
            return
        paths = glob.glob(os.path.join(subdir, "agent-*.jsonl"))
        paths += glob.glob(os.path.join(subdir, "workflows", "wf_*", "agent-*.jsonl"))
        for p in paths:
            st = self.agent_tails.get(p)
            if st is None:
                name = os.path.basename(p)
                aid = name[6:-6] if name.startswith("agent-") else name
                wf = None
                mm = re.search(r"/workflows/(wf_[^/]+)/", p)
                if mm:
                    wf = mm.group(1)
                meta = {}
                try:
                    with open(p[:-6] + ".meta.json", "r", encoding="utf-8") as fh:
                        meta = json.load(fh) or {}
                except Exception:
                    pass
                st = {"off": 0, "buf": b"", "aid": aid, "wf": wf, "meta": meta}
                self.agent_tails[p] = st
            own_box = [None]

            def _agent_line(raw, _o, box=own_box):
                if b'"usage"' not in raw:
                    return
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    return
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    return
                m = d.get("message")
                if not isinstance(m, dict) or (m.get("model") or "") == "<synthetic>":
                    return
                u = m.get("usage")
                if isinstance(u, dict):
                    box[0] = (_i(u.get("input_tokens"))
                              + _i(u.get("cache_read_input_tokens"))
                              + _i(u.get("cache_creation_input_tokens")))

            st["off"], st["buf"], _ = self._pump(p, st["off"], st["buf"],
                                                 _agent_line)
            own = own_box[0]
            aid = st["aid"]
            ag = sess.agents.get(aid)
            if ag is None:
                try:
                    born = os.path.getctime(p)
                except OSError:
                    born = time.time()
                ag = {"id": aid, "state": "running", "path": p,
                      "agent_type": st["meta"].get("agentType"),
                      "desc": st["meta"].get("description"), "wf": st["wf"],
                      "turn0": sess.turn_at_epoch(born),
                      "ts0": now_hhmmss(), "t0": born, "ts_last": born,
                      "turn1": None,
                      "own_tok": 0, "ret_tok": None, "tools": None,
                      "dur_ms": None}
                sess.agents[aid] = ag
                sess.pending["agents"].add(aid)
            if ag.get("path") != p:
                ag["path"] = p               # drill-in target (SPEC b agent)
                sess.pending["agents"].add(aid)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                mt = 0.0
            # the agent's OWN transcript is truth for its usage regardless
            # of state — a staleness-closed agent must still record it
            if own is not None and own != ag["own_tok"]:
                ag["own_tok"] = own
                sess.pending["agents"].add(aid)
            if ag["state"] == "running":
                if mt:
                    ag["ts_last"] = max(ag.get("ts_last") or 0.0, mt)
                # completion fallback: no journal, no parent completion —
                # a transcript quiet for 5 min is treated as finished, but
                # marked resurrectable (better a late "done" than an
                # infinite "running", and never a wedged one)
                if ag.get("ts_last") and time.time() - ag["ts_last"] > 300:
                    ag["state"] = "done"
                    ag["stale"] = True
                    ag["turn1"] = sess.turn_at_epoch(ag["ts_last"])
                    if ag.get("t0"):
                        ag["dur_ms"] = max(0, int((ag["ts_last"] - ag["t0"])
                                                  * 1000))
                    sess.pending["agents"].add(aid)
            elif ag.get("stale") and mt > (ag.get("ts_last") or 0.0) + 1.0:
                # resurrection: the transcript grew after a staleness close
                ag["state"] = "running"
                ag["stale"] = False
                ag["turn1"] = None
                ag["dur_ms"] = None
                ag["ts_last"] = mt
                sess.pending["agents"].add(aid)
            # per-agent context map (SPEC b `agent.map`): parse the agent's
            # own sidechain into its OVERVIEW map. Cached by (path, mtime) —
            # a live fleet must never re-parse every subagent each tick;
            # rebuild only when the transcript actually changed.
            c = self._agent_map_cache.get(p)
            if c and c[0] == mt:
                mp = c[1]
            else:
                mp = build_agent_map(p, sess.budget)
                self._agent_map_cache[p] = (mt, mp)
            if ag.get("map") != mp:
                ag["map"] = mp
                sess.pending["agents"].add(aid)
        # workflow journals are the completion truth for wf-spawned agents
        # (they never produce a parent toolUseResult): a {"type":"result"}
        # line marks its agentId done.
        base2 = sess.path[:-6] if sess.path.endswith(".jsonl") else sess.path
        for jp in glob.glob(os.path.join(base2, "subagents", "workflows",
                                         "wf_*", "journal.jsonl")):
            jst = self.wf_journals.setdefault(jp, {"off": 0, "buf": b""})

            def _journal_line(raw, _o, sess=sess, jp=jp):
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    return
                if not isinstance(d, dict) or d.get("type") != "result":
                    return
                ag = sess.agents.get(d.get("agentId") or "")
                if ag is None:
                    return
                # the journal is the completion truth: it finalizes a
                # running agent AND repairs one the staleness fallback
                # closed early; it never overwrites a parent toolUseResult
                # completion (those set ret_tok first)
                if ag["state"] == "running" or ag.get("stale"):
                    ag["state"] = "done"
                    ag["stale"] = False
                    ag["turn1"] = sess.turn_at_epoch(ag.get("ts_last") or 0.0)
                    if ag.get("ts_last") and ag.get("t0"):
                        ag["dur_ms"] = max(0, int((ag["ts_last"] - ag["t0"])
                                                  * 1000))
                    sess.pending["agents"].add(ag["id"])
                if ag.get("ret_tok") is None:
                    ag["ret_tok"] = est_obj(d.get("result"))
                    sess.pending["agents"].add(ag["id"])

            jst["off"], jst["buf"], _ = self._pump(jp, jst["off"], jst["buf"],
                                                   _journal_line)

    # ---- tasks -------------------------------------------------------------------------
    def _scan_provider_agents(self, sess):
        """Codex / Gemini subagents: the parent transcript announces them
        (the adapter registers the agent), their OWN transcript is truth for
        usage and completion — Codex: rollout-*-<thread id>.jsonl (task
        events); Gemini: chats/<parent id>/<agent id>.jsonl (idle ⇒ done)."""
        for aid, ag in list(sess.agents.items()):
            p = ag.get("path")
            if not p:
                if sess.provider == "codex":
                    hits = glob.glob(os.path.expanduser(
                        "~/.codex/sessions/*/*/*/rollout-*-%s.jsonl" % aid))
                    p = hits[0] if hits else None
                else:
                    cdir = os.path.dirname(sess.path)
                    cand = os.path.join(cdir, sess.session_id, "%s.jsonl" % aid)
                    p = cand if os.path.isfile(cand) else None
                if p:
                    ag["path"] = p
                    sess.pending["agents"].add(aid)
            if not p:
                continue
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            st = self.agent_tails.get(p)
            if st is not None and st.get("mt") == mt:
                continue
            lines = []
            try:
                with open(p, "rb") as fh:
                    fh.seek(max(0, os.path.getsize(p) - 65536))
                    lines = fh.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            info = codex_tail_parse(lines) if sess.provider == "codex" \
                else gemini_tail_parse(lines)
            self.agent_tails[p] = {"mt": mt, "info": info}
            # own_tok = the agent's last resident (the Claude rule: its
            # last request's context size, not a cumulative bill)
            own = info.get("resident")
            if own is not None and own != ag["own_tok"]:
                ag["own_tok"] = int(own)
                sess.pending["agents"].add(aid)
            # its OVERVIEW mini-map, through the same adapter path (cached
            # by mtime — a 25-agent codex tree must not re-parse per tick)
            c = self._agent_map_cache.get(p)
            if c and c[0] == mt:
                mp = c[1]
            else:
                mp = build_agent_map(p, sess.budget)
                self._agent_map_cache[p] = (mt, mp)
            if ag.get("map") != mp:
                ag["map"] = mp
                sess.pending["agents"].add(aid)
            if ag["state"] == "running":
                ag["ts_last"] = max(ag.get("ts_last") or 0.0, mt)
                finished = info.get("ended") if sess.provider == "codex" \
                    else info.get("status") == "idle"
                # a finished-or-quiet transcript closes the agent (a
                # transcript quiet for 5 min counts as finished either way)
                if finished or time.time() - mt > 300:
                    ag["state"] = "done"
                    ag["turn1"] = sess.turn_at_epoch(mt)
                    if ag.get("t0"):
                        ag["dur_ms"] = max(0, int((mt - ag["t0"]) * 1000))
                    sess.pending["agents"].add(aid)

    def _scan_tasks(self, sess, force=False):
        tdir = os.path.join(TASKS_DIR, sess.session_id)
        total = done = in_prog = 0
        active = None
        if os.path.isdir(tdir):
            for p in sorted(glob.glob(os.path.join(tdir, "*.json"))):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        t = json.load(fh)
                except Exception:
                    continue
                if not isinstance(t, dict):
                    continue
                total += 1
                st = t.get("status")
                if st == "completed":
                    done += 1
                elif st == "in_progress":
                    in_prog += 1
                    if active is None:
                        active = t.get("activeForm") or t.get("subject")
        payload = {"total": total, "done": done, "in_progress": in_prog,
                   "active": active}
        if force or payload != self._last_tasks:
            self._last_tasks = payload
            send(dict({"type": "tasks"}, **payload))

    def tasks_payload(self):
        return self._last_tasks if isinstance(self._last_tasks, dict) else \
            {"total": 0, "done": 0, "in_progress": 0, "active": None}

    # ---- fleet / health thread ------------------------------------------------------------
    def fleet_loop(self):
        while not self._quitting.is_set():
            self._fleet_force.wait(2.0)
            self._fleet_force.clear()
            if self._quitting.is_set():
                return
            try:
                self._fleet_tick()
            except Exception as e:
                log("fleet error: %s" % e)

    def _sess_entries(self):
        roster = scan_roster()
        self._roster_cache = roster
        prompts = history_last_prompts()
        now = time.time()
        out, seen = [], set()
        # one row per sessionId: a resumed session can leave a stale roster
        # file whose old pid was reused (kill -0 passes) — without this the
        # fleet shows the same session twice. Alive + newest update wins.
        roster = sorted(roster, key=lambda e: (e["_alive"],
                                               e.get("updatedAt") or 0),
                        reverse=True)
        for e in roster:
            sid = e["sessionId"]
            if sid in seen:
                continue
            tp = find_transcript(sid)
            mt = 0.0
            res, rmodel = None, ""
            if tp:
                try:
                    mt = os.path.getmtime(tp)
                except OSError:
                    pass
                res, rmodel = self._resident_of(tp, mt)
            status = fleet_row_status(e.get("status"), e["_alive"], mt, now)
            out.append({"id": sid, "path": tp or "", "pid": e.get("pid"),
                        "name": e.get("name") or memorable_name(sid),
                        "project": e.get("cwd") or "",
                        "status": status, "mtime": mt, "live": e["_alive"],
                        "resident": res,
                        "budget": self._row_budget(res, rmodel),
                        "last_prompt": prompts.get(sid),
                        # additive (SPEC b version-drift law): the session's
                        # tmux home "session:@window.%pane" when hosted there
                        # — lets front ends jump the user to the session
                        "tmux": e.get("tmux")})
            seen.add(sid)
        # recent non-live sessions: EVERY transcript (not just the newest per
        # project), newest first — the picker's search + scroll handle the
        # volume. Capped high so effectively all sessions are reachable.
        recents = []
        if os.path.isdir(PROJECTS_DIR):
            for d in os.listdir(PROJECTS_DIR):
                r = os.path.join(PROJECTS_DIR, d)
                if not os.path.isdir(r):
                    continue
                try:
                    names = os.listdir(r)
                except OSError:
                    continue
                for f in names:
                    if f.endswith(".jsonl") and f[:-6] not in seen:
                        p = os.path.join(r, f)
                        try:
                            mtv = os.path.getmtime(p)
                        except OSError:
                            continue
                        recents.append((mtv, p, d))
        recents.sort(reverse=True)
        for mt, p, d in recents[:500]:
            sid = os.path.basename(p)[:-6]
            res, rmodel = self._resident_of(p, mt)
            out.append({"id": sid, "path": p, "pid": None,
                        "name": memorable_name(sid),
                        "project": d, "status": "offline", "mtime": mt,
                        "live": False, "resident": res,
                        "budget": self._row_budget(res, rmodel),
                        "last_prompt": prompts.get(sid)})
        # other providers: live rows first (interleaved with the live Claude
        # rows by the front end's own sort), then their recent transcripts
        try:
            prov = self._codex_rows() + self._gemini_rows()
            live_ids = {r["id"] for r in prov}
            prov += [r for r in self._provider_recent_rows()
                     if r["id"] not in live_ids]
        except Exception as e:
            log("provider scan error: %s" % e)
            prov = []
        live_rows = [r for r in out if r.get("live")]
        rest = [r for r in out if not r.get("live")]
        return live_rows + [r for r in prov if r.get("live")] + rest + \
            [r for r in prov if not r.get("live")]

    def _provider_recent_rows(self):
        """Offline Codex rollouts (last two day-dirs) and Gemini recordings
        (newest 60), so the picker can attach a finished session of either
        CLI the same way it attaches an old Claude one."""
        rows = []
        for mt, path, meta in self._codex_recent_rollouts(days=30)[:60]:
            base = os.path.basename((meta["cwd"] or "").rstrip("/")) or "codex"
            rows.append({"id": meta["id"], "path": path, "pid": None,
                         "name": "%s-cx" % base, "project": meta["cwd"] or "",
                         "status": "offline", "mtime": mt, "live": False,
                         "resident": None, "budget": self.budget,
                         "last_prompt": None, "provider": "codex"})
        found = []
        for p in glob.glob(os.path.expanduser("~/.gemini/tmp/*/chats/session-*.jsonl")):
            try:
                found.append((os.path.getmtime(p), p))
            except OSError:
                continue
        found.sort(reverse=True)
        for mt, p in found[:60]:
            short = os.path.basename(os.path.dirname(os.path.dirname(p)))
            root = self._gemini_root_of(p)
            base = os.path.basename(root.rstrip("/")) or short[:8]
            rows.append({"id": self._gemini_sid(p), "path": p, "pid": None,
                         "name": "%s-gm" % base, "project": root,
                         "status": "offline", "mtime": mt, "live": False,
                         "resident": None, "budget": self.budget,
                         "last_prompt": None, "provider": "gemini"})
        return rows

    def _row_budget(self, resident, model=""):
        """A fleet row's budget: a probe-confirmed served window when the
        row's model has one (local sessions render on THEIR window, not an
        Anthropic rung), else pinned --budget verbatim, else the base budget
        auto-bumped to fit the row's own resident (`fleet_budget`)."""
        if model and not model.startswith("claude-"):
            ctx = self._backend_ctx.get(model)
            if ctx:
                return int(ctx)
        if self.budget_pinned:
            return self.budget
        return fleet_budget(self.budget, resident)

    # ---- codex provider (SPEC f2 providers; fleet feed only) ----------------
    def _codex_pids(self):
        """Live codex CLI processes as (pid, tty, start_epoch)."""
        try:
            out = subprocess.run(["ps", "-axo", "pid=,tty=,lstart=,comm="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        except Exception:
            return []
        rows = []
        for ln in out.splitlines():
            parts = ln.split(None, 7)
            if len(parts) == 8 and os.path.basename(parts[7]) == "codex":
                try:
                    start = time.mktime(time.strptime(
                        " ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
                    rows.append((int(parts[0]), parts[1], start))
                except (ValueError, OverflowError):
                    pass
        return rows

    def _codex_cwd(self, pid):
        """The process's working directory (cheap single-descriptor lsof)."""
        if pid in self._codex_files:
            return self._codex_files[pid]
        cwd = None
        try:
            out = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if ln.startswith("n"):
                    cwd = ln[1:]
                    break
        except Exception:
            cwd = None
        self._codex_files[pid] = cwd
        return cwd

    def _codex_recent_rollouts(self, days=2):
        """Newest-first non-subagent rollouts from the last `days` day-dirs
        (codex append-closes files, so open-file discovery is impossible —
        rows pair to processes by cwd + start time instead)."""
        base = os.path.expanduser("~/.codex/sessions")
        day_dirs = sorted(glob.glob(os.path.join(base, "*", "*", "*")))[-days:]
        out = []
        for d in day_dirs:
            for p in glob.glob(os.path.join(d, "rollout-*.jsonl")):
                meta = self._codex_meta_for(p)
                if not meta or meta.get("subagent") or not meta.get("id"):
                    continue
                try:
                    out.append((os.path.getmtime(p), p, meta))
                except OSError:
                    continue
        out.sort(reverse=True)
        return out

    def _codex_meta_for(self, path):
        if path in self._codex_meta:
            return self._codex_meta[path]
        meta = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.loads(fh.readline())
            if d.get("type") == "session_meta":
                p = d.get("payload") or {}
                src = p.get("source")
                sub = (p.get("thread_source") == "subagent" or
                       (isinstance(src, dict) and "subagent" in src))
                meta = {"id": p.get("id") or "", "cwd": p.get("cwd") or "",
                        "subagent": sub}
        except Exception:
            meta = None
        self._codex_meta[path] = meta
        return meta

    def _codex_tmux_map(self):
        """tty -> tmux locator, refreshed at fleet cadence. Codex has no
        roster tmux field; pane ttys identify the pane generically."""
        ts, cached = self._codex_tmux
        now = time.time()
        if now - ts < 2.0:
            return cached
        m = {}
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{pane_tty}|#{session_name}:#{window_id}.#{pane_id}"],
                capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if "|" in ln:
                    tty, loc = ln.split("|", 1)
                    m[tty] = loc
        except Exception:
            pass
        self._codex_tmux = (now, m)
        return m

    def _codex_rows(self):
        """Fleet rows for live Codex CLI sessions (provider:"codex").
        Pairing law: each live codex process claims the newest unclaimed
        rollout whose cwd matches and whose mtime is not older than the
        process start (a session that has not taken a prompt yet has no
        rollout and simply does not appear until it does)."""
        rows = []
        pids = self._codex_pids()
        live = {p for p, _, _ in pids}
        for k in list(self._codex_files):
            if k not in live:
                self._codex_files.pop(k, None)
        if not pids:
            return rows
        rollouts = self._codex_recent_rollouts()
        tmuxmap = self._codex_tmux_map()
        claimed = set()
        for pid, tty, start in pids:
            cwd = self._codex_cwd(pid)
            match = None
            for mt, path, meta in rollouts:
                if path in claimed or mt < start - 60:
                    continue
                if cwd and meta["cwd"] and meta["cwd"] != cwd:
                    continue
                match = (mt, path, meta)
                break
            if not match:
                continue
            mt, path, meta = match
            claimed.add(path)
            cached = self._codex_tail.get(path)
            if not cached or cached[0] != mt:
                lines = []
                try:
                    with open(path, "rb") as fh:
                        fh.seek(max(0, os.path.getsize(path) - 65536))
                        lines = fh.read().decode(
                            "utf-8", "replace").splitlines()
                except OSError:
                    pass
                cached = (mt, codex_tail_parse(lines))
                self._codex_tail[path] = cached
            info = cached[1]
            base = os.path.basename((meta["cwd"] or "").rstrip("/")) or "codex"
            rows.append({
                "id": meta["id"], "path": path, "pid": pid,
                "name": "%s-cx" % base, "project": meta["cwd"],
                "status": info["status"], "mtime": mt, "live": True,
                "resident": info["resident"],
                "budget": info["budget"] or self.budget,
                "last_prompt": info["last_prompt"],
                "tmux": tmuxmap.get("/dev/" + tty),
                "provider": "codex"})
        return rows

    # ---- gemini provider (SPEC f2 providers) ---------------------------------
    def _gemini_registry(self):
        """~/.gemini/projects.json: project root -> short id (the tmp dir
        name that holds the project's chats/). Refreshed at fleet cadence."""
        ts, cached = self._gemini_reg
        now = time.time()
        if now - ts < 5.0:
            return cached
        reg = {}
        try:
            with open(os.path.expanduser("~/.gemini/projects.json"), "r",
                      encoding="utf-8") as fh:
                d = json.load(fh) or {}
            pr = d.get("projects") if isinstance(d, dict) else None
            if isinstance(pr, dict):
                reg = {str(k): str(v) for k, v in pr.items()
                       if isinstance(v, (str, int))}
        except Exception:
            reg = {}
        self._gemini_reg = (now, reg)
        return reg

    def _gemini_root_of(self, path):
        """The project root of a Gemini recording: ~/.gemini/tmp/<short>/
        .project_root (the CLI writes it), else the registry's inverse."""
        tmpdir = os.path.dirname(os.path.dirname(path))
        try:
            with open(os.path.join(tmpdir, ".project_root"), "r",
                      encoding="utf-8") as fh:
                root = fh.read().strip()
            if root:
                return root
        except OSError:
            pass
        short = os.path.basename(tmpdir)
        for k, v in self._gemini_registry().items():
            if v == short:
                return k
        return ""

    @staticmethod
    def _gemini_sid(path):
        """The session id of a recording: its first line's sessionId, else
        the filename's 8-char tail."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.loads(fh.readline())
            if isinstance(d, dict) and isinstance(d.get("sessionId"), str):
                return d["sessionId"]
        except Exception:
            pass
        return os.path.basename(path)[:-6]

    def _gemini_pids(self):
        """Live Gemini CLI processes as (pid, tty, start_epoch): the CLI is a
        node script, so match the script path, not the command name."""
        try:
            out = subprocess.run(["ps", "-axo", "pid=,tty=,lstart=,command="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        except Exception:
            return []
        rows = []
        for ln in out.splitlines():
            parts = ln.split(None, 7)
            if len(parts) < 8:
                continue
            argv = parts[7].split()
            head = [os.path.basename(a) for a in argv[:2]]
            if "gemini" not in head and "gemini-cli" not in head:
                continue
            if any(a in ("--fleet",) for a in argv):
                continue
            try:
                start = time.mktime(time.strptime(
                    " ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
                rows.append((int(parts[0]), parts[1], start))
            except (ValueError, OverflowError):
                pass
        return rows

    def _proc_cwd(self, pid, cache):
        if pid in cache:
            return cache[pid]
        cwd = None
        try:
            out = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if ln.startswith("n"):
                    cwd = ln[1:]
                    break
        except Exception:
            cwd = None
        cache[pid] = cwd
        return cwd

    def _gemini_rows(self):
        """Fleet rows for live Gemini CLI sessions (provider:"gemini").
        Pairing law (the Codex one, with the registry in place of the
        rollout's own cwd): each live gemini process claims the newest
        unclaimed recording under its project's chats/ dir whose mtime is
        not older than the process start."""
        rows = []
        pids = self._gemini_pids()
        live = {p for p, _, _ in pids}
        for k in list(self._gemini_cwd):
            if k not in live:
                self._gemini_cwd.pop(k, None)
        if not pids:
            return rows
        reg = self._gemini_registry()
        tmuxmap = self._codex_tmux_map()
        claimed = set()
        for pid, tty, start in pids:
            cwd = self._proc_cwd(pid, self._gemini_cwd)
            if not cwd:
                continue
            short = reg.get(cwd) or reg.get(os.path.realpath(cwd))
            if not short:
                short = hashlib.sha256(cwd.encode("utf-8")).hexdigest()
            cdir = os.path.expanduser("~/.gemini/tmp/%s/chats" % short)
            cands = []
            for p in glob.glob(os.path.join(cdir, "session-*.jsonl")):
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if p in claimed or mt < start - 60:
                    continue
                cands.append((mt, p))
            if not cands:
                continue
            mt, path = max(cands)
            claimed.add(path)
            cached = self._gemini_tail.get(path)
            if not cached or cached[0] != mt:
                lines = []
                try:
                    with open(path, "rb") as fh:
                        fh.seek(max(0, os.path.getsize(path) - 65536))
                        lines = fh.read().decode("utf-8", "replace").splitlines()
                except OSError:
                    pass
                cached = (mt, gemini_tail_parse(lines))
                self._gemini_tail[path] = cached
            info = cached[1]
            status = info["status"]
            if status == "busy" and time.time() - mt > 120:
                status = "stalled"
            base = os.path.basename(cwd.rstrip("/")) or "gemini"
            rows.append({
                "id": self._gemini_sid(path), "path": path, "pid": pid,
                "name": "%s-gm" % base, "project": cwd,
                "status": status, "mtime": mt, "live": True,
                "resident": info["resident"],
                "budget": info["budget"] or self.budget,
                "last_prompt": info["last_prompt"],
                "tmux": tmuxmap.get("/dev/" + tty),
                "provider": "gemini"})
        return rows

    def _resident_of(self, path, mtime):
        """(resident, model) of a roster transcript, mtime-cached."""
        c = self._resident_cache.get(path)
        if c and c[0] == mtime:
            return c[1]
        r = tail_usage(path)
        self._resident_cache[path] = (mtime, r)
        return r

    def fleet_peek_payload(self, sid):
        """Fleet quicklook (`fleet_peek`): the conversation tail of ANY
        roster session, attached or not — same bounded content exception
        as `peek`."""
        base = {"id": sid, "found": False, "name": memorable_name(sid),
                "project": "", "msgs": []}
        entry = None
        if isinstance(self._last_fleet, list):
            for e in self._last_fleet:
                if e.get("id") == sid:
                    entry = e
                    break
        if entry:
            base["name"] = entry.get("name") or base["name"]
            base["project"] = entry.get("project") or ""
        tp = (entry or {}).get("path") or find_transcript(sid)
        if not tp or not os.path.isfile(tp):
            return base
        msgs = transcript_tail_msgs(tp)
        if msgs is None:
            return base
        base.update({"found": True, "msgs": msgs})
        return base

    def kill_session(self, sid):
        """`sess_kill`: SIGTERM the session's pid (roster-verified). The
        polite signal — Claude Code shuts down cleanly on it; never -9."""
        entry = None
        for e in self._roster_cache:
            if e.get("sessionId") == sid:
                entry = e
                break
        if entry is None and isinstance(self._last_fleet, list):
            for e in self._last_fleet:
                if e.get("id") == sid and e.get("provider"):
                    entry = {"name": e.get("name"), "pid": e.get("pid")}
                    break
        name = (entry or {}).get("name") or memorable_name(sid)
        pid = (entry or {}).get("pid")
        if not pid_alive(pid):
            send({"type": "log", "msg": "end %s: no live process" % name})
            return
        try:
            os.kill(int(pid), signal.SIGTERM)
            send({"type": "log",
                  "msg": "end %s: SIGTERM sent (pid %s)" % (name, pid)})
        except OSError as e:
            send({"type": "log", "msg": "end %s failed: %s" % (name, e)})
        self._fleet_force.set()

    def _fleet_tick(self):
        sessions = self._sess_entries()
        if sessions != self._last_fleet:
            self._last_fleet = sessions
            send({"type": "fleet", "sessions": sessions})
        with self.lock:
            sess = self.session
        if sess is None:
            return
        entry = None
        for e in self._roster_cache:
            if e["sessionId"] == sess.session_id:
                entry = e
                break
        if entry is None and sess.provider != "claude" and \
                isinstance(self._last_fleet, list):
            # provider sessions: the fleet row is the roster
            for e in self._last_fleet:
                if e.get("id") == sess.session_id and e.get("provider") \
                        and e.get("live"):
                    entry = {"_alive": True, "status": e.get("status")}
                    break
        try:
            mt = os.path.getmtime(sess.path)
        except OSError:
            mt = 0.0
        now = time.time()
        if entry is None:
            status = "offline"
        elif not entry["_alive"]:
            status = "dead"
        else:
            status = entry.get("status") or "idle"
            # stall threshold is API-shaped (120s); a local model legitimately
            # thinks longer — scale by ITS observed rhythm (3x median turn
            # duration), only when a backend is confirmed, so API sessions
            # keep the exact historical behavior
            limit = 120.0
            if sess.backend:
                durs = sorted(t["dur_ms"] for t in sess.turns[-32:]
                              if t.get("dur_ms"))
                if durs:
                    limit = max(limit, 3.0 * durs[len(durs) // 2] / 1000.0)
            if status == "busy" and now - max(mt, self._last_growth) > limit:
                status = "stalled"
        payload = {"status": status, "last_activity_ts": mt,
                   "api_errors": sess.api_errors,
                   "stalled": status == "stalled"}
        if sess.last_retry_ms is not None:
            payload["retry_in_ms"] = sess.last_retry_ms
        changed = payload != self._last_health
        if changed and status == "stalled" and (
                not isinstance(self._last_health, dict)
                or self._last_health.get("status") != "stalled"):
            send({"type": "event", "kind": "stall", "severity": "warn",
                  "ts": now_hhmmss(),
                  "turn": max(0, len(sess.turns) - 1),
                  "msg": "no transcript growth for %ds while busy"
                         % int(limit)})
        if changed or now - self._last_health_emit >= 5.0:
            self._last_health = payload
            self._last_health_emit = now
            send(dict({"type": "health"}, **payload))

    # ---- seek worker (latest-wins coalescing) --------------------------------------------------
    def seek_loop(self):
        while not self._quitting.is_set():
            with self._seek_cond:
                while self._seek_pending is None and not self._quitting.is_set():
                    self._seek_cond.wait(0.5)
                if self._quitting.is_set():
                    return
                turn, gen = self._seek_pending
                self._seek_pending = None
            try:
                with self.lock:
                    sess = self.session
                if sess is None:
                    continue
                # checkpoint pick + clone under lock; replay unlocked
                st = sess.state_at_turn(turn, lock=self.lock)
                with self._seek_cond:
                    if self._seek_gen != gen:
                        continue              # a newer seek superseded this one
                # merge dir-discovered agents the checkpoint predates:
                # copy under lock (the tail thread mutates sess.agents)
                with self.lock:
                    extra = {k: dict(v) for k, v in sess.agents.items()
                             if v.get("path")}
                last = st.turns[-1] if st.turns else None
                snap = {"type": "snapshot", "turn": int(turn),
                        "resident": st.resident(),
                        "waterline": int(last["waterline"]) if last else 0,
                        "cc": int(last["cc"]) if last else 0,
                        "map": st.map_payload(),
                        "files": [st.file_payload(f) for f in st.files],
                        "cats": st.cats_payload(),
                        "agents": ([st.agent_payload(a) for a in st.agents]
                                   + [st.agent_payload(None, a=v)
                                      for k, v in sorted(extra.items())
                                      if k not in st.agents
                                      and v.get("turn0", 0) <= int(turn)]),
                        "tasks": self.tasks_payload()}
                send(snap)
            except Exception as e:
                log("seek error: %s" % e)

    def request_seek(self, turn):
        with self._seek_cond:
            self._seek_gen += 1
            self._seek_pending = (turn, self._seek_gen)
            self._seek_cond.notify()

    def cancel_seek(self):
        with self._seek_cond:
            self._seek_gen += 1
            self._seek_pending = None

    # ---- control dispatch -------------------------------------------------------------------------
    def handle(self, ctrl):
        t = ctrl.get("type")
        if t == "attach":
            arg = ctrl.get("session")
            if not isinstance(arg, str) or not arg:
                log("attach: missing session")
                return
            p = self.resolve_session(arg)
            if not p:
                log("attach: session %r not found" % arg)
                return
            self.cancel_seek()
            self.attach(p)
        elif t == "peek":
            sid = ctrl.get("seg")
            if isinstance(sid, int):
                with self.lock:
                    sess = self.session
                if sess is not None:
                    send(dict({"type": "peek"}, **sess.peek_payload(sid)))
        elif t == "seek":
            turn = ctrl.get("turn")
            if isinstance(turn, (int, float)):
                self.request_seek(int(turn))
        elif t == "live":
            self.cancel_seek()
        elif t == "report":
            # the live engine already has the whole session parsed — build the
            # report from it (no re-read) and write it to a findable file
            with self.lock:
                sess = self.session
            if sess is None:
                send({"type": "report_done", "ok": False, "path": "",
                      "msg": "no session attached"})
            else:
                try:
                    # canonical output is a self-contained DIRECTORY housing
                    # report.pdf, report.md, figures/, turns/. Build the dir
                    # path here, write report.md immediately for instant
                    # feedback, then spawn amtr_paper --dir in the background
                    # to fill in the PDF, figures, and per-turn capture.
                    name = re.sub(r"[^A-Za-z0-9._-]", "-",
                                  session_name(sess.session_id))
                    outdir = os.path.join(CLAUDE_DIR, "amtr-reports",
                                          "%s-%s" % (name,
                                                     sess.session_id[:8]))
                    os.makedirs(outdir, exist_ok=True)
                    md_path = os.path.join(outdir, "report.md")
                    with open(md_path, "w", encoding="utf-8") as fh:
                        fh.write(render_report_md(build_report(sess)))
                    # kick off the full compiled paper (figures + phase table +
                    # algorithm sections + per-turn capture) in the BACKGROUND
                    # — it takes ~30-60s, so we don't block; report.md is
                    # overwritten with identical content when it completes.
                    paper = os.path.join(os.path.dirname(
                        os.path.abspath(__file__)), "amtr_paper.py")
                    try:
                        subprocess.Popen(
                            [sys.executable, paper, "--session", sess.path,
                             "--dir", outdir],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)   # detach: survive amtr quit
                        msg = "md now · full report building → %s" % outdir
                    except Exception:
                        msg = "report written (PDF unavailable)"
                    send({"type": "report_done", "ok": True, "path": outdir,
                          "msg": msg})
                except Exception as e:
                    send({"type": "report_done", "ok": False, "path": "",
                          "msg": "report failed: %s" % e})
        elif t == "set":
            key, val = ctrl.get("key"), ctrl.get("value")
            if not isinstance(key, str) or val is None:
                return
            try:
                if key == "chars_per_tok":
                    Est.chars_per_tok = max(0.5, float(val))
                elif key == "fit":
                    on = bool(int(val))
                    globals()["FIT_ON"] = on
                    with self.lock:
                        if self.session:
                            self.session.fit_on = on
                            self.session.fit_next = 0     # re-decide next turn
                            if not on:
                                self.session.fit = None
                elif key == "poll_ms":
                    self.poll_ms = max(20, int(val))
                elif key == "t_auto":
                    with self.lock:
                        if self.session:
                            self.session.t_auto = min(0.99, max(0.1, float(val)))
                # unknown keys silently ignored (forward compatibility)
            except (TypeError, ValueError):
                log("set %s: bad value %r" % (key, val))
        elif t == "fleet_peek":
            sid = ctrl.get("session")
            if isinstance(sid, str) and sid:
                send(dict({"type": "fleet_peek"},
                          **self.fleet_peek_payload(sid)))
        elif t == "sess_kill":
            sid = ctrl.get("session")
            if isinstance(sid, str) and sid:
                self.kill_session(sid)
        elif t == "fleet_refresh":
            self._fleet_force.set()
        elif t == "quit":
            self._quitting.set()
        # unknown Control types ignored (forward compatibility)

    # ---- run -------------------------------------------------------------------------------------------
    def run(self):
        sessions = []
        try:
            sessions = self._sess_entries()
        except Exception as e:
            log("initial discovery failed: %s" % e)
        default = self.pick_default()
        default_id = os.path.basename(default)[:-6] if default else None
        send({"type": "init", "engine_version": ENGINE_VERSION,
              "sessions": sessions, "default_session": default_id})
        if default:
            try:
                self.attach(default)
            except Exception as e:
                log("attach failed: %s" % e)
        else:
            log("no session found; waiting for attach")
        threading.Thread(target=self.tail_loop, daemon=True).start()
        threading.Thread(target=self.fleet_loop, daemon=True).start()
        threading.Thread(target=self.seek_loop, daemon=True).start()
        for line in sys.stdin:                 # EOF == quit
            if self._quitting.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                ctrl = json.loads(line)
            except Exception:
                log("ignored malformed control line: %r" % line[:80])
                continue
            if not isinstance(ctrl, dict):
                continue
            try:
                self.handle(ctrl)
            except Exception as e:
                log("control error: %s" % e)
            if self._quitting.is_set():
                break
        self._quitting.set()
        with self._seek_cond:
            self._seek_cond.notify_all()
        self._fleet_force.set()

# ---------------------------------------------------------------- standalone modes
def run_selftest(args):
    """Replay a fixture transcript at full speed: attach-flow messages for all
    but the last two turns, then the remainder as incremental flow. Exit 0."""
    path = args.session or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "tests", "fixtures", "golden.jsonl")
    if not os.path.isfile(path):
        log("selftest fixture missing: %s" % path)
        return 1
    with open(path, "rb") as fh:
        raw = fh.read()
    lines, offs, pos = [], [], 0
    for ln in raw.split(b"\n"):
        lines.append(ln.decode("utf-8", "replace"))
        offs.append(pos)
        pos += len(ln) + 1
    # pass 1: count total turns
    probe = Session(path, budget=args.budget or BUDGET_RUNGS[0],
                    budget_pinned=bool(args.budget))
    for ln, off in zip(lines, offs):
        probe.feed_line(ln, off)
    total_turns = len(probe.turns)
    cut_turns = max(1, total_turns - 2)
    # pass 2: backfill up to the cut, then stream the rest incrementally
    eng = Engine(args)
    sess = Session(path, budget=args.budget or BUDGET_RUNGS[0],
                   budget_pinned=bool(args.budget))
    eng.session = sess
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln:
            try:
                d = json.loads(ln)
            except Exception:
                d = None
            if isinstance(d, dict) and len(sess.turns) >= cut_turns \
                    and sess.is_new_turn(d):
                break
        sess.feed_line(lines[i], offs[i])
        i += 1
    sess.pending = _fresh_pending()
    send({"type": "init", "engine_version": ENGINE_VERSION,
          "sessions": [{"id": sess.session_id, "path": path, "pid": None,
                        "name": "selftest", "project": sess.project or "",
                        "status": "offline", "mtime": 0.0, "live": False,
                        "resident": sess.resident(), "budget": sess.budget,
                        "last_prompt": None}],
          "default_session": sess.session_id})
    send(dict({"type": "meta"}, **sess.meta_payload()))
    eng._last_meta = sess.meta_payload()
    if sess.files:
        send({"type": "files",
              "upserts": [sess.file_payload(f) for f in sess.files]})
    send(dict({"type": "map"}, **sess.map_payload()))
    send(dict({"type": "backfill"}, **sess.backfill_payload()))
    send({"type": "ready", "session_id": sess.session_id,
          "turns": len(sess.turns), "resident": sess.resident(),
          "budget": sess.budget})
    while i < len(lines):
        sess.feed_line(lines[i], offs[i])
        eng.drain(sess)
        i += 1
    return 0

def run_validate(args):
    path = args.session or newest_transcript(args.project)
    if not path or not os.path.isfile(path):
        print("no session transcript found (try --session PATH)")
        return 1
    sess = Session(path, budget=args.budget or default_budget(),
                   budget_pinned=bool(args.budget))
    t0 = time.time()
    Engine(args)._pump(
        path, 0, b"",
        lambda raw, o: sess.feed_line(raw.decode("utf-8", "replace"), o))
    dt = time.time() - t0
    R = sess.resident()
    est = sess.overhead + int(sess.live_est() * sess.alpha)
    print("session   : %s" % path)
    print("parsed    : %d records, %d turns in %.2fs (%d malformed skipped)"
          % (sess.rec_count, len(sess.turns), dt, sess.malformed))
    print("RESIDENT (API usage, last turn) : %s tokens (%d%% of %s)  <- ground truth"
          % ("{:,}".format(R), R * 100 // max(1, sess.budget),
             "{:,}".format(sess.budget)))
    print("MODEL     : %s   waterline C=%s" % (sess.model or "?",
          "{:,}".format(sess.turns[-1]["waterline"] if sess.turns else 0)))
    print("estimate  : overhead %s + live est %s x alpha %.3f = %s (vs R %s)"
          % ("{:,}".format(sess.overhead),
             "{:,}".format(int(sess.live_est())),
             sess.alpha, "{:,}".format(est), "{:,}".format(R)))
    ft = sess.fit_payload()
    if ft["active"]:
        print("ratios    : %s, fitted from this session's own R over %d turns "
              "(R² %.3f, residual RMS %.1f%% of mean R)"
              % ("PER-CATEGORY" if ft["mode"] == "cats"
                 else "ONE GLOBAL RATIO", ft["turns"], ft.get("r2", 0.0),
                 ft.get("rms_pct", 0.0)))
        print("            held-out median |err|/R: %.2f%% fitted vs %.2f%% "
              "for the %.1f constant (one-ratio control %.2f%%)"
              % (ft.get("holdout_fit_pct", 0.0),
                 ft.get("holdout_prior_pct", 0.0), ft["prior_cpt"],
                 ft.get("holdout_scale_pct", 0.0)))
        print("            " + "  ".join(
            "%s %.2f%s" % (c, v, "" if c in (ft.get("fitted_cats") or ())
                           else "*")
            for c, v in sorted(ft["cpt"].items())) + "   chars/token"
            + "   [* = not identifiable, left on the fitted global rate]")
        print("            fitted overhead intercept %s (vs measured %s)"
              % ("{:,}".format(ft["overhead"]), "{:,}".format(sess.overhead)))
        if ft.get("clamped"):
            print("            pinned to the plausible-range bound: "
                  + ", ".join(ft["clamped"]))
    else:
        print("ratios    : single global %.1f chars/token (fit inactive: %s)"
              % (ft["prior_cpt"], ft["reason"]))
    print("cats      : " + "  ".join(
        "%s %s" % (c, "{:,}".format(int(v)))
        for c, v in sess.cats_payload().items() if v))
    rebuilds = [e for e in sess.events if e["kind"] == "rebuild"]
    print("rebuilds  : %d server context rebuild(s)%s"
          % (len(rebuilds),
             "".join("  [t%d %s %s]" % (e["turn"], e["ts"], e["msg"])
                     for e in rebuilds)))
    print("files     : %d tracked, %d resident" % (len(sess.files),
          sum(1 for f in sess.files.values() if f["resident"])))
    print("compaction: %d events, %s tokens cumulatively dropped"
          % (len(sess.compactions), "{:,}".format(sess.cum_dropped)))
    top = sorted(sess.files.values(), key=lambda f: -(f["tok"] + f["waste"]))[:10]
    for f in top:
        print("  %8s tok  waste %-8s %s%s" % (
            "{:,}".format(int(f["tok"])), "{:,}".format(int(f["waste"])),
            "" if f["resident"] else "✝ ", f["path"]))
    print("validation: run /context in the live session; its total should track")
    print("RESIDENT above (same underlying API usage).")
    return 0

# ---------------------------------------------------------------- report (SPEC f)
SPARK_RAMP = "▁▂▃▄▅▆▇█"
BIG_PULL_TOK = 16_000            # the SHELL/RETRIEVAL "screams" threshold
NOTABLE_KINDS = ("compaction", "rebuild", "model_switch")

def _fc(n):
    return "{:,}".format(int(n))

def _fmt_span(secs):
    s = int(max(0, secs))
    if s >= 3600:
        return "%dh %dm" % (s // 3600, (s % 3600) // 60)
    if s >= 60:
        return "%dm %ds" % (s // 60, s % 60)
    return "%ds" % s

def build_report(sess, interrupted=False):
    """One JSON-able dict holding every report section, in SPEC (f) order.
    Markdown is a rendering of THIS dict and --json dumps it verbatim, so
    both formats carry the same content and the same authoritative/estimated
    labels — and every number is the Session's own accounting."""
    R = sess.resident()
    B = max(1, sess.budget)
    # -- 1 HEADER
    models, seen, switches, prev = [], set(), 0, None
    for t in sess.turns:
        m = t["model"] or "?"
        if m not in seen:
            seen.add(m)
            models.append(m)
        if prev is not None and m != prev:
            switches += 1
        prev = m
    t0 = sess.started_epoch
    t1 = ts_epoch(sess.last_ts or "") or t0
    header = {"session_id": sess.session_id, "title": sess.title,
              "name": session_name(sess.session_id),
              "project": sess.project or "", "models": models,
              "model_switches": switches, "cc_version": sess.cc_version,
              "started_at": sess.started_at, "ended_at": sess.last_ts,
              "duration_s": round(max(0.0, t1 - t0), 1),
              "turns": len(sess.turns), "entrypoint": sess.entrypoint,
              "interrupted": bool(interrupted)}
    # -- 2 CONTEXT (authoritative)
    peak_r, peak_t = sess.peak_resident()
    last = sess.turns[-1] if sess.turns else None
    context = {"label": "authoritative", "final_r": int(R), "budget": int(B),
               "pct_budget": round(100.0 * R / B, 1),
               "peak_r": int(peak_r), "peak_turn": int(peak_t),
               "waterline": int(last["waterline"]) if last else 0,
               "compactions": [dict(c) for c in sess.compactions],
               "cum_dropped": int(sess.cum_dropped),
               "rebuilds": [dict(r) for r in sess.rebuilds],
               "cats": sess.cats_payload(), "alpha": round(sess.alpha, 4),
               "overhead": int(sess.overhead), "fit": sess.fit_payload()}
    # -- 3 ECONOMICS (authoritative)
    tot = sess.usage_totals()
    cost = sess.cost_stats()
    economics = {"label": "authoritative", "in": tot["in"],
                 "cache_read": tot["cr"], "cc_5m": tot["cc_5m"],
                 "cc_1h": tot["cc_1h"], "out": tot["out"], "hit": tot["hit"],
                 "cost_total": cost["total"], "cost_mean": cost["mean"],
                 "cost_p95": cost["p95"],
                 "thrash": sum(1 for e in sess.events
                               if e["kind"] == "thrash"),
                 "models": sess.model_totals()}
    # -- 4 FILES (estimated)
    allf = list(sess.files.values())
    frows = []
    for f in sorted(allf, key=lambda f: -int(f["tok"]))[:15]:
        tok = int(f["tok"])
        frows.append({"tok": tok,
                      "pct_r": (round(100.0 * tok * sess.alpha
                                      / max(1, R), 1)
                                if f["resident"] else None),
                      "reads": f["reads"], "writes": f["writes"],
                      "edits": f["edits"], "waste": int(f["waste"]),
                      "resident": bool(f["resident"]), "path": f["path"]})
    files = {"label": "estimated", "table": frows,
             "totals": {"files": len(allf),
                        "tok": sum(int(f["tok"]) for f in allf),
                        "reads": sum(f["reads"] for f in allf),
                        "writes": sum(f["writes"] for f in allf),
                        "edits": sum(f["edits"] for f in allf),
                        "waste": sum(int(f["waste"]) for f in allf)},
             "total_waste": sum(int(f["waste"]) for f in allf),
             "evicted": sum(1 for f in allf if not f["resident"])}
    # -- 5 SHELL
    shell = sess.cmd_totals()
    shell["failures"] = [{"ts": c["ts"], "turn": c["turn"],
                          "cmd": c["cmd"], "err": c["err"] or c["out"]}
                         for c in sess.cmds
                         if not c["ok"] and not c["interrupted"]]
    shell["top"] = [{"ts": c["ts"], "turn": c["turn"], "ok": c["ok"],
                     "interrupted": c["interrupted"], "bg": c["bg"],
                     "tok_out": c["tok_out"], "cmd": c["cmd"]}
                    for c in sorted(sess.cmds,
                                    key=lambda c: -c["tok_out"])[:5]]
    # -- 6 RETRIEVAL
    retrieval = sess.ret_totals()
    retrieval["failures"] = [{"ts": r["ts"], "kind": r["kind"],
                              "src": r["src"], "q": r["q"], "tok": r["tok"]}
                             for r in retrieval["failures"]]
    # -- 7 AGENTS
    agents = sess.agent_totals()
    agents["top"] = []
    for a in sorted(sess.agents.values(),
                    key=lambda a: -_i(a["own_tok"]))[:5]:
        ret = a.get("ret_tok")
        agents["top"].append(
            {"type": a.get("agent_type"), "desc": a.get("desc"),
             "state": a["state"], "own_tok": _i(a["own_tok"]),
             "ret_tok": ret,
             "amp": (round(_i(a["own_tok"]) / max(1, _i(ret)), 1)
                     if ret is not None else None),
             "dur_ms": a.get("dur_ms")})
    # -- 8 EVENTS (ledger verbatim, errors first)
    evs = list(sess.events)
    pick = ("ts", "kind", "severity", "turn", "msg")
    events = ([{k: e[k] for k in pick} for e in evs
               if e["severity"] == "error"]
              + [{k: e[k] for k in pick} for e in evs
                 if e["severity"] != "error"])
    # -- 9 TIMELINE (R per turn, scaled to the session's OWN peak — a report
    # is about THIS run; scaling to the 1M budget floors every headless run to
    # a flat row of ▁ since they rarely approach it)
    tl_peak = max((t["resident"] for t in sess.turns), default=1) or 1
    spark = "".join(SPARK_RAMP[min(7, int(8 * min(1.0, t["resident"] / tl_peak)))]
                    for t in sess.turns)
    marks = [" "] * len(sess.turns)
    for r in sess.rebuilds:
        if 0 <= r["turn"] < len(marks):
            marks[r["turn"]] = "≈"
    for c in sess.compactions:          # a compaction owns its cell
        if 0 <= c["turn"] < len(marks):
            marks[c["turn"]] = "▼"
    notes = [{"turn": e["turn"], "ts": e["ts"], "kind": e["kind"],
              "msg": e["msg"]} for e in evs
             if e["severity"] in ("warn", "error")
             or e["kind"] in NOTABLE_KINDS]
    timeline = {"spark": spark, "marks": "".join(marks), "notes": notes,
                "peak": tl_peak}
    # -- 10 DIAGNOSTICS
    diags = []
    for f in sorted(allf, key=lambda f: -int(f["waste"])):
        if f["cum"] > 0 and f["waste"] > 0.25 * f["cum"]:
            diags.append("waste hot-spot: %s — %s of its %s-token traffic "
                         "was re-read or overwritten (%d%%)"
                         % (f["path"], _fc(f["waste"]), _fc(f["cum"]),
                            round(100.0 * f["waste"] / f["cum"])))
    trunc = [t["turn"] for t in sess.turns if t.get("stop") == "max_tokens"]
    if trunc:
        diags.append("truncation stops: %d turn(s) hit max_tokens (%s)"
                     % (len(trunc), ", ".join("t%d" % t for t in trunc)))
    lows = [i for i in range(len(sess.turns))
            if sess.turn_payload(i)["hit"] < 0.5]
    if lows:
        diags.append("sub-50%% cache-hit turns: %s"
                     % ", ".join("t%d" % i for i in lows))
    for c in sess.cmds:
        if c["tok_out"] >= BIG_PULL_TOK:
            diags.append(">16k-token command output: %s tok — $ %s"
                         % (_fc(c["tok_out"]), c["cmd"]))
    for r in sess.rets:
        if r["tok"] >= BIG_PULL_TOK:
            diags.append(">16k-token retrieval: %s tok — %s %s"
                         % (_fc(r["tok"]), r["src"], r["q"]))
    failed_ags = [a for a in sess.agents.values() if a["state"] == "failed"]
    if failed_ags:
        diags.append("failed agents: %d (%s)"
                     % (len(failed_ags),
                        "; ".join((a.get("desc") or a["id"])[:60]
                                  for a in failed_ags)))
    if R >= 0.85 * B:
        diags.append("unanswered pressure: session ended at %d%% of budget "
                     "(red zone)" % round(100.0 * R / B))
    return {"header": header, "context": context, "economics": economics,
            "files": files, "shell": shell, "retrieval": retrieval,
            "agents": agents, "events": events, "timeline": timeline,
            "diagnostics": diags}

def render_report_md(rep):
    L = []
    h = rep["header"]
    L.append("# amtr report — %s" % h.get("name", h["session_id"]))
    if h["title"]:
        L.append("*%s*" % h["title"])
    if h["interrupted"]:
        L += ["", "**INTERRUPTED — partial run**"]
    L.append("")
    models = " → ".join(h["models"]) if h["models"] else "?"
    if h["model_switches"]:
        models += " (%d switch%s)" % (h["model_switches"],
                                      "" if h["model_switches"] == 1
                                      else "es")
    L.append("- session: %s (%s)" % (h.get("name", "?"), h["session_id"]))
    if h["project"]:
        L.append("- project: %s" % h["project"])
    L.append("- model: %s" % models)
    if h["cc_version"]:
        L.append("- cc version: %s" % h["cc_version"])
    L.append("- span: %s → %s (%s)" % (h["started_at"] or "?",
                                       h["ended_at"] or "?",
                                       _fmt_span(h["duration_s"])))
    L.append("- turns: %d" % h["turns"])
    if h["entrypoint"]:
        L.append("- entrypoint: %s" % h["entrypoint"])
    # CONTEXT
    c = rep["context"]
    L += ["", "## CONTEXT (authoritative)", ""]
    L.append("- final R: %s / %s (%.1f%% of budget)"
             % (_fc(c["final_r"]), _fc(c["budget"]), c["pct_budget"]))
    L.append("- peak R: %s (turn %d)" % (_fc(c["peak_r"]), c["peak_turn"]))
    L.append("- waterline at end: %s" % _fc(c["waterline"]))
    L.append("- compactions: %d · %s tokens dropped cumulatively"
             % (len(c["compactions"]), _fc(c["cum_dropped"])))
    for cp in c["compactions"]:
        L.append("  - #%d t%d %s %s: %s → %s (dropped %s)"
                 % (cp["n"], cp["turn"], cp["ts"], cp["trigger"],
                    _fc(cp["pre"]), _fc(cp["post"]), _fc(cp["dropped"])))
    L.append("- server rebuilds: %d" % len(c["rebuilds"]))
    for rb in c["rebuilds"]:
        L.append("  - t%d %s: R %s → %s (flushed %s est reasoning)"
                 % (rb["turn"], rb["ts"], _fc(rb["pre"]), _fc(rb["post"]),
                    _fc(rb["flushed"])))
    L.append("- composition at end (α %.3f):" % c["alpha"])
    ft = c.get("fit") or {}
    cpt = ft.get("cpt") or {}
    fitted = set(ft.get("fitted_cats") or ())
    L += ["", "| category | tokens | %R | chars/tok |", "|:--|--:|--:|--:|"]
    for cat, v in c["cats"].items():
        if v:
            r = cpt.get(cat)
            L.append("| %s | %s | %.1f | %s |"
                     % (cat, _fc(v), 100.0 * v / max(1, c["final_r"]),
                        "—" if r is None else
                        ("%.2f" % r if cat in fitted else "%.2f*" % r)))
    L.append("")
    if ft.get("active"):
        L.append("- token ratios: %s, fitted from this session's own "
                 "authoritative R — %d turns, R² %.3f, residual RMS %.1f%% of "
                 "mean R; held-out median |err|/R %.2f%% vs %.2f%% for the "
                 "%.1f constant (one-ratio control %.2f%%). Fitted overhead "
                 "intercept %s tokens. `*` = not identifiable here, left on "
                 "the fitted global rate."
                 % ("per category" if ft.get("mode") == "cats"
                    else "ONE GLOBAL RATIO (the per-category split did not "
                         "beat it out of sample)",
                    ft.get("turns", 0), ft.get("r2", 0.0),
                    ft.get("rms_pct", 0.0), ft.get("holdout_fit_pct", 0.0),
                    ft.get("holdout_prior_pct", 0.0),
                    ft.get("prior_cpt", 3.8), ft.get("holdout_scale_pct", 0.0),
                    _fc(ft.get("overhead", 0))))
        if ft.get("clamped"):
            L.append("  - pinned to the plausible-range bound (the data could "
                     "not identify them): %s" % ", ".join(ft["clamped"]))
    else:
        L.append("- token ratios: single global constant %.1f chars/token "
                 "(fit inactive: %s)"
                 % (ft.get("prior_cpt", 3.8), ft.get("reason") or "n/a"))
    # ECONOMICS
    e = rep["economics"]
    L += ["", "## ECONOMICS (authoritative)", ""]
    L.append("| Σ input | Σ cache-read | Σ cc 5m | Σ cc 1h | Σ output |")
    L.append("|--:|--:|--:|--:|--:|")
    L.append("| %s | %s | %s | %s | %s |"
             % tuple(_fc(e[k]) for k in ("in", "cache_read", "cc_5m",
                                         "cc_1h", "out")))
    L.append("")
    L.append("- overall hit rate: %.1f%%" % (100.0 * e["hit"]))
    L.append("- total cost: %s u" % e["cost_total"])
    L.append("- cost/turn: mean %s u · p95 %s u"
             % (e["cost_mean"], e["cost_p95"]))
    L.append("- thrash events: %d" % e["thrash"])
    if len(e["models"]) > 1:
        L += ["", "| model | turns | Σ in | Σ cr | Σ cc | Σ out | cost u |",
              "|:--|--:|--:|--:|--:|--:|--:|"]
        for m in e["models"]:
            L.append("| %s | %d | %s | %s | %s | %s | %s |"
                     % (m["model"], m["turns"], _fc(m["in"]), _fc(m["cr"]),
                        _fc(m["cc"]), _fc(m["out"]), m["cost_u"]))
    # FILES
    f = rep["files"]
    L += ["", "## FILES (estimated)", ""]
    if f["table"]:
        L.append("| tok | %R | rd | wr | ed | waste | path |")
        L.append("|--:|--:|--:|--:|--:|--:|:--|")
        for r in f["table"]:
            L.append("| %s | %s | %d | %d | %d | %s | %s%s |"
                     % (_fc(r["tok"]),
                        ("%.1f" % r["pct_r"]) if r["pct_r"] is not None
                        else "—",
                        r["reads"], r["writes"], r["edits"], _fc(r["waste"]),
                        "" if r["resident"] else "✝ ", r["path"]))
        t = f["totals"]
        L.append("| %s | | %d | %d | %d | %s | Σ %d files |"
                 % (_fc(t["tok"]), t["reads"], t["writes"], t["edits"],
                    _fc(t["waste"]), t["files"]))
        L.append("")
        L.append("- total waste: %s tokens" % _fc(f["total_waste"]))
        L.append("- evicted files: %d" % f["evicted"])
    else:
        L.append("no files touched")
    # SHELL
    s = rep["shell"]
    L += ["", "## SHELL", ""]
    if s["n"]:
        L.append("- %d command(s): %d ok · %d failed · %d interrupted "
                 "· %d bg" % (s["n"], s["ok"], s["failed"],
                              s["interrupted"], s["bg"]))
        L.append("- Σ tok_out: %s" % _fc(s["tok_out"]))
        if s["failures"]:
            L += ["- failures:", "", "```"]
            for c2 in s["failures"]:
                L.append("$ %s" % c2["cmd"])
                if c2["err"]:
                    L.append(c2["err"])
            L += ["```", ""]
        L.append("- top by tok_out:")
        for c2 in s["top"]:
            mark = "^" if c2["interrupted"] else ("ok" if c2["ok"] else "✖")
            L.append("  - %s tok · %s · $ %s%s"
                     % (_fc(c2["tok_out"]), mark, c2["cmd"],
                        " &" if c2["bg"] else ""))
    else:
        L.append("no commands run")
    # RETRIEVAL
    r = rep["retrieval"]
    L += ["", "## RETRIEVAL", ""]
    if r["n"]:
        L.append("- %d pull(s) · Σ %s tokens" % (r["n"], _fc(r["tok"])))
        L.append("- by kind: " + " · ".join(
            "%s ×%d (%s tok)" % (k["kind"], k["n"], _fc(k["tok"]))
            for k in r["by_kind"]))
        L.append("- by src: " + " · ".join(
            "%s ×%d (%s tok)" % (k["src"], k["n"], _fc(k["tok"]))
            for k in r["by_src"]))
        if r["failures"]:
            L.append("- failures:")
            for x in r["failures"]:
                L.append("  - ✖ %s %s — %s" % (x["kind"], x["src"], x["q"]))
    else:
        L.append("no external retrievals")
    # AGENTS
    a = rep["agents"]
    L += ["", "## AGENTS", ""]
    if a["n"]:
        L.append("- %d agent(s): %s"
                 % (a["n"], " · ".join("%d %s" % (v, k) for k, v
                                       in sorted(a["counts"].items()))))
        L.append("- fan-out %s ≡ %.2f× main · Σ ret %s · median amp %s"
                 % (_fc(a["own_tok"]), a["x_main"], _fc(a["ret_tok"]),
                    a["amp_median"]))
        L.append("- top by own tokens:")
        for t2 in a["top"]:
            L.append("  - %s · %s · own %s / ret %s / amp %s / dur %s"
                     % (t2["type"] or "?", t2["desc"] or "—",
                        _fc(t2["own_tok"]),
                        _fc(t2["ret_tok"]) if t2["ret_tok"] is not None
                        else "—",
                        t2["amp"] if t2["amp"] is not None else "—",
                        _fmt_span(t2["dur_ms"] / 1000.0)
                        if t2["dur_ms"] else "—"))
    else:
        L.append("no agents launched")
    # EVENTS
    L += ["", "## EVENTS", ""]
    if rep["events"]:
        L.append("```")
        for ev in rep["events"]:
            L.append("%s · %s · %s" % (ev["ts"], ev["kind"], ev["msg"]))
        L.append("```")
    else:
        L.append("no events")
    # TIMELINE
    t3 = rep["timeline"]
    L += ["", "## TIMELINE", ""]
    if t3["spark"]:
        L.append("R per turn (▁=0 … █=%s, the session peak; %s budget; "
                 "▼ compaction, ≈ rebuild):"
                 % (_fc(t3.get("peak", 0)), _fc(rep["context"]["budget"])))
        L += ["", "```"]
        sp, mk = t3["spark"], t3["marks"]
        for i in range(0, len(sp), 100):
            L.append("t%-4d %s" % (i, sp[i:i + 100]))
            seg = mk[i:i + 100].rstrip()
            if seg.strip():                 # only when there are markers
                L.append("      %s" % seg)
        L.append("```")
        for n in t3["notes"]:
            L.append("- t%d %s %s: %s" % (n["turn"], n["ts"], n["kind"],
                                          n["msg"]))
    else:
        L.append("no turns")
    # DIAGNOSTICS
    L += ["", "## DIAGNOSTICS", ""]
    if rep["diagnostics"]:
        for d in rep["diagnostics"]:
            L.append("- %s" % d)
    else:
        L.append("no findings")
    return "\n".join(L) + "\n"

def _roster_entry(session_id):
    for e in scan_roster():
        if e.get("sessionId") == session_id:
            return e
    return None

def run_report(args):
    eng = Engine(args)                  # discovery + _pump; never run()
    path = eng.pick_default()
    if not path or not os.path.isfile(path):
        sys.stderr.write("no session transcript found (try --session PATH)\n")
        return 1
    sess = Session(path, budget=args.budget or default_budget(),
                   budget_pinned=bool(args.budget))
    feed = lambda raw, o: sess.feed_line(raw.decode("utf-8", "replace"), o)
    off, buf, _ = eng._pump(path, 0, b"", feed)
    interrupted = False
    if args.watch:
        sys.stderr.write("watching %s — report on completion\n"
                         % sess.session_id)
        sys.stderr.flush()
        idle = max(1.0, float(args.idle_secs))
        seen_roster = False
        try:
            while True:
                time.sleep(1.0)
                off, buf, _ = eng._pump(path, off, buf, feed)
                entry = _roster_entry(sess.session_id)
                if entry is not None:
                    seen_roster = True
                    if not entry["_alive"]:
                        break            # roster pid dead: run ended
                elif seen_roster:
                    break                # roster entry gone: run ended
                busy = (entry is not None and entry["_alive"]
                        and entry.get("status") == "busy")
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    mt = 0.0
                if not busy and time.time() - mt >= idle:
                    break                # transcript quiet: run ended
        except KeyboardInterrupt:
            interrupted = True           # report what was parsed so far
    rep = build_report(sess, interrupted=interrupted)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(render_report_md(rep), end="")
    return 130 if interrupted else 0

def run_fleet(args):
    """--fleet: headless roster stream (SPEC f2). Emits the `fleet` Update
    message as JSON lines on the protocol fd, change-detected on a --poll-secs
    roster scan, first emission immediate. For external front ends (the menu
    bar companion). Exits when the consumer closes the pipe — send() swallows
    BrokenPipeError by design, so this loop writes the protocol fd directly
    and lets the failure end the process."""
    eng = Engine(args)
    poll = max(0.5, args.poll_secs)
    last = None
    while True:
        try:
            sessions = eng._sess_entries()
            if args.live_only:
                sessions = [s for s in sessions if s.get("live")]
            # provider rows (Codex, Gemini) are part of _sess_entries now:
            # picker, wall, and this feed all see the same roster
        except Exception as e:
            log("fleet scan error: %s" % e)
            sessions = last
        if sessions is not None and sessions != last:
            last = sessions
            line = json.dumps({"type": "fleet", "sessions": sessions},
                              separators=(",", ":"), ensure_ascii=False)
        else:
            # quiet tick: heartbeat, so a closed pipe is noticed within one
            # poll and the consumer can distinguish "no change" from "dead"
            line = '{"type":"hb"}'
        try:
            with _PROTO_LOCK:
                _PROTO.write(line + "\n")
                _PROTO.flush()
        except (BrokenPipeError, ValueError, OSError):
            return 0
        time.sleep(poll)


def main():
    ap = argparse.ArgumentParser(description="amtr v2 data engine")
    ap.add_argument("--session", help="transcript path or session id")
    ap.add_argument("--project", help="project dir (newest session under it)")
    ap.add_argument("--budget", type=int, help="pin the context budget")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print a ground-truth session report to stdout")
    ap.add_argument("--json", action="store_true",
                    help="with --report: emit the report as one JSON object")
    ap.add_argument("--watch", action="store_true",
                    help="with --report: wait for the run to end, then report")
    ap.add_argument("--idle-secs", type=float, default=60, dest="idle_secs",
                    help="with --watch: transcript-quiet seconds that end a "
                         "run with no live roster pid (default 60)")
    ap.add_argument("--fleet", action="store_true",
                    help="headless roster stream: emit the `fleet` Update "
                         "message as JSON lines on stdout, change-detected "
                         "(for external front ends, e.g. the menu bar)")
    ap.add_argument("--poll-secs", type=float, default=2.0, dest="poll_secs",
                    help="with --fleet: seconds between roster scans "
                         "(default 2, floor 0.5)")
    ap.add_argument("--live-only", action="store_true", dest="live_only",
                    help="with --fleet: emit only live roster sessions "
                         "(drop the recent-transcript tail)")
    ap.add_argument("--cal", type=float,
                    help="chars per token: the global prior the per-category "
                         "fit regularizes toward and the constant used when "
                         "it is inactive (default 3.8)")
    ap.add_argument("--no-fit", action="store_true", dest="no_fit",
                    help="disable the per-category ratio fit: size every "
                         "category with the single global constant (the "
                         "pre-fit behaviour, for comparison)")
    ap.add_argument("--proxy", action="store_true",
                    help="recording passthrough between an Anthropic-API "
                         "client and a local backend: forwards untouched, "
                         "records each request's composition so overhead "
                         "can be itemized from the real wire bytes")
    ap.add_argument("--listen", type=int, default=11435,
                    help="with --proxy: port to listen on (default 11435)")
    ap.add_argument("--upstream", default="http://localhost:11434",
                    help="with --proxy: backend base URL "
                         "(default http://localhost:11434)")
    args = ap.parse_args()
    if args.cal:
        Est.chars_per_tok = args.cal
    if args.no_fit or os.environ.get("AMTR_NO_FIT"):
        globals()["FIT_ON"] = False
    if args.validate or args.report:
        _use_real_stdout()               # the report IS fd 1's payload
    if args.selftest:
        sys.exit(run_selftest(args))
    if args.validate:
        sys.exit(run_validate(args))
    if args.report:
        sys.exit(run_report(args))
    if args.fleet:
        sys.exit(run_fleet(args))
    if args.proxy:
        sys.exit(run_proxy(args))
    Engine(args).run()

if __name__ == "__main__":
    main()
