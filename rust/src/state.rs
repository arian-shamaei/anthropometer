//! Mirrored session state: everything the UI knows, fed exclusively by
//! [`crate::ipc::Update`] messages. All histories are capped rings (SPEC §e):
//! turns 512, faccess 4096, log 200, events 256, segs 1024+overhead.
//!
//! The state also owns the UI-side derived signals: fill-rate EMA, compaction
//! ETA (least-squares slope over the last 16 turns), thrash detection for the
//! MAP flash, animation pulse counters, and the alert slot.

use std::collections::{HashMap, VecDeque};

use crate::ipc::{
    AgentRec, Backfill, CmdRec, Compaction, EventRec, Faccess, FileRec, Health, MapMsg, Meta,
    RetRec, Seg, Sess, Severity, Snapshot, Tasks, Turn, Update,
};

pub const TURNS_CAP: usize = 512;
pub const FACCESS_CAP: usize = 4096;
pub const LOG_CAP: usize = 200;
pub const EVENTS_CAP: usize = 256;
/// SHELL console ring — mirrors the `cmds ≤256` backfill cap (SPEC §e).
pub const CMDS_CAP: usize = 256;
/// RETRIEVAL feed ring — mirrors the `rets ≤256` backfill cap (SPEC §b/§e).
pub const RETS_CAP: usize = 256;
/// 1024 segs + the overhead segment (SPEC §e).
pub const SEGS_CAP: usize = 1024 + 1;

/// Fixed 8-hue file accent wheel (SPEC §e MAP mode 1), assigned at first
/// access, cycled.
// files carry individual hues from a cohesive COOL family (cyan→azure→teal),
// bright for the dark terminal — so file cells read as one group, distinct
// from gold user / magenta attach / violet reasoning / green assistant
pub const FILE_HUES: [(u8, u8, u8); 8] = [
    (64, 202, 226),
    (84, 168, 248),
    (46, 216, 208),
    (128, 194, 252),
    (40, 182, 222),
    (150, 208, 240),
    (72, 150, 240),
    (36, 214, 234),
];

/// The newest unacked alert (footer ribbon).
#[derive(Debug, Clone)]
pub struct Alert {
    pub label: String,
    pub severity: Severity,
    pub msg: String,
}

#[derive(Default)]
pub struct State {
    // --- session identity & meta ---
    pub meta: Option<Meta>,
    pub ready: bool,
    pub engine_version: String,

    // --- capped rings ---
    pub turns: VecDeque<Turn>,       // ≤ 512, oldest→newest
    pub faccess: VecDeque<Faccess>,  // ≤ 4096
    pub log: VecDeque<String>,       // ≤ 200
    pub events: VecDeque<EventRec>,  // ≤ 256, oldest→newest
    pub cmds: VecDeque<CmdRec>,      // ≤ 256, oldest→newest, seq-stamped
    cmd_seq: u64,
    pub rets: VecDeque<RetRec>,      // ≤ 256, oldest→newest, seq-stamped
    ret_seq: u64,

    // --- MAP ---
    pub map_rev: u64,
    pub alpha: f64,
    pub segs: Vec<Seg>, // ≤ 1024 + overhead; segs[0] is the overhead segment

    // --- tables ---
    pub files: HashMap<u64, FileRec>,
    /// file id → accent hue index (first-access order % 8)
    pub file_hue: HashMap<u64, usize>,
    hue_next: usize,
    pub cats: HashMap<String, u64>,
    pub compactions: Vec<Compaction>, // all
    pub agents: Vec<AgentRec>,        // upsert by id, first-appearance order
    pub tasks: Option<Tasks>,
    pub health: Option<Health>,
    pub fleet: Vec<Sess>,

    // --- headline numbers ---
    pub budget: u64,
    pub t_auto: f64,
    pub resident: u64,
    pub waterline: u64,
    pub last_cc: u64,
    pub cost_total: f64,
    pub fill_ema: f64, // EMA of ΔR per turn, k=8

