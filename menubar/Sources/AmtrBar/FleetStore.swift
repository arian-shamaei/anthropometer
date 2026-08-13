// Session state between fleet ticks: stable ordering, the busy→idle edge
// ("response finished"), and which sessions the icon actually shows.

import Foundation

/// One session as the icon wants it: wire row + transition decoration.
struct DisplaySession: Equatable {
    let sess: FleetSession
    /// Seconds since this session finished a response (busy → idle/shell
    /// edge), nil once the flash window has passed.
    let finishedAgo: TimeInterval?
}

final class FleetStore {
    /// How long the "response finished" flash lasts.
    static let flashSecs: TimeInterval = 6.0

    private(set) var sessions: [FleetSession] = []
    private var lastStatus: [String: SessionStatus] = [:]
    private var finishedAt: [String: Date] = [:]
    /// Fired on the busy→idle edge (notification hook), main queue.
    var onFinished: ((FleetSession) -> Void)?

    func apply(_ rows: [FleetSession]) {
        // defensive: one row per id, whatever the feed sends (§b drift law)
        var seen = Set<String>()
        let rows = rows.filter { seen.insert($0.id).inserted }
        let now = Date()
        for s in rows {
            let prev = lastStatus[s.id]
            // The edge: was responding, now settled. `stalled` counts as
            // responding — long responses go quiet >120 s, get remapped
            // busy→stalled, and then FINISH as stalled→idle; missing that
            // edge silences exactly the responses worth notifying about.
            // dead/offline ends without an answer: no flash.
            let wasResponding = prev == .busy || prev == .stalled
            if wasResponding, s.status == .idle || s.status == .shell {
                finishedAt[s.id] = now
                onFinished?(s)
            }
            lastStatus[s.id] = s.status
        }
        // Stable (project, id) order — the systemwide wall's law, so the
        // menu bar dots and the TUI tiles agree on position.
        sessions = rows.sorted {
            ($0.project, $0.id) < ($1.project, $1.id)
        }
        // forget edges older than the flash window and rows that vanished
        let liveIds = Set(rows.map(\.id))
        finishedAt = finishedAt.filter {
            liveIds.contains($0.key) && now.timeIntervalSince($0.value) < Self.flashSecs
        }
        lastStatus = lastStatus.filter { liveIds.contains($0.key) }
    }

    /// The sessions the icon shows, decorated, honoring the user's hidden
    /// set and the 9-dot cap.
    func displayed(hidden: Set<String>, cap: Int = 9) -> [DisplaySession] {
        let now = Date()
        return sessions
            .filter { !hidden.contains($0.id) && $0.status.isLiveish }
            .prefix(cap)
            .map { s in
                let ago = finishedAt[s.id].map { now.timeIntervalSince($0) }
                return DisplaySession(
                    sess: s,
                    finishedAgo: (ago ?? Self.flashSecs) < Self.flashSecs ? ago : nil)
            }
    }

    /// The 9 grid slots: assigned sessions pinned to their nodes (an
    /// assigned-but-absent slot stays EMPTY — the node is reserved),
    /// unassigned slots auto-filled from the remaining pool in stable
    /// order. This is the grid's layout law once assignments exist.
    func slotted(hidden: Set<String>, slots: [String?]) -> [DisplaySession?] {
        let pool = displayed(hidden: hidden, cap: Int.max)
        var byId = [String: DisplaySession]()
        for d in pool { byId[d.sess.id] = d }
        var out = [DisplaySession?](repeating: nil, count: 9)
        var used = Set<String>()
        var reserved = Set<Int>()
        for i in 0..<9 {
            guard i < slots.count, let sid = slots[i] else { continue }
            reserved.insert(i)
            if let d = byId[sid] {
                out[i] = d
                used.insert(sid)
            }
        }
        var rest = pool.filter { !used.contains($0.sess.id) }.makeIterator()
        for i in 0..<9 where !reserved.contains(i) {
            out[i] = rest.next()
        }
        return out
    }

    /// The single session for one-session mode: the pinned id when it is
    /// still around, else the busiest (busy > stalled > shell > idle), ties
    /// to the most recently active.
    func single(pinned: String?) -> DisplaySession? {
        if let pin = pinned,
           let s = displayed(hidden: [], cap: Int.max).first(where: { $0.sess.id == pin }) {
            return s
        }
        func rank(_ st: SessionStatus) -> Int {
            switch st {
            case .busy: return 0
            case .stalled: return 1
            case .shell: return 2
            case .idle: return 3
            default: return 4
            }
        }
        return displayed(hidden: [], cap: Int.max)
            .sorted {
                let (a, b) = (rank($0.sess.status), rank($1.sess.status))
                return a != b ? a < b : $0.sess.mtime > $1.sess.mtime
            }
            .first
    }

    /// True while anything on screen animates (busy pulse or finish flash) —
    /// the redraw timer only runs then, the TUI's clock discipline.
    func animating(hidden: Set<String>) -> Bool {
        slotted(hidden: hidden, slots: Settings.slots).contains {
            $0?.sess.status == .busy || $0?.finishedAgo != nil
        }
    }
}
