// The menu's session-assignment section: ONE custom menu-item view holding
// every open session as a row and, below them, the 9 grid nodes. Drag a
// session row onto a node to pin it there; click a row to show/hide it;
// click an occupied node to clear it. All tracking is internal mouse
// handling (NSDraggingSession would dismiss the tracking menu), so the menu
// stays open while you arrange the grid.

import AppKit

final class AssignSectionView: NSView {
    // inputs, set before the menu opens
    private var rows: [DisplaySession] = []          // all live sessions
    private var slotOf: [String: Int] = [:]          // session id -> node
    private var slots: [DisplaySession?] = []        // current 9-node layout
    private var reserved: Set<Int> = []              // assigned node indices
    /// repaint the status item after any change
    var onChange: (() -> Void)?

    // geometry — the nodes are a 3×3 grid in EXACTLY the menu bar icon's
    // layout (node 1 = top-left … node 9 = bottom-right), so dropping a
    // session somewhere means putting it THERE in the bar
    private let rowH: CGFloat = 22
    private let nodeD: CGFloat = 34
    private let nodeGap: CGFloat = 7
    private let leftPad: CGFloat = 22
    private let hintH: CGFloat = 4
    private var nodesH: CGFloat { nodeD * 3 + nodeGap * 2 + 12 }

    // interaction state
    private var hoverRow: Int?
    private var hoverNode: Int?
    private var pressRow: Int?
    private var dragging = false
    private var dragPoint = NSPoint.zero
    private var tracking: NSTrackingArea?

