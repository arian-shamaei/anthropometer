// The theme editor window. A custom theme IS a gradient, so the editor is a
// gradient editor: the strip with draggable color points on it. Click a
// point to select it (the color well edits it), drag to move it,
// double-click the strip to add a point, "−" removes the selected one
// (minimum 2). Nine session dots under the strip preview what a fleet
// looks like. (Grid-slot assignment lives in the menu, not here.)

import AppKit

final class GradientEditorView: NSView {
    var stops: [RampStop] = [RampStop(pos: 0, color: [30, 30, 60]),
                             RampStop(pos: 1, color: [240, 200, 120])] {
        didSet { needsDisplay = true }
    }
    private(set) var selected = 0
    /// stops or positions changed
    var onChange: (() -> Void)?
    /// the selected point changed (host syncs its color well)
    var onSelect: (() -> Void)?

    private var dragging = false

    private var stripRect: NSRect {
        NSRect(x: 8, y: bounds.height - 30, width: bounds.width - 16,
               height: 22)
    }

    private func markerCenter(_ s: RampStop) -> NSPoint {
        NSPoint(x: stripRect.minX + CGFloat(s.pos) * stripRect.width,
                y: stripRect.minY - 11)
    }

    private var theme: CustomTheme { CustomTheme(name: "", stops: stops) }

    override func draw(_ dirtyRect: NSRect) {
        IconRenderer.barIsDark = effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        // the gradient strip
        let strip = stripRect
        let n = max(2, Int(strip.width / 3))
        for i in 0..<n {
            let x = Double(i) / Double(n - 1)
            let c = IconRenderer.customRampColor(theme, x)
            NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                    blue: CGFloat(c.b) / 255, alpha: 1).setFill()
            NSRect(x: strip.minX + CGFloat(i) * strip.width / CGFloat(n),
                   y: strip.minY, width: strip.width / CGFloat(n) + 1,
                   height: strip.height).fill()
        }
        NSColor.separatorColor.setStroke()
        let frame = NSBezierPath(rect: strip)
        frame.lineWidth = 1
        frame.stroke()

        // the color points, hanging under the strip at their positions
        for (i, s) in stops.enumerated() {
            let p = markerCenter(s)
            let r: CGFloat = i == selected ? 8 : 6.5
            let dot = NSBezierPath(ovalIn: NSRect(
                x: p.x - r, y: p.y - r, width: r * 2, height: r * 2))
            let c = s.rgb
            NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                    blue: CGFloat(c.b) / 255, alpha: 1).setFill()
            dot.fill()
            (i == selected ? NSColor.controlAccentColor
                           : NSColor.separatorColor).setStroke()
            dot.lineWidth = i == selected ? 2.5 : 1
            dot.stroke()
        }

        // nine session dots: what a fleet looks like under this gradient
        let rr: CGFloat = 5
        for i in 0..<9 {
            let x = Double(i) / 8.0
            let c = IconRenderer.customRampColor(theme, x)
            NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                    blue: CGFloat(c.b) / 255, alpha: 1).setFill()
            let cx = bounds.width * (0.06 + 0.88 * CGFloat(i) / 8.0)
            NSBezierPath(ovalIn: NSRect(
                x: cx - rr, y: 2, width: rr * 2, height: rr * 2)).fill()
        }
    }

    // MARK: interaction

    private func markerAt(_ p: NSPoint) -> Int? {
        for (i, s) in stops.enumerated() {
            let c = markerCenter(s)
            if hypot(p.x - c.x, p.y - c.y) < 11 { return i }
        }
        return nil
    }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if let i = markerAt(p) {
            selected = i
            dragging = true
            needsDisplay = true
            onSelect?()
            return
        }
        if stripRect.insetBy(dx: 0, dy: -6).contains(p),
           event.clickCount == 2 {
            let pos = Double((p.x - stripRect.minX) / stripRect.width)
            addStop(at: min(max(pos, 0), 1))
        }
    }

    override func mouseDragged(with event: NSEvent) {
        guard dragging else { return }
        let p = convert(event.locationInWindow, from: nil)
        let pos = Double((p.x - stripRect.minX) / stripRect.width)
        stops[selected].pos = min(max(pos, 0), 1)
        onChange?()
    }

    override func mouseUp(with event: NSEvent) {
        dragging = false
    }

    func addStop(at pos: Double) {
        let c = IconRenderer.customRampColor(theme, pos)
        stops.append(RampStop(pos: pos, color: [Int(c.r), Int(c.g), Int(c.b)]))
        selected = stops.count - 1
        onChange?()
        onSelect?()
    }

    func removeSelected() {
        guard stops.count > 2 else { return }
        stops.remove(at: selected)
        selected = min(selected, stops.count - 1)
        onChange?()
        onSelect?()
    }

    var selectedColor: NSColor {
        get {
            let c = stops[selected].rgb
            return NSColor(srgbRed: CGFloat(c.r) / 255,
                           green: CGFloat(c.g) / 255,
                           blue: CGFloat(c.b) / 255, alpha: 1)
        }
        set {
            let s = newValue.usingColorSpace(.sRGB) ?? newValue
            stops[selected].color = [Int((s.redComponent * 255).rounded()),
                                     Int((s.greenComponent * 255).rounded()),
                                     Int((s.blueComponent * 255).rounded())]
            onChange?()
        }
    }
}

