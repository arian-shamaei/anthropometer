//! Pure renderers: `(state, view, Rect, &mut Frame)` in, cells out. No widget
//! state, no channels — everything tests headless (split-process discipline).
//! Every renderer guards degenerate rects. Width-1 glyphs ONLY (blocks,
//! eighths, shade, braille, `▼◆▲✝«»·┃`); no emoji, no wide glyphs.
//! Fixed scales everywhere: MAP rung ladder, EKG y=[0,B], sparklines 0–16k,
//! cost 0–100ku, dur 0–120s. No per-frame autoscale.

use std::collections::HashMap;

use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::symbols::Marker;
use ratatui::text::{Line, Span};
use ratatui::widgets::canvas::{Canvas, Line as CanvasLine, Points};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph};

use crate::ipc::{
    AgentRec, Cat, CAT_ORDER, CmdRec, FileRec, FleetPeekMsg, MapMsg, Op, PeekMsg, RetKind, RetRec,
    Seg, Severity, Tasks,
};
use crate::state::{fmt_age, fmt_dur, fmt_k0, fmt_k1, State};

// ---------------------------------------------------------------------------
// palette (SPEC §e MAP mode 1 — fixed)
// ---------------------------------------------------------------------------

pub const C_OVERHEAD: (u8, u8, u8) = (110, 120, 145); // slate-indigo
pub const C_USER: (u8, u8, u8) = (232, 188, 62); // gold — the human's turns pop
pub const C_ASSIST: (u8, u8, u8) = (95, 200, 120); // green
pub const C_THINK: (u8, u8, u8) = (70, 150, 120); // teal-green
pub const C_BASH: (u8, u8, u8) = (238, 150, 78); // orange
pub const C_TOOL: (u8, u8, u8) = (155, 155, 165); // neutral gray
pub const C_ATTACH: (u8, u8, u8) = (230, 100, 175); // magenta (distinct from violet)
pub const C_SUMMARY: (u8, u8, u8) = (235, 235, 235); // white

pub const C_STEEL: (u8, u8, u8) = (70, 110, 160); // cache-read span
pub const C_CYAN: (u8, u8, u8) = (80, 200, 220); // cache-creation span
pub const C_WLINE: (u8, u8, u8) = (150, 240, 255); // waterline marker (brighter)
pub const C_AMBER: (u8, u8, u8) = (230, 170, 60); // uncached span / amber zone
pub const C_RED: (u8, u8, u8) = (230, 85, 85); // red zone / errors
pub const C_GREEN: (u8, u8, u8) = (95, 200, 120); // green zone / done
pub const C_MAGENTA: (u8, u8, u8) = (220, 90, 220); // compaction markers
pub const C_DIM: (u8, u8, u8) = (95, 95, 108); // hints / labels
pub const C_BG: (u8, u8, u8) = (10, 11, 14); // app background (main.rs BG derives from this)
pub const C_FREE: (u8, u8, u8) = (34, 37, 46); // unused context space (box fill, not black)
pub const C_GRID: (u8, u8, u8) = (58, 58, 66); // rules
pub const C_FG: (u8, u8, u8) = (200, 205, 215); // body text
pub const C_WHITE: (u8, u8, u8) = (245, 245, 245); // pulses

pub const SHADE: [char; 5] = ['·', '░', '▒', '▓', '█'];
pub const SPARK: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
pub const LBLOCK: [char; 8] = ['▏', '▎', '▍', '▌', '▋', '▊', '▉', '█'];

pub fn rgb(c: (u8, u8, u8)) -> Color {
    Color::Rgb(c.0, c.1, c.2)
}
pub fn fg(c: (u8, u8, u8)) -> Style {
    Style::default().fg(rgb(c))
}
pub fn scale(c: (u8, u8, u8), k: f64) -> (u8, u8, u8) {
    let k = k.clamp(0.0, 1.0);
    (
        (c.0 as f64 * k) as u8,
        (c.1 as f64 * k) as u8,
        (c.2 as f64 * k) as u8,
    )
}

/// Linear blend a→b by t ∈ 0..=1 (white-blend pulses).
pub fn lerp(a: (u8, u8, u8), b: (u8, u8, u8), t: f64) -> (u8, u8, u8) {
    let t = t.clamp(0.0, 1.0);
    let mix = |x: u8, y: u8| (x as f64 + (y as f64 - x as f64) * t) as u8;
    (mix(a.0, b.0), mix(a.1, b.1), mix(a.2, b.2))
}

/// Max heat age (s) after which the τ=45 s decay is visually static:
/// `0.70·e^(−dt/45) ≤ 0.05 ⟺ dt ≥ 45·ln(14) ≈ 118.7 s`. ONE shared law:
/// MAP heat mode, FILES NOW hot/cold split, AGENTS running-row glow.
pub const HEAT_STATIC_S: f64 = 118.8;

/// The shared decay-brightness law: `0.30 + 0.70·e^(−age/45 s)`.
pub fn heat_k(age_s: f64) -> f64 {
    0.30 + 0.70 * (-age_s.max(0.0) / 45.0).exp()
}

/// Big-number tank palettes: GENERATED, not stored — same design philosophy
/// as the retired hand-curated art-movement set (4-stop gradient, dark →
/// vivid: the drain law, deep at the far end, luminous at the surface), but
/// algorithmic. Built in OkLCH so every ramp is perceptually even:
///   L (lightness)  ~0.24 → ~0.84  shadow floor → luminous surface
///   C (chroma)     V·C_max(L,h)   spent as a fraction of the sRGB gamut
///     ceiling (V: ~0.65 → ~0.92) so every hue rides near its own wall —
///     absolute-chroma targets read dusty at wide-gamut hues (magenta)
///   h (hue)        h₀ + Δh·t      one signed sweep, three schemes:
///     40% gilt — the surface pulls toward ≈95° (OkLCH gold): yellow is
///       where sRGB has the most brightness headroom, which is why
///       hand-curated art palettes so often end in gilt;
///     30% free swing — Δh uniform in ±180° (the vaporwave outlier);
///     30% long arc — Δh = ±(150°..260°), deliberately the FAR way around
///       the wheel, so the mid stops cross hue families a shortest-arc
///       sweep can never reach (teal → violet → ember).
/// Out-of-gamut stops desaturate toward pastel (the foam / lily-pad endings).
pub fn gen_palette(seed: u64) -> [(u8, u8, u8); 4] {
    // splitmix64 stream: unlike raw xorshift, fully diffuses even small or
    // structured seeds (first draw is the hue — it must be uniform)
    let mut x = seed;
    let mut unit = move || -> f64 {
        x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = x;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        (z >> 11) as f64 / (1u64 << 53) as f64
    };
    let h0 = unit() * 360.0;
    let scheme = unit();
    let dh = if scheme < 0.40 {
        // gilt: signed shortest arc toward gold, strong but not total pull
        let gold = 95.0 + (unit() - 0.5) * 30.0; // 80°..110°
        signed_arc(h0, gold) * (0.55 + unit() * 0.45)
    } else if scheme < 0.70 {
        (unit() - 0.5) * 360.0 // free swing, ±180°
    } else {
        // long arc: the far way around the wheel — mid stops visit hue
        // families no shortest-arc sweep can touch
        let dir = if unit() < 0.5 { 1.0 } else { -1.0 };
        dir * (150.0 + unit() * 110.0) // ±(150°..260°)
    };
    let l0 = 0.20 + unit() * 0.08;
    let l3 = 0.80 + unit() * 0.08;
    // chroma is spent as a FRACTION of the sRGB gamut ceiling at each
    // stop's (L, h) — absolute chroma targets read dusty where the gamut
    // is wide (magenta) and clip where it is narrow (low-L yellow);
    // riding the wall at a fixed fraction is what reads as vivid at
    // every hue
    let v0 = 0.55 + unit() * 0.20; // depth: 55–75% of the wall
    let v3 = 0.85 + unit() * 0.13; // surface: 85–98%
    std::array::from_fn(|i| {
        let t = i as f64 / 3.0;
        let l = l0 + (l3 - l0) * t;
        let h = h0 + dh * t;
        // chroma arrives ahead of lightness (t^0.75): the mid stops carry
        // the color, the way the curated sets did
        let v = v0 + (v3 - v0) * t.powf(0.75);
        oklch_to_srgb(l, v * gamut_cmax(l, h), h)
    })
}

/// Max in-gamut chroma at (L, h): binary search against the sRGB walls.
fn gamut_cmax(l: f64, h_deg: f64) -> f64 {
    let h = h_deg.to_radians();
    let (mut lo, mut hi) = (0.0_f64, 0.5_f64);
    for _ in 0..24 {
        let mid = 0.5 * (lo + hi);
        if oklab_to_srgb(l, mid * h.cos(), mid * h.sin()).is_some() {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    lo
}

/// Signed shortest-arc delta a → b (degrees, in ±180°).
fn signed_arc(a: f64, b: f64) -> f64 {
    ((b - a).rem_euclid(360.0) + 540.0).rem_euclid(360.0) - 180.0
}

/// OkLCH → sRGB. Gamut mapping = chroma decay: shave saturation, keep the
/// lightness ramp intact (the drain law lives in L, not C).
fn oklch_to_srgb(l: f64, c: f64, h_deg: f64) -> (u8, u8, u8) {
    let h = h_deg.to_radians();
    let mut c = c;
    for _ in 0..32 {
        if let Some(rgb) = oklab_to_srgb(l, c * h.cos(), c * h.sin()) {
            return rgb;
        }
        c *= 0.90;
    }
    oklab_to_srgb(l.clamp(0.0, 1.0), 0.0, 0.0).unwrap_or((0, 0, 0))
}

/// Oklab → sRGB (Björn Ottosson's reference matrices); None if out of gamut.
fn oklab_to_srgb(l: f64, a: f64, b: f64) -> Option<(u8, u8, u8)> {
    let l_ = l + 0.396_337_777_4 * a + 0.215_803_757_3 * b;
    let m_ = l - 0.105_561_345_8 * a - 0.063_854_172_8 * b;
    let s_ = l - 0.089_484_177_5 * a - 1.291_485_548_0 * b;
    let (l3, m3, s3) = (l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_);
    let r = 4.076_741_662_1 * l3 - 3.307_711_591_3 * m3 + 0.230_969_929_2 * s3;
    let g = -1.268_438_004_6 * l3 + 2.609_757_401_1 * m3 - 0.341_319_396_5 * s3;
    let b = -0.004_196_086_3 * l3 - 0.703_418_614_7 * m3 + 1.707_614_701_0 * s3;
    let enc = |v: f64| -> Option<u8> {
        if !(-0.001..=1.001).contains(&v) {
            return None;
        }
        let v = v.clamp(0.0, 1.0);
        let s = if v <= 0.003_130_8 {
            12.92 * v
        } else {
            1.055 * v.powf(1.0 / 2.4) - 0.055
        };
        Some((s * 255.0).round() as u8)
    };
    Some((enc(r)?, enc(g)?, enc(b)?))
}

/// Reroll counter for the big-view tank palette (space steps it forward).
static PAL_ROLL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Advance to the next generated tank palette.
pub fn reroll_palette() {
    PAL_ROLL.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
}

/// The current tank palette: one time-based seed rolled per process (the
/// only randomness in the renderer — deterministic between rerolls), offset
/// by the reroll counter so space steps to a fresh palette.
pub fn art_palette() -> [(u8, u8, u8); 4] {
    static BASE: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    let base = *BASE.get_or_init(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9E37_79B9_7F4A_7C15)
    });
    gen_palette(base.wrapping_add(PAL_ROLL.load(std::sync::atomic::Ordering::Relaxed)))
}

/// The palette-export payload (SPEC e): pure, testable.
pub fn palette_export_json(session_id: &str, pal: &[(u8, u8, u8); 4]) -> String {
    let stops: Vec<String> = pal
        .iter()
        .map(|(r, g, b)| format!("[{r},{g},{b}]"))
        .collect();
    format!(
        r#"{{"pid":{},"session":"{}","palette":[{}]}}"#,
        std::process::id(),
        session_id,
        stops.join(",")
    )
}

/// SPEC e (palette export): companion front ends (the amtrino menu bar app)
/// match their single-session gradient to the TUI's big-gauge tank. Written
/// on attach and on reroll, atomic tmp+rename, last-writer-wins across
/// instances; consumers honor it only while `pid` is alive and `session`
/// matches theirs.
/// amtr's per-user state directory (`~/.claude/amtr`): the palette export
/// and the first-launch marker live here. None when HOME is unset.
pub fn state_dir() -> Option<std::path::PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(std::path::Path::new(&home).join(".claude").join("amtr"))
}

pub fn export_palette(session_id: &str) {
    let json = palette_export_json(session_id, &art_palette());
    let Some(dir) = state_dir() else { return };
    let _ = std::fs::create_dir_all(&dir);
    let tmp = dir.join(".palette.json.tmp");
    if std::fs::write(&tmp, json).is_ok() {
        let _ = std::fs::rename(&tmp, dir.join("palette.json"));
    }
}

/// Sample a 4-stop palette at t ∈ 0..=1 (piecewise-linear).
pub fn palette_color(stops: &[(u8, u8, u8); 4], t: f64) -> (u8, u8, u8) {
    let t = t.clamp(0.0, 1.0) * 3.0;
    let i = (t.floor() as usize).min(2);
    lerp(stops[i], stops[i + 1], t - i as f64)
}

/// Zone color for a fill ratio (fixed 0.60 / 0.85 thresholds).
pub fn zone_color(ratio: f64) -> (u8, u8, u8) {
    if ratio >= 0.85 {
        C_RED
    } else if ratio >= 0.60 {
        C_AMBER
    } else {
        C_GREEN
    }
}

// ---------------------------------------------------------------------------
// block themes — class-mode identity colors (categories + the file wheel)
// ---------------------------------------------------------------------------

/// A complete class-mode identity palette: the who-speaks colors for MAP
/// blocks plus the 8-hue per-file accent wheel. Semantic colors (pressure
/// zones, alerts, turn-stack cache tiers) are NOT themed — they carry
/// meaning, not style. Every shipped theme must clear the colorblind floors
/// enforced by the `theme_cvd_floors` test (min CIE76 dE between every
/// cross-family pair under normal vision, protanopia and deuteranopia —
/// Machado 2009 severity-1.0 matrices; wheel-vs-wheel pairs are exempt,
/// files are one family by design).
pub struct ClassTheme {
    pub name: &'static str,
    pub overhead: (u8, u8, u8),
    pub user: (u8, u8, u8),
    pub assist: (u8, u8, u8),
    pub think: (u8, u8, u8),
    pub reason: (u8, u8, u8),
    pub bash: (u8, u8, u8),
    pub tool: (u8, u8, u8),
    pub attach: (u8, u8, u8),
    pub summary: (u8, u8, u8),
    pub wheel: [(u8, u8, u8); crate::state::FILE_WHEEL_LEN],
}

/// Shipped themes, `t` cycles in listed order; index 0 is the default.
pub static THEMES: [ClassTheme; 4] = [
    // gruvbox-flavored: blends into a gruvbox terminal. reason/attach are
    // gruvbox-adjacent rather than canon — the canonical purples collapse
    // into the teal wheel for dichromats, so they were re-tuned outward.
    ClassTheme {
        name: "terminal",
        overhead: (102, 92, 84),
        user: (250, 189, 47),
        assist: (142, 192, 124),
        think: (96, 130, 70),
        reason: (236, 116, 230),
        bash: (214, 93, 14),
        tool: (168, 153, 132),
        attach: (110, 24, 178),
        summary: (235, 219, 178),
        wheel: [
            (69, 133, 136), (138, 176, 170), (88, 158, 180), (58, 110, 158),
            (110, 186, 202), (150, 200, 214), (80, 140, 196), (46, 142, 150),
        ],
    },
    // woodblock inks: gold-leaf human, spring-green voice, pine thought,
    // murasaki hidden reasoning, vermillion shell, water-family files
    ClassTheme {
        name: "ukiyo-e",
        overhead: (98, 110, 152),
        user: (244, 198, 56),
        assist: (108, 206, 118),
        think: (24, 104, 88),
        reason: (178, 150, 255),
        bash: (198, 76, 44),
        tool: (158, 158, 166),
        attach: (150, 40, 230),
        summary: (238, 233, 220),
        wheel: [
            (64, 202, 226), (168, 200, 254), (168, 208, 214), (8, 240, 246),
            (120, 176, 206), (144, 240, 246), (152, 224, 254), (88, 216, 214),
        ],
    },
    // primary-triad discipline: cadmium yellow, vermillion, cobalt wheel
    ClassTheme {
        name: "bauhaus",
        overhead: (86, 92, 110),
        user: (248, 198, 40),
        assist: (118, 184, 92),
        think: (62, 108, 80),
        reason: (214, 190, 216),
        bash: (204, 60, 38),
        tool: (154, 150, 142),
        attach: (90, 16, 216),
        summary: (240, 236, 226),
        wheel: [
            (56, 110, 214), (28, 150, 220), (96, 164, 248), (20, 88, 180),
            (84, 192, 238), (130, 182, 255), (44, 170, 208), (104, 140, 244),
        ],
    },
    // luminance IS the encoding (hue only whispers: warm = human, cool =
    // files) — never degrades for any color vision, works on e-ink; the
    // ladder is inherently tighter than the color themes' floors
    ClassTheme {
        name: "mono",
        overhead: (84, 86, 96),
        user: (240, 226, 190),
        assist: (212, 214, 222),
        think: (108, 110, 120),
        reason: (186, 188, 198),
        bash: (134, 136, 146),
        tool: (160, 162, 172),
        attach: (52, 54, 62),
        summary: (252, 252, 252),
        wheel: [
            (96, 110, 134), (122, 138, 164), (150, 168, 196), (180, 198, 226),
            (96, 110, 134), (122, 138, 164), (150, 168, 196), (180, 198, 226),
        ],
    },
];

static THEME_IDX: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

pub fn theme() -> &'static ClassTheme {
    &THEMES[THEME_IDX.load(std::sync::atomic::Ordering::Relaxed) % THEMES.len()]
}

/// `t`: advance to the next theme; returns the new theme's name for the log.
pub fn cycle_theme() -> &'static str {
    let i = THEME_IDX.load(std::sync::atomic::Ordering::Relaxed);
    THEME_IDX.store((i + 1) % THEMES.len(), std::sync::atomic::Ordering::Relaxed);
    theme().name
}

/// `--theme <name>`: pin a theme at launch. False if the name is unknown.
pub fn set_theme(name: &str) -> bool {
    match THEMES.iter().position(|t| t.name == name) {
        Some(i) => {
            THEME_IDX.store(i, std::sync::atomic::Ordering::Relaxed);
            true
        }
        None => false,
    }
}

/// The file's accent color: its stable wheel index, colored by the theme.
pub fn hue_of(st: &State, id: u64) -> (u8, u8, u8) {
    theme().wheel[st.hue_idx(id)]
}

pub fn cat_color(cat: Cat) -> (u8, u8, u8) {
    let t = theme();
    match cat {
        Cat::Overhead => t.overhead,
        Cat::User => t.user,
        Cat::Assistant => t.assist,
        Cat::Thinking => t.think,
        // hidden reasoning: the visible trace of what the transcript
        // cannot show — each theme gives it its own off-family accent
        Cat::Reasoning => t.reason,
        Cat::File => t.wheel[0],
        Cat::Bash => t.bash,
        Cat::Tool => t.tool,
        Cat::Attach => t.attach,
        Cat::Summary => t.summary,
        Cat::Unknown => C_DIM,
    }
}

fn seg_color(st: &State, sg: &Seg) -> (u8, u8, u8) {
    match sg.file {
        Some(fid) if sg.cat == Cat::File => hue_of(st, fid),
        _ => cat_color(sg.cat),
    }
}

/// Half-block compose: one char cell from top/bottom half colors
/// (`▀` fg-top/bg-bottom — the WEAVE idiom).
pub fn compose(top: Option<Color>, bot: Option<Color>) -> Span<'static> {
    match (top, bot) {
        (Some(t), Some(b)) if t == b => Span::styled("█", Style::default().fg(t)),
        (Some(t), Some(b)) => Span::styled("▀", Style::default().fg(t).bg(b)),
        (Some(t), None) => Span::styled("▀", Style::default().fg(t)),
        (None, Some(b)) => Span::styled("▄", Style::default().fg(b)),
        (None, None) => Span::raw(" "),
    }
}

/// Smooth 1/8-cell horizontal bar over `width` cells, `frac` ∈ 0..=1.
pub fn eighth_bar(frac: f64, width: usize) -> String {
    const EIGHTHS: [char; 8] = ['▏', '▎', '▍', '▌', '▋', '▊', '▉', '█'];
    let cells = frac.clamp(0.0, 1.0) * width as f64;
    let full = cells.floor() as usize;
    let rem = ((cells - full as f64) * 8.0).round() as usize;
    let mut s = "█".repeat(full.min(width));
    if rem > 0 && full < width {
        s.push(EIGHTHS[rem - 1]);
    }
    s
}

fn spark_char(v: f64, max: f64) -> char {
    if max <= 0.0 {
        return SPARK[0];
    }
    let idx = ((v / max).clamp(0.0, 0.999) * 8.0) as usize;
    SPARK[idx]
}

/// Right-truncate a path to `w` cells keeping the tail (`…tail/of/path`).
pub fn tail_trunc(s: &str, w: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= w {
        return s.to_string();
    }
    if w == 0 {
        return String::new();
    }
    let tail: String = chars[chars.len() - (w - 1)..].iter().collect();
    format!("…{tail}")
}

// ---------------------------------------------------------------------------
// view parameters passed from the App (pure inputs)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum MapMode {
    Class,
    Heat,
    Age,
    Cache,
}

