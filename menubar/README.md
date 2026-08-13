# amtrino

The macOS menu bar companion for amtr: the TUI's system-wide wall shrunk to
a status item. Swift/AppKit, built with SPM — no Xcode project.

- **Data**: spawns the bundled `amtr_engine.py --fleet --live-only`
  (SPEC f2) and renders its JSON stream. Nothing else; the engine owns all
  data, exactly like the TUI.
- **Grid mode** (default): up to 9 live sessions as dots, identity-colored
  by the same `gen_palette(sess_seed(id))` the TUI tiles use. Busy dots
  pulse (per-session phase), a finished response flashes white for 6 s,
  stalled gets an amber ring, shell renders hollow, idle dim.
- **Single mode**: one session (pinned or auto-busiest) as a draining
  gradient tank, or as `NN%` zone-colored text.
- **Menu**: per-session show/hide (grid) or pin (single) with a mini-map
  image locating each session's dot, a live-animated legend, and an
  Options submenu: theme, single style, notify-on-finish, launch at login
  (bundled app only).
- **Themes** (Options ▸ Theme): identity (default, TUI-matching) · pastel ·
  monochrome · pressure zones (color = context fill) · vaporwave · matrix ·
  ember · candy. Fun themes key each session to a stable position on the
  theme's signature ramp (seed-keyed — surface-stop luma is deliberately
  uniform, so a luma key would collapse identity).

```
swift build                      # dev build
.build/debug/AmtrBar --selfcheck # golden palette parity vs rust (no test fw needed)
sh ../packaging/build-menubar.sh # assemble + sign + selfcheck amtrino.app
```

Dev affordances: `AMTRINO_ENGINE=/path/to/amtr_engine.py` overrides the
bundled engine; `AMTRINO_DUMP=/tmp/frame.png` writes every rendered icon
frame for visual validation.

Release signing: `build-menubar.sh` auto-signs with a Developer ID
Application cert (hardened runtime) when one is in the keychain, then
notarizes + staples when `xcrun notarytool store-credentials amtrino`
has been run once; otherwise it falls back to ad-hoc (local dev).

The golden palette values in `Selfcheck.swift` are pinned against
`rust/src/main.rs::palette_golden_cross_language` — update both only on a
deliberate palette change.