    // --- animation pulses (decremented on the 80 ms pulse clock) ---
    pub write_pulse: u8,
    pub write_span: (u64, u64), // token address range of freshly appended segs
    pub thrash_pulse: u8,
    pub thrash_span: (u64, u64), // re-created span (C_t .. C_{t-1})
    pub compact_sweep: u8,       // 3-frame dim sweep after a compaction
    /// TURNS new-turn white-blend pulse: set to 6 by a NEW turn index
    /// (never same-index upserts, never backfill).
    pub turn_pulse: u8,
    /// FILES NOW entry pulse: a LIVE faccess (post-`ready` only, never
    /// backfill) flashes the entering file's AGE/OP cells white.
    pub touch_pulse: u8,
    pub touch_file: u64,
    /// AGENTS bar-tip pulse: agent id → frames left; set to 6 when a running
    /// agent's own_tok grows on an upsert.
    pub agent_pulse: HashMap<String, u8>,
    /// SHELL arrival pulse: a LIVE `cmd` (never backfill) flashes its prompt
    /// line white for 6 frames; `cmd_pulse_seq` names the entry.
    pub cmd_pulse: u8,
    pub cmd_pulse_seq: u64,
    /// RETRIEVAL arrival pulse: a LIVE `ret` (never backfill) flashes its
    /// row white for 6 frames; `ret_pulse_seq` names the entry.
    pub ret_pulse: u8,
    pub ret_pulse_seq: u64,

    // --- replay ---
    pub replay: Option<Snapshot>,

    // --- alerts (newest unacked) ---
    pub alert: Option<Alert>,
    pressure_zone: u8, // 0 green / 1 amber / 2 red — for crossing detection
    eta_alerted: bool,
}

impl State {
    pub fn new() -> Self {
        let mut s = State {
            alpha: 1.0,
            t_auto: 0.85,
            budget: 200_000,
            ..Default::default()
        };
        s.push_log("waiting for engine…".to_string());
        s
    }

    pub fn push_log(&mut self, line: String) {
        self.log.push_back(line);
        while self.log.len() > LOG_CAP {
            self.log.pop_front();
        }
    }

    /// Ensure a file id has an accent hue (assigned at first sight, cycled).
    pub fn ensure_hue(&mut self, id: u64) -> usize {
        if let Some(&h) = self.file_hue.get(&id) {
            return h;
        }
        let h = self.hue_next % FILE_HUES.len();
        self.hue_next += 1;
        self.file_hue.insert(id, h);
        h
    }

    pub fn hue_of(&self, id: u64) -> (u8, u8, u8) {
        FILE_HUES[self.file_hue.get(&id).copied().unwrap_or(0) % FILE_HUES.len()]
    }

    /// Sum of the current map's segment tokens (== R by construction).
    pub fn map_total(&self) -> u64 {
        self.segs.iter().map(|s| s.tok).sum()
    }

    /// Last turn number (0-based), if any turn was seen.
    pub fn last_turn(&self) -> Option<u64> {
        self.turns.back().map(|t| t.turn)
    }

    /// Total turn count M for the scrubber (last turn + 1).
    pub fn turn_count(&self) -> u64 {
        self.last_turn().map(|t| t + 1).unwrap_or(0)
    }

    /// Least-squares slope of R over the last ≤16 turns (tokens per turn).
    pub fn slope(&self) -> f64 {
        let n = self.turns.len().min(16);
        if n < 2 {
            return 0.0;
        }
        let pts: Vec<(f64, f64)> = self
            .turns
            .iter()
            .skip(self.turns.len() - n)
            .map(|t| (t.turn as f64, t.resident as f64))
            .collect();
        let nf = n as f64;
        let sx: f64 = pts.iter().map(|p| p.0).sum();
        let sy: f64 = pts.iter().map(|p| p.1).sum();
        let sxx: f64 = pts.iter().map(|p| p.0 * p.0).sum();
        let sxy: f64 = pts.iter().map(|p| p.0 * p.1).sum();
        let denom = nf * sxx - sx * sx;
        if denom.abs() < 1e-9 {
            return 0.0;
        }
        (nf * sxy - sx * sy) / denom
    }

