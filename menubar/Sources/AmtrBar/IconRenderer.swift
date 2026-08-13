// Draws the status-item image. Two modes (SPEC's systemwide view, shrunk to
// a menu bar): SINGLE — one session as a draining gradient tank (the TUI
// tile in miniature; % text is handled as button title, not an image) —
// and GRID — up to 9 sessions as a 3×3 dot matrix, identity color per
// session, status carried by style and animation:
//   busy     pulsing bright (per-session phase so dots don't march in step)
//   finished 2 Hz white flash for 6 s (the busy→idle edge)
//   stalled  amber ring around a dim dot
//   shell    hollow identity ring
//   idle     dim identity dot
// All time-varying styling is a pure function of (state, now) — the redraw
// timer just re-renders, the TUI clock discipline.

import AppKit

enum IconRenderer {
    static let barHeight: CGFloat = 18

    // MARK: single-session tank

    /// The TUI wall tile in miniature: palette gradient fills the fraction
    /// of context LEFT (drain law), free space is the dim tank body.
    static func tankImage(_ d: DisplaySession, now: Date) -> NSImage {
        let w: CGFloat = 12, h: CGFloat = barHeight
        let img = NSImage(size: NSSize(width: w, height: h), flipped: false) { _ in
            let pal = themedStops(d)
            let body = NSRect(x: 1, y: 1, width: w - 2, height: h - 2)
            let path = NSBezierPath(roundedRect: body, xRadius: 2.5, yRadius: 2.5)
            nsColor(cDim, alpha: 0.35).setFill()
            path.fill()
            // context left, bottom-up, gradient dark (deep) → vivid (surface)
            let left = 1.0 - (d.sess.fill ?? 0.0)
            let boost = pulseBoost(d, now: now)
            if left > 0.01 {
                NSGraphicsContext.current?.saveGraphicsState()
                path.addClip()
                let fillH = body.height * CGFloat(left)
                let slices = max(3, Int(fillH))
                for i in 0..<slices {
                    let t = Double(i) / Double(max(1, slices - 1))
                    var c = paletteColor(pal, t)
                    c = scaleRGB(c, boost)
                    nsColor(c).setFill()
                    let y = body.minY + fillH * CGFloat(i) / CGFloat(slices)
                    NSRect(x: body.minX, y: y, width: body.width,
                           height: fillH / CGFloat(slices) + 0.5).fill()
                }
                NSGraphicsContext.current?.restoreGraphicsState()
            }
            // status rim: stalled amber, dead red, finished white flash
            if let rim = rimColor(d, now: now) {
                nsColor(rim).setStroke()
                path.lineWidth = 1.5
                path.stroke()
            }
            return true
        }
        img.isTemplate = false
        return img
    }

