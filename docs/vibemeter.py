#!/usr/bin/env python3
"""vibemeter — the analog 'AI usage' meter on the README.

Everything is computed, nothing placed by eye: a braille sub-pixel arc
(the braille canvas is isotropic: cell = w x 2w, subpixel = w/2 x w/2),
inward graduations, a solid red wedge band, and a needle drawn as a true
sub-pixel vector stroke at its angle.

    python3 docs/vibemeter.py            # text render to stdout
    python3 docs/vibemeter.py --png OUT  # amtr-theme PNG (needs Pillow)

The PNG is the artifact the README embeds: web fonts give braille glyphs
inconsistent advances, so the text form only aligns in a real terminal.
"""
import math
import sys

RS = 54                 # arc radius, subpixels
CW, CH = 78, 17         # dial canvas, cells
SW, SH = CW * 2, CH * 4
CXS, CYS = SW // 2, SH - 8
NEEDLE_DEG = 8
RED_DEG = 26

sub = [[0] * SW for _ in range(SH)]        # face: arc + graduations
nsub = [[0] * SW for _ in range(SH)]       # needle stroke (own color)

def sset(x, y, layer=sub):
    if 0 <= x < SW and 0 <= y < SH:
        layer[y][x] = 1

def spolar(deg, r):
    t = math.radians(deg)
    return CXS + r * math.cos(t), CYS - r * math.sin(t)

def cell(x, y):
    return int(x) // 2, int(y) // 4

# ---- braille layers --------------------------------------------------------
for i in range(RED_DEG * 10, 1801):
    x, y = spolar(i / 10, RS)
    sset(int(round(x)), int(round(y)))

# graduations: majors only, stroked INWARD from the arc (instrument style)
for deg in range(0, 181, 45):
    horiz = deg in (0, 180)
    a, b = (RS - 4, RS + 2) if horiz else (RS - 8, RS)
    for k in range(17):
        x, y = spolar(deg, a + (b - a) * k / 16)
        sset(int(round(x)), int(round(y)))

# needle: vector stroke in its own subpixel layer
for i in range(600):
    r = (RS - 10) * i / 599
    x, y = spolar(NEEDLE_DEG, r)
    for oy in (-1, 0):
        sset(int(round(x)), int(round(y)) + oy, nsub)

