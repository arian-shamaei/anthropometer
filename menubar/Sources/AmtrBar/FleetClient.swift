// Spawns the amtr engine in SPEC f2 fleet-feed mode and streams its rows.
// The same split-process discipline as the TUI and the amtr3d bridge: the
// engine is a child on pipes, this process only renders. The engine copy is
// bundled (synced by packaging/sync-engine.sh) so amtrino works without the
// TUI installed; $AMTRINO_ENGINE overrides for development.

import Foundation

final class FleetClient {
    /// Called on the main queue with each changed roster.
    var onFleet: (([FleetSession]) -> Void)?
    /// Called on the main queue when the feed dies/revives (icon shows it).
    var onLink: ((Bool) -> Void)?
    /// Called on the main queue on EVERY feed line (fleet or heartbeat) —
    /// the app's steady clock for cheap external checks (palette file mtime).
    var onTick: (() -> Void)?

    private var proc: Process?
    private var buf = Data()
    private var stopping = false
    private var lastLine = Date()
    private var watchdog: Timer?
    private let pollSecs = 2.0

    func start() {
        stopping = false
        launch()
        // Watchdog: the feed heartbeats every poll tick (SPEC f2), so a
        // silent feed is a dead/hung feed — restart it.
        watchdog = Timer.scheduledTimer(withTimeInterval: pollSecs * 2, repeats: true) { [weak self] _ in
            guard let self, let p = self.proc else { return }
            if p.isRunning && Date().timeIntervalSince(self.lastLine) > self.pollSecs * 4 {
                p.terminate()
            }
        }
    }

    func stop() {
        stopping = true
        watchdog?.invalidate()
        watchdog = nil
        proc?.terminate()
        proc = nil
    }

    // MARK: engine resolution

    static func enginePath() -> String? {
        if let env = ProcessInfo.processInfo.environment["AMTRINO_ENGINE"],
           FileManager.default.isReadableFile(atPath: env) {
            return env
        }
        if let bundled = Bundle.module.path(forResource: "amtr_engine", ofType: "py") {
            return bundled
        }
        // Dev fallback: walk up from the executable looking for the repo copy.
        var dir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        for _ in 0..<6 {
            let candidate = dir.appendingPathComponent("amtr_engine.py").path
            if FileManager.default.isReadableFile(atPath: candidate) { return candidate }
            dir.deleteLastPathComponent()
        }
        return nil
    }

    // MARK: lifecycle

    private func launch() {
        guard !stopping, let engine = FleetClient.enginePath() else {
            NSLog("amtrino: engine not found (set AMTRINO_ENGINE)")
            DispatchQueue.main.async { self.onLink?(false) }
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", engine, "--fleet", "--live-only",
                       "--poll-secs", String(pollSecs)]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        buf.removeAll()
        lastLine = Date()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] fh in
            let d = fh.availableData
            guard let self else { return }
            if d.isEmpty {   // EOF
                fh.readabilityHandler = nil
                return
            }
            self.ingest(d)
        }
        p.terminationHandler = { [weak self] _ in
            guard let self else { return }
            DispatchQueue.main.async {
                self.onLink?(false)
                guard !self.stopping else { return }
                // engine died on its own: revive after a beat
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { self.launch() }
            }
        }
        do {
            try p.run()
            proc = p
            DispatchQueue.main.async { self.onLink?(true) }
        } catch {
            NSLog("amtrino: engine spawn failed: \(error)")
            DispatchQueue.main.async { self.onLink?(false) }
        }
    }

    private func ingest(_ d: Data) {
        buf.append(d)
        while let nl = buf.firstIndex(of: 0x0A) {
            let line = buf.subdata(in: buf.startIndex..<nl)
            buf.removeSubrange(buf.startIndex...nl)
            lastLine = Date()
            handle(line)
        }
    }

    private func handle(_ line: Data) {
        // §b wire rules: skip malformed lines, ignore unknown types.
        guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let type = obj["type"] as? String else { return }
        switch type {
        case "fleet":
            let rows = (obj["sessions"] as? [[String: Any]] ?? [])
                .compactMap(FleetSession.init(json:))
            DispatchQueue.main.async { self.onFleet?(rows) }
        default:
            break
        }
        DispatchQueue.main.async { self.onTick?() }
    }
}
