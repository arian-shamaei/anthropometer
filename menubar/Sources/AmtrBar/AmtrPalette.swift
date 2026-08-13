// The palette handshake with a running amtr TUI (SPEC e palette export):
// amtr writes ~/.claude/amtr/palette.json — {pid, session, palette:[[r,g,b]×4]}
// — on attach and on tank reroll. When that file names OUR displayed session
// and its pid is still alive, the single-session tank adopts amtr's exact
// gradient, so the menu bar and the terminal show the same tank. Absent,
// stale, or for another session: the deterministic sess_seed palette.
// amtrino stays fully standalone — this is an optional match, never a
// dependency.

import Foundation

enum AmtrPalette {
    private static var cached: (mtime: Date, pid: Int32, session: String,
                                stops: [RGB])?

    static var path: String {
        NSHomeDirectory() + "/.claude/amtr/palette.json"
    }

    /// The export file's mtime, nil when absent — the cheap change signal
    /// the redraw clock polls (a reroll must repaint even on a quiet fleet).
    static func fileMtime() -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: path))?[
            .modificationDate] as? Date
    }

    /// The exported stops for `session`, or nil (no file / other session /
    /// dead amtr). mtime-cached; a kill(pid,0) liveness probe per lookup.
    static func stops(for session: String) -> [RGB]? {
        guard let attrs = try? FileManager.default
            .attributesOfItem(atPath: path),
            let mtime = attrs[.modificationDate] as? Date else { return nil }
        if cached?.mtime != mtime {
            guard let data = FileManager.default.contents(atPath: path),
                  let parsed = parse(data) else { return nil }
            cached = (mtime, parsed.pid, parsed.session, parsed.stops)
        }
        guard let c = cached, c.session == session,
              kill(c.pid, 0) == 0 else { return nil }
        return c.stops
    }

    /// Pure parser (selfcheck target).
    static func parse(_ data: Data) -> (pid: Int32, session: String, stops: [RGB])? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data))
                as? [String: Any],
              let pid = obj["pid"] as? Int,
              let session = obj["session"] as? String,
              let raw = obj["palette"] as? [[Int]], raw.count == 4
        else { return nil }
        let stops: [RGB] = raw.compactMap { c in
            guard c.count == 3,
                  c.allSatisfy({ (0...255).contains($0) }) else { return nil }
            return (UInt8(c[0]), UInt8(c[1]), UInt8(c[2]))
        }
        guard stops.count == 4 else { return nil }
        return (Int32(pid), session, stops)
    }
}