    /// ETA to auto-compact in turns: `(T_auto·B − R)/max(1,slope)`; None when
    /// the slope is non-positive (not filling).
    pub fn compact_eta(&self) -> Option<f64> {
        let slope = self.slope();
        if slope <= 0.0 || self.budget == 0 {
            return None;
        }
        let target = self.t_auto * self.budget as f64;
        let room = target - self.resident as f64;
        if room <= 0.0 {
            return Some(0.0);
        }
        Some(room / slope.max(1.0))
    }

    fn raise_alert(&mut self, label: &str, severity: Severity, msg: String) {
        self.alert = Some(Alert {
            label: label.to_string(),
            severity,
            msg,
        });
    }

    pub fn ack_alert(&mut self) {
        self.alert = None;
    }

    /// Reset all per-session state (re-attach / new `meta`). Fleet, log and
    /// engine identity survive.
    fn reset_session(&mut self) {
        self.ready = false;
        self.turns.clear();
        self.faccess.clear();
        self.events.clear();
        self.map_rev = 0;
        self.alpha = 1.0;
        self.segs.clear();
        self.files.clear();
        self.file_hue.clear();
        self.hue_next = 0;
        self.cats.clear();
        self.compactions.clear();
        self.agents.clear();
        self.tasks = None;
        self.health = None;
        self.resident = 0;
        self.waterline = 0;
        self.last_cc = 0;
        self.cost_total = 0.0;
        self.fill_ema = 0.0;
        self.write_pulse = 0;
        self.thrash_pulse = 0;
        self.compact_sweep = 0;
        self.turn_pulse = 0;
        self.touch_pulse = 0;
        self.touch_file = 0;
        self.agent_pulse.clear();
        self.cmds.clear();
        self.cmd_seq = 0;
        self.cmd_pulse = 0;
        self.cmd_pulse_seq = 0;
        self.rets.clear();
        self.ret_seq = 0;
        self.ret_pulse = 0;
        self.ret_pulse_seq = 0;
        self.replay = None;
        self.alert = None;
        self.pressure_zone = 0;
        self.eta_alerted = false;
    }

    fn apply_turn(&mut self, t: Turn) {
        // The engine upserts: a turn is re-emitted when streamed same-requestId
        // usage or a trailing turn_duration updates it. Replace in place — the
        // EMA/alert edges already fired for this index.
        if let Some(back) = self.turns.back_mut() {
            if back.turn == t.turn {
                self.cost_total += t.cost_u - back.cost_u;
                self.resident = t.resident;
                self.waterline = t.waterline;
                self.last_cc = t.cc;
                *back = t;
                return;
            }
        }
        // fill-rate EMA (k=8) on ΔR per turn
        if let Some(prev) = self.turns.back() {
            let dr = t.resident as f64 - prev.resident as f64;
            let k = 2.0 / (8.0 + 1.0);
            self.fill_ema += (dr - self.fill_ema) * k;

            // UI-side thrash flash: prefix invalidation C_t < C_{t-1} − 1024
            if t.waterline + 1024 < prev.waterline {
                self.thrash_pulse = 6;
                self.thrash_span = (t.waterline, prev.waterline);
                self.raise_alert(
                    "CACHE_THRASH",
                    Severity::Warn,
                    format!(
                        "waterline fell {} → {}",
                        fmt_k0(prev.waterline),
                        fmt_k0(t.waterline)
                    ),
                );
            }
        }
        self.resident = t.resident;
        self.waterline = t.waterline;
        self.last_cc = t.cc;
        self.cost_total += t.cost_u;

        // pressure zone crossings (fixed 0.60 / 0.85 thresholds)
        if self.budget > 0 {
            let ratio = t.resident as f64 / self.budget as f64;
            let zone = if ratio >= 0.85 {
                2
            } else if ratio >= 0.60 {
                1
            } else {
                0
            };
            if zone > self.pressure_zone {
                if zone == 2 {
                    self.raise_alert(
                        "PRESSURE_RED",
                        Severity::Error,
                        format!("R {:.0}% of budget", ratio * 100.0),
                    );
                } else {
                    self.raise_alert(
                        "PRESSURE_AMBER",
                        Severity::Warn,
                        format!("R {:.0}% of budget", ratio * 100.0),
                    );
                }
            }
            self.pressure_zone = zone;
        }

        // compact-ETA alert, edge-triggered
        match self.compact_eta() {
            Some(eta) if eta <= 3.0 => {
                if !self.eta_alerted {
                    self.raise_alert(
                        "COMPACT_ETA",
                        Severity::Warn,
                        format!("auto-compact in ≈{:.0} turns", eta.max(0.0)),
                    );
                    self.eta_alerted = true;
                }
            }
            _ => self.eta_alerted = false,
        }

        self.turns.push_back(t);
        while self.turns.len() > TURNS_CAP {
            self.turns.pop_front();
        }
        // TURNS write-head: a genuinely new index pulses (upserts returned
        // early above; backfill never routes through apply_turn)
        self.turn_pulse = 6;
    }

