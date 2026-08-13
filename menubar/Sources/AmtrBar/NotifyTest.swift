// `amtrino --notify-test` — prints the OS notification-permission state,
// requests authorization if undetermined, posts one test banner, exits.
// The debugging probe for "the notification feature is not working".

import AppKit
import UserNotifications

func import_notify_test() -> Never {
    guard Bundle.main.bundleIdentifier != nil else {
        print("notify-test: no bundle identifier — run the copy inside "
              + "amtrino.app, not the bare binary")
        exit(1)
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let center = UNUserNotificationCenter.current()

    center.getNotificationSettings { s in
        func name(_ a: UNAuthorizationStatus) -> String {
            switch a {
            case .notDetermined: return "notDetermined"
            case .denied: return "denied"
            case .authorized: return "authorized"
            case .provisional: return "provisional"
            default: return "other(\(a.rawValue))"
            }
        }
        print("notify-test: status=\(name(s.authorizationStatus)) "
              + "alerts=\(s.alertSetting.rawValue) sound=\(s.soundSetting.rawValue)")
        center.requestAuthorization(options: [.alert, .sound]) { ok, err in
            print("notify-test: requestAuthorization ok=\(ok) "
                  + "err=\(err.map(String.init(describing:)) ?? "none")")
            guard ok else { exit(2) }
            let content = UNMutableNotificationContent()
            content.title = "amtrino test"
            content.body = "notifications are working"
            content.sound = .default
            // same identity-dot attachment the real finish banner carries
            if let sess = FleetSession(json: ["id": "notify-test",
                                              "status": "idle", "live": true,
                                              "project": "/test"]) {
                let d = DisplaySession(sess: sess, finishedAgo: 0.3)
                let img = IconRenderer.statusDotImage(d, r: 52, canvas: 128,
                                                      now: Date())
                if let tiff = img.tiffRepresentation,
                   let rep = NSBitmapImageRep(data: tiff),
                   let png = rep.representation(using: .png, properties: [:]) {
                    let url = URL(fileURLWithPath: NSTemporaryDirectory())
                        .appendingPathComponent("amtrino-test-\(UUID().uuidString).png")
                    try? png.write(to: url)
                    if let att = try? UNNotificationAttachment(
                        identifier: "identity", url: url) {
                        content.attachments = [att]
                    }
                }
            }
            // unique id per run: a repeated identifier UPDATES the delivered
            // notification silently instead of presenting a new banner
            let req = UNNotificationRequest(
                identifier: "amtrino-test-\(UUID().uuidString)",
                content: content, trigger: nil)
            center.add(req) { err in
                print("notify-test: post "
                      + (err.map { "failed: \($0)" } ?? "ok"))
                exit(err == nil ? 0 : 3)
            }
        }
    }
    // run the event loop so completion handlers arrive; hard stop at 10 s
    DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
        print("notify-test: timed out")
        exit(4)
    }
    app.run()
    exit(0)
}
