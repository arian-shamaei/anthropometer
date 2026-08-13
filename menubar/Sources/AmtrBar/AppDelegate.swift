// The status item and its clocks. Redraw is driven by (a) fleet ticks and
// (b) a 100 ms animation timer that runs ONLY while something on screen
// animates (busy pulse / finish flash) — the TUI's clock discipline.

import AppKit
import ServiceManagement
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate,
                         UNUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private let client = FleetClient()
    private let store = FleetStore()
    private var animTimer: Timer?
    private var linkUp = false
    private var canNotify = false
    private var notifyDenied = false
    private var legendView: LegendView?
    private var lastPaletteMtime: Date?
    private var appearanceObs: NSKeyValueObservation?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        // the bar's appearance changes often (light/dark, wallpaper tint,
        // per-display) — repaint the moment macOS re-resolves it, not on
        // the next fleet tick
        appearanceObs = statusItem.button?.observe(\.effectiveAppearance) {
            [weak self] _, _ in
            DispatchQueue.main.async { self?.redraw() }
        }

        // Notifications need a real bundle; a bare `swift run` binary has
        // none, so degrade to icon-flash-only silently.
        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = self
            UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound]) { ok, err in
                    NSLog("%@", "amtrino: notification auth ok=\(ok) err=\(err.map(String.init(describing:)) ?? "none")")
                    DispatchQueue.main.async {
                        self.canNotify = ok
                        self.notifyDenied = !ok
                    }
                }
        }

        store.onFinished = { [weak self] s in
            self?.postFinished(s)
        }
        client.onFleet = { [weak self] rows in
            guard let self else { return }
            self.store.apply(rows)
            self.redraw()
            self.retimer()
        }
        client.onLink = { [weak self] up in
            guard let self else { return }
            self.linkUp = up
            self.redraw()
        }
        // palette handshake clock: a reroll in amtr must repaint the tank
        // even when the fleet itself is quiet (change-detected feed)
        client.onTick = { [weak self] in
            guard let self else { return }
            // appearance drift safety net (KVO is primary): repaint if the
            // bar flipped since the last draw
            if let btn = self.statusItem.button {
                let dark = btn.effectiveAppearance
                    .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                if dark != IconRenderer.barIsDark { self.redraw() }
            }
            guard Settings.mode == .single else { return }
            let m = AmtrPalette.fileMtime()
            if m != self.lastPaletteMtime {
                self.lastPaletteMtime = m
                self.redraw()
            }
        }
        client.start()
        redraw()

        // very first launch: greet with the welcome/help tour (once)
        if !Settings.hasSeenWelcome {
            Settings.hasSeenWelcome = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                HelpPanel.shared.show()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        client.stop()
    }

    // MARK: drawing

    /// AMTRINO_DUMP=<path.png>: write each rendered icon frame there and log
    /// the status-item window frame — the visual-validation hook (the tmux
    /// capture-pane of the menu bar, where a screenshot can't be trusted to
    /// find the item among a bar full of strangers).
    private func dumpFrame(_ btn: NSStatusBarButton) {
        guard let path = ProcessInfo.processInfo.environment["AMTRINO_DUMP"] else { return }
        if let img = btn.image,
           let tiff = img.tiffRepresentation,
           let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
        }
        if let win = btn.window {
            NSLog("%@", "amtrino frame: \(NSStringFromRect(win.frame)) title='\(btn.attributedTitle.string)'")
        }
    }

    private func redraw() {
        defer { statusItem.button.map(dumpFrame) }
        guard let btn = statusItem.button else { return }
        // identity mode adapts to the bar it actually sits on
        let dark = btn.effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        if dark != IconRenderer.barIsDark {
            NSLog("%@", "amtrino: bar appearance -> \(dark ? "dark" : "light") (\(btn.effectiveAppearance.name.rawValue))")
        }
        IconRenderer.barIsDark = dark
        guard linkUp else {
            btn.image = IconRenderer.linkDownImage()
            btn.attributedTitle = NSAttributedString(string: "")
            return
        }
        let now = Date()
        switch Settings.mode {
        case .grid:
            let slots = store.slotted(hidden: Settings.hidden,
                                      slots: Settings.slots)
            btn.image = slots.allSatisfy { $0 == nil }
                ? IconRenderer.emptyFleetImage()
                : IconRenderer.gridImage(slots, now: now)
            btn.attributedTitle = NSAttributedString(string: "")
        case .single:
            guard let one = store.single(pinned: Settings.pinned) else {
                // healthy feed, no sessions: the empty grid, not "dead"
                btn.image = IconRenderer.emptyFleetImage()
                btn.attributedTitle = NSAttributedString(string: "")
                return
            }
            switch Settings.singleStyle {
            case .tank:
                btn.image = IconRenderer.tankImage(one, now: now)
                btn.attributedTitle = NSAttributedString(string: "")
            case .percent:
                btn.image = nil
                btn.attributedTitle = IconRenderer.percentTitle(one)
            }
        }
    }

    /// Arm the 100 ms clock only while something animates.
    private func retimer() {
        let animating = linkUp && store.animating(hidden: Settings.hidden)
        if animating, animTimer == nil {
            animTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.redraw()
                if !(self.linkUp && self.store.animating(hidden: Settings.hidden)) {
                    self.animTimer?.invalidate()
                    self.animTimer = nil
                    self.redraw()
                }
            }
        } else if !animating {
            animTimer?.invalidate()
            animTimer = nil
        }
    }

    // MARK: finished notification

    private func postFinished(_ s: FleetSession) {
        guard Settings.notifyOnFinish else { return }
        guard canNotify else {
            // no banner permission: still make the finish audible
            NSLog("%@", "amtrino: finish for \(s.name) — no notification permission, sound fallback")
            NSSound(named: "Glass")?.play()
            return
        }
        let content = UNMutableNotificationContent()
        content.title = "\(s.name) finished"
        content.body = s.lastPrompt.map { String($0.prefix(120)) }
            ?? s.projectTail
        content.sound = .default
        // clicking the banner jumps to the session (SessionFocus)
        content.userInfo = ["session": s.id, "tmux": s.tmux ?? ""]
        // macOS 26 banners ignore legacy .icns (generic glass tile instead),
        // so carry the session's identity as an image ATTACHMENT — the
        // banner then shows WHICH session finished, in its color.
        if let att = identityAttachment(s) {
            content.attachments = [att]
        }
        let req = UNNotificationRequest(
            identifier: "amtrino-\(s.id)-\(Date().timeIntervalSince1970)",
            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { err in
            if let err {
                NSLog("%@", "amtrino: notification post failed: \(err)")
            }
        }
    }

    /// Notification click → jump to the session's terminal pane. The tmux
    /// locator travels in userInfo; a session that moved since the post is
    /// re-resolved from the current roster by id.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler:
                                @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let id = info["session"] as? String
        var tmux = info["tmux"] as? String
        var pid: Int?
        if let id, let live = store.sessions.first(where: { $0.id == id }) {
            if let t = live.tmux, !t.isEmpty { tmux = t }  // freshest wins
            pid = live.pid
        }
        SessionFocus.focus(tmux: (tmux?.isEmpty ?? true) ? nil : tmux,
                           pid: pid)
        completionHandler()
    }

    /// Banners must show even while amtrino is technically frontmost.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                @escaping (UNNotificationPresentationOptions)
                                -> Void) {
        completionHandler([.banner, .sound])
    }

    /// The session's identity dot as a notification attachment. The system
    /// MOVES the file into its own store, so a fresh temp file per post.
    private func identityAttachment(_ s: FleetSession) -> UNNotificationAttachment? {
        // finishedAgo 0.3 = the flash's identity-colored phase at full
        // brightness (the session is idle by now — the dim idle style would
        // undersell the banner)
        let d = DisplaySession(sess: s, finishedAgo: 0.3)
        let img = IconRenderer.statusDotImage(d, r: 52, canvas: 128, now: Date())
        guard let tiff = img.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:])
        else { return nil }
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("amtrino-\(s.id)-\(UUID().uuidString).png")
        do {
            try png.write(to: url)
            return try UNNotificationAttachment(identifier: "identity", url: url)
        } catch {
            NSLog("%@", "amtrino: attachment failed: \(error)")
            return nil
        }
    }

    // MARK: menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        // menu surfaces judge their OWN background (a light menu can hang
        // from a dark bar); the status-item redraw re-sets this per frame
        IconRenderer.barIsDark = NSApp.effectiveAppearance
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        menu.removeAllItems()

        let rows = store.sessions.filter { $0.status.isLiveish }
        let head = NSMenuItem(
            title: rows.isEmpty
                ? (linkUp
                    ? "no agent sessions — start claude or codex"
                    : "engine offline — is python3 installed?")
                : "\(rows.count) live session\(rows.count == 1 ? "" : "s")",
            action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        menu.addItem(.separator())

        // mode
        let grid = item("Grid — all sessions", #selector(setGrid))
        grid.state = Settings.mode == .grid ? .on : .off
        menu.addItem(grid)
        let single = item("Single session", #selector(setSingle))
        single.state = Settings.mode == .single ? .on : .off
        menu.addItem(single)

        menu.addItem(.separator())

        // sessions, in GRID ORDER, each carrying a picture of the answer to
        // "which dot am I": the 3×3 map with this session's dot lit at its
        // real position. Non-displayed sessions (hidden / overflow / single
        // mode) follow as lone identity dots. Checkmark = shown in grid;
        // in single mode selecting a session pins it.
        let now = Date()
        if Settings.mode == .grid {
            // grid mode: the whole session section is ONE custom view —
            // every open session as a row, the 9 grid nodes beneath, drag
            // to pin (standard menu items cannot be dragged)
            let section = NSMenuItem()
            let v = AssignSectionView(width: 380)
            v.storeOwner = store
            v.onChange = { [weak self] in self?.redraw() }
            v.reload(store: store)
            section.view = v
            menu.addItem(section)
        } else {
            // single mode: plain rows, click pins the shown session
            var nameCount: [String: Int] = [:]
            for s in rows { nameCount[s.name, default: 0] += 1 }
            for s in rows {
                let d = DisplaySession(sess: s, finishedAgo: nil)
                let fill = s.fill.map { " \(Int(($0 * 100).rounded()))%" } ?? ""
                let tag = (nameCount[s.name] ?? 0) > 1
                    ? " [\(s.id.prefix(4))]" : ""
                let mi = item("\(s.name)\(tag) — \(s.projectTail)\(fill)",
                              #selector(toggleSession(_:)))
                mi.representedObject = s.id
                mi.image = IconRenderer.menuDot(
                    d, gridIndex: nil, occupied: [], now: now)
                mi.state = Settings.pinned == s.id ? .on : .off
                menu.addItem(mi)
            }
        }
        if Settings.mode == .single {
            let auto = item("Auto (busiest)", #selector(pinAuto))
            auto.state = Settings.pinned == nil ? .on : .off
            menu.addItem(auto)
        }
        menu.addItem(.separator())

        // legend: live samples of every dot style (animates while open)
        menu.addItem(.separator())
        let legendItem = NSMenuItem()
        let legend = LegendView(width: 300)
        legendItem.view = legend
        legendItem.isEnabled = false
        menu.addItem(legendItem)
        legendView = legend

        menu.addItem(.separator())

        // Options ▸ — everything that isn't picking a session
        let options = NSMenuItem(title: "Options", action: nil, keyEquivalent: "")
        let opts = NSMenu()

        let theme = NSMenuItem(title: "Theme", action: nil, keyEquivalent: "")
        let themeMenu = NSMenu()
        for t in Theme.allCases {
            let mi = item(t.label, #selector(setTheme(_:)))
            mi.representedObject = t.rawValue
            mi.state = Settings.themeRaw == t.rawValue ? .on : .off
            mi.image = themeSwatch(t.rawValue)
            themeMenu.addItem(mi)
        }
        if !Settings.customThemes.isEmpty {
            themeMenu.addItem(.separator())
            for c in Settings.customThemes {
                let mi = item(c.name, #selector(setTheme(_:)))
                mi.representedObject = "custom:\(c.name)"
                mi.state = Settings.themeRaw == "custom:\(c.name)" ? .on : .off
                mi.image = themeSwatch("custom:\(c.name)")
                themeMenu.addItem(mi)
            }
        }
        themeMenu.addItem(.separator())
        themeMenu.addItem(item("New custom theme…", #selector(newCustomTheme)))
        themeMenu.addItem(item("Manage themes…", #selector(manageThemes)))
        theme.submenu = themeMenu
        opts.addItem(theme)

        let style = NSMenuItem(title: "Single style", action: nil, keyEquivalent: "")
        let styleMenu = NSMenu()
        let tank = item("Gradient tank", #selector(setStyleTank))
        tank.state = Settings.singleStyle == .tank ? .on : .off
        styleMenu.addItem(tank)
        let pct = item("Percentage", #selector(setStylePercent))
        pct.state = Settings.singleStyle == .percent ? .on : .off
        styleMenu.addItem(pct)
        style.submenu = styleMenu
        opts.addItem(style)

        opts.addItem(.separator())
        let notify = item("Notify when a response finishes", #selector(toggleNotify))
        notify.state = Settings.notifyOnFinish ? .on : .off
        opts.addItem(notify)
        if Settings.notifyOnFinish && notifyDenied {
            // permission is off at the OS level — say so instead of failing
            // silently, and hand the user the fix
            let warn = item("⚠ notifications denied — open System Settings",
                            #selector(openNotifySettings))
            opts.addItem(warn)
        }
        // login item registration needs a real bundle (SMAppService)
        if Bundle.main.bundleIdentifier != nil {
            let login = item("Launch at login", #selector(toggleLogin))
            login.state = SMAppService.mainApp.status == .enabled ? .on : .off
            opts.addItem(login)
        }
        opts.addItem(.separator())
        opts.addItem(item("Help", #selector(showHelp)))
        opts.addItem(item("Report a bug…", #selector(reportBug)))
        options.submenu = opts
        menu.addItem(options)

        menu.addItem(.separator())
        menu.addItem(item("Quit amtrino", #selector(quit), key: "q"))
    }

    func menuWillOpen(_ menu: NSMenu) {
        legendView?.startAnimating()
    }

    func menuDidClose(_ menu: NSMenu) {
        legendView?.stopAnimating()
        legendView = nil
    }

    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let mi = NSMenuItem(title: title, action: action, keyEquivalent: key)
        mi.target = self
        return mi
    }

    // MARK: actions

    @objc private func setGrid() { Settings.mode = .grid; redraw(); retimer() }
    @objc private func setSingle() { Settings.mode = .single; redraw(); retimer() }
    @objc private func setStyleTank() { Settings.singleStyle = .tank; redraw() }
    @objc private func setStylePercent() { Settings.singleStyle = .percent; redraw() }

    @objc private func setTheme(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String else { return }
        Settings.themeRaw = raw
        redraw()
    }

    @objc private func newCustomTheme() {
        ThemeManager.shared.onChange = { [weak self] in self?.redraw() }
        ThemeManager.shared.show(newDraft: true)
    }

    @objc private func manageThemes() {
        ThemeManager.shared.onChange = { [weak self] in self?.redraw() }
        ThemeManager.shared.show(newDraft: false)
    }

    /// A 24×10 swatch strip for any theme's menu row.
    private func themeSwatch(_ raw: String) -> NSImage {
        let colors = IconRenderer.themeSwatchColors(raw, n: 24)
        let img = NSImage(size: NSSize(width: 24, height: 10), flipped: false) { rect in
            for (i, c) in colors.enumerated() {
                NSColor(srgbRed: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255,
                        blue: CGFloat(c.b) / 255, alpha: 1).setFill()
                NSRect(x: CGFloat(i), y: 1, width: 1.5,
                       height: rect.height - 2).fill()
            }
            return true
        }
        img.isTemplate = false
        return img
    }
    @objc private func pinAuto() { Settings.pinned = nil; redraw() }
    @objc private func toggleNotify() { Settings.notifyOnFinish.toggle() }

    @objc private func openNotifySettings() {
        if let url = URL(string:
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
    }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func showHelp() { HelpPanel.shared.show() }

    @objc private func reportBug() {
        BugReport.compose(linkUp: linkUp, sessions: store.sessions)
    }

    @objc private func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            NSLog("%@", "amtrino: login item toggle failed: \(error)")
        }
    }

    @objc private func toggleSession(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        if Settings.mode == .single {
            Settings.pinned = id
        } else {
            var h = Settings.hidden
            if h.contains(id) { h.remove(id) } else { h.insert(id) }
            Settings.hidden = h
        }
        redraw()
        retimer()
    }
}
