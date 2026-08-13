// Options ▸ Report a bug… — composes an email to the maintainer in the
// user's default mail client, prefilled with a compact diagnostic block
// (no transcripts, no prompts — versions, counts, and states only).

import AppKit

enum BugReport {
    static let address = "arianshamaei@gmail.com"

    /// The diagnostic block: everything useful, nothing private.
    static func diagnostics(linkUp: Bool, sessions: [FleetSession]) -> String {
        let ver = Bundle.main.infoDictionary?["CFBundleShortVersionString"]
            as? String ?? "dev"
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        let byProvider = Dictionary(grouping: sessions, by: \.provider)
            .map { "\($0.value.count) \($0.key)" }
            .sorted().joined(separator: ", ")
        return """
        --- diagnostics ---
        amtrino \(ver) · \(os)
        engine link: \(linkUp ? "up" : "down")
        sessions: \(sessions.isEmpty ? "none" : byProvider)
        mode: \(Settings.mode.rawValue) · theme: \(Settings.themeRaw)
        pinned slots: \(Settings.slots.compactMap { $0 }.count)
        -------------------

        What happened:

        What I expected:
        """
    }

    /// mailto: URL for the report (pure — selfchecked).
    static func mailtoURL(diag: String) -> URL? {
        var comps = URLComponents()
        comps.scheme = "mailto"
        comps.path = address
        comps.queryItems = [
            URLQueryItem(name: "subject", value: "amtrino bug report"),
            URLQueryItem(name: "body", value: diag),
        ]
        return comps.url
    }

    static func compose(linkUp: Bool, sessions: [FleetSession]) {
        guard let url = mailtoURL(
            diag: diagnostics(linkUp: linkUp, sessions: sessions)) else { return }
        NSWorkspace.shared.open(url)
    }
}