    init(width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 10))
    }

    required init?(coder: NSCoder) { nil }

    private var dupNames: Set<String> = []

    private var cfg: [String?] = []

    func reload(store: FleetStore) {
        rows = store.sessions.filter { $0.status.isLiveish }
            .map { DisplaySession(sess: $0, finishedAgo: nil) }
        var counts: [String: Int] = [:]
        for d in rows { counts[d.sess.name, default: 0] += 1 }
        dupNames = Set(counts.filter { $0.value > 1 }.map(\.key))
        cfg = Settings.slots
        slots = store.slotted(hidden: Settings.hidden, slots: cfg)
        slotOf = [:]
        for (i, d) in slots.enumerated() {
            if let d { slotOf[d.sess.id] = i }
        }
        reserved = Set((0..<9).filter { $0 < cfg.count && cfg[$0] != nil })
        setFrameSize(NSSize(
            width: frame.width,
            height: CGFloat(rows.count) * rowH + hintH + nodesH))
        needsDisplay = true
    }

    /// True when the node holds a PINNED session (a real id, not the
    /// explicit-empty sentinel).
    private func isPinned(_ i: Int) -> Bool {
        i < cfg.count && cfg[i] != nil && cfg[i] != Settings.emptySlot
    }

    // MARK: geometry helpers — nodes on TOP, sessions below

    private var rowsTop: CGFloat { bounds.height - nodesH - hintH }

    private func rowRect(_ i: Int) -> NSRect {
        NSRect(x: 0, y: rowsTop - CGFloat(i + 1) * rowH,
               width: bounds.width, height: rowH)
    }

    private func nodeRect(_ i: Int) -> NSRect {
        let col = CGFloat(i % 3), row = CGFloat(i / 3)
        let gridW = nodeD * 3 + nodeGap * 2
        let x0 = (bounds.width - gridW) / 2
        // row 0 on TOP, matching the icon (view origin is bottom-left)
        let yTop = bounds.height - 8
        return NSRect(x: x0 + col * (nodeD + nodeGap),
                      y: yTop - (row + 1) * nodeD - row * nodeGap,
                      width: nodeD, height: nodeD)
    }

    private func rowAt(_ p: NSPoint) -> Int? {
        guard p.y < rowsTop else { return nil }
        let i = Int((rowsTop - p.y) / rowH)
        return (0..<rows.count).contains(i) ? i : nil
    }

    private func nodeAt(_ p: NSPoint) -> Int? {
        (0..<9).first { nodeRect($0).insetBy(dx: -4, dy: -4).contains(p) }
    }

    // MARK: drawing

    override func draw(_ dirtyRect: NSRect) {
        // this view lives on the MENU's background, not the bar's
        IconRenderer.barIsDark = effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let now = Date()
        let occupied = slots.enumerated().compactMap { $1 == nil ? nil : $0 }

        // session rows (below the nodes). No checkmark column: the glyph
        // already says everything — mini-map = in the bar, lone dot = not —
        // and a HIDDEN session renders dimmed instead.
        for (i, d) in rows.enumerated() {
            let r = rowRect(i)
            if hoverRow == i && !dragging {
                NSColor.selectedContentBackgroundColor
                    .withAlphaComponent(0.25).setFill()
                NSBezierPath(roundedRect: r.insetBy(dx: 6, dy: 1),
                             xRadius: 5, yRadius: 5).fill()
            }
            let hidden = Settings.hidden.contains(d.sess.id)
            let alpha: CGFloat = hidden ? 0.35 : 1.0
            let map = IconRenderer.menuDot(
                d, gridIndex: slotOf[d.sess.id], occupied: occupied, now: now)
            map.draw(in: NSRect(x: 12, y: r.minY + 2, width: 18, height: 18),
                     from: .zero, operation: .sourceOver, fraction: alpha)

            // invisible table: name | project | % | badge, fixed columns,
            // tail-truncated so no field can shove its neighbors
            let trunc = NSMutableParagraphStyle()
            trunc.lineBreakMode = .byTruncatingTail
            let nameColor = hidden ? NSColor.tertiaryLabelColor
                                   : NSColor.labelColor
            let tag = dupNames.contains(d.sess.name)
                ? " [\(d.sess.id.prefix(4))]" : ""
            ("\(d.sess.name)\(tag)" as NSString).draw(
                in: NSRect(x: 38, y: r.minY + 3, width: 150, height: 16),
                withAttributes: [
                    .font: NSFont.menuFont(ofSize: 13),
                    .foregroundColor: nameColor,
                    .paragraphStyle: trunc])
            let proj = d.sess.provider == "claude"
                ? d.sess.projectTail
                : "\(d.sess.projectTail) · \(d.sess.provider)"
            (proj as NSString).draw(
                in: NSRect(x: 196, y: r.minY + 3, width: 104, height: 16),
                withAttributes: [
                    .font: NSFont.menuFont(ofSize: 13),
                    .foregroundColor: hidden
                        ? NSColor.tertiaryLabelColor
                        : NSColor.secondaryLabelColor,
                    .paragraphStyle: trunc])
            if let f = d.sess.fill {
                let pct = "\(Int((f * 100).rounded()))%" as NSString
                let attrs: [NSAttributedString.Key: Any] = [
                    .font: NSFont.monospacedDigitSystemFont(
                        ofSize: 12, weight: .regular),
                    .foregroundColor: hidden
                        ? NSColor.tertiaryLabelColor
                        : NSColor.secondaryLabelColor]
                let sz = pct.size(withAttributes: attrs)
                pct.draw(at: NSPoint(x: 340 - sz.width, y: r.minY + 4),
                         withAttributes: attrs)
            }
            // badge column: pin location, or the hidden marker
            if let n = slotOf[d.sess.id], reserved.contains(n) {
                ("⌖\(n + 1)" as NSString).draw(
                    at: NSPoint(x: 348, y: r.minY + 3),
                    withAttributes: [
                        .font: NSFont.systemFont(ofSize: 11, weight: .semibold),
                        .foregroundColor: NSColor.controlAccentColor])
            } else if hidden {
                ("off" as NSString).draw(
                    at: NSPoint(x: 348, y: r.minY + 4),
                    withAttributes: [
                        .font: NSFont.systemFont(ofSize: 10, weight: .medium),
                        .foregroundColor: NSColor.tertiaryLabelColor])
            }
        }


        // the 9 nodes. Ring language must mirror REALITY in the bar:
        // every occupied node reads identically (they are all shown);
        // pinned-vs-auto is a ⌖ badge, not a heavier ring; a reserved
        // node holding no session is a dashed ring (a kept-open seat).
        for i in 0..<9 {
            let r = nodeRect(i)
            let occupiedHere = (slots[safe: i] ?? nil) != nil
            let ring = NSBezierPath(ovalIn: r.insetBy(dx: 1.5, dy: 1.5))
            let active = dragging && hoverNode == i
            if reserved.contains(i) && !occupiedHere {
                ring.setLineDash([3, 2], count: 2, phase: 0)
            }
            (active ? NSColor.controlAccentColor
                    : occupiedHere
                        ? NSColor.labelColor.withAlphaComponent(0.55)
                        : NSColor.separatorColor).setStroke()
            ring.lineWidth = active ? 2.5 : occupiedHere ? 1.4 : 1.1
            ring.stroke()
            if let d = slots[safe: i] ?? nil {
                IconRenderer.statusDotImage(d, r: 7, canvas: 20, now: now)
                    .draw(in: NSRect(x: r.midX - 10, y: r.midY - 10,
                                     width: 20, height: 20))
            } else {
                ("\(i + 1)" as NSString).draw(
                    at: NSPoint(x: r.midX - 3, y: r.midY - 7),
                    withAttributes: [
                        .font: NSFont.systemFont(ofSize: 10, weight: .medium),
                        .foregroundColor: NSColor.tertiaryLabelColor])
            }
            if isPinned(i) {
                // pinned marker: corner badge, same glyph as the row tag
                ("⌖" as NSString).draw(
                    at: NSPoint(x: r.maxX - 8, y: r.minY - 2),
                    withAttributes: [
                        .font: NSFont.systemFont(ofSize: 9, weight: .semibold),
                        .foregroundColor: NSColor.controlAccentColor])
            }
        }

        // drag ghost
        if dragging, let i = pressRow, i < rows.count {
            let d = rows[i]
            IconRenderer.statusDotImage(d, r: 8, canvas: 22, now: now)
                .draw(in: NSRect(x: dragPoint.x - 11, y: dragPoint.y - 11,
                                 width: 22, height: 22))
        }
    }

    // MARK: mouse

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(
            rect: bounds,
            options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways],
            owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseMoved(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        let r = rowAt(p)
        if r != hoverRow {
            hoverRow = r
            needsDisplay = true
        }
    }

    override func mouseExited(with event: NSEvent) {
        hoverRow = nil
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        pressRow = rowAt(p)
        dragging = false
        dragPoint = p
        if pressRow == nil, let n = nodeAt(p) {
            // The node click cycle, and every step is VISIBLE (clearing a
            // pin straight to auto-fill refills instantly with a full
            // fleet — it looked like the click did nothing):
            //   occupied (pinned or auto) → EMPTY (a real gap in the bar)
            //   empty (dashed)            → auto-fill again
            //   free numbered             → no-op
            var s = Settings.slots
            let occupiedHere = (slots[safe: n] ?? nil) != nil
            if s[n] == Settings.emptySlot {
                s[n] = nil
            } else if s[n] != nil || occupiedHere {
                s[n] = Settings.emptySlot
            } else {
                return
            }
            Settings.slots = s
            refreshAfterChange()
        }
    }

    override func mouseDragged(with event: NSEvent) {
        guard pressRow != nil else { return }
        let p = convert(event.locationInWindow, from: nil)
        if !dragging && hypot(p.x - dragPoint.x, p.y - dragPoint.y) > 4 {
            dragging = true
        }
        dragPoint = p
        hoverNode = nodeAt(p)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        defer {
            dragging = false
            pressRow = nil
            hoverNode = nil
            needsDisplay = true
        }
        let p = convert(event.locationInWindow, from: nil)
        guard let i = pressRow, i < rows.count else { return }
        let id = rows[i].sess.id
        if dragging {
            guard let n = nodeAt(p) else { return }
            var s = Settings.slots
            for j in 0..<s.count where s[j] == id { s[j] = nil }
            s[n] = id
            Settings.slots = s
            refreshAfterChange()
        } else if rowAt(p) == i {
            // plain click: show/hide (grid) — the old menu-row behavior
            var h = Settings.hidden
            if h.contains(id) { h.remove(id) } else { h.insert(id) }
            Settings.hidden = h
            refreshAfterChange()
        }
    }

    private func refreshAfterChange() {
        if let store = storeRef { reload(store: store) }
        onChange?()
    }

    /// weak-ish backref so internal changes can re-derive the layout
    weak var storeOwner: AnyObject?
    private var storeRef: FleetStore? { storeOwner as? FleetStore }
}

extension Array {
    subscript(safe i: Int) -> Element? {
        indices.contains(i) ? self[i] : nil
    }
}
