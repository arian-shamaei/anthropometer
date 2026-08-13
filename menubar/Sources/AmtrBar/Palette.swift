// Port of viz.rs gen_palette / sess_seed / palette_color — the SAME
// deterministic per-session gradient the TUI's system-wide wall uses, so a
// session's menu bar tank and its terminal tile are recognizably one thing.
// Every constant here mirrors rust/src/viz.rs; that file is the reference.

import Foundation

typealias RGB = (r: UInt8, g: UInt8, b: UInt8)

/// FNV-1a over the session id (viz.rs sess_seed): deterministic identity.
func sessSeed(_ id: String) -> UInt64 {
    var h: UInt64 = 0xcbf2_9ce4_8422_2325
    for b in id.utf8 {
        h ^= UInt64(b)
        h = h &* 0x0100_0000_01b3
    }
    return h
}

/// splitmix64 stream (viz.rs gen_palette's `unit`).
private struct SplitMix {
    var x: UInt64
    mutating func unit() -> Double {
        x = x &+ 0x9E37_79B9_7F4A_7C15
        var z = x
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        z ^= z >> 31
        return Double(z >> 11) / Double(UInt64(1) << 53)
    }
}

private func remEuclid(_ a: Double, _ m: Double) -> Double {
    let r = a.truncatingRemainder(dividingBy: m)
    return r < 0 ? r + m : r
}

/// Signed shortest-arc delta a → b (degrees, ±180°).
private func signedArc(_ a: Double, _ b: Double) -> Double {
    remEuclid(remEuclid(b - a, 360.0) + 540.0, 360.0) - 180.0
}

/// Oklab → sRGB (Björn Ottosson's matrices); nil if out of gamut.
private func oklabToSrgb(_ l: Double, _ a: Double, _ b: Double) -> RGB? {
    let l_ = l + 0.396_337_777_4 * a + 0.215_803_757_3 * b
    let m_ = l - 0.105_561_345_8 * a - 0.063_854_172_8 * b
    let s_ = l - 0.089_484_177_5 * a - 1.291_485_548_0 * b
    let (l3, m3, s3) = (l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_)
    let r = 4.076_741_662_1 * l3 - 3.307_711_591_3 * m3 + 0.230_969_929_2 * s3
    let g = -1.268_438_004_6 * l3 + 2.609_757_401_1 * m3 - 0.341_319_396_5 * s3
    let bb = -0.004_196_086_3 * l3 - 0.703_418_614_7 * m3 + 1.707_614_701_0 * s3
    func enc(_ v: Double) -> UInt8? {
        if !(v >= -0.001 && v <= 1.001) { return nil }
        let v = min(max(v, 0.0), 1.0)
        let s = v <= 0.003_130_8 ? 12.92 * v : 1.055 * pow(v, 1.0 / 2.4) - 0.055
        return UInt8((s * 255.0).rounded())
    }
    guard let er = enc(r), let eg = enc(g), let eb = enc(bb) else { return nil }
    return (er, eg, eb)
}

/// Max in-gamut chroma at (L, h): binary search against the sRGB walls.
private func gamutCmax(_ l: Double, _ hDeg: Double) -> Double {
    let h = hDeg * .pi / 180.0
    var lo = 0.0, hi = 0.5
    for _ in 0..<24 {
        let mid = 0.5 * (lo + hi)
        if oklabToSrgb(l, mid * cos(h), mid * sin(h)) != nil { lo = mid } else { hi = mid }
    }
    return lo
}

/// OkLCH → sRGB with chroma-decay gamut mapping (drain law lives in L, not C).
private func oklchToSrgb(_ l: Double, _ c: Double, _ hDeg: Double) -> RGB {
    let h = hDeg * .pi / 180.0
    var c = c
    for _ in 0..<32 {
        if let rgb = oklabToSrgb(l, c * cos(h), c * sin(h)) { return rgb }
        c *= 0.90
    }
    return oklabToSrgb(min(max(l, 0.0), 1.0), 0.0, 0.0) ?? (0, 0, 0)
}

/// 4-stop generated tank palette, dark → vivid (viz.rs gen_palette, verbatim).
func genPalette(seed: UInt64) -> [RGB] {
    var rng = SplitMix(x: seed)
    let h0 = rng.unit() * 360.0
    let scheme = rng.unit()
    let dh: Double
    if scheme < 0.40 {
        let gold = 95.0 + (rng.unit() - 0.5) * 30.0
        dh = signedArc(h0, gold) * (0.55 + rng.unit() * 0.45)
    } else if scheme < 0.70 {
        dh = (rng.unit() - 0.5) * 360.0
    } else {
        let dir: Double = rng.unit() < 0.5 ? 1.0 : -1.0
        dh = dir * (150.0 + rng.unit() * 110.0)
    }
    let l0 = 0.20 + rng.unit() * 0.08
    let l3 = 0.80 + rng.unit() * 0.08
    let v0 = 0.55 + rng.unit() * 0.20
    let v3 = 0.85 + rng.unit() * 0.13
    return (0..<4).map { i in
        let t = Double(i) / 3.0
        let l = l0 + (l3 - l0) * t
        let h = h0 + dh * t
        let v = v0 + (v3 - v0) * pow(t, 0.75)
        return oklchToSrgb(l, v * gamutCmax(l, h), h)
    }
}

private func lerpRGB(_ a: RGB, _ b: RGB, _ t: Double) -> RGB {
    func ch(_ x: UInt8, _ y: UInt8) -> UInt8 {
        UInt8((Double(x) + (Double(y) - Double(x)) * t).rounded())
    }
    return (ch(a.r, b.r), ch(a.g, b.g), ch(a.b, b.b))
}

/// Sample a 4-stop palette at t ∈ 0...1 (piecewise-linear, viz.rs).
func paletteColor(_ stops: [RGB], _ t: Double) -> RGB {
    let t = min(max(t, 0.0), 1.0) * 3.0
    let i = min(Int(t), 2)
    return lerpRGB(stops[i], stops[i + 1], t - Double(i))
}

/// Zone color for a fill ratio (fixed 0.60 / 0.85 thresholds — semantic,
/// never themed; exact viz.rs C_GREEN/C_AMBER/C_RED values).
func zoneColor(_ ratio: Double) -> RGB {
    if ratio >= 0.85 { return (230, 85, 85) }
    if ratio >= 0.60 { return (230, 170, 60) }
    return (95, 200, 120)
}

/// viz.rs C_DIM / C_RED — status accents shared with the TUI fleet glyphs.
let cDim: RGB = (95, 95, 108)
let cRed: RGB = (230, 85, 85)
let cAmber: RGB = (230, 170, 60)
let cGreen: RGB = (95, 200, 120)
