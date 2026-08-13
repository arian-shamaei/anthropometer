// Options ▸ Help — an animated, nearly wordless tour. Five looping scenes,
// each a focused vignette rendered by the SAME drawing primitives the real
// UI uses (a live animation cannot drift from the product the way a
// recorded gif would):
//   1 nodes ⇄ bar     the 3×3 nodes and the bar icon light up in step
//   2 drag = pin ⌖    a session dot flies onto a node and pins
//   3 click = gap     clicking an occupied node leaves a real gap
//   4 click = refill  clicking the dashed node lets auto-fill return
//   5 dot language    busy / done / stalled / shell / idle, animating

import AppKit

final class HelpView: NSView {
    private var timer: Timer?
    private var sceneIx = 0
    private var sceneStart = Date()

    // scene lengths (seconds); each scene loops its own animation and
    // auto-advances, arrows jump directly. Scene 0 is the WELCOME page —
    // shown first on every open, and the app opens this window on its
    // very first launch.
    private let lens: [Double] = [4.5, 5.0, 4.5, 4.0, 3.5, 5.0]

    func start() {
        sceneIx = 0
        sceneStart = Date()
        stop()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0,
                                     repeats: true) { [weak self] _ in
            self?.needsDisplay = true
        }
        RunLoop.current.add(timer!, forMode: .common)
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// back/forward arrows
    func step(_ delta: Int) {
        sceneIx = (sceneIx + delta + lens.count) % lens.count
        sceneStart = Date()
        needsDisplay = true
    }

    // MARK: plumbing

    private func mk(_ id: String, _ status: String) -> DisplaySession {
        DisplaySession(
            sess: FleetSession(json: ["id": id, "status": status,
                                      "live": true, "project": "/demo"])!,
            finishedAgo: nil)
    }

    private func ease(_ x: Double) -> Double {
        let c = min(max(x, 0), 1)
        return c * c * (3 - 2 * c)
    }