impl MapMode {
    pub fn next(self) -> Self {
        match self {
            MapMode::Class => MapMode::Heat,
            MapMode::Heat => MapMode::Age,
            MapMode::Age => MapMode::Cache,
            MapMode::Cache => MapMode::Class,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            MapMode::Class => "class",
            MapMode::Heat => "heat",
            MapMode::Age => "age",
            MapMode::Cache => "cache",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Full,    // ≥110×30
    Medium,  // ≥80×24
    Compact, // ≥50×15
}

/// Everything the renderers need from the App beyond `State`.
pub struct Ui {
    pub now_epoch: f64,
    pub blink: bool,
    pub cursor: Option<u64>, // turn cursor; None = live
    pub sel_file: Option<u64>,
    /// INSPECT-mode segment selection: the walked seg's id (None when
    /// INSPECT is off). Renders inverse on the MAP in every mode, same
    /// precedence as the file cross-link (SPEC §e INSPECT).
    pub sel_seg: Option<u64>,
    /// hold the INSPECT spotlight steady (peek overlay open — the reader is
    /// on the text, a breathing map behind it is noise)
    pub spotlight_static: bool,
    /// pack the MAP to RESIDENT content (no free-space dot field) — the
    /// OVERVIEW gauge already shows fullness, so the map is all colour
    pub map_pack: bool,
    pub map_mode: MapMode,
    pub rung_override: i8,
    pub tier: Tier,
    /// waterline / cc effective at the viewed turn (cursor-aware)
    pub wl: u64,
    pub cc: u64,
}

// effective (replay-aware) data selectors — snapshot re-renders
// MAP/FILES/cats/AGENTS/tasks at that turn (SPEC §e Replay)
pub fn eff_segs(st: &State) -> &[Seg] {
    st.replay
        .as_ref()
        .map(|r| r.map.segs.as_slice())
        .unwrap_or(&st.segs)
}
pub fn eff_alpha(st: &State) -> f64 {
    st.replay.as_ref().map(|r| r.map.alpha).unwrap_or(st.alpha)
}
pub fn eff_cats(st: &State) -> &HashMap<String, u64> {
    st.replay.as_ref().map(|r| &r.cats).unwrap_or(&st.cats)
}
pub fn eff_agents(st: &State) -> &[AgentRec] {
    st.replay
        .as_ref()
        .map(|r| r.agents.as_slice())
        .unwrap_or(&st.agents)
}
pub fn eff_tasks(st: &State) -> Option<&Tasks> {
    match &st.replay {
        Some(r) => Some(&r.tasks),
        None => st.tasks.as_ref(),
    }
}
pub fn eff_files(st: &State) -> Vec<&FileRec> {
    match &st.replay {
        Some(r) => r.files.iter().collect(),
        None => st.files.values().collect(),
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum FileSort {
    Size,
    Recent,
    Churn,
    Name,
}

impl FileSort {
    pub fn next(self) -> Self {
        match self {
            FileSort::Size => FileSort::Recent,
            FileSort::Recent => FileSort::Churn,
            FileSort::Churn => FileSort::Name,
            FileSort::Name => FileSort::Size,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            FileSort::Size => "size",
            FileSort::Recent => "recent",
            FileSort::Churn => "churn",
            FileSort::Name => "name",
        }
    }
}

/// FILES tab perspective: HISTORY (roll+table, default) ↔ NOW (`v` toggles).
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum FilesView {
    History,
    Now,
}

/// Sorted file ids for the FILES tab (table order == roll order == selection).
pub fn file_order(st: &State, sort: FileSort) -> Vec<u64> {
    let mut files = eff_files(st);
    match sort {
        FileSort::Size => files.sort_by(|a, b| b.tok.cmp(&a.tok).then(a.path.cmp(&b.path))),
        FileSort::Recent => files.sort_by(|a, b| b.last_ts.cmp(&a.last_ts).then(a.path.cmp(&b.path))),
        FileSort::Churn => files.sort_by(|a, b| b.waste.cmp(&a.waste).then(a.path.cmp(&b.path))),
        FileSort::Name => files.sort_by(|a, b| a.path.cmp(&b.path)),
    }
    files.iter().map(|f| f.id).collect()
}

// ---------------------------------------------------------------------------
// MAP (OVERVIEW top pane)
// ---------------------------------------------------------------------------

/// Fixed cell-size ladder in tokens (SPEC §e MAP).
pub const RUNGS: [u64; 11] = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384];

fn rung_label(s: u64) -> String {
    if s < 1024 {
        format!("▪={s}")
    } else {
        format!("▪={}k", s / 1024)
    }
}

/// Pick the smallest rung whose cell count fits `capacity`; apply the manual
/// `+/-` override (clamped to the ladder). Pure function of B + pane size.
pub fn auto_rung_idx(budget: u64, capacity: usize) -> usize {
    let mut idx = RUNGS.len() - 1;
    for (i, &s) in RUNGS.iter().enumerate() {
        if budget.div_ceil(s) as usize <= capacity.max(1) {
            idx = i;
            break;
        }
    }
    idx
}

pub fn pick_rung(budget: u64, capacity: usize, over: i8) -> u64 {
    let idx = auto_rung_idx(budget, capacity);
    let idx = (idx as i32 + over as i32).clamp(0, RUNGS.len() as i32 - 1) as usize;
    RUNGS[idx]
}

/// The MAP's cell capacity for a pane of `width`×`h` char cells.
pub fn map_capacity(width: u16, h: u16, full_cell: bool) -> usize {
    if width <= MAP_GUTTER || h == 0 {
        return 1;
    }
    let w = (width - MAP_GUTTER) as usize;
    let logical = if full_cell { h as usize } else { 2 * h as usize };
    w * logical.max(1)
}

/// Char rows the MAP actually needs to show the full budget at the rung it
/// would pick given `max_h` rows (SPEC e: pane height = min(60%, needed)).
pub fn map_rows_needed(st: &State, ui: &Ui, width: u16, max_h: u16) -> u16 {
    if width <= MAP_GUTTER || max_h == 0 {
        return max_h;
    }
    let w = (width - MAP_GUTTER) as usize;
    let full_cell = ui.map_mode == MapMode::Age;
    let capacity = map_capacity(width, max_h, full_cell);
    let s = pick_rung(st.budget.max(1), capacity, ui.rung_override);
    let n = (st.budget.max(1).div_ceil(s) as usize).min(capacity);
    let lrows = n.div_ceil(w).max(1);
    let rows = if full_cell { lrows } else { lrows.div_ceil(2) };
    (rows as u16).min(max_h)
}

// The map's scale labels (mode · rung · alpha) moved to a horizontal header
// line above the box (render_map_header), so the map now uses the FULL width —
// no left gutter.
const MAP_GUTTER: u16 = 0;

/// plurality owner per cell: seg index (into `segs`) or None (free)
fn cell_owners(segs: &[Seg], s_tok: u64, n_cells: usize) -> Vec<Option<usize>> {
    let mut bounds: Vec<(u64, u64, usize)> = Vec::with_capacity(segs.len());
    let mut acc = 0u64;
    for (i, sg) in segs.iter().enumerate() {
        let st = acc;
        acc = acc.saturating_add(sg.tok);
        if sg.tok > 0 {
            bounds.push((st, acc, i));
        }
    }
    let mut out = vec![None; n_cells];
    let mut p = 0usize;
    for (ci, slot) in out.iter_mut().enumerate() {
        let a = ci as u64 * s_tok;
        let b = a + s_tok;
        while p < bounds.len() && bounds[p].1 <= a {
            p += 1;
        }
        let mut best: Option<(u64, usize)> = None;
        let mut q = p;
        while q < bounds.len() && bounds[q].0 < b {
            let ov = bounds[q].1.min(b) - bounds[q].0.max(a);
            if best.map(|(bo, _)| ov > bo).unwrap_or(true) {
                best = Some((ov, bounds[q].2));
            }
            q += 1;
        }
        *slot = best.map(|(_, i)| i);
    }
    out
}

/// per-logical-cell render decision
#[derive(Clone, Copy)]
struct CellPx {
    color: Option<Color>,
    reversed: bool,
}

/// Color one logical cell for the current mode + pulse overrides.
#[allow(clippy::too_many_arguments)]
fn cell_px(
    st: &State,
    ui: &Ui,
    segs: &[Seg],
    owner: Option<usize>,
    addr: u64,
    s_tok: u64,
    total: u64,
) -> CellPx {
    let reversed = false; // never reverse a solid map cell — it renders black
    let mut color: Option<Color> = None;
    if let Some(oi) = owner {
        let sg = &segs[oi];
        let base = match ui.map_mode {
            MapMode::Class => seg_color(st, sg),
            MapMode::Heat => {
                let dt = (ui.now_epoch - sg.ts).max(0.0);
                let b = 0.30 + 0.70 * (-dt / 45.0).exp();
                scale((255, 160, 60), b)
            }
            MapMode::Cache => {
                let mid = addr + s_tok / 2;
                if mid < ui.wl {
                    C_STEEL
                } else if mid < ui.wl.saturating_add(ui.cc) {
                    C_CYAN
                } else {
                    C_AMBER
                }
            }
            MapMode::Age => {
                // handled by glyph ramp in the full-cell path; color = white
                C_SUMMARY
            }
        };
        color = Some(rgb(base));
        // selection highlights (every mode): FILES cross-link · INSPECT walk.
        // The INSPECT walk ANIMATES on the blink clock — a static inversion
        // vanishes into a dense map; a white↔inverted breath does not.
        if ui.sel_seg == Some(sg.id) {
            // breathing spotlight: white blaze <-> deep dim. NOT reverse —
            // fg/bg swap is a visual no-op on a solid single-color chunk
            // (the walked segment usually is one), which made the off-phase
            // invisible in the field.
            color = Some(if ui.blink || ui.spotlight_static {
                rgb(C_WHITE)
            } else {
                rgb(scale(base, 0.40))
            });
        } else if ui.sel_seg.is_none() && ui.sel_file.is_some() && sg.file == ui.sel_file {
            // FILES cross-link: brighten the cell toward white. Reverse-video on
            // a solid "█" cell (t==b) paints the glyph in the terminal's default
            // background — i.e. a BLACK square (field-found glitch: appears in
            // OVERVIEW after a file is picked, since INSPECT suppresses this).
            color = Some(rgb(lerp(base, C_WHITE, 0.6)));
        }
    } else if addr < total {
        // a zero-token hole inside the resident span (shouldn't happen)
        color = Some(rgb(C_GRID));
    }
    // pulse overrides (all modes)
    if st.compact_sweep > 0 && color.is_some() {
        color = Some(rgb(C_GRID)); // 3-frame dim sweep
    }
    if st.thrash_pulse > 0 && st.thrash_pulse % 2 == 0 {
        let (lo, hi) = st.thrash_span;
        if addr + s_tok > lo && addr < hi {
            color = Some(rgb(C_RED));
        }
    }
    if st.write_pulse > 0 && st.write_pulse % 2 == 0 {
        let (lo, hi) = st.write_span;
        if addr + s_tok > lo && addr < hi {
            color = Some(rgb(C_WHITE));
        }
    }
    // waterline marker: the cell containing address C — BLINKS RED in every
    // mode. The cache boundary is the most consequential address in the window
    // (everything before it is cache-served, everything after is re-billed), so
    // it pulses instead of sitting quietly in one colour.
    // CLAMPED to the drawn content: segs and the turn's waterline arrive as
    // separate messages, so mid-update wl can briefly exceed the laid segments.
    // Unclamped it painted a lone marker floating out in free space (field-
    // reported). The boundary can never sit past the content that exists.
    let wl_addr = ui.wl.min(total.saturating_sub(1));
    if total > 0 && ui.wl > 0 && wl_addr >= addr && wl_addr < addr + s_tok {
        color = Some(rgb(if ui.blink {
            lerp(C_RED, C_WHITE, 0.35)
        } else {
            scale(C_RED, 0.50)
        }));
    }
    CellPx { color, reversed }
}

/// Row-major memory map, half-block cells (age mode: full-cell shade ramp).
pub fn render_map(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width <= MAP_GUTTER + 1 || area.height == 0 {
        return;
    }
    let segs = eff_segs(st);
    let w = (area.width - MAP_GUTTER) as usize;
    let h = area.height as usize;
    let full_cell = ui.map_mode == MapMode::Age;
    let logical_rows = if full_cell { h } else { 2 * h };
    let capacity = w * logical_rows;
    // pack to resident content (all colour, no headroom dots) when the gauge
    // carries fullness; else size to the budget (dots = free headroom)
    let total: u64 = segs.iter().map(|s| s.tok).sum();
    let (s_tok, n_cells) = if ui.map_pack {
        // pack resident content to FILL the pane (no dots, no gap): pick the
        // cell size that spreads R across ~all cells, off the rung ladder
        let scale = total.max(1);
        let s = (scale as f64 / capacity.max(1) as f64).ceil().max(1.0) as u64;
        (s, (scale.div_ceil(s) as usize).min(capacity))
    } else {
        let scale = st.budget.max(1);
        let s = pick_rung(scale, capacity, ui.rung_override);
        (s, (scale.div_ceil(s) as usize).min(capacity))
    };
    let mut owners = cell_owners(segs, s_tok, n_cells);
    // INSPECT: a sub-cell chunk never wins plurality ownership, leaving the
    // spotlight with nothing to animate (field-found via tmux capture). The
    // walked segment always claims the cell at its midpoint address.
    if let Some(sel) = ui.sel_seg {
        let mut addr = 0u64;
        for (i, sg) in segs.iter().enumerate() {
            if sg.id == sel {
                let ci = ((addr + sg.tok / 2) / s_tok) as usize;
                if ci < n_cells {
                    owners[ci] = Some(i);
                }
                break;
            }
            addr += sg.tok;
        }
    }
    // age is measured against the EFFECTIVE turn: in replay the snapshot's
    // segments must age relative to the cursor, not the live turn count
    let turns_now = ui.cursor.map(|c| c + 1).unwrap_or_else(|| st.turn_count());

    // scale labels (mode · rung · alpha) render in a header line above the map
    // (render_map_header); the map itself is now full-width, no left gutter.
    // The cell grid itself is drawn by the shared core so the per-agent
    // mini-maps (AGENTS tab) render byte-identical cells.
    render_map_cells(
        f,
        area,
        segs,
        s_tok,
        n_cells,
        &owners,
        full_cell,
        turns_now,
        |owner, addr| cell_px(st, ui, segs, owner, addr, s_tok, total),
    );
}

/// Shared cell-drawing core for the OVERVIEW map AND the per-agent mini-maps
/// (SPEC b `agent.map`). Given pre-sized cells (`s_tok`, `n_cells`, `owners`)
/// and a per-cell color resolver `px(owner, addr)`, it paints the row-major
/// half-block grid (or the full-cell age ramp) into `area`. The OVERVIEW map
/// passes a resolver that calls `cell_px` (pulses, waterline, INSPECT); a
/// mini-map passes a plain class resolver. Cell LOGIC lives here once.
#[allow(clippy::too_many_arguments)]
fn render_map_cells<F>(
    f: &mut Frame<'_>,
    area: Rect,
    segs: &[Seg],
    s_tok: u64,
    n_cells: usize,
    owners: &[Option<usize>],
    full_cell: bool,
    turns_now: u64,
    px: F,
) where
    F: Fn(Option<usize>, u64) -> CellPx,
{
    if area.width == 0 || area.height == 0 {
        return;
    }
    let w = area.width as usize;
    let h = area.height as usize;
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(h);
    for row in 0..h {
        let mut spans: Vec<Span<'static>> = Vec::with_capacity(w + 1);
        if full_cell {
            for col in 0..w {
                let ci = row * w + col;
                if ci >= n_cells {
                    // past the budget — not context space; background, so the
                    // box's area IS the budget and dim = free headroom only
                    spans.push(Span::raw(" "));
                    continue;
                }
                let addr = ci as u64 * s_tok;
                match owners[ci] {
                    Some(oi) => {
                        let sg = &segs[oi];
                        let age = turns_now.saturating_sub(sg.born + 1);
                        let bucket = if age < 1 {
                            0
                        } else if age < 4 {
                            1
                        } else if age < 16 {
                            2
                        } else if age < 64 {
                            3
                        } else {
                            4
                        };
                        let cpx = px(Some(oi), addr);
                        let glyph = SHADE[4 - bucket]; // newest brightest
                        let mut style =
                            Style::default().fg(cpx.color.unwrap_or(rgb(C_DIM)));
                        if cpx.reversed {
                            style = style.add_modifier(Modifier::REVERSED);
                        }
                        spans.push(Span::styled(glyph.to_string(), style));
                    }
                    // free/unowned cell → solid dim fill (the box is a clean
                    // rectangle: bright = used, dim = free; never a black gap)
                    None => spans.push(Span::styled("█", fg(C_FREE))),
                }
            }
        } else {
            let (lt, lb) = (2 * row, 2 * row + 1);
            for col in 0..w {
                let (ci_t, ci_b) = (lt * w + col, lb * w + col);
                let px_of = |ci: usize| -> Option<CellPx> {
                    if ci >= n_cells {
                        return None;
                    }
                    let addr = ci as u64 * s_tok;
                    Some(px(owners[ci], addr))
                };
                let (pt, pb) = (px_of(ci_t), px_of(ci_b));
                // A half PAST the budget (px_of == None) is not context space at
                // all — leave it as background so the box ends exactly at B. Only
                // in-budget unowned halves get the dim free-headroom fill. (Both
                // used to paint C_FREE, which drew a box bigger than the budget
                // and made a 97%-full window look far emptier than it is.)
                let (tc, bc) = (
                    pt.map(|p| p.color.unwrap_or(rgb(C_FREE))),
                    pb.map(|p| p.color.unwrap_or(rgb(C_FREE))),
                );
                if tc.is_none() && bc.is_none() {
                    spans.push(Span::raw(" ")); // wholly beyond the budget
                    continue;
                }
                if tc == Some(rgb(C_FREE)) && bc == Some(rgb(C_FREE)) {
                    // fully-free cell → solid dim block; no black gaps, no dots
                    spans.push(Span::styled("█", fg(C_FREE)));
                    continue;
                }
                let mut span = compose(tc, bc);
                let rev = pt.map(|p| p.reversed).unwrap_or(false)
                    || pb.map(|p| p.reversed).unwrap_or(false);
                if rev {
                    span = span.patch_style(Style::default().add_modifier(Modifier::REVERSED));
                }
                spans.push(span);
            }
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// Plain class-mode color for a per-agent mini-map cell: no waterline, no
/// pulses, no cross-session selection (the agent's own `file` ids are local
/// to its Session, so file segs index the theme wheel LOCALLY — never the
/// main UI's `hue_of`, which would map to an unrelated file).
fn agent_cell_px(segs: &[Seg], owner: Option<usize>) -> CellPx {
    let color = owner.map(|oi| {
        let sg = &segs[oi];
        let wheel = &theme().wheel;
        let base = match sg.file {
            Some(fid) if sg.cat == Cat::File => wheel[(fid as usize) % wheel.len()],
            _ => cat_color(sg.cat),
        };
        rgb(base)
    });
    CellPx {
        color,
        reversed: false,
    }
}

/// Legend row under the map: `class:tokens` pairs in class colors.
/// The OVERVIEW headline — a big, glanceable context gauge. A wide bar that
/// warms green → amber → red as the window fills (the zones you cross on the
/// way to compaction), a large percentage, and the essentials inline. Fun to
/// look at, and you read the whole state in one glance.
// Retired from the OVERVIEW layout (R / % / rate / compaction all live in the
// top ribbon; the MAP box is the context-space headline). Kept for reference.
#[allow(dead_code)]
pub fn render_context_gauge(st: &State, f: &mut Frame<'_>, area: Rect) {
    if area.height == 0 || area.width < 16 {
        return;
    }
    let ratio = (st.resident as f64 / st.budget.max(1) as f64).clamp(0.0, 1.0);
    let rows: Vec<Rect> = if area.height >= 2 {
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)])
            .split(Rect { height: 2, ..area })
            .to_vec()
    } else {
        vec![area]
    };

    // headline text row (only when we have 2+ rows)
    if rows.len() == 2 {
        let zone = zone_color(ratio);
        let mut spans = vec![
            Span::styled(" CONTEXT  ".to_string(), fg(C_DIM).add_modifier(Modifier::BOLD)),
            Span::styled(
                format!("{} / {}  ", fmt_k0(st.resident), fmt_k0(st.budget)),
                fg(C_FG),
            ),
            Span::styled(
                format!("{:.0}%", ratio * 100.0),
                fg(zone).add_modifier(Modifier::BOLD),
            ),
        ];
        match st.compact_eta() {
            Some(eta) => spans.push(Span::styled(
                format!("   compact ≈{:.0} turns", eta.ceil()),
                fg(C_DIM),
            )),
            None => {}
        }
        spans.push(Span::styled(
            format!(
                "   {}{}/turn",
                if st.fill_ema >= 0.0 { "+" } else { "−" },
                fmt_k1(st.fill_ema.abs() as u64)
            ),
            fg(C_DIM),
        ));
        f.render_widget(Paragraph::new(Line::from(spans)), rows[0]);
    }

    // the bar row: fills to `ratio`, each cell tinted by the zone at its own
    // position so the bar visibly warms toward red; free space is a dim rail
    let bar = rows[rows.len() - 1];
    let w = bar.width.saturating_sub(2) as usize;
    if w == 0 {
        return;
    }
    let filled = (ratio * w as f64).round() as usize;
    let mut spans: Vec<Span<'static>> = vec![Span::raw(" ")];
    for i in 0..w {
        if i < filled {
            let fr = i as f64 / w as f64;
            spans.push(Span::styled("█".to_string(), fg(zone_color(fr))));
        } else {
            spans.push(Span::styled("░".to_string(), fg(C_GRID)));
        }
    }
    f.render_widget(Paragraph::new(Line::from(spans)), bar);
}

/// Header line above the MAP: scale labels that used to sit in the left gutter,
/// now one horizontal row (`class · ▪=256 · α1.00`) so the map is full-width.
/// `map_area` is the MAP's own rect — the rung is computed against it so the
/// label matches the box exactly.
pub fn render_map_header(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect, map_area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let full_cell = ui.map_mode == MapMode::Age;
    let cap = map_capacity(map_area.width, map_area.height, full_cell);
    let s_tok = if ui.map_pack {
        let total: u64 = eff_segs(st).iter().map(|s| s.tok).sum();
        (total.max(1) as f64 / cap.max(1) as f64).ceil().max(1.0) as u64
    } else {
        pick_rung(st.budget.max(1), cap, ui.rung_override)
    };
    let dot = || Span::styled("  ·  ".to_string(), fg(C_GRID));
    let spans = vec![
        Span::styled(" ".to_string(), fg(C_DIM)),
        Span::styled(
            ui.map_mode.label().to_string(),
            fg(C_DIM).add_modifier(Modifier::BOLD),
        ),
        dot(),
        Span::styled(rung_label(s_tok), fg(C_DIM)),
        dot(),
        Span::styled(format!("α{:.2}", eff_alpha(st)), fg(C_DIM)),
    ];
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// The category legend spans (one `▪name Nk` chip per non-zero category).
fn legend_spans(st: &State) -> Vec<Span<'static>> {
    let cats = eff_cats(st);
    let mut spans: Vec<Span<'static>> = Vec::new();
    for cat in CAT_ORDER {
        let tok = cats.get(cat.label()).copied().unwrap_or(0);
        if tok == 0 {
            continue;
        }
        spans.push(Span::styled(
            format!("▪{} {}  ", cat.display_name(), fmt_k0(tok)),
            fg(cat_color(cat)),
        ));
    }
    if spans.is_empty() {
        spans.push(Span::styled("no categories yet".to_string(), fg(C_DIM)));
    }
    spans
}

/// Pack the legend chips into full-width lines, breaking only BETWEEN chips so
/// a category name (e.g. "shell output") is never split across a line boundary.
fn legend_lines(st: &State, width: u16) -> Vec<Line<'static>> {
    let w = width.max(1) as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut cur: Vec<Span<'static>> = vec![Span::styled(" ".to_string(), fg(C_DIM))];
    let mut curlen = 1usize; // leading space
    for chip in legend_spans(st) {
        let len = chip.content.chars().count();
        if curlen + len > w && curlen > 1 {
            lines.push(Line::from(std::mem::take(&mut cur)));
            curlen = 0;
        }
        curlen += len;
        cur.push(chip);
    }
    lines.push(Line::from(cur));
    lines
}

/// Char rows the legend needs at `width` (capped at 5).
pub fn legend_rows(st: &State, width: u16) -> u16 {
    (legend_lines(st, width).len() as u16).clamp(1, 5)
}

pub fn render_legend(st: &State, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    f.render_widget(Paragraph::new(legend_lines(st, area.width)), area);
}

// ---------------------------------------------------------------------------
// INSPECT mode (OVERVIEW `i`) — segment walk + peek overlay (SPEC §e)
// ---------------------------------------------------------------------------

/// Segment identity line — replaces the legend row while INSPECT is active:
/// `#id cat · file path? · born t · est N ×α = M tok`. Cat-colored; the file
/// path resolves through the files table and takes the file's accent hue.
pub fn render_inspect_line(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let segs = eff_segs(st);
    let Some(sg) = segs.iter().find(|s| Some(s.id) == ui.sel_seg) else {
        f.render_widget(
            Paragraph::new(Line::styled(
                " INSPECT — no segments yet".to_string(),
                fg(C_DIM),
            )),
            area,
        );
        return;
    };
    let alpha = eff_alpha(st);
    let mut spans: Vec<Span<'static>> = vec![Span::styled(
        format!(" #{} {}", sg.id, sg.cat.label()),
        fg(seg_color(st, sg)).add_modifier(Modifier::BOLD),
    )];
    if let Some(fid) = sg.file {
        let path = eff_files(st)
            .into_iter()
            .find(|f| f.id == fid)
            .map(|f| f.path.clone())
            .unwrap_or_else(|| format!("file#{fid}"));
        spans.push(Span::styled(" · ".to_string(), fg(C_DIM)));
        spans.push(Span::styled(tail_trunc(&path, 36), fg(hue_of(st, fid))));
    }
    spans.push(Span::styled(format!(" · born t{}", sg.born), fg(C_DIM)));
    if sg.cat == Cat::Overhead || alpha <= 0.0 {
        // overhead is measured (R − Σest), not estimator-derived: no ×α story
        spans.push(Span::styled(format!(" · {} tok", fmt_k1(sg.tok)), fg(C_FG)));
    } else {
        let est = (sg.tok as f64 / alpha).round() as u64;
        spans.push(Span::styled(
            format!(
                " · est {} ×α{:.2} = {} tok",
                fmt_k1(est),
                alpha,
                fmt_k1(sg.tok)
            ),
            fg(C_FG),
        ));
    }
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// PEEK overlay (INSPECT `Enter`): the record's ACTUAL text, served on
/// demand by the engine. Centered ~76 cols; the excerpt splits on `\n` and
/// soft-wraps; a dim `…` line marks a view clip and `…truncated` marks the
/// engine's 2000-char cap. `found:false` renders the eviction notice; the
/// overhead segment's answer is the engine's explainer excerpt.
pub fn render_peek_overlay(
    st: &State,
    p: &PeekMsg,
    scroll: &std::cell::Cell<usize>,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 8 || area.height < 5 {
        return;
    }
    let w = area.width.saturating_sub(2).clamp(20, 76);
    let text_w = (w as usize).saturating_sub(3).max(1); // borders + lead pad
    let mut lines: Vec<Line<'static>> = Vec::new();
    // header: `#id cat kind · born tN · est → tok`
    let kind = p.kind.clone().unwrap_or_else(|| "?".into());
    let mut head: Vec<Span<'static>> = vec![Span::styled(
        format!(" #{} {} {}", p.seg, p.cat.label(), kind),
        fg(cat_color(p.cat)).add_modifier(Modifier::BOLD),
    )];
    head.push(Span::styled(
        if p.kind.as_deref() == Some("overhead") {
            // overhead is measured (R − visible), not estimated — no ×α story
            format!(" · measured {} tok", fmt_k1(p.tok))
        } else {
            format!(" · born t{} · est {} → {} tok", p.born, fmt_k1(p.est), fmt_k1(p.tok))
        },
        fg(C_DIM),
    ));
    if let Some(fid) = p.file {
        let path = eff_files(st)
            .into_iter()
            .find(|f| f.id == fid)
            .map(|f| f.path.clone())
            .unwrap_or_else(|| format!("file#{fid}"));
        head.push(Span::styled(" · ".to_string(), fg(C_DIM)));
        head.push(Span::styled(tail_trunc(&path, 28), fg(hue_of(st, fid))));
    }
    lines.push(Line::from(head));
    lines.push(Line::from(""));
    let mut body: Vec<Line<'static>> = Vec::new();
    if !p.found {
        body.push(Line::styled(
            " evicted — no longer in the transcript window".to_string(),
            fg(C_DIM),
        ));
    } else if p.excerpt.is_empty() {
        body.push(Line::styled(" (empty record)".to_string(), fg(C_DIM)));
    } else {
        for raw in p.excerpt.split('\n') {
            for chunk in wrap_line(raw, text_w) {
                body.push(Line::styled(format!(" {chunk}"), fg(C_FG)));
            }
        }
    }
    // shared pane: scroll clamp, footer range, position bar all uniform
    let mut foot = String::from(" esc close");
    if p.truncated {
        foot.push_str(" · …truncated at 2000 chars");
    }
    render_pane(
        &PaneFrame {
            title: format!("PEEK — seg #{}", p.seg),
            width: w,
            header: lines,
            body,
            footer: foot,
            scroll,
        },
        f,
        area,
    );
}

// ---------------------------------------------------------------------------
// EKG (OVERVIEW bottom pane)
// ---------------------------------------------------------------------------

/// Fixed future margin on the EKG x-axis so the dotted projection has room
/// to reach the T_auto rule. Window = last 480 turns + 32 future = 512 fixed.
const EKG_FUTURE: f64 = 32.0;
const EKG_PAST: f64 = 479.0;

