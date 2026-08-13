// amtrino — menu bar companion for amtr. Accessory app: no dock icon, no
// windows; the status item is the whole surface.

import AppKit

if CommandLine.arguments.contains("--selfcheck") {
    exit(runSelfcheck())
}
if let i = CommandLine.arguments.firstIndex(of: "--render-appicon"),
   i + 1 < CommandLine.arguments.count {
    exit(AppIconRenderer.writeIconset(to: CommandLine.arguments[i + 1]))
}
if CommandLine.arguments.contains("--notify-test") {
    // scriptable probe: report authorization state, post one banner, exit.
    // Must run from inside the .app (UNUserNotificationCenter needs a bundle).
    import_notify_test()
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