final class ThemeManager: NSObject, NSWindowDelegate {
    static let shared = ThemeManager()
    /// Called after any change that should repaint the status item.
    var onChange: (() -> Void)?

    private var window: NSWindow?
    private let picker = NSPopUpButton()
    private let nameField = NSTextField()
    private let colorWell = NSColorWell()
    private let editor = GradientEditorView()

    func show(newDraft: Bool) {
        buildWindowIfNeeded()
        reloadPicker(selectNew: newDraft)
        loadSelection()
        // open on the mouse's screen, floating (multi-display: "main" is
        // often the screen the user is NOT looking at)
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
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    // MARK: window construction

    private let W: CGFloat = 400

    private func buildWindowIfNeeded() {
        guard window == nil else { return }
        let H: CGFloat = 316
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: W, height: H),
            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "amtrino themes"
        w.isReleasedWhenClosed = false
        w.center()
        w.delegate = self
        let v = NSView(frame: w.contentLayoutRect)

        func label(_ s: String, _ x: CGFloat, _ ly: CGFloat,
                   w lw: CGFloat = 120, dim: Bool = false) {
            let l = NSTextField(labelWithString: s)
            l.frame = NSRect(x: x, y: ly, width: lw, height: 16)
            l.font = .systemFont(ofSize: dim ? 11 : 12,
                                 weight: dim ? .medium : .regular)
            if dim { l.textColor = .tertiaryLabelColor }
            v.addSubview(l)
        }

        var y = H - 44
        picker.frame = NSRect(x: 16, y: y, width: W - 32, height: 26)
        picker.target = self
        picker.action = #selector(pickerChanged)
        v.addSubview(picker)
        y -= 34
        label("Name", 16, y + 3, w: 60)
        nameField.frame = NSRect(x: 92, y: y, width: W - 108, height: 24)
        v.addSubview(nameField)
        y -= 24
        label("gradient points — sessions are colored along this ramp",
              16, y, w: W - 32, dim: true)
        y -= 86
        editor.frame = NSRect(x: 16, y: y, width: W - 32, height: 82)
        editor.onChange = { [weak self] in self?.editorChanged() }
        editor.onSelect = { [weak self] in
            guard let self else { return }
            self.colorWell.color = self.editor.selectedColor
        }
        v.addSubview(editor)
        y -= 24
        label("drag a point to move it · double-click the strip to add one",
              16, y, w: W - 32, dim: true)
        y -= 38
        label("Point color", 16, y + 7, w: 76)
        colorWell.frame = NSRect(x: 96, y: y, width: 56, height: 30)
        colorWell.target = self
        colorWell.action = #selector(colorChanged)
        v.addSubview(colorWell)
        let add = NSButton(title: "+", target: self, action: #selector(addPoint))
        add.frame = NSRect(x: 168, y: y, width: 40, height: 30)
        add.bezelStyle = .rounded
        v.addSubview(add)
        let rem = NSButton(title: "−", target: self,
                           action: #selector(removePoint))
        rem.frame = NSRect(x: 212, y: y, width: 40, height: 30)
        rem.bezelStyle = .rounded
        v.addSubview(rem)
        y -= 40
        func button(_ title: String, _ x: CGFloat, _ sel: Selector) {
            let b = NSButton(title: title, target: self, action: sel)
            b.frame = NSRect(x: x, y: y, width: 118, height: 28)
            b.bezelStyle = .rounded
            v.addSubview(b)
        }
        button("Delete", 16, #selector(deleteTheme))
        button("Save", 142, #selector(saveTheme))
        button("Save & Apply", 266, #selector(saveApply))

        w.contentView = v
        window = w
    }

    private func editorChanged() {
        colorWell.color = editor.selectedColor
    }

    // MARK: state

    private var selectedName: String? {
        picker.indexOfSelectedItem < Settings.customThemes.count
            ? Settings.customThemes[picker.indexOfSelectedItem].name : nil
    }

    private func reloadPicker(selectNew: Bool) {
        picker.removeAllItems()
        for t in Settings.customThemes { picker.addItem(withTitle: t.name) }
        picker.addItem(withTitle: "— new theme —")
        if selectNew || Settings.customThemes.isEmpty {
            picker.selectItem(at: picker.numberOfItems - 1)
        }
    }

    private func loadSelection() {
        if let name = selectedName,
           let t = Settings.customThemes.first(where: { $0.name == name }) {
            nameField.stringValue = t.name
            editor.stops = t.stops
        } else {
            nameField.stringValue = ""
            editor.stops = [RampStop(pos: 0, color: [30, 30, 60]),
                            RampStop(pos: 1, color: [240, 200, 120])]
        }
        colorWell.color = editor.selectedColor
    }

    private func currentDraft() -> CustomTheme {
        var name = nameField.stringValue
            .trimmingCharacters(in: .whitespaces)
        if name.isEmpty { name = "custom" }
        return CustomTheme(name: name, stops: editor.stops)
    }

    // MARK: actions

    @objc private func pickerChanged() { loadSelection() }

    @objc private func colorChanged() {
        editor.selectedColor = colorWell.color
    }

    @objc private func addPoint() {
        // largest gap's midpoint keeps additions useful
        let sorted = editor.stops.sorted { $0.pos < $1.pos }
        var bestA = 0.0, bestB = 1.0, bestGap = -1.0
        for i in 0..<(sorted.count - 1) {
            let gap = sorted[i + 1].pos - sorted[i].pos
            if gap > bestGap {
                bestGap = gap
                bestA = sorted[i].pos
                bestB = sorted[i + 1].pos
            }
        }
        editor.addStop(at: (bestA + bestB) / 2)
    }

    @objc private func removePoint() { editor.removeSelected() }

    @objc private func saveTheme() {
        let draft = currentDraft()
        var list = Settings.customThemes
        if let old = selectedName, old != draft.name {
            list.removeAll { $0.name == old }
            if Settings.themeRaw == "custom:\(old)" {
                Settings.themeRaw = "custom:\(draft.name)"
            }
        }
        if let i = list.firstIndex(where: { $0.name == draft.name }) {
            list[i] = draft
        } else {
            list.append(draft)
        }
        Settings.customThemes = list
        reloadPicker(selectNew: false)
        picker.selectItem(withTitle: draft.name)
        onChange?()
    }

    @objc private func saveApply() {
        saveTheme()
        Settings.themeRaw = "custom:\(currentDraft().name)"
        onChange?()
    }

    @objc private func deleteTheme() {
        guard let name = selectedName else { return }
        Settings.customThemes.removeAll { $0.name == name }
        if Settings.themeRaw == "custom:\(name)" {
            Settings.themeRaw = Theme.identity.rawValue
        }
        reloadPicker(selectNew: true)
        loadSelection()
        onChange?()
    }

    func windowWillClose(_ notification: Notification) {
        NSColorPanel.shared.close()
    }
}
