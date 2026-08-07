#!/usr/bin/env python3
"""vibemeter — an analog 'AI usage' meter for the amtr README.

Everything is computed: braille sub-pixel arc + graduations (the braille
canvas is isotropic: cell = w x 2w, subpixel = w/2 x w/2), a solid red
wedge band, a bold pegged needle, hub, readout window, housing.
"""
import math

RS = 54                 # arc radius, subpixels
CW, CH = 78, 17         # dial canvas, cells
SW, SH = CW * 2, CH * 4
CXS, CYS = SW // 2, SH - 8
NEEDLE_DEG = 8
RED_DEG = 26

sub = [[0] * SW for _ in range(SH)]
grid = None

def sset(x, y):
    if 0 <= x < SW and 0 <= y < SH:
        sub[y][x] = 1

def spolar(deg, r):
    t = math.radians(deg)
    return CXS + r * math.cos(t), CYS - r * math.sin(t)

def cell(x, y):
    return int(x) // 2, int(y) // 4

def put(c, r, s):
    for i, ch in enumerate(s):
        if 0 <= c + i < CW and 0 <= r < CH:
            grid[r][c + i] = ch

# ---- braille layer: arc (single crisp stroke) + graduations ----------------
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

# needle: vector stroke in the subpixel layer (thick: ±1 subpixel normal)
for i in range(600):
    r = (RS - 10) * i / 599
    x, y = spolar(NEEDLE_DEG, r)
    for oy in (-1, 0):
        sset(int(round(x)), int(round(y)) + oy)

BR = [(0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
      (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80)]
grid = []
for cy in range(CH):
    row = []
    for cx in range(CW):
        m = 0
        for dx, dy, bit in BR:
            if sub[cy * 4 + dy][cx * 2 + dx]:
                m |= bit
        row.append(chr(0x2800 + m) if m else " ")
    grid.append(row)

# ---- text layer ------------------------------------------------------------
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
            put(c, r, "█" if ang < 9 else ("▓" if ang < 18 else "▒"))

# tick labels
for deg, label in [(180, "none"), (135, "copilot"), (90, "paired"),
                   (45, "agentic"), (7, "VIBE")]:
    if deg in (0, 180):
        x, y = spolar(deg, RS)          # center under the baseline foot
        r = cell(*spolar(deg, RS + 13))[1]
        c = cell(x, y)[0]
    else:
        x, y = spolar(deg, RS + 13)
        c, r = cell(x, y)
    c = max(1, min(CW - len(label) - 1, c - len(label) // 2))
    put(c, r, label)

# tip arrowhead where the vector stroke ends
tc, tr = cell(*spolar(NEEDLE_DEG, RS - 8))
put(tc, tr, "▶")

# hub
pc, pr = cell(CXS, CYS)
put(pc - 1, pr - 1, "◢█◣")
put(pc - 1, pr, "▐█▌")

dial = ["".join(r).rstrip() for r in grid]
while dial and not dial[-1]:
    dial.pop()

# ---- readout window + housing ---------------------------------------------
window = [
    "╔══════════════╗",
    "║  98 %  VIBE  ║",
    "╚══════════════╝",
]
plate = "VU · vibe units"

W = CW + 2                      # housing inner width
out = []
out.append("╭" + "─" * W + "╮")
out.append("│ ⊙" + " " * (W - 4) + "⊙ │")
for line in dial:
    out.append("│ " + line.ljust(W - 2) + " │")
wpad = (W - len(window[0])) // 2
for wl in window:
    out.append("│ " + (" " * wpad + wl).ljust(W - 2) + " │")
ppad = (W - len(plate)) // 2
out.append("│ " + (" " * ppad + plate).ljust(W - 2) + " │")
out.append("│ ⊙" + " " * (W - 4) + "⊙ │")
out.append("╰" + "─" * W + "╯")

print("\n".join(out))
