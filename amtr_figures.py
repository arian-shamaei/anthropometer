#!/usr/bin/env python3
"""amtr_figures.py — professional, TUI-faithful figures for the amtr compiled
report (SPEC.md a/e/f). Pure renderers over a fed amtr_engine.Session.

Stdlib + matplotlib (+ numpy, which ships with matplotlib) + Pillow only.
Everything renders CLEANLY with matplotlib (vector-quality) on a WHITE
academic-paper canvas, retaining amtr's category HUES as data encoding (tuned
for legibility on white) — never by capturing terminal glyphs.

Public API (each takes a fed Session; animated ones also take per-turn map
snapshots where noted) and returns the output path(s):

    map_static(sess, out)                      -> path (PDF/PNG)
    map_animation(sess, snapshots, out_gif)    -> path (GIF, 1 frame/turn)
    files_static(sess, out)                    -> path
    files_animation(sess, out_gif)             -> path
    divergence_static(sess, out)               -> path
    divergence_animation(sess, out_gif)        -> path
    agents_timeline_static(sess, out)          -> path
    agents_timeline_animation(sess, out_gif)   -> path
    ekg_static(sess, out)                       -> path
    montage(frame_pngs, out_png, n=5)          -> path (keyframe strip)

The animated figures also expose *_frames(...) helpers that yield PIL images so
the paper builder can both write the GIF and lift keyframes for the montage.
"""
import os
import math

import matplotlib
matplotlib.use("Agg")                       # headless, no display
import matplotlib.pyplot as plt             # noqa: E402
from matplotlib.patches import PathPatch, Rectangle  # noqa: E402
from matplotlib.path import Path            # noqa: E402
import numpy as np                          # noqa: E402
from PIL import Image                       # noqa: E402

# ------------------------------------------------------------------ palette
# Academic (light) palette: a WHITE page in the informational-paper tradition.
# The amtr category HUES are retained as data encoding (they identify each
# category), but lightness is tuned for legibility on white — pale TUI tones
# (summary white, cyan) are darkened so they read as ink, not paper.
BG        = (255, 255, 255)                  # white paper
FREE      = (238, 240, 244)                  # near-white: free / unused headroom
CAT = {
    "overhead":  (74, 84, 120),             # deep slate-indigo (system frame)
    "user":      (226, 178, 46),            # gold — the human's turns pop
    "assistant": (56, 170, 92),             # green
    "thinking":  (34, 132, 108),            # teal-green
    "reasoning": (150, 92, 226),            # violet
    "bash":      (234, 126, 42),            # orange
    "tool":      (150, 150, 158),           # neutral gray
    "attach":    (214, 72, 150),            # magenta (distinct from violet)
    "summary":   (96, 108, 130),            # muted slate
    "file":      (32, 176, 200),            # cyan (base; per-file uses the wheel)
}
# files carry individual hues from a cohesive COOL family (cyan→azure→teal)
# so file cells read as one group, distinct from the categorical colors above
FILE_WHEEL = [(32, 172, 204), (46, 138, 216), (22, 194, 214), (78, 158, 226),
              (20, 148, 190), (104, 182, 220), (40, 116, 200), (0, 176, 222)]
# op colours for the FILES roll (SPEC FILES NOW: r cyan, w orange, e yellow),
# darkened for white-background contrast
OP = {"r": (40, 155, 170), "w": (215, 120, 55), "e": (200, 160, 35),
      "both": (40, 40, 48)}
# gauge zones (band fills use the light tone; lines/text use the dark tone)
ZONE_GREEN = (95, 200, 120)
ZONE_AMBER = (230, 170, 60)
ZONE_RED   = (230, 85, 85)
ZONE_GREEN_D = (40, 150, 75)
ZONE_AMBER_D = (195, 135, 25)
ZONE_RED_D   = (200, 50, 50)
# agent branch states (darkened for white bg)
AGENT_STATE = {"running": (30, 155, 170), "done": (55, 140, 70),
               "failed": (205, 55, 55)}

GRID_LINE = (185, 190, 200)
TEXT_DIM  = (95, 100, 112)
TEXT_FG   = (20, 22, 28)

CAT_ORDER = ("overhead", "user", "attach", "assistant", "thinking",
             "reasoning", "file", "bash", "tool", "summary")

# formal, self-explaining legend labels (the engine keys stay short internally)
CAT_LABEL = {
    "overhead":  "system overhead",
    "user":      "user input",
    "attach":    "injected context",
    "assistant": "assistant output",
    "thinking":  "visible reasoning",
    "reasoning": "hidden reasoning",
    "file":      "file content",
    "bash":      "shell output",
    "tool":      "tool results",
    "summary":   "compaction summary",
    "free":      "free headroom",
}

MAP_LADDER = (128, 256, 512, 1024, 2048, 4096, 8192, 16384)


