// The legend row at the bottom of the menu: LIVE samples of every dot style,
// drawn by the same IconRenderer code the grid uses — the busy sample
// actually breathes, the finished sample actually flashes, so the legend
// teaches by showing, not by describing. Animates only while the menu is
// open (menuWillOpen/menuDidClose drive start/stop).

import AppKit

final class LegendView: NSView {
    private var timer: Timer?

    /// (sample session, whether finishedAgo animates, label)
    private struct Row {
        let sess: FleetSession
        let finished: Bool
        let label: String
    }

    private static func mk(_ id: String, _ status: String,
                           fill: Int? = nil) -> FleetSession {
        var json: [String: Any] = ["id": id, "status": status, "live": true,
                                   "project": "/legend"]
        if let fill {
            json["resident"] = fill
            json["budget"] = 100
        }
        return FleetSession(json: json)!
    }

    // one shared identity for the status rows: color stays constant so the
    // eye learns "style = status"; the identity line below teaches
    // "color = session"
    private let rows: [Row] = [
        Row(sess: mk("legend", "busy"), finished: false,
            label: "breathing — responding"),
        Row(sess: mk("legend", "idle"), finished: true,
            label: "white flash — response finished"),
        Row(sess: mk("legend", "stalled"), finished: false,
            label: "amber ring — stalled (quiet 2 min)"),
        Row(sess: mk("legend", "shell"), finished: false,
            label: "hollow — sitting in a shell"),
        Row(sess: mk("legend", "idle"), finished: false,
            label: "dim — waiting for a prompt"),
    ]

    private let rowH: CGFloat = 19
    private let headH: CGFloat = 18
    private let identityH: CGFloat = 21

    init(width: CGFloat) {
        super.init(frame: NSRect(
            x: 0, y: 0, width: width,
            height: headH + rowH * CGFloat(rows.count) + identityH))
    }

    required init?(coder: NSCoder) { nil }

    func startAnimating() {
        stopAnimating()
        timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            self?.needsDisplay = true
        }
        // menus track in the event-tracking run loop mode
        RunLoop.current.add(timer!, forMode: .eventTracking)
        RunLoop.current.add(timer!, forMode: .common)
    }

    func stopAnimating() {
        timer?.invalidate()
        timer = nil
    }

    override func draw(_ dirtyRect: NSRect) {
        // legend samples sit on the menu's background
        IconRenderer.barIsDark = effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let now = Date()
        let labelFont = NSFont.menuFont(ofSize: 12)
        let headFont = NSFont.systemFont(ofSize: 11, weight: .medium)
        let x0: CGFloat = 22  // aligns with menu item text column
        var y = bounds.height - headH

        ("legend" as NSString).draw(
            at: NSPoint(x: x0, y: y + 2),
            withAttributes: [.font: headFont,
                             .foregroundColor: NSColor.tertiaryLabelColor])

        for r in rows {
            y -= rowH
            let ago = r.finished
                ? now.timeIntervalSinceReferenceDate
                    .truncatingRemainder(dividingBy: FleetStore.flashSecs)
                : nil
            let d = DisplaySession(sess: r.sess, finishedAgo: ago)
            if r.finished {
                // the white phase of the flash is invisible on a light menu —
                // a hairline ring keeps the sample legible in both phases
                let ring = NSBezierPath(ovalIn: NSRect(
                    x: x0 + 2.5, y: y + (rowH - 14) / 2 + 2.5,
                    width: 9, height: 9))
                NSColor.tertiaryLabelColor.setStroke()
                ring.lineWidth = 0.8
                ring.stroke()
            }
            let img = IconRenderer.statusDotImage(d, r: 4.0, canvas: 14, now: now)
            img.draw(in: NSRect(x: x0, y: y + (rowH - 14) / 2, width: 14, height: 14))
            (r.label as NSString).draw(
                at: NSPoint(x: x0 + 20, y: y + 2),
                withAttributes: [.font: labelFont,
                                 .foregroundColor: NSColor.secondaryLabelColor])
        }

        // color line: what color MEANS under the active theme — three
        // different sessions (identity themes) or three pressure levels
        // (zone theme). busy style = full brightness; this line is about COLOR.
        y -= identityH
        var x = x0
        let zone = Settings.choice == .builtin(.zone)
        let samples: [FleetSession] = zone
            ? [LegendView.mk("legend-a", "busy", fill: 30),
               LegendView.mk("legend-b", "busy", fill: 70),
               LegendView.mk("legend-c", "busy", fill: 90)]
            : [LegendView.mk("legend-a", "busy"),
               LegendView.mk("legend-b", "busy"),
               LegendView.mk("legend-c", "busy")]
        for s in samples {
            let img = IconRenderer.statusDotImage(
                DisplaySession(sess: s, finishedAgo: nil),
                r: 3.4, canvas: 12, now: now)
            img.draw(in: NSRect(x: x, y: y + (identityH - 12) / 2, width: 12, height: 12))
            x += 13
        }
        let colorLine = zone
            ? "color = context pressure · % = context used"
            : "color = session identity · % = context used"
        (colorLine as NSString).draw(
            at: NSPoint(x: x + 7, y: y + 3),
            withAttributes: [.font: labelFont,
                             .foregroundColor: NSColor.secondaryLabelColor])
    }
}
