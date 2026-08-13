// `amtrino --selfcheck` — the engine's --selftest idiom, no test framework
// needed (Command Line Tools ship neither XCTest nor swift-testing). Exits 0
// iff every assertion holds; CI and packaging run it after every build.
//
// The golden palettes are pinned in BOTH implementations — here and
// rust/src/main.rs `palette_golden_cross_language` — so a session's menu bar
// tank and its TUI tile can never drift apart silently. Update both only on
// a deliberate palette change.

import Foundation

private let goldens: [(UInt64, [[UInt8]])] = [
    (1, [[9, 35, 37], [50, 97, 29], [190, 130, 45], [252, 190, 203]]),
    (42, [[6, 17, 61], [35, 85, 104], [53, 162, 148], [76, 253, 41]]),
    (0xDEAD_BEEF, [[28, 27, 8], [36, 91, 76], [51, 150, 207], [220, 193, 252]]),
    (sessSeed("golden-fixture"),
     [[66, 14, 15], [130, 59, 29], [189, 116, 49], [244, 180, 66]]),
]

func runSelfcheck() -> Int32 {
    var failures = 0
    func check(_ ok: Bool, _ what: String) {
        if ok {
            print("ok   \(what)")
        } else {
            print("FAIL \(what)")
            failures += 1
        }
    }

    // seed: the id → seed step is part of parity
    check(sessSeed("golden-fixture") == 15_870_744_387_491_988_508,
          "sessSeed FNV-1a matches the Rust value")

    // golden palettes (cross-language contract)
    for (seed, want) in goldens {
        let got = genPalette(seed: seed).map { [$0.r, $0.g, $0.b] }
        check(got == want, "gen_palette(\(seed)) matches the Rust golden")
    }

    // drain law: surface outshines the floor for arbitrary seeds
    for seed: UInt64 in [7, 99, 123_456_789] {
        let p = genPalette(seed: seed)
        func luma(_ c: RGB) -> Double {
            0.2126 * Double(c.r) + 0.7152 * Double(c.g) + 0.0722 * Double(c.b)
        }
        check(luma(p[0]) < luma(p[3]), "drain law holds for seed \(seed)")
    }

    // palette sampling endpoints
    let p = genPalette(seed: 7)
    check(paletteColor(p, 0) == p[0] && paletteColor(p, 1) == p[3],
          "paletteColor endpoints")

    // zone thresholds (semantic colors, viz.rs values)
    check(zoneColor(0.30) == (95, 200, 120) && zoneColor(0.70) == (230, 170, 60)
          && zoneColor(0.90) == (230, 85, 85), "zone thresholds 0.60/0.85")

    // wire model: one real-shaped fleet row parses; unknown status survives
    let row: [String: Any] = [
        "id": "ad272663-e78b-413f-a9e9-88f1eded5ad7",
        "path": "/tmp/x.jsonl", "pid": 123, "name": "anthropometer-4b",
        "project": "/Users/x/Developer/anthropometer", "status": "busy",
        "mtime": 1786603239.0, "live": true, "resident": 591_000,
        "budget": 1_000_000, "last_prompt": "build the menu bar",
    ]
    if let s = FleetSession(json: row) {
        check(s.status == .busy && s.fill.map { abs($0 - 0.591) < 0.001 } == true,
              "FleetSession parses status and fill")
    } else {
        check(false, "FleetSession parses status and fill")
    }
    check(FleetSession(json: ["id": "x", "status": "weird"])?.status
          == .unknown("weird"), "unknown status survives (version-drift law)")

    // busy→idle edge produces the finish flash
    let store = FleetStore()
    func mk(_ st: String) -> FleetSession {
        FleetSession(json: ["id": "e", "status": st, "live": true,
                            "project": "/p"])!
    }
    store.apply([mk("busy")])
    store.apply([mk("idle")])
    check(store.displayed(hidden: []).first?.finishedAgo != nil,
          "busy→idle edge flags finishedAgo")

    // stalled→idle is ALSO a finish: long responses go quiet >120 s and get
    // remapped busy→stalled before they complete
    let store2 = FleetStore()
    var fired = false
    store2.onFinished = { _ in fired = true }
    store2.apply([mk("busy")])
    store2.apply([mk("stalled")])
    store2.apply([mk("idle")])
    check(fired && store2.displayed(hidden: []).first?.finishedAgo != nil,
          "stalled→idle edge notifies (the long-response path)")

    // idle→idle and fresh appearance must NOT notify
    let store3 = FleetStore()
    var fired3 = false
    store3.onFinished = { _ in fired3 = true }
    store3.apply([mk("idle")])
    store3.apply([mk("idle")])
    check(!fired3, "no finish fires without a responding→settled edge")

    // grid slot law: pinned session sits in its node; a reserved node whose
    // session is gone stays EMPTY; unpinned nodes auto-fill in stable order
    let slotStore = FleetStore()
    func sess(_ id: String, _ proj: String) -> FleetSession {
        FleetSession(json: ["id": id, "status": "idle", "live": true,
                            "project": proj])!
    }
    slotStore.apply([sess("aa", "/p1"), sess("bb", "/p2"), sess("cc", "/p3")])
    var slotCfg = [String?](repeating: nil, count: 9)
    slotCfg[4] = "bb"      // pin bb to the center node
    slotCfg[8] = "gone"    // reserved for a dead session
    let laid = slotStore.slotted(hidden: [], slots: slotCfg)
    check(laid[4]?.sess.id == "bb", "pinned session sits in its node")
    check(laid[8] == nil, "reserved node with absent session stays empty")
    check(laid[0]?.sess.id == "aa" && laid[1]?.sess.id == "cc",
          "unpinned nodes auto-fill in stable order")
    check(!laid.compactMap { $0?.sess.id }.contains(where: { id in
        laid.compactMap { $0?.sess.id }.filter { $0 == id }.count > 1
    }), "no session occupies two nodes")
    var gapCfg = [String?](repeating: nil, count: 9)
    gapCfg[0] = Settings.emptySlot
    let gapped = slotStore.slotted(hidden: [], slots: gapCfg)
    check(gapped[0] == nil && gapped[1]?.sess.id == "aa",
          "explicit-empty sentinel keeps a node as a gap")

    // accessible identity: contrast law in both bar appearances
    func luma(_ c: RGB) -> Double {
        (0.2126 * Double(c.r) + 0.7152 * Double(c.g)
            + 0.0722 * Double(c.b)) / 255.0
    }
    let wasDark = IconRenderer.barIsDark
    var okDark = true, okLight = true
    for id in ["mercury", "venus", "earth", "mars", "jupiter",
               "saturn", "uranus", "neptune", "pluto"] {
        IconRenderer.barIsDark = true
        if luma(IconRenderer.accessibleIdentity(seed: sessSeed(id))) < 0.44 {
            okDark = false
        }
        IconRenderer.barIsDark = false
        if luma(IconRenderer.accessibleIdentity(seed: sessSeed(id))) > 0.62 {
            okLight = false
        }
    }
    IconRenderer.barIsDark = wasDark
    check(okDark, "identity floors luma on a dark bar")
    check(okLight, "identity caps luma on a light bar")

    // the tank gradient obeys the same law per stop
    IconRenderer.barIsDark = false
    let capped = IconRenderer.adaptStops(
        [(10, 10, 30), (120, 200, 240), (250, 250, 200), (255, 255, 255)])
    check(capped.allSatisfy { luma($0) <= 0.63 },
          "light bar caps every gradient stop")
    IconRenderer.barIsDark = true
    let floored = IconRenderer.adaptStops([(10, 10, 30), (20, 30, 40)])
    check(luma(floored.last!) >= 0.30,
          "dark bar lifts a too-deep surface stop")
    IconRenderer.barIsDark = wasDark

    // custom themes: endpoints, clamping, multi-stop interpolation, and
    // legacy {low, high} decode (saved two-color themes must keep working)
    let ct = CustomTheme(name: "test", low: [10, 20, 30], high: [300, -5, 250])
    check(IconRenderer.customRampColor(ct, 0) == (10, 20, 30)
          && IconRenderer.customRampColor(ct, 1) == (255, 0, 250),
          "gradient endpoints are the chosen colors, clamped")
    let tri = CustomTheme(name: "tri", stops: [
        RampStop(pos: 0, color: [0, 0, 0]),
        RampStop(pos: 0.5, color: [200, 100, 0]),
        RampStop(pos: 1, color: [255, 255, 255]),
    ])
    check(IconRenderer.customRampColor(tri, 0.5) == (200, 100, 0),
          "a middle gradient point is hit exactly")
    check(IconRenderer.customRampColor(tri, 0.25) == (100, 50, 0),
          "between points interpolates linearly")
    let legacyJSON = #"{"name":"old","low":[40,10,60],"high":[255,120,40]}"#
    if let old = try? JSONDecoder().decode(CustomTheme.self,
                                           from: legacyJSON.data(using: .utf8)!) {
        check(old.stops.count == 2 && old.stops[1].rgb == (255, 120, 40),
              "legacy two-color themes decode into gradient points")
    } else {
        check(false, "legacy two-color themes decode into gradient points")
    }

    // tmux locator parsing (notification click → session focus)
    if let t = SessionFocus.parseTmuxTarget("misc:@8.%85") {
        check(t.window == "misc:@8" && t.pane == "%85",
              "tmux locator parses window and pane")
    } else {
        check(false, "tmux locator parses window and pane")
    }
    check(SessionFocus.parseTmuxTarget("v1.2:@3.%7")?.window == "v1.2:@3",
          "dotted session names survive locator parse")
    check(SessionFocus.parseTmuxTarget("plain:@1")?.pane == nil,
          "locator without pane parses window-only")

    // amtr palette-export handshake: parse the SPEC e payload
    let palJSON = #"{"pid":123,"session":"abc","palette":[[9,35,37],[50,97,29],[190,130,45],[252,190,203]]}"#
    if let p = AmtrPalette.parse(palJSON.data(using: .utf8)!) {
        check(p.pid == 123 && p.session == "abc"
              && p.stops[3] == (252, 190, 203),
              "amtr palette export parses")
    } else {
        check(false, "amtr palette export parses")
    }
    check(AmtrPalette.parse(#"{"pid":1,"session":"x","palette":[[1,2]]}"#
        .data(using: .utf8)!) == nil, "malformed palette rejected")

    // bug-report mailto: correct recipient, encodable body
    if let u = BugReport.mailtoURL(diag: "line1\nline2 & <specials>") {
        check(u.scheme == "mailto"
              && u.absoluteString.contains("arianshamaei%40gmail.com")
              || u.absoluteString.contains("arianshamaei@gmail.com"),
              "bug report addresses the maintainer")
    } else {
        check(false, "bug report addresses the maintainer")
    }

    // engine resolution (informational: bundled copy must exist in a build)
    check(FleetClient.enginePath() != nil, "engine copy resolvable")

    print(failures == 0 ? "selfcheck ok" : "selfcheck FAILED (\(failures))")
    return failures == 0 ? 0 : 1
}