pub fn render_ekg(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width < 4 || area.height == 0 {
        return;
    }
    let lanes = if area.height >= 5 { 2u16 } else { 0 };
    let canvas_h = area.height - lanes;
    let [plot, lane_out, lane_cost] = if lanes == 2 {
        let [a, b, c] = Layout::vertical([
            Constraint::Length(canvas_h),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .areas(area);
        [a, b, c]
    } else {
        [area, Rect::ZERO, Rect::ZERO]
    };

    let b = st.budget.max(1) as f64;
    let x1 = st.last_turn().unwrap_or(0) as f64;
    // fixed 512-turn axis, but LEFT-ANCHORED until the session outgrows it —
    // a young session's trace starts at the left edge, not mid-pane
    let x0 = (x1 - EKG_PAST).max(0.0);
    let x_hi = x0 + EKG_PAST + EKG_FUTURE;
    let t_auto = st.t_auto;
    let pts: Vec<(f64, f64, f64)> = st
        .turns
        .iter()
        .map(|t| (t.turn as f64, t.resident as f64, t.waterline as f64))
        .collect();
    let slope = st.slope();
    let r_now = st.resident as f64;
    let cliffs: Vec<(f64, f64)> = st
        .compactions
        .iter()
        .map(|c| (c.turn as f64, c.pre as f64))
        .collect();
    let cursor = ui.cursor.map(|c| c as f64);

    if plot.height > 0 {
        let canvas = Canvas::default()
            .marker(Marker::Braille)
            .x_bounds([x0, x_hi])
            .y_bounds([0.0, b])
            .paint(move |ctx| {
                // zone rules at 0.60 / 0.85 and the T_auto rule
                for (frac, col) in [(0.60, C_GRID), (0.85, C_GRID)] {
                    ctx.draw(&CanvasLine {
                        x1: x0,
                        y1: b * frac,
                        x2: x_hi,
                        y2: b * frac,
                        color: rgb(col),
                    });
                }
                ctx.draw(&CanvasLine {
                    x1: x0,
                    y1: b * t_auto,
                    x2: x_hi,
                    y2: b * t_auto,
                    color: rgb(scale(C_AMBER, 0.55)),
                });
                ctx.layer();
                // waterline C_t — dim cyan
                for w in pts.windows(2) {
                    ctx.draw(&CanvasLine {
                        x1: w[0].0,
                        y1: w[0].2,
                        x2: w[1].0,
                        y2: w[1].2,
                        color: rgb(scale(C_CYAN, 0.5)),
                    });
                }
                // resident R_t — zone-colored bright line
                for w in pts.windows(2) {
                    let zone = zone_color(w[1].1 / b);
                    ctx.draw(&CanvasLine {
                        x1: w[0].0,
                        y1: w[0].1,
                        x2: w[1].0,
                        y2: w[1].1,
                        color: rgb(zone),
                    });
                }
                // dotted least-squares projection to the T_auto rule
                if slope > 0.0 && r_now < b * t_auto {
                    let mut dots: Vec<(f64, f64)> = Vec::new();
                    let mut x = x1;
                    let mut y = r_now;
                    while x <= x_hi && y <= b * t_auto {
                        dots.push((x, y));
                        x += 2.0;
                        y += slope * 2.0;
                    }
                    ctx.draw(&Points {
                        coords: &dots,
                        color: rgb(C_DIM),
                    });
                }
                // cursor as a vertical line (replay)
                if let Some(cx) = cursor {
                    ctx.draw(&CanvasLine {
                        x1: cx,
                        y1: 0.0,
                        x2: cx,
                        y2: b,
                        color: rgb(scale(C_AMBER, 0.8)),
                    });
                }
                ctx.layer();
                // ▼ above compaction cliffs
                for &(cx, pre) in &cliffs {
                    if cx >= x0 {
                        ctx.print(
                            cx,
                            (pre + b * 0.04).min(b),
                            Span::styled("▼", fg(C_MAGENTA)),
                        );
                    }
                }
            });
        f.render_widget(canvas, plot);
    }

    // sparkline lanes: out/turn (0–16k fixed), cost_u/turn (0–100ku fixed)
    // lanes right-align so newest-at-right matches the plot axis
    if lane_out.height > 0 {
        let w = lane_out.width.saturating_sub(6) as usize;
        let take = st.turns.len().min(w);
        let mut s = " ".repeat(w - take);
        s.extend(
            st.turns
                .iter()
                .skip(st.turns.len() - take)
                .map(|t| spark_char(t.out as f64, 16_000.0)),
        );
        f.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("out/t ".to_string(), fg(C_DIM)),
                Span::styled(s, fg(C_ASSIST)),
            ])),
            lane_out,
        );
    }
    if lane_cost.height > 0 {
        let w = lane_cost.width.saturating_sub(6) as usize;
        let take = st.turns.len().min(w);
        let mut s = " ".repeat(w - take);
        s.extend(
            st.turns
                .iter()
                .skip(st.turns.len() - take)
                .map(|t| spark_char(t.cost_u, 100.0)),
        );
        f.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("ku/t  ".to_string(), fg(C_DIM)),
                Span::styled(s, fg(C_AMBER)),
            ])),
            lane_cost,
        );
    }
}

// ---------------------------------------------------------------------------
// FILES — traffic roll + allocation table
// ---------------------------------------------------------------------------

const ROLL_GUTTER: usize = 23; // 22-cell path + 1 space

/// WEAVE-idiom traffic roll: rows = files, x = turns; `▀`=read `▄`=write/edit
/// `█`=both, intensity by the fixed access-size ramp.
#[allow(clippy::too_many_arguments)]
pub fn render_files_roll(
    st: &State,
    ui: &Ui,
    order: &[u64],
    sel: usize,
    scroll: usize,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width as usize <= ROLL_GUTTER + 1 || area.height == 0 {
        return;
    }
    let w_turns = area.width as usize - ROLL_GUTTER;
    let last = st.last_turn().unwrap_or(0);
    let t_lo = last.saturating_sub(w_turns as u64 - 1);
    // (file, turn) → (read?, write/edit?, max tok)
    let mut acc: HashMap<(u64, u64), (bool, bool, u64)> = HashMap::new();
    for fa in &st.faccess {
        if fa.turn < t_lo {
            continue;
        }
        let e = acc.entry((fa.file, fa.turn)).or_insert((false, false, 0));
        match fa.op {
            Op::R => e.0 = true,
            Op::W | Op::E => e.1 = true,
            Op::Other => {}
        }
        e.2 = e.2.max(fa.tok);
    }
    let files_by_id: HashMap<u64, &FileRec> =
        eff_files(st).into_iter().map(|f| (f.id, f)).collect();

    let mut lines: Vec<Line<'static>> = Vec::with_capacity(area.height as usize);
    for row in 0..area.height as usize {
        let idx = scroll + row;
        let Some(&fid) = order.get(idx) else {
            lines.push(Line::from(""));
            continue;
        };
        let hue = hue_of(st, fid);
        let (path, resident) = files_by_id
            .get(&fid)
            .map(|f| (f.path.clone(), f.resident))
            .unwrap_or_else(|| (format!("file#{fid}"), true));
        let mark = if resident { "" } else { "✝" };
        let label = tail_trunc(&format!("{mark}{path}"), ROLL_GUTTER - 1);
        let mut gstyle = fg(hue);
        if idx == sel {
            gstyle = gstyle.add_modifier(Modifier::REVERSED);
        }
        let mut spans = vec![Span::styled(
            format!("{label:<width$}", width = ROLL_GUTTER),
            gstyle,
        )];
        for c in 0..w_turns {
            let turn = t_lo + c as u64;
            // shared turn cursor: a visible column band in replay (live, the
            // cursor IS the right edge)
            let is_cur = ui.cursor == Some(turn);
            match acc.get(&(fid, turn)) {
                Some(&(r, we, tok)) => {
                    let glyph = match (r, we) {
                        (true, true) => "█",
                        (true, false) => "▀",
                        (false, true) => "▄",
                        (false, false) => "·",
                    };
                    // fixed access-size intensity ramp
                    let (k, bold) = if tok > 16_000 {
                        (1.0, true)
                    } else if tok >= 4_000 {
                        (1.0, false)
                    } else if tok >= 1_000 {
                        (0.75, false)
                    } else {
                        (0.45, false)
                    };
                    let mut style = fg(scale(hue, k));
                    if bold {
                        style = style.add_modifier(Modifier::BOLD);
                    }
                    // newest column pulses
                    if turn == last && ui.blink {
                        style = fg(C_WHITE);
                    }
                    if is_cur {
                        style = style.bg(rgb(C_GRID));
                    }
                    spans.push(Span::styled(glyph.to_string(), style));
                }
                None => spans.push(if is_cur {
                    Span::styled("·".to_string(), fg(C_AMBER).bg(rgb(C_GRID)))
                } else {
                    Span::raw(" ")
                }),
            }
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// per-file 8-slot access-size spark (fixed 0–16k)
fn file_spark(st: &State, fid: u64) -> String {
    let mut toks: Vec<u64> = st
        .faccess
        .iter()
        .rev()
        .filter(|fa| fa.file == fid)
        .take(8)
        .map(|fa| fa.tok)
        .collect();
    toks.reverse();
    let mut s = String::new();
    for _ in 0..(8 - toks.len()) {
        s.push(' ');
    }
    for t in toks {
        s.push(spark_char(t as f64, 16_000.0));
    }
    s
}

/// Allocation table: `tok(est) %res rd/wr/ed waste last spark path`
/// (`ed` counts Edit-tool patches — NOT execute; executions live in SHELL).
#[allow(clippy::too_many_arguments)]
pub fn render_files_table(
    st: &State,
    ui: &Ui,
    order: &[u64],
    sel: usize,
    sort: FileSort,
    detail: bool,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let files_by_id: HashMap<u64, &FileRec> =
        eff_files(st).into_iter().map(|f| (f.id, f)).collect();
    let total = eff_segs(st).iter().map(|s| s.tok).sum::<u64>().max(1);
    let alpha = eff_alpha(st);

    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::styled(
        format!(
            "{:>8} {:>5} {:>8} {:>7} {:>8}  {:8} path  [sort:{}]",
            "tok(est)", "%res", "rd/wr/ed", "waste", "last", "spark", sort.label()
        ),
        fg(C_DIM),
    ));
    let rows = area.height.saturating_sub(1) as usize;
    let detail_rows = if detail { 2 } else { 0 };
    let list_rows = rows.saturating_sub(detail_rows);
    // keep the selection visible
    let scroll = sel.saturating_sub(list_rows.saturating_sub(1));
    for i in 0..list_rows {
        let idx = scroll + i;
        let Some(&fid) = order.get(idx) else { break };
        let Some(fr) = files_by_id.get(&fid) else {
            continue;
        };
        let hue = hue_of(st, fid);
        let pres = if fr.resident {
            format!("{:>4.0}%", (fr.tok as f64 * alpha) / total as f64 * 100.0)
        } else {
            "   ✝ ".to_string()
        };
        let mut style = fg(C_FG);
        if idx == sel {
            style = style.add_modifier(Modifier::REVERSED);
        }
        lines.push(Line::from(vec![
            Span::styled(
                format!(
                    "{:>8} {:>5} {:>8} {:>7} {:>8}  ",
                    fmt_k1(fr.tok),
                    pres,
                    format!("{}/{}/{}", fr.reads, fr.writes, fr.edits),
                    fmt_k1(fr.waste),
                    fr.last_ts
                ),
                style,
            ),
            Span::styled(file_spark(st, fid), fg(scale(hue, 0.8))),
            Span::styled(
                format!(
                    "  {}{}",
                    if fr.resident { "" } else { "✝" },
                    tail_trunc(&fr.path, area.width.saturating_sub(48) as usize)
                ),
                if idx == sel {
                    fg(hue).add_modifier(Modifier::REVERSED)
                } else {
                    fg(hue)
                },
            ),
        ]));
    }
    if detail {
        if let Some(&fid) = order.get(sel) {
            if let Some(fr) = files_by_id.get(&fid) {
                lines.push(Line::styled(format!(" {}", fr.path), fg(C_FG)));
                lines.push(Line::styled(
                    format!(
                        " est {} · ×α{:.2} = {} resident · r {} w {} e {} · waste {}",
                        fmt_k1(fr.tok),
                        alpha,
                        fmt_k1((fr.tok as f64 * alpha) as u64),
                        fr.reads,
                        fr.writes,
                        fr.edits,
                        fmt_k1(fr.waste)
                    ),
                    fg(C_DIM),
                ));
            }
        }
    }
    let _ = ui;
    f.render_widget(Paragraph::new(lines), area);
}

// ---------------------------------------------------------------------------
// FILES — NOW perspective (live file activity, no turn history)
// ---------------------------------------------------------------------------

/// NOW ordering (pure): hot = `now − last_epoch < HEAT_STATIC_S` (evicted
/// files included while still hot), `last_epoch` desc, tie path asc.
/// Cold = every other RESIDENT file: known epochs desc first, then the
/// `last_epoch == 0` unknowns (by `last_ts` desc, then path). Fully cold AND
/// evicted → dropped from the view entirely.
pub fn file_now_order(st: &State, now: f64) -> (Vec<u64>, Vec<u64>) {
    let mut hot: Vec<&FileRec> = Vec::new();
    let mut cold: Vec<&FileRec> = Vec::new();
    for f in st.files.values() {
        let is_hot = f.last_epoch > 0.0 && now - f.last_epoch < HEAT_STATIC_S;
        if is_hot {
            hot.push(f);
        } else if f.resident {
            cold.push(f);
        } // cold + evicted: dropped
    }
    let by_epoch = |a: &&FileRec, b: &&FileRec| {
        b.last_epoch
            .partial_cmp(&a.last_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.path.cmp(&b.path))
    };
    hot.sort_by(by_epoch);
    cold.sort_by(|a, b| {
        // unknowns sink below every known epoch
        match (a.last_epoch > 0.0, b.last_epoch > 0.0) {
            (true, false) => std::cmp::Ordering::Less,
            (false, true) => std::cmp::Ordering::Greater,
            (true, true) => by_epoch(&a, &b),
            (false, false) => b.last_ts.cmp(&a.last_ts).then_with(|| a.path.cmp(&b.path)),
        }
    });
    (
        hot.iter().map(|f| f.id).collect(),
        cold.iter().map(|f| f.id).collect(),
    )
}

/// Newest faccess (op, tok) for a file, if the 4096-ring still holds one.
fn newest_access(st: &State, fid: u64) -> Option<(Op, u64)> {
    st.faccess
        .iter()
        .rev()
        .find(|fa| fa.file == fid)
        .map(|fa| (fa.op, fa.tok))
}

/// NOW row anatomy: `AGE op LAST TOKBAR TOK PATH` (Full) — a btop-style live
/// process list for files. Brightness = the shared τ=45 s decay law; hot and
/// cold zones split by a dim divider; live-only (replay renders a notice).
#[allow(clippy::too_many_arguments)]
pub fn render_files_now(
    st: &State,
    ui: &Ui,
    hot: &[u64],
    cold: &[u64],
    sel: usize,
    scroll: usize,
    detail: bool,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 12 || area.height == 0 {
        return;
    }
    // NOW is live-only: "currently" has no meaning at turn N−80.
    if ui.cursor.is_some() {
        let msg = "NOW is live-only — End/Esc → LIVE · v → history";
        let rect = centered(area, (msg.chars().count() as u16).min(area.width), 1);
        f.render_widget(Paragraph::new(Line::styled(msg, fg(C_DIM))), rect);
        return;
    }
    let now = ui.now_epoch;
    let compact = ui.tier == Tier::Compact;
    let full = ui.tier == Tier::Full;
    let files_by_id: HashMap<u64, &FileRec> =
        eff_files(st).into_iter().map(|f| (f.id, f)).collect();
    let alpha = eff_alpha(st);

    let mut lines: Vec<Line<'static>> = Vec::new();
    // header: column labels left, counts + toggle hint right
    let left = if compact {
        format!("NOW hot {} · +{} cold", hot.len(), cold.len())
    } else if full {
        format!("{:>4} {:1} {:>6} {:<8} {:>6}  path", "AGE", "O", "LAST", "TOK", "")
    } else {
        format!("{:>4} {:1} {:>6} path", "AGE", "O", "LAST")
    };
    let right = if compact {
        "v hist".to_string()
    } else {
        format!("hot {} · cold {}   v history", hot.len(), cold.len())
    };
    let pad = (area.width as usize)
        .saturating_sub(left.chars().count() + right.chars().count());
    let hdr = if pad > 0 {
        format!("{left}{}{right}", " ".repeat(pad))
    } else {
        left
    };
    lines.push(Line::styled(hdr, fg(C_DIM)));

    // flat display list: hot rows, divider (when both zones visible), cold rows
    #[derive(Clone, Copy)]
    enum Row {
        File(u64, usize), // fid, flat selection index
        Divider,
    }
    let mut rows: Vec<Row> = Vec::new();
    for (i, &fid) in hot.iter().enumerate() {
        rows.push(Row::File(fid, i));
    }
    if !compact {
        if !hot.is_empty() && !cold.is_empty() {
            rows.push(Row::Divider);
        }
        for (i, &fid) in cold.iter().enumerate() {
            rows.push(Row::File(fid, hot.len() + i));
        }
    }

    let detail_rows = if detail && !compact { 2usize } else { 0 };
    let list_rows = (area.height as usize)
        .saturating_sub(1)
        .saturating_sub(detail_rows);
    // keep the selection visible (same pattern as the table)
    let sel_row = rows
        .iter()
        .position(|r| matches!(r, Row::File(_, i) if *i == sel))
        .unwrap_or(0);
    let scroll = scroll
        .min(sel_row)
        .max(sel_row.saturating_sub(list_rows.saturating_sub(1)));

    for r in rows.iter().skip(scroll).take(list_rows) {
        match *r {
            Row::Divider => {
                let w = area.width as usize;
                let label = " cold ";
                let lead = 8usize.min(w.saturating_sub(label.len()));
                let tail = w.saturating_sub(lead + label.len());
                lines.push(Line::styled(
                    format!("{}{label}{}", "─".repeat(lead), "─".repeat(tail)),
                    fg(C_DIM),
                ));
            }
            Row::File(fid, idx) => {
                let Some(fr) = files_by_id.get(&fid) else { continue };
                let hue = hue_of(st, fid);
                let age = if fr.last_epoch > 0.0 {
                    Some((now - fr.last_epoch).max(0.0))
                } else {
                    None
                };
                // decay brightness: the exact MAP-heat formula. The SELECTED
                // row renders at full brightness — inversion over a dimmed fg
                // is near-black-on-near-black and swallows both signals.
                let selected = idx == sel;
                let k = if selected {
                    1.0
                } else {
                    age.map(heat_k).unwrap_or(0.30)
                };
                let touched = st.touch_pulse > 0 && st.touch_file == fid;
                let rev = |s: Style| {
                    if selected {
                        s.add_modifier(Modifier::REVERSED)
                    } else {
                        s
                    }
                };
                let age_txt = age.map(fmt_age).unwrap_or_else(|| "—".into());
                let (op_txt, op_col) = match newest_access(st, fid) {
                    Some((Op::R, _)) => ("r", (90, 200, 200)),
                    Some((Op::W, _)) => ("w", (230, 140, 80)),
                    Some((Op::E, _)) => ("e", (230, 200, 80)),
                    Some((Op::Other, _)) | None => ("·", C_DIM),
                };
                let last_txt = newest_access(st, fid)
                    .map(|(_, t)| fmt_k1(t))
                    .unwrap_or_else(|| "—".into());
                // entry pulse: AGE+OP cells flash white for 6 frames
                let age_style = rev(if touched {
                    fg(C_WHITE)
                } else {
                    fg(scale(C_WHITE, k))
                });
                let op_style = rev(if touched { fg(C_WHITE) } else { fg(scale(op_col, k)) });
                let mut spans: Vec<Span<'static>> = Vec::new();
                if compact {
                    spans.push(Span::styled(format!("{age_txt:>3} "), age_style));
                    spans.push(Span::styled(op_txt.to_string(), op_style));
                    spans.push(Span::styled(" ".to_string(), rev(fg(C_DIM))));
                } else {
                    spans.push(Span::styled(format!("{age_txt:>4} "), age_style));
                    spans.push(Span::styled(op_txt.to_string(), op_style));
                    spans.push(Span::styled(
                        format!(" {last_txt:>6} "),
                        rev(fg(scale(C_WHITE, k))),
                    ));
                    if full {
                        // FIXED 0–64k token bar; ≥64k → full + BOLD
                        let res = fr.tok as f64 * alpha;
                        let frac = res / 64_000.0;
                        let bar = eighth_bar(frac.min(1.0), 8);
                        let mut bs = fg(scale(hue, k));
                        if res >= 64_000.0 {
                            bs = bs.add_modifier(Modifier::BOLD);
                        }
                        spans.push(Span::styled(format!("{bar:<8} "), rev(bs)));
                        spans.push(Span::styled(
                            format!("{:>6}  ", fmt_k1(fr.tok)),
                            rev(fg(C_DIM)),
                        ));
                    }
                }
                // two-tone path: dir prefix dim, basename in the accent hue
                let prefix_w: usize = if compact { 6 } else if full { 31 } else { 16 };
                let path_w = (area.width as usize).saturating_sub(prefix_w);
                let cross = if fr.resident { "" } else { "✝" };
                let shown = tail_trunc(
                    &format!("{cross}{}", fr.path),
                    path_w,
                );
                let cut = shown.rfind('/').map(|i| i + 1).unwrap_or(0);
                let (dir, base) = shown.split_at(cut);
                spans.push(Span::styled(dir.to_string(), rev(fg(scale(C_DIM, k)))));
                spans.push(Span::styled(base.to_string(), rev(fg(scale(hue, k)))));
                lines.push(Line::from(spans));
            }
        }
    }
    if detail_rows > 0 {
        let flat: Vec<u64> = hot.iter().chain(cold.iter()).copied().collect();
        if let Some(fr) = flat.get(sel).and_then(|fid| files_by_id.get(fid)) {
            lines.push(Line::styled(format!(" {}", fr.path), fg(C_FG)));
            lines.push(Line::styled(
                format!(
                    " est {} · ×α{:.2} = {} resident · r {} w {} e {} · waste {}",
                    fmt_k1(fr.tok),
                    alpha,
                    fmt_k1((fr.tok as f64 * alpha) as u64),
                    fr.reads,
                    fr.writes,
                    fr.edits,
                    fmt_k1(fr.waste)
                ),
                fg(C_DIM),
            ));
        }
    }
    f.render_widget(Paragraph::new(lines), area);
}

// ---------------------------------------------------------------------------
// TURNS — cache & cost ledger
// ---------------------------------------------------------------------------

const TURNS_GUTTER: usize = 6;

/// TURNS band edges as rounded PREFIX sums (cumulative rounding), so the
/// stack's total height equals R_t at the shared 0–B scale — the spec's
/// stack-identity rule. Four bands, bottom-up: cr steel · cc_5m cyan ·
/// cc_1h purple · in red. Returns (cr, cc_5m, cc, in) edges in logical rows
/// from the bottom; the min-1-cell rule for nonzero `in` is folded in.
///
/// Drift-proof (SPEC §e TURNS): the `in` edge is computed from `cr + cc`
/// (never the split sum) and `cc5m′ = cc_5m if cc_5m+cc_1h > 0 else cc` —
/// an old engine sending only `cc` renders a fully-cyan band with the stack
/// total unchanged; a mis-summed split can only move the internal
/// cyan/purple boundary, never the top.
pub fn stack_edges4(
    cr: u64,
    cc: u64,
    cc_5m: u64,
    cc_1h: u64,
    in_tok: u64,
    b: f64,
    logical: usize,
) -> (usize, usize, usize, usize) {
    let e_of = |tok: u64| (tok as f64 / b * logical as f64).round() as usize;
    let e1 = e_of(cr);
    let cc5m_p = if cc_5m + cc_1h > 0 { cc_5m } else { cc };
    let e3 = e_of(cr + cc);
    let e2 = e_of(cr.saturating_add(cc5m_p)).clamp(e1, e3);
    let mut e4 = e_of(cr + cc + in_tok);
    if in_tok > 0 && e4 == e3 {
        e4 = e3 + 1;
    }
    (e1, e2, e3, e4)
}

/// Legacy 3-edge view (cr, cc, in) — a shim over [`stack_edges4`] kept so the
/// stack-identity contract stays byte-comparable across the split.
pub fn stack_edges(cr: u64, cc: u64, in_tok: u64, b: f64, logical: usize) -> (usize, usize, usize) {
    let (e1, _e2, e3, e4) = stack_edges4(cr, cc, cc, 0, in_tok, b, logical);
    (e1, e3, e4)
}

/// Logical row of a token address at the shared 0–B scale (waterline tick).
fn tick_row(tok: u64, b: f64, logical: usize) -> usize {
    (tok as f64 / b * logical as f64).round() as usize
}

/// Lane fg color rules (fixed thresholds, no autoscale): out×stop-reason,
/// dur×tools, ku×hit — the causally-linked second quantity per lane.
fn out_lane_color(t: &crate::ipc::Turn) -> (u8, u8, u8) {
    match t.stop.as_deref() {
        Some("max_tokens") => C_RED,
        None | Some("end_turn") | Some("tool_use") | Some("") => C_ASSIST,
        Some(_) => C_AMBER,
    }
}
fn dur_lane_color(t: &crate::ipc::Turn) -> (u8, u8, u8) {
    match t.tools {
        0 => scale(C_USER, 0.45),
        1..=2 => C_USER,
        3..=5 => (150, 190, 250),
        _ => C_WHITE,
    }
}
fn ku_lane_color(t: &crate::ipc::Turn) -> (u8, u8, u8) {
    if t.hit >= 0.90 {
        C_AMBER
    } else if t.hit >= 0.50 {
        C_BASH
    } else {
        C_RED
    }
}

/// Rail marker per turn column, priority ▲ thrash > ▼ compaction > ◆ model
/// switch (all UI-side derivations from the turn ring + compaction list).
fn rail_marker(
    st: &State,
    t: &crate::ipc::Turn,
    prev: Option<&&crate::ipc::Turn>,
) -> Option<(char, (u8, u8, u8), bool)> {
    let thrash = prev
        .map(|p| t.waterline + 1024 < p.waterline) // same rule as state.rs
        .unwrap_or(false);
    if thrash {
        return Some(('▲', C_RED, st.thrash_pulse > 0));
    }
    if st.compactions.iter().any(|c| c.turn == t.turn) {
        return Some(('▼', C_MAGENTA, false));
    }
    let switched = prev
        .map(|p| !t.model.is_empty() && !p.model.is_empty() && t.model != p.model)
        .unwrap_or(false);
    if switched {
        return Some(('◆', C_WHITE, false));
    }
    None
}

pub fn render_turns_tab(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width as usize <= TURNS_GUTTER + 1 || area.height == 0 {
        return;
    }
    // degradation (per-Rect, tier-independent): ledger 2 iff h ≥ 10; lanes
    // 3 iff h ≥ 12, 2 (drop dur) iff 8 ≤ h < 12, else 0; rail iff chart ≥ 4
    let detail_rows: u16 = if area.height >= 10 { 2 } else { 0 };
    let lane_rows: u16 = if area.height >= 12 {
        3
    } else if area.height >= 8 {
        2
    } else {
        0
    };
    let chart_h = area.height - detail_rows - lane_rows;
    let mut ys = vec![Constraint::Length(chart_h)];
    ys.extend((0..lane_rows).map(|_| Constraint::Length(1)));
    if detail_rows > 0 {
        ys.push(Constraint::Length(2));
    }
    let chunks = Layout::vertical(ys).split(area);
    let chart = chunks[0];
    let rail = chart.height >= 4;

    let w_turns = area.width as usize - TURNS_GUTTER;
    let last = st.last_turn().unwrap_or(0);
    let t_lo = last.saturating_sub(w_turns as u64 - 1);
    let by_turn: HashMap<u64, &crate::ipc::Turn> =
        st.turns.iter().map(|t| (t.turn, t)).collect();
    let b = st.budget.max(1) as f64;
    let logical = (2 * chart.height as usize).max(2);
    let cursor_turn = ui.cursor.unwrap_or(last);
    // new-turn white-blend pulse strength (the ledger's write head)
    let blend = if st.turn_pulse > 0 {
        st.turn_pulse as f64 / 6.0 * 0.6
    } else {
        0.0
    };

    // stacked columns: cr steel + cc_5m cyan + cc_1h purple + in red, y
    // fixed 0–B; prev-waterline tick overrides the band color at C_{t−1}
    let col_color = |t: &crate::ipc::Turn, lr: usize| -> Option<Color> {
        // lr counted from the BOTTOM
        let (e1, e2, e3, e4) = stack_edges4(t.cr, t.cc, t.cc_5m, t.cc_1h, t.in_tok, b, logical);
        let mut c = if lr < e1 {
            Some(C_STEEL)
        } else if lr < e2 {
            Some(C_CYAN)
        } else if lr < e3 {
            Some(C_ATTACH)
        } else if lr < e4 {
            Some(C_RED)
        } else {
            None
        };
        // prev-waterline tick: below steel top = promotion into cache,
        // floating above = invalidation depth. Skipped for a ringless prev.
        if t.turn > 0 {
            if let Some(p) = by_turn.get(&(t.turn - 1)) {
                let tr = tick_row(p.waterline, b, logical);
                if tr == lr && tr < logical && p.waterline > 0 {
                    c = Some(C_WLINE);
                }
            }
        }
        // newest column pulses white-blended while turn_pulse > 0
        let mut c = c?;
        if blend > 0.0 && t.turn == last {
            c = lerp(c, C_WHITE, blend);
        }
        Some(rgb(c))
    };

    let mut lines: Vec<Line<'static>> = Vec::with_capacity(chart.height as usize);
    for row in 0..chart.height as usize {
        let mut spans: Vec<Span<'static>> = Vec::with_capacity(w_turns + 1);
        // gutter: B at top, 0 at bottom
        let g = if row == 0 {
            format!("{:>5} ", fmt_k0(st.budget))
        } else if row + 1 == chart.height as usize {
            format!("{:>5} ", "0")
        } else {
            " ".repeat(TURNS_GUTTER)
        };
        spans.push(Span::styled(g, fg(C_DIM)));
        // logical rows from top: lt is higher
        let lt = logical - 1 - 2 * row; // top half logical index (from bottom)
        let lb = lt.saturating_sub(1);
        for c in 0..w_turns {
            let turn = t_lo + c as u64;
            let is_cur = turn == cursor_turn;
            let cursorize = |mut sp: Span<'static>| -> Span<'static> {
                if is_cur {
                    sp = sp.patch_style(Style::default().add_modifier(Modifier::REVERSED));
                }
                sp
            };
            match by_turn.get(&turn) {
                Some(t) => {
                    // rail row: the event marker wins over the column pixel
                    if row == 0 && rail {
                        let prev = if t.turn > 0 {
                            by_turn.get(&(t.turn - 1))
                        } else {
                            None
                        };
                        if let Some((glyph, col, pulse)) = rail_marker(st, t, prev) {
                            let mut style = fg(col);
                            if pulse {
                                style = style.add_modifier(Modifier::BOLD);
                            }
                            spans.push(cursorize(Span::styled(glyph.to_string(), style)));
                            continue;
                        }
                    }
                    let top = col_color(t, lt);
                    let bot = if lt == 0 { None } else { col_color(t, lb) };
                    spans.push(cursorize(compose(top, bot)));
                }
                None => {
                    spans.push(cursorize(Span::raw(" ")));
                }
            }
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), chart);

    // lanes: out (0–16k) · dur (0–120s) · ku (0–100ku) — glyphs and scales
    // unchanged; per-column fg carries the causally-linked second quantity
    if lane_rows > 0 {
        let lane = |vals: &dyn Fn(&crate::ipc::Turn) -> f64,
                    color: &dyn Fn(&crate::ipc::Turn) -> (u8, u8, u8),
                    max: f64,
                    label: &str|
         -> Line<'static> {
            let mut spans: Vec<Span<'static>> = Vec::with_capacity(w_turns + 1);
            spans.push(Span::styled(format!("{label:<6}"), fg(C_DIM)));
            for c in 0..w_turns {
                let turn = t_lo + c as u64;
                match by_turn.get(&turn) {
                    Some(t) => spans.push(Span::styled(
                        spark_char(vals(t), max).to_string(),
                        fg(color(t)),
                    )),
                    None => spans.push(Span::raw(" ")),
                }
            }
            Line::from(spans)
        };
        f.render_widget(
            Paragraph::new(lane(&|t| t.out as f64, &out_lane_color, 16_000.0, "out")),
            chunks[1],
        );
        if lane_rows == 3 {
            f.render_widget(
                Paragraph::new(lane(
                    &|t| t.dur_ms.unwrap_or(0) as f64,
                    &dur_lane_color,
                    120_000.0,
                    "dur",
                )),
                chunks[2],
            );
        }
        f.render_widget(
            Paragraph::new(lane(&|t| t.cost_u, &ku_lane_color, 100.0, "ku")),
            chunks[lane_rows as usize],
        );
    }

    // cursor-selected turn readout (2 lines)
    if detail_rows > 0 {
        let d = chunks[chunks.len() - 1];
        let lines: Vec<Line<'static>> = match by_turn.get(&cursor_turn) {
            Some(t) => {
                let mut l2: Vec<Span<'static>> = vec![Span::styled(
                    format!(
                        "hit {:.1}%   cost {:.1}ku   dur {}   stop {}   tools {}",
                        t.hit * 100.0,
                        t.cost_u,
                        t.dur_ms.map(fmt_dur).unwrap_or_else(|| "—".into()),
                        t.stop.clone().unwrap_or_else(|| "—".into()),
                        t.tools
                    ),
                    fg(C_DIM),
                )];
                // per-turn faccess counts (omitted when zero accesses)
                let (mut r, mut w, mut e) = (0u64, 0u64, 0u64);
                for fa in st.faccess.iter().filter(|fa| fa.turn == t.turn) {
                    match fa.op {
                        Op::R => r += 1,
                        Op::W => w += 1,
                        Op::E => e += 1,
                        Op::Other => {}
                    }
                }
                if r + w + e > 0 {
                    let mut fa_txt = format!("  fa {r}r/{w}w");
                    if e > 0 {
                        fa_txt.push_str(&format!("/{e}e"));
                    }
                    l2.push(Span::styled(fa_txt, fg(C_DIM)));
                }
                // model-switch annotation
                if t.turn > 0 {
                    if let Some(p) = by_turn.get(&(t.turn - 1)) {
                        if !t.model.is_empty() && !p.model.is_empty() && t.model != p.model {
                            l2.push(Span::styled(
                                format!("  ◆ {}→{}", p.model, t.model),
                                fg(C_WHITE),
                            ));
                        }
                    }
                }
                vec![
                    Line::from(vec![
                        Span::styled(format!("turn {}  {}   ", t.turn, t.ts), fg(C_FG)),
                        Span::styled(format!("in {} ", fmt_k1(t.in_tok)), fg(C_RED)),
                        Span::styled("│ ".to_string(), fg(C_GRID)),
                        Span::styled(format!("cr {} ", fmt_k1(t.cr)), fg(C_STEEL)),
                        Span::styled("│ ".to_string(), fg(C_GRID)),
                        Span::styled(format!("cc {} (", fmt_k1(t.cc)), fg(C_CYAN)),
                        Span::styled(format!("5m {}", fmt_k1(t.cc_5m)), fg(C_CYAN)),
                        Span::styled("·".to_string(), fg(C_GRID)),
                        Span::styled(format!("1h {}", fmt_k1(t.cc_1h)), fg(C_ATTACH)),
                        Span::styled(") ".to_string(), fg(C_CYAN)),
                        Span::styled("│ ".to_string(), fg(C_GRID)),
                        Span::styled(format!("out {}", fmt_k1(t.out)), fg(C_ASSIST)),
                    ]),
                    Line::from(l2),
                ]
            }
            None => vec![Line::styled(
                format!("turn {cursor_turn} — outside the turn ring"),
                fg(C_DIM),
            )],
        };
        f.render_widget(Paragraph::new(lines), d);
    }
}