    fn apply_event(&mut self, e: EventRec) {
        if e.severity >= Severity::Warn {
            let label = match e.kind.as_str() {
                "api_error" => "API_ERROR",
                "model_fallback" => "MODEL_FALLBACK",
                "agent_failed" => "AGENT_FAILED",
                "stall" => "STALLED",
                "thrash" => "CACHE_THRASH",
                "pressure" => "PRESSURE",
                "compaction" => "COMPACTION",
                _ => "EVENT",
            };
            self.raise_alert(label, e.severity, e.msg.clone());
        }
        self.events.push_back(e);
        while self.events.len() > EVENTS_CAP {
            self.events.pop_front();
        }
    }

    fn apply_faccess(&mut self, fa: Faccess, live: bool) {
        self.ensure_hue(fa.file);
        // FILES NOW entry pulse: LIVE accesses only — backfill floods (which
        // route here with live=false) must never pulse.
        if live && self.ready {
            self.touch_pulse = 6;
            self.touch_file = fa.file;
        }
        self.faccess.push_back(fa);
        while self.faccess.len() > FACCESS_CAP {
            self.faccess.pop_front();
        }
    }

    fn apply_map(&mut self, m: MapMsg) {
        self.map_rev = m.rev;
        self.alpha = m.alpha;
        self.segs = m.segs;
        self.segs.truncate(SEGS_CAP);
        let fids: Vec<u64> = self.segs.iter().filter_map(|s| s.file).collect();
        for fid in fids {
            self.ensure_hue(fid);
        }
    }

    fn apply_map_add(&mut self, rev: u64, segs: Vec<Seg>) {
        if rev != self.map_rev {
            self.push_log(format!(
                "stale map_add rev {rev} (have {}), ignored",
                self.map_rev
            ));
            return;
        }
        let lo = self.map_total();
        for s in segs {
            if let Some(fid) = s.file {
                self.ensure_hue(fid);
            }
            if self.segs.len() < SEGS_CAP {
                self.segs.push(s);
            }
        }
        let hi = self.map_total();
        if hi > lo {
            self.write_pulse = 6; // white write-head pulse at the tail
            self.write_span = (lo, hi);
        }
    }

    /// Push one Bash execution into the SHELL ring, stamping its UI-side seq
    /// (selection anchors by seq identity, never index).
    fn push_cmd(&mut self, mut c: CmdRec) -> u64 {
        c.seq = self.cmd_seq;
        self.cmd_seq += 1;
        let seq = c.seq;
        self.cmds.push_back(c);
        while self.cmds.len() > CMDS_CAP {
            self.cmds.pop_front();
        }
        seq
    }

    /// Push one external retrieval into the RETRIEVAL ring, stamping its
    /// UI-side seq (same identity law as the console).
    fn push_ret(&mut self, mut r: RetRec) -> u64 {
        r.seq = self.ret_seq;
        self.ret_seq += 1;
        let seq = r.seq;
        self.rets.push_back(r);
        while self.rets.len() > RETS_CAP {
            self.rets.pop_front();
        }
        seq
    }