    private func color(_ id: String) -> NSColor {
        let c = paletteColor(genPalette(seed: sessSeed(id)), 1.0)
        return NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                       blue: CGFloat(c.b) / 255, alpha: 1)
    }

    private let demoIds = ["mercury", "venus", "earth", "mars", "jupiter",
                           "saturn", "uranus", "neptune", "pluto"]

    // MARK: scene layout helpers

    /// A 3×3 node grid centered at `c` with node diameter `d`.
    private func nodeCenter(_ i: Int, c: NSPoint, d: CGFloat,
                            gap: CGFloat) -> NSPoint {
        let col = CGFloat(i % 3) - 1, row = CGFloat(i / 3) - 1
        return NSPoint(x: c.x + col * (d + gap),
                       y: c.y - row * (d + gap))
    }

    private func drawNode(_ i: Int, at p: NSPoint, d: CGFloat,
                          dot: DisplaySession?, dashed: Bool = false,
                          highlight: Bool = false, pin: Bool = false,
                          now: Date) {
        let r = NSRect(x: p.x - d / 2, y: p.y - d / 2, width: d, height: d)
        let ring = NSBezierPath(ovalIn: r)
        if dashed { ring.setLineDash([4, 3], count: 2, phase: 0) }
        (highlight ? NSColor.controlAccentColor
                   : dot != nil ? NSColor.labelColor.withAlphaComponent(0.55)
                                : NSColor.separatorColor).setStroke()
        ring.lineWidth = highlight ? 3 : 1.6
        ring.stroke()
        if let dot {
            IconRenderer.statusDotImage(dot, r: d * 0.26, canvas: d * 0.7,
                                        now: now)
                .draw(in: NSRect(x: p.x - d * 0.35, y: p.y - d * 0.35,
                                 width: d * 0.7, height: d * 0.7))
        }
        if pin {
            ("⌖" as NSString).draw(
                at: NSPoint(x: r.maxX - 10, y: r.minY - 3),
                withAttributes: [
                    .font: NSFont.systemFont(ofSize: 12, weight: .semibold),
                    .foregroundColor: NSColor.controlAccentColor])
        }
    }

    /// The menu bar icon in miniature (magnified): 3×3 dots on a bar chip.
    private func drawBarIcon(center: NSPoint, skip: Set<Int> = [],
                             highlight: Int? = nil, now: Date) {
        let chip = NSRect(x: center.x - 44, y: center.y - 26,
                          width: 88, height: 52)
        let bg = NSBezierPath(roundedRect: chip, xRadius: 10, yRadius: 10)
        NSColor.labelColor.withAlphaComponent(0.08).setFill()
        bg.fill()
        for i in 0..<9 where !skip.contains(i) {
            let col = CGFloat(i % 3) - 1, row = CGFloat(i / 3) - 1
            let p = NSPoint(x: center.x + col * 14, y: center.y - row * 14)
            let d = mk(demoIds[i], "idle")
            IconRenderer.statusDotImage(d, r: 4.4, canvas: 12, now: now)
                .draw(in: NSRect(x: p.x - 6, y: p.y - 6, width: 12, height: 12))
            if highlight == i {
                let hl = NSBezierPath(ovalIn: NSRect(
                    x: p.x - 8, y: p.y - 8, width: 16, height: 16))
                NSColor.controlAccentColor.setStroke()
                hl.lineWidth = 2
                hl.stroke()
            }
        }
    }

    private func caption(_ s: String) {
        (s as NSString).draw(
            at: NSPoint(x: 24, y: 16),
            withAttributes: [
                .font: NSFont.systemFont(ofSize: 16, weight: .semibold),
                .foregroundColor: NSColor.labelColor])
    }

    private func pointer(at p: NSPoint, pressed: Bool) {
        let arrow = NSBezierPath()
        arrow.move(to: p)
        arrow.line(to: NSPoint(x: p.x + 11, y: p.y - 4))
        arrow.line(to: NSPoint(x: p.x + 5, y: p.y - 5))
        arrow.line(to: NSPoint(x: p.x + 7, y: p.y - 12))
        arrow.line(to: NSPoint(x: p.x + 4, y: p.y - 13))
        arrow.line(to: NSPoint(x: p.x + 2, y: p.y - 6))
        arrow.close()
        NSColor.labelColor.setFill()
        arrow.fill()
        if pressed {
            let ripple = NSBezierPath(ovalIn: NSRect(
                x: p.x - 10, y: p.y - 10, width: 20, height: 20))
            NSColor.controlAccentColor.withAlphaComponent(0.6).setStroke()
            ripple.lineWidth = 2
            ripple.stroke()
        }
    }

    // MARK: scenes

    override func draw(_ dirtyRect: NSRect) {
        // the tutorial draws on a window background, not the bar
        IconRenderer.barIsDark = effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        NSColor.windowBackgroundColor.setFill()
        bounds.fill()
        let now = Date()
        var t = now.timeIntervalSince(sceneStart)
        if t >= lens[sceneIx] {
            sceneIx = (sceneIx + 1) % lens.count
            sceneStart = now
            t = 0
        }
        let scene = sceneIx
        let gridC = NSPoint(x: bounds.width * 0.32, y: bounds.height * 0.55)
        let barC = NSPoint(x: bounds.width * 0.78, y: bounds.height * 0.55)
        let d: CGFloat = 40, gap: CGFloat = 9

        switch scene {
        case 0:
            // WELCOME: the nine dots fly into formation, staggered
            let center = NSPoint(x: bounds.width / 2,
                                 y: bounds.height * 0.58)
            for i in 0..<9 {
                let target = nodeCenter(i, c: center, d: 34, gap: 8)
                let angle = Double(i) * 0.7 + 0.4
                let from = NSPoint(
                    x: center.x + CGFloat(cos(angle)) * bounds.width * 0.7,
                    y: center.y + CGFloat(sin(angle)) * bounds.height * 0.7)
                let k = ease((t - 0.12 * Double(i)) / 1.1)
                let p = NSPoint(x: from.x + (target.x - from.x) * CGFloat(k),
                                y: from.y + (target.y - from.y) * CGFloat(k))
                IconRenderer.statusDotImage(
                    mk(demoIds[i], k >= 1 ? "busy" : "idle"),
                    r: 9, canvas: 24, now: now)
                    .draw(in: NSRect(x: p.x - 12, y: p.y - 12,
                                     width: 24, height: 24))
            }
            ("every agent session on your Mac, at a glance"
                as NSString).draw(
                at: NSPoint(x: 24, y: 40),
                withAttributes: [
                    .font: NSFont.systemFont(ofSize: 12),
                    .foregroundColor: NSColor.secondaryLabelColor])
            caption("welcome to amtrino")
        case 1:
            // nodes ⇄ bar: light matching positions in step
            let hi = Int(t / 0.55) % 9
            for i in 0..<9 {
                drawNode(i, at: nodeCenter(i, c: gridC, d: d, gap: gap), d: d,
                         dot: mk(demoIds[i], "idle"), highlight: i == hi,
                         now: now)
            }
            drawBarIcon(center: barC, highlight: hi, now: now)
            caption("nodes represent sessions")
        case 2:
            // drag = pin: a dot flies from below onto node 2
            for i in 0..<9 where i != 1 {
                drawNode(i, at: nodeCenter(i, c: gridC, d: d, gap: gap), d: d,
                         dot: mk(demoIds[i], "idle"), now: now)
            }
            let target = nodeCenter(1, c: gridC, d: d, gap: gap)
            let from = NSPoint(x: bounds.width * 0.72, y: bounds.height * 0.16)
            let k = ease((t - 0.8) / 1.8)
            let arrived = k >= 1
            drawNode(1, at: target, d: d,
                     dot: arrived ? mk("venus", "idle") : nil,
                     highlight: !arrived && k > 0.6, pin: arrived, now: now)
            if !arrived {
                let p = NSPoint(x: from.x + (target.x - from.x) * k,
                                y: from.y + (target.y - from.y) * k)
                IconRenderer.statusDotImage(mk("venus", "idle"), r: 9,
                                            canvas: 24, now: now)
                    .draw(in: NSRect(x: p.x - 12, y: p.y - 12,
                                     width: 24, height: 24))
                pointer(at: NSPoint(x: p.x + 6, y: p.y - 4), pressed: true)
            }
            caption("drag session to pin")
        case 3:
            // click = gap: node 4 empties, the bar shows the gap
            let clickT = 1.2
            let clicked = t > clickT
            for i in 0..<9 {
                let isTarget = i == 4
                drawNode(i, at: nodeCenter(i, c: gridC, d: d, gap: gap), d: d,
                         dot: isTarget && clicked ? nil : mk(demoIds[i], "idle"),
                         dashed: isTarget && clicked, now: now)
            }
            drawBarIcon(center: barC, skip: clicked ? [4] : [], now: now)
            let np = nodeCenter(4, c: gridC, d: d, gap: gap)
            pointer(at: NSPoint(x: np.x + 6, y: np.y - 4),
                    pressed: abs(t - clickT) < 0.35)
            caption("click node to remove session pin")
        case 4:
            // click again = refill
            let clickT = 1.0
            let refilled = t > clickT
            for i in 0..<9 {
                let isTarget = i == 4
                let fadeIn = ease((t - clickT) / 0.8)
                if isTarget {
                    drawNode(i, at: nodeCenter(i, c: gridC, d: d, gap: gap),
                             d: d, dot: refilled ? mk(demoIds[i], "idle") : nil,
                             dashed: !refilled, now: now)
                    if refilled && fadeIn < 1 {
                        // brief accent flash as the seat refills
                        let p = nodeCenter(i, c: gridC, d: d, gap: gap)
                        let hl = NSBezierPath(ovalIn: NSRect(
                            x: p.x - d / 2, y: p.y - d / 2, width: d, height: d))
                        NSColor.controlAccentColor
                            .withAlphaComponent(1 - fadeIn).setStroke()
                        hl.lineWidth = 2.5
                        hl.stroke()
                    }
                } else {
                    drawNode(i, at: nodeCenter(i, c: gridC, d: d, gap: gap),
                             d: d, dot: mk(demoIds[i], "idle"), now: now)
                }
            }
            drawBarIcon(center: barC, skip: refilled ? [] : [4], now: now)
            let np = nodeCenter(4, c: gridC, d: d, gap: gap)
            pointer(at: NSPoint(x: np.x + 6, y: np.y - 4),
                    pressed: abs(t - clickT) < 0.35)
            caption("click again to auto-fill")
        default:
            // the dot language, animating for real
            let styles: [(DisplaySession, String)] = [
                (mk("earth", "busy"), "busy"),
                (DisplaySession(
                    sess: mk("venus", "idle").sess,
                    finishedAgo: t.truncatingRemainder(
                        dividingBy: FleetStore.flashSecs)), "done"),
                (mk("mars", "stalled"), "stalled"),
                (mk("saturn", "shell"), "shell"),
                (mk("pluto", "idle"), "idle"),
            ]
            let step = bounds.width / CGFloat(styles.count + 1)
            for (i, (dd, name)) in styles.enumerated() {
                let x = step * CGFloat(i + 1)
                let y = bounds.height * 0.58
                IconRenderer.statusDotImage(dd, r: 11, canvas: 34, now: now)
                    .draw(in: NSRect(x: x - 17, y: y - 17,
                                     width: 34, height: 34))
                let attrs: [NSAttributedString.Key: Any] = [
                    .font: NSFont.systemFont(ofSize: 12),
                    .foregroundColor: NSColor.secondaryLabelColor]
                let sz = (name as NSString).size(withAttributes: attrs)
                (name as NSString).draw(
                    at: NSPoint(x: x - sz.width / 2, y: y - 44),
                    withAttributes: attrs)
            }
            caption("session key")
        }

        // progress dots — right edge, clear of the caption
        for i in 0..<lens.count {
            let p = NSPoint(
                x: bounds.width - 24 - CGFloat(lens.count - 1 - i) * 14,
                y: 22)
            let dot = NSBezierPath(ovalIn: NSRect(
                x: p.x - 3, y: p.y - 3, width: 6, height: 6))
            (i == scene ? NSColor.labelColor
                        : NSColor.quaternaryLabelColor).setFill()
            dot.fill()
        }
    }
}