    /// Percent text for single-percent style (goes in the button title).
    static func percentTitle(_ d: DisplaySession) -> NSAttributedString {
        let pct = Int(((d.sess.fill ?? 0) * 100).rounded())
        let color = nsColor(zoneColor(d.sess.fill ?? 0))
        return NSAttributedString(
            string: "\(pct)%",
            attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold),
                .foregroundColor: color,
                .baselineOffset: 0.5,
            ])
    }

    // MARK: multi-session grid

    static func gridImage(_ slots: [DisplaySession?], now: Date) -> NSImage {
        let cell: CGFloat = 6.8, pad: CGFloat = 0.2
        let side = cell * 3 + pad * 2
        let img = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            for (i, d) in slots.prefix(9).enumerated() {
                guard let d else { continue }   // reserved/empty node: a gap
                let col = CGFloat(i % 3), row = CGFloat(i / 3)
                // row 0 on top: flip the row axis (drawing origin is bottom-left)
                let cx = pad + col * cell + cell / 2
                let cy = side - (pad + row * cell + cell / 2)
                drawDot(d, at: NSPoint(x: cx, y: cy), now: now)
            }
            return true
        }
        img.isTemplate = false
        return img
    }

    // MARK: menu row images — "which dot is this session"

    /// The picker's answer to "which dot am I": the 3×3 map with THIS
    /// session's dot lit (identity color, status style) at its real grid
    /// position, every other occupied slot a faint placeholder. A session
    /// not in the grid (hidden, or single mode has no grid) renders as its
    /// lone identity dot instead.
    static func menuDot(_ d: DisplaySession, gridIndex: Int?, occupied: [Int],
                        now: Date) -> NSImage {
        let cell: CGFloat = 6, side: CGFloat = cell * 3
        let img = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            if let idx = gridIndex {
                for j in occupied where j != idx && (0..<9).contains(j) {
                    let c = cellCenter(j, cell: cell, side: side)
                    let ph = NSBezierPath(ovalIn: NSRect(
                        x: c.x - 1.6, y: c.y - 1.6, width: 3.2, height: 3.2))
                    NSColor(white: 0.5, alpha: 0.45).setFill()
                    ph.fill()
                }
                drawDot(d, at: cellCenter(idx, cell: cell, side: side),
                        r: 2.6, now: now)
            } else {
                drawDot(d, at: NSPoint(x: side / 2, y: side / 2), r: 3.4, now: now)
            }
            return true
        }
        img.isTemplate = false
        return img
    }

    /// One status-styled dot as a standalone image — the legend's sample
    /// swatch. Same code path as the grid, so the legend can never lie.
    static func statusDotImage(_ d: DisplaySession, r: CGFloat, canvas: CGFloat,
                               now: Date) -> NSImage {
        let img = NSImage(size: NSSize(width: canvas, height: canvas), flipped: false) { _ in
            drawDot(d, at: NSPoint(x: canvas / 2, y: canvas / 2), r: r, now: now)
            return true
        }
        img.isTemplate = false
        return img
    }

    private static func cellCenter(_ i: Int, cell: CGFloat, side: CGFloat) -> NSPoint {
        let col = CGFloat(i % 3), row = CGFloat(i / 3)
        // row 0 on top (drawing origin is bottom-left)
        return NSPoint(x: col * cell + cell / 2,
                       y: side - (row * cell + cell / 2))
    }

    /// The dot color under the active theme. `identity` = the session's
    /// luminous surface stop (TUI parity); `pastel`/`mono` transform it;
    /// `zone` abandons identity for pressure (fill → green/amber/red).
    /// Whether the surface behind the dots is dark. Updated by the app from
    /// the status item's effectiveAppearance on every redraw; identity mode
    /// uses it to pick the most CONTRASTING spot on each session's palette.
    static var barIsDark = true

    /// Identity, background-aware: on a dark bar ride each palette's
    /// luminous surface (lifted if too deep); on a light bar ride the
    /// saturated mid-palette with a luma cap so nothing washes out. The
    /// hue stays the session's own — only where we sample it moves.
    static func accessibleIdentity(seed: UInt64) -> RGB {
        let pal = genPalette(seed: seed)
        if barIsDark {
            var c = paletteColor(pal, 1.0)
            if lumaT(c) < 0.45 { c = liftRGB(c, 0.30) }
            return c
        }
        var c = paletteColor(pal, 0.55)
        let l = lumaT(c)
        if l > 0.60 { c = scaleRGB(c, 0.60 / max(l, 0.001)) }
        return c
    }

    static func themedColor(_ d: DisplaySession) -> RGB {
        let base = paletteColor(genPalette(seed: sessSeed(d.sess.id)), 1.0)
        switch Settings.choice {
        case .custom(let t):
            return customRampColor(t, rampT(d.sess.id))
        case .builtin(let theme):
            switch theme {
            case .identity:
                return accessibleIdentity(seed: sessSeed(d.sess.id))
            case .pastel: return liftRGB(base, 0.38)
            case .mono: return grayRGB(base)
            case .zone: return zoneColor(d.sess.fill ?? 0)
            case .vaporwave:
                return duoRGB(rampT(d.sess.id), (255, 113, 206), (1, 229, 254))
            case .matrix:
                return duoRGB(rampT(d.sess.id), (40, 140, 60), (170, 255, 180))
            case .ember:
                return duoRGB(rampT(d.sess.id), (215, 60, 30), (255, 215, 95))
            case .candy: return candyRGB(base)
            }
        }
    }

    /// A custom gradient sampled at x ∈ 0...1: piecewise-linear across the
    /// theme's sorted stops (any number of points ≥ 2).
    static func customRampColor(_ t: CustomTheme, _ x: Double) -> RGB {
        let stops = t.stops.sorted { $0.pos < $1.pos }
        guard let first = stops.first, let last = stops.last else {
            return (128, 128, 128)
        }
        let x = min(max(x, 0), 1)
        if x <= first.pos { return first.rgb }
        if x >= last.pos { return last.rgb }
        for i in 0..<(stops.count - 1) {
            let a = stops[i], b = stops[i + 1]
            if x >= a.pos && x <= b.pos {
                let span = max(b.pos - a.pos, 1e-9)
                return duoRGB((x - a.pos) / span, a.rgb, b.rgb)
            }
        }
        return last.rgb
    }

    /// `n` sample colors representing a theme — the menu swatch strip.
    /// Ramp themes sweep their ramp; identity-family themes show striped
    /// session chips (what a fleet actually looks like under them).
    static func themeSwatchColors(_ raw: String, n: Int) -> [RGB] {
        let ids = ["mercury", "venus", "earth", "mars", "jupiter",
                   "saturn", "uranus", "neptune", "pluto"]
        func ramp(_ f: (Double) -> RGB) -> [RGB] {
            (0..<n).map { f(Double($0) / Double(max(1, n - 1))) }
        }
        func chips() -> [RGB] {
            (0..<n).map {
                paletteColor(genPalette(
                    seed: sessSeed(ids[$0 * ids.count / n])), 1.0)
            }
        }
        func accessibleChips() -> [RGB] {
            (0..<n).map {
                accessibleIdentity(seed: sessSeed(ids[$0 * ids.count / n]))
            }
        }
        if raw.hasPrefix("custom:") {
            let name = String(raw.dropFirst("custom:".count))
            if let t = Settings.customThemes.first(where: { $0.name == name }) {
                return ramp { customRampColor(t, $0) }
            }
        }
        switch Theme(rawValue: raw) ?? .identity {
        case .identity: return accessibleChips()
        case .pastel: return chips().map { liftRGB($0, 0.38) }
        case .mono: return chips().map(grayRGB)
        case .zone: return ramp(zoneColor)
        case .vaporwave:
            return ramp { duoRGB($0, (255, 113, 206), (1, 229, 254)) }
        case .matrix:
            return ramp { duoRGB($0, (40, 140, 60), (170, 255, 180)) }
        case .ember:
            return ramp { duoRGB($0, (215, 60, 30), (255, 215, 95)) }
        case .candy: return chips().map(candyRGB)
        }
    }

    /// A session's position on a fun theme's signature ramp. Seed-keyed, not
    /// luma-keyed: gen_palette makes every surface stop similarly bright, so
    /// a luma key collapses all sessions onto one ramp point (observed).
    private static func rampT(_ id: String) -> Double {
        Double(sessSeed(id) % 1009) / 1008.0
    }

    /// Rec. 709 luma as a gray dot color (mono theme).
    private static func grayRGB(_ c: RGB) -> RGB {
        let luma: Double = 0.2126 * Double(c.r) + 0.7152 * Double(c.g)
            + 0.0722 * Double(c.b)
        let l = UInt8(min(255.0, luma))
        return (l, l, l)
    }

    private static func lumaT(_ c: RGB) -> Double {
        let luma: Double = 0.2126 * Double(c.r) + 0.7152 * Double(c.g)
            + 0.0722 * Double(c.b)
        return min(1.0, luma / 255.0)
    }

    /// Duotone: a position t ∈ 0...1 on a low→high signature ramp.
    private static func duoRGB(_ t: Double, _ low: RGB, _ high: RGB) -> RGB {
        func ch(_ a: UInt8, _ b: UInt8) -> UInt8 {
            UInt8(min(255.0, Double(a) + (Double(b) - Double(a)) * t))
        }
        return (ch(low.r, high.r), ch(low.g, high.g), ch(low.b, high.b))
    }

    /// Candy: push saturation hard, then lift — sugar-bright identity.
    private static func candyRGB(_ c: RGB) -> RGB {
        let g: Double = 255.0 * lumaT(c)
        func ch(_ v: UInt8) -> UInt8 {
            let boosted = g + (Double(v) - g) * 1.9
            return UInt8(min(255.0, max(0.0, boosted)))
        }
        return liftRGB((ch(c.r), ch(c.g), ch(c.b)), 0.12)
    }

    private static func drawDot(_ d: DisplaySession, at p: NSPoint,
                                r: CGFloat = 2.9, now: Date) {
        // brightest color the theme allows — status modulates from there
        // (menu bar dots must SHOUT, the bar background eats mid-brightness)
        let identity = themedColor(d)
        let dot = NSBezierPath(
            ovalIn: NSRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2))

        if let ago = d.finishedAgo {
            // finished: 2 Hz white ↔ identity flash
            let on = Int(ago * 4).isMultiple(of: 2)
            nsColor(on ? (250, 250, 250) : identity).setFill()
            dot.fill()
            return
        }
        switch d.sess.status {
        case .busy:
            // pulse LIFTS toward white instead of dipping dark — brightness
            // is the signal and the trough must never sink into the bar
            let t = pulse01(now, phase: phase(d))
            nsColor(liftRGB(identity, 0.45 * t)).setFill()
            dot.fill()
        case .stalled:
            nsColor(scaleRGB(identity, 0.60)).setFill()
            dot.fill()
            let ring = NSBezierPath(
                ovalIn: NSRect(x: p.x - r - 1.1, y: p.y - r - 1.1,
                               width: (r + 1.1) * 2, height: (r + 1.1) * 2))
            nsColor(cAmber).setStroke()
            ring.lineWidth = 1.2
            ring.stroke()
        case .shell:
            nsColor(scaleRGB(identity, 0.85)).setStroke()
            dot.lineWidth = 1.5
            dot.stroke()
        case .idle:
            nsColor(scaleRGB(identity, 0.62)).setFill()
            dot.fill()
        case .dead:
            nsColor(scaleRGB(cRed, 0.75)).setFill()
            dot.fill()
        case .offline, .unknown:
            nsColor(cDim, alpha: 0.7).setFill()
            dot.fill()
        }
    }

    /// Empty-state icon: the grid as faint hollow rings — visible in the
    /// bar (an empty fleet must never render an invisible icon) but quiet.
    /// Shown when the feed is healthy yet no agent sessions exist.
    static func emptyFleetImage() -> NSImage {
        let cell: CGFloat = 6.8, pad: CGFloat = 0.2
        let side = cell * 3 + pad * 2
        let img = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            for i in 0..<9 {
                let col = CGFloat(i % 3), row = CGFloat(i / 3)
                let cx = pad + col * cell + cell / 2
                let cy = side - (pad + row * cell + cell / 2)
                let r: CGFloat = 2.2
                let ring = NSBezierPath(ovalIn: NSRect(
                    x: cx - r, y: cy - r, width: r * 2, height: r * 2))
                nsColor(cDim, alpha: 0.55).setStroke()
                ring.lineWidth = 1.0
                ring.stroke()
            }
            return true
        }
        img.isTemplate = false
        return img
    }

    /// Feed-down placeholder: a dim hollow square (the engine is not talking).
    static func linkDownImage() -> NSImage {
        let s: CGFloat = 14
        let img = NSImage(size: NSSize(width: s, height: s), flipped: false) { _ in
            let p = NSBezierPath(
                roundedRect: NSRect(x: 2, y: 2, width: s - 4, height: s - 4),
                xRadius: 3, yRadius: 3)
            nsColor(cDim).setStroke()
            p.lineWidth = 1.5
            p.stroke()
            let slash = NSBezierPath()
            slash.move(to: NSPoint(x: 3.5, y: 3.5))
            slash.line(to: NSPoint(x: s - 3.5, y: s - 3.5))
            nsColor(cDim).setStroke()
            slash.lineWidth = 1.5
            slash.stroke()
            return true
        }
        img.isTemplate = false
        return img
    }

    // MARK: animation laws

    /// Busy pulse position 0...1 at ~0.8 Hz.
    private static func pulse01(_ now: Date, phase: Double) -> Double {
        let t = now.timeIntervalSinceReferenceDate
        return 0.5 + 0.5 * sin(t * 2 * .pi * 0.8 + phase)
    }

    /// Per-session phase from the identity seed so busy dots breathe
    /// independently, the way distinct sessions are distinct.
    private static func phase(_ d: DisplaySession) -> Double {
        Double(sessSeed(d.sess.id) % 628) / 100.0
    }

    private static func pulseBoost(_ d: DisplaySession, now: Date) -> Double {
        d.sess.status == .busy ? 0.85 + 0.3 * pulse01(now, phase: phase(d)) : 1.0
    }

    private static func rimColor(_ d: DisplaySession, now: Date) -> RGB? {
        if let ago = d.finishedAgo {
            return Int(ago * 4).isMultiple(of: 2) ? (245, 245, 245) : nil
        }
        switch d.sess.status {
        case .stalled: return cAmber
        case .dead: return cRed
        default: return nil
        }
    }

    // MARK: color plumbing

    private static func scaleRGB(_ c: RGB, _ k: Double) -> RGB {
        func s(_ v: UInt8) -> UInt8 { UInt8(min(255.0, Double(v) * k)) }
        return (s(c.r), s(c.g), s(c.b))
    }

    /// The tank's 4 gradient stops under the active theme. In identity
    /// theme, a running amtr TUI attached to this session wins: its exported
    /// tank palette (SPEC e) replaces the generated one, so terminal and
    /// menu bar show the same tank. Other themes are a deliberate look and
    /// are never overridden.
    /// Background-adaptive gradient: hue kept, luma bounded so the tank
    /// reads on the CURRENT bar — dark: surface stop floored; light: every
    /// stop capped (the same law as the dots, applied per stop).
    static func adaptStops(_ stops: [RGB]) -> [RGB] {
        if barIsDark {
            var out = stops
            if let last = out.last, lumaT(last) < 0.45 {
                out[out.count - 1] = liftRGB(last, 0.30)
            }
            return out
        }
        return stops.map { c in
            let l = lumaT(c)
            return l > 0.62 ? scaleRGB(c, 0.62 / max(l, 0.001)) : c
        }
    }

    private static func themedStops(_ d: DisplaySession) -> [RGB] {
        if Settings.choice == .builtin(.identity),
           let amtr = AmtrPalette.stops(for: d.sess.id) {
            // amtr's hues, this bar's luma bounds
            return adaptStops(amtr)
        }
        let pal = genPalette(seed: sessSeed(d.sess.id))
        switch Settings.choice {
        case .custom:
            // drain law on the session's ramp color
            let c = themedColor(d)
            return [scaleRGB(c, 0.35), scaleRGB(c, 0.55),
                    scaleRGB(c, 0.80), c]
        case .builtin(let theme):
            switch theme {
            case .identity: return adaptStops(pal)
            case .pastel: return pal.map { liftRGB($0, 0.38) }
            case .mono:
                return pal.map(grayRGB)
            case .zone:
                // drain law kept: deep dark → zone color at the surface
                let z = zoneColor(d.sess.fill ?? 0)
                return [scaleRGB(z, 0.35), scaleRGB(z, 0.55),
                        scaleRGB(z, 0.80), z]
            case .vaporwave, .matrix, .ember:
                let c = themedColor(d)
                return [scaleRGB(c, 0.35), scaleRGB(c, 0.55),
                        scaleRGB(c, 0.80), c]
            case .candy:
                return pal.map(candyRGB)
            }
        }
    }

    /// Blend toward white by k ∈ 0...1 (the pulse brightens ABOVE identity).
    private static func liftRGB(_ c: RGB, _ k: Double) -> RGB {
        func l(_ v: UInt8) -> UInt8 {
            UInt8(min(255.0, Double(v) + (250.0 - Double(v)) * k))
        }
        return (l(c.r), l(c.g), l(c.b))
    }

    private static func nsColor(_ c: RGB, alpha: CGFloat = 1.0) -> NSColor {
        NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                blue: CGFloat(c.b) / 255, alpha: alpha)
    }
}
