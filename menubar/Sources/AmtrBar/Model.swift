// The wire model: one row of the engine's `fleet` message (SPEC b `Sess`,
// consumed via the SPEC f2 headless stream). Unknown fields are ignored and
// unknown statuses map to `.unknown` — the §b version-drift law.

import Foundation

enum SessionStatus: Equatable {
    case busy       // roster: responding right now
    case shell      // roster: user parked in shell mode — alive, not responding
    case idle       // roster: waiting for a prompt
    case stalled    // busy but transcript quiet >120 s (engine-derived)
    case dead       // roster entry with a dead pid
    case offline    // transcript with no roster entry
    case unknown(String)

    init(wire: String) {
        switch wire {
        case "busy": self = .busy
        case "shell": self = .shell
        case "idle": self = .idle
        case "stalled": self = .stalled
        case "dead": self = .dead
        case "offline": self = .offline
        default: self = .unknown(wire)
        }
    }

    /// The session is running and worth showing by default.
    var isLiveish: Bool {
        switch self {
        case .dead, .offline: return false
        default: return true
        }
    }
}

struct FleetSession: Equatable {
    let id: String
    let path: String
    let pid: Int?
    let name: String
    let project: String
    let status: SessionStatus
    let mtime: Double
    let live: Bool
    let resident: Int?
    let budget: Int?
    let lastPrompt: String?
    /// tmux locator "session:@window.%pane" when the session lives in tmux.
    let tmux: String?
    /// which agent CLI this row belongs to ("claude" when absent).
    let provider: String

    /// Context fill 0...1, nil when resident is unknown.
    var fill: Double? {
        guard let r = resident, let b = budget, b > 0 else { return nil }
        return min(max(Double(r) / Double(b), 0.0), 1.0)
    }

    /// Last path component of the project — the human handle next to `name`.
    var projectTail: String {
        (project as NSString).lastPathComponent
    }

    init?(json: [String: Any]) {
        guard let id = json["id"] as? String else { return nil }
        self.id = id
        self.path = json["path"] as? String ?? ""
        self.pid = json["pid"] as? Int
        self.name = json["name"] as? String ?? String(id.prefix(8))
        self.project = json["project"] as? String ?? ""
        self.status = SessionStatus(wire: json["status"] as? String ?? "")
        self.mtime = json["mtime"] as? Double ?? 0
        self.live = json["live"] as? Bool ?? false
        self.resident = json["resident"] as? Int
        self.budget = json["budget"] as? Int
        self.lastPrompt = json["last_prompt"] as? String
        self.tmux = json["tmux"] as? String
        self.provider = json["provider"] as? String ?? "claude"
    }
}