// ---------------------------------------------------------------------------
// AGENTS — concurrency load strip + unified ledger
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum AgentSort {
    Recent,
    Tok,
    Launch,
}

impl AgentSort {
    pub fn next(self) -> Self {
        match self {
            AgentSort::Recent => AgentSort::Tok,
            AgentSort::Tok => AgentSort::Launch,
            AgentSort::Launch => AgentSort::Recent,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            AgentSort::Recent => "recent",
            AgentSort::Tok => "tok",
            AgentSort::Launch => "launch",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum AgentFilter {
    All,
    Run,
    Fail,
}

impl AgentFilter {
    pub fn next(self) -> Self {
        match self {
            AgentFilter::All => AgentFilter::Run,
            AgentFilter::Run => AgentFilter::Fail,
            AgentFilter::Fail => AgentFilter::All,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            AgentFilter::All => "all",
            AgentFilter::Run => "run",
            AgentFilter::Fail => "fail",
        }
    }
    pub fn pass(self, a: &AgentRec) -> bool {
        match self {
            AgentFilter::All => true,
            AgentFilter::Run => a.state == "running",
            AgentFilter::Fail => a.state == "failed",
        }
    }
}

/// The turn an agent's span ends on: `turn1`, else the live tail while it
/// runs, else its own launch turn.
pub fn agent_end(a: &AgentRec, last: u64) -> u64 {
    a.turn1
        .unwrap_or(if a.state == "running" { last } else { a.turn0 })
}

/// One row of the unified ledger (gantt and table can no longer desync —
/// there is only one sequence).
#[derive(Clone)]
pub enum ARow {
    /// workflow rollup: name, child indices (turn0 asc), expansion state
    Wf {
        wf: String,
        kids: Vec<usize>,
        expanded: bool,
    },
    /// agent row; `child` = indented under a wf rollup
    Ag { idx: usize, child: bool },
}

/// The unified ledger's row sequence (pure): filter → group → sort → expand.
/// Top-level entries = solo agents + wf groups; a wf group passes the filter
/// if any child passes and shows only passing children. Auto-expanded while
/// any child is running or failed, auto-collapsed when all done; a manual
/// `wf_open` override wins.
pub fn agent_view_rows(
    agents: &[AgentRec],
    sort: AgentSort,
    filter: AgentFilter,
    wf_open: &HashMap<String, bool>,
    last: u64,
) -> Vec<ARow> {
    struct Entry {
        wf: Option<String>,
        kids: Vec<usize>,
    }
    let mut entries: Vec<Entry> = Vec::new();
    let mut wf_slot: HashMap<String, usize> = HashMap::new();
    for (i, a) in agents.iter().enumerate() {
        if !filter.pass(a) {
            continue;
        }
        match &a.wf {
            None => entries.push(Entry {
                wf: None,
                kids: vec![i],
            }),
            Some(w) => match wf_slot.get(w) {
                Some(&ei) => entries[ei].kids.push(i),
                None => {
                    wf_slot.insert(w.clone(), entries.len());
                    entries.push(Entry {
                        wf: Some(w.clone()),
                        kids: vec![i],
                    });
                }
            },
        }
    }
    // group keys: running any-child, turn0 max, end max, tok sum, launch min
    let key = |e: &Entry| -> (bool, u64, u64, u64, u64) {
        let running = e.kids.iter().any(|&i| agents[i].state == "running");
        let t0max = e.kids.iter().map(|&i| agents[i].turn0).max().unwrap_or(0);
        let endmax = e
            .kids
            .iter()
            .map(|&i| agent_end(&agents[i], last))
            .max()
            .unwrap_or(0);
        let tok: u64 = e.kids.iter().map(|&i| agents[i].own_tok).sum();
        let t0min = e.kids.iter().map(|&i| agents[i].turn0).min().unwrap_or(0);
        (running, t0max, endmax, tok, t0min)
    };
    match sort {
        AgentSort::Recent => entries.sort_by(|a, b| {
            let (ar, at0, aend, _, _) = key(a);
            let (br, bt0, bend, _, _) = key(b);
            match (ar, br) {
                (true, false) => std::cmp::Ordering::Less,
                (false, true) => std::cmp::Ordering::Greater,
                (true, true) => bt0.cmp(&at0),
                (false, false) => bend.cmp(&aend),
            }
        }),
        AgentSort::Tok => entries.sort_by(|a, b| key(b).3.cmp(&key(a).3)),
        AgentSort::Launch => entries.sort_by(|a, b| key(a).4.cmp(&key(b).4)),
    }
    let mut rows: Vec<ARow> = Vec::new();
    for e in entries {
        match e.wf {
            None => rows.push(ARow::Ag {
                idx: e.kids[0],
                child: false,
            }),
            Some(wf) => {
                let mut kids = e.kids;
                kids.sort_by_key(|&i| agents[i].turn0);
                let auto = kids
                    .iter()
                    .any(|&i| matches!(agents[i].state.as_str(), "running" | "failed"));
                let expanded = wf_open.get(&wf).copied().unwrap_or(auto);
                rows.push(ARow::Wf {
                    wf,
                    kids: kids.clone(),
                    expanded,
                });
                if expanded {
                    for i in kids {
                        rows.push(ARow::Ag { idx: i, child: true });
                    }
                }
            }
        }
    }
    rows
}

/// FIXED log token bar: 1k → empty edge, 10k → ⅓, 100k → ⅔, 1M → full.
/// `own_tok < 1k` shows a single `▏`. Never autoscaled.
pub fn agent_tok_bar(tok: u64, width: usize) -> String {
    if tok < 1000 {
        return "▏".to_string();
    }
    let frac = ((tok as f64).log10() - 3.0) / 3.0;
    let bar = eighth_bar(frac.clamp(0.0, 1.0), width);
    // monotonic across the 1k edge: 1000 must not render EMPTIER than 999
    if bar.trim_end().is_empty() {
        return "▏".to_string();
    }
    bar
}

/// LOAD strip (2 rows): x = shared turn axis, y = agents alive per turn on a
/// FIXED 0–8 half-block scale. Steel history · cyan where a running agent is
/// alive (blink-dimmed) · white top half-cell above 8 (overload cap) · red
/// top half-cell at a failed agent's end turn (notch wins).
pub fn render_agent_load_strip(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width as usize <= TURNS_GUTTER + 1 || area.height < 2 {
        return;
    }
    let agents = eff_agents(st);
    let w_turns = area.width as usize - TURNS_GUTTER;
    let last = st.last_turn().unwrap_or(0);
    let t_lo = last.saturating_sub(w_turns as u64 - 1);
    let cursor_turn = ui.cursor.unwrap_or(last);

    // per-column: (filled half-rows 0..=4, base color, cap color override)
    let col_info = |turn: u64| -> (usize, (u8, u8, u8), Option<(u8, u8, u8)>) {
        let mut alive = 0usize;
        let mut running_here = false;
        let mut failed_end = false;
        for a in agents {
            let end = agent_end(a, last);
            if a.turn0 <= turn && turn <= end {
                alive += 1;
                if a.state == "running" {
                    running_here = true;
                }
            }
            if end == turn && a.state == "failed" {
                failed_end = true;
            }
        }
        let half = alive.min(8).div_ceil(2);
        let base = if running_here {
            scale(C_CYAN, if ui.blink { 1.0 } else { 0.55 })
        } else {
            C_STEEL
        };
        let cap = if failed_end && half > 0 {
            Some(C_RED) // notch wins over the overload cap
        } else if alive > 8 {
            Some(C_WHITE)
        } else {
            None
        };
        (half, base, cap)
    };

    let mut lines: Vec<Line<'static>> = Vec::with_capacity(2);
    for row in 0..2usize {
        let mut spans: Vec<Span<'static>> = Vec::with_capacity(w_turns + 1);
        let g = if row == 0 { "8+" } else { "0" };
        spans.push(Span::styled(format!("{g:>5} "), fg(C_DIM)));
        // char row 0 covers half-rows 3(top)/2, row 1 covers 1/0
        let ht = 3 - 2 * row; // top half-row index (from bottom)
        let hb = ht - 1;
        for c in 0..w_turns {
            let turn = t_lo + c as u64;
            let (half, base, cap) = col_info(turn);
            let color_at = |h: usize| -> Option<Color> {
                if h >= half {
                    return None;
                }
                if h + 1 == half {
                    if let Some(capc) = cap {
                        return Some(rgb(capc));
                    }
                }
                Some(rgb(base))
            };
            let mut span = compose(color_at(ht), color_at(hb));
            if turn == cursor_turn {
                span = span.patch_style(Style::default().add_modifier(Modifier::REVERSED));
            }
            spans.push(span);
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// View parameters for the AGENTS ledger, owned by the App.
pub struct AgentsView<'a> {
    pub sel: usize,
    pub sort: AgentSort,
    pub filter: AgentFilter,
    pub wf_open: &'a HashMap<String, bool>,
    /// grid of per-agent context mini-maps (default) ⇄ numeric ledger (`v`)
    pub grid: bool,
}

// ---------------------------------------------------------------------------
// AGENTS — grid of per-agent context mini-maps (SPEC b `agent.map`)
// ---------------------------------------------------------------------------

/// Minimum legible mini-map cell: `MIN_CELL_W` wide (label + a few map cols)
/// × `MIN_CELL_H` tall (1 label row + ≥2 map rows).
const MIN_CELL_W: usize = 12;
const MIN_CELL_H: usize = 3;

/// Autosize the grid: pick `cols` ≈ round(√(n · aspect)) where `aspect =
/// w/(2h)` corrects for the ~1:2 terminal cell (a char is roughly twice as
/// tall as wide) so mini-maps read square, then clamp to what fits at
/// `MIN_CELL_*`. Pure function of n + pane size (test-assertable).
pub fn grid_dims(n: usize, w: u16, h: u16) -> (usize, usize) {
    let w = (w as usize).max(1);
    let h = (h as usize).max(1);
    let n = n.max(1);
    let max_cols = (w / MIN_CELL_W).max(1);
    let max_rows = (h / MIN_CELL_H).max(1);
    let aspect = w as f64 / (2.0 * h as f64);
    let ideal = ((n as f64) * aspect).sqrt().round() as usize;
    let cols = ideal.clamp(1, max_cols);
    let rows = n.div_ceil(cols).clamp(1, max_rows);
    // rows capped by height → widen cols to still cover as many as fit, then
    // re-derive rows so the last row is never wholly empty
    let cols = n.div_ceil(rows).clamp(1, max_cols);
    let rows = n.div_ceil(cols).clamp(1, max_rows);
    (cols, rows)
}

/// state glyph + color for an agent (mirrors the ledger's glyph law).
fn agent_state_glyph(a: &AgentRec, ui: &Ui) -> (&'static str, (u8, u8, u8)) {
    match a.state.as_str() {
        "running" => ("●", scale(C_CYAN, if ui.blink { 1.0 } else { 0.55 })),
        "done" => ("○", C_GREEN),
        "failed" => ("✖", C_RED),
        _ => ("·", C_DIM),
    }
}

fn agent_state_color(a: &AgentRec) -> (u8, u8, u8) {
    match a.state.as_str() {
        "running" => C_CYAN,
        "done" => C_GREEN,
        "failed" => C_RED,
        _ => C_DIM,
    }
}

/// One per-agent mini-map on the FIXED budget rung (headroom = dim dots),
/// pinned to CLASS mode — the stripped agent segs carry no ts/born/waterline,
/// so heat/age/cache would degenerate. Reuses the shared cell core.
fn render_agent_minimap(m: &MapMsg, fallback_budget: u64, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 || m.segs.is_empty() {
        return;
    }
    let segs = &m.segs;
    let budget = m.budget.unwrap_or(fallback_budget).max(1);
    let w = area.width as usize;
    let h = area.height as usize;
    let capacity = w * 2 * h; // half-block cells (class mode)
    let s_tok = pick_rung(budget, capacity, 0);
    let n_cells = (budget.div_ceil(s_tok) as usize).min(capacity);
    let owners = cell_owners(segs, s_tok, n_cells);
    render_map_cells(f, area, segs, s_tok, n_cells, &owners, false, 0, |owner, _addr| {
        agent_cell_px(segs, owner)
    });
}

/// One grid cell: a 1-line label (`glyph type own · desc`, state-colored) over
/// the agent's mini-map (or a dim "no map" body). Selected cell's label is
/// reversed so it's unmistakable which agent is active.
fn render_agent_map_cell(
    st: &State,
    ui: &Ui,
    agents: &[AgentRec],
    row: &ARow,
    selected: bool,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let [label_a, map_a] =
        Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).areas(area);
    let w = label_a.width as usize;

    // ---- label ------------------------------------------------------------
    let mut spans: Vec<Span<'static>> = Vec::new();
    let (map, budget): (Option<&MapMsg>, u64) = match row {
        ARow::Ag { idx, child } => {
            let a = &agents[*idx];
            let (glyph, gcol) = agent_state_glyph(a, ui);
            let scol = agent_state_color(a);
            let ty = a.agent_type.clone().unwrap_or_else(|| "agent".into());
            let own = fmt_k1(a.own_tok);
            let prefix = if *child { "·" } else { glyph };
            spans.push(Span::styled(format!("{prefix} "), fg(gcol)));
            let mut used = 2usize;
            let ty_room = w.saturating_sub(used + 1 + own.chars().count());
            let ty_s = head_trunc(&ty, ty_room.max(1));
            used += ty_s.chars().count();
            spans.push(Span::styled(ty_s, fg(scol).add_modifier(Modifier::BOLD)));
            spans.push(Span::styled(format!(" {own}"), fg(C_FG)));
            used += 1 + own.chars().count();
            if let Some(d) = &a.desc {
                let room = w.saturating_sub(used + 3);
                if room >= 3 && !d.is_empty() {
                    spans.push(Span::styled(
                        format!(" · {}", head_trunc(d, room)),
                        fg(C_DIM),
                    ));
                }
            }
            (
                a.map.as_ref(),
                a.map.as_ref().and_then(|m| m.budget).unwrap_or(st.budget),
            )
        }
        ARow::Wf { wf, kids, expanded } => {
            let glyph = if *expanded { "▾" } else { "▸" };
            let krun = kids
                .iter()
                .filter(|&&i| agents[i].state == "running")
                .count();
            let kfail = kids
                .iter()
                .filter(|&&i| agents[i].state == "failed")
                .count();
            let head = format!("{glyph} {wf} ×{}", kids.len());
            spans.push(Span::styled(
                head_trunc(&head, w.saturating_sub(6).max(1)),
                fg(C_DIM).add_modifier(Modifier::BOLD),
            ));
            spans.push(Span::styled(format!(" {krun}●"), fg(C_CYAN)));
            spans.push(Span::styled(format!(" {kfail}✖"), fg(C_RED)));
            (None, st.budget)
        }
    };
    let mut label = Line::from(spans);
    if selected {
        label = label.patch_style(Style::default().add_modifier(Modifier::REVERSED));
    }
    f.render_widget(Paragraph::new(label), label_a);

    // ---- body: mini-map, or a "no map" / "workflow" placeholder ------------
    if map_a.height == 0 || map_a.width == 0 {
        return;
    }
    match map {
        Some(m) if !m.segs.is_empty() => render_agent_minimap(m, budget, f, map_a),
        _ => {
            let msg = if matches!(row, ARow::Wf { .. }) {
                "workflow"
            } else {
                "no map"
            };
            let mid = Rect {
                y: map_a.y + map_a.height / 2,
                height: 1,
                ..map_a
            };
            f.render_widget(
                Paragraph::new(Line::styled(msg, fg(C_GRID))).alignment(Alignment::Center),
                mid,
            );
        }
    }
}

/// The AGENTS-tab centerpiece: an autosized, ledger-sorted GRID of per-agent
/// context mini-maps. One cell per ledger row (agent or wf rollup), so the
/// grid respects the `s` sort, the `a` filter and wf expansion exactly like
/// the ledger; `sel` (the same index) highlights one cell. Pages when there
/// are more cells than fit (tiny non-compact sizes).
fn render_agent_maps_grid(
    st: &State,
    ui: &Ui,
    agents: &[AgentRec],
    rows: &[ARow],
    sel: usize,
    f: &mut Frame<'_>,
    area: Rect,
) {
    let n = rows.len();
    if area.width == 0 || area.height == 0 {
        return;
    }
    if n == 0 {
        f.render_widget(
            Paragraph::new(Line::styled(" no agents", fg(C_DIM))),
            area,
        );
        return;
    }
    let (cols, grows) = grid_dims(n, area.width, area.height);
    let cap = (cols * grows).max(1);
    let sel = sel.min(n - 1);
    let start = (sel / cap) * cap; // page containing the selection
    let end = (start + cap).min(n);
    let cell_w = (area.width / cols as u16).max(1);
    let cell_h = (area.height / grows as u16).max(1);
    for (vi, ri) in (start..end).enumerate() {
        let gc = (vi % cols) as u16;
        let gr = (vi / cols) as u16;
        let x = area.x + gc * cell_w;
        let y = area.y + gr * cell_h;
        if x >= area.right() || y >= area.bottom() {
            continue;
        }
        // last column / row absorb the remainder so the grid fills the pane;
        // reserve a 1-col gutter between columns for visual separation
        let right = if gc + 1 == cols as u16 {
            area.right()
        } else {
            (x + cell_w).min(area.right())
        };
        let bottom = if gr + 1 == grows as u16 {
            area.bottom()
        } else {
            (y + cell_h).min(area.bottom())
        };
        let cw = right.saturating_sub(x).saturating_sub(1).max(1);
        let ch = bottom.saturating_sub(y).max(1);
        let cell = Rect {
            x,
            y,
            width: cw,
            height: ch,
        };
        render_agent_map_cell(st, ui, agents, &rows[ri], ri == sel, f, cell);
    }
}

pub fn render_agents_tab(
    st: &State,
    ui: &Ui,
    view: &AgentsView<'_>,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 8 || area.height == 0 {
        return;
    }
    let agents = eff_agents(st);
    let last = st.last_turn().unwrap_or(0);
    let now = ui.now_epoch;
    let compact = ui.tier == Tier::Compact;
    let full = ui.tier == Tier::Full;
    let bar_w: usize = if full { 12 } else { 8 };

    // GRID mode (the new centerpiece): a grid of per-agent context mini-maps.
    // Falls back to the numeric ledger at Compact tier or when the pane is too
    // small for even a single legible cell.
    let grid_mode =
        view.grid && !compact && area.width as usize >= 2 * MIN_CELL_W && area.height >= 8;
    let strip_h: u16 = if !compact && area.height >= 7 { 2 } else { 0 };
    // the table column-header only applies to the ledger fallback
    let colhdr_h: u16 = if !grid_mode && !compact && area.height >= 4 { 1 } else { 0 };
    let detail_h: u16 = if full && area.height >= 12 { 1 } else { 0 };
    let footer_h: u16 = if !compact && area.height >= 5 { 1 } else { 0 };
    let [hdr, strip, colhdr, list, detail, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(strip_h),
        Constraint::Length(colhdr_h),
        Constraint::Min(0),
        Constraint::Length(detail_h),
        Constraint::Length(footer_h),
    ])
    .areas(area);

    // header: totals from ALL agents, unfiltered
    let running = agents.iter().filter(|a| a.state == "running").count();
    let done = agents.iter().filter(|a| a.state == "done").count();
    let failed = agents.iter().filter(|a| a.state == "failed").count();
    let own_total: u64 = agents.iter().map(|a| a.own_tok).sum();
    let ratio = own_total as f64 / st.resident.max(1) as f64;
    let mut hspans: Vec<Span<'static>> = vec![Span::styled(
        format!("fan-out {} ", fmt_k0(own_total)),
        fg(C_FG).add_modifier(Modifier::BOLD),
    )];
    if !compact {
        hspans.push(Span::styled(format!("≡ {ratio:.2}× main "), fg(C_DIM)));
    }
    hspans.push(Span::styled("· ".to_string(), fg(C_GRID)));
    hspans.push(Span::styled(format!("{running}●"), fg(C_CYAN)));
    if full {
        hspans.push(Span::styled(format!(" {done}○"), fg(C_DIM)));
    }
    hspans.push(Span::styled(format!(" {failed}✖"), fg(C_RED)));
    hspans.push(Span::styled(
        format!(" · sort:{}", view.sort.label()),
        fg(C_DIM),
    ));
    if view.filter != AgentFilter::All {
        hspans.push(Span::styled(
            format!(" · filter:{}", view.filter.label()),
            fg(C_AMBER),
        ));
    }
    f.render_widget(Paragraph::new(Line::from(hspans)), hdr);