    fn upsert_agent(&mut self, a: AgentRec) {
        if a.state == "failed" {
            self.raise_alert(
                "AGENT_FAILED",
                Severity::Warn,
                format!("{} failed", a.agent_type.clone().unwrap_or_else(|| a.id.clone())),
            );
        }
        if let Some(slot) = self.agents.iter_mut().find(|x| x.id == a.id) {
            // AGENTS bar-tip pulse on own_tok growth of a running agent
            // (first sight can't pulse — there is no old value to grow from)
            if a.state == "running" && a.own_tok > slot.own_tok {
                self.agent_pulse.insert(a.id.clone(), 6);
            }
            *slot = a;
        } else {
            self.agents.push(a);
        }
    }

    fn apply_compaction(&mut self, c: Compaction) {
        self.compact_sweep = 3;
        self.compactions.push(c);
        self.compactions.sort_by_key(|c| c.turn);
    }

    fn apply_backfill(&mut self, b: Backfill) {
        // A backfill is the authoritative history snapshot for an attach —
        // REPLACE the rings (idempotent across re-attaches), don't append.
        self.turns.clear();
        self.faccess.clear();
        self.events.clear();
        self.compactions.clear();
        self.agents.clear();
        self.agent_pulse.clear();
        self.cmds.clear();
        self.cmd_pulse = 0;
        self.rets.clear();
        self.ret_pulse = 0;
        self.cost_total = 0.0;
        self.fill_ema = 0.0;
        let mut turns = b.turns;
        turns.sort_by_key(|t| t.turn);
        for t in turns {
            // no pulses / alerts for history: apply the ring + numbers only
            if let Some(prev) = self.turns.back() {
                let dr = t.resident as f64 - prev.resident as f64;
                self.fill_ema += (dr - self.fill_ema) * (2.0 / 9.0);
            }
            self.resident = t.resident;
            self.waterline = t.waterline;
            self.last_cc = t.cc;
            self.cost_total += t.cost_u;
            self.turns.push_back(t);
            while self.turns.len() > TURNS_CAP {
                self.turns.pop_front();
            }
        }
        if self.budget > 0 {
            let ratio = self.resident as f64 / self.budget as f64;
            self.pressure_zone = if ratio >= 0.85 {
                2
            } else if ratio >= 0.60 {
                1
            } else {
                0
            };
        }
        let mut fas = b.faccess;
        fas.sort_by_key(|f| f.turn);
        for fa in fas {
            self.apply_faccess(fa, false); // history: no entry pulse
        }
        for c in b.compactions {
            self.compactions.push(c);
        }
        self.compactions.sort_by_key(|c| c.turn);
        for a in b.agents {
            self.upsert_agent(a);
        }
        for e in b.events {
            self.events.push_back(e);
            while self.events.len() > EVENTS_CAP {
                self.events.pop_front();
            }
        }
        // SHELL ring: replace, ordered by (epoch, turn), seq stamped, NO
        // pulse — history must never flash the console.
        let mut cmds = b.cmds;
        cmds.sort_by(|a, b| {
            a.epoch
                .partial_cmp(&b.epoch)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.turn.cmp(&b.turn))
        });
        for c in cmds {
            self.push_cmd(c);
        }
        // RETRIEVAL ring: same law — replace, (epoch, turn) order, seq
        // stamped, NO pulse.
        let mut rets = b.rets;
        rets.sort_by(|a, b| {
            a.epoch
                .partial_cmp(&b.epoch)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.turn.cmp(&b.turn))
        });
        for r in rets {
            self.push_ret(r);
        }
    }

    /// Apply one Update. Returns true when the screen should be redrawn
    /// (everything but pure-noop cases redraws).
    pub fn apply(&mut self, u: Update) -> bool {
        match u {
            Update::Init {
                engine_version,
                sessions,
                default_session,
            } => {
                self.engine_version = engine_version.clone();
                self.fleet = sessions;
                self.push_log(match default_session {
                    Some(s) => format!("engine {engine_version} — default session {s}"),
                    None => format!("engine {engine_version} — no default session"),
                });
            }
            Update::Meta(m) => {
                let switched = self
                    .meta
                    .as_ref()
                    .map(|old| {
                        old.session_id != m.session_id
                            || (m.attach_gen > 0 && old.attach_gen != m.attach_gen)
                    })
                    .unwrap_or(false);
                if switched {
                    self.reset_session();
                }
                if m.budget > 0 {
                    self.budget = m.budget;
                }
                if m.t_auto > 0.0 {
                    self.t_auto = m.t_auto;
                }
                self.push_log(format!(
                    "meta: {} · {} · B={}",
                    m.session_id,
                    m.model,
                    fmt_k0(self.budget)
                ));
                self.meta = Some(m);
            }
            Update::Backfill(b) => self.apply_backfill(b),
            Update::Ready {
                session_id,
                turns,
                resident,
                budget,
            } => {
                self.ready = true;
                if budget > 0 {
                    self.budget = budget;
                }
                if resident > 0 {
                    self.resident = resident;
                }
                self.push_log(format!(
                    "ready: {session_id} — {turns} turns, R {}",
                    fmt_k0(resident)
                ));
            }
            Update::Turn(t) => self.apply_turn(t),
            Update::Map(m) => self.apply_map(m),
            Update::MapAdd { rev, segs } => self.apply_map_add(rev, segs),
            Update::Files { upserts } => {
                for f in upserts {
                    self.ensure_hue(f.id);
                    self.files.insert(f.id, f);
                }
            }
            Update::Faccess(fa) => self.apply_faccess(fa, true),
            Update::Cats { totals } => self.cats = totals,
            Update::Compaction(c) => self.apply_compaction(c),
            Update::Agent(a) => self.upsert_agent(a),
            Update::Cmd(c) => {
                // a live arrival (backfill routes through apply_backfill and
                // never pulses); invisible in replay by construction — the
                // new entry's turn is always > the cursor
                let seq = self.push_cmd(c);
                self.cmd_pulse = 6;
                self.cmd_pulse_seq = seq;
            }
            Update::Ret(r) => {
                // same live-arrival law as `cmd`: backfill routes through
                // apply_backfill and never pulses; invisible in replay by
                // construction (the new entry's turn is always > the cursor)
                let seq = self.push_ret(r);
                self.ret_pulse = 6;
                self.ret_pulse_seq = seq;
            }
            Update::Tasks(t) => self.tasks = Some(t),
            Update::Health(h) => self.health = Some(h),
            Update::Event(e) => self.apply_event(e),
            Update::Fleet { sessions } => self.fleet = sessions,
            Update::Snapshot(s) => self.replay = Some(s),
            // Peek is UI-modal state (the App stores/drops it against the
            // INSPECT selection before State ever sees it) — never session
            // state; the arm exists only for match exhaustiveness.
            Update::Peek(_) | Update::ReportDone { .. } => {}
            Update::Log { msg } => self.push_log(msg),
        }
        true
    }
}

