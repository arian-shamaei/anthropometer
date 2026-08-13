// Jump the user to a session: clicking a "finished" notification focuses
// the terminal pane the session lives in. Claude Code's roster records each
// session's tmux home ("session:@window.%pane"), so for tmux-hosted
// sessions this selects the exact pane and activates the terminal app
// hosting the tmux client. Best-effort everywhere: failures log, never throw.

import AppKit

enum SessionFocus {
    /// "misc:@8.%85" → (window: "misc:@8", pane: "%85"). Pure (selfchecked).
    static func parseTmuxTarget(_ t: String)
        -> (window: String, pane: String?)? {
        guard !t.isEmpty else { return nil }
        // the pane id follows the LAST ".%" (session names may contain dots)
        if let r = t.range(of: ".%", options: .backwards) {
            let win = String(t[..<r.lowerBound])
            let pane = "%" + String(t[r.upperBound...])
            return (win, pane)
        }
        return (t, nil)
    }

    static func focus(tmux: String?, pid: Int?) {
        DispatchQueue.global().async {
            if let t = tmux, let target = parseTmuxTarget(t) {
                runTmux(["select-window", "-t", target.window])
                if let pane = target.pane {
                    runTmux(["select-pane", "-t", pane])
                }
                // point an attached client at the session (no-op when there)
                runTmux(["switch-client", "-t", target.window])
                activateTerminalHostingTmux()
            } else if let pid {
                // no tmux: at least bring the hosting terminal app forward —
                // works for ANY emulator via process ancestry, no per-app code
                activateAncestorApp(of: Int32(pid))
            }
        }
    }

    // MARK: plumbing

    private static func tmuxPath() -> String? {
        for p in ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux",
                  "/usr/bin/tmux"]
        where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return nil
    }

    @discardableResult
    private static func runTmux(_ args: [String]) -> String? {
        guard let tmux = tmuxPath() else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tmux)
        p.arguments = args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            return String(data: out.fileHandleForReading.readDataToEndOfFile(),
                          encoding: .utf8)
        } catch {
            NSLog("%@", "amtrino: tmux \(args.first ?? "") failed: \(error)")
            return nil
        }
    }

    /// Activate the GUI app hosting the (first) attached tmux client, found
    /// by walking the client process's ancestry to a regular application.
    private static func activateTerminalHostingTmux() {
        guard let outStr = runTmux(["list-clients", "-F", "#{client_pid}"]),
              let firstLine = outStr.split(separator: "\n").first,
              let pid = Int32(firstLine.trimmingCharacters(in: .whitespaces))
        else { return }
        activateAncestorApp(of: pid)
    }

    /// Walk a process's ancestry to the first regular GUI app and activate
    /// it — emulator-agnostic (Ghostty, iTerm, Terminal, Alacritty, …).
    private static func activateAncestorApp(of startPid: Int32) {
        var pid = startPid
        for _ in 0..<12 {
            if let app = NSRunningApplication(processIdentifier: pid),
               app.activationPolicy == .regular {
                DispatchQueue.main.async {
                    app.activate()
                }
                return
            }
            guard let parent = ppid(of: pid), parent > 1 else { return }
            pid = parent
        }
    }

    private static func ppid(of pid: Int32) -> Int32? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-o", "ppid=", "-p", String(pid)]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        p.waitUntilExit()
        let s = String(data: out.fileHandleForReading.readDataToEndOfFile(),
                       encoding: .utf8) ?? ""
        return Int32(s.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}