    if strip_h > 0 {
        render_agent_load_strip(st, ui, f, strip);
    }

    if colhdr_h > 0 {
        let h = if full {
            format!(
                "   {:<w$}{:>8} {:>8} {:>6} {:>13} {:>7}  agent",
                "own-tok(log)",
                "own-tok",
                "ret-tok",
                "amp",
                "tools(r/s/b/e)",
                "dur",
                w = bar_w + 1
            )
        } else {
            format!(
                "   {:<w$}{:>8} {:>8} {:>6} {:>7}  agent",
                "own(log)",
                "own-tok",
                "ret-tok",
                "amp",
                "dur",
                w = bar_w + 1
            )
        };
        f.render_widget(Paragraph::new(Line::styled(h, fg(C_DIM))), colhdr);
    }

    // unified ledger row sequence (shared by the grid and the fallback list —
    // the grid renders one mini-map cell per row, so it respects the same
    // sort/filter/wf-expansion and the same `sel` index)
    let rows = agent_view_rows(agents, view.sort, view.filter, view.wf_open, last);
    let sel = view.sel.min(rows.len().saturating_sub(1));
    if grid_mode && list.height > 0 {
        render_agent_maps_grid(st, ui, agents, &rows, sel, f, list);
    } else if list.height > 0 {
        let visible = list.height as usize;
        let scroll = sel.saturating_sub(visible.saturating_sub(1));
        let mut lines: Vec<Line<'static>> = Vec::new();
        if rows.is_empty() {
            lines.push(Line::styled(" no agents", fg(C_DIM)));
        }
        for (ri, row) in rows.iter().enumerate().skip(scroll).take(visible) {
            let selected = ri == sel;
            let mut spans: Vec<Span<'static>> = Vec::new();
            match row {
                ARow::Wf { wf, kids, expanded } => {
                    let glyph = if *expanded { "▾" } else { "▸" };
                    spans.push(Span::styled(
                        format!(" {glyph} "),
                        fg(C_DIM).add_modifier(Modifier::BOLD),
                    ));
                    let own: u64 = kids.iter().map(|&i| agents[i].own_tok).sum();
                    let krun = kids
                        .iter()
                        .filter(|&&i| agents[i].state == "running")
                        .count();
                    let kfail = kids
                        .iter()
                        .filter(|&&i| agents[i].state == "failed")
                        .count();
                    let bar_col = if krun > 0 { C_CYAN } else { C_GREEN };
                    push_bar(&mut spans, own, bar_w, bar_col, 1.0, false);
                    spans.push(Span::styled(format!("{:>8}", fmt_k1(own)), fg(C_FG)));
                    let ret_kn: u64 = kids
                        .iter()
                        .filter_map(|&i| agents[i].ret_tok)
                        .sum();
                    let any_ret = kids.iter().any(|&i| agents[i].ret_tok.is_some());
                    if !compact {
                        spans.push(Span::styled(
                            format!(
                                " {:>8}",
                                if any_ret { fmt_k1(ret_kn) } else { "—".into() }
                            ),
                            fg(C_FG),
                        ));
                    }
                    let amp = if any_ret && ret_kn > 0 {
                        format!("{:.1}×", own as f64 / ret_kn as f64)
                    } else {
                        "—".into()
                    };
                    spans.push(Span::styled(format!(" {amp:>6}"), fg(C_FG)));
                    if full {
                        let (mut r, mut s, mut b2, mut e) = (0u64, 0u64, 0u64, 0u64);
                        for &i in kids {
                            let tl = agents[i].tools.unwrap_or_default();
                            r += tl.r;
                            s += tl.s;
                            b2 += tl.b;
                            e += tl.e;
                        }
                        spans.push(Span::styled(
                            format!(" {:>13}", format!("{r}/{s}/{b2}/{e}")),
                            fg(C_FG),
                        ));
                    }
                    if !compact {
                        // dur = max child dur (live elapsed for running kids)
                        let dmax = kids
                            .iter()
                            .filter_map(|&i| agent_dur_ms(&agents[i], now))
                            .max();
                        spans.push(Span::styled(
                            format!(
                                " {:>7}",
                                dmax.map(fmt_dur).unwrap_or_else(|| "—".into())
                            ),
                            fg(C_FG),
                        ));
                    }
                    spans.push(Span::styled(
                        format!("  {wf} ×{}", kids.len()),
                        fg(C_DIM).add_modifier(Modifier::BOLD),
                    ));
                    spans.push(Span::styled(format!(" {krun}●"), fg(C_CYAN)));
                    spans.push(Span::styled(format!(" {kfail}✖"), fg(C_RED)));
                }
                ARow::Ag { idx, child } => {
                    let a = &agents[*idx];
                    let (glyph, gcol) = match a.state.as_str() {
                        "running" => (
                            "●",
                            scale(C_CYAN, if ui.blink { 1.0 } else { 0.55 }),
                        ),
                        "done" => ("○", C_GREEN),
                        "failed" => ("✖", C_RED),
                        _ => ("·", C_DIM),
                    };
                    spans.push(Span::styled(format!(" {glyph} "), fg(gcol)));
                    let state_col = match a.state.as_str() {
                        "running" => C_CYAN,
                        "done" => C_GREEN,
                        "failed" => C_RED,
                        _ => C_DIM,
                    };
                    // bar brightness = activity heat for running agents:
                    // producing glows, wedged fades; old engines (ts_last=0)
                    // render the un-heated base color
                    let k = if a.state == "running" && a.ts_last > 0.0 {
                        heat_k(now - a.ts_last)
                    } else {
                        1.0
                    };
                    let pulsing = st.agent_pulse.get(&a.id).copied().unwrap_or(0) > 0;
                    push_bar(&mut spans, a.own_tok, bar_w, state_col, k, pulsing);
                    spans.push(Span::styled(
                        format!("{:>8}", fmt_k1(a.own_tok)),
                        if pulsing { fg(C_WHITE) } else { fg(C_FG) },
                    ));
                    if !compact {
                        spans.push(Span::styled(
                            format!(
                                " {:>8}",
                                a.ret_tok.map(fmt_k1).unwrap_or_else(|| "—".into())
                            ),
                            fg(C_FG),
                        ));
                    }
                    let amp = if a.state == "running" {
                        "—".to_string()
                    } else {
                        match a.ret_tok {
                            Some(rt) => {
                                format!("{:.1}×", a.own_tok as f64 / rt.max(1) as f64)
                            }
                            None => "—".into(),
                        }
                    };
                    spans.push(Span::styled(format!(" {amp:>6}"), fg(C_FG)));
                    if full {
                        let tl = a.tools.unwrap_or_default();
                        spans.push(Span::styled(
                            format!(" {:>13}", format!("{}/{}/{}/{}", tl.r, tl.s, tl.b, tl.e)),
                            fg(C_FG),
                        ));
                    }
                    if !compact {
                        spans.push(Span::styled(
                            format!(
                                " {:>7}",
                                agent_dur_ms(a, now)
                                    .map(fmt_dur)
                                    .unwrap_or_else(|| "—".into())
                            ),
                            fg(C_FG),
                        ));
                    }
                    let prefix = if *child { "· " } else { "" };
                    spans.push(Span::styled(
                        format!(
                            "  {prefix}{}",
                            a.agent_type.clone().unwrap_or_else(|| "agent".into())
                        ),
                        fg(C_DIM).add_modifier(Modifier::BOLD),
                    ));
                    if let Some(d) = &a.desc {
                        if !d.is_empty() {
                            spans.push(Span::styled(format!(" · {d}"), fg(C_DIM)));
                        }
                    }
                }
            }
            let mut line = Line::from(spans);
            if selected {
                line = line.patch_style(Style::default().add_modifier(Modifier::REVERSED));
            }
            lines.push(line);
        }
        f.render_widget(Paragraph::new(lines), list);
    }

    // detail line (Full, ≥12 rows)
    if detail_h > 0 {
        let line = match rows.get(sel) {
            Some(ARow::Ag { idx, .. }) => {
                let a = &agents[*idx];
                // the numeric ledger stays accessible on the detail line for
                // the SELECTED agent (own/ret/amp/tools), since the grid cell
                // shows only own-tok
                let amp = match (a.state.as_str(), a.ret_tok) {
                    ("running", _) | (_, None) => "—".to_string(),
                    (_, Some(rt)) => format!("{:.1}×", a.own_tok as f64 / rt.max(1) as f64),
                };
                let tl = a.tools.unwrap_or_default();
                Line::styled(
                    format!(
                        "sel {} · {} · \"{}\" · own {} · ret {} · amp {} · {}r/{}s/{}b/{}e · t{}→{}",
                        a.id,
                        a.wf.clone().unwrap_or_else(|| "solo".into()),
                        a.desc.clone().unwrap_or_default(),
                        fmt_k1(a.own_tok),
                        a.ret_tok.map(fmt_k1).unwrap_or_else(|| "—".into()),
                        amp,
                        tl.r,
                        tl.s,
                        tl.b,
                        tl.e,
                        a.turn0,
                        a.turn1
                            .map(|t| format!("t{t}"))
                            .unwrap_or_else(|| "…".into()),
                    ),
                    fg(C_DIM),
                )
            }
            Some(ARow::Wf { wf, kids, .. }) => Line::styled(
                format!("sel {wf} · ×{} children", kids.len()),
                fg(C_DIM),
            ),
            None => Line::from(""),
        };
        f.render_widget(Paragraph::new(line), detail);
    }

    if footer_h > 0 {
        let line = match eff_tasks(st) {
            Some(t) => Line::from(vec![
                Span::styled(format!("tasks {}/{}", t.done, t.total), fg(C_FG)),
                Span::styled(
                    match &t.active {
                        Some(a) => format!(" ▸ \"{a}\""),
                        None => String::new(),
                    },
                    fg(C_DIM),
                ),
            ]),
            None => Line::styled("tasks —", fg(C_DIM)),
        };
        f.render_widget(Paragraph::new(line), footer);
    }
}

/// dur cell value in ms: finished → `dur_ms`; running → live `now − t0`
/// (None when `t0` unknown → `—`).
fn agent_dur_ms(a: &AgentRec, now: f64) -> Option<u64> {
    if a.state == "running" {
        if a.t0 > 0.0 {
            Some(((now - a.t0).max(0.0) * 1000.0) as u64)
        } else {
            None
        }
    } else {
        a.dur_ms
    }
}

/// Push the fixed-log token bar (padded to `bar_w`+1) with state color ×
/// heat brightness; the bar tip flashes white while the growth pulse lives.
fn push_bar(
    spans: &mut Vec<Span<'static>>,
    tok: u64,
    bar_w: usize,
    color: (u8, u8, u8),
    k: f64,
    pulse: bool,
) {
    let bar = agent_tok_bar(tok, bar_w);
    let chars: Vec<char> = bar.chars().collect();
    let n = chars.len();
    let style = fg(scale(color, k));
    if pulse && n > 0 {
        let body: String = chars[..n - 1].iter().collect();
        spans.push(Span::styled(body, style));
        spans.push(Span::styled(chars[n - 1].to_string(), fg(C_WHITE)));
    } else {
        spans.push(Span::styled(bar, style));
    }
    spans.push(Span::raw(" ".repeat(bar_w + 1 - n.min(bar_w + 1))));
}

// ---------------------------------------------------------------------------
// EVENTS — ledger
// ---------------------------------------------------------------------------

pub fn event_glyph(kind: &str) -> &'static str {
    match kind {
        "compaction" => "▼",
        "api_error" => "✖",
        "model_fallback" | "model_switch" => "⇄",
        "hook_block" => "⚑",
        "queued_prompt" => "◇",
        "pressure" | "thrash" | "stall" => "▲",
        "agent_failed" => "✖",
        "attach" => "»",
        _ => "·",
    }
}

pub fn severity_color(sev: Severity) -> (u8, u8, u8) {
    match sev {
        Severity::Error => C_RED,
        Severity::Warn => C_AMBER,
        Severity::Info => C_DIM,
    }
}

pub fn render_events_tab(st: &State, sel: usize, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    // newest first
    let evs: Vec<&crate::ipc::EventRec> = st.events.iter().rev().collect();
    let rows = area.height as usize;
    let scroll = sel.saturating_sub(rows.saturating_sub(1));
    let mut lines: Vec<Line<'static>> = Vec::new();
    if evs.is_empty() {
        lines.push(Line::styled(" no events yet", fg(C_DIM)));
    }
    for i in 0..rows {
        let idx = scroll + i;
        let Some(e) = evs.get(idx) else { break };
        let col = severity_color(e.severity);
        let mut style = fg(col);
        if idx == sel {
            style = style.add_modifier(Modifier::REVERSED);
        }
        lines.push(Line::from(vec![
            Span::styled(format!(" {} ", event_glyph(&e.kind)), style),
            Span::styled(
                format!("{:8} t{:<5} {:<14} ", e.ts, e.turn, e.kind),
                if idx == sel {
                    fg(C_FG).add_modifier(Modifier::REVERSED)
                } else {
                    fg(C_FG)
                },
            ),
            Span::styled(
                e.msg.clone(),
                if idx == sel {
                    fg(C_DIM).add_modifier(Modifier::REVERSED)
                } else {
                    fg(C_DIM)
                },
            ),
        ]));
    }
    f.render_widget(Paragraph::new(lines), area);
}

// ---------------------------------------------------------------------------
// SHELL — the command console (tab 6)
// ---------------------------------------------------------------------------

/// SHELL perspective: CONSOLE (the Bash feed, default) ↔ RETRIEVAL (the
/// agentic-retrieval feed). `v` toggles — the FILES-view idiom.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ShellView {
    Console,
    Retrieval,
}

/// SHELL filter: `all ⇄ err` (`err` = `!ok || interrupted`; for retrievals
/// simply `!ok`). Two states only — this is an instrument, not a pager.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ShellFilter {
    All,
    Err,
}

impl ShellFilter {
    pub fn next(self) -> Self {
        match self {
            ShellFilter::All => ShellFilter::Err,
            ShellFilter::Err => ShellFilter::All,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            ShellFilter::All => "all",
            ShellFilter::Err => "err",
        }
    }
    pub fn pass(self, c: &CmdRec) -> bool {
        match self {
            ShellFilter::All => true,
            ShellFilter::Err => !c.ok || c.interrupted,
        }
    }
    pub fn pass_ret(self, r: &RetRec) -> bool {
        match self {
            ShellFilter::All => true,
            ShellFilter::Err => !r.ok,
        }
    }
}

/// The console's visible entry set (shared with the key handlers): ring
/// order (oldest→newest), minus filter misses, minus `turn > cursor` in
/// replay — scrubbing visibly rewinds the console UI-side, zero wire
/// traffic; future output does not exist yet at turn t.
pub fn shell_visible<'a>(
    st: &'a State,
    filter: ShellFilter,
    cursor: Option<u64>,
) -> Vec<&'a CmdRec> {
    st.cmds
        .iter()
        .filter(|c| filter.pass(c))
        .filter(|c| cursor.map(|t| c.turn <= t).unwrap_or(true))
        .collect()
}

/// The retrieval feed's visible entry set — same laws as the console:
/// ring order, minus filter misses, minus `turn > cursor` in replay.
pub fn ret_visible<'a>(
    st: &'a State,
    filter: ShellFilter,
    cursor: Option<u64>,
) -> Vec<&'a RetRec> {
    st.rets
        .iter()
        .filter(|r| filter.pass_ret(r))
        .filter(|r| cursor.map(|t| r.turn <= t).unwrap_or(true))
        .collect()
}

/// Exit-state mark: `^` interrupted (wins) · `✖` failed · `○` ok.
fn cmd_mark(c: &CmdRec) -> (&'static str, (u8, u8, u8)) {
    if c.interrupted {
        ("^", C_AMBER)
    } else if c.ok {
        ("○", C_GREEN)
    } else {
        ("✖", C_RED)
    }
}

/// tok_out spark+number color — the FILES access-size ramp law (fixed):
/// <1k dim · 1–4k normal · 4–16k amber · ≥16k red BOLD (a command that
/// dumps ≥16k tokens into context screams, which is the point).
fn tok_out_style(tok: u64) -> Style {
    if tok >= 16_000 {
        fg(C_RED).add_modifier(Modifier::BOLD)
    } else if tok >= 4_000 {
        fg(C_AMBER)
    } else if tok >= 1_000 {
        fg(C_FG)
    } else {
        fg(C_DIM)
    }
}

/// Keep the HEAD of a line, truncating the TAIL with `…`. Commands and
/// output read left-to-right — `tail_trunc` (which keeps the tail) would eat
/// the command name, so the console truncates the other way around.
fn head_trunc(s: &str, w: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= w {
        return s.to_string();
    }
    if w == 0 {
        return String::new();
    }
    let head: String = chars[..w - 1].iter().collect();
    format!("{head}…")
}

/// Last non-empty line of an engine-side tail.
fn last_line(s: &str) -> Option<&str> {
    s.lines().rev().find(|l| !l.trim().is_empty())
}

/// Hard-wrap one tail line into `w`-cell chunks (expanded view: wrapped,
/// never truncated).
/// Word-aware wrap: each line breaks at the LAST space that fits the
/// window; mid-word splits happen only when a single token exceeds the
/// width (long paths/URLs). Interior spacing and indentation are preserved
/// byte-for-byte — only the one break space is swallowed. Every popup and
/// lane view wraps through here; the old char-chunking split words across
/// lines ("th/e", "s/ame").
fn wrap_line(s: &str, w: usize) -> Vec<String> {
    if s.is_empty() || w == 0 {
        return vec![String::new()];
    }
    let chars: Vec<char> = s.chars().collect();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        if chars.len() - i <= w {
            out.push(chars[i..].iter().collect());
            break;
        }
        let win = &chars[i..i + w];
        match win.iter().rposition(|&c| c == ' ') {
            Some(p) if p > 0 => {
                out.push(chars[i..i + p].iter().collect());
                i += p + 1; // swallow the break space
            }
            _ => {
                out.push(win.iter().collect());
                i += w;
            }
        }
    }
    if out.is_empty() {
        out.push(String::new());
    }
    out
}

/// Clip an entry from its TOP (tails win), marking the first shown line with
/// a leading dim `…`.
fn clip_top(mut lines: Vec<Line<'static>>, keep: usize) -> Vec<Line<'static>> {
    if lines.len() <= keep {
        return lines;
    }
    let cut = lines.len() - keep;
    let mut kept: Vec<Line<'static>> = lines.drain(cut..).collect();
    if let Some(first) = kept.first_mut() {
        first.spans.insert(0, Span::styled("…".to_string(), fg(C_DIM)));
    }
    kept
}

/// Per-tier console geometry: ts column · text indent (the `$` sits two
/// cells left of it) · inline desc · spark · collapsed output lines.
struct ShellGeom {
    ts: bool,
    text_col: usize,
    desc: bool,
    spark: bool,
    out_lines: usize,
}

fn shell_geom(tier: Tier) -> ShellGeom {
    match tier {
        Tier::Full => ShellGeom { ts: true, text_col: 13, desc: true, spark: true, out_lines: 2 },
        Tier::Medium => ShellGeom { ts: false, text_col: 4, desc: false, spark: false, out_lines: 1 },
        Tier::Compact => ShellGeom { ts: false, text_col: 4, desc: false, spark: false, out_lines: 0 },
    }
}

/// One console entry as lines: prompt (`ts mark $ cmd [— desc] spark tok`),
/// then output tails — stdout dim ×0.72 at the text indent, stderr behind a
/// red `▎` gutter. Expanded adds the `# desc` comment line, the FULL wrapped
/// tails and a trailing amber `^C` on interrupted entries.
fn shell_entry_lines(
    st: &State,
    ui: &Ui,
    c: &CmdRec,
    expanded: bool,
    selected: bool,
    width: usize,
) -> Vec<Line<'static>> {
    let g = shell_geom(ui.tier);
    let text_w = width.saturating_sub(g.text_col).max(1);
    let mut lines: Vec<Line<'static>> = Vec::new();

    // expanded: desc as a shell comment above the prompt line
    if expanded {
        if let Some(d) = c.desc.as_ref().filter(|d| !d.is_empty()) {
            lines.push(Line::styled(
                format!("{}# {}", " ".repeat(g.text_col.saturating_sub(2)), d),
                fg(C_DIM),
            ));
        }
    }

    // prompt line. Arrival pulse: white-blend while cmd_pulse names this seq
    // (live only — arrivals are always > the cursor, so replay never blends).
    let blend = if st.cmd_pulse > 0 && st.cmd_pulse_seq == c.seq && ui.cursor.is_none() {
        st.cmd_pulse as f64 / 6.0 * 0.6
    } else {
        0.0
    };
    let bl = |col: (u8, u8, u8)| -> Style {
        if blend > 0.0 {
            fg(lerp(col, C_WHITE, blend))
        } else {
            fg(col)
        }
    };
    let (mark, mcol) = cmd_mark(c);
    let mut spans: Vec<Span<'static>> = Vec::new();
    if g.ts {
        spans.push(Span::styled(format!("{:8} ", c.ts), fg(C_DIM)));
    }
    spans.push(Span::styled(mark.to_string(), bl(mcol)));
    spans.push(Span::styled(" $ ".to_string(), bl(C_BASH)));
    let right_block = if g.spark { 7 } else { 6 }; // spark + 6-wide tok_out
    let avail = width.saturating_sub(g.text_col + right_block + 1);
    let suffix = if c.bg { 2 } else { 0 }; // ` &` survives truncation
    let cmd_shown = head_trunc(&c.cmd, avail.saturating_sub(suffix));
    let cmd_fit = cmd_shown == c.cmd;
    let mut used = cmd_shown.chars().count();
    spans.push(Span::styled(cmd_shown, bl(C_FG)));
    if c.bg {
        spans.push(Span::styled(" &".to_string(), bl(C_CYAN)));
        used += 2;
    }
    // inline desc: Full only, only if the whole cmd fit and ≥12 cells remain
    if g.desc && !expanded && cmd_fit {
        if let Some(d) = c.desc.as_ref().filter(|d| !d.is_empty()) {
            let rem = avail.saturating_sub(used);
            if rem >= 12 {
                let dtxt = head_trunc(&format!(" — {d}"), rem);
                used += dtxt.chars().count();
                spans.push(Span::styled(dtxt, fg(C_DIM)));
            }
        }
    }
    let pad = width.saturating_sub(g.text_col + used + right_block);
    spans.push(Span::raw(" ".repeat(pad)));
    let tstyle = tok_out_style(c.tok_out);
    if g.spark {
        spans.push(Span::styled(
            spark_char(c.tok_out as f64, 16_000.0).to_string(),
            tstyle,
        ));
    }
    spans.push(Span::styled(format!("{:>6}", fmt_k1(c.tok_out)), tstyle));
    let mut prompt = Line::from(spans);
    if selected {
        prompt = prompt.patch_style(Style::default().add_modifier(Modifier::REVERSED));
    }
    lines.push(prompt);

    // output tails — no heat-dimming ever: transcripts must stay readable
    let out_style = fg(scale(C_FG, 0.72));
    let err_style = fg(scale(C_RED, 0.9));
    let gut = g.text_col.saturating_sub(2);
    let out_line = |txt: String| -> Line<'static> {
        Line::styled(format!("{}{txt}", " ".repeat(g.text_col)), out_style)
    };
    let err_line = |txt: String| -> Line<'static> {
        Line::from(vec![
            Span::raw(" ".repeat(gut)),
            Span::styled("▎ ".to_string(), fg(C_RED)),
            Span::styled(txt, err_style),
        ])
    };
    if expanded {
        for l in c.out.lines() {
            for chunk in wrap_line(l, text_w) {
                lines.push(out_line(chunk));
            }
        }
        for l in c.err.lines() {
            for chunk in wrap_line(l, text_w) {
                lines.push(err_line(chunk));
            }
        }
        if c.interrupted {
            lines.push(Line::styled(
                format!("{}^C", " ".repeat(g.text_col)),
                fg(C_AMBER),
            ));
        }
    } else {
        match g.out_lines {
            // Full: last stdout line, then last stderr line — stderr
            // closest to the exit, as in a real terminal
            2 => {
                if let Some(l) = last_line(&c.out) {
                    lines.push(out_line(head_trunc(l, text_w)));
                }
                if let Some(l) = last_line(&c.err) {
                    lines.push(err_line(head_trunc(l, text_w)));
                }
            }
            // Medium: one line, stderr preferred
            1 => {
                if let Some(l) = last_line(&c.err) {
                    lines.push(err_line(head_trunc(l, text_w)));
                } else if let Some(l) = last_line(&c.out) {
                    lines.push(out_line(head_trunc(l, text_w)));
                }
            }
            _ => {}
        }
    }
    lines
}

/// Header: `$ N cmds · ✖E · ^I · &B · filter all|err` left; posture right
/// (`● FOLLOW` blink / `↑ +N newer` / `« console @ t=N`).
#[allow(clippy::too_many_arguments)]
fn render_shell_header(
    st: &State,
    ui: &Ui,
    follow: bool,
    sel: u64,
    filter: ShellFilter,
    vis: &[&CmdRec],
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let full = ui.tier == Tier::Full;
    let n = st.cmds.len();
    let e = st.cmds.iter().filter(|c| !c.ok && !c.interrupted).count();
    let i = st.cmds.iter().filter(|c| c.interrupted).count();
    let b = st.cmds.iter().filter(|c| c.bg).count();
    let mut spans: Vec<Span<'static>> = vec![
        Span::styled("$ ".to_string(), fg(C_BASH).add_modifier(Modifier::BOLD)),
        Span::styled(format!("{n} cmds"), fg(C_FG).add_modifier(Modifier::BOLD)),
        Span::styled(" · ".to_string(), fg(C_GRID)),
        Span::styled(format!("✖{e}"), if e > 0 { fg(C_RED) } else { fg(C_DIM) }),
    ];
    if full && i > 0 {
        spans.push(Span::styled(" · ".to_string(), fg(C_GRID)));
        spans.push(Span::styled(format!("^{i}"), fg(C_AMBER)));
    }
    if full && b > 0 {
        spans.push(Span::styled(" · ".to_string(), fg(C_GRID)));
        spans.push(Span::styled(format!("&{b}"), fg(C_CYAN)));
    }
    spans.push(Span::styled(" · ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!("filter {}", filter.label()),
        if filter == ShellFilter::Err {
            fg(C_AMBER)
        } else {
            fg(C_DIM)
        },
    ));
    let newer = vis.iter().filter(|c| c.seq > sel).count();
    let (rtxt, rstyle) = feed_posture(ui, follow, newer, "console");
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let pad = (area.width as usize).saturating_sub(used + rtxt.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(rtxt, rstyle));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Header posture, shared by both SHELL perspectives:
/// replay > follow (blink-dimmed like ● LIVE) > browsing.
fn feed_posture(ui: &Ui, follow: bool, newer: usize, what: &str) -> (String, Style) {
    if let Some(t) = ui.cursor {
        (
            format!("« {what} @ t={t}"),
            fg(C_AMBER).add_modifier(Modifier::BOLD),
        )
    } else if follow {
        let style = if ui.blink {
            fg(C_GREEN).add_modifier(Modifier::BOLD)
        } else {
            fg(scale(C_GREEN, 0.6))
        };
        ("● FOLLOW".to_string(), style)
    } else {
        (
            format!("↑ +{newer} newer"),
            fg(C_AMBER).add_modifier(Modifier::BOLD),
        )
    }
}