BR = [(0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
      (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80)]
grid, cls = [], []                          # char + color-class per cell
for cy in range(CH):
    grow, crow = [], []
    for cx in range(CW):
        m = nm = 0
        for dx, dy, bit in BR:
            if sub[cy * 4 + dy][cx * 2 + dx]:
                m |= bit
            if nsub[cy * 4 + dy][cx * 2 + dx]:
                nm |= bit
        both = m | nm
        grow.append(chr(0x2800 + both) if both else " ")
        crow.append("n" if nm else ("a" if m else " "))
    grid.append(grow)
    cls.append(crow)

def put(c, r, s, k):
    for i, ch in enumerate(s):
        if 0 <= c + i < CW and 0 <= r < CH:
            grid[r][c + i] = ch
            cls[r][c + i] = k

# ---- text layers -----------------------------------------------------------
# red wedge band: solid fill between RS-7 and RS+3 for 0..RED_DEG degrees
for r in range(CH):
    for c in range(CW):
        dx = (2 * c + 1) - CXS
        dy = CYS - (4 * r + 2)
        rad = math.hypot(dx, dy)
        if dy < -2 or dx <= 0:
            continue
        ang = math.degrees(math.atan2(dy, dx))
        if 0 <= ang <= RED_DEG and RS - 7 <= rad <= RS + 3:
            if ang < 9:
                put(c, r, "█", "R")
            elif ang < 18:
                put(c, r, "▓", "r")
            else:
                put(c, r, "▒", "p")

for deg, label in [(180, "none"), (135, "copilot"), (90, "paired"),
                   (45, "agentic"), (7, "VIBE")]:
    if deg in (0, 180):
        r = cell(*spolar(deg, RS + 13))[1]   # center under the baseline foot
        c = cell(*spolar(deg, RS))[0]
    else:
        c, r = cell(*spolar(deg, RS + 13))
    c = max(1, min(CW - len(label) - 1, c - len(label) // 2))
    put(c, r, label, "V" if label == "VIBE" else "l")

# tip arrowhead where the vector stroke ends, then the hub
tc, tr = cell(*spolar(NEEDLE_DEG, RS - 8))
put(tc, tr, "▶", "n")
pc, pr = cell(CXS, CYS)
put(pc - 1, pr - 1, "◢█◣", "n")
put(pc - 1, pr, "▐█▌", "n")

dial = ["".join(r).rstrip() for r in grid]
dcls = ["".join(r) for r in cls]
while dial and not dial[-1]:
    dial.pop()
    dcls.pop()

# ---- readout window + housing ---------------------------------------------
window = ["╔══════════════╗",
          "║  98 %  VIBE  ║",
          "╚══════════════╝"]
plate = "VU · vibe units"
W = CW + 2

out, ocls = [], []
def emit(line, k):
    out.append(line)
    ocls.append(k)

emit("╭" + "─" * W + "╮", "f" * (W + 2))
emit("│ ⊙" + " " * (W - 4) + "⊙ │", "f" * (W + 2))
for line, kline in zip(dial, dcls):
    emit("│ " + line.ljust(W - 2) + " │", "f " + kline.ljust(W - 2) + " f")
wpad = (W - len(window[0])) // 2
for wl in window:
    emit("│ " + (" " * wpad + wl).ljust(W - 2) + " │",
         "f " + " " * wpad + "w" * len(wl) + " " * (W - 2 - wpad - len(wl)) + " f")
ppad = (W - len(plate)) // 2
emit("│ " + (" " * ppad + plate).ljust(W - 2) + " │",
     "f " + " " * ppad + "l" * len(plate) + " " * (W - 2 - ppad - len(plate)) + " f")
emit("│ ⊙" + " " * (W - 4) + "⊙ │", "f" * (W + 2))
emit("╰" + "─" * W + "╯", "f" * (W + 2))

# ---- output ----------------------------------------------------------------
if "--png" not in sys.argv:
    print("\n".join(out))
    sys.exit(0)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

outpath = sys.argv[sys.argv.index("--png") + 1]
FS = 28
font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", FS)
cw = font.getlength("M")
chh = int(FS * 1.30)
PAL = {
    "f": (92, 99, 112),      # housing
    "l": (170, 177, 189),    # labels
    "V": (224, 108, 117),    # VIBE label
    "w": (152, 195, 121),    # readout window
}
ARC = (150, 158, 172)
NEEDLE = (238, 238, 238)
BAND = [(224, 108, 117), (168, 50, 50), (112, 38, 38)]   # 0-9, 9-18, 18-26 deg
PAD = 26
img = Image.new("RGB", (int(PAD * 2 + cw * max(len(l) for l in out)),
                        int(PAD * 2 + chh * len(out))), (13, 15, 19))
d = ImageDraw.Draw(img)

# text layer: housing, labels, window ONLY — dial geometry is drawn as vectors
for r, (line, kline) in enumerate(zip(out, ocls)):
    for c, ch in enumerate(line):
        if ch == " ":
            continue
        k = kline[c] if c < len(kline) else " "
        if k in PAL:
            d.text((PAD + c * cw, PAD + r * chh), ch, fill=PAL[k], font=font)

# subpixel space → pixels; dial starts after 2 chrome rows + 2 frame cols
sx, sy = cw / 2.0, chh / 4.0
OX, OY = PAD + 2 * cw, PAD + 2 * chh
def px(xs, ys):
    return OX + xs * sx, OY + ys * sy

cx, cy = px(CXS, CYS)
rx, ry = RS * sx, RS * sy

def ell(rr):
    return [cx - rr * sx, cy - rr * sy, cx + rr * sx, cy + rr * sy]

# red band: thick arc spanning RS-7..RS+3, three gradient segments
bw = int(10 * sy)
for (a0, a1), col in zip([(0, 9), (9, 18), (18, RED_DEG)], BAND):
    d.arc(ell(RS - 2), start=-a1, end=-a0, fill=col, width=bw)

# arc + inward major graduations
d.arc(ell(RS), start=180, end=-RED_DEG, fill=ARC, width=3)
for deg in (45, 90, 135):        # no baseline feet in the vector render —
    t = math.radians(deg)        # the arc ends and the band terminate the scale
    d.line([px(CXS + (RS - 8) * math.cos(t), CYS - (RS - 8) * math.sin(t)),
            px(CXS + RS * math.cos(t), CYS - RS * math.sin(t))],
           fill=ARC, width=3)

# needle: one anti-aliased stroke, hub circle, arrowhead at the band
t = math.radians(NEEDLE_DEG)
tipx, tipy = px(CXS + (RS - 5) * math.cos(t), CYS - (RS - 5) * math.sin(t))
d.line([(cx, cy), (tipx, tipy)], fill=NEEDLE, width=5)
ah = 9
d.polygon([(tipx + ah * math.cos(t) * 1.6, tipy - ah * math.sin(t) * 1.6),
           (tipx - ah * math.sin(t), tipy - ah * math.cos(t)),
           (tipx + ah * math.sin(t), tipy + ah * math.cos(t))], fill=NEEDLE)
d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=NEEDLE)
d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(13, 15, 19))

img.save(outpath)
print("wrote", outpath, img.size)
