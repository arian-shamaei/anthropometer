// `amtrino --render-appicon <dir.iconset>` — the app icon, rendered by the
// app's own palette code (no design tool, no drift): the 3×3 session grid on
// a dark rounded square, one dot mid-pulse, one stalled ring, one hollow
// shell — the icon is a screenshot of the idea. build-menubar.sh runs this
// then `iconutil -c icns`.

import AppKit

enum AppIconRenderer {
    /// Fixed identity seeds — chosen once so the icon is stable forever.
    private static let ids = ["mercury", "venus", "earth", "mars", "jupiter",
                              "saturn", "uranus", "neptune", "pluto"]

    static func writeIconset(to dir: String) -> Int32 {
        let fm = FileManager.default
        try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
        // (filename points, scale)
        let entries: [(Int, Int)] = [(16, 1), (16, 2), (32, 1), (32, 2),
                                     (128, 1), (128, 2), (256, 1), (256, 2),
                                     (512, 1), (512, 2)]
        for (pts, scale) in entries {
            let px = pts * scale
            guard let rep = render(px: px),
                  let png = rep.representation(using: .png, properties: [:])
            else {
                print("appicon: render failed at \(px)px")
                return 1
            }
            let name = scale == 1 ? "icon_\(pts)x\(pts).png"
                                  : "icon_\(pts)x\(pts)@2x.png"
            do {
                try png.write(to: URL(fileURLWithPath: dir)
                    .appendingPathComponent(name))
            } catch {
                print("appicon: write \(name) failed: \(error)")
                return 1
            }
        }
        // dots-only transparent layer at 1024 px for the Icon Composer
        // (.icon) document — its background comes from the icon's own fill.
        // Written BESIDE the iconset (iconutil rejects foreign member names).
        if let rep = render(px: 1024, layerOnly: true),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: dir)
                .deletingLastPathComponent()
                .appendingPathComponent("layer_dots.png"))
        }
        print("appicon: wrote \(entries.count) sizes into \(dir)")
        return 0
    }

    private static func render(px: Int, layerOnly: Bool = false) -> NSBitmapImageRep? {
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
            isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0),
            let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        defer { NSGraphicsContext.restoreGraphicsState() }

        let s = CGFloat(px)
        // the macOS icon grid: full-bleed rounded square, ~10% margin,
        // ~22.4% corner radius of the plate. layerOnly = dots on
        // transparency (the .icon document paints its own background).
        let plate = NSRect(x: s * 0.10, y: s * 0.10, width: s * 0.80, height: s * 0.80)
        if !layerOnly {
            let path = NSBezierPath(roundedRect: plate,
                                    xRadius: plate.width * 0.224,
                                    yRadius: plate.width * 0.224)
            // subtle vertical depth on the C_FREE terminal ground
            let bg = NSGradient(
                starting: NSColor(srgbRed: 24 / 255, green: 26 / 255, blue: 33 / 255, alpha: 1),
                ending: NSColor(srgbRed: 44 / 255, green: 48 / 255, blue: 60 / 255, alpha: 1))
            bg?.draw(in: path, angle: 90)
        }

        // Small sizes (banners, notification thumbnails) get a SIMPLER,
        // louder motif: 2×2 giant dots — macOS 26 puts legacy icns behind a
        // glass treatment that washes out fine detail, so the small rungs
        // must shout to read through it.
        if px <= 64 {
            let inset = plate.insetBy(dx: plate.width * 0.10, dy: plate.height * 0.10)
            let cell = inset.width / 2
            let rDot = cell * 0.42
            for i in 0..<4 {
                let col = CGFloat(i % 2), row = CGFloat(i / 2)
                let p = NSPoint(x: inset.minX + col * cell + cell / 2,
                                y: inset.maxY - (row * cell + cell / 2))
                let c = paletteColor(genPalette(seed: sessSeed(ids[i])), 1.0)
                NSColor(srgbRed: min(CGFloat(c.r) / 255 + 0.10, 1),
                        green: min(CGFloat(c.g) / 255 + 0.10, 1),
                        blue: min(CGFloat(c.b) / 255 + 0.10, 1), alpha: 1).setFill()
                NSBezierPath(ovalIn: NSRect(
                    x: p.x - rDot, y: p.y - rDot,
                    width: rDot * 2, height: rDot * 2)).fill()
            }
            return rep
        }

        // 3×3 dots, statuses sampled mid-story: center busy (lifted), one
        // stalled ring, one hollow shell, the rest idle-dim vs bright mix
        let inset = plate.insetBy(dx: plate.width * 0.13, dy: plate.height * 0.13)
        let cell = inset.width / 3
        let rDot = cell * 0.38
        for i in 0..<9 {
            let col = CGFloat(i % 3), row = CGFloat(i / 3)
            let p = NSPoint(x: inset.minX + col * cell + cell / 2,
                            y: inset.maxY - (row * cell + cell / 2))
            let identity = paletteColor(genPalette(seed: sessSeed(ids[i])), 1.0)
            func fill(_ c: RGB, _ k: Double) {
                NSColor(srgbRed: min(CGFloat(c.r) / 255 * k, 1),
                        green: min(CGFloat(c.g) / 255 * k, 1),
                        blue: min(CGFloat(c.b) / 255 * k, 1), alpha: 1).setFill()
            }
            let dot = NSBezierPath(ovalIn: NSRect(
                x: p.x - rDot, y: p.y - rDot, width: rDot * 2, height: rDot * 2))
            switch i {
            case 4: // center: busy at pulse peak — white-lifted
                let c = identity
                NSColor(srgbRed: min(CGFloat(c.r) / 255 + 0.35, 1),
                        green: min(CGFloat(c.g) / 255 + 0.35, 1),
                        blue: min(CGFloat(c.b) / 255 + 0.35, 1), alpha: 1).setFill()
                dot.fill()
            case 5: // shell: hollow ring
                NSColor(srgbRed: CGFloat(identity.r) / 255,
                        green: CGFloat(identity.g) / 255,
                        blue: CGFloat(identity.b) / 255, alpha: 0.9).setStroke()
                dot.lineWidth = max(1, rDot * 0.34)
                dot.stroke()
            case 8: // stalled: dim dot + amber ring
                fill(identity, 0.62)
                dot.fill()
                let ring = NSBezierPath(ovalIn: NSRect(
                    x: p.x - rDot * 1.45, y: p.y - rDot * 1.45,
                    width: rDot * 2.9, height: rDot * 2.9))
                NSColor(srgbRed: 230 / 255, green: 170 / 255, blue: 60 / 255,
                        alpha: 1).setStroke()
                ring.lineWidth = max(1, rDot * 0.30)
                ring.stroke()
            default:
                fill(identity, i % 2 == 0 ? 1.0 : 0.72)
                dot.fill()
            }
        }
        return rep
    }
}