/// Tab 6 SHELL — the Bash feed as the terminal Claude never shows you,
/// priced in context tokens. Newest at BOTTOM, tail-pinned follow; entries
/// laid out bottom-up from the anchor (newest when following, the selected
/// seq when browsing — NOTHING renders below the anchor, so arrivals can
/// never shift a seq-anchored selection).
#[allow(clippy::too_many_arguments)]
pub fn render_shell_tab(
    st: &State,
    ui: &Ui,
    follow: bool,
    sel: u64,
    expand: bool,
    filter: ShellFilter,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 12 || area.height == 0 {
        return;
    }
    let compact = ui.tier == Tier::Compact;
    let header_h: u16 = if compact { 0 } else { 1 };
    let [hdr, console] =
        Layout::vertical([Constraint::Length(header_h), Constraint::Min(0)]).areas(area);

    let vis = shell_visible(st, filter, ui.cursor);
    if header_h > 0 {
        render_shell_header(st, ui, follow, sel, filter, &vis, f, hdr);
    }
    if console.height == 0 {
        return;
    }
    if vis.is_empty() {
        let msg = match ui.cursor {
            Some(t) => format!("no commands ≤ t={t}"),
            None => "no commands yet — Bash runs will stream here".to_string(),
        };
        let rect = centered(console, (msg.chars().count() as u16).min(console.width), 1);
        f.render_widget(Paragraph::new(Line::styled(msg, fg(C_DIM))), rect);
        return;
    }

    let anchor = if follow {
        vis.len() - 1
    } else {
        vis.iter()
            .position(|c| c.seq == sel)
            .unwrap_or(vis.len() - 1)
    };
    let pane_h = console.height as usize;
    let width = console.width as usize;
    let lines = fill_bottom_up(anchor, pane_h, |i| {
        let c = vis[i];
        let is_sel = !follow && c.seq == sel;
        // verbose follow: the newest entry renders expanded as it streams
        let is_expanded = expand && i == anchor;
        shell_entry_lines(st, ui, c, is_expanded, is_sel, width)
    });
    f.render_widget(Paragraph::new(lines), console);
}

/// The console's bottom-up layout, shared by both SHELL perspectives:
/// entries stack upward from the anchor (index `anchor` down to 0 —
/// NOTHING renders below the anchor, so arrivals can never shift a
/// seq-anchored selection); each entry is height-capped (tail wins) and the
/// topmost is clipped from its top to exactly fill the pane.
fn fill_bottom_up(
    anchor: usize,
    pane_h: usize,
    mut entry_of: impl FnMut(usize) -> Vec<Line<'static>>,
) -> Vec<Line<'static>> {
    let cap = pane_h.saturating_sub(2).max(1); // entry height cap, tail wins
    let mut used = 0usize;
    let mut chunks: Vec<Vec<Line<'static>>> = Vec::new(); // anchor-first
    for i in (0..=anchor).rev() {
        let mut entry = entry_of(i);
        if entry.len() > cap {
            entry = clip_top(entry, cap);
        }
        if used + entry.len() > pane_h {
            let need = pane_h - used;
            if need > 0 {
                chunks.push(clip_top(entry, need));
            }
            used = pane_h;
            break;
        }
        used += entry.len();
        chunks.push(entry);
        if used == pane_h {
            break;
        }
    }
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(pane_h);
    for _ in 0..pane_h.saturating_sub(used) {
        lines.push(Line::from(""));
    }
    for entry in chunks.iter().rev() {
        lines.extend(entry.iter().cloned());
    }
    lines
}

// ---------------------------------------------------------------------------
// SHELL / RETRIEVAL — the agentic-retrieval perspective (`v` inside SHELL)
// ---------------------------------------------------------------------------

/// Per-server mcp accent: the theme's file wheel keyed by a stable FNV-1a
/// hash of the server name. Servers aren't files — the first-access file hue
/// map is deliberately not involved (a server keeps its hue across sessions).
pub fn server_hue(name: &str) -> (u8, u8, u8) {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in name.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    let wheel = &theme().wheel;
    wheel[(h % wheel.len() as u64) as usize]
}

/// Kind glyph + accent: `⌕` search cyan · `⇣` fetch blue · `◆` mcp
/// (per-server accent) · `#` toolsearch dim; a failed pull's `✖` red wins.
fn ret_glyph(r: &RetRec) -> (&'static str, (u8, u8, u8)) {
    if !r.ok {
        return ("✖", C_RED);
    }
    match r.kind {
        RetKind::Search => ("⌕", C_CYAN),
        RetKind::Fetch => ("⇣", C_USER),
        RetKind::Mcp => ("◆", server_hue(&r.src)),
        RetKind::Toolsearch => ("#", C_DIM),
        RetKind::Unknown => ("·", C_DIM),
    }
}

/// Bytes, humanized: `812B`, `48.2kB`.
fn fmt_bytes(b: u64) -> String {
    if b < 1000 {
        format!("{b}B")
    } else {
        format!("{:.1}kB", b as f64 / 1000.0)
    }
}

/// One retrieval entry as lines. Row grammar (SPEC §e RETRIEVAL):
/// `ts glyph src q → n · dur tok` — src in the kind's accent, q body-tinted,
/// result meta dim, `tok` on the console's fixed 0–16k ramp/color law.
/// Expanded adds the FULL query/url (wrapped, never truncated) and a detail
/// line with result count, bytes, duration and tok.
fn ret_entry_lines(
    st: &State,
    ui: &Ui,
    r: &RetRec,
    expanded: bool,
    selected: bool,
    width: usize,
) -> Vec<Line<'static>> {
    let g = shell_geom(ui.tier);
    // prefix: `ts ` (Full only) + glyph + space
    let indent = if g.ts { 11 } else { 2 };
    let text_w = width.saturating_sub(indent).max(1);
    let mut lines: Vec<Line<'static>> = Vec::new();

    // arrival pulse: white-blend while ret_pulse names this seq (live only)
    let blend = if st.ret_pulse > 0 && st.ret_pulse_seq == r.seq && ui.cursor.is_none() {
        st.ret_pulse as f64 / 6.0 * 0.6
    } else {
        0.0
    };
    let bl = |col: (u8, u8, u8)| -> Style {
        if blend > 0.0 {
            fg(lerp(col, C_WHITE, blend))
        } else {
            fg(col)
        }
    };
    let (glyph, gcol) = ret_glyph(r);
    let mut spans: Vec<Span<'static>> = Vec::new();
    if g.ts {
        spans.push(Span::styled(format!("{:8} ", r.ts), fg(C_DIM)));
    }
    spans.push(Span::styled(format!("{glyph} "), bl(gcol)));
    let right_block = if g.spark { 7 } else { 6 }; // spark + 6-wide tok
    let avail = width.saturating_sub(indent + right_block + 1);
    // src in the kind accent, head-truncated so the query keeps room
    let src_show = head_trunc(&r.src, avail.saturating_sub(1).min(28));
    let mut used = src_show.chars().count() + 1;
    spans.push(Span::styled(format!("{src_show} "), bl(gcol)));
    // result meta AFTER the query: ` → n · dur` (only the parts that exist)
    let mut meta = String::new();
    if let Some(n) = r.n {
        meta.push_str(&format!(" → {n}"));
    }
    if let Some(d) = r.dur_ms {
        meta.push_str(&format!(" · {}", fmt_dur(d)));
    }
    let meta_w = meta.chars().count();
    let q_show = head_trunc(&r.q, avail.saturating_sub(used + meta_w));
    used += q_show.chars().count();
    spans.push(Span::styled(q_show, bl(C_FG)));
    if meta_w > 0 && used + meta_w <= avail {
        spans.push(Span::styled(meta, fg(C_DIM)));
        used += meta_w;
    }
    let pad = width.saturating_sub(indent + used + right_block);
    spans.push(Span::raw(" ".repeat(pad)));
    let tstyle = tok_out_style(r.tok);
    if g.spark {
        spans.push(Span::styled(
            spark_char(r.tok as f64, 16_000.0).to_string(),
            tstyle,
        ));
    }
    spans.push(Span::styled(format!("{:>6}", fmt_k1(r.tok)), tstyle));
    let mut row = Line::from(spans);
    if selected {
        row = row.patch_style(Style::default().add_modifier(Modifier::REVERSED));
    }
    lines.push(row);

    if expanded {
        // full query/url — wrapped, never truncated (the console's tail law)
        let q_style = fg(scale(C_FG, 0.72));
        for chunk in wrap_line(&r.q, text_w) {
            lines.push(Line::styled(
                format!("{}{chunk}", " ".repeat(indent)),
                q_style,
            ));
        }
        let mut parts: Vec<String> = Vec::new();
        if let Some(n) = r.n {
            parts.push(format!("n {n}"));
        }
        if let Some(b) = r.bytes {
            parts.push(fmt_bytes(b));
        }
        if let Some(d) = r.dur_ms {
            parts.push(fmt_dur(d));
        }
        parts.push(format!("tok {}", fmt_k1(r.tok)));
        lines.push(Line::styled(
            format!("{}{}", " ".repeat(indent), parts.join(" · ")),
            fg(C_DIM),
        ));
    }
    lines
}

/// Header: `⌕ N pulls · web X tok · mcp Y tok · tools Z tok · filter all|err`
/// left (per-source token totals: web = search+fetch, mcp, tools =
/// toolsearch); the shared FOLLOW/browse/replay posture right.
#[allow(clippy::too_many_arguments)]
fn render_ret_header(
    st: &State,
    ui: &Ui,
    follow: bool,
    sel: u64,
    filter: ShellFilter,
    vis: &[&RetRec],
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let n = st.rets.len();
    let (mut web, mut mcp, mut tools) = (0u64, 0u64, 0u64);
    for r in &st.rets {
        match r.kind {
            RetKind::Search | RetKind::Fetch => web += r.tok,
            RetKind::Mcp => mcp += r.tok,
            RetKind::Toolsearch => tools += r.tok,
            RetKind::Unknown => {}
        }
    }
    let mut spans: Vec<Span<'static>> = vec![
        Span::styled("⌕ ".to_string(), fg(C_CYAN).add_modifier(Modifier::BOLD)),
        Span::styled(
            format!("{n} pulls"),
            fg(C_FG).add_modifier(Modifier::BOLD),
        ),
    ];
    for (label, tok) in [("web", web), ("mcp", mcp), ("tools", tools)] {
        spans.push(Span::styled(" · ".to_string(), fg(C_GRID)));
        spans.push(Span::styled(
            format!("{label} {} tok", fmt_k1(tok)),
            if tok > 0 { fg(C_FG) } else { fg(C_DIM) },
        ));
    }
    spans.push(Span::styled(" · ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!("filter {}", filter.label()),
        if filter == ShellFilter::Err {
            fg(C_AMBER)
        } else {
            fg(C_DIM)
        },
    ));
    let newer = vis.iter().filter(|r| r.seq > sel).count();
    let (rtxt, rstyle) = feed_posture(ui, follow, newer, "retrieval");
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let pad = (area.width as usize).saturating_sub(used + rtxt.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(rtxt, rstyle));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Tab 6 SHELL, RETRIEVAL perspective — every EXTERNAL pull the session's
/// own retriever made (web searches, fetches, MCP connector calls, tool
/// searches), newest at bottom, on the console's follow/browse machinery
/// (`shell_follow` shared; seq-anchored selection separate).
#[allow(clippy::too_many_arguments)]
pub fn render_retrieval_tab(
    st: &State,
    ui: &Ui,
    follow: bool,
    sel: u64,
    expand: bool,
    filter: ShellFilter,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 12 || area.height == 0 {
        return;
    }
    let compact = ui.tier == Tier::Compact;
    let header_h: u16 = if compact { 0 } else { 1 };
    let [hdr, feed] =
        Layout::vertical([Constraint::Length(header_h), Constraint::Min(0)]).areas(area);

    let vis = ret_visible(st, filter, ui.cursor);
    if header_h > 0 {
        render_ret_header(st, ui, follow, sel, filter, &vis, f, hdr);
    }
    if feed.height == 0 {
        return;
    }
    if vis.is_empty() {
        let msg = match ui.cursor {
            Some(t) => format!("no retrievals ≤ t={t}"),
            None => "no retrievals yet — web/MCP pulls will stream here".to_string(),
        };
        let rect = centered(feed, (msg.chars().count() as u16).min(feed.width), 1);
        f.render_widget(Paragraph::new(Line::styled(msg, fg(C_DIM))), rect);
        return;
    }

    let anchor = if follow {
        vis.len() - 1
    } else {
        vis.iter()
            .position(|r| r.seq == sel)
            .unwrap_or(vis.len() - 1)
    };
    let pane_h = feed.height as usize;
    let width = feed.width as usize;
    let lines = fill_bottom_up(anchor, pane_h, |i| {
        let r = vis[i];
        let is_sel = !follow && r.seq == sel;
        let is_expanded = expand && i == anchor;
        ret_entry_lines(st, ui, r, is_expanded, is_sel, width)
    });
    f.render_widget(Paragraph::new(lines), feed);
}

// ---------------------------------------------------------------------------
// overlays
// ---------------------------------------------------------------------------

/// A rect of (w,h) centered in `area`, clamped to fit.
pub fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    Rect {
        x: area.x + (area.width - w) / 2,
        y: area.y + (area.height - h) / 2,
        width: w,
        height: h,
    }
}

fn overlay_block(title: String) -> Block<'static> {
    Block::new()
        .borders(Borders::ALL)
        .border_type(BorderType::Plain)
        .border_style(fg(C_AMBER))
        .title(title)
        .title_style(fg(C_AMBER).add_modifier(Modifier::BOLD))
}

/// The ONE scrollable content-popup core. Every text overlay (help · PEEK
/// · fleet PREVIEW) renders through this, so the frame, scroll clamping,
/// wheel/j-k semantics, footer range, and the 8-cell position bar are
/// uniform BY CONSTRUCTION — an improvement here propagates to every
/// popup, which is the whole point (the 2026-08 help revamp initially
/// didn't reach PREVIEW because each overlay hand-rolled this).
pub struct PaneFrame<'a> {
    pub title: String,
    pub width: u16,
    /// pinned rows above the scroll window (search box, identity line…)
    pub header: Vec<Line<'static>>,
    /// the scrollable content
    pub body: Vec<Line<'static>>,
    /// left-side footer hints; scroll range + bar appended when scrollable
    pub footer: String,
    /// lines hidden above the window; render clamps it so key/wheel
    /// presses never bank past the ends (usize::MAX = jump to bottom)
    pub scroll: &'a std::cell::Cell<usize>,
}

pub fn render_pane(pf: &PaneFrame<'_>, f: &mut Frame<'_>, area: Rect) {
    if area.width < 4 || area.height < 4 {
        return;
    }
    let chrome = pf.header.len() as u16 + 3; // borders + footer row
    let h = (pf.body.len() as u16).saturating_add(chrome);
    let rect = centered(
        area,
        pf.width,
        h.min(area.height.saturating_sub(2)).max(5).min(area.height),
    );
    f.render_widget(Clear, rect);
    let block = overlay_block(pf.title.clone());
    let inner = block.inner(rect);
    f.render_widget(block, rect);
    if inner.height < 2 || inner.width < 4 {
        return;
    }
    let headh = pf.header.len().min(inner.height as usize - 1);
    let viewh = (inner.height as usize).saturating_sub(headh + 1).max(1);
    let max_scroll = pf.body.len().saturating_sub(viewh);
    let s = pf.scroll.get().min(max_scroll);
    pf.scroll.set(s);
    if headh > 0 {
        f.render_widget(
            Paragraph::new(pf.header[..headh].to_vec()),
            Rect {
                height: headh as u16,
                ..inner
            },
        );
    }
    let end = (s + viewh).min(pf.body.len());
    f.render_widget(
        Paragraph::new(pf.body[s..end].to_vec()),
        Rect {
            y: inner.y + headh as u16,
            height: viewh as u16,
            ..inner
        },
    );
    let mut foot: Vec<Span<'static>> = vec![Span::styled(pf.footer.clone(), fg(C_DIM))];
    if max_scroll > 0 {
        const CELLS: usize = 8;
        let total = pf.body.len().max(1);
        let range = format!("j/k scroll {}–{} of {} ", s + 1, end, total);
        let a = s * CELLS / total;
        let b = ((s + viewh) * CELLS).div_ceil(total).min(CELLS);
        let bar: String = (0..CELLS)
            .map(|i| if i >= a && i < b { '█' } else { '▂' })
            .collect();
        let pad = (inner.width as usize)
            .saturating_sub(pf.footer.chars().count() + range.chars().count() + CELLS + 1);
        foot.push(Span::raw(" ".repeat(pad)));
        foot.push(Span::styled(range, fg(C_DIM)));
        foot.push(Span::styled(bar, fg(C_STEEL)));
    }
    f.render_widget(
        Paragraph::new(Line::from(foot)),
        Rect {
            y: inner.y + inner.height - 1,
            height: 1,
            ..inner
        },
    );
}

/// Compaction post-mortem overlay (EVENTS `Enter`, global `c`).
pub fn render_postmortem(st: &State, idx: usize, f: &mut Frame<'_>, area: Rect) {
    let Some(c) = st.compactions.get(idx) else {
        return;
    };
    if area.width < 4 || area.height < 4 {
        return;
    }
    let b = st.budget.max(1) as f64;
    let barw = 28usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    // pre/post bars on one fixed 0–B scale
    for (label, tok, col) in [("pre ", c.pre, C_AMBER), ("post", c.post, C_GREEN)] {
        lines.push(Line::from(vec![
            Span::styled(format!(" {label} "), fg(C_DIM)),
            Span::styled(eighth_bar(tok as f64 / b, barw), fg(col)),
            Span::styled(format!(" {}", fmt_k1(tok)), fg(C_FG)),
        ]));
    }
    lines.push(Line::from(vec![
        Span::styled(
            format!(
                " dropped {} (cum {}) · {} · preserved {} msgs",
                fmt_k1(c.dropped),
                fmt_k1(c.cum_dropped),
                fmt_dur(c.dur_ms),
                c.preserved_msgs
            ),
            fg(C_FG),
        ),
    ]));
    lines.push(Line::from(""));
    // dropped-by-category eighth-block bars (fraction of the total drop)
    let denom = c.dropped.max(1) as f64;
    for cat in CAT_ORDER {
        let tok = c.dropped_cats.get(cat.label()).copied().unwrap_or(0);
        if tok == 0 {
            continue;
        }
        lines.push(Line::from(vec![
            Span::styled(format!(" {:<9} ", cat.label()), fg(cat_color(cat))),
            Span::styled(eighth_bar(tok as f64 / denom, barw), fg(cat_color(cat))),
            Span::styled(format!(" {}", fmt_k1(tok)), fg(C_DIM)),
        ]));
    }
    if !c.dropped_files.is_empty() {
        lines.push(Line::from(""));
        let files_by_id: HashMap<u64, &FileRec> =
            eff_files(st).into_iter().map(|f| (f.id, f)).collect();
        for df in c.dropped_files.iter().take(6) {
            let path = files_by_id
                .get(&df.file)
                .map(|f| f.path.clone())
                .unwrap_or_else(|| format!("file#{}", df.file));
            lines.push(Line::from(vec![
                Span::styled(" ✝ ".to_string(), fg(C_RED)),
                Span::styled(tail_trunc(&path, 40), fg(hue_of(st, df.file))),
                Span::styled(format!(" −{}", fmt_k1(df.tok)), fg(C_DIM)),
            ]));
        }
    }
    lines.push(Line::from(""));
    lines.push(Line::styled(
        format!(
            " ←/→ prev/next ({}/{}) · esc close",
            idx + 1,
            st.compactions.len()
        ),
        fg(C_DIM),
    ));

    let rect = centered(area, 64, lines.len() as u16 + 2);
    f.render_widget(Clear, rect);
    let block = overlay_block(format!(
        "COMPACTION {} — turn {} ({}) {}",
        c.n, c.turn, c.trigger, c.ts
    ));
    let inner = block.inner(rect);
    f.render_widget(block, rect);
    f.render_widget(Paragraph::new(lines), inner);
}

/// Fleet session-picker overlay.
/// Roster rows filtered by a case-insensitive substring query over name /
/// project / id. Empty query → all rows (in the usual fleet_rows order).
pub fn fleet_rows_filtered<'a>(st: &'a State, query: &str) -> Vec<&'a crate::ipc::Sess> {
    let rows = fleet_rows(st);
    let q = query.trim().to_lowercase();
    if q.is_empty() {
        return rows;
    }
    rows.into_iter()
        .filter(|s| {
            let id = s.id.to_lowercase();
            s.name
                .as_deref()
                .map(|n| n.to_lowercase().contains(&q))
                .unwrap_or(false)
                || s.project.to_lowercase().contains(&q)
                || id.contains(&q)
                // typing a full jsonl PATH (or a full uuid) that CONTAINS this
                // session's id surfaces it as a row, not just an attach prompt
                || q.contains(&id)
        })
        .collect()
}

pub fn render_fleet(st: &State, sel: usize, query: &str, f: &mut Frame<'_>, area: Rect) {
    if area.width < 4 || area.height < 8 {
        return;
    }
    let rows = fleet_rows_filtered(st, query);
    let total = rows.len();
    // the box is: data rows + 5 chrome lines (search, blank, header, blank,
    // footer) + 2 borders. Cap data rows to what fits, and SCROLL the rest.
    let cap_visible = (area.height.saturating_sub(7)).max(1) as usize;
    let visible = total.min(cap_visible);
    let sel = if total == 0 { 0 } else { sel.min(total - 1) };
    // keep the selection roughly centered in the scroll window
    let offset = if total <= visible {
        0
    } else {
        sel.saturating_sub(visible / 2).min(total - visible)
    };

    let mut lines: Vec<Line<'static>> = Vec::new();
    // live search box: type a name to filter, or a .jsonl path to attach direct
    lines.push(Line::from(vec![
        Span::styled(" search ".to_string(), fg(C_DIM)),
        Span::styled(
            format!("{query}▌"),
            fg(C_WHITE).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            "    type a session name, or a .jsonl path".to_string(),
            fg(C_DIM),
        ),
    ]));
    lines.push(Line::from(""));
    lines.push(Line::styled(
        format!(
            " {:1} {:<16} {:<18} {:8} {:>4}  last prompt",
            "", "name", "project", "resident", "age"
        ),
        fg(C_DIM),
    ));
    if total == 0 {
        let q = query.trim();
        let looks_path = q.contains('/') || q.ends_with(".jsonl");
        let msg = if q.is_empty() {
            " no sessions discovered".to_string()
        } else if looks_path {
            format!(" ⏎  attach path  {}", tail_trunc(q, 56))
        } else {
            format!(" no roster match \"{q}\"  —  ⏎ still tries it as a path / session id")
        };
        lines.push(Line::styled(msg, fg(C_DIM)));
    } else {
        for (gi, s) in rows.iter().enumerate().skip(offset).take(visible) {
            let selrow = gi == sel;
            let (glyph, col) = fleet_glyph(s);
            let bar = match (s.resident, s.budget) {
                (Some(r), Some(b)) if b > 0 => {
                    let fill = ((r as f64 / b as f64).clamp(0.0, 1.0) * 8.0).round() as usize;
                    format!("{}{}", "█".repeat(fill), "░".repeat(8 - fill))
                }
                _ => "        ".to_string(),
            };
            let zone = match (s.resident, s.budget) {
                (Some(r), Some(b)) if b > 0 => zone_color(r as f64 / b as f64),
                _ => C_DIM,
            };
            let name = s
                .name
                .clone()
                .unwrap_or_else(|| s.id.chars().take(12).collect());
            let proj = tail_trunc(&s.project, 18);
            let age = fmt_age(0.0f64.max(now_minus(st, s.mtime)));
            let base = if selrow {
                fg(C_FG).add_modifier(Modifier::REVERSED)
            } else {
                fg(C_FG)
            };
            lines.push(Line::from(vec![
                Span::styled(format!(" {glyph} "), if selrow { fg(col).add_modifier(Modifier::REVERSED) } else { fg(col) }),
                Span::styled(format!("{:<16} {:<18} ", tail_trunc(&name, 16), proj), base),
                Span::styled(bar, if selrow { fg(zone).add_modifier(Modifier::REVERSED) } else { fg(zone) }),
                Span::styled(
                    format!(
                        " {:>4}  {}",
                        age,
                        tail_trunc(&s.last_prompt.clone().unwrap_or_default(), 30)
                    ),
                    if selrow { fg(C_DIM).add_modifier(Modifier::REVERSED) } else { fg(C_DIM) },
                ),
            ]));
        }
    }
    lines.push(Line::from(""));
    // footer: keys + scroll position (which slice of how many sessions)
    let pos = if total > visible {
        format!("   {}\u{2013}{} of {}", offset + 1, offset + visible, total)
    } else if total > 0 {
        format!("   {total} session{}", if total == 1 { "" } else { "s" })
    } else {
        String::new()
    };
    lines.push(Line::styled(
        format!(" ⏎ attach · ↑↓ move · type to filter · tab system-wide · esc close{pos}"),
        fg(C_DIM),
    ));

    let w = area.width.saturating_sub(4).min(96).max(20);
    let rect = centered(area, w, (lines.len() as u16 + 2).min(area.height));
    f.render_widget(Clear, rect);
    let block = overlay_block("SESSIONS".to_string());
    let inner = block.inner(rect);
    f.render_widget(block, rect);
    f.render_widget(Paragraph::new(lines), inner);
}

fn now_minus(_st: &State, mtime: f64) -> f64 {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    now - mtime
}

/// Sorted fleet rows: live roster first, then recent by mtime.
pub fn fleet_rows(st: &State) -> Vec<&crate::ipc::Sess> {
    let mut live: Vec<&crate::ipc::Sess> = st.fleet.iter().filter(|s| s.live).collect();
    let mut rest: Vec<&crate::ipc::Sess> = st.fleet.iter().filter(|s| !s.live).collect();
    live.sort_by(|a, b| b.mtime.partial_cmp(&a.mtime).unwrap_or(std::cmp::Ordering::Equal));
    rest.sort_by(|a, b| b.mtime.partial_cmp(&a.mtime).unwrap_or(std::cmp::Ordering::Equal));
    live.extend(rest);
    live
}

/// Live sessions only, in a STABLE order (project, then id) — wall tiles
/// must not jump around as mtimes tick.
pub fn fleet_live_rows(st: &State) -> Vec<&crate::ipc::Sess> {
    let mut v: Vec<&crate::ipc::Sess> = st.fleet.iter().filter(|s| s.live).collect();
    v.sort_by(|a, b| a.project.cmp(&b.project).then(a.id.cmp(&b.id)));
    v
}