// ---------------------------------------------------------------------------
// small shared formatters (used by state alerts and every renderer)
// ---------------------------------------------------------------------------

/// Tokens, no decimals: `591k`, `842`, `1200k`.
pub fn fmt_k0(tok: u64) -> String {
    if tok < 1000 {
        format!("{tok}")
    } else {
        format!("{:.0}k", tok as f64 / 1000.0)
    }
}

/// Tokens, one decimal: `512.3k`, `842`.
pub fn fmt_k1(tok: u64) -> String {
    if tok < 1000 {
        format!("{tok}")
    } else {
        format!("{:.1}k", tok as f64 / 1000.0)
    }
}

/// Duration in ms, humanized: `8.4s`, `102ms`, `1m40s`.
pub fn fmt_dur(ms: u64) -> String {
    if ms < 1000 {
        format!("{ms}ms")
    } else if ms < 100_000 {
        format!("{:.1}s", ms as f64 / 1000.0)
    } else {
        let s = ms / 1000;
        format!("{}m{:02}s", s / 60, s % 60)
    }
}

/// Age in seconds, humanized: `42s`, `7m`, `3h`, `2d`.
pub fn fmt_age(secs: f64) -> String {
    // floor per unit: 59.9s is "59s", never "60s" (which reads as a minute)
    let s = secs.max(0.0) as u64;
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m", s / 60)
    } else if s < 86400 {
        format!("{}h", s / 3600)
    } else {
        format!("{}d", s / 86400)
    }
}