final class HelpPanel: NSObject, NSWindowDelegate {
    static let shared = HelpPanel()
    private var window: NSWindow?
    private var view: HelpView?

    func show() {
        if window == nil {
            let w = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 460, height: 336),
                styleMask: [.titled, .closable], backing: .buffered,
                defer: false)
            w.title = "amtrino help"
            w.isReleasedWhenClosed = false
            w.center()
            w.delegate = self
            let root = NSView(frame: w.contentLayoutRect)
            let v = HelpView(frame: NSRect(
                x: 0, y: 40, width: root.bounds.width,
                height: root.bounds.height - 40))
            root.addSubview(v)
            func button(_ title: String, _ x: CGFloat, _ width: CGFloat,
                        _ sel: Selector) {
                let b = NSButton(title: title, target: self, action: sel)
                b.frame = NSRect(x: x, y: 6, width: width, height: 28)
                b.bezelStyle = .rounded
                root.addSubview(b)
            }
            button("◀", 16, 44, #selector(back))
            button("▶", 64, 44, #selector(forward))
            button("Done", root.bounds.width - 96, 80, #selector(done))
            w.contentView = root
            window = w
            view = v
        }
        // open on the screen the user is actually on (the mouse's screen) —
        // center() targets the "main" screen, which on a multi-display setup
        // is often the one they are NOT looking at — and float above other
        // windows so it cannot open buried.
        if let w = window {
            let mouse = NSEvent.mouseLocation
            let screen = NSScreen.screens.first {
                NSMouseInRect(mouse, $0.frame, false)
            } ?? NSScreen.main
            if let f = screen?.visibleFrame {
                w.setFrameOrigin(NSPoint(
                    x: f.midX - w.frame.width / 2,
                    y: f.midY - w.frame.height / 2))
            }
            w.level = .floating
        }
        view?.start()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    @objc private func back() { view?.step(-1) }
    @objc private func forward() { view?.step(1) }
    @objc private func done() { window?.close() }

    func windowWillClose(_ notification: Notification) {
        view?.stop()
    }
}