/// A session's palette seed: FNV-1a over its id. Same algorithmic family as
/// the per-process roll, but DETERMINISTIC per session — a session keeps its
/// colors across launches, so the wall reads as identity, not confetti.
fn sess_seed(id: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in id.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// SYSTEM-WIDE (`tab` in the SESSIONS picker): a full SCREEN, not a popup —
/// every ACTIVE session as a gradient-tank tile (the big-number gauge in
/// miniature), all draining live. Tiles scale up to fill the terminal.
/// ⏎ attaches the selected tile.
pub fn render_system_wide(st: &State, sel: usize, f: &mut Frame<'_>, area: Rect) {
    if area.width < 30 || area.height < 10 {
        return;
    }
    f.render_widget(Clear, area);
    f.render_widget(Block::default().style(Style::default().bg(rgb(scale(C_FREE, 0.3)))), area);

    let rows = fleet_live_rows(st);
    let n = rows.len();
    let sel = if n == 0 { 0 } else { sel.min(n - 1) };

    // header + footer chrome, grid in between
    f.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" SYSTEM-WIDE ".to_string(), fg(C_WHITE).add_modifier(Modifier::BOLD)),
            Span::styled(
                format!("— {n} active session{}", if n == 1 { "" } else { "s" }),
                fg(C_DIM),
            ),
        ])),
        Rect { x: area.x, y: area.y, width: area.width, height: 1 },
    );
    f.render_widget(
        Paragraph::new(Line::styled(
            " ⏎ attach · ␣ preview · x end · ←↑↓→ move · tab sessions list · esc back".to_string(),
            fg(C_DIM),
        )),
        Rect {
            x: area.x,
            y: area.y + area.height - 1,
            width: area.width,
            height: 1,
        },
    );
    let grid = Rect {
        x: area.x + 1,
        y: area.y + 2,
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(4),
    };
    if n == 0 {
        f.render_widget(
            Paragraph::new(Line::styled(" no active sessions", fg(C_DIM))),
            Rect { x: grid.x, y: grid.y, width: grid.width, height: 1 },
        );
        return;
    }

    // tiles scale to fill: as many 26-wide columns as fit (capped at n),
    // then split the grid evenly; height splits across the needed rows
    let cols = ((grid.width / 26).max(1) as usize).min(n);
    let grid_rows = n.div_ceil(cols);
    let tw = grid.width / cols as u16;
    let th = (grid.height / grid_rows as u16).clamp(5, 12);
    let vis_rows = ((grid.height / th).max(1) as usize).min(grid_rows);
    // scroll whole tile-rows so the selection stays visible
    let row_off = (sel / cols).saturating_sub(vis_rows.saturating_sub(1));
    let visible = vis_rows * cols;

    for (gi, s) in rows.iter().enumerate().skip(row_off * cols).take(visible) {
        let vi = gi - row_off * cols;
        let tile = Rect {
            x: grid.x + (vi % cols) as u16 * tw,
            y: grid.y + (vi / cols) as u16 * th,
            width: tw,
            height: th,
        };
        draw_sess_tile(s, gi == sel, f, tile);
    }
    if n > visible {
        let shown_from = row_off * cols;
        f.render_widget(
            Paragraph::new(Line::styled(
                format!("   {}\u{2013}{} of {n}", shown_from + 1, (shown_from + visible).min(n)),
                fg(C_DIM),
            )),
            Rect {
                x: area.x,
                y: area.y + area.height - 2,
                width: area.width,
                height: 1,
            },
        );
    }
}

/// One wall tile: bordered mini gradient tank (context left, per-session
/// palette) with the R% readout on top and status · project at the foot.
fn draw_sess_tile(s: &crate::ipc::Sess, selected: bool, f: &mut Frame<'_>, tile: Rect) {
    let pal = gen_palette(sess_seed(&s.id));
    let ratio = match (s.resident, s.budget) {
        (Some(r), Some(b)) if b > 0 => Some((r as f64 / b as f64).clamp(0.0, 1.0)),
        _ => None,
    };
    let name = sess_display_name(s);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(if selected {
            BorderType::Thick
        } else {
            BorderType::Plain
        })
        .border_style(if selected { fg(pal[3]) } else { fg(C_GRID) })
        .title(Span::styled(
            format!(" {} ", tail_trunc(&name, (tile.width as usize).saturating_sub(6))),
            if selected {
                fg(pal[3]).add_modifier(Modifier::BOLD)
            } else {
                fg(C_FG)
            },
        ));
    let inner = block.inner(tile);
    f.render_widget(block, tile);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    if let Some(r) = ratio {
        render_tank(f, inner, 1.0 - r, &pal);
    }
    // readouts draw fg-only, so the tank's background shows through
    let pct = ratio.map(|r| format!("{:.0}%", r * 100.0)).unwrap_or("—".into());
    let zone = ratio.map(zone_color).unwrap_or(C_DIM);
    f.render_widget(
        Paragraph::new(Line::styled(
            format!(" {pct}"),
            fg(lerp(zone, C_WHITE, 0.20)).add_modifier(Modifier::BOLD),
        )),
        Rect {
            x: inner.x,
            y: inner.y,
            width: inner.width,
            height: 1,
        },
    );
    let (glyph, gcol) = fleet_glyph(s);
    let foot = Line::from(vec![
        Span::styled(format!(" {glyph}"), fg(gcol)),
        Span::styled(
            format!(
                "{} · {}",
                s.status,
                tail_trunc(&s.project, (inner.width as usize).saturating_sub(8))
            ),
            fg(C_FG),
        ),
    ]);
    f.render_widget(
        Paragraph::new(foot),
        Rect {
            x: inner.x,
            y: inner.y + inner.height - 1,
            width: inner.width,
            height: 1,
        },
    );
}

/// A session's display handle: roster name, else the id's first 12 chars.
pub fn sess_display_name(s: &crate::ipc::Sess) -> String {
    s.name
        .clone()
        .unwrap_or_else(|| s.id.chars().take(12).collect())
}

/// Quicklook PREVIEW overlay (SESSIONS/SYSTEM-WIDE `space`): the tail of the
/// session's conversation, served on demand by the engine. Opens anchored
/// to the NEWEST exchange (scroll starts at the bottom — the opener sets
/// the cell to usize::MAX and the pane clamps); j/k · wheel scroll back.
pub fn render_fleet_peek_overlay(
    p: &FleetPeekMsg,
    scroll: &std::cell::Cell<usize>,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 12 || area.height < 6 {
        return;
    }
    let w = area.width.saturating_sub(4).clamp(30, 84);
    let text_w = (w as usize).saturating_sub(5).max(1); // borders + marker gutter
    let mut body: Vec<Line<'static>> = Vec::new();
    if !p.found {
        body.push(Line::styled(
            " no transcript found for this session".to_string(),
            fg(C_DIM),
        ));
    } else if p.msgs.is_empty() {
        body.push(Line::styled(" (no conversation yet)".to_string(), fg(C_DIM)));
    } else {
        for m in &p.msgs {
            let (glyph, gcol) = match m.role.as_str() {
                "user" => ("»", C_USER),
                _ => ("●", C_ASSIST),
            };
            let mut first = true;
            for raw in m.text.split('\n') {
                for chunk in wrap_line(raw, text_w) {
                    if first {
                        body.push(Line::from(vec![
                            Span::styled(
                                format!(" {glyph} "),
                                fg(gcol).add_modifier(Modifier::BOLD),
                            ),
                            Span::styled(chunk, fg(C_FG)),
                        ]));
                        first = false;
                    } else {
                        body.push(Line::from(vec![
                            Span::raw("   ".to_string()),
                            Span::styled(chunk, fg(C_FG)),
                        ]));
                    }
                }
            }
            if first {
                // empty message text still shows the speaker changed hands
                body.push(Line::styled(format!(" {glyph} …"), fg(C_DIM)));
            }
            body.push(Line::from(""));
        }
        body.pop(); // trailing spacer
    }
    render_pane(
        &PaneFrame {
            title: format!(" PREVIEW — {} ", p.name),
            width: w,
            header: vec![
                Line::styled(
                    format!(" {}", tail_trunc(&p.project, (w as usize).saturating_sub(4))),
                    fg(C_DIM),
                ),
                Line::from(""),
            ],
            body,
            footer: " ␣/esc close · ⏎ attach".to_string(),
            scroll,
        },
        f,
        area,
    );
}

/// End-session confirm modal (SYSTEM-WIDE `x`): nothing is signalled until
/// `y`/⏎ — every other key cancels.
pub fn render_kill_confirm(name: &str, f: &mut Frame<'_>, area: Rect) {
    if area.width < 12 || area.height < 5 {
        return;
    }
    let body = format!(" end session {name}? (SIGTERM its process)");
    let w = (body.chars().count() as u16 + 3).clamp(30, area.width.saturating_sub(2));
    let lines = vec![
        Line::styled(body, fg(C_FG)),
        Line::from(""),
        Line::styled(" y/⏎ end it · any other key cancels".to_string(), fg(C_DIM)),
    ];
    let rect = centered(area, w, 5);
    f.render_widget(Clear, rect);
    let block = Block::new()
        .borders(Borders::ALL)
        .border_type(BorderType::Plain)
        .border_style(fg(C_RED))
        .title(" END SESSION ")
        .title_style(fg(C_RED).add_modifier(Modifier::BOLD));
    let inner = block.inner(rect);
    f.render_widget(block, rect);
    f.render_widget(Paragraph::new(lines), inner);
}

fn fleet_glyph(s: &crate::ipc::Sess) -> (&'static str, (u8, u8, u8)) {
    match s.status.as_str() {
        "busy" => ("●", C_GREEN),
        "stalled" => ("◐", C_AMBER),
        "idle" => ("○", C_DIM),
        "dead" => ("✖", C_RED),
        _ => ("·", C_DIM), // offline
    }
}

/// Help overlay — ONE searchable, scrollable browser (yazi/lazygit idiom;
/// no pages). Sections: the current tab pinned on top, then GLOBAL, the
/// other tabs, FLEET, GLYPHS, THE NUMBERS, MAP & TURN ANATOMY. `/` filters
/// every entry live; sections with no match disappear.
#[derive(Default)]
pub struct HelpState {
    pub filter: String,
    pub typing: bool,
    pub scroll: usize,
}

/// One searchable unit: a key (or term) plus its description. `lines` is the
/// styled render; `search` is the plain text the filter matches against.
struct HelpItem {
    search: String,
    lines: Vec<Line<'static>>,
}

struct HelpSection {
    title: &'static str,
    items: Vec<HelpItem>,
}

/// key/term item: first desc line carries the colored key column, the rest
/// hang indented under it. `w` is the key-column width (14 keys · 11 terms
/// · 7 glyph rows); a hard space always separates column from description.
fn hitem(w: usize, k: &str, desc: &[&str]) -> HelpItem {
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(desc.len());
    for (i, d) in desc.iter().enumerate() {
        let col = if i == 0 { k } else { "" };
        lines.push(Line::from(vec![
            Span::styled(format!(" {col:<w$} "), fg(C_AMBER)),
            Span::styled((*d).to_string(), fg(C_FG)),
        ]));
    }
    HelpItem {
        search: format!("{k} {}", desc.join(" ")),
        lines,
    }
}

/// pre-styled line (palette swatches etc.) with explicit search text
fn hrich(search: &str, line: Line<'static>) -> HelpItem {
    HelpItem {
        search: search.to_string(),
        lines: vec![line],
    }
}

fn help_sections(tab: usize) -> Vec<HelpSection> {
    let k = |key: &str, desc: &[&str]| hitem(12, key, desc);
    let t = |term: &str, desc: &[&str]| hitem(11, term, desc);
    let g = |term: &str, desc: &[&str]| hitem(7, term, desc);

    let global = HelpSection {
        title: "GLOBAL",
        items: vec![
            k("1–6", &["tabs: OVERVIEW FILES TURNS AGENTS EVENTS SHELL"]),
            k("f / 0", &["fleet — every session on the machine"]),
            k("?", &["this help · q quit · p pause render"]),
            k("w", &["welcome tour — the guided walkthrough (opens once by itself)"]),
            k("x", &["amtr3d — stream this session to the Vision Pro memspace"]),
            k("←/→  ⇧←/→", &["turn cursor ±1 / ±10 · home first · end LIVE"]),
            k("esc", &["ack the visible alert, then snap to LIVE"]),
            k("bksp", &["drill OUT of an agent, back to its parent session"]),
            k("m", &["MAP mode: class → heat → age → cache"]),
            k("t · tab", &["block theme · small-window theme tank⇄blocks"]),
            k("␣ · +/-", &["reroll tank palette · cell rung up/down"]),
            k("c", &["latest compaction post-mortem (←/→ walk history)"]),
            k("R", &["write a ground-truth report (→ ~/.claude/amtr-reports/)"]),
        ],
    };
    let overview = HelpSection {
        title: "OVERVIEW",
        items: vec![
            k("i", &["INSPECT — walk the actual context-window segments"]),
            k("←/→ j/k", &["(inspect) prev / next segment"]),
            k("⏎", &["(inspect) file seg → $EDITOR · else peek the content"]),
            k("p", &["(inspect) peek the in-context copy (can differ on disk)"]),
            k("esc", &["(inspect) exit"]),
        ],
    };
    let files = HelpSection {
        title: "FILES",
        items: vec![
            k("j/k g/G", &["select · jump to ends"]),
            k("⏎", &["expand the selected file's detail"]),
            k("v", &["HISTORY ⇄ NOW (recency-ordered live view)"]),
            k("s", &["cycle sort (HISTORY only — NOW is recency by definition)"]),
            k("o", &["open the selected file in $EDITOR"]),
        ],
    };
    let turns = HelpSection {
        title: "TURNS",
        items: vec![
            k("←/→  ⇧←/→", &["scrub the turn cursor across the columns"]),
            k("c", &["compaction post-mortem for the latest ▼"]),
            k("", &["column anatomy: see MAP & TURN ANATOMY below"]),
        ],
    };
    let agents = HelpSection {
        title: "AGENTS",
        items: vec![
            k("j/k g/G", &["select · jump to ends"]),
            k("⏎", &["expand workflow · drill INTO an agent (bksp returns)"]),
            k("s · a", &["cycle sort · run/fail filter"]),
            k("v", &["grid of context mini-maps ⇄ numeric ledger"]),
        ],
    };
    let events = HelpSection {
        title: "EVENTS",
        items: vec![
            k("j/k g/G", &["select · jump to ends (ledger is newest-first)"]),
            k("⏎", &["compaction → post-mortem · other event → jump cursor"]),
        ],
    };
    let shell = HelpSection {
        title: "SHELL",
        items: vec![
            k("v", &["CONSOLE ⇄ RETRIEVAL"]),
            k("j/k", &["browse (breaks follow) · g oldest · G newest + follow"]),
            k("⏎", &["expand the selected entry"]),
            k("a", &["errors only (console) · failures only (retrieval)"]),
            k("end", &["restore follow"]),
        ],
    };
    let fleet = HelpSection {
        title: "FLEET",
        items: vec![
            k("f / 0", &["open SESSIONS (esc clears the query, then closes)"]),
            k("type", &["live search box — printable keys filter the roster,"]),
            k("", &["or paste a .jsonl path / session id and ⏎ attaches it"]),
            k("↑/↓ ⏎", &["move · attach the selected session"]),
            k("tab", &["list ⇄ SYSTEM-WIDE tab wall (full-screen tanks)"]),
            k("␣ · x", &["quicklook preview · end session (y confirms)"]),
        ],
    };

    // GLYPHS — the palette swatches keep their live colors (rich items)
    let mut sw: Vec<Span<'static>> = vec![Span::styled(" cats  ".to_string(), fg(C_DIM))];
    let mut sw_search = String::from("cats palette swatches");
    for cat in CAT_ORDER {
        let label: String = cat.label().chars().take(4).collect();
        sw_search.push(' ');
        sw_search.push_str(&label);
        sw.push(Span::styled(format!("█{label} "), fg(cat_color(cat))));
    }
    let glyphs = HelpSection {
        title: "GLYPHS",
        items: vec![
            hrich(&sw_search, Line::from(sw)),
            hrich(
                "cache palette cr read cc create in uncached",
                Line::from(vec![
                    Span::styled(" cache ".to_string(), fg(C_DIM)),
                    Span::styled("█cr(read) ".to_string(), fg(C_STEEL)),
                    Span::styled("█cc(create) ".to_string(), fg(C_CYAN)),
                    Span::styled("█in(uncached) ".to_string(), fg(C_AMBER)),
                ]),
            ),
            hrich(
                "turns palette cr 5m 1h in wl cmp thr mdl",
                Line::from(vec![
                    Span::styled(" turns ".to_string(), fg(C_DIM)),
                    Span::styled("█cr ".to_string(), fg(C_STEEL)),
                    Span::styled("█5m ".to_string(), fg(C_CYAN)),
                    Span::styled("█1h ".to_string(), fg(C_ATTACH)),
                    Span::styled("█in ".to_string(), fg(C_RED)),
                    Span::styled("▀wl ".to_string(), fg(C_WLINE)),
                    Span::styled("▼cmp ".to_string(), fg(C_MAGENTA)),
                    Span::styled("▲thr ".to_string(), fg(C_RED)),
                    Span::styled("◆mdl".to_string(), fg(C_WHITE)),
                ]),
            ),
            hrich(
                "zones thresholds 60% 85% T_auto auto-compact threshold",
                Line::from(vec![
                    Span::styled(" zones ".to_string(), fg(C_DIM)),
                    Span::styled("<60% ".to_string(), fg(C_GREEN)),
                    Span::styled("<85% ".to_string(), fg(C_AMBER)),
                    Span::styled("≥85% ".to_string(), fg(C_RED)),
                    Span::styled("· T_auto rule = auto-compact threshold".to_string(), fg(C_DIM)),
                ]),
            ),
            g("map", &["▀ read · ▄ write/edit · █ both · ▼ cliff · ·░▒▓█ shade"]),
            g("", &["✝ evicted · ◆ compaction · ▲ thrash · « replay · ┃ playhead"]),
            g("shell", &["$ prompt · ○ ok · ✖ fail · ^ interrupt · & bg · ▎ stderr"]),
            g("retr", &["⌕ search · ⇣ fetch · ◆ mcp · # toolsearch · ✖ failed"]),
        ],
    };

    let numbers = HelpSection {
        title: "THE NUMBERS",
        items: vec![
            t("R", &[
                "resident context: exactly what the model was sent last",
                "turn (input + cache-read + cache-written) vs the budget",
            ]),
            t("hot/cold", &[
                "recency of LAST touch, nothing else: hot = accessed",
                "within ~2 min (the glow's fade window); cold = still in",
                "context, just idle. A re-read jumps a file back to hot.",
            ]),
            t("AGE O", &[
                "time since last access (ticking) · its op: r read ·",
            ]),
            t("LAST", &[
                "w write · e edit (NOT execute — Bash lives in SHELL)",
                "· LAST = that access's token size",
            ]),
            t("spark", &["the file's last 8 accesses, height = size (0–16k)"]),
            t("%res", &["the file's share of resident context R"]),
            t("waste", &[
                "tokens ever loaded for the file MINUS its live copy:",
                "the cumulative price of re-reads and re-writes",
            ]),
            t("ku", &[
                "kilo cost-units, honest relative cost per turn:",
                "in×1.0 + cache-read×0.1 + 5m-write×1.25 + 1h-write×2.0",
                "+ out×5.0 · ribbon shows the session total",
            ]),
            t("out/t ku/t", &["output tokens per turn · cost per turn"]),
            t("ag N●", &[
                "subagents currently running · amp = tokens an agent",
                "burned in its own window per token returned to yours",
            ]),
            t("waterline", &[
                "end of the cache-served prefix (billed 0.1×) — the MAP",
                "blinks that boundary cell RED; traces stay cyan",
            ]),
            t("thrash", &[
                "red flash: the waterline FELL — the prefix was",
                "invalidated and re-billed at full price",
            ]),
            t("post-mortem", &[
                "(EVENTS ⏎ / c) a compaction's autopsy: size",
                "before/after, dropped tokens by category and by file",
            ]),
        ],
    };

    let anatomy = HelpSection {
        title: "MAP & TURN ANATOMY",
        items: vec![
            t("MAP modes", &["(m) — one geometry, four measures:"]),
            t("class", &["colored by WHAT each block is (user/file/bash/…)"]),
            t("heat", &["brightness = how recently touched (45s decay law)"]),
            t("age", &["shade = how many turns ago it entered — sediment"]),
            t("cache", &[
                "last turn's billing: steel = served from cache ·",
                "cyan = newly cached · amber = uncached (full price);",
                "the bright cell is the waterline (cache prefix end)",
            ]),
            t("TURNS", &["columns, bottom→top (stack top = R by identity):"]),
            t("█cr", &["tokens read from cache (0.1× — the cheap bulk)"]),
            t("█5m █1h", &["cache WRITES at the 5-min / 1-hour tier (1.25×/2×)"]),
            t("█in", &["uncached input (1×) — near-invisible when healthy"]),
            t("▀wl", &[
                "prev turn's waterline: below steel top = newly",
                "cached · floating above = invalidation depth",
            ]),
            t("▼cmp", &[
                "compaction · ▲thr thrash (cache prefix invalidated",
                "and re-billed) · ◆mdl model switched this turn",
            ]),
            t("lanes", &[
                "out red on truncation · dur brighter with more tools",
                "· ku red when the cache missed (hit < 50%)",
            ]),
        ],
    };

    // current tab pinned first, then GLOBAL, then everything else in order
    let tabbed = [overview, files, turns, agents, events, shell];
    let mut out: Vec<HelpSection> = Vec::with_capacity(11);
    let mut rest: Vec<HelpSection> = Vec::new();
    for (i, s) in tabbed.into_iter().enumerate() {
        if i == tab.min(5) {
            out.push(s);
        } else {
            rest.push(s);
        }
    }
    out.push(global);
    out.extend(rest);
    out.push(fleet);
    out.push(glyphs);
    out.push(numbers);
    out.push(anatomy);
    // section title joins each item's search text, so "/files" finds the tab
    for s in &mut out {
        for it in &mut s.items {
            it.search = format!("{} {}", s.title, it.search).to_lowercase();
        }
    }
    out
}

pub fn render_help(hs: &mut HelpState, tab: usize, f: &mut Frame<'_>, area: Rect) {
    if area.width < 4 || area.height < 4 {
        return;
    }
    let q = hs.filter.to_lowercase();
    let mut body: Vec<Line<'static>> = Vec::new();
    let (mut nsec, mut nhit) = (0usize, 0usize);
    for (si, sec) in help_sections(tab).into_iter().enumerate() {
        let items: Vec<HelpItem> = sec
            .items
            .into_iter()
            .filter(|it| q.is_empty() || it.search.contains(&q))
            .collect();
        if items.is_empty() {
            continue;
        }
        nsec += 1;
        nhit += items.len();
        if !body.is_empty() {
            body.push(Line::from(""));
        }
        let mut head = vec![Span::styled(
            format!(" ▸ {}", sec.title),
            fg(C_STEEL).add_modifier(Modifier::BOLD),
        )];
        if si == 0 && q.is_empty() {
            head.push(Span::styled("  (current tab)".to_string(), fg(C_DIM)));
        }
        body.push(Line::from(head));
        for it in items {
            body.extend(it.lines);
        }
    }
    if body.is_empty() {
        body.push(Line::styled(
            " no matches — esc clears the filter".to_string(),
            fg(C_DIM),
        ));
    }

    // row 0 — the filter box (⌕), live like the fleet search
    let search_line = if hs.typing {
        Line::from(vec![
            Span::styled(" ⌕ ".to_string(), fg(C_AMBER)),
            Span::styled(hs.filter.clone(), fg(C_FG).add_modifier(Modifier::BOLD)),
            Span::styled("▏".to_string(), fg(C_AMBER)),
        ])
    } else if hs.filter.is_empty() {
        Line::styled(
            " ⌕ / filter · j/k scroll · g/G ends · esc close".to_string(),
            fg(C_DIM),
        )
    } else {
        Line::from(vec![
            Span::styled(" ⌕ ".to_string(), fg(C_AMBER)),
            Span::styled(hs.filter.clone(), fg(C_FG).add_modifier(Modifier::BOLD)),
            Span::styled("   / edit · esc clear".to_string(), fg(C_DIM)),
        ])
    };
    let left = if q.is_empty() {
        format!(" {nsec} sections · {nhit} entries")
    } else {
        format!(" {nsec} sections · {nhit} matches")
    };
    // bridge HelpState's plain scroll field into the shared pane's Cell
    let cell = std::cell::Cell::new(hs.scroll);
    render_pane(
        &PaneFrame {
            title: "amtr — help".to_string(),
            width: 72,
            header: vec![search_line],
            body,
            footer: left,
            scroll: &cell,
        },
        f,
        area,
    );
    hs.scroll = cell.get();
}

// ---------------------------------------------------------------------------
// welcome tour — the guided first-launch walkthrough
// ---------------------------------------------------------------------------

/// One stop of the welcome tour, ready to draw. The APP owns the stop list
/// and drives (and animates) the instrument; viz only knows how to place
/// and paint the strip: the cat, its speech, the keys, progress + nav.
pub struct TourCard {
    /// what the cat says (revealed typewriter-style; see `TourTalk`)
    pub speech: String,
    /// the keys that do it (amber) — may be empty
    pub keys: String,
    pub idx: usize,
    pub n: usize,
}

/// The talking state: how many chars of the speech are revealed so far
/// (the mouth moves while `pos < speech.len()`), and a frame counter that
/// keeps ticking after — the idle blink and the mouth cycle both key off it.
#[derive(Clone, Copy, Debug, Default)]
pub struct TourTalk {
    pub pos: usize,
    pub frame: u32,
    /// the char most recently revealed — the mouth is shaped by it (a
    /// vowel opens it, a consonant half-opens it, a space or punctuation
    /// closes it), so the face reads as speaking the words rather than
    /// flapping at random
    pub last: Option<char>,
}

/// Mouth shape for the char just spoken: the dialogue-box convention.
pub fn mouth_for(c: Option<char>) -> &'static str {
    match c {
        Some(c) if "aeiouAEIOU".contains(c) => "o",
        Some('!' | '?') => "▽",
        Some(c) if c.is_alphanumeric() => "‿",
        _ => "ﻌ",
    }
}

/// The guide: a three-row cat, monochrome, built from the established
/// tiny-cat idioms — `=( )=` whiskers, the `ﻌ` cat mouth, `⁼` smug
/// half-lidded eyes, drawn as the superscript `⁼` so they sit above the
/// mouth line — over a pair of shoulders. While `talking` the mouth follows
/// the char being spoken (`mouth_for`); idle blinks `˘˘` every so often;
/// `happy` (the last stop) is `^^`. Width-1 glyphs only (the viz rule) — 7 columns, 3 rows.
pub const CAT_W: u16 = 7;
pub const CAT_H: u16 = 3;
fn cat_rows(talk: TourTalk, talking: bool, happy: bool) -> [Vec<Span<'static>>; 3] {
    let f = talk.frame as usize;
    let mouth = if talking { mouth_for(talk.last) } else { "ﻌ" };
    let eye = if happy {
        "^"
    } else if !talking && f.is_multiple_of(14) {
        "˘"
    } else {
        "⁼"
    };
    let body = fg(C_FG);
    let face = fg(C_WHITE).add_modifier(Modifier::BOLD);
    [
        vec![Span::styled(r" /\ /\ ".to_string(), body)],
        vec![
            Span::styled("=(".to_string(), body),
            Span::styled(format!("{eye}{mouth}{eye}"), face),
            Span::styled(")=".to_string(), body),
        ],
        vec![Span::styled("  ╱ ╲  ".to_string(), body)],
    ]
}

