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

import json, time, math, copy, glob, re, argparse, threading, zlib, subprocess
from collections import deque
from datetime import datetime, timezone

ENGINE_VERSION = "0.1.1"
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

def est_obj(o):
    if o is None:
        return 0
    if isinstance(o, str):
        return est_text(o)
    if isinstance(o, list):
        t = 0
        for b in o:
            t += est_obj(b)
        return t
    if isinstance(o, dict):
        if o.get("type") == "image":
            return IMG_TOK
        try:
            return est_text(json.dumps(o, ensure_ascii=False))
        except Exception:
            return 0
    return est_text(str(o))

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

# ---------------------------------------------------------------- session
class Session:
    """Pure accounting for one transcript. No I/O emission of its own —
    everything lands in self.pending for the caller to drain."""

    _SKIP_CLONE = ("checkpoints", "rec_offsets", "pending", "_no_ckpt")

    def __init__(self, path, budget=None, budget_pinned=False, t_auto=0.85,
                 ckpt_every=200, sidechain_ok=False):
        # an explicitly attached agent transcript IS the main conversation
        # from this Session's point of view (SPEC c `attach`)
        self.sidechain_ok = sidechain_ok
        self.path = path
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
        # config / meta
        self.budget = budget if budget else BUDGET_RUNGS[0]
        self.budget_pinned = budget_pinned
        self.t_auto = t_auto
        self.model = ""
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
        u = d.get("uuid")
        if isinstance(u, str) and u not in self.uuid_order:
            self.uuid_order[u] = self.rec_count
        try:
            self.feed_obj(d)
        except Exception as e:
            self.pending["logs"].append("record parse error: %s" % e)
        self._maybe_checkpoint()

    def is_new_turn(self, d):
        """Would this record open a new turn? Must mirror _feed_assistant."""
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return False
        if d.get("isSidechain") and not self.sidechain_ok:
            return False
        m = d.get("message")
        if not isinstance(m, dict):
            return False
        if (m.get("model") or "") == "<synthetic>" or not d.get("requestId"):
            return False
        if d.get("isApiErrorMessage"):
            return False
        if not isinstance(m.get("usage"), dict):
            return False
        return d.get("requestId") != self.req_last

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

    def _alloc(self, cat, tok, uuid, ts, file_id=None):
        if tok <= 0:
            return
        sid = self.seg_next
        self.seg_next += 1
        seg = {"id": sid, "uuid": uuid, "cat": cat, "est": tok,
               "file": file_id, "born": self._born(), "ts": ts_epoch(ts)}
        self.ring[sid] = seg
        self.by_uuid.setdefault(uuid, []).append(sid)
        self.cat_est[cat] = self.cat_est.get(cat, 0) + tok
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
        if model == "<synthetic>" or not d.get("requestId"):
            return  # synthetic: never a turn, never resident
        rid = d.get("requestId")
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
                    e = est_text(b.get("text") or "")
                    self._alloc("assistant", e, uuid, ts)
                elif bt == "thinking":
                    e = est_text(b.get("thinking") or "")
                    self._alloc("thinking", e, uuid, ts)
                elif bt == "tool_use":
                    e = self._tool_use(b, uuid, ts)
                else:
                    e = est_obj(b)
                    self._alloc("tool", e, uuid, ts)
                self._vis_acc += e
        elif isinstance(content, str):
            e = est_text(content)
            self._alloc("assistant", e, uuid, ts)
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
        itok = est_obj(inp)
        if tid:
            self.tu[tid] = (name, fp)
        if name in AGENT_TOOLS:
            desc = inp.get("description") if isinstance(inp, dict) else None
            if tid:
                self.tu2agent[tid] = {"turn": self._born(), "ts": hhmmss(ts),
                                      "t0": ts_epoch(ts), "desc": desc}
            self._alloc("tool", itok, uuid, ts)
        elif name == "Read" and fp:
            # addressing only: rings as file context, no file-stat change
            self._alloc("file", itok, uuid, ts, self._file_id(fp))
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
            self._alloc("file", itok, uuid, ts, fid)
        elif name == "Bash":
            if tid and isinstance(inp, dict):
                self.tu2cmd[tid] = {"cmd": inp.get("command"),
                                    "desc": inp.get("description")}
            self._alloc("bash", itok, uuid, ts)
        else:
            if tid:
                r = _ret_classify(name, inp)
                if r is not None:
                    self.tu2ret[tid] = r
            self._alloc("tool", itok, uuid, ts)
        if self.turns:
            self.turns[-1]["tools"] += 1
            self.pending["turns"].add(self.turns[-1]["turn"])
        return itok

    def _on_turn_usage(self, tr, ts, new_turn=False):
        R = tr["resident"]
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
        # overhead calibration (the honesty rule)
        if self.overhead0 is None or self.rebase_pending:
            self.overhead0 = max(0, R - self.est_live)
            self.rebase_pending = False
        if self.overhead0 + self.est_live <= R:
            self.alpha = 1.0
            self.overhead = R - self.est_live
        else:
            a = (R - self.overhead0) / max(1, self.est_live)
            self.alpha = min(1.0, max(1e-6, a))
            self.overhead = self.overhead0
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
            self._alloc("summary", est_obj(content), uuid, ts)
            return
        if isinstance(content, str):
            self._alloc("user", est_text(content), uuid, ts)
            return
        if not isinstance(content, list):
            return
        tur = d.get("toolUseResult")
        for b in content:
            if isinstance(b, str):
                self._alloc("user", est_text(b), uuid, ts)
                continue
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text") or ""
                cat = "attach" if "<system-reminder>" in txt else "user"
                self._alloc(cat, est_text(txt), uuid, ts)
            elif bt == "image":
                self._alloc("user", IMG_TOK, uuid, ts)
            elif bt == "tool_result":
                self._tool_result(b, tur, uuid, ts)
            else:
                self._alloc("user", est_obj(b), uuid, ts)

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
        rtok = est_obj(b.get("content"))
        if isinstance(tur, dict):
            f = tur.get("file")
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                rtok = max(rtok, est_text(f["content"]))
        if name == "Read" and fp:
            fid = self._file_id(fp)
            f = self.files[fid]
            f["cum"] += rtok
            f["tok"] = rtok             # a read resets the live copy
            f["reads"] += 1
            self._file_touch(fid, ts)
            self._faccess(fid, "r", rtok, ts)
            self._alloc("file", rtok, uuid, ts, fid)
        elif name in WRITE_TOOLS and fp:
            # ack/patch echo: resident context but not a new file copy
            self._alloc("file", rtok, uuid, ts, self._file_id(fp))
        elif name == "Bash":
            self._alloc("bash", rtok, uuid, ts)
            self._cmd_result(tuid, b, tur, rtok, ts)
        else:
            self._alloc("tool", rtok, uuid, ts)
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
        self._alloc("attach", est_obj(att), uuid, ts)
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
        pre = _i(cm.get("preTokens"))
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
        oh = {"id": 0, "cat": "overhead", "tok": int(self.overhead),
              "file": None, "born": 0, "ts": self.started_epoch}
        segs = [oh]
        total = oh["tok"]
        for s in self.ring.values():
            tok = int(s["est"] * self.alpha)
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
            base.update({
                "found": True, "cat": "overhead", "kind": "overhead",
                "tok": int(self.overhead),
                "excerpt": ("Server-side context the transcript cannot "
                            "itemize: the system prompt, tool schemas, "
                            "skill listings and MCP instructions. Measured "
                            "honestly as R minus everything visible "
                            "(re-based at each compaction).")})
            return base
        seg = self.ring.get(sid)
        if seg is None:
            return base
        base.update({"cat": seg["cat"], "uuid": seg["uuid"],
                     "born": int(seg["born"]), "est": int(seg["est"]),
                     "tok": int(seg["est"] * self.alpha),
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
        for c, v in self.cat_est.items():
            out[c] = int(v * self.alpha)
        return out

    def agent_payload(self, aid, a=None):
        if a is None:
            a = self.agents[aid]
        p = {"id": a["id"], "state": a["state"], "turn0": a["turn0"],
             "ts0": a["ts0"], "own_tok": int(a["own_tok"]),
             "t0": round(float(a.get("t0") or 0.0), 3),
             "ts_last": round(float(a.get("ts_last") or 0.0), 3)}
        for k in ("agent_type", "desc", "wf", "path", "turn1", "ret_tok", "tools",
                  "dur_ms"):
            if a.get(k) is not None:
                p[k] = a[k]
        return p

    def map_payload(self):
        segs = self.build_map_segs()
        self.map_base_n = len(segs)          # rebuild resets the cadence counter
        self.map_adds_since = 0
        return {"rev": self.map_rev, "alpha": round(self.alpha, 4),
                "segs": segs}

    def meta_payload(self):
        return {"session_id": self.session_id, "path": self.path,
                "attach_gen": getattr(self, "attach_gen", 0),
                "name": session_name(self.session_id),
                "project": self.project or "", "title": self.title,
                "model": self.model or "?", "budget": int(self.budget),
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
                                sidechain_ok=self.sidechain_ok)
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
                        if len(clone.turns) >= target and clone.is_new_turn(dd):
                            clone.pending = _fresh_pending()
                            return clone
                        clone.rec_offsets.append(0)
                        clone.rec_count += 1
                        u = dd.get("uuid")
                        if isinstance(u, str) and u not in clone.uuid_order:
                            clone.uuid_order[u] = clone.rec_count
                        try:
                            clone.feed_obj(dd)
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

def tail_usage(path, span=65536):
    """Resident tokens of the newest non-synthetic assistant usage, from a
    bounded backward read. Returns int or None."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - span))
            data = fh.read(span + 1)
    except OSError:
        return None
    best = None
    for line in data.split(b"\n"):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant" or not d.get("requestId"):
            continue
        m = d.get("message")
        if not isinstance(m, dict) or (m.get("model") or "") == "<synthetic>":
            continue
        u = m.get("usage")
        if isinstance(u, dict):
            best = (_i(u.get("input_tokens")) + _i(u.get("cache_read_input_tokens"))
                    + _i(u.get("cache_creation_input_tokens")))
    return best

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
        self._resident_cache = {}              # path -> (mtime, resident)
        # seek coalescing (latest wins)
        self._seek_cond = threading.Condition()
        self._seek_pending = None
        self._seek_gen = 0
        self._fleet_force = threading.Event()

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
                     "tok": int(s["est"] * sess.alpha), "file": s["file"],
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
        out, seen = [], set()
        for e in roster:
            sid = e["sessionId"]
            tp = find_transcript(sid)
            mt = 0.0
            res = None
            if tp:
                try:
                    mt = os.path.getmtime(tp)
                except OSError:
                    pass
                res = self._resident_of(tp, mt)
            status = e.get("status") or "idle"
            if not e["_alive"]:
                status = "dead"
            out.append({"id": sid, "path": tp or "", "pid": e.get("pid"),
                        "name": e.get("name") or memorable_name(sid),
                        "project": e.get("cwd") or "",
                        "status": status, "mtime": mt, "live": e["_alive"],
                        "resident": res, "budget": self.budget,
                        "last_prompt": prompts.get(sid)})
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
            out.append({"id": sid, "path": p, "pid": None,
                        "name": memorable_name(sid),
                        "project": d, "status": "offline", "mtime": mt,
                        "live": False, "resident": self._resident_of(p, mt),
                        "budget": self.budget, "last_prompt": prompts.get(sid)})
        return out

    def _resident_of(self, path, mtime):
        c = self._resident_cache.get(path)
        if c and c[0] == mtime:
            return c[1]
        r = tail_usage(path)
        self._resident_cache[path] = (mtime, r)
        return r

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
            if status == "busy" and now - max(mt, self._last_growth) > 120:
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
                  "msg": "no transcript growth for 120s while busy"})
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
                elif key == "poll_ms":
                    self.poll_ms = max(20, int(val))
                elif key == "t_auto":
                    with self.lock:
                        if self.session:
                            self.session.t_auto = min(0.99, max(0.1, float(val)))
                # unknown keys silently ignored (forward compatibility)
            except (TypeError, ValueError):
                log("set %s: bad value %r" % (key, val))
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
    est = sess.overhead + int(sess.est_live * sess.alpha)
    print("session   : %s" % path)
    print("parsed    : %d records, %d turns in %.2fs (%d malformed skipped)"
          % (sess.rec_count, len(sess.turns), dt, sess.malformed))
    print("RESIDENT (API usage, last turn) : %s tokens (%d%% of %s)  <- ground truth"
          % ("{:,}".format(R), R * 100 // max(1, sess.budget),
             "{:,}".format(sess.budget)))
    print("MODEL     : %s   waterline C=%s" % (sess.model or "?",
          "{:,}".format(sess.turns[-1]["waterline"] if sess.turns else 0)))
    print("estimate  : overhead %s + live est %s x alpha %.3f = %s (vs R %s)"
          % ("{:,}".format(sess.overhead), "{:,}".format(sess.est_live),
             sess.alpha, "{:,}".format(est), "{:,}".format(R)))
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
               "overhead": int(sess.overhead)}
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
    L += ["", "| category | tokens | %R |", "|:--|--:|--:|"]
    for cat, v in c["cats"].items():
        if v:
            L.append("| %s | %s | %.1f |"
                     % (cat, _fc(v), 100.0 * v / max(1, c["final_r"])))
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
    ap.add_argument("--cal", type=float, help="chars per token (default 3.8)")
    args = ap.parse_args()
    if args.cal:
        Est.chars_per_tok = args.cal
    if args.validate or args.report:
        _use_real_stdout()               # the report IS fd 1's payload
    if args.selftest:
        sys.exit(run_selftest(args))
    if args.validate:
        sys.exit(run_validate(args))
    if args.report:
        sys.exit(run_report(args))
    Engine(args).run()

if __name__ == "__main__":
    main()
