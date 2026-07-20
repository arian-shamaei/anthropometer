Output "docs/assets/context-fill.gif"

Set FontFamily "JetBrainsMono NF"
Set FontSize 14
Set Width 1400
Set Height 760
Set Padding 8
Set Framerate 24
Set Theme { "name": "amtr", "background": "#0d0f13", "foreground": "#aab1bd", "black": "#0d0f13", "red": "#e06c75", "green": "#98c379", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#aab1bd", "brightBlack": "#5c6370", "brightRed": "#e06c75", "brightGreen": "#98c379", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#56b6c2", "brightWhite": "#ffffff", "selectionBackground": "#3e4451", "cursorColor": "#0d0f13" }

# Launch the real session (absolute engine + session paths) and let it settle,
# then jump the turn cursor to the very start (turn 0) — all off-camera.
# The app has no reachable Home key in VHS, so repeated Left (which clamps at
# turn 0) walks the cursor to the very start.
Hide
Type "amtr --engine ./amtr_engine.py --session ./tests/fixtures/showcase.jsonl"
Enter
Sleep 4s
Left@8ms 60
Sleep 800ms
Show

# On camera: hold the near-empty map a beat, then step forward turn-by-turn.
# The context map fills, the EKG climbs. Overshoot the ~41 turns (cursor clamps).
Sleep 900ms
Right@200ms 46
Sleep 400ms

# Snap to LIVE (Esc = ack alert + go_live) and hold the full frame before loop.
Escape
Sleep 2s
