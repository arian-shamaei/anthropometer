// UserDefaults-backed configuration. Keys are stable; defaults are the
// zero-config experience: grid mode, everything shown, no notifications.

import Foundation

enum IconMode: String {
    case grid, single
}

/// One gradient point: a position on the ramp (0...1) and its color.
struct RampStop: Codable, Equatable {
    var pos: Double
    var color: [Int]   // [r,g,b] 0...255

    var rgb: RGB {
        func ch(_ i: Int) -> UInt8 {
            UInt8(min(255, max(0, i < color.count ? color[i] : 0)))
        }
        return (ch(0), ch(1), ch(2))
    }
}

/// A user-authored theme: a gradient defined by ≥2 color points. Sessions
/// land on a stable seed-keyed ramp position. Legacy two-color themes
/// ({low, high}) decode into the endpoint stops automatically.
struct CustomTheme: Codable, Equatable {
    var name: String
    var stops: [RampStop]

    init(name: String, stops: [RampStop]) {
        self.name = name
        self.stops = stops
    }

    /// Legacy convenience: two endpoints.
    init(name: String, low: [Int], high: [Int]) {
        self.init(name: name, stops: [RampStop(pos: 0, color: low),
                                      RampStop(pos: 1, color: high)])
    }

    enum CodingKeys: String, CodingKey { case name, stops, low, high }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        if let s = try? c.decode([RampStop].self, forKey: .stops),
           s.count >= 2 {
            stops = s
        } else {
            let low = (try? c.decode([Int].self, forKey: .low))
                ?? [30, 30, 60]
            let high = (try? c.decode([Int].self, forKey: .high))
                ?? [240, 200, 120]
            stops = [RampStop(pos: 0, color: low),
                     RampStop(pos: 1, color: high)]
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(name, forKey: .name)
        try c.encode(stops, forKey: .stops)
    }
}

/// What the renderer actually themes with: a builtin or a custom ramp.
enum ThemeChoice: Equatable {
    case builtin(Theme)
    case custom(CustomTheme)
}

enum SingleStyle: String {
    case tank, percent
}

/// How dots/tanks are colored. `identity` is the TUI-matching default;
/// `zone` trades identity for pressure (green→amber→red by context fill);
/// the fun ones remap identity onto a signature ramp (sessions stay
/// distinguishable by where their luma lands on it).
enum Theme: String, CaseIterable {
    case identity, pastel, mono, zone
    case vaporwave, matrix, ember, candy

    var label: String {
        switch self {
        case .identity: return "Identity colors"
        case .pastel: return "Pastel"
        case .mono: return "Monochrome"
        case .zone: return "Pressure zones"
        case .vaporwave: return "Vaporwave"
        case .matrix: return "Matrix"
        case .ember: return "Ember"
        case .candy: return "Candy"
        }
    }
}

struct Settings {
    private static let d = UserDefaults.standard

    static var mode: IconMode {
        get { IconMode(rawValue: d.string(forKey: "mode") ?? "") ?? .grid }
        set { d.set(newValue.rawValue, forKey: "mode") }
    }

    static var singleStyle: SingleStyle {
        get { SingleStyle(rawValue: d.string(forKey: "singleStyle") ?? "") ?? .tank }
        set { d.set(newValue.rawValue, forKey: "singleStyle") }
    }

    /// Session id pinned for single mode; nil = auto (busiest).
    static var pinned: String? {
        get { d.string(forKey: "pinned") }
        set { d.set(newValue, forKey: "pinned") }
    }

    /// Session ids the user unchecked in the grid.
    static var hidden: Set<String> {
        get { Set(d.stringArray(forKey: "hidden") ?? []) }
        set { d.set(Array(newValue).sorted(), forKey: "hidden") }
    }

    static var notifyOnFinish: Bool {
        get { d.bool(forKey: "notifyOnFinish") }
        set { d.set(newValue, forKey: "notifyOnFinish") }
    }

    /// First-launch flag: the app opens the welcome/help tour once.
    static var hasSeenWelcome: Bool {
        get { d.bool(forKey: "hasSeenWelcome") }
        set { d.set(newValue, forKey: "hasSeenWelcome") }
    }

    /// Active theme, raw: a builtin's rawValue, or "custom:<name>".
    static var themeRaw: String {
        get { d.string(forKey: "theme") ?? Theme.identity.rawValue }
        set { d.set(newValue, forKey: "theme") }
    }

    /// Resolved choice; a dangling custom name falls back to identity.
    static var choice: ThemeChoice {
        let raw = themeRaw
        if raw.hasPrefix("custom:") {
            let name = String(raw.dropFirst("custom:".count))
            if let t = customThemes.first(where: { $0.name == name }) {
                return .custom(t)
            }
            return .builtin(.identity)
        }
        return .builtin(Theme(rawValue: raw) ?? .identity)
    }

    /// Sentinel id meaning "keep this node EMPTY" (a deliberate gap in the
    /// bar). It can never collide with a session id.
    static let emptySlot = "-"

    /// Grid slot assignments: 9 entries — a session id (pinned), the
    /// `emptySlot` sentinel (explicit gap), or nil (auto-fill). A pinned
    /// slot shows its session when alive and stays empty when not.
    /// Stored as a JSON array with "" for nil.
    static var slots: [String?] {
        get {
            guard let data = d.data(forKey: "slots"),
                  let raw = try? JSONDecoder().decode([String].self, from: data)
            else { return [String?](repeating: nil, count: 9) }
            var out = raw.map { $0.isEmpty ? nil : Optional($0) }
            while out.count < 9 { out.append(nil) }
            return Array(out.prefix(9))
        }
        set {
            let raw = newValue.prefix(9).map { $0 ?? "" }
            d.set((try? JSONEncoder().encode(Array(raw))) ?? Data(),
                  forKey: "slots")
        }
    }

    static var customThemes: [CustomTheme] {
        get {
            guard let data = d.data(forKey: "customThemes"),
                  let list = try? JSONDecoder().decode([CustomTheme].self,
                                                       from: data)
            else { return [] }
            return list
        }
        set {
            d.set((try? JSONEncoder().encode(newValue)) ?? Data(),
                  forKey: "customThemes")
        }
    }
}