/// Darken every cell outside `keep` (the focused region) and outside
/// `card`: the coach-mark effect. Colors are scaled in RGB rather than
/// tagged DIM so the look is identical on every terminal (SGR 2 is
/// interpreted inconsistently) and asserted in tests. Cells with no explicit
/// color (Reset) get the app bg so text on them still recedes.
fn tour_darken(f: &mut Frame<'_>, area: Rect, keep: Rect, card: Rect) {
    const K: f64 = 0.38;
    let inside = |r: Rect, x: u16, y: u16| {
        x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height
    };
    let buf = f.buffer_mut();
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            if inside(keep, x, y) || inside(card, x, y) {
                continue;
            }
            let Some(cell) = buf.cell_mut((x, y)) else { continue };
            let st = cell.style();
            let nfg = match st.fg {
                Some(Color::Rgb(r, g, b)) => rgb(scale((r, g, b), K)),
                _ => rgb(scale(C_FG, K)),
            };
            let nbg = match st.bg {
                Some(Color::Rgb(r, g, b)) => rgb(scale((r, g, b), K)),
                _ => rgb(C_BG),
            };
            cell.set_style(
                Style::default()
                    .fg(nfg)
                    .bg(nbg)
                    .add_modifier(st.add_modifier & !Modifier::BOLD),
            );
        }
    }
}

/// Where the strip goes: below the focused region when it fits, else
/// above, else pinned to the edge FARTHEST from the focus (top when the
/// focus sits low, bottom when it sits high). `prefer_top` pins it to the
/// focus's own top row (views whose content hugs the bottom). A zero-size
/// focus → centered (WELCOME / FINISH); `focus == area` → bottom edge.
fn tour_place(area: Rect, focus: Rect, prefer_top: bool, w: u16, h: u16) -> Rect {
    let x = area.x + (area.width.saturating_sub(w)) / 2;
    let h = h.min(area.height);
    if focus.width == 0 || focus.height == 0 {
        return centered(area, w, h);
    }
    let bottom_y = (area.y + area.height).saturating_sub(h).max(area.y);
    if prefer_top && focus != area {
        // one row in, so the view's own header line stays readable
        let y = if focus.height > h + 1 { focus.y + 1 } else { focus.y };
        return Rect { x, y, width: w, height: h };
    }
    let below = focus.y + focus.height;
    if focus != area && below + h <= area.y + area.height {
        // one row of breathing space when there is one
        let y = if below + h < area.y + area.height { below + 1 } else { below };
        return Rect { x, y, width: w, height: h };
    }
    if focus != area && focus.y >= area.y + h {
        let y = if focus.y > area.y + h { focus.y - h - 1 } else { focus.y - h };
        return Rect { x, y, width: w, height: h };
    }
    // top edge only when the focus starts in the lower half (its own top
    // row is what would be covered otherwise); a tall focus → bottom
    let area_mid = area.y + area.height / 2;
    let y = if focus != area && focus.y > area_mid { area.y } else { bottom_y };
    Rect { x, y, width: w, height: h }
}

/// Paint one tour stop over the finished frame: darken everything but the
/// focused region, then the cyan strip — the cat on the left, its speech
/// typing out beside it (wrapped to the strip's width, revealed `talk.pos`
/// chars in), the keys line in amber, progress dots + nav on the last row.
/// Cyan on purpose: every other overlay is amber, so the guide reads as a
/// different voice from the instrument.
pub fn render_tour(
    card: &TourCard,
    talk: TourTalk,
    focus: Rect,
    prefer_top: bool,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width < 30 || area.height < 7 {
        return;
    }
    let w = area.width.saturating_sub(2).clamp(30, 84);
    let tw = w as usize - 2 - CAT_W as usize - 2; // borders, cat, gutter
    // wrap the FULL speech first (layout must not jump as it types), then
    // reveal `pos` chars across the wrapped lines
    let full: Vec<String> = wrap_line(&card.speech, tw);
    let total: usize = card.speech.chars().count();
    let talking = talk.pos < total;
    let mut left = talk.pos;
    let mut placed = false; // the caret sits on the line being typed
    let mut rows: Vec<Line<'static>> = Vec::new();
    for l in &full {
        let n = l.chars().count();
        let shown: String = l.chars().take(left).collect();
        let cursor = if talking && !placed && left <= n {
            placed = true;
            "▏"
        } else {
            ""
        };
        left = left.saturating_sub(n + 1); // +1: the swallowed break space
        rows.push(Line::from(vec![
            Span::styled(shown, fg(C_FG)),
            Span::styled(cursor.to_string(), fg(C_CYAN)),
        ]));
    }
    if !card.keys.is_empty() {
        rows.push(Line::styled(card.keys.clone(), fg(C_AMBER)));
    }
    let dots: String = (0..card.n)
        .map(|i| if i <= card.idx { '●' } else { '○' })
        .collect();
    let nav = if card.idx + 1 == card.n {
        "⏎ finish · ← back"
    } else if card.idx == 0 {
        "→ next · esc skip · ? help"
    } else {
        "→ next · ← back · esc skip"
    };
    let pad = tw.saturating_sub(dots.chars().count() + nav.chars().count() + 1);
    rows.push(Line::from(vec![
        Span::styled(dots, fg(C_CYAN)),
        Span::raw(" ".repeat(pad)),
        Span::styled(format!("{nav} "), fg(C_DIM)),
    ]));
    let inner_h = (rows.len() as u16).max(CAT_H);
    let h = (inner_h + 2).min(area.height);
    let rect = tour_place(area, focus, prefer_top, w, h);
    tour_darken(f, area, focus, rect);
    f.render_widget(Clear, rect);
    let block = Block::new()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(fg(C_CYAN))
        .title_top(
            Line::styled(format!(" {}/{} ", card.idx + 1, card.n), fg(C_DIM))
                .alignment(Alignment::Right),
        )
        .style(Style::default().bg(rgb(C_BG)));
    let inner = block.inner(rect);
    f.render_widget(block, rect);
    if inner.height == 0 || inner.width <= CAT_W + 2 {
        return;
    }
    // the cat, top-left; happy on the last stop
    let cat = cat_rows(talk, talking, card.idx + 1 == card.n);
    let cat_lines: Vec<Line<'static>> = cat.into_iter().map(Line::from).collect();
    f.render_widget(
        Paragraph::new(cat_lines),
        Rect { width: CAT_W, height: CAT_H.min(inner.height), ..inner },
    );
    f.render_widget(
        Paragraph::new(rows),
        Rect {
            x: inner.x + CAT_W + 2,
            width: inner.width - CAT_W - 2,
            ..inner
        },
    );
}

// ---------------------------------------------------------------------------
// chrome: ribbon, tabs, scrubber, footer, big-number mode
// ---------------------------------------------------------------------------

pub const TAB_NAMES: [&str; 6] = ["OVERVIEW", "FILES", "TURNS", "AGENTS", "EVENTS", "SHELL"];

/// Row 0 — status ribbon.
/// The `amtr` wordmark for the ribbon — bold-script letterforms swept by a
/// cyan→magenta gradient. Renders single-width in a Nerd Font; falls back to
/// plain glyphs elsewhere (still colored), so the brand always reads as amtr.
fn amtr_wordmark() -> Vec<Span<'static>> {
    const MARK: [(&str, (u8, u8, u8)); 4] = [
        ("𝓪", (64, 202, 226)),
        ("𝓶", (108, 154, 240)),
        ("𝓽", (176, 138, 255)),
        ("𝓻", (232, 100, 180)),
    ];
    let mut out: Vec<Span<'static>> = MARK
        .iter()
        .map(|(g, c)| Span::styled((*g).to_string(), fg(*c).add_modifier(Modifier::BOLD)))
        .collect();
    out.push(Span::raw(" "));
    out
}

pub fn render_ribbon(
    st: &State,
    engine_dead: bool,
    paused: bool,
    amtr3d: bool,
    depth: usize,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let ratio = st.resident as f64 / st.budget.max(1) as f64;
    let zone = zone_color(ratio);
    // the distinct, readable session handle — the primary identity, shown
    // prominently so amtr sessions never blur together
    let name = st
        .meta
        .as_ref()
        .map(|m| {
            if m.name.is_empty() {
                m.session_id.chars().take(8).collect()
            } else {
                m.name.clone()
            }
        })
        .unwrap_or_else(|| "—".to_string());
    // the descriptive title (what it's about) — secondary, elided first
    let title = st
        .meta
        .as_ref()
        .and_then(|m| m.title.clone())
        .unwrap_or_default();
    // strip the redundant `claude-` prefix: `claude-fable-5` → `fable-5`
    // (reads better and makes room for the distinct session name)
    let model = st
        .meta
        .as_ref()
        .map(|m| {
            m.model
                .strip_prefix("claude-")
                .unwrap_or(&m.model)
                .to_string()
        })
        .unwrap_or_default();
    let mut spans: Vec<Span<'static>> = amtr_wordmark();
    if engine_dead {
        spans.push(Span::styled(
            "ENGINE DEAD ".to_string(),
            Style::default()
                .fg(rgb(C_WHITE))
                .bg(rgb(C_RED))
                .add_modifier(Modifier::BOLD),
        ));
    }
    if paused {
        spans.push(Span::styled("⏸ ".to_string(), fg(C_AMBER)));
    }
    if amtr3d {
        // memspace bridge live: this session is streaming to the headset
        spans.push(Span::styled(
            "3D ".to_string(),
            fg(C_CYAN).add_modifier(Modifier::BOLD),
        ));
    }
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    if depth > 0 {
        // drilled into an agent's window: ◂ per level, Backspace returns
        spans.push(Span::styled(
            format!("{} ", "◂".repeat(depth)),
            fg(C_AMBER).add_modifier(Modifier::BOLD),
        ));
    }
    // session NAME — the distinct handle, bright cyan bold, never elided
    spans.push(Span::styled(
        format!("{name} "),
        fg(C_CYAN).add_modifier(Modifier::BOLD),
    ));
    let title_at = spans.len(); // title placeholder — elided FIRST (name wins)
    spans.push(Span::raw(String::new()));
    spans.push(Span::styled(format!("{model} "), fg(C_DIM)));
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!(
            "R {}/{} {:.0}% ",
            fmt_k0(st.resident),
            fmt_k0(st.budget),
            ratio * 100.0
        ),
        fg(C_FG),
    ));
    spans.push(Span::styled("▮".to_string(), fg(zone)));
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!("{}{}/t ", if st.fill_ema >= 0.0 { "+" } else { "−" }, fmt_k1(st.fill_ema.abs() as u64)),
        fg(C_DIM),
    ));
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        match st.compact_eta() {
            Some(eta) => format!("compact≈{:.0}t ", eta.ceil()),
            None => "compact — ".to_string(),
        },
        fg(C_DIM),
    ));
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!("{:.0}ku ", st.cost_total),
        fg(C_AMBER),
    ));
    let running = eff_agents(st)
        .iter()
        .filter(|a| a.state == "running")
        .count();
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    spans.push(Span::styled(
        format!("ag {running}{} ", if running > 0 { "●" } else { "" }),
        if running > 0 { fg(C_CYAN) } else { fg(C_DIM) },
    ));
    if let Some(t) = eff_tasks(st) {
        spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
        spans.push(Span::styled(
            format!("tasks {}/{} ", t.done, t.total),
            fg(C_DIM),
        ));
    }
    spans.push(Span::styled("│ ".to_string(), fg(C_GRID)));
    let (hglyph, hcol, hname) = match st.health.as_ref().map(|h| h.status.as_str()) {
        Some("busy") => ("●", C_GREEN, "busy"),
        Some("idle") => ("○", C_DIM, "idle"),
        Some("stalled") => ("◐", C_AMBER, "stalled"),
        Some("dead") => ("✖", C_RED, "dead"),
        Some("offline") => ("·", C_DIM, "offline"),
        _ => ("·", C_DIM, "—"),
    };
    spans.push(Span::styled(format!("{hglyph}{hname}"), fg(hcol)));
    // SPEC elision rule: truncate the TITLE so every field to its right
    // (and the distinct NAME) survives intact; fields never cut mid-value.
    let other: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let avail = (area.width as usize).saturating_sub(other + 1);
    let mut t = title;
    if t.chars().count() > avail {
        t = if avail >= 2 {
            let cut: String = t.chars().take(avail - 1).collect();
            format!("{cut}…")
        } else {
            String::new()
        };
    }
    spans[title_at] = if t.is_empty() {
        Span::raw(String::new())
    } else {
        Span::styled(format!("{t} "), fg(C_DIM))
    };
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Row 1 — tabs line with the LIVE / REPLAY indicator right-aligned.
pub fn render_tabs(st: &State, ui: &Ui, active: usize, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let right = match ui.cursor {
        None => ("● LIVE".to_string(), C_GREEN),
        Some(t) => (
            format!("« REPLAY t={}/{}", t, st.last_turn().unwrap_or(0)),
            C_AMBER,
        ),
    };
    // SPEC elision rule: shorten the LEFT content (drop hints, then compact
    // tab labels) before ever clipping the LIVE/REPLAY indicator.
    let w = area.width as usize;
    let right_w = right.0.chars().count();
    let hints = "(f)SESSIONS (?)help";
    let full: Vec<String> = TAB_NAMES
        .iter()
        .enumerate()
        .map(|(i, n)| format!("[{}]{} ", i + 1, n))
        .collect();
    let full_w: usize = full.iter().map(|t| t.chars().count()).sum();
    let (labels, show_hints) = if full_w + hints.chars().count() + 1 + right_w <= w {
        (full, true)
    } else if full_w + right_w <= w {
        (full, false)
    } else {
        let compact: Vec<String> = (0..TAB_NAMES.len())
            .map(|i| format!("[{}]", i + 1))
            .collect();
        (compact, false)
    };
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut used = 0usize;
    for (i, txt) in labels.into_iter().enumerate() {
        used += txt.chars().count();
        let style = if i == active {
            fg(C_FG).add_modifier(Modifier::REVERSED)
        } else {
            fg(C_DIM)
        };
        spans.push(Span::styled(txt, style));
    }
    if show_hints {
        used += hints.chars().count();
        spans.push(Span::styled(hints.to_string(), fg(C_DIM)));
    }
    let pad = (area.width as usize)
        .saturating_sub(used)
        .saturating_sub(right.0.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    let mut rstyle = fg(right.1).add_modifier(Modifier::BOLD);
    if ui.cursor.is_none() && !ui.blink {
        rstyle = fg(scale(right.1, 0.6));
    }
    spans.push(Span::styled(right.0, rstyle));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// The scrubber column for a turn (shared with tests).
pub fn scrub_col(turn: u64, total: u64, width: u16) -> u16 {
    if total == 0 || width == 0 {
        return 0;
    }
    ((turn * width as u64) / total).min(width as u64 - 1) as u16
}

/// Row 2 — timeline scrubber: the whole session in one row.
pub fn render_scrubber(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let w = area.width;
    let m = st.turn_count();
    if m == 0 {
        f.render_widget(
            Paragraph::new(Line::styled("·".repeat(w as usize), fg(C_GRID))),
            area,
        );
        return;
    }
    let b = st.budget.max(1) as f64;
    // max resident per bucket from the turn ring
    let mut buckets = vec![0.0f64; w as usize];
    for t in &st.turns {
        let c = scrub_col(t.turn, m, w) as usize;
        buckets[c] = buckets[c].max(t.resident as f64 / b);
    }
    let mut cells: Vec<Span<'static>> = buckets
        .iter()
        .map(|&r| {
            let idx = ((r * 4.0).round() as usize).min(4);
            let ch = if r <= 0.0 { '·' } else { SHADE[idx.max(1)] };
            Span::styled(ch.to_string(), fg(scale(zone_color(r), 0.75)))
        })
        .collect();
    // markers win over shade
    for c in &st.compactions {
        let col = scrub_col(c.turn, m, w) as usize;
        cells[col] = Span::styled("◆", fg(C_MAGENTA));
    }
    for e in &st.events {
        if e.kind == "thrash" {
            let col = scrub_col(e.turn, m, w) as usize;
            cells[col] = Span::styled("▲", fg(C_RED));
        }
    }
    // playhead wins over everything
    let (pt, pcol) = match ui.cursor {
        Some(t) => (t, C_AMBER),
        None => (st.last_turn().unwrap_or(0), C_GREEN),
    };
    let col = scrub_col(pt, m, w) as usize;
    cells[col] = Span::styled("┃", fg(pcol).add_modifier(Modifier::REVERSED));
    f.render_widget(Paragraph::new(Line::from(cells)), area);
}

/// Last row — alert ribbon or contextual key hints. The TURNS footer is a
/// colored mark legend (the eight marks in their band colors).
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
pub fn render_footer(
    st: &State,
    ui: &Ui,
    tab: usize,
    inspect: bool,
    files_view: FilesView,
    shell_view: ShellView,
    shell_follow: bool,
    shell_newer: usize,
    notice: Option<&str>,
    f: &mut Frame<'_>,
    area: Rect,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    // a just-triggered notice (report written, …) wins briefly — the user
    // pressed a key and is looking for the result
    if let Some(msg) = notice {
        f.render_widget(
            Paragraph::new(Line::styled(
                format!(" {msg}"),
                fg(C_GREEN).add_modifier(Modifier::BOLD),
            )),
            area,
        );
        return;
    }
    if let Some(a) = &st.alert {
        let col = severity_color(a.severity);
        let mut style = Style::default().fg(rgb(C_WHITE)).bg(rgb(scale(col, 0.8)));
        if ui.blink {
            style = style.add_modifier(Modifier::BOLD);
        }
        f.render_widget(
            Paragraph::new(Line::styled(
                format!(" ▲ {} — {}  (esc acks)", a.label, a.msg),
                style,
            )),
            area,
        );
        return;
    }
    if tab == 2 {
        // TURNS: colored legend — one visual vocabulary with the chart
        let line = Line::from(vec![
            Span::styled(" █cr".to_string(), fg(C_STEEL)),
            Span::styled(" █5m".to_string(), fg(C_CYAN)),
            Span::styled(" █1h".to_string(), fg(C_ATTACH)),
            Span::styled(" █in".to_string(), fg(C_RED)),
            Span::styled(" ▀wl".to_string(), fg(C_WLINE)),
            Span::styled(" ▼cmp".to_string(), fg(C_MAGENTA)),
            Span::styled(" ▲thr".to_string(), fg(C_RED)),
            Span::styled(" ◆mdl".to_string(), fg(C_WHITE)),
            Span::styled(" · ←/→ turn ⇧±10".to_string(), fg(C_DIM)),
        ]);
        f.render_widget(Paragraph::new(line), area);
        return;
    }
    let hint: String = match tab {
        0 if inspect => "←/→ walk · enter open/peek · p peek · esc exit".into(),
        0 => "m map-mode · +/- rung · ←/→ turn · c post-mortem · i inspect · ? help · w tour".into(),
        1 => match files_view {
            FilesView::History => {
                "j/k select · s sort · v now/history · enter detail · o $EDITOR".into()
            }
            FilesView::Now => "j/k select · v now/history · enter detail · o $EDITOR".into(),
        },
        3 => "j/k select · s sort · a filter · enter expand/jump".into(),
        4 => "j/k select · enter post-mortem/jump · g/G ends".into(),
        5 => {
            let other = match shell_view {
                ShellView::Console => "v retrieval",
                ShellView::Retrieval => "v console",
            };
            if shell_follow {
                format!("j/k browse · enter expand · a err filter · {other} · G follow")
            } else {
                format!(
                    "↑ +{shell_newer} newer · G/End follow · enter expand · a err filter · {other}"
                )
            }
        }
        _ => "j/k select · enter post-mortem/jump · g/G ends".into(),
    };
    f.render_widget(Paragraph::new(Line::styled(format!(" {hint}"), fg(C_DIM))), area);
}

/// Big-number mode (<50×15): the whole screen is the gauge. An art-movement
/// palette tank (one palette rolled per process — see `art_palette`) covers
/// exactly the context-left fraction, draining as the window fills. Tall
/// windows fill bottom-up; flat windows (w > 2h, visually wider than tall)
/// fill left-to-right. Fractional edge = eighth-blocks, brightest at the
/// surface. The only chrome: R% in the top-left corner.
///
/// `blocks` (tab toggles): the second theme — the OVERVIEW MAP's colored
/// content cells fill the used fraction instead of the tank, bare (free
/// space stays terminal background, no dim fill, no box), oriented the same
/// way the tank drains: tall windows fill bottom-up, flat windows (w > 2h)
/// left-to-right. Budget maps exactly to the screen, so painted area reads
/// as fullness — just composed of the real content.
pub fn render_big(st: &State, ui: &Ui, blocks: bool, f: &mut Frame<'_>, area: Rect) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let ratio = st.resident as f64 / st.budget.max(1) as f64;
    let pct = (ratio * 100.0).round() as u64;
    let zone = zone_color(ratio);

    if blocks {
        render_big_blocks(st, ui, f, area);
    } else {
        // gradient tank: cells covered = context left, palette sweeps its
        // stops dark → vivid toward the surface edge
        render_tank(f, area, (1.0 - ratio).clamp(0.0, 1.0), &art_palette());
    }
    // simple readout: percentage + session handle in the top-left corner,
    // nothing else. Over the tank it stays transparent (the tank's own bg
    // shows through); over the block field it sits on an app-bg scrim so it
    // reads as an overlay instead of dissolving into content cells.
    let name = st
        .meta
        .as_ref()
        .map(|m| {
            if m.name.is_empty() {
                m.session_id.chars().take(8).collect()
            } else {
                m.name.clone()
            }
        })
        .unwrap_or_else(|| "—".to_string());
    let mut pct_style = fg(lerp(zone, C_WHITE, 0.20)).add_modifier(Modifier::BOLD);
    let mut name_style = fg(C_FG);
    if blocks {
        pct_style = pct_style.bg(rgb(C_BG));
        name_style = name_style.bg(rgb(C_BG));
    }
    let spans = vec![
        Span::styled(format!(" {pct}%"), pct_style),
        Span::styled(format!(" · {name} "), name_style),
    ];
    let line = Line::from(spans);
    let rect = Rect {
        x: area.x,
        y: area.y,
        width: (line.width() as u16).min(area.width),
        height: 1,
    };
    f.render_widget(Paragraph::new(line), rect);
}

/// The blocks big-gauge theme: the MAP's colored content cells (half-block
/// grid, colors from the current map mode `m` — pulses, waterline and all,
/// via `cell_px`), laid out in the tank's fill orientation: tall windows
/// fill bottom-up (address 0 at the bottom edge, content sediments upward),
/// flat windows (w > 2h) fill left-to-right (column-major). Bare: free and
/// beyond-budget cells stay terminal background. The budget spans the whole
/// area (no rung ladder — big mode has no scale labels), so the painted
/// fraction IS the fullness fraction, exactly like the tank's fill line.
fn render_big_blocks(st: &State, ui: &Ui, f: &mut Frame<'_>, area: Rect) {
    let segs = eff_segs(st);
    let (w, h) = (area.width as usize, area.height as usize);
    let capacity = w * 2 * h; // half-block cells
    let budget = st.budget.max(1);
    let s_tok = (budget as f64 / capacity.max(1) as f64).ceil().max(1.0) as u64;
    let n_cells = (budget.div_ceil(s_tok) as usize).min(capacity);
    let owners = cell_owners(segs, s_tok, n_cells);
    let total: u64 = segs.iter().map(|sg| sg.tok).sum();
    let horiz = area.width > 2 * area.height;
    // logical half-cell index → color (None = free / beyond budget: unpainted)
    let color_at = |ci: usize| -> Option<Color> {
        if ci >= n_cells {
            return None;
        }
        cell_px(st, ui, segs, owners[ci], ci as u64 * s_tok, s_tok, total).color
    };
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(h);
    for row in 0..h {
        let mut spans: Vec<Span<'static>> = Vec::with_capacity(w);
        for col in 0..w {
            let (ci_t, ci_b) = if horiz {
                // column-major from the left edge, top-down within a column
                (col * 2 * h + 2 * row, col * 2 * h + 2 * row + 1)
            } else {
                // row-major from the BOTTOM edge up; a char row's lower half
                // is the earlier address
                let from_bot = h - 1 - row;
                ((2 * from_bot + 1) * w + col, (2 * from_bot) * w + col)
            };
            spans.push(compose(color_at(ci_t), color_at(ci_b)));
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// The gradient tank itself: paint `left` (context-left fraction) of `area`
/// with `pal` swept dark → vivid toward the surface edge. Tall areas fill
/// bottom-up; flat areas (w > 2h) fill left-to-right; fractional surface =
/// eighth-blocks. Shared by big-number mode and the SESSIONS live wall.
pub fn render_tank(f: &mut Frame<'_>, area: Rect, left: f64, pal: &[(u8, u8, u8); 4]) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let grad = |t: f64| palette_color(pal, t);
    let left = left.clamp(0.0, 1.0);
    let horiz = area.width > 2 * area.height;
    let span = if horiz { area.width } else { area.height };
    let cells = span as f64 * left;
    let full = (cells.floor() as u16).min(span);
    let lvl = ((cells - full as f64) * 8.0).round() as usize;
    for i in 0..full {
        let t = if full > 1 {
            i as f64 / (full - 1) as f64
        } else {
            1.0
        };
        let slice = if horiz {
            Rect {
                x: area.x + i,
                y: area.y,
                width: 1,
                height: area.height,
            }
        } else {
            Rect {
                x: area.x,
                y: area.y + area.height - 1 - i,
                width: area.width,
                height: 1,
            }
        };
        f.render_widget(
            Block::default().style(Style::default().bg(rgb(grad(t)))),
            slice,
        );
    }
    if lvl > 0 && full < span {
        // fractional surface slice: eighth-blocks growing toward the fill
        let edge = fg(grad(1.0));
        if horiz {
            let col = Rect {
                x: area.x + full,
                y: area.y,
                width: 1,
                height: area.height,
            };
            let rows: Vec<Line<'static>> = (0..area.height)
                .map(|_| Line::styled(LBLOCK[lvl - 1].to_string(), edge))
                .collect();
            f.render_widget(Paragraph::new(rows), col);
        } else {
            let row = Rect {
                x: area.x,
                y: area.y + area.height - 1 - full,
                width: area.width,
                height: 1,
            };
            let surface: String = std::iter::repeat(SPARK[lvl - 1])
                .take(area.width as usize)
                .collect();
            f.render_widget(Paragraph::new(Line::styled(surface, edge)), row);
        }
    }
}