def _n(rgb):
    """0-255 tuple -> matplotlib 0-1 tuple."""
    return (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def file_hue(fid):
    return FILE_WHEEL[fid % len(FILE_WHEEL)]


def zone_rgb(frac):
    return ZONE_RED if frac >= 0.85 else (ZONE_AMBER if frac >= 0.60 else ZONE_GREEN)


def zone_rgb_d(frac):
    """Darkened zone colour for lines/text legible on white."""
    return ZONE_RED_D if frac >= 0.85 else (ZONE_AMBER_D if frac >= 0.60
                                            else ZONE_GREEN_D)


def _scale(rgb, k):
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


def _blend(a, b, t):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


# ------------------------------------------------------------------ theme
def _apply_theme():
    plt.rcParams.update({
        "figure.facecolor": _n(BG),
        "savefig.facecolor": _n(BG),
        "axes.facecolor": _n(BG),
        "axes.edgecolor": _n(GRID_LINE),
        "axes.labelcolor": _n(TEXT_FG),
        "axes.titlecolor": _n(TEXT_FG),
        "text.color": _n(TEXT_FG),
        "xtick.color": _n(TEXT_DIM),
        "ytick.color": _n(TEXT_DIM),
        "grid.color": _n(GRID_LINE),
        "grid.alpha": 0.5,
        "font.family": "sans-serif",
        "font.size": 9,
        "axes.linewidth": 0.8,
        "figure.dpi": 130,
    })


_apply_theme()


def _save(fig, out):
    fig.savefig(out, facecolor=_n(BG), bbox_inches="tight", pad_inches=0.18)
    plt.close(fig)
    return out


def _fig_to_pil(fig):
    fig.canvas.draw()
    w, h = fig.canvas.get_width_height()
    buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
    return Image.fromarray(buf.copy(), "RGB")


def _write_gif(frames, out_gif, duration_ms=110):
    """Assemble PIL frames into a looping GIF quantized to ONE shared palette
    (frame 0's) so the animation stays flicker-free and small."""
    if not frames:
        return out_gif
    base = frames[0].convert("RGB").quantize(colors=128, method=Image.MEDIANCUT)
    q = [base] + [f.convert("RGB").quantize(palette=base, dither=Image.NONE)
                  for f in frames[1:]]
    q[0].save(out_gif, save_all=True, append_images=q[1:], loop=0,
              duration=duration_ms, disposal=2, optimize=True)
    return out_gif


def montage(frame_pngs, out_png, n=5, gap=10):
    """Horizontal keyframe strip from a list of PIL frames (evenly sampled)."""
    if not frame_pngs:
        return None
    m = len(frame_pngs)
    if m <= n:
        idx = list(range(m))
    else:
        idx = [round(i * (m - 1) / (n - 1)) for i in range(n)]
    imgs = [frame_pngs[i] for i in idx]
    h = max(im.height for im in imgs)
    w = sum(im.width for im in imgs) + gap * (len(imgs) - 1)
    strip = Image.new("RGB", (w, h), BG)
    x = 0
    for im in imgs:
        strip.paste(im, (x, (h - im.height) // 2))
        x += im.width + gap
    strip.save(out_png)
    return out_png


# ------------------------------------------------------------------ MAP grid
def _pick_cell(budget, lo=420, hi=1600):
    """Cell token-size from the fixed ladder so budget/cell lands in [lo,hi]."""
    for rung in MAP_LADDER:
        if budget / rung <= hi:
            if budget / rung >= lo or rung == MAP_LADDER[-1]:
                return rung
    return MAP_LADDER[-1]


_MAP_ROWS, _MAP_COLS = 24, 48    # fixed 2:1 box; the whole box == the budget

def _map_geometry(budget):
    """Fixed box dims + a cell size derived so rows*cols cells cover EXACTLY
    the budget — then used-cells/total-cells == R/budget and the free area
    reads true (a 99%-full window shows ~1% free, proportionally)."""
    return budget / float(_MAP_ROWS * _MAP_COLS), _MAP_ROWS, _MAP_COLS


def _map_grid_array(segs, budget, cell, rows, cols):
    """Row-major RGB grid: overhead + resident segs laid in prompt order as
    colour cells; the rest dim-slate free headroom up to budget."""
    total = rows * cols
    arr = np.zeros((total, 3), dtype=np.uint8)
    arr[:] = FREE
    pos = 0.0                                # fractional running cell cursor
    for s in segs:
        cat = s["cat"]
        col = file_hue(s["file"]) if (cat == "file" and s.get("file") is not None) \
            else CAT.get(cat, CAT["tool"])
        span = s["tok"] / cell
        a = int(math.floor(pos))
        b = int(math.floor(pos + span))
        a = max(0, min(a, total))
        b = max(a, min(b, total))
        if b == a and s["tok"] > 0 and a < total:
            b = a + 1                        # never let a real seg vanish
        arr[a:b] = col
        pos += span
    return arr.reshape(rows, cols, 3)


def _draw_map(ax, segs, budget, resident, cell, rows, cols, turn=None,
              show_free_note=True):
    grid = _map_grid_array(segs, budget, cell, rows, cols)
    ax.imshow(grid, interpolation="nearest", aspect="equal")
    # dark "grout" cell separators: a flat category block reads as packed tiles,
    # not an empty rectangle — and unlike white lines the grout adds no blank
    # feel, so the only light region left is the true (tiny) free headroom
    ax.set_xticks(np.arange(-0.5, cols, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    ax.grid(which="minor", color=(0.11, 0.12, 0.16), linewidth=0.5)
    ax.tick_params(which="both", length=0)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_edgecolor(_n(GRID_LINE))
        sp.set_linewidth(1.0)
    frac = resident / max(1, budget)
    zc = _n(zone_rgb_d(frac))
    ttl = "Context map" if turn is None else "Context map  ·  turn %d" % turn
    ax.text(0.0, 1.045, ttl, transform=ax.transAxes, ha="left", va="bottom",
            color=_n(TEXT_FG), fontsize=11, fontweight="bold")
    ax.text(1.0, 1.02, "R %s / %s  %.0f%%" % (_k(resident), _k(budget),
            100 * frac), transform=ax.transAxes, ha="right", va="bottom",
            color=zc, fontsize=9.5, fontweight="bold")
    if show_free_note:
        ax.text(0.0, -0.03, "cell = %s tok   ·   %d×%d grid   ·   dim = free "
                "headroom" % (_k(cell), rows, cols), transform=ax.transAxes,
                ha="left", va="top", color=_n(TEXT_DIM), fontsize=7.5)


def _k(n):
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 1000:
        return "%dk" % round(n / 1000)
    return str(n)


def _map_legend(ax, sess):
    cats = sess.cats_payload()
    items = [(c, cats.get(c, 0)) for c in CAT_ORDER if cats.get(c, 0) > 0]
    ax.axis("off")
    ax.text(0, 1.045, "Composition", transform=ax.transAxes, color=_n(TEXT_FG),
            fontsize=11, fontweight="bold", va="bottom")
    y = 0.95
    R = max(1, sess.resident())

    def _row(c, label, v, pct=True, dim=False):
        nonlocal y
        col = _n(FREE) if c == "free" else _n(CAT.get(c, CAT["tool"]))
        ax.add_patch(Rectangle((0.0, y - 0.038), 0.11, 0.045, color=col,
                     transform=ax.transAxes, clip_on=False))
        tc = _n(TEXT_DIM) if dim else _n(TEXT_FG)
        ax.text(0.16, y - 0.016, label, transform=ax.transAxes, color=tc,
                fontsize=8.4, va="center")
        val = _k(v) + ("  %.1f%%" % (100 * v / R) if pct else "")
        ax.text(1.0, y - 0.016, val, transform=ax.transAxes, color=tc,
                fontsize=8.2, va="center", ha="right", family="monospace")
        if c == "file":
            y -= 0.040
            ax.text(0.18, y - 0.006, "- one hue per distinct file",
                    transform=ax.transAxes, color=_n(TEXT_DIM), fontsize=7.0,
                    va="center", fontstyle="italic")
        y -= 0.062

    for c, v in items:
        _row(c, CAT_LABEL.get(c, c), v)
    _row("free", CAT_LABEL["free"], max(0, sess.budget - R), pct=False, dim=True)


def map_static(sess, out):
    segs = sess.build_map_segs()
    budget, R = sess.budget, sess.resident()
    cell, rows, cols = _map_geometry(budget)
    fig = plt.figure(figsize=(11.2, 5.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[2.15, 1.2], wspace=0.05)
    axm = fig.add_subplot(gs[0, 0])
    axl = fig.add_subplot(gs[0, 1])
    _draw_map(axm, segs, budget, R, cell, rows, cols)
    _map_legend(axl, sess)
    return _save(fig, out)


def map_animation_frames(sess, snapshots):
    """One PIL frame per turn from per-turn map snapshots (list of seg lists,
    index = turn). Fixed grid geometry so the box grows in a stable frame."""
    budget = sess.budget
    cell, rows, cols = _map_geometry(budget)
    frames = []
    turns = sess.turns
    for t, segs in enumerate(snapshots):
        R = turns[t]["resident"] if t < len(turns) else sess.resident()
        fig = plt.figure(figsize=(6.4, 5.2))
        ax = fig.add_axes([0.04, 0.06, 0.92, 0.86])
        _draw_map(ax, segs, budget, R, cell, rows, cols, turn=t,
                  show_free_note=False)
        frames.append(_fig_to_pil(fig))
        plt.close(fig)
    return frames


def map_animation(sess, snapshots, out_gif):
    return _write_gif(map_animation_frames(sess, snapshots), out_gif)


# ------------------------------------------------------------------ FILES roll
def _files_order(sess, top=40):
    fs = sorted(sess.files.values(), key=lambda f: -int(f["tok"]))[:top]
    return fs


def _ramp(tok):
    tok = abs(int(tok))
    if tok < 1000:
        return 0.42
    if tok < 4000:
        return 0.66
    if tok < 16000:
        return 0.88
    return 1.0


def _files_matrix(sess, files, nturns):
    fid_row = {f["id"]: i for i, f in enumerate(files)}
    # aggregate per (file,turn): op priority both>w>e>r, brightness by max tok
    ops = {}                                 # (row,turn) -> (op, tok)
    for fa in sess.faccess:
        row = fid_row.get(fa["file"])
        if row is None:
            continue
        t = fa["turn"]
        if not (0 <= t < nturns):
            continue
        key = (row, t)
        cur = ops.get(key)
        op = fa["op"]
        tok = fa["tok"]
        if cur is None:
            ops[key] = (op, tok)
        else:
            pop, ptok = cur
            nop = "both" if pop != op and pop not in (op,) else pop
            if pop == "both" or (pop != op):
                nop = "both"
            ops[key] = (nop, max(ptok, tok))
    return fid_row, ops


def _files_grid_array(files, ops, nturns, reveal=None):
    rows = max(1, len(files))
    arr = np.zeros((rows, max(1, nturns), 3), dtype=np.uint8)
    arr[:] = FREE
    lim = nturns if reveal is None else reveal
    for (row, t), (op, tok) in ops.items():
        if t >= lim:
            continue
        base = OP.get(op, OP["both"])
        arr[row, t] = _scale(base, _ramp(tok))
    return arr


# operation → marker shape. Shape (not just hue) carries the op, so read /
# write / edit are readable at a glance: read ○ · write △ · edit □ · multiple ◇
OP_MARK = {"r": "o", "w": "^", "e": "s", "both": "D"}


def _draw_files(ax, files, ops, nturns, reveal=None, turn=None):
    nrows = max(1, len(files))
    lim = nturns if reveal is None else reveal
    # faint alternating row bands so a file's marks are easy to track across
    for i in range(nrows):
        if i % 2:
            ax.axhspan(i - 0.5, i + 0.5, color=_n(GRID_LINE), alpha=0.10,
                       zorder=0)
    # bucket accesses by op, then scatter each op with its own marker + colour
    pts = {k: ([], [], []) for k in OP_MARK}
    for (row, t), (op, tok) in ops.items():
        if t >= lim:
            continue
        o = op if op in OP_MARK else "both"
        xs, ys, ss = pts[o]
        xs.append(t)
        ys.append(row)
        ss.append(16 + 34 * (_ramp(tok) - 0.42) / 0.58)   # size ∝ access size
    for o, (xs, ys, ss) in pts.items():
        if not xs:
            continue
        ax.scatter(xs, ys, s=ss, marker=OP_MARK[o], color=_n(OP[o]),
                   edgecolors="white", linewidths=0.4, zorder=3)
    ax.set_ylim(nrows - 0.5, -0.5)              # first file at the top
    ax.set_xlim(-0.5, nturns - 0.5)
    ax.set_yticks(range(nrows))
    labels = []
    for f in files:
        p = f["path"]
        labels.append(p if len(p) <= 30 else "…" + p[-29:])
    ax.set_yticklabels(labels, fontsize=6.6, family="monospace")
    for i, f in enumerate(files):
        ax.get_yticklabels()[i].set_color(_n(file_hue(f["id"])))
    ax.set_xlabel("turn", fontsize=8.5)
    ax.tick_params(axis="x", labelsize=7.5)
    ax.tick_params(axis="y", length=0)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_edgecolor(_n(GRID_LINE))
    ttl = "File traffic roll" if turn is None else \
        "File traffic roll  ·  turn %d" % turn
    ax.text(0.0, 1.02, ttl, transform=ax.transAxes, ha="left", va="bottom",
            color=_n(TEXT_FG), fontsize=11, fontweight="bold")


def _files_legend(ax):
    ax.axis("off")
    entries = [("read", "o", OP["r"]), ("write", "^", OP["w"]),
               ("edit", "s", OP["e"]), ("multiple", "D", OP["both"])]
    x = 0.0
    for name, mk, col in entries:
        ax.plot([x + 0.006], [0.5], marker=mk, color=_n(col), markersize=8,
                markeredgecolor="white", markeredgewidth=0.5, ls="none",
                transform=ax.transAxes, clip_on=False)
        ax.text(x + 0.028, 0.5, name, color=_n(TEXT_FG), fontsize=8,
                va="center", transform=ax.transAxes)
        x += 0.14
    ax.text(1.0, 0.5, "marker size ∝ access size (1k · 4k · 16k)",
            color=_n(TEXT_DIM), fontsize=7.5, va="center", ha="right",
            transform=ax.transAxes)


def files_static(sess, out):
    files = _files_order(sess)
    nturns = max(1, len(sess.turns))
    _, ops = _files_matrix(sess, files, nturns)
    fig = plt.figure(figsize=(10.5, max(3.2, 0.28 * len(files) + 1.6)))
    gs = fig.add_gridspec(2, 1, height_ratios=[len(files) + 2, 1.1],
                          hspace=0.12)
    ax = fig.add_subplot(gs[0, 0])
    axl = fig.add_subplot(gs[1, 0])
    _draw_files(ax, files, ops, nturns)
    _files_legend(axl)
    return _save(fig, out)


def files_animation_frames(sess):
    files = _files_order(sess)
    nturns = max(1, len(sess.turns))
    _, ops = _files_matrix(sess, files, nturns)
    frames = []
    h = max(2.6, 0.24 * len(files) + 1.2)
    for t in range(nturns):
        fig = plt.figure(figsize=(7.6, h))
        ax = fig.add_axes([0.28, 0.14, 0.68, 0.74])
        _draw_files(ax, files, ops, nturns, reveal=t + 1, turn=t)
        frames.append(_fig_to_pil(fig))
        plt.close(fig)
    return frames


def files_animation(sess, out_gif):
    return _write_gif(files_animation_frames(sess), out_gif)


# ------------------------------------------------------------------ divergence
def _agent_rows(sess):
    """Branch specs for the git-tree view: fork turn, work-scaled visible span,
    return tokens, packed into non-overlapping lanes (a lane frees once its
    branch merges back)."""
    ags = list(sess.agents.values())
    nturns = max(1, len(sess.turns))
    maxwork = max((int(a.get("own_tok") or 0) for a in ags), default=0)
    wmin = max(1.0, nturns * 0.05)            # min visible branch width
    wspan = nturns * 0.14                      # extra width for the busiest agent
    rows = []
    for a in ags:
        t0 = float(a.get("turn0", 0) or 0)
        rt1 = a.get("turn1")                   # real return turn (None = running)
        work = int(a.get("own_tok") or 0)
        ext = wmin + (wspan * work / maxwork if maxwork else 0.0)
        if rt1 is None:
            mx = nturns - 1.0                  # open branch: dangle to the edge
        else:
            mx = min(nturns - 1.0, max(float(rt1), t0 + ext))
        rows.append({
            "id": a["id"], "state": a.get("state", "done"),
            "wf": a.get("wf"), "type": a.get("agent_type") or "agent",
            "desc": a.get("desc"), "work": work,
            "ret": int(a.get("ret_tok") or 0),
            "t0": t0, "rt1": rt1, "mx": mx, "running": rt1 is None,
        })
    rows.sort(key=lambda r: (r["t0"], -r["work"]))
    lane_end, gap = [], nturns * 0.015
    for r in rows:
        placed = next((li for li in range(len(lane_end))
                       if r["t0"] > lane_end[li] + gap), None)
        if placed is None:
            lane_end.append(r["mx"]); placed = len(lane_end) - 1
        else:
            lane_end[placed] = r["mx"]
        r["lane"] = placed + 1
    return rows, nturns


def _scurve(x0, y0, x1, y1):
    """Smooth cubic S between two points, horizontal tangents at both ends."""
    mx = (x0 + x1) / 2.0
    return Path([(x0, y0), (mx, y0), (mx, y1), (x1, y1)],
                [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4])


def _draw_divergence(ax, rows, nturns, reveal=None, turn=None):
    ax.set_facecolor(_n(BG))
    lim = (nturns - 1) if reveal is None else reveal
    ax.plot([0, nturns - 1], [0, 0], color=_n(TEXT_FG), lw=3.2,
            solid_capstyle="round", zorder=3)                      # trunk
    ax.annotate("", xy=(nturns - 1 + nturns * 0.015, 0), xytext=(nturns - 1, 0),
                arrowprops=dict(arrowstyle="-|>", color=_n(TEXT_FG), lw=2))
    ax.text(0, -0.42, "main session", color=_n(TEXT_DIM), fontsize=8,
            va="top", family="monospace")
    maxlane = max((r["lane"] for r in rows), default=1)
    curve = nturns * 0.02
    for r in rows:
        if r["t0"] > lim:
            continue
        y = r["lane"]
        merged = (not r["running"]) and (reveal is None or lim >= (r["rt1"] or 0))
        st = r["state"] if merged else "running"
        col = _n(AGENT_STATE.get(st, AGENT_STATE["done"]))
        vx = r["mx"] if reveal is None else min(r["mx"], max(r["t0"] + curve, lim))
        bx0 = r["t0"] + curve
        bx1 = max(bx0, vx - (curve if merged else 0))
        ax.add_patch(PathPatch(_scurve(r["t0"], 0, bx0, y), fill=False,
                     edgecolor=col, lw=2.2, zorder=4))                # fork
        ax.plot([bx0, bx1], [y, y], color=col, lw=2.2, zorder=4,
                solid_capstyle="round")                              # body
        ncom = max(2, min(9, int(round(r["work"] / 6000.0))))
        if bx1 > bx0:
            for k in range(ncom):
                cx = bx0 + (bx1 - bx0) * (k + 0.5) / ncom
                ax.plot([cx], [y], "o", color=col, ms=3.0, zorder=5)  # commits
        ax.plot([r["t0"]], [0], "o", color=col, ms=5, zorder=6)       # spawn
        lab = r["type"]
        if r["wf"]:
            lab = "%s\u00b7%s" % (r["wf"][:8], lab)
        cxm = (bx0 + bx1) / 2.0
        ax.text(cxm, y + 0.16, lab, color=col, fontsize=7.0, ha="center",
                va="bottom", family="monospace", fontweight="bold")
        ax.text(cxm, y - 0.04, "%s work" % _k(r["work"]), color=_n(TEXT_DIM),
                fontsize=6.2, ha="center", va="top", family="monospace")
        if merged:
            ax.add_patch(PathPatch(_scurve(bx1, y, vx, 0), fill=False,
                         edgecolor=col, lw=2.2, zorder=4))            # merge
            if r["state"] == "failed":
                ax.plot([vx], [0], "x", color=col, ms=7, mew=2.2, zorder=6)
            else:
                ax.plot([vx], [0], "D", color=col, ms=6, zorder=6,
                        markerfacecolor=_n(BG), markeredgewidth=1.6)
            if r["ret"] > 0:
                ax.annotate("\u21a9 %s" % _k(r["ret"]), xy=(vx, 0),
                            xytext=(vx, 0.34), color=col, fontsize=6.8,
                            ha="center", va="bottom", family="monospace")
        elif r["running"]:
            ax.annotate("", xy=(vx + nturns * 0.012, y), xytext=(vx, y),
                        arrowprops=dict(arrowstyle="-|>", color=col, lw=1.8))
    ax.set_ylim(-1.1, maxlane + 1.15)
    ax.set_xlim(-nturns * 0.02, nturns * 1.06)
    ax.set_yticks([])
    ax.set_xlabel("turn   \u00b7   branch length \u2248 agent work   \u00b7   "
                  "\u21a9 tokens returned to parent", fontsize=8.0)
    ax.tick_params(axis="x", labelsize=7.5)
    for sp in ("top", "left", "right"):
        ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_edgecolor(_n(GRID_LINE))
    ttl = "Subagent branch tree" if turn is None else \
        "Subagent branch tree  \u00b7  turn %d" % turn
    ax.text(0.0, 1.04, ttl, transform=ax.transAxes, ha="left", va="bottom",
            color=_n(TEXT_FG), fontsize=11, fontweight="bold")
    for nm, cc in [("running", AGENT_STATE["running"]),
                   ("done", AGENT_STATE["done"]), ("failed", AGENT_STATE["failed"])]:
        ax.plot([], [], color=_n(cc), lw=2.4, label=nm)
    ax.legend(loc="lower right", fontsize=7.0, frameon=False,
              labelcolor=_n(TEXT_FG), ncol=3, handlelength=1.4,
              bbox_to_anchor=(1.0, 1.0))


def divergence_static(sess, out):
    rows, nturns = _agent_rows(sess)
    h = max(3.2, 0.85 * (max((r["lane"] for r in rows), default=1)) + 1.9)
    fig = plt.figure(figsize=(10.5, h))
    ax = fig.add_subplot(111)
    _draw_divergence(ax, rows, nturns)
    return _save(fig, out)


def divergence_animation_frames(sess):
    rows, nturns = _agent_rows(sess)
    h = max(3.2, 0.85 * (max((r["lane"] for r in rows), default=1)) + 1.9)
    frames = []
    for t in range(nturns):
        fig = plt.figure(figsize=(8.0, h))
        ax = fig.add_axes([0.03, 0.14, 0.94, 0.76])
        _draw_divergence(ax, rows, nturns, reveal=t, turn=t)
        frames.append(_fig_to_pil(fig))
        plt.close(fig)
    return frames


def divergence_animation(sess, out_gif):
    return _write_gif(divergence_animation_frames(sess), out_gif)


# ------------------------------------------------------------------ agents timeline
# A static mirror of the TUI's AGENTS tab: a fan-out concurrency strip over the
# turn axis (TUI "load strip") stacked above a per-agent Gantt whose right gutter
# reproduces the TUI ledger columns (own(log) bar, own-tok, ret-tok, amp, dur,
# agent · desc). Distinct from the divergence branch tree (fork/merge topology).
def _fmt_dur(ms):
    """Humanized duration, mirroring the TUI fmt_dur: 102ms, 8.4s, 10m58s."""
    if ms is None:
        return "—"
    ms = int(ms)
    if ms < 1000:
        return "%dms" % ms
    if ms < 100_000:
        return "%.1fs" % (ms / 1000.0)
    s = ms // 1000
    return "%dm%02ds" % (s // 60, s % 60)


def _own_log_frac(tok):
    """FIXED log token fraction (TUI law): <1k -> tiny edge, 1k->0, 10k->1/3,
    100k->2/3, 1M->full. Guards log10(0) before it can fire."""
    tok = int(tok or 0)
    if tok < 1000:
        return 0.03 if tok > 0 else 0.0
    frac = (math.log10(tok) - 3.0) / 3.0
    return max(0.03, min(1.0, frac))


def _agent_timeline_rows(sess):
    """Per-agent Gantt specs sorted by launch turn: span [t0, end] on the turn
    axis (running agents dangle to the axis end), plus the ledger metrics."""
    ags = list(sess.agents.values())
    nturns = max(1, len(sess.turns))
    end_turn = nturns - 1
    rows = []
    for a in ags:
        t0 = float(a.get("turn0", 0) or 0)
        rt1 = a.get("turn1")
        running = rt1 is None
        end = end_turn if running else float(rt1)
        end = max(t0, min(float(end_turn), end))
        own = int(a.get("own_tok") or 0)
        ret = a.get("ret_tok")
        ret = int(ret) if ret is not None else None
        if running or ret is None:
            amp = None
        else:
            amp = own / max(1, ret)
        rows.append({
            "id": a["id"], "state": a.get("state", "done"),
            "type": a.get("agent_type") or "agent", "desc": a.get("desc") or "",
            "wf": a.get("wf"), "own": own, "ret": ret, "amp": amp,
            "dur_ms": a.get("dur_ms"), "t0": t0, "end": end, "running": running,
        })
    rows.sort(key=lambda r: (r["t0"], -r["own"]))
    return rows, nturns


def _fanout_profile(rows, nturns):
    """Concurrency per turn t: count of agents with t0 <= t <= end."""
    counts = np.zeros(max(1, nturns), dtype=int)
    for r in rows:
        a = int(math.floor(r["t0"]))
        b = int(math.floor(r["end"]))
        a = max(0, min(a, nturns - 1))
        b = max(a, min(b, nturns - 1))
        counts[a:b + 1] += 1
    return counts


def _turn_ticks(nturns):
    if nturns <= 1:
        return [0]
    raw = np.linspace(0, nturns, min(9, nturns + 1))
    return sorted({int(round(x)) for x in raw if round(x) <= nturns})


def _draw_agents_timeline(fig, rows, nturns, reveal=None, turn=None):
    """Render fan-out strip (top) + per-agent Gantt (bottom) onto fig."""
    # ---- geometry: turn axis on the left, a ledger gutter on the right
    xlo = -nturns * 0.02
    gx = nturns * 1.04                          # gutter start
    logmax = nturns * 0.13                       # own(log) bar full width
    gx2 = gx + logmax + nturns * 0.03            # metrics text start
    gx3 = gx2 + nturns * 0.56                    # identity (type · desc) start
    xhi = gx3 + nturns * 0.78
    accent = AGENT_STATE["running"]              # cool teal fan-out band

    n = max(1, len(rows))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.0, max(1.4, 0.62 * n)],
                          hspace=0.16)
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)

    # ---- TOP: fan-out concurrency strip
    lim = nturns if reveal is None else min(nturns, reveal + 1)
    counts = _fanout_profile(rows, nturns)
    if reveal is not None:
        counts = counts.copy()
        counts[lim:] = 0
    xs = np.arange(nturns)
    ax1.fill_between(xs, counts, step="mid", color=_n(accent), alpha=0.22,
                     zorder=2)
    ax1.step(xs, counts, where="mid", color=_n(accent), lw=1.4, zorder=3)
    cmax = int(counts.max()) if counts.size else 0
    ax1.set_ylim(0, max(1, cmax) * 1.25)
    ax1.set_yticks(range(0, max(1, cmax) + 1))
    ax1.set_ylabel("agents", fontsize=8.5)
    ax1.tick_params(axis="y", labelsize=7.5)
    ax1.grid(True, axis="y", alpha=0.25)
    for sp in ("top", "right"):
        ax1.spines[sp].set_visible(False)
    ax1.spines["left"].set_edgecolor(_n(GRID_LINE))
    ax1.spines["bottom"].set_edgecolor(_n(GRID_LINE))
    plt.setp(ax1.get_xticklabels(), visible=False)
    ttl = "Agent fan-out timeline" if turn is None else \
        "Agent fan-out timeline  ·  turn %d" % turn
    ax1.text(0.0, 1.12, ttl, transform=ax1.transAxes, ha="left", va="bottom",
             color=_n(TEXT_FG), fontsize=11, fontweight="bold")
    own_total = sum(r["own"] for r in rows)
    nrun = sum(1 for r in rows if r["running"])
    ax1.text(1.0, 1.12, "fan-out %s tok  ·  %d● running" %
             (_k(own_total), nrun), transform=ax1.transAxes, ha="right",
             va="bottom", color=_n(TEXT_DIM), fontsize=8.5)
    for nm, cc in [("running", AGENT_STATE["running"]),
                   ("done", AGENT_STATE["done"]), ("failed", AGENT_STATE["failed"])]:
        ax1.plot([], [], color=_n(cc), lw=3.0, label=nm)
    ax1.legend(loc="upper left", fontsize=7.0, frameon=False,
               labelcolor=_n(TEXT_FG), ncol=3, handlelength=1.4)

    # ---- BOTTOM: per-agent Gantt + ledger gutter
    hh = 0.24                                    # bar half-height
    ax2.axvline(nturns - 1, color=_n(GRID_LINE), lw=0.8, ls=(0, (2, 3)),
                alpha=0.7, zorder=1)
    # gutter column header
    ax2.text(gx, n - 0.15, "own(log)", color=_n(TEXT_DIM), fontsize=6.6,
             ha="left", va="bottom", family="monospace")
    ax2.text(gx2, n - 0.15, "%6s %6s %6s %7s" %
             ("own", "ret", "amp", "dur"), color=_n(TEXT_DIM), fontsize=6.6,
             ha="left", va="bottom", family="monospace")
    ax2.text(gx3, n - 0.15, "agent · description", color=_n(TEXT_DIM),
             fontsize=6.6, ha="left", va="bottom", family="monospace")

    for i, r in enumerate(rows):
        y = n - 1 - i                            # first launched at the top
        col = _n(AGENT_STATE.get(r["state"], AGENT_STATE["done"]))
        revealed = reveal is None or r["t0"] <= reveal
        if not revealed:
            continue
        vend = r["end"] if reveal is None else min(r["end"], max(r["t0"], reveal))
        # a very short parent-turn span (a subagent occupies ~1 parent turn even
        # when it did lots of internal work) would collapse to a bare marker —
        # give every bar a minimum visible width, anchoring the end marker to it
        ve = r["t0"] + max(vend - r["t0"], nturns * 0.02)
        # concurrency guide band across the row
        ax2.axhspan(y - 0.5, y + 0.5, color=_n(GRID_LINE),
                    alpha=0.05 if i % 2 else 0.0, zorder=0)
        # timeline bar
        ax2.add_patch(Rectangle((r["t0"], y - hh), ve - r["t0"],
                      2 * hh, facecolor=col, edgecolor="none", alpha=0.85,
                      zorder=3))
        # launch marker
        ax2.plot([r["t0"]], [y], "o", color=col, ms=5, zorder=5)
        # end marker: diamond (done/failed) or open arrow (running)
        running_now = r["running"] or (reveal is not None and vend < r["end"])
        if running_now:
            ax2.annotate("", xy=(ve + nturns * 0.012, y), xytext=(ve, y),
                         arrowprops=dict(arrowstyle="-|>", color=col, lw=1.6),
                         zorder=5)
        elif r["state"] == "failed":
            ax2.plot([ve], [y], "x", color=col, ms=7, mew=2.0, zorder=6)
        else:
            ax2.plot([ve], [y], "D", color=col, ms=5, zorder=6,
                     markerfacecolor=_n(BG), markeredgewidth=1.4)

        # ---- ledger gutter: own(log) bar
        ax2.add_patch(Rectangle((gx, y - 0.10), logmax, 0.20,
                      facecolor=_n(GRID_LINE), edgecolor="none", alpha=0.35,
                      zorder=2))
        ax2.add_patch(Rectangle((gx, y - 0.10), logmax * _own_log_frac(r["own"]),
                      0.20, facecolor=col, edgecolor="none", zorder=3))
        # metrics columns
        amp = "—" if r["amp"] is None else (
            "%.0f×" % r["amp"] if r["amp"] >= 100 else "%.1f×" % r["amp"])
        ret = "—" if r["ret"] is None else _k(r["ret"])
        ax2.text(gx2, y, "%6s %6s %6s %7s" % (_k(r["own"]), ret, amp,
                 _fmt_dur(r["dur_ms"])), color=_n(TEXT_FG), fontsize=7.0,
                 ha="left", va="center", family="monospace")
        # identity: agent type · description
        ident = r["type"]
        if r["wf"]:
            ident = "%s·%s" % (r["wf"][:8], ident)
        desc = r["desc"]
        if len(desc) > 34:
            desc = desc[:33] + "…"
        label = ident if not desc else "%s · %s" % (ident, desc)
        ax2.text(gx3, y, label, color=col, fontsize=7.0, ha="left",
                 va="center", family="monospace", fontweight="bold")

    ax2.set_ylim(-0.7, n - 0.3 + 0.6)
    ax2.set_xlim(xlo, xhi)
    ax2.set_xticks(_turn_ticks(nturns))
    ax2.set_yticks([])
    ax2.set_xlabel("turn", fontsize=8.5)
    ax2.tick_params(axis="x", labelsize=7.5)
    for sp in ("top", "left", "right"):
        ax2.spines[sp].set_visible(False)
    ax2.spines["bottom"].set_edgecolor(_n(GRID_LINE))


def _empty_agents_fig(out):
    fig = plt.figure(figsize=(10.5, 2.4))
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0.0, 1.0, "Agent fan-out timeline", transform=ax.transAxes,
            ha="left", va="top", color=_n(TEXT_FG), fontsize=11,
            fontweight="bold")
    ax.text(0.5, 0.45, "no subagents were spawned in this session",
            transform=ax.transAxes, ha="center", va="center",
            color=_n(TEXT_DIM), fontsize=10, family="monospace")
    return _save(fig, out)


def agents_timeline_static(sess, out):
    rows, nturns = _agent_timeline_rows(sess)
    if not rows:
        return _empty_agents_fig(out)
    h = max(3.0, 1.5 + 0.52 * len(rows))
    fig = plt.figure(figsize=(10.5, h))
    _draw_agents_timeline(fig, rows, nturns)
    return _save(fig, out)


def agents_timeline_animation_frames(sess):
    rows, nturns = _agent_timeline_rows(sess)
    if not rows:
        return []
    h = max(3.0, 1.5 + 0.52 * len(rows))
    frames = []
    for t in range(nturns):
        fig = plt.figure(figsize=(9.0, h))
        _draw_agents_timeline(fig, rows, nturns, reveal=t, turn=t)
        frames.append(_fig_to_pil(fig))
        plt.close(fig)
    return frames


def agents_timeline_animation(sess, out_gif):
    return _write_gif(agents_timeline_animation_frames(sess), out_gif)


# ------------------------------------------------------------------ EKG trend
def ekg_static(sess, out):
    turns = sess.turns
    n = len(turns)
    if n == 0:
        fig = plt.figure(figsize=(10.5, 4))
        _save(fig, out)
        return out
    B = max(1, sess.budget)
    xs = list(range(n))
    R = [t["resident"] for t in turns]
    C = [t["waterline"] for t in turns]
    out_t = [t["out"] for t in turns]
    cost_t = [sess.turn_payload(i)["cost_u"] for i in range(n)]

    fig = plt.figure(figsize=(10.5, 6.2))
    gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 0.7, 0.7], hspace=0.28)
    ax = fig.add_subplot(gs[0, 0])
    axo = fig.add_subplot(gs[1, 0], sharex=ax)
    axc = fig.add_subplot(gs[2, 0], sharex=ax)

    # zone bands
    ax.axhspan(0, 0.60 * B, color=_n(ZONE_GREEN), alpha=0.05)
    ax.axhspan(0.60 * B, 0.85 * B, color=_n(ZONE_AMBER), alpha=0.07)
    ax.axhspan(0.85 * B, B, color=_n(ZONE_RED), alpha=0.08)
    ax.axhline(0.60 * B, color=_n(ZONE_AMBER), lw=0.8, ls=(0, (4, 3)), alpha=0.6)
    ax.axhline(0.85 * B, color=_n(ZONE_RED), lw=0.8, ls=(0, (4, 3)), alpha=0.6)
    ax.axhline(B, color=_n(TEXT_DIM), lw=1.0, ls="--", alpha=0.7)
    ax.text(n * 0.995, B, " budget %s" % _k(B), color=_n(TEXT_DIM),
            fontsize=7.5, va="bottom", ha="right")
    ax.text(n * 0.005, 0.85 * B, " 85%", color=_n(ZONE_RED), fontsize=7,
            va="bottom")
    ax.text(n * 0.005, 0.60 * B, " 60%", color=_n(ZONE_AMBER), fontsize=7,
            va="bottom")

    # waterline + R (zone-coloured, dotted)
    ax.plot(xs, C, color=_n((60, 130, 160)), lw=1.0, alpha=0.7,
            label="waterline C")
    final_frac = R[-1] / B
    rc = _n(zone_rgb_d(final_frac))
    ax.fill_between(xs, R, color=rc, alpha=0.10)
    ax.plot(xs, R, color=rc, lw=1.7, ls=(0, (1, 1.2)), label="resident R")

    # compaction ▼ / rebuild ≈ markers
    ymax = B
    for c in sess.compactions:
        t = c["turn"]
        if 0 <= t < n:
            ax.plot([t], [R[t]], marker="v", color=_n((170, 60, 175)),
                    ms=8, zorder=6)
    for rb in sess.rebuilds:
        t = rb["turn"]
        if 0 <= t < n:
            ax.text(t, R[t], "≈", color=_n(ZONE_AMBER_D), fontsize=13,
                    ha="center", va="bottom", zorder=6, fontweight="bold")
    # legend proxies for markers
    if sess.compactions:
        ax.plot([], [], "v", color=_n((170, 60, 175)), label="▼ compaction")
    if sess.rebuilds:
        ax.plot([], [], marker="$≈$", color=_n(ZONE_AMBER_D), ls="none",
                label="≈ rebuild")

    ax.set_ylim(0, B * 1.02)
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_ylabel("resident tokens")
    ax.set_yticks([0, 0.25 * B, 0.5 * B, 0.75 * B, B])
    ax.set_yticklabels([_k(v) for v in (0, 0.25 * B, 0.5 * B, 0.75 * B, B)])
    ax.set_title("Resident trend (EKG)", color=_n(TEXT_FG),
                 fontsize=11, loc="left", pad=8, fontweight="bold")
    ax.grid(True, axis="y", alpha=0.25)
    ax.legend(loc="upper left", fontsize=7.5, frameon=False,
              labelcolor=_n(TEXT_FG), ncol=2)
    plt.setp(ax.get_xticklabels(), visible=False)

    # out/turn lane (0-16k)
    axo.bar(xs, [min(v, 16000) for v in out_t], color=_n(ZONE_GREEN_D),
            width=1.0, alpha=0.85)
    axo.set_ylim(0, 16000)
    axo.set_yticks([0, 16000])
    axo.set_yticklabels(["0", "16k"], fontsize=6.5)
    axo.set_ylabel("out/t", fontsize=7.5, rotation=0, ha="right", va="center")
    axo.grid(True, axis="y", alpha=0.2)
    plt.setp(axo.get_xticklabels(), visible=False)

    # cost lane (0-100ku)
    axc.bar(xs, [min(v, 100) for v in cost_t], color=_n(ZONE_AMBER_D),
            width=1.0, alpha=0.85)
    axc.set_ylim(0, 100)
    axc.set_yticks([0, 100])
    axc.set_yticklabels(["0", "100"], fontsize=6.5)
    axc.set_ylabel("ku/t", fontsize=7.5, rotation=0, ha="right", va="center")
    axc.set_xlabel("turn", fontsize=8.5)
    axc.grid(True, axis="y", alpha=0.2)

    return _save(fig, out)
