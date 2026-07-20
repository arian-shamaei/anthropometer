//! amtr — a btop-style diagnostic instrument for Claude Code sessions.
//!
//! This process owns ONLY the terminal UI (layout, keys, renderers). All data
//! — session discovery, transcript tailing, token accounting — lives in the
//! Python child (`amtr_engine.py`), spoken to over newline-delimited JSON via
//! [`ipc`]. SPEC.md is the normative contract.

mod demo;
mod ipc;
mod state;
mod viz;

use std::sync::mpsc::{Receiver, Sender, TryRecvError};
use std::time::{Duration, Instant};

use ratatui::Frame;
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Color, Style};
use ratatui::widgets::{Block, Paragraph};

use ipc::{Control, Update};
use state::State;
use viz::{AgentFilter, AgentSort, FileSort, FilesView, MapMode, ShellFilter, ShellView, Tier, Ui};

const BG: Color = Color::Rgb(10, 11, 14);

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const USAGE: &str = "\
amtr — context monitor for Claude Code sessions (UI side; data via amtr_engine.py)

usage: amtr [--session FILE|ID] [--project PATH] [--budget N]
            [--engine PATH] [--python PATH] [--engine-args ARGS...]

  --session      attach to a specific session (.jsonl path or session id)
  --project      newest session under this project path
  --budget       pin the context budget (tokens); passed to the engine
  --engine       path to amtr_engine.py (default: repo root, or $AMTR_ENGINE)
  --python       python interpreter (default: python3)
  --engine-args  everything after this flag goes to the engine argv verbatim
  --help         this text

  --demo         run a deterministic demo session (no engine) — the
                 reproducible scene source for visual/animation validation

keys: 1-6 tabs · f sessions · ? help · ←/→ scrub · m map mode · q quit";

struct Cli {
    help: bool,
    demo: bool,
    python: String,
    engine: std::path::PathBuf,
    passthrough: Vec<String>,
}

fn parse_args(argv: &[String]) -> Result<Cli, String> {
    let mut cli = Cli {
        help: false,
        demo: false,
        python: "python3".to_string(),
        engine: ipc::default_engine_path(),
        passthrough: Vec::new(),
    };
    let mut i = 0;
    while i < argv.len() {
        let a = argv[i].clone();
        let val = |i: &mut usize| -> Result<String, String> {
            *i += 1;
            argv.get(*i)
                .cloned()
                .ok_or_else(|| format!("{a} needs a value"))
        };
        match argv[i].as_str() {
            "--help" | "-h" => cli.help = true,
            "--demo" => cli.demo = true,
            "--python" => cli.python = val(&mut i)?,
            "--engine" => cli.engine = std::path::PathBuf::from(val(&mut i)?),
            "--session" => {
                let v = val(&mut i)?;
                cli.passthrough.push("--session".into());
                cli.passthrough.push(v);
            }
            "--project" => {
                let v = val(&mut i)?;
                cli.passthrough.push("--project".into());
                cli.passthrough.push(v);
            }
            "--budget" => {
                let v = val(&mut i)?;
                cli.passthrough.push("--budget".into());
                cli.passthrough.push(v);
            }
            "--engine-args" => {
                cli.passthrough.extend(argv[i + 1..].iter().cloned());
                break;
            }
            other => return Err(format!("unknown flag: {other}")),
        }
        i += 1;
    }
    Ok(cli)
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

struct App {
    tx: Sender<Control>,
    rx: Receiver<Update>,
    st: State,

    tab: usize, // 0..=5
    cursor: Option<u64>,
    seek_inflight: Option<u64>,
    seek_pending: Option<u64>,

    // OVERVIEW INSPECT mode (`i`): walk the MAP's segments in prompt order
    /// while true, ←/→ j/k are CAPTURED (they walk segments, not turns)
    inspect: bool,
    /// index into eff_segs (0 = the overhead segment); clamped at use — the
    /// seg list can shrink under it (replay snapshots)
    inspect_idx: usize,
    /// the open peek overlay (Some ⟺ open). Stored only when the reply
    /// matches the currently selected seg id — stale replies are dropped.
    peek: Option<ipc::PeekMsg>,

    // FILES
    file_sel: usize,
    file_sort: FileSort,
    file_detail: bool,
    /// HISTORY (default) ↔ NOW; resets to HISTORY on re-attach
    files_view: FilesView,
    // AGENTS (unified ledger: selection + sort + filter + wf overrides)
    agent_sel: usize,
    agent_sort: AgentSort,
    agent_filter: AgentFilter,
    /// manual wf expand/collapse overrides (win over the automatic rule)
    wf_open: std::collections::HashMap<String, bool>,
    /// drill-in breadcrumb: parent session PATHS; Backspace pops (SPEC e)
    attach_stack: Vec<String>,
    // EVENTS
    event_sel: usize,
    // SHELL (console posture: tail-pinned follow vs seq-anchored browse)
    /// tail-pinned follow (default). While true there is no selection bar.
    shell_follow: bool,
    /// selection anchor by SEQ IDENTITY (valid when `!shell_follow`) — an
    /// index anchor would slide as entries arrive.
    shell_sel: u64,
    /// `Enter` expand: the selection while browsing, verbose follow while
    /// following (the newest entry renders expanded as it streams).
    shell_expand: bool,
    shell_filter: ShellFilter,
    /// CONSOLE (default) ↔ RETRIEVAL perspective; resets on re-attach.
    /// `shell_follow` is SHARED between the perspectives (one tail posture);
    /// selection/expand/filter are per-view.
    shell_view: ShellView,
    /// RETRIEVAL selection anchor by SEQ IDENTITY (valid when `!shell_follow`)
    ret_sel: u64,
    ret_expand: bool,
    ret_filter: ShellFilter,
    // SESSIONS picker
    fleet_sel: usize,
    /// live search query in the SESSIONS overlay: filters the roster by name /
    /// project, or is attached directly when it is a jsonl path
    fleet_query: String,
    /// MAP pane (w, h) as last rendered — lets the +/- handler clamp the rung
    /// override at press time instead of banking presses past the ladder edge
    map_geom: std::cell::Cell<(u16, u16)>,

    // overlays (dispatch priority: help > post-mortem > fleet > tabs)
    show_help: bool,
    help_page: usize,
    show_fleet: bool,
    postmortem: Option<usize>,

    map_mode: MapMode,
    rung_override: i8,

    paused: bool,
    /// one draw is allowed through the pause gate (pause-toggle feedback)
    force_draw: bool,
    blink: bool,
    dirty: bool,
    quitting: bool,
    engine_dead: bool,
    pending_editor: Option<String>,
    /// demo/testbench mode: no engine, so peek requests are answered locally
    demo: bool,
    /// transient footer notice (report written, etc.) + expiry epoch
    notice: Option<(String, f64)>,
    /// fixed clock for tests (heat decay determinism)
    now_override: Option<f64>,
}

impl App {
    fn new(tx: Sender<Control>, rx: Receiver<Update>) -> Self {
        App {
            tx,
            rx,
            st: State::new(),
            tab: 0,
            cursor: None,
            seek_inflight: None,
            seek_pending: None,
            inspect: false,
            inspect_idx: 0,
            peek: None,
            file_sel: 0,
            file_sort: FileSort::Size,
            file_detail: false,
            files_view: FilesView::History,
            agent_sel: 0,
            agent_sort: AgentSort::Recent,
            agent_filter: AgentFilter::All,
            wf_open: std::collections::HashMap::new(),
            attach_stack: Vec::new(),
            event_sel: 0,
            shell_follow: true,
            shell_sel: 0,
            shell_expand: false,
            shell_filter: ShellFilter::All,
            shell_view: ShellView::Console,
            ret_sel: 0,
            ret_expand: false,
            ret_filter: ShellFilter::All,
            fleet_sel: 0,
            fleet_query: String::new(),
            map_geom: std::cell::Cell::new((0, 0)),
            show_help: false,
            help_page: 0,
            show_fleet: false,
            postmortem: None,
            map_mode: MapMode::Class,
            rung_override: 0,
            paused: false,
            force_draw: false,
            blink: true,
            dirty: true,
            quitting: false,
            engine_dead: false,
            pending_editor: None,
            demo: false,
            notice: None,
            now_override: None,
        }
    }

    fn send(&self, ctrl: Control) {
        let _ = self.tx.send(ctrl); // a dead engine can't crash a keypress
    }

    fn now_epoch(&self) -> f64 {
        self.now_override.unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0)
        })
    }

    fn apply_update(&mut self, u: Update) {
        // FILES view + per-session AGENTS view state reset to defaults on a
        // re-attach (a meta for a DIFFERENT session; same-session re-emits —
        // model switch, budget bump — must not disturb the view).
        if let Update::Meta(ref m) = u {
            let switched = self
                .st
                .meta
                .as_ref()
                .map(|old| old.session_id != m.session_id)
                .unwrap_or(true);
            if switched {
                self.reset_inspect(None);
                self.files_view = FilesView::History;
                self.file_sel = 0;
                self.agent_sel = 0;
                self.wf_open.clear();
                self.shell_follow = true;
                self.shell_sel = 0;
                self.shell_expand = false;
                self.shell_filter = ShellFilter::All;
                self.shell_view = ShellView::Console;
                self.ret_sel = 0;
                self.ret_expand = false;
                self.ret_filter = ShellFilter::All;
            }
        }
        if let Update::ReportDone { ok, path, msg } = &u {
            self.set_notice(if *ok {
                format!("✓ report → {path}")
            } else {
                format!("✖ report: {msg}")
            });
            return; // UI-only feedback; never session state
        }
        if let Update::Peek(ref p) = u {
            // INSPECT peek reply: store only when it answers the CURRENTLY
            // selected seg — a stale reply (the user walked on before it
            // arrived) must never open an overlay for the wrong segment.
            if self.inspect && self.selected_seg_id() == Some(p.seg) {
                self.peek = Some(p.clone());
                self.dirty = true;
            }
            return; // UI-modal state; never session state
        }
        if let Update::Map(ref m) = u {
            // A map rebuild (rev change) may retire the walked seg ids. The
            // simplest honest rule (documented over remapping heuristics):
            // exit INSPECT with a log line; the user re-enters on the fresh
            // map. Same-rev map_add appends are safe and don't exit.
            if self.inspect && m.rev != self.st.map_rev {
                self.reset_inspect(Some(m.rev));
            }
        }
        if let Update::Snapshot(ref s) = u {
            // UI-side seek coalescing: one in-flight seek, newest wins. A
            // snapshot only clears the latch when it answers the in-flight
            // turn — a stale reply must not clear a newer seek.
            if self.seek_inflight.is_some() && self.seek_inflight != Some(s.turn) {
                return;
            }
            self.seek_inflight = None;
            if let Some(p) = self.seek_pending.take() {
                self.send(Control::Seek { turn: p });
                self.seek_inflight = Some(p);
            }
            if self.cursor.is_none() {
                return; // stale snapshot after snapping back to LIVE
            }
        }
        self.st.apply(u);
        self.dirty = true;
    }

    fn drain_updates(&mut self) {
        loop {
            match self.rx.try_recv() {
                Ok(u) => self.apply_update(u),
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }
    }

    // --- replay / cursor -------------------------------------------------

    fn cursor_pos(&self) -> u64 {
        self.cursor
            .unwrap_or_else(|| self.st.last_turn().unwrap_or(0))
    }

    fn set_cursor(&mut self, want: i64) {
        let Some(last) = self.st.last_turn() else {
            return;
        };
        let t = want.clamp(0, last as i64) as u64;
        if t >= last {
            self.go_live();
            return;
        }
        if self.cursor != Some(t) {
            self.cursor = Some(t);
            self.request_seek(t);
        }
        self.dirty = true;
    }

    fn request_seek(&mut self, t: u64) {
        if self.seek_inflight.is_none() {
            self.send(Control::Seek { turn: t });
            self.seek_inflight = Some(t);
        } else {
            self.seek_pending = Some(t); // newest wins
        }
    }

    /// Every attach path (fleet pick, agent drill-in, Backspace drill-out)
    /// must shed the replay posture — a stale cursor/seek latch on a fresh
    /// session renders live data under a phantom REPLAY banner. INSPECT is
    /// per-map state, so it sheds too.
    fn clear_replay_for_attach(&mut self) {
        self.cursor = None;
        self.st.replay = None;
        self.seek_inflight = None;
        self.seek_pending = None;
        if self.inspect {
            self.reset_inspect(None);
        }
    }

    // --- INSPECT (OVERVIEW segment walk) ----------------------------------

    /// The walked segment's id: eff_segs[inspect_idx], index clamped — the
    /// seg list can change length under the cursor (replay snapshots).
    fn selected_seg_id(&self) -> Option<u64> {
        if !self.inspect {
            return None;
        }
        let segs = viz::eff_segs(&self.st);
        segs.get(self.inspect_idx.min(segs.len().saturating_sub(1)))
            .map(|s| s.id)
    }

    /// Request the walked segment's content. Live: ask the engine. Demo: no
    /// engine, so synthesize the reply from the segment's own metadata so the
    /// INSPECT peek overlay is fully exercised by the testbench.
    fn request_peek(&mut self) {
        let Some(id) = self.selected_seg_id() else {
            return;
        };
        if !self.demo {
            self.send(Control::Peek { seg: id });
            return;
        }
        let segs = viz::eff_segs(&self.st);
        if let Some(sg) = segs.iter().find(|s| s.id == id).cloned() {
            let kind = match sg.cat.label() {
                "overhead" => "overhead",
                "reasoning" => "reasoning",
                "file" | "bash" | "tool" | "attach" => "user",
                other => other,
            };
            let excerpt = match sg.cat.label() {
                "overhead" => "Server-side context the transcript cannot itemize: \
                    the system prompt, tool schemas, skill listings and MCP \
                    instructions. (demo)".to_string(),
                "reasoning" => "Encrypted signature-only thinking, generated this \
                    turn; resident and re-billed as cached input. (demo)".to_string(),
                "file" => sg
                    .file
                    .and_then(|f| self.st.files.get(&f))
                    .map(|f| format!("// {} — the in-context copy of this file. (demo)", f.path))
                    .unwrap_or_else(|| "file content (demo)".to_string()),
                c => format!("{c} record content — the actual text these tokens \
                    represent, read back from the transcript. (demo)"),
            };
            self.apply_update(Update::Peek(ipc::PeekMsg {
                seg: id,
                found: true,
                cat: sg.cat,
                kind: Some(kind.to_string()),
                uuid: Some(format!("demo-{id}")),
                born: sg.born,
                est: sg.tok,
                tok: sg.tok,
                file: sg.file,
                excerpt,
                truncated: false,
            }));
        }
    }

    /// Exit INSPECT (attach / meta switch / map rebuild). `new_rev` names
    /// the rebuild in the log; None = a plain reset (attach paths).
    fn reset_inspect(&mut self, new_rev: Option<u64>) {
        let was = self.inspect;
        self.inspect = false;
        self.inspect_idx = 0;
        self.peek = None;
        if was {
            if let Some(rev) = new_rev {
                self.st
                    .push_log(format!("map rebuilt (rev {rev}) — INSPECT exited"));
            }
        }
    }

    fn go_live(&mut self) {
        if self.cursor.is_some() || self.st.replay.is_some() {
            self.cursor = None;
            self.st.replay = None;
            // SPEC liveness rule: the engine never answers a cancelled seek,
            // so the in-flight latch must be cleared here or replay wedges.
            self.seek_inflight = None;
            self.seek_pending = None;
            self.send(Control::Live);
        }
        // "End = snap to NOW" on both time axes, one law: going live also
        // re-pins the SHELL console to its tail.
        self.shell_follow = true;
        self.dirty = true;
    }

    // --- view params ------------------------------------------------------

    /// FILES selection order for the ACTIVE perspective: the NOW recency
    /// order (hot then cold) when `files_view == Now`, else the sorted
    /// HISTORY order. The MAP cross-link follows whichever is active.
    fn current_file_order(&self) -> Vec<u64> {
        if self.files_view == FilesView::Now {
            let (hot, cold) = viz::file_now_order(&self.st, self.now_epoch());
            hot.into_iter().chain(cold).collect()
        } else {
            viz::file_order(&self.st, self.file_sort)
        }
    }

    fn ui(&self, tier: Tier) -> Ui {
        let last = self.st.last_turn().unwrap_or(0);
        let vt = self.cursor.unwrap_or(last);
        // Replay renders never fall back to live numbers (SPEC b): the
        // snapshot carries the sought turn's own waterline/cc; the turn ring
        // covers the gap while a seek is still in flight.
        let (wl, cc) = if let Some(sn) =
            self.st.replay.as_ref().filter(|_| self.cursor.is_some())
        {
            (sn.waterline, sn.cc)
        } else {
            self.st
                .turns
                .iter()
                .find(|t| t.turn == vt)
                .map(|t| (t.waterline, t.cc))
                .unwrap_or((self.st.waterline, self.st.last_cc))
        };
        let order = self.current_file_order();
        let sel_file = order
            .get(self.file_sel.min(order.len().saturating_sub(1)))
            .copied();
        Ui {
            now_epoch: self.now_epoch(),
            blink: self.blink,
            cursor: self.cursor,
            sel_file,
            sel_seg: self.selected_seg_id(),
            spotlight_static: self.peek.is_some(),
            // OVERVIEW packs the MAP to content (gauge shows fullness); other
            // MAP uses (none today) would keep budget-relative headroom dots
            map_pack: false, // MAP is a FIXED-scale box (context space), not stretched
            map_mode: self.map_mode,
            rung_override: self.rung_override,
            tier,
            wl,
            cc,
        }
    }

    // --- keys (dispatch: overlay > tab-contextual > global) ---------------

    fn on_key(&mut self, k: KeyEvent) {
        let code = k.code;
        let shift = k.modifiers.contains(KeyModifiers::SHIFT);
        self.dirty = true;

        if self.show_help {
            match code {
                KeyCode::Char('q') => self.quit(),
                // ?/→/j page forward, ←/k back — the glossary lives here
                KeyCode::Char('?') | KeyCode::Right | KeyCode::Char('j') => {
                    self.help_page = (self.help_page + 1) % viz::HELP_PAGES;
                }
                KeyCode::Left | KeyCode::Char('k') => {
                    self.help_page =
                        (self.help_page + viz::HELP_PAGES - 1) % viz::HELP_PAGES;
                }
                _ => self.show_help = false,
            }
            return;
        }
        if let Some(i) = self.postmortem {
            match code {
                KeyCode::Esc | KeyCode::Char('c') | KeyCode::Enter => self.postmortem = None,
                KeyCode::Left => self.postmortem = Some(i.saturating_sub(1)),
                KeyCode::Right => {
                    self.postmortem =
                        Some((i + 1).min(self.st.compactions.len().saturating_sub(1)))
                }
                KeyCode::Char('q') => self.quit(),
                _ => {}
            }
            return;
        }
        if self.peek.is_some() {
            // peek overlay (dispatch: help > post-mortem > peek > fleet):
            // Esc/Enter/i close IT — Esc exits INSPECT only on the NEXT press
            match code {
                KeyCode::Esc | KeyCode::Enter | KeyCode::Char('i') => self.peek = None,
                KeyCode::Char('q') => self.quit(),
                _ => {}
            }
            return;
        }
        if self.show_fleet {
            // the SESSIONS overlay is a live search box: printable keys type
            // into the query (filtering the roster); ↑/↓ move; ⏎ attaches the
            // selected row — or, when the query is a .jsonl path, that path
            // directly; Esc clears the query, then closes.
            match code {
                KeyCode::Esc => {
                    if self.fleet_query.is_empty() {
                        self.show_fleet = false;
                    } else {
                        self.fleet_query.clear();
                        self.fleet_sel = 0;
                    }
                }
                KeyCode::Down => {
                    let n = viz::fleet_rows_filtered(&self.st, &self.fleet_query).len();
                    self.fleet_sel = (self.fleet_sel + 1).min(n.saturating_sub(1));
                }
                KeyCode::Up => self.fleet_sel = self.fleet_sel.saturating_sub(1),
                KeyCode::Backspace => {
                    self.fleet_query.pop();
                    self.fleet_sel = 0;
                }
                KeyCode::Enter => {
                    let q = self.fleet_query.trim().to_string();
                    let looks_path = q.contains('/') || q.ends_with(".jsonl");
                    let rows = viz::fleet_rows_filtered(&self.st, &self.fleet_query);
                    let target = if looks_path && !q.is_empty() {
                        Some(q) // explicit path (engine expands ~ / resolves)
                    } else if let Some(s) = rows.get(self.fleet_sel) {
                        Some(s.id.clone()) // a matched roster row
                    } else if !q.is_empty() {
                        Some(q) // no match → hand the raw query (id) to the engine
                    } else {
                        None
                    };
                    if let Some(sess) = target {
                        self.clear_replay_for_attach();
                        self.attach_stack.clear(); // picks are roots
                        self.send(Control::Attach { session: sess });
                        self.show_fleet = false;
                        self.fleet_query.clear();
                        self.fleet_sel = 0;
                    }
                }
                KeyCode::Char(c) => {
                    self.fleet_query.push(c);
                    self.fleet_sel = 0;
                }
                _ => {}
            }
            return;
        }

        if self.on_key_contextual(code) {
            return;
        }
        self.on_key_global(code, shift);
    }

    fn on_key_contextual(&mut self, code: KeyCode) -> bool {
        match self.tab {
            0 => {
                // OVERVIEW — INSPECT segment walk (SPEC e). While active,
                // ←/→ and j/k are CAPTURED: they walk the MAP's segments in
                // prompt order and the turn cursor does not move.
                match code {
                    KeyCode::Char('i') => {
                        self.inspect = !self.inspect;
                        self.inspect_idx = 0; // the overhead segment
                        self.peek = None;
                        self.st.push_log(if self.inspect {
                            "INSPECT on — ←/→ walk · enter peek · esc exit".into()
                        } else {
                            "INSPECT off".into()
                        });
                        true
                    }
                    _ if self.inspect => {
                        let n = viz::eff_segs(&self.st).len();
                        match code {
                            KeyCode::Right | KeyCode::Char('j') => {
                                self.inspect_idx =
                                    (self.inspect_idx + 1).min(n.saturating_sub(1));
                                true
                            }
                            KeyCode::Left | KeyCode::Char('k') => {
                                self.inspect_idx = self.inspect_idx.saturating_sub(1);
                                true
                            }
                            KeyCode::Enter => {
                                // file-backed chunk → open the real file in
                                // $EDITOR; anything else → peek overlay
                                // (explicit-request-only, SPEC c). `p` peeks
                                // unconditionally — the in-context copy can
                                // differ from the file on disk.
                                let seg_file = viz::eff_segs(&self.st)
                                    .get(self.inspect_idx)
                                    .and_then(|sg| sg.file);
                                match seg_file
                                    .and_then(|fid| self.st.files.get(&fid))
                                    .map(|f| f.path.clone())
                                {
                                    Some(path) => {
                                        self.st.push_log(format!("$EDITOR ← {path}"));
                                        self.pending_editor = Some(path);
                                    }
                                    None => self.request_peek(),
                                }
                                true
                            }
                            KeyCode::Char('p') => {
                                self.request_peek();
                                true
                            }
                            KeyCode::Esc => {
                                self.inspect = false;
                                self.inspect_idx = 0;
                                self.peek = None;
                                self.st.push_log("INSPECT off".into());
                                true
                            }
                            _ => false,
                        }
                    }
                    _ => false,
                }
            }
            1 => {
                // FILES — `j/k g/G Enter o` run over the ACTIVE view's order
                let n = self.current_file_order().len();
                match code {
                    KeyCode::Char('j') | KeyCode::Down => {
                        self.file_sel = (self.file_sel + 1).min(n.saturating_sub(1));
                        true
                    }
                    KeyCode::Char('k') | KeyCode::Up => {
                        self.file_sel = self.file_sel.saturating_sub(1);
                        true
                    }
                    KeyCode::Char('g') => {
                        self.file_sel = 0;
                        true
                    }
                    KeyCode::Char('G') => {
                        self.file_sel = n.saturating_sub(1);
                        true
                    }
                    KeyCode::Enter => {
                        self.file_detail = !self.file_detail;
                        true
                    }
                    KeyCode::Char('v') => {
                        self.files_view = match self.files_view {
                            FilesView::History => FilesView::Now,
                            FilesView::Now => FilesView::History,
                        };
                        self.file_sel = 0;
                        true
                    }
                    KeyCode::Char('s') => {
                        // NOW: recency IS the order — `s` is a strict no-op
                        // (no state change, no log)
                        if self.files_view == FilesView::Now {
                            return true;
                        }
                        self.file_sort = self.file_sort.next();
                        self.file_sel = 0;
                        self.st
                            .push_log(format!("files sorted by {}", self.file_sort.label()));
                        true
                    }
                    KeyCode::Char('o') => {
                        let order = self.current_file_order();
                        if let Some(&fid) =
                            order.get(self.file_sel.min(order.len().saturating_sub(1)))
                        {
                            let path = viz::eff_files(&self.st)
                                .into_iter()
                                .find(|f| f.id == fid)
                                .map(|f| f.path.clone());
                            if let Some(p) = path {
                                self.pending_editor = Some(p);
                            }
                        }
                        true
                    }
                    _ => false,
                }
            }
            3 => {
                // AGENTS — unified-ledger selection + sort + filter + drill
                let rows = viz::agent_view_rows(
                    viz::eff_agents(&self.st),
                    self.agent_sort,
                    self.agent_filter,
                    &self.wf_open,
                    self.st.last_turn().unwrap_or(0),
                );
                let n = rows.len();
                match code {
                    KeyCode::Char('j') | KeyCode::Down => {
                        self.agent_sel = (self.agent_sel + 1).min(n.saturating_sub(1));
                        true
                    }
                    KeyCode::Char('k') | KeyCode::Up => {
                        self.agent_sel = self.agent_sel.saturating_sub(1);
                        true
                    }
                    KeyCode::Char('g') => {
                        self.agent_sel = 0;
                        true
                    }
                    KeyCode::Char('G') => {
                        self.agent_sel = n.saturating_sub(1);
                        true
                    }
                    KeyCode::Char('s') => {
                        self.agent_sort = self.agent_sort.next();
                        self.agent_sel = 0;
                        self.st
                            .push_log(format!("agents sorted by {}", self.agent_sort.label()));
                        true
                    }
                    KeyCode::Char('a') => {
                        self.agent_filter = self.agent_filter.next();
                        self.agent_sel = 0;
                        self.st
                            .push_log(format!("agents filter: {}", self.agent_filter.label()));
                        true
                    }
                    KeyCode::Enter => {
                        match rows.get(self.agent_sel.min(n.saturating_sub(1))) {
                            Some(viz::ARow::Wf { wf, expanded, .. }) => {
                                // manual override wins over the automatic rule
                                self.wf_open.insert(wf.clone(), !expanded);
                            }
                            Some(viz::ARow::Ag { idx, .. }) => {
                                // DRILL INTO the agent: the full instrument
                                // re-targets to the agent's own context
                                // window (SPEC e AGENTS); Backspace returns.
                                let a = &viz::eff_agents(&self.st)[*idx];
                                let (apath, aid) = (a.path.clone(), a.id.clone());
                                match (apath, self.st.meta.as_ref()) {
                                    (Some(p), Some(m)) => {
                                        let parent = m.path.clone();
                                        self.clear_replay_for_attach();
                                        self.attach_stack.push(parent);
                                        self.st.push_log(format!(
                                            "drilling into agent {} — bksp returns",
                                            &aid[..aid.len().min(8)]
                                        ));
                                        self.send(Control::Attach { session: p });
                                    }
                                    _ => self.st.push_log(
                                        "agent has no transcript path yet".into(),
                                    ),
                                }
                            }
                            None => {}
                        }
                        true
                    }
                    _ => false,
                }
            }
            5 => {
                // SHELL — follow/browse over the ACTIVE perspective's visible
                // set (filter- and replay-aware); follow is SHARED between
                // CONSOLE and RETRIEVAL, selection anchors by seq identity
                // per view.
                let retr = self.shell_view == ShellView::Retrieval;
                let seqs: Vec<u64> = if retr {
                    viz::ret_visible(&self.st, self.ret_filter, self.cursor)
                        .iter()
                        .map(|r| r.seq)
                        .collect()
                } else {
                    viz::shell_visible(&self.st, self.shell_filter, self.cursor)
                        .iter()
                        .map(|c| c.seq)
                        .collect()
                };
                let sel = if retr { self.ret_sel } else { self.shell_sel };
                let set_sel = |app: &mut Self, s: u64| {
                    if retr {
                        app.ret_sel = s;
                    } else {
                        app.shell_sel = s;
                    }
                };
                match code {
                    KeyCode::Char('v') => {
                        // CONSOLE ↔ RETRIEVAL (the FILES-view idiom); the
                        // shared follow posture carries across untouched
                        self.shell_view = if retr {
                            ShellView::Console
                        } else {
                            ShellView::Retrieval
                        };
                        self.st.push_log(format!(
                            "shell view: {}",
                            if retr { "console" } else { "retrieval" }
                        ));
                        true
                    }
                    KeyCode::Char('j') | KeyCode::Down => {
                        // break follow; first press anchors on the newest
                        // visible entry, then moves newer (clamped)
                        if self.shell_follow {
                            if let Some(&newest) = seqs.last() {
                                self.shell_follow = false;
                                set_sel(self, newest);
                            }
                        } else if let Some(p) = seqs.iter().position(|&s| s == sel) {
                            let s = seqs[(p + 1).min(seqs.len() - 1)];
                            set_sel(self, s);
                        } else if let Some(&newest) = seqs.last() {
                            set_sel(self, newest);
                        }
                        true
                    }
                    KeyCode::Char('k') | KeyCode::Up => {
                        if self.shell_follow {
                            if let Some(&newest) = seqs.last() {
                                self.shell_follow = false;
                                set_sel(self, newest);
                            }
                        } else if let Some(p) = seqs.iter().position(|&s| s == sel) {
                            let s = seqs[p.saturating_sub(1)];
                            set_sel(self, s);
                        } else if let Some(&newest) = seqs.last() {
                            set_sel(self, newest);
                        }
                        true
                    }
                    KeyCode::Char('g') => {
                        // oldest retained entry (breaks follow)
                        if let Some(&oldest) = seqs.first() {
                            self.shell_follow = false;
                            set_sel(self, oldest);
                        }
                        true
                    }
                    KeyCode::Char('G') => {
                        // newest + restore follow
                        self.shell_follow = true;
                        true
                    }
                    KeyCode::Enter => {
                        if retr {
                            self.ret_expand = !self.ret_expand;
                        } else {
                            self.shell_expand = !self.shell_expand;
                        }
                        true
                    }
                    KeyCode::Char('a') => {
                        let filter = if retr {
                            self.ret_filter = self.ret_filter.next();
                            self.ret_filter
                        } else {
                            self.shell_filter = self.shell_filter.next();
                            self.shell_filter
                        };
                        // re-clamp a browsing selection to the new visible set
                        if !self.shell_follow {
                            let seqs2: Vec<u64> = if retr {
                                viz::ret_visible(&self.st, filter, self.cursor)
                                    .iter()
                                    .map(|r| r.seq)
                                    .collect()
                            } else {
                                viz::shell_visible(&self.st, filter, self.cursor)
                                    .iter()
                                    .map(|c| c.seq)
                                    .collect()
                            };
                            if seqs2.is_empty() {
                                self.shell_follow = true;
                            } else if !seqs2.contains(&sel) {
                                let s = seqs2
                                    .iter()
                                    .rev()
                                    .find(|&&s| s <= sel)
                                    .copied()
                                    .unwrap_or(seqs2[0]);
                                set_sel(self, s);
                            }
                        }
                        self.st.push_log(format!(
                            "{} filter: {}",
                            if retr { "retrieval" } else { "shell" },
                            filter.label()
                        ));
                        true
                    }
                    KeyCode::End => {
                        // browsing AND live: restore follow, consumed; else
                        // fall through to global go_live (which also re-pins)
                        if !self.shell_follow && self.cursor.is_none() {
                            self.shell_follow = true;
                            true
                        } else {
                            false
                        }
                    }
                    _ => false,
                }
            }
            4 => {
                // EVENTS (ledger is newest-first)
                let n = self.st.events.len();
                match code {
                    KeyCode::Char('j') | KeyCode::Down => {
                        self.event_sel = (self.event_sel + 1).min(n.saturating_sub(1));
                        true
                    }
                    KeyCode::Char('k') | KeyCode::Up => {
                        self.event_sel = self.event_sel.saturating_sub(1);
                        true
                    }
                    KeyCode::Char('g') => {
                        self.event_sel = 0;
                        true
                    }
                    KeyCode::Char('G') => {
                        self.event_sel = n.saturating_sub(1);
                        true
                    }
                    KeyCode::Enter => {
                        if n == 0 {
                            return true;
                        }
                        let idx = n - 1 - self.event_sel.min(n - 1); // ring index
                        if let Some(e) = self.st.events.get(idx) {
                            if e.kind == "compaction" {
                                // drill into the matching compaction post-mortem
                                let turn = e.turn;
                                if let Some(ci) =
                                    self.st.compactions.iter().position(|c| c.turn == turn)
                                {
                                    self.postmortem = Some(ci);
                                    return true;
                                }
                            }
                            // any other event: jump the turn cursor to it
                            let t = e.turn;
                            self.set_cursor(t as i64);
                        }
                        true
                    }
                    _ => false,
                }
            }
            _ => false,
        }
    }

    fn on_key_global(&mut self, code: KeyCode, shift: bool) {
        match code {
            KeyCode::Char('q') => self.quit(),
            KeyCode::Char('?') => {
                self.show_help = true;
                self.help_page = 0;
            }
            KeyCode::Char(c @ '1'..='6') => {
                self.tab = (c as usize) - ('1' as usize);
            }
            KeyCode::Char('f') | KeyCode::Char('0') => {
                self.show_fleet = true;
                self.fleet_sel = 0;
                self.fleet_query.clear();
                self.send(Control::FleetRefresh); // rescan the roster on open
            }
            KeyCode::Char('p') => {
                self.paused = !self.paused;
                self.force_draw = true; // show the ⏸ state change immediately
                self.st.push_log(if self.paused {
                    "render paused (p resumes)".into()
                } else {
                    "render resumed".into()
                });
            }
            KeyCode::Left => {
                let step = if shift { 10 } else { 1 };
                self.set_cursor(self.cursor_pos() as i64 - step);
            }
            KeyCode::Right => {
                let step = if shift { 10 } else { 1 };
                self.set_cursor(self.cursor_pos() as i64 + step);
            }
            KeyCode::Home => self.set_cursor(0),
            KeyCode::Backspace => {
                // drill-out: re-attach the parent session
                if let Some(parent) = self.attach_stack.pop() {
                    self.clear_replay_for_attach();
                    self.send(Control::Attach { session: parent });
                }
            }
            KeyCode::End => self.go_live(),
            KeyCode::Esc => {
                // ack the visible alert, then snap to LIVE (ack is a amtr
                // extension: the spec names no dedicated ack key)
                self.st.ack_alert();
                self.go_live();
            }
            KeyCode::Char('m') => {
                self.map_mode = self.map_mode.next();
                self.st
                    .push_log(format!("map mode: {}", self.map_mode.label()));
            }
            KeyCode::Char('c') => {
                if !self.st.compactions.is_empty() {
                    self.postmortem = Some(self.st.compactions.len() - 1);
                }
            }
            KeyCode::Char('+') | KeyCode::Char('=') => self.nudge_rung(1),
            KeyCode::Char('-') => self.nudge_rung(-1),
            KeyCode::Char('R') => {
                // one-key ground-truth report of the attached session — the
                // engine already has it parsed, so it's instant
                self.send(Control::Report);
                self.set_notice("writing report…".into());
            }
            _ => {}
        }
    }

    /// Show a transient footer notice for ~6 s (report written, etc.).
    fn set_notice(&mut self, msg: String) {
        self.notice = Some((msg, self.now_epoch() + 6.0));
        self.dirty = true;
    }

    fn quit(&mut self) {
        self.send(Control::Quit);
        self.quitting = true;
    }

    /// Clamp the rung override at PRESS time so the effective rung stays on
    /// the ladder — presses past the edge must not bank (SPEC e).
    fn nudge_rung(&mut self, dir: i8) {
        let (w, h) = self.map_geom.get();
        let want = self.rung_override.saturating_add(dir);
        if w == 0 || h == 0 {
            self.rung_override = want.clamp(-5, 5);
            return;
        }
        let full_cell = self.map_mode == MapMode::Age;
        let cap = viz::map_capacity(w, h, full_cell);
        let auto = viz::auto_rung_idx(self.st.budget.max(1), cap) as i8;
        let hi = viz::RUNGS.len() as i8 - 1 - auto;
        self.rung_override = want.clamp(-auto, hi);
    }
}

// ---------------------------------------------------------------------------
// layout (pure; pane-dropping tiers, never squishing)
// ---------------------------------------------------------------------------

struct Panes {
    ribbon: Option<Rect>,
    tabs: Option<Rect>,
    scrubber: Option<Rect>,
    body: Option<Rect>,
    footer: Option<Rect>,
    tier: Tier,
    big: bool,
    too_small: bool,
}

fn layout(area: Rect) -> Panes {
    let (w, h) = (area.width, area.height);
    if w < 14 || h < 6 {
        return Panes {
            ribbon: None,
            tabs: None,
            scrubber: None,
            body: None,
            footer: None,
            tier: Tier::Compact,
            big: false,
            too_small: true,
        };
    }
    if w < 50 || h < 15 {
        return Panes {
            ribbon: None,
            tabs: None,
            scrubber: None,
            body: Some(area),
            footer: None,
            tier: Tier::Compact,
            big: true,
            too_small: false,
        };
    }
    let tier = if w >= 110 && h >= 30 {
        Tier::Full
    } else if w >= 80 && h >= 24 {
        Tier::Medium
    } else {
        Tier::Compact
    };
    if tier != Tier::Compact {
        let [ribbon, tabs, scrubber, body, footer] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .areas(area);
        Panes {
            ribbon: Some(ribbon),
            tabs: Some(tabs),
            scrubber: Some(scrubber),
            body: Some(body),
            footer: Some(footer),
            tier,
            big: false,
            too_small: false,
        }
    } else {
        // ≥50×15: one primary pane per tab, 1-line ribbon, no scrubber
        let [ribbon, tabs, body, footer] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .areas(area);
        Panes {
            ribbon: Some(ribbon),
            tabs: Some(tabs),
            scrubber: None,
            body: Some(body),
            footer: Some(footer),
            tier,
            big: false,
            too_small: false,
        }
    }
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------

fn render_all(f: &mut Frame<'_>, app: &App) {
    let area = f.area();
    f.render_widget(
        Block::new().style(Style::default().bg(BG).fg(viz::rgb(viz::C_FG))),
        area,
    );

    let panes = layout(area);
    if panes.too_small {
        f.render_widget(
            Paragraph::new("amtr\n≥14×6")
                .style(viz::fg(viz::C_DIM))
                .alignment(Alignment::Center),
            area,
        );
        return;
    }
    let ui = app.ui(panes.tier);
    if panes.big {
        viz::render_big(&app.st, &ui, f, panes.body.unwrap());
        return;
    }

    if let Some(r) = panes.ribbon {
        viz::render_ribbon(
            &app.st,
            app.engine_dead,
            app.paused,
            app.attach_stack.len(),
            f,
            r,
        );
    }
    if let Some(r) = panes.tabs {
        viz::render_tabs(&app.st, &ui, app.tab, f, r);
    }
    if let Some(r) = panes.scrubber {
        viz::render_scrubber(&app.st, &ui, f, r);
    }
    if let Some(body) = panes.body {
        render_tab_body(f, app, &ui, panes.tier, body);
    }
    if let Some(r) = panes.footer {
        let shell_newer = if app.tab == 5 && !app.shell_follow {
            match app.shell_view {
                ShellView::Console => viz::shell_visible(&app.st, app.shell_filter, app.cursor)
                    .iter()
                    .filter(|c| c.seq > app.shell_sel)
                    .count(),
                ShellView::Retrieval => viz::ret_visible(&app.st, app.ret_filter, app.cursor)
                    .iter()
                    .filter(|x| x.seq > app.ret_sel)
                    .count(),
            }
        } else {
            0
        };
        let notice = app
            .notice
            .as_ref()
            .filter(|(_, exp)| app.now_epoch() < *exp)
            .map(|(m, _)| m.as_str());
        viz::render_footer(
            &app.st,
            &ui,
            app.tab,
            app.inspect,
            app.files_view,
            app.shell_view,
            app.shell_follow,
            shell_newer,
            notice,
            f,
            r,
        );
    }

    // overlays: fleet < peek < post-mortem < help
    if app.show_fleet {
        viz::render_fleet(&app.st, app.fleet_sel, &app.fleet_query, f, area);
    }
    if let Some(p) = &app.peek {
        viz::render_peek_overlay(&app.st, p, f, area);
    }
    if let Some(i) = app.postmortem {
        viz::render_postmortem(&app.st, i, f, area);
    }
    if app.show_help {
        viz::render_help(app.help_page, f, area);
    }
}

fn render_tab_body(f: &mut Frame<'_>, app: &App, ui: &Ui, tier: Tier, body: Rect) {
    if body.width == 0 || body.height == 0 {
        return;
    }
    match app.tab {
        0 => {
            // OVERVIEW: MAP + legend + EKG; Compact = MAP only. Pane height =
            // min(60%, rows the budget actually needs at the chosen rung)
            // (SPEC e) — small budgets must not leave a mostly-blank pane.
            if tier == Tier::Compact || body.height < 10 {
                app.map_geom.set((body.width, body.height));
                viz::render_map(&app.st, ui, f, body);
                return;
            }
            // MAP (a FIXED-scale box that represents the whole context space:
            // bright = used, dim = free, no black gaps, not stretched) + legend
            // + the EKG trend fills the rest as a line graph. The old headline
            // gauge was dropped — R / % / rate / compaction all live in the top
            // ribbon already, and the MAP box IS the context-space headline.
            let ui0 = app.ui(tier);
            // legend wraps to the width: reserve exactly the rows it needs
            // (INSPECT replaces it with a single identity line)
            let legend_h = if app.inspect {
                1
            } else {
                viz::legend_rows(&app.st, body.width)
            };
            let reserved = 1 + legend_h + 3; // map header + legend + min EKG
            let max_map = (body.height.saturating_sub(reserved)).max(3);
            let map_h = viz::map_rows_needed(&app.st, &ui0, body.width, max_map)
                .max(3)
                .min(max_map);
            let [maphdr, map, legend, ekg] = Layout::vertical([
                Constraint::Length(1),
                Constraint::Length(map_h),
                Constraint::Length(legend_h),
                Constraint::Min(3),
            ])
            .areas(body);
            viz::render_map_header(&app.st, ui, f, maphdr, map);
            app.map_geom.set((map.width, map.height));
            viz::render_map(&app.st, ui, f, map);
            if app.inspect {
                // INSPECT: the legend row becomes the segment identity line
                viz::render_inspect_line(&app.st, ui, f, legend);
            } else {
                viz::render_legend(&app.st, f, legend);
            }
            viz::render_ekg(&app.st, ui, f, ekg);
        }
        1 => {
            // FILES: two perspectives — NOW (live recency list) or HISTORY
            // (roll + table; Compact = table only)
            if app.files_view == FilesView::Now {
                let (hot, cold) = viz::file_now_order(&app.st, app.now_epoch());
                let n = hot.len() + cold.len();
                let sel = app.file_sel.min(n.saturating_sub(1));
                viz::render_files_now(
                    &app.st,
                    ui,
                    &hot,
                    &cold,
                    sel,
                    0,
                    app.file_detail,
                    f,
                    body,
                );
                return;
            }
            let order = viz::file_order(&app.st, app.file_sort);
            let sel = app.file_sel.min(order.len().saturating_sub(1));
            if tier == Tier::Compact || body.height < 10 {
                viz::render_files_table(
                    &app.st,
                    ui,
                    &order,
                    sel,
                    app.file_sort,
                    app.file_detail,
                    f,
                    body,
                );
                return;
            }
            let roll_h = ((body.height as u32 * 2 / 5) as u16)
                .clamp(3, body.height - 5)
                .min(order.len().max(1) as u16);
            let [roll, table] =
                Layout::vertical([Constraint::Length(roll_h), Constraint::Min(4)]).areas(body);
            let scroll = sel.saturating_sub((roll_h as usize).saturating_sub(1));
            viz::render_files_roll(&app.st, ui, &order, sel, scroll, f, roll);
            viz::render_files_table(
                &app.st,
                ui,
                &order,
                sel,
                app.file_sort,
                app.file_detail,
                f,
                table,
            );
        }
        2 => viz::render_turns_tab(&app.st, ui, f, body),
        3 => viz::render_agents_tab(
            &app.st,
            ui,
            &viz::AgentsView {
                sel: app.agent_sel,
                sort: app.agent_sort,
                filter: app.agent_filter,
                wf_open: &app.wf_open,
            },
            f,
            body,
        ),
        4 => {
            let n = app.st.events.len();
            viz::render_events_tab(&app.st, app.event_sel.min(n.saturating_sub(1)), f, body);
        }
        5 => match app.shell_view {
            ShellView::Console => viz::render_shell_tab(
                &app.st,
                ui,
                app.shell_follow,
                app.shell_sel,
                app.shell_expand,
                app.shell_filter,
                f,
                body,
            ),
            ShellView::Retrieval => viz::render_retrieval_tab(
                &app.st,
                ui,
                app.shell_follow,
                app.ret_sel,
                app.ret_expand,
                app.ret_filter,
                f,
                body,
            ),
        },
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// run loop (split-process pattern, normative per SPEC §e)
// ---------------------------------------------------------------------------

/// max heat age (s) after which the heat animation is static:
/// 0.70·e^(−dt/45) ≤ 0.05  ⟺  dt ≥ 45·ln(14) ≈ 118.7 s — the shared law
/// (viz::HEAT_STATIC_S): MAP heat, FILES NOW decay, AGENTS running glow.
const HEAT_STATIC_S: f64 = viz::HEAT_STATIC_S;

fn pulses_active(app: &App) -> bool {
    app.st.write_pulse > 0
        || app.st.thrash_pulse > 0
        || app.st.compact_sweep > 0
        || app.st.turn_pulse > 0
        || app.st.touch_pulse > 0
        || app.st.cmd_pulse > 0
        || app.st.ret_pulse > 0
        || app.st.agent_pulse.values().any(|&p| p > 0)
}

fn heat_active(app: &App) -> bool {
    let now = app.now_epoch();
    // MAP heat mode: any segment still visibly decaying
    if app.tab == 0 && app.map_mode == MapMode::Heat {
        return viz::eff_segs(&app.st)
            .iter()
            .any(|s| now - s.ts < HEAT_STATIC_S);
    }
    // FILES NOW (live only): any hot file still decaying / AGE ticking
    if app.tab == 1 && app.files_view == FilesView::Now && app.cursor.is_none() {
        return app.st.files.values().any(|f| {
            f.last_epoch > 0.0 && now - f.last_epoch < HEAT_STATIC_S
        });
    }
    // AGENTS: any running agent's bar still glowing
    if app.tab == 3 {
        return viz::eff_agents(&app.st).iter().any(|a| {
            a.state == "running" && a.ts_last > 0.0 && now - a.ts_last < HEAT_STATIC_S
        });
    }
    false
}

fn run(terminal: &mut ratatui::DefaultTerminal, app: &mut App) -> std::io::Result<()> {
    let mut next_pulse = Instant::now();
    let mut next_heat = Instant::now();
    let mut next_blink = Instant::now() + Duration::from_millis(500);
    loop {
        // BLOCK on the update channel: deadline = min of every due clock
        // (idle ceiling 30 ms · pulse 80 ms · heat 500 ms · blink 500 ms).
        let now0 = Instant::now();
        let mut deadline = now0 + Duration::from_millis(30);
        if pulses_active(app) {
            deadline = deadline.min(next_pulse);
        }
        if heat_active(app) {
            deadline = deadline.min(next_heat);
        }
        deadline = deadline.min(next_blink);
        let timeout = deadline
            .saturating_duration_since(now0)
            .max(Duration::from_micros(500));
        match app.rx.recv_timeout(timeout) {
            Ok(u) => app.apply_update(u),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                if !app.engine_dead {
                    app.engine_dead = true;
                    app.st
                        .push_log("engine dead — UI stays navigable, data frozen".into());
                    app.dirty = true;
                }
                std::thread::sleep(Duration::from_millis(10)); // never busy-spin
            }
        }
        app.drain_updates(); // coalesce everything queued before drawing

        // wall-clock catch-up with `+=` rescheduling (never compounds error)
        let now = Instant::now();
        if pulses_active(app) {
            while now >= next_pulse {
                let st = &mut app.st;
                if st.write_pulse > 0 {
                    st.write_pulse -= 1;
                }
                if st.thrash_pulse > 0 {
                    st.thrash_pulse -= 1;
                }
                if st.compact_sweep > 0 {
                    st.compact_sweep -= 1;
                }
                if st.turn_pulse > 0 {
                    st.turn_pulse -= 1;
                }
                if st.touch_pulse > 0 {
                    st.touch_pulse -= 1;
                }
                if st.cmd_pulse > 0 {
                    st.cmd_pulse -= 1;
                }
                if st.ret_pulse > 0 {
                    st.ret_pulse -= 1;
                }
                st.agent_pulse.retain(|_, p| {
                    *p -= 1;
                    *p > 0
                });
                next_pulse += Duration::from_millis(80);
                app.dirty = true;
            }
        } else {
            next_pulse = now; // no backlog while idle
        }
        if heat_active(app) {
            while now >= next_heat {
                next_heat += Duration::from_millis(500);
                app.dirty = true;
            }
        } else {
            next_heat = now;
        }
        while now >= next_blink {
            app.blink = !app.blink;
            next_blink += Duration::from_millis(500);
            app.dirty = true;
        }

        // input: zero-timeout drain, modal-first dispatch, Press only
        while event::poll(Duration::from_millis(0))? {
            match event::read()? {
                Event::Key(k) if k.kind == KeyEventKind::Press => app.on_key(k),
                Event::Resize(_, _) => app.dirty = true,
                _ => {}
            }
        }

        // `o`: suspend the TUI, run $EDITOR, resume (read-only otherwise)
        if let Some(path) = app.pending_editor.take() {
            ratatui::restore();
            let editor = std::env::var("EDITOR").unwrap_or_else(|_| "vi".to_string());
            let mut parts = editor.split_whitespace();
            if let Some(prog) = parts.next() {
                let args: Vec<&str> = parts.collect();
                let _ = std::process::Command::new(prog)
                    .args(args)
                    .arg(&path)
                    .status();
            }
            *terminal = ratatui::init();
            app.dirty = true;
        }

        if app.dirty && (!app.paused || app.force_draw) {
            terminal.draw(|f| render_all(f, app))?;
            app.dirty = false;
            app.force_draw = false;
        }
        if app.quitting {
            break;
        }
    }
    Ok(())
}

fn main() -> std::io::Result<()> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let cli = match parse_args(&argv) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("amtr: {e}\n\n{USAGE}");
            std::process::exit(2);
        }
    };
    if cli.help {
        println!("{USAGE}");
        return Ok(());
    }

    if cli.demo {
        // deterministic scene source for the visual/animation testbench:
        // demo state, live clocks (no now_override), no engine. Leak the
        // far channel ends so the loop blocks on timeouts (animations tick)
        // instead of seeing a disconnected engine.
        let (tx, keep_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_rx);
        std::mem::forget(keep_tx);
        let mut app = App::new(tx, rx);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        app.demo = true;
        demo::populate(&mut app, now);
        let mut terminal = ratatui::init();
        let res = run(&mut terminal, &mut app);
        ratatui::restore();
        return res;
    }

    let cfg = ipc::SpawnCfg {
        python: cli.python,
        engine: cli.engine,
        passthrough: cli.passthrough,
    };
    let (tx, rx, handle) = match ipc::spawn_engine(&cfg) {
        Ok(t) => t,
        Err(e) => {
            eprintln!(
                "amtr: failed to spawn engine `{} {}`: {e}",
                cfg.python,
                cfg.engine.display()
            );
            std::process::exit(1);
        }
    };

    let mut app = App::new(tx, rx);
    let mut terminal = ratatui::init(); // installs a restoring panic hook
    let res = run(&mut terminal, &mut app);
    ratatui::restore(); // restore-first shutdown

    app.send(Control::Quit); // defensive second quit
    let _ = handle.shutdown();
    res
}

// ---------------------------------------------------------------------------
// tests (day one): demo fixture, semantic assertions, size sweep,
// fixed-scale invariance, scrubber markers, shots harness
// ---------------------------------------------------------------------------

#[cfg(test)]
mod screenshots {
    use super::*;
    use ipc::{
        AgentRec, CmdRec, Compaction, DroppedFile, EventRec, Faccess, FileRec, Health, MapMsg,
        Meta, Op, RetKind, RetRec, Seg, Sess, Severity, Snapshot, Tasks, ToolCounts, Turn,
    };
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use std::collections::HashMap;

    const NOW: f64 = 1_800_000_000.0;

    fn cat(s: &str) -> ipc::Cat {
        serde_json::from_value(serde_json::Value::String(s.to_string())).unwrap()
    }

    fn seg(id: u64, c: &str, tok: u64, file: Option<u64>, born: u64, age_s: f64) -> Seg {
        Seg {
            id,
            cat: cat(c),
            tok,
            file,
            born,
            ts: NOW - age_s,
        }
    }

    fn fa(turn: u64, file: u64, op: Op, tok: u64) -> Faccess {
        Faccess {
            turn,
            ts: format!("16:{:02}:00", turn % 60),
            file,
            op,
            tok,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn cmdrec(
        turn: u64,
        ts: &str,
        cmd: &str,
        desc: Option<&str>,
        out: &str,
        err: &str,
        ok: bool,
        interrupted: bool,
        bg: bool,
        tok_out: u64,
    ) -> CmdRec {
        CmdRec {
            turn,
            ts: ts.into(),
            epoch: NOW - 300.0 + turn as f64,
            cmd: cmd.into(),
            desc: desc.map(str::to_string),
            out: out.into(),
            err: err.into(),
            ok,
            interrupted,
            bg,
            tok_out,
            seq: 0, // stamped by the ring
        }
    }

    fn rkind(s: &str) -> RetKind {
        serde_json::from_value(serde_json::Value::String(s.to_string())).unwrap()
    }

    #[allow(clippy::too_many_arguments)]
    fn retrec(
        turn: u64,
        ts: &str,
        kind: &str,
        src: &str,
        q: &str,
        n: Option<u64>,
        bytes: Option<u64>,
        dur_ms: Option<u64>,
        tok: u64,
        ok: bool,
    ) -> RetRec {
        RetRec {
            turn,
            ts: ts.into(),
            epoch: NOW - 280.0 + turn as f64,
            kind: rkind(kind),
            src: src.into(),
            q: q.into(),
            n,
            bytes,
            dur_ms,
            tok,
            ok,
            seq: 0, // stamped by the ring
        }
    }

    /// Crafted peek reply for the INSPECT tests.
    fn pk(
        seg: u64,
        found: bool,
        c: &str,
        kind: Option<&str>,
        file: Option<u64>,
        excerpt: &str,
        truncated: bool,
    ) -> ipc::PeekMsg {
        ipc::PeekMsg {
            seg,
            found,
            cat: cat(c),
            kind: kind.map(str::to_string),
            uuid: Some(format!("uuid-{seg}")),
            born: 21,
            est: 2_600,
            tok: 2_500,
            file,
            excerpt: excerpt.into(),
            truncated,
        }
    }

    /// Fully-populated offline App: all cats on the MAP, 40 turns, 6 files,
    /// 1 compaction, 2 agents, 3 events, alert present (SPEC §e tests).
    fn demo_app() -> App {
        let (tx, keep_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        // keep the far ends alive so App sends never error mid-test
        std::mem::forget(keep_rx);
        std::mem::forget(keep_tx);
        demo_app_on_channels(tx, rx)
    }

    /// demo_app with caller-held channel ends (for asserting sent Controls).
    fn demo_app_on_channels(tx: Sender<Control>, rx: Receiver<Update>) -> App {
        let mut app = App::new(tx, rx);
        app.now_override = Some(NOW);
        app.blink = true;

        crate::demo::populate(&mut app, NOW);
        app
    }

    fn draw(app: &mut App, w: u16, h: u16) -> String {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| render_all(f, app)).unwrap();
        let buf = term.backend().buffer().clone();
        let mut out = String::new();
        for y in 0..h {
            for x in 0..w {
                out.push_str(buf[(x, y)].symbol());
            }
            out.push('\n');
        }
        out
    }

    fn buffer_of(app: &mut App, w: u16, h: u16) -> ratatui::buffer::Buffer {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| render_all(f, app)).unwrap();
        term.backend().buffer().clone()
    }

    // ---- (1) semantic string assertions: every tab + every overlay --------

    #[test]
    fn overview_semantics() {
        let mut app = demo_app();
        app.tab = 0;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("OVERVIEW"), "tab bar missing:\n{s}");
        assert!(s.contains("● LIVE"), "live indicator missing:\n{s}");
        assert!(s.contains("R 121k/200k"), "ribbon R missing:\n{s}");
        assert!(s.contains("▪="), "MAP rung label missing:\n{s}");
        assert!(s.contains("class"), "MAP mode label missing:\n{s}");
        assert!(s.contains("α0.97"), "alpha display missing:\n{s}");
        assert!(s.contains("▪system overhead 18k"), "legend overhead missing:\n{s}");
        assert!(s.contains("▪compaction summary"), "legend summary missing:\n{s}");
        assert!(s.contains("out/t"), "out sparkline lane missing:\n{s}");
        assert!(s.contains("ku/t"), "cost sparkline lane missing:\n{s}");
        assert!(s.contains("▼"), "compaction cliff marker missing:\n{s}");
        assert!(s.contains("API_ERROR"), "alert footer missing:\n{s}");
        assert!(s.contains("●busy"), "health missing:\n{s}");
        assert!(s.contains("tasks 3/7"), "tasks missing:\n{s}");
        // braille EKG actually drew lines
        let braille = s
            .chars()
            .filter(|c| ('\u{2800}'..='\u{28FF}').contains(c))
            .count();
        assert!(braille > 20, "EKG braille too sparse ({braille}):\n{s}");
    }

    #[test]
    fn files_semantics() {
        let mut app = demo_app();
        app.tab = 1;
        app.file_detail = true;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("amtr_engine.py"), "file path missing:\n{s}");
        assert!(s.contains("tok(est)"), "table header missing:\n{s}");
        assert!(s.contains("waste"), "waste column missing:\n{s}");
        assert!(s.contains("✝"), "evicted marker missing:\n{s}");
        assert!(s.contains("sort:size"), "sort label missing:\n{s}");
        // roll glyphs present (▀ read / ▄ write / █ both)
        assert!(
            s.contains('▀') || s.contains('▄') || s.contains('█'),
            "roll cells missing:\n{s}"
        );
        // detail line: alpha-calibrated vs raw
        assert!(s.contains("×α0.97"), "detail alpha line missing:\n{s}");
    }

    #[test]
    fn turns_semantics() {
        let mut app = demo_app();
        app.tab = 2;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("turn 39"), "cursor turn readout missing:\n{s}");
        assert!(s.contains("hit "), "hit readout missing:\n{s}");
        assert!(s.contains("cost "), "cost readout missing:\n{s}");
        assert!(s.contains("stop "), "stop readout missing:\n{s}");
        assert!(s.contains("cc "), "cc readout missing:\n{s}");
        assert!(s.contains("out"), "out lane missing:\n{s}");
        assert!(s.contains("dur"), "dur lane missing:\n{s}");
        assert!(s.contains("200k"), "fixed 0–B gutter missing:\n{s}");
    }

    #[test]
    fn agents_semantics() {
        // AGENTS design test 1 (header counts) + general ledger sanity
        let mut app = demo_app();
        app.tab = 3;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("fan-out"), "fan-out header missing:\n{s}");
        assert!(s.contains("2●"), "running count missing:\n{s}");
        assert!(s.contains("1✖"), "failed count missing:\n{s}");
        assert!(s.contains("sort:recent"), "sort mode missing:\n{s}");
        assert!(s.contains("× main"), "ratio-vs-main missing:\n{s}");
        assert!(s.contains("wf_refactor ×12"), "wf rollup row missing:\n{s}");
        assert!(s.contains("own-tok"), "economics header missing:\n{s}");
        assert!(s.contains("tools("), "tools column missing at Full:\n{s}");
        assert!(s.contains("· explore"), "indented wf child missing:\n{s}");
        assert!(s.contains("tasks 3/7"), "tasks footer missing:\n{s}");
        assert!(s.contains("wire the EKG lanes"), "active task missing:\n{s}");
        // running child with live t0 shows a ticking elapsed dur (2m14s)
        assert!(s.contains("2m14s"), "live elapsed dur missing:\n{s}");
        // detail line for the selected row (sel 0 = the wf rollup)
        assert!(s.contains("sel wf_refactor"), "detail line missing:\n{s}");
    }

    #[test]
    fn events_semantics() {
        let mut app = demo_app();
        app.tab = 4;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("api_error"), "api_error row missing:\n{s}");
        assert!(s.contains("thrash"), "thrash row missing:\n{s}");
        assert!(s.contains("compaction"), "compaction row missing:\n{s}");
        assert!(s.contains("✖"), "api_error glyph missing:\n{s}");
        assert!(s.contains("▼"), "compaction glyph missing:\n{s}");
        // newest first: api_error (applied last) above compaction (t20)
        let api = s.find("api_error").unwrap();
        let comp = s.find(" compaction").unwrap();
        assert!(api < comp, "ledger must be newest-first:\n{s}");
    }

    #[test]
    fn report_key_sends_and_confirms() {
        let (tx, ctl_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_tx);
        let mut app = demo_app_on_channels(tx, rx);
        app.now_override = Some(NOW);
        // R sends Control::Report and shows a "writing…" notice
        press(&mut app, KeyCode::Char('R'));
        let sent: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert_eq!(sent, vec![r#"{"type":"report"}"#.to_string()]);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("writing report"), "no writing notice:\n{s}");
        // the engine's reply names the path in the footer
        app.apply_update(Update::ReportDone {
            ok: true,
            path: "/Users/x/.claude/amtr-reports/brisk-otter-demo4d2f.md".into(),
            msg: "report written".into(),
        });
        let s = draw(&mut app, 110, 30);
        assert!(
            s.contains("✓ report → ") && s.contains("brisk-otter"),
            "report path not confirmed in footer:\n{s}"
        );
        // the notice expires (transient)
        app.now_override = Some(NOW + 7.0);
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("✓ report"), "notice should expire:\n{s}");
    }

    #[test]
    fn help_glossary_pages() {
        let mut app = demo_app();
        app.show_help = true;
        // page 2: the numbers
        press(&mut app, KeyCode::Char('?'));
        let s = draw(&mut app, 80, 24);
        for needed in ["hot/cold", "waste", "ku", "spark", "%res", "post-mortem"] {
            assert!(s.contains(needed), "numbers page missing {needed}:
{s}");
        }
        // page 3: modes & anatomy
        press(&mut app, KeyCode::Char('?'));
        let s = draw(&mut app, 80, 24);
        for needed in ["MAP modes", "cache", "▀wl", "▲thr", "◆mdl"] {
            assert!(s.contains(needed), "anatomy page missing {needed}:
{s}");
        }
        // wraps back to keys; ←/k go back; any other key closes
        press(&mut app, KeyCode::Char('?'));
        assert_eq!(app.help_page, 0);
        press(&mut app, KeyCode::Char('k'));
        assert_eq!(app.help_page, 2);
        press(&mut app, KeyCode::Esc);
        assert!(!app.show_help);
    }

    #[test]
    fn help_overlay_semantics() {
        let mut app = demo_app();
        app.show_help = true;
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("keys + legend"), "help title missing:\n{s}");
        assert!(s.contains("waterline"), "waterline explanation missing:\n{s}");
        assert!(s.contains("thrash"), "thrash explanation missing:\n{s}");
        assert!(s.contains("▲ thrash"), "glyph dictionary missing:\n{s}");
        assert!(s.contains("<60%"), "zone thresholds missing:\n{s}");
        assert!(s.contains("█over"), "palette swatches missing:\n{s}");
        assert!(s.contains("█reas"), "reasoning swatch missing:\n{s}");
        assert!(s.contains("█summ"), "palette swatches clipped:\n{s}");
    }

    #[test]
    fn fleet_overlay_semantics() {
        let mut app = demo_app();
        app.show_fleet = true;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("SESSIONS"), "sessions title missing:\n{s}");
        assert!(s.contains("search"), "search box missing:\n{s}");
        assert!(s.contains("brisk-otter"), "live session missing:\n{s}");
        assert!(s.contains("notes-site"), "offline session missing:\n{s}");
        assert!(s.contains("●"), "busy glyph missing:\n{s}");
        assert!(s.contains("attach"), "footer missing:\n{s}");
        assert!(s.contains("wire the EKG lanes"), "last prompt missing:\n{s}");
        // live rows sort before offline rows
        let live = s.find("brisk-otter").unwrap();
        let off = s.find("notes-site").unwrap();
        assert!(live < off, "live roster must sort first:\n{s}");
    }

    #[test]
    fn postmortem_overlay_semantics() {
        let mut app = demo_app();
        app.postmortem = Some(0);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("COMPACTION 1"), "pm title missing:\n{s}");
        assert!(s.contains("turn 20"), "pm turn missing:\n{s}");
        assert!(s.contains("auto"), "pm trigger missing:\n{s}");
        assert!(s.contains("pre "), "pre bar missing:\n{s}");
        assert!(s.contains("post"), "post bar missing:\n{s}");
        assert!(s.contains("dropped 110.0k"), "dropped total missing:\n{s}");
        assert!(s.contains("preserved 12 msgs"), "preserved missing:\n{s}");
        assert!(s.contains("✝"), "dropped files ✝ missing:\n{s}");
        assert!(s.contains("amtr_engine.py"), "top dropped file missing:\n{s}");
        assert!(s.contains("prev/next"), "pm nav footer missing:\n{s}");
    }

    #[test]
    fn big_number_and_too_small() {
        let mut app = demo_app();
        let s = draw(&mut app, 40, 12); // < 50×15 → big-number mode
        assert!(s.contains('%'), "big-number % missing:\n{s}");
        assert!(s.contains("compact"), "big-number ETA missing:\n{s}");
        let s = draw(&mut app, 12, 5); // < 14×6 → centered floor message
        assert!(s.contains("amtr"), "floor message missing:\n{s}");
    }

    #[test]
    fn replay_indicator_and_snapshot() {
        let mut app = demo_app();
        app.cursor = Some(15);
        // a seek reply: snapshot re-renders MAP/FILES/cats at that turn
        let snap_segs = vec![
            seg(0, "overhead", 18_000, None, 0, 600.0),
            seg(1, "user", 4_000, None, 3, 500.0),
            seg(2, "file", 60_000, Some(4), 8, 400.0),
        ];
        app.apply_update(Update::Snapshot(Snapshot {
            turn: 15,
            resident: 82_000,
            waterline: 70_000,
            cc: 9_000,
            map: MapMsg {
                rev: 1,
                alpha: 0.91,
                segs: snap_segs,
            },
            files: vec![FileRec {
                id: 4,
                path: "amtr_engine.py".into(),
                tok: 60_000,
                reads: 2,
                writes: 1,
                edits: 0,
                waste: 4_000,
                last_ts: "16:14:00".into(),
                last_epoch: 0.0,
                resident: true,
            }],
            cats: [("overhead", 18_000u64), ("user", 4_000), ("file", 60_000)]
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect(),
            agents: vec![],
            tasks: Tasks {
                total: 2,
                done: 1,
                in_progress: 1,
                active: None,
            },
        }));
        app.tab = 0;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("« REPLAY t=15/39"), "replay indicator missing:\n{s}");
        assert!(s.contains("α0.91"), "snapshot alpha not applied:\n{s}");
        assert!(s.contains("▪user input 4k"), "snapshot cats not applied:\n{s}");
        // live buffers keep filling: the ribbon still shows live R
        assert!(s.contains("R 121k/200k"), "ribbon must stay live:\n{s}");
    }

    #[test]
    fn seek_coalescing_newest_wins() {
        let (tx, ctl_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_tx);
        let mut app = App::new(tx, rx);
        // seed enough turns for a cursor range
        for i in 0..20u64 {
            app.st.apply(Update::Turn(Turn {
                turn: i,
                ts: String::new(),
                model: String::new(),
                in_tok: 1,
                cr: 0,
                cc: 0,
                cc_5m: 0,
                cc_1h: 0,
                out: 1,
                resident: 1000 * (i + 1),
                waterline: 500,
                dur_ms: None,
                stop: None,
                tools: 0,
                cost_u: 0.0,
                hit: 1.0,
            }));
        }
        app.set_cursor(10); // sends seek 10 (in-flight)
        app.set_cursor(8); // pending = 8
        app.set_cursor(5); // pending = 5 (newest wins, 8 discarded)
        let mut sent: Vec<String> = Vec::new();
        while let Ok(c) = ctl_rx.try_recv() {
            sent.push(serde_json::to_string(&c).unwrap());
        }
        assert_eq!(sent, vec![r#"{"type":"seek","turn":10}"#.to_string()]);
        // snapshot for 10 arrives → the coalesced newest (5) goes out
        app.apply_update(Update::Snapshot(Snapshot {
            turn: 10,
            resident: 0,
            waterline: 0,
            cc: 0,
            map: MapMsg {
                rev: 0,
                alpha: 1.0,
                segs: vec![],
            },
            files: vec![],
            cats: HashMap::new(),
            agents: vec![],
            tasks: Tasks::default(),
        }));
        let mut sent2: Vec<String> = Vec::new();
        while let Ok(c) = ctl_rx.try_recv() {
            sent2.push(serde_json::to_string(&c).unwrap());
        }
        assert_eq!(sent2, vec![r#"{"type":"seek","turn":5}"#.to_string()]);
        // End snaps back with no re-fetch: only a `live` goes out
        app.go_live();
        let live: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert_eq!(live, vec![r#"{"type":"live"}"#.to_string()]);
        assert!(app.st.replay.is_none());

        // SPEC liveness rule: End during an in-flight seek must not wedge —
        // the next scrub sends a real seek (the engine never answers a
        // cancelled one, so go_live must clear the in-flight latch).
        app.set_cursor(7); // in-flight seek 7
        app.go_live(); //    cancelled: no snapshot will ever arrive
        app.set_cursor(3); // must SEND, not park in seek_pending
        let after: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert_eq!(
            after,
            vec![
                r#"{"type":"seek","turn":7}"#.to_string(),
                r#"{"type":"live"}"#.to_string(),
                r#"{"type":"seek","turn":3}"#.to_string(),
            ]
        );
        // a STALE snapshot (turn 7) must not clear the newer latch for 3
        app.apply_update(Update::Snapshot(Snapshot {
            turn: 7,
            resident: 0,
            waterline: 0,
            cc: 0,
            map: MapMsg {
                rev: 0,
                alpha: 1.0,
                segs: vec![],
            },
            files: vec![],
            cats: HashMap::new(),
            agents: vec![],
            tasks: Tasks::default(),
        }));
        assert_eq!(app.seek_inflight, Some(3));
        assert!(app.st.replay.is_none(), "stale snapshot must not apply");
    }

    // ---- (2) no-panic size sweep ------------------------------------------

    #[test]
    fn no_panic_size_sweep() {
        // every tab × every overlay × sizes from 1×1 through 200×60
        let widths = [
            1u16, 2, 5, 10, 13, 14, 15, 30, 49, 50, 60, 79, 80, 109, 110, 140, 200,
        ];
        let heights = [1u16, 2, 5, 6, 9, 14, 15, 20, 23, 24, 29, 30, 45, 60];
        for tab in 0..6usize {
            for overlay in 0..4usize {
                let mut app = demo_app();
                app.tab = tab;
                match overlay {
                    1 => app.show_help = true,
                    2 => app.show_fleet = true,
                    3 => app.postmortem = Some(0),
                    _ => {}
                }
                for &w in &widths {
                    for &h in &heights {
                        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                        term.draw(|f| render_all(f, &mut app)).unwrap();
                    }
                }
            }
        }
        // all four MAP modes, replay cursor, active pulses
        for mode in [MapMode::Class, MapMode::Heat, MapMode::Age, MapMode::Cache] {
            let mut app = demo_app();
            app.map_mode = mode;
            app.cursor = Some(7);
            app.st.write_pulse = 6;
            app.st.write_span = (110_000, 121_000);
            app.st.thrash_pulse = 6;
            app.st.thrash_span = (60_000, 90_000);
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // FILES NOW view (live + replay + entry pulse + both selection ends)
        for (cursor, sel, pulse) in [(None, 0usize, 6u8), (Some(7), 5, 0)] {
            let mut app = demo_app();
            app.tab = 1;
            app.files_view = FilesView::Now;
            app.cursor = cursor;
            app.file_sel = sel;
            app.file_detail = true;
            app.st.touch_pulse = pulse;
            app.st.touch_file = 1;
            app.st.turn_pulse = 6;
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // AGENTS: 70-agent fixture, selection at both ends, every filter
        for (sel_end, filter) in [
            (false, AgentFilter::All),
            (true, AgentFilter::All),
            (true, AgentFilter::Run),
            (true, AgentFilter::Fail),
        ] {
            let mut app = demo_app();
            app.tab = 3;
            app.agent_filter = filter;
            for i in 0..55u64 {
                app.st.apply(Update::Agent(AgentRec {
                    id: format!("agent-x{i:02}"),
                    state: ["running", "done", "failed"][(i % 3) as usize].into(),
                    agent_type: Some("swarm".into()),
                    desc: Some(format!("swarm task {i}")),
                    wf: if i % 2 == 0 {
                        Some(format!("wf_swarm{}", i % 5))
                    } else {
                        None
                    },
                    path: None,
                    turn0: i % 40,
                    ts0: "16:00:00".into(),
                    turn1: if i % 3 == 1 { Some(i % 40 + 1) } else { None },
                    own_tok: 1_000 * (i + 1),
                    ret_tok: if i % 3 == 1 { Some(100 + i) } else { None },
                    tools: None,
                    dur_ms: if i % 3 == 1 { Some(10_000) } else { None },
                    t0: if i % 4 == 0 { 0.0 } else { NOW - 100.0 },
                    ts_last: if i % 4 == 0 { 0.0 } else { NOW - 20.0 },
                }));
            }
            let n = viz::agent_view_rows(
                viz::eff_agents(&app.st),
                app.agent_sort,
                app.agent_filter,
                &app.wf_open,
                app.st.last_turn().unwrap_or(0),
            )
            .len();
            app.agent_sel = if sel_end { n.saturating_sub(1) } else { 0 };
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // SHELL permutations: follow broken × expand on × filter err ×
        // replay × live arrival pulse
        for (cursor, follow, expand, filter, pulse) in [
            (None, false, true, viz::ShellFilter::Err, 0u8),
            (None, true, true, viz::ShellFilter::All, 6),
            (Some(7), true, false, viz::ShellFilter::Err, 6),
            (None, false, true, viz::ShellFilter::All, 0),
        ] {
            let mut app = demo_app();
            app.tab = 5;
            app.cursor = cursor;
            app.shell_follow = follow;
            if !follow {
                app.shell_sel = 2; // the failed cargo test entry
            }
            app.shell_expand = expand;
            app.shell_filter = filter;
            app.st.cmd_pulse = pulse;
            app.st.cmd_pulse_seq = 5;
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // RETRIEVAL perspective permutations: same posture matrix over the
        // rets ring (browse × expand × err filter × replay × arrival pulse)
        for (cursor, follow, expand, filter, pulse) in [
            (None, false, true, viz::ShellFilter::Err, 0u8),
            (None, true, true, viz::ShellFilter::All, 6),
            (Some(7), true, false, viz::ShellFilter::Err, 6),
            (None, false, false, viz::ShellFilter::All, 0),
        ] {
            let mut app = demo_app();
            app.tab = 5;
            app.shell_view = ShellView::Retrieval;
            app.cursor = cursor;
            app.shell_follow = follow;
            if !follow {
                app.ret_sel = 2; // the 17.9k mcp entry
            }
            app.ret_expand = expand;
            app.ret_filter = filter;
            app.st.ret_pulse = pulse;
            app.st.ret_pulse_seq = 4;
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // INSPECT + peek overlay permutations: walk ends, multi-line long
        // excerpt (truncated), file-seg header path, evicted answer
        for (idx, peek) in [
            (0usize, None),
            (
                24,
                Some(pk(
                    24,
                    true,
                    "assistant",
                    Some("assistant"),
                    None,
                    &format!("line one\nline two\n{}", "wide unbroken excerpt ".repeat(90)),
                    true,
                )),
            ),
            (
                9,
                Some(pk(9, true, "file", Some("user"), Some(4), "tool_result body", false)),
            ),
            (5, Some(pk(5, false, "file", None, Some(1), "", false))),
        ] {
            let mut app = demo_app();
            app.tab = 0;
            app.inspect = true;
            app.inspect_idx = idx;
            app.peek = peek;
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut app)).unwrap();
                }
            }
        }
        // empty state (no engine data at all) must also never panic
        let (tx, keep_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_rx);
        std::mem::forget(keep_tx);
        let mut empty = App::new(tx, rx);
        for tab in 0..6usize {
            empty.tab = tab;
            for &w in &widths {
                for &h in &heights {
                    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                    term.draw(|f| render_all(f, &mut empty)).unwrap();
                }
            }
        }
        // INSPECT over an empty map must render its no-segments line
        empty.tab = 0;
        empty.inspect = true;
        for &w in &widths {
            for &h in &heights {
                let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
                term.draw(|f| render_all(f, &mut empty)).unwrap();
            }
        }
    }

    // ---- (3) MAP fixed-scale invariance ------------------------------------

    #[test]
    fn overview_map_header_and_packed_map() {
        // OVERVIEW leads with a one-line MAP scale header (mode · rung · alpha)
        // above a FULL-WIDTH map; R / % / rate all live in the top ribbon now
        // (the CONTEXT gauge was retired). The map packs edge-to-edge.
        let mut app = demo_app();
        app.tab = 0;
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("CONTEXT"), "retired gauge must be gone:\n{s}");
        assert!(s.contains("61%"), "ribbon % missing:\n{s}");
        assert!(s.contains("▪="), "map rung label missing:\n{s}");
        assert!(s.contains("class"), "map mode header missing:\n{s}");
        assert!(s.contains("▪system overhead"), "legend missing:\n{s}");
        // no row between the header and the legend is a pure dot field — the
        // packed map leaves no headroom void
        let lines: Vec<&str> = s.lines().collect();
        let legend = lines
            .iter()
            .position(|l| l.contains("▪system overhead"))
            .expect("legend");
        for (i, l) in lines.iter().enumerate().take(legend).skip(4) {
            let body = l.trim_start_matches(|c: char| c != '·' && c != '█' && c != '▀');
            assert!(
                !(body.chars().count() > 20 && body.chars().all(|c| c == '·' || c == ' ')),
                "row {i} is a dot void — map should pack:\n{l}"
            );
        }
    }

    // ---- (4) scrubber marker placement -------------------------------------

    #[test]
    fn scrubber_markers() {
        let mut app = demo_app();
        app.tab = 0;
        let buf = buffer_of(&mut app, 110, 30);
        // scrubber row is y=2 at Full tier. M=40 turns, compaction at t20.
        let w = 110u16;
        let m = app.st.turn_count();
        assert_eq!(m, 40);
        let comp_col = viz::scrub_col(20, m, w);
        assert_eq!(comp_col, 55);
        assert_eq!(
            buf[(comp_col, 2)].symbol(),
            "◆",
            "compaction marker not at col {comp_col}"
        );
        // thrash event at t30
        let thrash_col = viz::scrub_col(30, m, w);
        assert_eq!(buf[(thrash_col, 2)].symbol(), "▲", "thrash marker missing");
        // playhead at the tail (live)
        let play_col = viz::scrub_col(39, m, w);
        assert_eq!(buf[(play_col, 2)].symbol(), "┃", "playhead missing");
        // replay moves the playhead
        app.cursor = Some(10);
        let buf = buffer_of(&mut app, 110, 30);
        let play_col = viz::scrub_col(10, m, w);
        assert_eq!(buf[(play_col, 2)].symbol(), "┃", "replay playhead missing");
    }

    // ---- wire-format spot checks (the contract with the concurrently-built
    //      engine: exact field names from SPEC §b/§c) -----------------------

    #[test]
    fn wire_parse_samples() {
        let turn = r#"{"type":"turn","turn":214,"ts":"16:48:51","model":"claude-fable-5",
            "in":2,"cr":512300,"cc":17863,"cc_5m":17863,"cc_1h":0,"out":203,
            "resident":530166,"waterline":512300,"dur_ms":8400,"stop":"tool_use",
            "tools":3,"cost_u":21.4,"hit":0.966}"#;
        match serde_json::from_str::<Update>(turn).unwrap() {
            Update::Turn(t) => {
                assert_eq!(t.in_tok, 2);
                assert_eq!(t.cr, 512_300);
                assert_eq!(t.stop.as_deref(), Some("tool_use"));
            }
            other => panic!("wrong variant: {other:?}"),
        }
        let map = r#"{"type":"map","rev":3,"alpha":0.93,
            "segs":[{"id":0,"cat":"overhead","tok":18000,"file":null,"born":0,"ts":1.5}]}"#;
        match serde_json::from_str::<Update>(map).unwrap() {
            Update::Map(m) => assert_eq!(m.segs[0].cat, ipc::Cat::Overhead),
            other => panic!("wrong variant: {other:?}"),
        }
        // unknown cat / unknown severity degrade, never fail
        let sg = r#"{"id":1,"cat":"holograms","tok":5,"born":0,"ts":0.0}"#;
        assert_eq!(serde_json::from_str::<Seg>(sg).unwrap().cat, ipc::Cat::Unknown);
        let ev =
            r#"{"type":"event","kind":"pressure","severity":"warn","ts":"a","turn":3,"msg":"x"}"#;
        match serde_json::from_str::<Update>(ev).unwrap() {
            Update::Event(e) => assert_eq!(e.severity, Severity::Warn),
            other => panic!("wrong variant: {other:?}"),
        }
        // controls serialize with the spec'd snake_case tags
        assert_eq!(
            serde_json::to_string(&Control::FleetRefresh).unwrap(),
            r#"{"type":"fleet_refresh"}"#
        );
        assert_eq!(
            serde_json::to_string(&Control::Attach {
                session: "x".into()
            })
            .unwrap(),
            r#"{"type":"attach","session":"x"}"#
        );
        assert_eq!(
            serde_json::to_string(&Control::Set {
                key: "poll_ms".into(),
                value: serde_json::json!(100)
            })
            .unwrap(),
            r#"{"type":"set","key":"poll_ms","value":100}"#
        );
        // an unknown update type fails parse (the reader turns it into a Log)
        assert!(serde_json::from_str::<Update>(r#"{"type":"quux","x":1}"#).is_err());

        // PEEK wire row (SPEC b): absent `found` defaults FALSE — a missing
        // answer must never render as content that exists; unknown cat
        // degrades, never fails
        let pkm = r#"{"type":"peek","seg":4}"#;
        match serde_json::from_str::<Update>(pkm).unwrap() {
            Update::Peek(p) => {
                assert_eq!(p.seg, 4);
                assert!(!p.found, "absent found must default FALSE");
                assert_eq!(p.cat, ipc::Cat::Unknown);
                assert!(p.excerpt.is_empty() && !p.truncated && p.kind.is_none());
            }
            other => panic!("wrong variant: {other:?}"),
        }
        let pkm = r#"{"type":"peek","seg":0,"found":true,"cat":"overhead",
            "kind":"overhead","uuid":null,"born":0,"est":0,"tok":18000,
            "file":null,"excerpt":"system prompt + tool schemas","truncated":false}"#;
        match serde_json::from_str::<Update>(pkm).unwrap() {
            Update::Peek(p) => {
                assert!(p.found);
                assert_eq!(p.cat, ipc::Cat::Overhead);
                assert_eq!(p.kind.as_deref(), Some("overhead"));
                assert_eq!(p.tok, 18_000);
            }
            other => panic!("wrong variant: {other:?}"),
        }
        assert_eq!(
            serde_json::to_string(&Control::Peek { seg: 3 }).unwrap(),
            r#"{"type":"peek","seg":3}"#
        );

        // SHELL wire row: desc:null parses, and a MISSING `ok` defaults TRUE
        // (version-drift law — absence must never render as failure)
        let cmd = r#"{"type":"cmd","turn":7,"ts":"16:48:20","epoch":1784289620.5,
            "cmd":"cargo build","desc":null,"out":"ok tail\n","err":"",
            "interrupted":false,"bg":true,"tok_out":214}"#;
        match serde_json::from_str::<Update>(cmd).unwrap() {
            Update::Cmd(c) => {
                assert!(c.ok, "absent ok must default TRUE");
                assert!(c.bg);
                assert_eq!(c.desc, None);
                assert_eq!(c.tok_out, 214);
                assert_eq!(c.seq, 0, "seq is UI-side, never from the wire");
            }
            other => panic!("wrong variant: {other:?}"),
        }
        // a backfill with cmds populates the ring in (epoch, turn) order,
        // seq-stamped, without arming the arrival pulse
        let bf = r#"{"type":"backfill","cmds":[
            {"turn":3,"ts":"b","epoch":2.0,"cmd":"second","ok":false},
            {"turn":1,"ts":"a","epoch":1.0,"cmd":"first"}]}"#;
        let mut stt = State::new();
        stt.apply(serde_json::from_str::<Update>(bf).unwrap());
        assert_eq!(stt.cmds.len(), 2);
        assert_eq!(stt.cmds[0].cmd, "first");
        assert_eq!(stt.cmds[1].cmd, "second");
        assert_eq!(stt.cmds[0].seq, 0);
        assert_eq!(stt.cmds[1].seq, 1);
        assert!(stt.cmds[0].ok, "absent ok in backfill defaults TRUE");
        assert!(!stt.cmds[1].ok);
        assert_eq!(stt.cmd_pulse, 0, "backfill must not arm the pulse");

        // RETRIEVAL wire row: every field #[serde(default)]; a MISSING `ok`
        // defaults TRUE and an unknown kind degrades, never fails
        let ret = r#"{"type":"ret","turn":9,"ts":"12:00:33","epoch":1784289633.0,
            "kind":"search","src":"web","q":"ratatui braille canvas","n":5,
            "bytes":null,"dur_ms":2400,"tok":13}"#;
        match serde_json::from_str::<Update>(ret).unwrap() {
            Update::Ret(r) => {
                assert_eq!(r.kind, RetKind::Search);
                assert_eq!(r.src, "web");
                assert_eq!(r.n, Some(5));
                assert_eq!(r.bytes, None);
                assert_eq!(r.dur_ms, Some(2_400));
                assert!(r.ok, "absent ok must default TRUE");
                assert_eq!(r.seq, 0, "seq is UI-side, never from the wire");
            }
            other => panic!("wrong variant: {other:?}"),
        }
        let rk = r#"{"turn":1,"kind":"holo-beam"}"#;
        assert_eq!(
            serde_json::from_str::<RetRec>(rk).unwrap().kind,
            RetKind::Unknown
        );
        // a backfill with rets populates the ring in (epoch, turn) order,
        // seq-stamped, without arming the arrival pulse
        let bf = r#"{"type":"backfill","rets":[
            {"turn":3,"ts":"b","epoch":2.0,"kind":"mcp","src":"claude_ai_Dropbox",
             "q":"second","ok":false},
            {"turn":1,"ts":"a","epoch":1.0,"kind":"fetch","src":"docs.rs","q":"first"}]}"#;
        let mut stt = State::new();
        stt.apply(serde_json::from_str::<Update>(bf).unwrap());
        assert_eq!(stt.rets.len(), 2);
        assert_eq!(stt.rets[0].q, "first");
        assert_eq!(stt.rets[1].q, "second");
        assert_eq!(stt.rets[0].seq, 0);
        assert_eq!(stt.rets[1].seq, 1);
        assert!(stt.rets[0].ok, "absent ok in backfill defaults TRUE");
        assert!(!stt.rets[1].ok);
        assert_eq!(stt.ret_pulse, 0, "backfill must not arm the pulse");
    }

    #[test]
    fn cli_parses_by_hand() {
        let cli = parse_args(&[
            "--session".into(),
            "abc".into(),
            "--python".into(),
            "python3.12".into(),
            "--engine-args".into(),
            "--selftest".into(),
        ])
        .unwrap();
        assert_eq!(cli.python, "python3.12");
        assert_eq!(
            cli.passthrough,
            vec!["--session".to_string(), "abc".into(), "--selftest".into()]
        );
        assert!(parse_args(&["--bogus".into()]).is_err());
        assert!(parse_args(&["--help".into()]).unwrap().help);
    }

    // ---- (5) shots: human-reviewable full-screen dumps ---------------------
    // run: cargo test shots -- --nocapture

    #[test]
    fn shots() {
        let dump = |app: &mut App, w: u16, h: u16, label: &str| {
            println!("\n===== {w}x{h} {label} =====");
            print!("{}", draw(app, w, h));
        };
        // OVERVIEW at ~12 sizes
        for (w, h) in [
            (110u16, 30u16),
            (140, 40),
            (200, 60),
            (100, 28),
            (80, 24),
            (70, 18),
            (60, 20),
            (50, 15),
            (45, 12),
            (30, 8),
            (14, 6),
            (12, 5),
        ] {
            let mut app = demo_app();
            dump(&mut app, w, h, "OVERVIEW");
        }
        // every other tab at the reference size
        for (tab, name) in [(1usize, "FILES"), (2, "TURNS"), (3, "AGENTS"), (4, "EVENTS")] {
            let mut app = demo_app();
            app.tab = tab;
            if tab == 1 {
                app.file_detail = true;
            }
            dump(&mut app, 110, 30, name);
        }
        // FILES NOW perspective at all three tiers + replay notice
        for (w, h) in [(110u16, 30u16), (80, 24), (50, 15)] {
            let mut app = demo_app();
            app.tab = 1;
            app.files_view = FilesView::Now;
            app.file_detail = w >= 110;
            dump(&mut app, w, h, "FILES NOW");
        }
        let mut app = demo_app();
        app.tab = 1;
        app.files_view = FilesView::Now;
        app.cursor = Some(15);
        dump(&mut app, 110, 30, "FILES NOW replay");
        // AGENTS ledger at Medium + Compact (Full already in the tab loop)
        for (w, h) in [(80u16, 24u16), (50, 15)] {
            let mut app = demo_app();
            app.tab = 3;
            dump(&mut app, w, h, "AGENTS");
        }
        // SHELL console at all three tiers (following)
        for (w, h) in [(110u16, 30u16), (80, 24), (50, 15)] {
            let mut app = demo_app();
            app.tab = 5;
            dump(&mut app, w, h, "SHELL follow");
        }
        // SHELL browse / expand / replay postures
        let mut app = demo_app();
        app.tab = 5;
        app.shell_follow = false;
        app.shell_sel = 2; // the failed cargo test
        dump(&mut app, 110, 30, "SHELL browse");
        let mut app = demo_app();
        app.tab = 5;
        app.shell_follow = false;
        app.shell_sel = 1;
        app.shell_expand = true;
        dump(&mut app, 110, 30, "SHELL expand");
        let mut app = demo_app();
        app.tab = 5;
        app.cursor = Some(20);
        dump(&mut app, 110, 30, "SHELL replay t=20");
        // RETRIEVAL perspective at all three tiers (following)
        for (w, h) in [(110u16, 30u16), (80, 24), (50, 15)] {
            let mut app = demo_app();
            app.tab = 5;
            app.shell_view = ShellView::Retrieval;
            dump(&mut app, w, h, "SHELL retrieval follow");
        }
        // RETRIEVAL browse+expand / replay postures
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        app.shell_follow = false;
        app.ret_sel = 1; // the docs.rs fetch (bytes + dur)
        app.ret_expand = true;
        dump(&mut app, 110, 30, "SHELL retrieval expand");
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        app.cursor = Some(20);
        dump(&mut app, 110, 30, "SHELL retrieval replay t=20");
        // MAP modes
        for mode in [MapMode::Heat, MapMode::Age, MapMode::Cache] {
            let mut app = demo_app();
            app.map_mode = mode;
            dump(&mut app, 110, 30, &format!("OVERVIEW mode={}", mode.label()));
        }
        // INSPECT walk + peek overlay
        let mut app = demo_app();
        app.inspect = true;
        app.inspect_idx = 9; // the 16k amtr_engine.py file seg
        dump(&mut app, 110, 30, "OVERVIEW INSPECT");
        app.peek = Some(ipc::PeekMsg {
            born: 26,
            est: 16_495,
            tok: 16_000,
            ..pk(
                9,
                true,
                "file",
                Some("user"),
                Some(4),
                "def peek_payload(self, sid):\n    \"\"\"INSPECT-mode content \
                 lookup (SPEC b/c `peek`): re-read the segment's record from \
                 disk.\"\"\"\n    base = {\"seg\": int(sid), \"found\": False}",
                false,
            )
        });
        dump(&mut app, 110, 30, "OVERVIEW INSPECT +PEEK");
        // overlays
        let mut app = demo_app();
        app.show_help = true;
        dump(&mut app, 80, 24, "+HELP");
        let mut app = demo_app();
        app.show_fleet = true;
        dump(&mut app, 110, 30, "+FLEET");
        let mut app = demo_app();
        app.postmortem = Some(0);
        dump(&mut app, 110, 30, "+POSTMORTEM");
        // replay
        let mut app = demo_app();
        app.cursor = Some(15);
        dump(&mut app, 110, 30, "REPLAY t=15");
    }

    // ---- review-fix regressions -------------------------------------------

    #[test]
    fn ribbon_elides_title_never_fields() {
        let mut app = demo_app();
        if let Some(m) = app.st.meta.as_mut() {
            m.title = Some(
                "an extremely long ai generated session title that would \
                 previously clip every ribbon field to its right"
                    .into(),
            );
        }
        for w in [110u16, 140] {
            let s = draw(&mut app, w, 30);
            let ribbon = s.lines().next().unwrap_or("").to_string();
            assert!(ribbon.contains('…'), "title not elided at {w}:\n{ribbon}");
            // the distinct session NAME survives even when the title elides —
            // it is the identity, never cut
            assert!(
                ribbon.contains("brisk-otter"),
                "session name lost at {w}:\n{ribbon}"
            );
            assert!(ribbon.contains("ku"), "cost field lost at {w}:\n{ribbon}");
            assert!(
                ribbon.contains("R 121k/200k"),
                "R field lost at {w}:\n{ribbon}"
            );
            assert!(ribbon.contains("busy"), "health lost at {w}:\n{ribbon}");
        }
        // narrow: never panics, still leads with the app name
        let s = draw(&mut app, 60, 30);
        assert!(s.lines().next().unwrap_or("").starts_with("amtr"));
    }

    #[test]
    fn tabs_replay_indicator_survives_medium() {
        let mut app = demo_app();
        app.cursor = Some(15);
        let s = draw(&mut app, 80, 24);
        assert!(
            s.contains("« REPLAY t=15/"),
            "REPLAY indicator clipped at 80 cols:\n{s}"
        );
        assert!(
            !s.contains("(f)SESSIONS"),
            "hints must be dropped before the indicator clips:\n{s}"
        );
        // wide terminals keep the hints
        app.cursor = None;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("(f)SESSIONS"));
    }

    #[test]
    fn turns_stack_identity_cumulative_rounding() {
        // stack top edge == round(R/B·logical) (±1 only via the min-1 rule)
        let b = 200_000.0;
        let logical = 40usize;
        for &(cr, cc, in_tok) in &[
            (0u64, 0u64, 0u64),
            (121_000, 0, 0),
            (100_000, 17_900, 2),
            (3_000, 3_000, 3_000),
            (66_666, 33_333, 1),
            (199_999, 0, 1),
        ] {
            let (cr_e, cc_e, in_e) = viz::stack_edges(cr, cc, in_tok, b, logical);
            assert!(cr_e <= cc_e && cc_e <= in_e, "bands must be monotone");
            let want = ((cr + cc + in_tok) as f64 / b * logical as f64).round() as usize;
            let slack = usize::from(in_tok > 0);
            assert!(
                in_e == want || in_e == want + slack,
                "stack {in_e} != R height {want} for ({cr},{cc},{in_tok})"
            );
        }
    }

    #[test]
    fn roll_renders_cursor_column_in_replay() {
        let count_band = |app: &mut App| -> usize {
            let buf = buffer_of(app, 110, 30);
            let mut n = 0;
            for y in 0..30u16 {
                for x in 0..110u16 {
                    if buf[(x, y)].style().bg == Some(viz::rgb(viz::C_GRID)) {
                        n += 1;
                    }
                }
            }
            n
        };
        let mut app = demo_app();
        app.tab = 1;
        assert_eq!(count_band(&mut app), 0, "no cursor band when LIVE");
        app.cursor = Some(10);
        assert!(
            count_band(&mut app) > 0,
            "replay cursor column not visible on the roll"
        );
    }

    #[test]
    fn agents_tab_selects() {
        let mut app = demo_app();
        app.tab = 3;
        let top = draw(&mut app, 110, 30);
        app.agent_sel = 1;
        let moved = draw(&mut app, 110, 30);
        assert_ne!(top, moved, "agents j/k selection has no effect");
        // selection stays visible at the far end (scroll clamps)
        app.agent_sel = 15; // last row (16 rows total)
        let s = draw(&mut app, 110, 30);
        assert!(
            s.contains("survey transcript schema"),
            "selected far-end row not scrolled into view:\n{s}"
        );
    }

    #[test]
    fn overview_fills_no_blank_gaps() {
        // the OVERVIEW must not leave rows of dead space: gauge, packed map,
        // legend, compact EKG — everything between is used. Assert the map
        // band (gauge..legend) is full of block glyphs, not empty rows.
        let mut app = demo_app();
        app.tab = 0;
        let buf = buffer_of(&mut app, 130, 42);
        let s = draw(&mut app, 130, 42);
        let legend = s
            .lines()
            .position(|l| l.contains("▪system overhead"))
            .expect("legend") as u16;
        let mut blank = 0;
        for y in 6..legend {
            let filled = (7..130u16)
                .filter(|&x| {
                    let g = buf[(x, y)].symbol();
                    g == "█" || g == "▀" || g == "▄"
                })
                .count();
            if filled == 0 {
                blank += 1;
            }
        }
        assert_eq!(blank, 0, "packed map left {blank} blank rows before legend");
    }

    #[test]
    fn rung_override_clamps_at_press_time() {
        let mut app = demo_app();
        app.tab = 0;
        let _ = draw(&mut app, 110, 30); // populates map_geom
        // demo B=200k in a small map pane: auto is already the finest
        // fitting rung, so '-' beyond the edge must not bank
        for _ in 0..8 {
            app.nudge_rung(-1);
        }
        let banked = app.rung_override;
        app.nudge_rung(1);
        assert!(
            app.rung_override > banked || banked == 0,
            "opposite press looks dead: banked {banked}, now {}",
            app.rung_override
        );
        // and the effective rung never leaves the ladder
        let (w, h) = app.map_geom.get();
        let cap = viz::map_capacity(w, h, false);
        let auto = viz::auto_rung_idx(app.st.budget, cap) as i8;
        let eff = auto + app.rung_override;
        assert!((0..viz::RUNGS.len() as i8).contains(&eff));
    }

    // =======================================================================
    // v2.1 upgrades: TURNS fused channels · FILES NOW · AGENTS load strip
    // =======================================================================

    fn press(app: &mut App, code: KeyCode) {
        app.on_key(KeyEvent::new(code, KeyModifiers::NONE));
    }

    /// Minimal offline app (no demo data): budget 200k, ready, fixed clock.
    fn bare_app() -> App {
        let (tx, keep_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_rx);
        std::mem::forget(keep_tx);
        let mut app = App::new(tx, rx);
        app.now_override = Some(NOW);
        app.blink = true;
        app.st.ready = true;
        app
    }

    /// Crafted turn for the TURNS-channel tests (wl ≡ cr, R ≡ cr+cc+in).
    fn t4(i: u64, cr: u64, cc: u64, cc5: u64, cc1: u64, in_tok: u64) -> Turn {
        Turn {
            turn: i,
            ts: "00:00:00".into(),
            model: "m".into(),
            in_tok,
            cr,
            cc,
            cc_5m: cc5,
            cc_1h: cc1,
            out: 500,
            resident: cr + cc + in_tok,
            waterline: cr,
            dur_ms: Some(5_000),
            stop: Some("tool_use".into()),
            tools: 1,
            cost_u: 5.0,
            hit: 0.95,
        }
    }

    // ---- TURNS (design tests 1–10) ----------------------------------------

    #[test]
    fn stack_edges4_identity_and_drift() {
        let b = 200_000.0;
        let logical = 42usize;
        for &(cr, cc, cc5, cc1, in_tok) in &[
            (0u64, 0u64, 0u64, 0u64, 0u64),
            (100_000, 17_900, 12_000, 5_900, 2),
            (100_000, 9_000, 0, 0, 500),        // old engine: split absent
            (66_666, 33_333, 40_000, 3_000, 1), // mis-summed split
            (3_000, 3_000, 3_000, 0, 3_000),
            (199_999, 0, 0, 0, 1),
        ] {
            let (e1, e2, e3, e4) = viz::stack_edges4(cr, cc, cc5, cc1, in_tok, b, logical);
            assert!(
                e1 <= e2 && e2 <= e3 && e3 <= e4,
                "edges not monotone for ({cr},{cc},{cc5},{cc1},{in_tok})"
            );
            // stack top must equal the legacy 3-band top: R_t is sacred
            let (_, _, old_top) = viz::stack_edges(cr, cc, in_tok, b, logical);
            assert_eq!(e4, old_top, "stack top drifted for ({cr},{cc},{cc5},{cc1},{in_tok})");
        }
        // drift guard: cc-only (old engine) ≡ all-5m split, zero purple rows
        let a = viz::stack_edges4(50_000, 9_000, 0, 0, 700, b, logical);
        let s = viz::stack_edges4(50_000, 9_000, 9_000, 0, 700, b, logical);
        assert_eq!(a, s, "split-absent must render like an all-5m split");
        assert_eq!(a.1, a.2, "cc5m′=cc must leave no purple band");
    }

    #[test]
    fn turns_purple_band_and_drift_column() {
        let mut app = bare_app();
        for t in [
            t4(0, 10_000, 0, 0, 0, 1_000),
            t4(1, 40_000, 30_000, 10_000, 20_000, 5_000), // real 1h split
            t4(2, 60_000, 9_000, 0, 0, 700),              // split absent
        ] {
            app.st.apply(Update::Turn(t));
        }
        app.st.turn_pulse = 0;
        app.tab = 2;
        let buf = buffer_of(&mut app, 110, 30);
        // chart rows y=3..24 (chart 21 rows); turn 1 at x=7, turn 2 at x=8
        let col_fgs = |x: u16| -> Vec<(u16, Option<Color>)> {
            (3..24u16).map(|y| (y, buf[(x, y)].style().fg)).collect()
        };
        let purple = viz::rgb(viz::C_ATTACH);
        let cyan = viz::rgb(viz::C_CYAN);
        let red = viz::rgb(viz::C_RED);
        let ys = |x: u16, c: Color| -> Vec<u16> {
            col_fgs(x)
                .iter()
                .filter(|(_, f)| *f == Some(c))
                .map(|(y, _)| *y)
                .collect()
        };
        let p = ys(7, purple);
        assert!(!p.is_empty(), "cc_1h turn renders no purple cell");
        let r = ys(7, red);
        let c = ys(7, cyan);
        if let (Some(&rmax), Some(&pmin)) = (r.iter().max(), p.iter().min()) {
            assert!(rmax <= pmin, "purple must sit below the red band");
        }
        if let (Some(&pmax), Some(&cmin)) = (p.iter().max(), c.iter().min()) {
            assert!(pmax <= cmin, "purple must sit above the cyan band");
        }
        // old-engine column: all-cyan, no purple anywhere
        assert!(ys(8, purple).is_empty(), "split-absent column shows purple");
    }

    #[test]
    fn turns_rail_markers() {
        let mut app = bare_app();
        for i in 0..=12u64 {
            let cr = if i == 6 { 20_000 } else { 80_000 };
            let mut t = t4(i, cr, 2_000, 2_000, 0, 500);
            t.model = if i >= 9 { "n".into() } else { "m".into() };
            app.st.apply(Update::Turn(t));
        }
        for turn in [3u64, 6] {
            app.st.apply(Update::Compaction(Compaction {
                n: 1,
                turn,
                ts: String::new(),
                trigger: "auto".into(),
                pre: 90_000,
                post: 40_000,
                dropped: 50_000,
                cum_dropped: 50_000,
                dur_ms: 1_000,
                dropped_cats: HashMap::new(),
                dropped_files: vec![],
                preserved_msgs: 1,
            }));
        }
        app.st.turn_pulse = 0;
        app.st.thrash_pulse = 0;
        app.st.compact_sweep = 0;
        app.tab = 2;
        let buf = buffer_of(&mut app, 110, 30);
        // rail = chart row 0 (y=3); col x = 6 + turn (t_lo = 0)
        assert_eq!(buf[(9, 3)].symbol(), "▼", "compaction marker missing");
        assert_eq!(buf[(9, 3)].style().fg, Some(viz::rgb(viz::C_MAGENTA)));
        // thrash + compaction at t6 → ▲ wins
        assert_eq!(buf[(12, 3)].symbol(), "▲", "thrash must beat compaction");
        assert_eq!(buf[(12, 3)].style().fg, Some(viz::rgb(viz::C_RED)));
        // model switch at t9 → ◆ white
        assert_eq!(buf[(15, 3)].symbol(), "◆", "model-switch marker missing");
        assert_eq!(buf[(15, 3)].style().fg, Some(viz::rgb(viz::C_WHITE)));
    }

    #[test]
    fn turns_prev_waterline_tick() {
        let mut app = bare_app();
        app.st.apply(Update::Turn(t4(0, 100_000, 0, 0, 0, 2_000)));
        app.st.apply(Update::Turn(t4(1, 150_000, 8_000, 8_000, 0, 2_000)));
        app.st.turn_pulse = 0;
        app.tab = 2;
        let buf = buffer_of(&mut app, 110, 30);
        // logical=42: tick row e(100k)=21 → char row 10 top half → y=13, x=7
        assert_eq!(
            buf[(7, 13)].style().fg,
            Some(viz::rgb(viz::C_WLINE)),
            "prev-waterline tick missing inside the steel band"
        );
        // first turn in the ring has no tick anywhere in its column (x=6)
        for y in 3..24u16 {
            assert_ne!(
                buf[(6, y)].style().fg,
                Some(viz::rgb(viz::C_WLINE)),
                "turn 0 must not carry a tick (no prev)"
            );
        }
    }

    #[test]
    fn turns_lane_colors() {
        let mut app = bare_app();
        let mut a = t4(0, 30_000, 2_000, 2_000, 0, 500);
        a.stop = Some("max_tokens".into());
        a.tools = 7;
        a.hit = 0.30;
        a.out = 4_000;
        a.dur_ms = Some(30_000);
        let mut b = t4(1, 40_000, 2_000, 2_000, 0, 500);
        b.stop = Some("end_turn".into());
        b.tools = 1;
        b.hit = 0.95;
        b.out = 2_000;
        app.st.apply(Update::Turn(a));
        app.st.apply(Update::Turn(b));
        app.st.turn_pulse = 0;
        app.tab = 2;
        let buf = buffer_of(&mut app, 110, 30);
        // lanes at y=24 (out), 25 (dur), 26 (ku); cols x=6 (t0), x=7 (t1)
        assert_eq!(buf[(6, 24)].style().fg, Some(viz::rgb(viz::C_RED)), "max_tokens out");
        assert_eq!(buf[(7, 24)].style().fg, Some(viz::rgb(viz::C_ASSIST)), "normal out");
        assert_eq!(buf[(6, 25)].style().fg, Some(viz::rgb(viz::C_WHITE)), "tools≥6 dur");
        assert_eq!(buf[(6, 26)].style().fg, Some(viz::rgb(viz::C_RED)), "hit<0.5 ku");
        assert_eq!(buf[(7, 26)].style().fg, Some(viz::rgb(viz::C_AMBER)), "hit≥0.9 ku");
    }

    #[test]
    fn turns_lane_fixed_scale_clamps() {
        let render = |out: u64| -> (String, Vec<String>) {
            let mut app = bare_app();
            let mut t = t4(0, 30_000, 2_000, 2_000, 0, 500);
            t.out = out;
            app.st.apply(Update::Turn(t));
            app.st.turn_pulse = 0;
            app.tab = 2;
            let buf = buffer_of(&mut app, 110, 30);
            let glyph = buf[(6, 24)].symbol().to_string();
            let gutter = (3..24u16)
                .map(|y| (0..6u16).map(|x| buf[(x, y)].symbol().to_string()).collect())
                .collect();
            (glyph, gutter)
        };
        let (g16, gut16) = render(16_000);
        let (g32, gut32) = render(32_000);
        assert_eq!(g16, "█", "16k must saturate the fixed 0–16k lane");
        assert_eq!(g32, "█", "32k must clamp, never autoscale");
        assert_eq!(gut16, gut32, "TURNS gutter moved with data");
        assert!(gut16.iter().any(|r: &String| r.contains("200k")));
    }

    #[test]
    fn turns_degradation_two_lanes() {
        // h=10 body: ledger yes, lanes 2 (out+ku, dur lane dropped)
        let app = demo_app();
        let ui = app.ui(Tier::Medium);
        let mut term = Terminal::new(TestBackend::new(70, 10)).unwrap();
        term.draw(|f| viz::render_turns_tab(&app.st, &ui, f, Rect::new(0, 0, 70, 10)))
            .unwrap();
        let buf = term.backend().buffer().clone();
        let mut lines: Vec<String> = Vec::new();
        for y in 0..10u16 {
            lines.push((0..70u16).map(|x| buf[(x, y)].symbol()).collect());
        }
        assert!(lines.iter().any(|l| l.starts_with("out")), "out lane missing");
        assert!(lines.iter().any(|l| l.starts_with("ku")), "ku lane missing");
        assert!(
            !lines.iter().any(|l| l.starts_with("dur")),
            "dur lane must drop at 2-lane heights"
        );
    }

    #[test]
    fn turns_ledger_fa_and_model_switch() {
        let mut app = demo_app();
        app.tab = 2;
        app.cursor = Some(35); // the model-switch turn
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("◆ claude-fable-5→claude-opus-4"), "switch annotation:\n{s}");
        app.cursor = Some(36); // 2 reads + 1 write
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("fa 2r/1w"), "faccess counts missing:\n{s}");
        assert!(!s.contains("◆ claude"), "no switch annotation at t36:\n{s}");
    }

    #[test]
    fn turns_new_turn_pulse() {
        let mut app = demo_app();
        app.tab = 2;
        app.st.turn_pulse = 6;
        assert!(pulses_active(&app), "turn_pulse must drive the pulse clock");
        let with = buffer_of(&mut app, 110, 30);
        app.st.turn_pulse = 0;
        let without = buffer_of(&mut app, 110, 30);
        let mut diffs: Vec<(u16, u16)> = Vec::new();
        for y in 0..30u16 {
            for x in 0..110u16 {
                if with[(x, y)] != without[(x, y)] {
                    diffs.push((x, y));
                }
            }
        }
        assert!(!diffs.is_empty(), "pulse render must differ");
        // newest column only: x = 6 + (39 − t_lo) = 45
        assert!(
            diffs.iter().all(|&(x, _)| x == 45),
            "pulse leaked outside the newest column: {diffs:?}"
        );
    }

    // ---- FILES NOW (design tests 1–8; 8 lives in ipc.rs) -------------------

    /// A(3 s) B(50 s) hot · C(400 s) cold · D(epoch 0) unknown tail.
    fn now_app() -> App {
        let mut app = bare_app();
        app.st.ready = false; // faccess seeding must not pulse
        let file = |id: u64, path: &str, tok: u64, age: f64| FileRec {
            id,
            path: path.into(),
            tok,
            reads: 1,
            writes: 1,
            edits: 0,
            waste: 500,
            last_ts: format!("16:0{id}:00"),
            last_epoch: if age > 0.0 { NOW - age } else { 0.0 },
            resident: true,
        };
        app.st.apply(Update::Files {
            upserts: vec![
                file(1, "a.rs", 32_000, 3.0),
                file(2, "b.rs", 5_000, 50.0),
                file(3, "c.rs", 2_000, 400.0),
                file(4, "d.rs", 1_000, 0.0),
            ],
        });
        for (t, fid, op, tok) in [
            (1u64, 1u64, Op::W, 4_100u64),
            (2, 2, Op::R, 12_800),
            (3, 3, Op::R, 916),
        ] {
            app.st.apply(Update::Faccess(fa(t, fid, op, tok)));
        }
        app.st.ready = true;
        app.tab = 1;
        app.files_view = FilesView::Now;
        app
    }

    #[test]
    fn files_now_zones_and_order() {
        let (hot, cold) = viz::file_now_order(&now_app().st, NOW);
        assert_eq!(hot, vec![1, 2], "hot zone order (last_epoch desc)");
        assert_eq!(cold, vec![3, 4], "cold zone: known epoch, then unknowns");
        let mut app = now_app();
        let s = draw(&mut app, 110, 30);
        let lines: Vec<&str> = s.lines().collect();
        assert!(lines[3].contains("hot 2 · cold 2"), "header counts:\n{s}");
        assert!(lines[4].contains("a.rs"), "row 1 must be A:\n{s}");
        assert!(lines[5].contains("b.rs"), "row 2 must be B:\n{s}");
        assert!(lines[6].contains(" cold "), "divider missing:\n{s}");
        assert!(lines[7].contains("c.rs"), "cold row 1 must be C:\n{s}");
        assert!(lines[8].contains("d.rs"), "cold tail must be D:\n{s}");
        assert!(lines[7].contains("6m"), "C's AGE must read 6m (floored):\n{s}");
        assert!(lines[8].contains("—"), "D's AGE must read —:\n{s}");
        // ops from the faccess ring
        assert!(lines[4].contains(" w "), "A's newest op is w:\n{s}");
        assert!(lines[5].contains(" r "), "B's newest op is r:\n{s}");
    }

    #[test]
    fn files_now_decay_brightness() {
        let mut app = now_app();
        app.file_sel = 3; // selection renders FULL brightness — test unselected rows
        let buf = buffer_of(&mut app, 110, 30);
        // basename cell at x=31 (fixed 31-cell prefix); rows y=4 (A), y=5 (B)
        let hue_a = app.st.hue_of(1);
        let hue_b = app.st.hue_of(2);
        assert_eq!(
            buf[(31, 4)].style().fg,
            Some(viz::rgb(viz::scale(hue_a, viz::heat_k(3.0)))),
            "A's brightness must follow 0.30+0.70·e^(−3/45)"
        );
        assert_eq!(
            buf[(31, 5)].style().fg,
            Some(viz::rgb(viz::scale(hue_b, viz::heat_k(50.0)))),
            "B's brightness must follow the same law at dt=50"
        );
        // the SELECTED row is full-bright + reversed — inversion over a
        // dimmed fg would swallow both signals (review finding)
        app.file_sel = 0;
        let buf = buffer_of(&mut app, 110, 30);
        assert_eq!(
            buf[(31, 4)].style().fg,
            Some(viz::rgb(app.st.hue_of(1))),
            "selected row must not be heat-dimmed"
        );
    }

    #[test]
    fn files_now_bar_fixed_scale() {
        let bar_of = |app: &mut App| -> String {
            let buf = buffer_of(app, 110, 30);
            for y in 0..30u16 {
                let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
                if line.contains("a.rs") {
                    // TOKBAR cells x=14..22
                    return (14..22u16).map(|x| buf[(x, y)].symbol()).collect();
                }
            }
            panic!("row for a.rs not found");
        };
        let mut app = now_app();
        let before = bar_of(&mut app);
        app.st.apply(Update::Files {
            upserts: vec![FileRec {
                id: 9,
                path: "huge.bin".into(),
                tok: 500_000,
                reads: 1,
                writes: 0,
                edits: 0,
                waste: 0,
                last_ts: "16:09:00".into(),
                last_epoch: NOW - 1.0,
                resident: true,
            }],
        });
        let after = bar_of(&mut app);
        assert_eq!(before, after, "0–64k bar must never autoscale");
    }

    #[test]
    fn files_now_entry_pulse() {
        let mut app = now_app();
        app.file_sel = 3; // selection is full-bright; pulse test needs heat scaling
        app.st.apply(Update::Faccess(fa(5, 1, Op::E, 2_000)));
        assert_eq!(app.st.touch_pulse, 6, "live faccess must arm the pulse");
        assert_eq!(app.st.touch_file, 1);
        let buf = buffer_of(&mut app, 110, 30);
        // AGE (x=3) and OP (x=5) cells of row 1 flash white
        assert_eq!(buf[(3, 4)].style().fg, Some(viz::rgb(viz::C_WHITE)));
        assert_eq!(buf[(5, 4)].style().fg, Some(viz::rgb(viz::C_WHITE)));
        // after the six 80 ms decrements they return to the heat color
        app.st.touch_pulse = 0;
        let buf = buffer_of(&mut app, 110, 30);
        assert_eq!(
            buf[(3, 4)].style().fg,
            Some(viz::rgb(viz::scale(viz::C_WHITE, viz::heat_k(3.0)))),
        );
        // counterpart: a backfill flood must never pulse
        let mut app = bare_app();
        app.st.ready = false;
        app.st.apply(Update::Backfill(ipc::Backfill {
            faccess: vec![fa(1, 1, Op::R, 100), fa(2, 2, Op::W, 200)],
            ..Default::default()
        }));
        assert_eq!(app.st.touch_pulse, 0, "backfill must not arm the pulse");
    }

    #[test]
    fn files_now_keys_and_crosslink() {
        let mut app = demo_app();
        app.tab = 1;
        assert!(app.files_view == FilesView::History, "HISTORY is the default");
        press(&mut app, KeyCode::Char('v'));
        assert!(app.files_view == FilesView::Now);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("v history"), "NOW header hint missing:\n{s}");
        // j j → third NOW row = f2 (viz.rs, 50 s); HISTORY size order would
        // put f1 here — proves the NOW order drives the cross-link
        press(&mut app, KeyCode::Char('j'));
        press(&mut app, KeyCode::Char('j'));
        assert_eq!(app.ui(Tier::Full).sel_file, Some(2));
        app.tab = 0;
        // the cross-link BRIGHTENS the selected file's cells toward white
        // (reverse-video on a solid "█" cell renders black — field-found; the
        // highlight is now a lerp to white, matching cell_px).
        let want = viz::rgb(viz::lerp(app.st.hue_of(2), viz::C_WHITE, 0.6));
        let buf = buffer_of(&mut app, 110, 30);
        let mut hit = false;
        for y in 6..18u16 {
            for x in 0..110u16 {
                let st = buf[(x, y)].style();
                if st.fg == Some(want) || st.bg == Some(want) {
                    hit = true;
                }
            }
        }
        assert!(hit, "NOW selection must cross-link (brighten) the MAP cells");
        // `s` in NOW is a strict no-op
        app.tab = 1;
        let before = draw(&mut app, 110, 30);
        press(&mut app, KeyCode::Char('s'));
        let after = draw(&mut app, 110, 30);
        assert_eq!(before, after, "`s` must be inert in NOW");
        assert!(app.file_sort == FileSort::Size, "sort state must not change");
    }

    #[test]
    fn files_now_tier_degradation() {
        let mut app = demo_app();
        app.tab = 1;
        app.files_view = FilesView::Now;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains('▍'), "Full tier must show the eighth-block bar:\n{s}");
        assert!(s.contains(" cold "), "divider missing at Full:\n{s}");
        let s = draw(&mut app, 80, 24);
        assert!(!s.contains('▍'), "Medium must drop the TOKBAR:\n{s}");
        let s = draw(&mut app, 50, 15);
        assert!(!s.contains("─ cold"), "Compact must drop the divider:\n{s}");
        assert!(s.contains("+2 cold"), "Compact header must carry the count:\n{s}");
        assert!(s.contains("v hist"), "Compact toggle hint missing:\n{s}");
    }

    #[test]
    fn files_now_replay_guard() {
        let mut app = demo_app();
        app.tab = 1;
        app.files_view = FilesView::Now;
        app.cursor = Some(10);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("live-only"), "replay notice missing:\n{s}");
        assert!(
            !s.contains("src/main.rs") && !s.contains("SPEC.md"),
            "NOW must render zero rows in replay:\n{s}"
        );
    }

    // ---- AGENTS (design tests 1–11; 1 lives in agents_semantics) ----------

    /// Strip fixture: 9 agents alive at t10 (overload), 4 at t20, a failed
    /// agent ending at t40, nothing at t30. 51 bare turns for the axis.
    fn strip_app() -> App {
        let mut app = bare_app();
        for i in 0..=50u64 {
            app.st.apply(Update::Turn(t4(i, 1_000, 0, 0, 0, 100)));
        }
        app.st.turn_pulse = 0;
        let agent = |id: String, state: &str, turn0: u64, turn1: u64| AgentRec {
            id,
            state: state.into(),
            agent_type: Some("t".into()),
            desc: None,
            wf: None,
            path: None,
            turn0,
            ts0: String::new(),
            turn1: Some(turn1),
            own_tok: 5_000,
            ret_tok: Some(100),
            tools: None,
            dur_ms: Some(1_000),
            t0: 0.0,
            ts_last: 0.0,
        };
        for i in 0..9u64 {
            app.st
                .apply(Update::Agent(agent(format!("nine-{i}"), "done", 10, 10)));
        }
        for i in 0..4u64 {
            app.st
                .apply(Update::Agent(agent(format!("four-{i}"), "done", 20, 20)));
        }
        app.st
            .apply(Update::Agent(agent("boom".into(), "failed", 40, 40)));
        app.tab = 3;
        app
    }

    #[test]
    fn agents_strip_concurrency_and_notch() {
        let mut app = strip_app();
        let buf = buffer_of(&mut app, 110, 30);
        // strip rows y=4 (top) / y=5 (bottom); col x = 6 + turn (t_lo = 0)
        // 4 alive at t20 → bottom char full, top blank
        assert_eq!(buf[(26, 5)].symbol(), "█", "4-alive bottom cell");
        assert_eq!(buf[(26, 5)].style().fg, Some(viz::rgb(viz::C_STEEL)));
        assert_eq!(buf[(26, 4)].symbol(), " ", "4-alive top row must be blank");
        // 0 alive at t30 → blank column
        assert_eq!(buf[(36, 4)].symbol(), " ");
        assert_eq!(buf[(36, 5)].symbol(), " ");
        // 9 alive at t10 → clamped full + white overload cap on the top half
        assert_eq!(buf[(16, 5)].symbol(), "█");
        assert_eq!(buf[(16, 4)].symbol(), "▀", "cap must split the top cell");
        assert_eq!(
            buf[(16, 4)].style().fg,
            Some(viz::rgb(viz::C_WHITE)),
            "alive>8 must cap the column white"
        );
        // failed agent's end turn → red notch on its top-most filled half
        assert_eq!(buf[(46, 5)].symbol(), "▄", "1-alive bottom half");
        assert_eq!(
            buf[(46, 5)].style().fg,
            Some(viz::rgb(viz::C_RED)),
            "failure notch must win the top filled half-cell"
        );
    }

    #[test]
    fn agents_bar_fixed_log_scale() {
        // pure: 1k → edge, 10k → 4/12 cells, 1M → full, <1k → ▏
        assert_eq!(viz::agent_tok_bar(10_000, 12), "████");
        assert_eq!(viz::agent_tok_bar(1_000_000, 12), "████████████");
        assert_eq!(viz::agent_tok_bar(999, 12), "▏");
        // render: the 10k bar is byte-identical across wildly different peers
        let bar_of = |extra_tok: u64| -> String {
            let mut app = strip_app();
            app.st.apply(Update::Agent(AgentRec {
                id: "probe".into(),
                state: "done".into(),
                agent_type: Some("probe".into()),
                desc: Some("ten-k-probe".into()),
                wf: None,
                path: None,
                turn0: 45,
                ts0: String::new(),
                turn1: Some(46),
                own_tok: 10_000,
                ret_tok: Some(500),
                tools: None,
                dur_ms: Some(1_000),
                t0: 0.0,
                ts_last: 0.0,
            }));
            app.st.apply(Update::Agent(AgentRec {
                id: "whale".into(),
                state: "done".into(),
                agent_type: Some("whale".into()),
                desc: Some("giant peer".into()),
                wf: None,
                path: None,
                turn0: 44,
                ts0: String::new(),
                turn1: Some(45),
                own_tok: extra_tok,
                ret_tok: Some(500),
                tools: None,
                dur_ms: Some(1_000),
                t0: 0.0,
                ts_last: 0.0,
            }));
            let buf = buffer_of(&mut app, 110, 30);
            for y in 0..30u16 {
                let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
                if line.contains("ten-k-probe") {
                    return (3..16u16).map(|x| buf[(x, y)].symbol()).collect();
                }
            }
            panic!("probe row not found");
        };
        let small = bar_of(2_000);
        let huge = bar_of(1_000_000);
        assert_eq!(small, huge, "log bar must never autoscale");
        assert!(small.starts_with("████ "), "10k must fill exactly 4/12 cells");
    }

    #[test]
    fn agents_rollup_auto_and_override() {
        let mut app = demo_app();
        app.tab = 3;
        let rows_of = |app: &App| {
            viz::agent_view_rows(
                viz::eff_agents(&app.st),
                app.agent_sort,
                app.agent_filter,
                &app.wf_open,
                app.st.last_turn().unwrap_or(0),
            )
        };
        // auto-expanded while children run: wf row + 12 kids + 3 solos
        let rows = rows_of(&app);
        assert_eq!(rows.len(), 16, "expanded workflow must show its children");
        match &rows[0] {
            viz::ARow::Wf { kids, expanded, .. } => {
                assert_eq!(kids.len(), 12);
                assert!(expanded);
            }
            _ => panic!("running workflow must sort first under recent"),
        }
        // all children done → auto-collapse
        for i in [10u64, 11, 12] {
            let mut done = viz::eff_agents(&app.st)
                .iter()
                .find(|a| a.id == format!("agent-k{i:02}"))
                .unwrap()
                .clone();
            done.state = "done".into();
            done.turn1 = Some(39);
            done.ret_tok = Some(1_000);
            done.dur_ms = Some(60_000);
            app.st.apply(Update::Agent(done));
        }
        assert_eq!(rows_of(&app).len(), 4, "all-done workflow must collapse");
        // Enter sets a manual override that survives a redraw
        app.agent_sel = 0; // the wf row (max end 39 sorts first)
        press(&mut app, KeyCode::Enter);
        assert_eq!(rows_of(&app).len(), 16, "manual expand must win");
        let _ = draw(&mut app, 110, 30);
        assert_eq!(rows_of(&app).len(), 16, "override must survive a redraw");
        press(&mut app, KeyCode::Enter);
        assert_eq!(rows_of(&app).len(), 4, "second Enter collapses again");
    }

    #[test]
    fn agents_sort_and_filter_keys() {
        let mut app = demo_app();
        app.tab = 3;
        press(&mut app, KeyCode::Char('s'));
        assert!(app.agent_sort == AgentSort::Tok);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("sort:tok"), "sort label missing:\n{s}");
        // first data row under Σown desc = the 302k solo
        let lines: Vec<&str> = s.lines().collect();
        let hdr = lines
            .iter()
            .position(|l| l.contains("own-tok(log)"))
            .expect("column header");
        assert!(
            lines[hdr + 1].contains("302.0k") && lines[hdr + 1].contains("code-review"),
            "302k agent must sort first by tok:\n{s}"
        );
        // filter: a a → fail; only failed rows render
        let mut app = demo_app();
        app.tab = 3;
        press(&mut app, KeyCode::Char('a'));
        press(&mut app, KeyCode::Char('a'));
        assert!(app.agent_filter == AgentFilter::Fail);
        let rows = viz::agent_view_rows(
            viz::eff_agents(&app.st),
            app.agent_sort,
            app.agent_filter,
            &app.wf_open,
            app.st.last_turn().unwrap_or(0),
        );
        for r in &rows {
            if let viz::ARow::Ag { idx, .. } = r {
                assert_eq!(viz::eff_agents(&app.st)[*idx].state, "failed");
            }
        }
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("filter:fail"), "filter label missing:\n{s}");
        assert!(s.contains("refactor step 10"), "failed child must render:\n{s}");
        assert!(s.contains("wf_refactor ×1"), "wf shows only passing kids:\n{s}");
        assert!(
            !s.contains("survey transcript"),
            "done agents must be filtered out:\n{s}"
        );
    }

    #[test]
    fn agents_enter_drills_into_agent() {
        // Enter on an agent row = attach INTO its own window (SPEC e AGENTS);
        // Backspace pops the breadcrumb and re-attaches the parent.
        let (tx, ctl_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_tx);
        let mut app = demo_app_on_channels(tx, rx);
        app.tab = 3;
        app.agent_sort = viz::AgentSort::Launch; // deterministic row order
        // select agent-a1 (the demo agent WITH a path)
        let rows = viz::agent_view_rows(
            viz::eff_agents(&app.st),
            app.agent_sort,
            app.agent_filter,
            &app.wf_open,
            app.st.last_turn().unwrap_or(0),
        );
        let a1 = rows
            .iter()
            .position(|r| matches!(r, viz::ARow::Ag { idx, .. }
                if viz::eff_agents(&app.st)[*idx].id == "agent-a1"))
            .expect("agent-a1 row");
        app.agent_sel = a1;
        press(&mut app, KeyCode::Enter);
        assert_eq!(app.attach_stack.len(), 1, "parent pushed on drill");
        let sent: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert!(
            sent.iter().any(|m| m.contains("attach")
                && m.contains("/tmp/demo/subagents/agent-a1.jsonl")),
            "drill must attach the agent transcript: {sent:?}"
        );
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("◂"), "ribbon must mark the drilled view:\n{s}");
        // Backspace returns to the parent
        press(&mut app, KeyCode::Backspace);
        assert!(app.attach_stack.is_empty());
        let back: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert!(
            back.iter()
                .any(|m| m.contains("attach") && m.contains("demo-4d2f")),
            "backspace must re-attach the parent: {back:?}"
        );
        // an agent with no path only logs
        app.agent_sel = rows
            .iter()
            .position(|r| matches!(r, viz::ARow::Ag { idx, .. }
                if viz::eff_agents(&app.st)[*idx].id == "agent-b2"))
            .unwrap();
        press(&mut app, KeyCode::Enter);
        assert!(app.attach_stack.is_empty(), "no drill without a path");
    }

    #[test]
    fn ekg_left_anchored_for_young_sessions() {
        // a 40-turn session must plot from the LEFT edge of the fixed
        // 512-turn axis, not float mid-pane (the axis window clamps at 0)
        let mut app = demo_app();
        app.tab = 0;
        let buf = buffer_of(&mut app, 110, 30);
        // find the EKG canvas rows (below the MAP+legend) and require braille
        // in the leftmost data columns
        let mut left_braille = false;
        for y in 0..30u16 {
            for x in 1..12u16 {
                let ch = buf[(x, y)].symbol().chars().next().unwrap_or(' ');
                if ('\u{2800}'..='\u{28FF}').contains(&ch) && ch != '\u{2800}' {
                    left_braille = true;
                }
            }
        }
        assert!(left_braille, "EKG trace/rules must reach the left edge");
    }

    #[test]
    fn agents_tier_degradation() {
        let mut app = demo_app();
        app.tab = 3;
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("8+"), "Medium must keep the LOAD strip:\n{s}");
        assert!(!s.contains("tools("), "Medium must drop the tools column:\n{s}");
        let s = draw(&mut app, 50, 15);
        assert!(!s.contains("8+"), "Compact must drop the strip:\n{s}");
        assert!(s.contains("fan-out"), "Compact keeps the header:\n{s}");
        assert!(s.contains("wf_refactor ×12"), "Compact keeps the list:\n{s}");
        let body: String = s.lines().skip(2).collect::<Vec<_>>().join("\n");
        assert!(!body.contains("tasks"), "Compact must drop the footer:\n{s}");
    }

    #[test]
    fn agents_replay_renders_snapshot_set() {
        let mut app = demo_app();
        app.cursor = Some(15);
        app.apply_update(Update::Snapshot(Snapshot {
            turn: 15,
            resident: 82_000,
            waterline: 70_000,
            cc: 9_000,
            map: MapMsg {
                rev: 1,
                alpha: 1.0,
                segs: vec![],
            },
            files: vec![],
            cats: HashMap::new(),
            agents: vec![AgentRec {
                id: "snap-1".into(),
                state: "done".into(),
                agent_type: Some("archivist".into()),
                desc: Some("snapshot-only agent".into()),
                wf: None,
                path: None,
                turn0: 3,
                ts0: "16:05:00".into(),
                turn1: Some(9),
                own_tok: 8_000,
                ret_tok: Some(400),
                tools: None,
                dur_ms: Some(9_000),
                t0: 0.0,
                ts_last: 0.0,
            }],
            tasks: Tasks::default(),
        }));
        app.tab = 3;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("snapshot-only agent"), "snapshot set missing:\n{s}");
        assert!(
            !s.contains("survey transcript"),
            "live agents must not leak into replay:\n{s}"
        );
    }

    #[test]
    fn agents_old_engine_and_heat_degrade() {
        let mut app = demo_app();
        app.tab = 3;
        let buf = buffer_of(&mut app, 110, 30);
        // rows: wf y=7, kids k01..k12 at y=8..19 → k11 y=18, k12 y=19
        // k12 (running, t0=ts_last=0): dur `—`, bar at the un-heated base
        assert_eq!(buf[(61, 19)].symbol(), "—", "unknown t0 must render dur —");
        assert_eq!(
            buf[(3, 19)].style().fg,
            Some(viz::rgb(viz::C_CYAN)),
            "ts_last=0 bar must be the plain state color"
        );
        // k11 (running, ts_last=NOW−30): heat-scaled cyan + live elapsed
        assert_eq!(
            buf[(3, 18)].style().fg,
            Some(viz::rgb(viz::scale(viz::C_CYAN, viz::heat_k(30.0)))),
            "running bar brightness must follow the heat law"
        );
        let row18: String = (0..110u16).map(|x| buf[(x, 18)].symbol()).collect();
        assert!(row18.contains("2m14s"), "live elapsed missing: {row18}");
        // heat clock: AGENTS with a glowing runner drives heat_active
        assert!(heat_active(&app), "running heat must arm the 500 ms clock");
        app.tab = 0;
        assert!(!heat_active(&app), "class-mode MAP must not arm heat");
    }

    #[test]
    fn agents_growth_pulse() {
        let mut app = demo_app();
        // an upsert with own_tok growth on a running agent arms the tip pulse
        let mut grown = viz::eff_agents(&app.st)
            .iter()
            .find(|a| a.id == "agent-k11")
            .unwrap()
            .clone();
        grown.own_tok += 5_000;
        app.st.apply(Update::Agent(grown));
        assert_eq!(app.st.agent_pulse.get("agent-k11"), Some(&6));
        assert!(pulses_active(&app), "agent pulse must drive the pulse clock");
        app.tab = 3;
        let buf = buffer_of(&mut app, 110, 30);
        // k11's own-tok number renders white while the pulse lives
        let row18: String = (0..110u16).map(|x| buf[(x, 18)].symbol()).collect();
        assert!(row18.contains("46.0k"), "grown own-tok missing: {row18}");
        assert_eq!(
            buf[(21, 18)].style().fg,
            Some(viz::rgb(viz::C_WHITE)),
            "own-tok must flash white during the growth pulse"
        );
    }

    // ---- SHELL (design tests 1–14; 13 lives in wire_parse_samples, 14 in
    //      no_panic_size_sweep, the cross-process proof in ipc.rs) ----------

    /// (1) Full-tier semantics: marks, tails, stderr gutter, ts, tok_out,
    /// header — and newest at BOTTOM (the console reads downward).
    #[test]
    fn shell_semantics() {
        let mut app = demo_app();
        app.tab = 5;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("$ cargo build"), "prompt missing:\n{s}");
        assert!(s.contains("○"), "ok mark missing:\n{s}");
        assert!(s.contains("✖"), "fail mark missing:\n{s}");
        assert!(s.contains("^"), "interrupt mark missing:\n{s}");
        assert!(s.contains("&"), "bg suffix missing:\n{s}");
        assert!(s.contains("Finished dev"), "out tail missing:\n{s}");
        assert!(s.contains("assertion failed"), "stderr text missing:\n{s}");
        assert!(s.contains("▎"), "stderr gutter missing:\n{s}");
        assert!(s.contains("16:47:18"), "ts column missing:\n{s}");
        assert!(s.contains("2.1k"), "tok_out missing:\n{s}");
        assert!(s.contains("cmds"), "header missing:\n{s}");
        assert!(
            s.find("ls -la").unwrap() < s.find("--selftest").unwrap(),
            "newest must render at the bottom:\n{s}"
        );
    }

    /// (2) stderr and stdout are provably styled apart.
    #[test]
    fn shell_stderr_distinct() {
        let mut app = demo_app();
        app.tab = 5;
        let buf = buffer_of(&mut app, 110, 30);
        let mut gutter_fg = None;
        let mut out_fg = None;
        for y in 0..30u16 {
            let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
            if line.contains("▎") {
                for x in 0..110u16 {
                    if buf[(x, y)].symbol() == "▎" {
                        gutter_fg = buf[(x, y)].style().fg;
                    }
                }
            }
            if line.contains("Finished dev") {
                out_fg = buf[(13, y)].style().fg; // first text cell (col 13)
            }
        }
        assert_eq!(gutter_fg, Some(viz::rgb(viz::C_RED)), "▎ must be red");
        assert_eq!(
            out_fg,
            Some(viz::rgb(viz::scale(viz::C_FG, 0.72))),
            "stdout tail must be the dimmed body tint"
        );
    }

    /// (3) tok_out spark on the FIXED 0–16k ramp with the size-color law.
    #[test]
    fn shell_tok_scale() {
        let mut app = demo_app();
        app.tab = 5;
        let buf = buffer_of(&mut app, 110, 30);
        let row_of = |needle: &str| -> u16 {
            for y in 0..30u16 {
                let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
                if line.contains(needle) {
                    return y;
                }
            }
            panic!("row {needle} not found");
        };
        // right block: spark at x=103, 6-wide number ending at x=109
        let y = row_of("--selftest");
        assert_eq!(buf[(103, y)].symbol(), "█", "18.2k must saturate the spark");
        assert_eq!(buf[(103, y)].style().fg, Some(viz::rgb(viz::C_RED)));
        assert!(
            buf[(105, y)]
                .style()
                .add_modifier
                .contains(ratatui::style::Modifier::BOLD),
            "≥16k number must be bold"
        );
        let y = row_of("ls -la");
        assert_eq!(buf[(103, y)].symbol(), "▁", "214 tok sits at the spark floor");
        assert_eq!(buf[(103, y)].style().fg, Some(viz::rgb(viz::C_DIM)));
    }

    /// (4) tail-pinned follow: overflow keeps the newest visible; a live
    /// arrival advances the console and evicts the old top line.
    #[test]
    fn shell_follow_autoscroll() {
        let mut app = demo_app();
        app.tab = 5;
        for i in 0..12u64 {
            app.st.apply(Update::Cmd(cmdrec(
                39,
                "16:50:00",
                &format!("echo probe-{i}"),
                None,
                &format!("out-{i}"),
                "",
                true,
                false,
                false,
                10,
            )));
        }
        app.st.cmd_pulse = 0;
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("probe-11"), "newest must be visible:\n{s}");
        assert!(!s.contains("ls -la"), "oldest must have scrolled out:\n{s}");
        assert!(s.contains("out-2"), "clipped top entry keeps its tail:\n{s}");
        app.apply_update(Update::Cmd(cmdrec(
            39, "16:50:09", "echo fresh", None, "", "", true, false, false, 10,
        )));
        app.st.cmd_pulse = 0;
        let s2 = draw(&mut app, 80, 24);
        let lines: Vec<&str> = s2.lines().collect();
        assert!(
            lines[22].contains("echo fresh"),
            "arrival must pin to the bottom row:\n{s2}"
        );
        assert!(!s2.contains("out-2"), "old top line must scroll away:\n{s2}");
    }

    /// (5) j/k breaks follow and anchors by SEQ identity — arrivals cannot
    /// shift it; G and End (live) restore follow with zero wire traffic.
    #[test]
    fn shell_break_restore_follow() {
        let mut app = demo_app();
        app.tab = 5;
        app.st.ack_alert(); // surface the contextual footer hints
        press(&mut app, KeyCode::Char('k'));
        assert!(!app.shell_follow, "k must break follow");
        assert_eq!(app.shell_sel, 5, "first press anchors on the newest entry");
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("newer"), "browsing posture missing:\n{s}");
        let row_before = s.lines().position(|l| l.contains("--selftest")).unwrap();
        app.apply_update(Update::Cmd(cmdrec(
            39,
            "16:49:30",
            "git status --porcelain",
            None,
            "",
            "",
            true,
            false,
            false,
            180,
        )));
        let s2 = draw(&mut app, 110, 30);
        let row_after = s2.lines().position(|l| l.contains("--selftest")).unwrap();
        assert_eq!(row_before, row_after, "seq anchor shifted on arrival");
        assert!(
            !s2.contains("git status"),
            "nothing may render below the anchor:\n{s2}"
        );
        press(&mut app, KeyCode::Char('G'));
        assert!(app.shell_follow, "G must restore follow");
        let s3 = draw(&mut app, 110, 30);
        assert!(s3.contains("git status"), "follow must show the arrival:\n{s3}");
        press(&mut app, KeyCode::Char('k'));
        assert!(!app.shell_follow);
        press(&mut app, KeyCode::End);
        assert!(app.shell_follow, "End (live) must restore follow");
        assert!(
            app.cursor.is_none() && app.seek_inflight.is_none(),
            "End while live must not seek"
        );
    }

    /// (6) Enter expands the selection — `# desc` comment + tail lines
    /// beyond the collapsed pair; Enter again collapses.
    #[test]
    fn shell_expand_toggle() {
        let mut app = demo_app();
        app.tab = 5;
        for _ in 0..5 {
            press(&mut app, KeyCode::Char('k')); // anchor, then 4 older
        }
        assert_eq!(app.shell_sel, 1, "selection must sit on cargo build");
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("Compiling"), "collapsed hides early tail lines:\n{s}");
        press(&mut app, KeyCode::Enter);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("# build the workspace"), "desc comment missing:\n{s}");
        assert!(s.contains("Compiling amtr v0.1.1"), "full tail missing:\n{s}");
        press(&mut app, KeyCode::Enter);
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("# build the workspace"), "collapse failed:\n{s}");
        assert!(!s.contains("Compiling"), "collapse failed:\n{s}");
    }

    /// (7) replay filters the console to `turn ≤ cursor` UI-side; End
    /// restores everything and re-pins follow.
    #[test]
    fn shell_replay_filters_future() {
        let mut app = demo_app();
        app.tab = 5;
        app.set_cursor(20);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("ls -la"), "turn 18 ≤ 20 must stay:\n{s}");
        assert!(!s.contains("--selftest"), "turn 38 > 20 must vanish:\n{s}");
        assert!(!s.contains("cargo test"), "turn 25 > 20 must vanish:\n{s}");
        assert!(s.contains("« console @ t=20"), "replay posture missing:\n{s}");
        press(&mut app, KeyCode::End);
        assert!(app.cursor.is_none());
        assert!(app.shell_follow, "go_live must re-pin the console");
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("--selftest"), "live must restore the feed:\n{s}");
    }

    /// (8) arrival pulse is live-only: backfill never arms it; a live cmd
    /// white-blends the prompt line and decays over the pulse clock.
    #[test]
    fn shell_pulse_live_only() {
        let mut app = bare_app();
        app.st.apply(Update::Backfill(ipc::Backfill {
            cmds: vec![cmdrec(0, "16:00:00", "git log", None, "", "", true, false, false, 10)],
            ..Default::default()
        }));
        assert_eq!(app.st.cmd_pulse, 0, "backfill must not arm the pulse");
        app.st.apply(Update::Cmd(cmdrec(
            1,
            "16:00:05",
            "pulse-probe",
            None,
            "",
            "",
            true,
            false,
            false,
            10,
        )));
        assert_eq!(app.st.cmd_pulse, 6, "a live cmd must arm the pulse");
        assert!(pulses_active(&app), "cmd_pulse must drive the pulse clock");
        app.tab = 5;
        let cell_fg = |app: &mut App| -> Color {
            let buf = buffer_of(app, 110, 30);
            for y in 0..30u16 {
                let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
                if line.contains("pulse-probe") {
                    return buf[(13, y)].style().fg.unwrap();
                }
            }
            panic!("probe row not found");
        };
        let hot = cell_fg(&mut app);
        assert_eq!(
            hot,
            viz::rgb(viz::lerp(viz::C_FG, viz::C_WHITE, 0.6)),
            "prompt must render white-blended at full pulse"
        );
        app.st.cmd_pulse = 3; // three pulse ticks later
        let cooler = cell_fg(&mut app);
        assert_eq!(cooler, viz::rgb(viz::lerp(viz::C_FG, viz::C_WHITE, 0.3)));
        assert_ne!(hot, cooler, "pulse intensity must decay");
        app.st.cmd_pulse = 0;
        assert_eq!(cell_fg(&mut app), viz::rgb(viz::C_FG));
    }

    /// (9) `a` filter cycles all ⇄ err (err = !ok || interrupted).
    #[test]
    fn shell_err_filter() {
        let mut app = demo_app();
        app.tab = 5;
        press(&mut app, KeyCode::Char('a'));
        assert!(app.shell_filter == ShellFilter::Err);
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("ls -la"), "ok cmd must be filtered:\n{s}");
        assert!(!s.contains("cargo build"), "ok cmd must be filtered:\n{s}");
        assert!(!s.contains("--selftest"), "ok cmd must be filtered:\n{s}");
        assert!(s.contains("cargo test"), "✖ entry must stay:\n{s}");
        assert!(s.contains("npm run dev"), "^ entry must stay:\n{s}");
        assert!(s.contains("filter err"), "filter label missing:\n{s}");
        press(&mut app, KeyCode::Char('a'));
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("ls -la"), "all must restore:\n{s}");
        assert!(s.contains("filter all"), "label must cycle back:\n{s}");
    }

    /// (10) six tabs under the unmodified elision ladder: hints drop first,
    /// labels compact next, the LIVE/REPLAY indicator never clips.
    #[test]
    fn tabs_six_elide_80() {
        let mut app = demo_app();
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("[6]SHELL"), "sixth tab missing:\n{s}");
        assert!(!s.contains("(f)SESSIONS"), "hints must drop at 80:\n{s}");
        assert!(s.contains("● LIVE"), "LIVE must survive:\n{s}");
        app.cursor = Some(15);
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("« REPLAY t=15/39"), "REPLAY must survive:\n{s}");
        assert!(s.contains("[6]SHELL"), "full labels survive REPLAY at 80:\n{s}");
        app.cursor = None;
        let s = draw(&mut app, 54, 16);
        assert!(s.contains("[6]"), "compact label missing:\n{s}");
        assert!(!s.contains("SHELL"), "compact must drop the word:\n{s}");
    }

    /// (11) Compact = pure command ledger; Enter (verbose follow) still
    /// expands — drill-down overrides density.
    #[test]
    fn shell_compact_tier() {
        let mut app = demo_app();
        app.tab = 5;
        let s = draw(&mut app, 50, 15);
        assert!(s.contains("$ cargo build"), "prompt missing at Compact:\n{s}");
        assert!(!s.contains("cmds"), "Compact must drop the header:\n{s}");
        assert!(!s.contains("Finished dev"), "Compact must drop out tails:\n{s}");
        press(&mut app, KeyCode::Enter);
        let s = draw(&mut app, 50, 15);
        assert!(
            s.contains(r#""type":"ready""#),
            "Enter must expand even at Compact:\n{s}"
        );
    }

    /// (12) Medium: no ts, no desc, exactly one collapsed output line with
    /// stderr preferred over its sibling stdout.
    #[test]
    fn shell_medium_tier() {
        let mut app = demo_app();
        app.tab = 5;
        let s = draw(&mut app, 80, 24);
        assert!(!s.contains("16:47:18"), "Medium must drop the ts column:\n{s}");
        assert!(
            !s.contains("build the workspace"),
            "Medium must drop the desc:\n{s}"
        );
        assert!(s.contains("assertion failed"), "stderr must be preferred:\n{s}");
        assert!(
            !s.contains("test result: FAILED"),
            "the sibling stdout line must drop:\n{s}"
        );
        assert!(
            s.contains("Finished dev"),
            "stdout-only entries keep one line:\n{s}"
        );
    }

    // ---- SHELL / RETRIEVAL perspective (`v` inside SHELL) -----------------

    /// Full-tier semantics: kind glyphs, srcs, queries, result meta, header
    /// per-source token totals — and newest at BOTTOM (the feed reads down).
    #[test]
    fn retrieval_semantics() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("⌕"), "search glyph missing:\n{s}");
        assert!(s.contains("⇣"), "fetch glyph missing:\n{s}");
        assert!(s.contains("# tools"), "toolsearch glyph missing:\n{s}");
        assert!(s.contains("✖ example.com"), "failed ✖ glyph missing:\n{s}");
        assert!(s.contains("claude_ai_Dropbox"), "mcp src missing:\n{s}");
        assert!(s.contains("ratatui braille canvas api"), "query missing:\n{s}");
        assert!(s.contains("→ 5"), "result count missing:\n{s}");
        assert!(s.contains("2.4s"), "duration missing:\n{s}");
        assert!(s.contains("16:47:33"), "ts column missing:\n{s}");
        assert!(s.contains("5 pulls"), "header count missing:\n{s}");
        assert!(s.contains("web 3.5k tok"), "web total missing:\n{s}");
        assert!(s.contains("mcp 17.9k tok"), "mcp total missing:\n{s}");
        assert!(s.contains("tools 350 tok"), "tools total missing:\n{s}");
        assert!(s.contains("● FOLLOW"), "follow posture missing:\n{s}");
        assert!(
            s.find("ratatui braille").unwrap() < s.find("example.com").unwrap(),
            "newest must render at the bottom:\n{s}"
        );
    }

    /// `v` toggles CONSOLE ↔ RETRIEVAL (logged); follow is SHARED across the
    /// perspectives while selection stays per-view; a live `ret` arms the
    /// arrival pulse and the empty state renders.
    #[test]
    fn retrieval_toggle_and_shared_follow() {
        let mut app = demo_app();
        app.tab = 5;
        app.st.ack_alert(); // surface the contextual footer hints
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("cmds"), "default view must be the console:\n{s}");
        assert!(s.contains("v retrieval"), "footer must offer the toggle:\n{s}");
        press(&mut app, KeyCode::Char('v'));
        assert!(app.shell_view == ShellView::Retrieval);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("pulls"), "v must switch to retrieval:\n{s}");
        assert!(!s.contains("$ cargo build"), "console rows must vanish:\n{s}");
        assert!(s.contains("v console"), "footer must offer the way back:\n{s}");
        assert!(
            app.st.log.iter().any(|l| l.contains("shell view: retrieval")),
            "switch must be logged"
        );
        // k breaks the SHARED follow, anchoring on the newest ret seq
        press(&mut app, KeyCode::Char('k'));
        assert!(!app.shell_follow, "k must break follow");
        assert_eq!(app.ret_sel, 4, "first press anchors on the newest entry");
        press(&mut app, KeyCode::Char('v'));
        assert!(app.shell_view == ShellView::Console);
        assert!(
            !app.shell_follow,
            "follow posture is SHARED — v must not restore it"
        );
        assert_eq!(app.shell_sel, 0, "console selection must stay untouched");
        press(&mut app, KeyCode::Char('G'));
        assert!(app.shell_follow, "G restores the shared follow");
        // live arrival pulse + empty state (fresh app, no rets)
        let mut app = bare_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        let s = draw(&mut app, 110, 30);
        assert!(
            s.contains("no retrievals yet — web/MCP pulls will stream here"),
            "empty state missing:\n{s}"
        );
        app.st.apply(Update::Ret(retrec(
            0, "16:00:01", "search", "web", "probe", None, None, None, 10, true,
        )));
        assert_eq!(app.st.ret_pulse, 6, "a live ret must arm the pulse");
        assert!(pulses_active(&app), "ret_pulse must drive the pulse clock");
    }

    /// `a` filters the rets to err (= !ok) with its own per-view filter
    /// state; the console filter is untouched.
    #[test]
    fn retrieval_err_filter() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        press(&mut app, KeyCode::Char('a'));
        assert!(app.ret_filter == ShellFilter::Err);
        assert!(
            app.shell_filter == ShellFilter::All,
            "console filter must stay untouched"
        );
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("example.com"), "the failed pull must stay:\n{s}");
        assert!(!s.contains("docs.rs"), "ok pulls must be filtered:\n{s}");
        assert!(
            !s.contains("claude_ai_Dropbox"),
            "ok pulls must be filtered:\n{s}"
        );
        assert!(s.contains("filter err"), "filter label missing:\n{s}");
        press(&mut app, KeyCode::Char('a'));
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("docs.rs"), "all must restore:\n{s}");
        assert!(s.contains("filter all"), "label must cycle back:\n{s}");
    }

    /// Replay filters the feed to `turn ≤ cursor` UI-side (the console's
    /// law); End restores everything and re-pins the shared follow.
    #[test]
    fn retrieval_replay_filters_future() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        app.set_cursor(20);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("ratatui braille"), "turn 15 ≤ 20 must stay:\n{s}");
        assert!(!s.contains("docs.rs"), "turn 21 > 20 must vanish:\n{s}");
        assert!(
            !s.contains("claude_ai_Dropbox"),
            "turn 24 > 20 must vanish:\n{s}"
        );
        assert!(s.contains("« retrieval @ t=20"), "replay posture missing:\n{s}");
        press(&mut app, KeyCode::End);
        assert!(app.cursor.is_none());
        assert!(app.shell_follow, "go_live must re-pin the feed");
        let s = draw(&mut app, 110, 30);
        assert!(
            s.contains("claude_ai_Dropbox"),
            "live must restore the feed:\n{s}"
        );
    }

    /// tok on the FIXED 0–16k ramp with the console's color law (17.9k =
    /// saturated red BOLD; sub-1k dim at the floor), and the mcp glyph
    /// carries its per-server accent from the file-hue wheel.
    #[test]
    fn retrieval_tok_ramp_and_server_hue() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        let buf = buffer_of(&mut app, 110, 30);
        let row_of = |needle: &str| -> u16 {
            for y in 0..30u16 {
                let line: String = (0..110u16).map(|x| buf[(x, y)].symbol()).collect();
                if line.contains(needle) {
                    return y;
                }
            }
            panic!("row {needle} not found");
        };
        // right block: spark at x=103, 6-wide number ending at x=109
        let y = row_of("claude_ai_Dropbox");
        assert_eq!(buf[(103, y)].symbol(), "█", "17.9k must saturate the spark");
        assert_eq!(buf[(103, y)].style().fg, Some(viz::rgb(viz::C_RED)));
        assert!(
            buf[(105, y)]
                .style()
                .add_modifier
                .contains(ratatui::style::Modifier::BOLD),
            "≥16k number must be bold"
        );
        // mcp glyph at x=9 (after the 9-cell ts column): per-server accent
        assert_eq!(buf[(9, y)].symbol(), "◆", "mcp glyph missing");
        assert_eq!(
            buf[(9, y)].style().fg,
            Some(viz::rgb(viz::server_hue("claude_ai_Dropbox"))),
            "mcp accent must come from the server-name hash"
        );
        let y = row_of("ratatui braille");
        assert_eq!(buf[(103, y)].symbol(), "▁", "900 tok sits at the spark floor");
        assert_eq!(buf[(103, y)].style().fg, Some(viz::rgb(viz::C_DIM)));
        assert_eq!(buf[(9, y)].symbol(), "⌕", "search glyph missing");
        assert_eq!(buf[(9, y)].style().fg, Some(viz::rgb(viz::C_CYAN)));
    }

    /// Enter expands the selection: the full url plus the bytes / duration /
    /// result-count detail line; Enter again collapses.
    #[test]
    fn retrieval_expand_detail() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        for _ in 0..4 {
            press(&mut app, KeyCode::Char('k')); // anchor, then 3 older
        }
        assert_eq!(app.ret_sel, 1, "selection must sit on the docs.rs fetch");
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("48.2kB"), "collapsed must hide the detail:\n{s}");
        press(&mut app, KeyCode::Enter);
        assert!(app.ret_expand);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("48.2kB"), "bytes detail missing:\n{s}");
        assert!(s.contains("1.9s"), "duration detail missing:\n{s}");
        assert!(s.contains("tok 2.6k"), "tok detail missing:\n{s}");
        press(&mut app, KeyCode::Enter);
        let s = draw(&mut app, 110, 30);
        assert!(!s.contains("48.2kB"), "collapse failed:\n{s}");
    }

    /// Medium drops the ts column; Compact drops the header — mirroring the
    /// console's degradation ladder.
    #[test]
    fn retrieval_tier_degradation() {
        let mut app = demo_app();
        app.tab = 5;
        app.shell_view = ShellView::Retrieval;
        let s = draw(&mut app, 80, 24);
        assert!(s.contains("pulls"), "Medium keeps the header:\n{s}");
        assert!(!s.contains("16:47:33"), "Medium must drop the ts column:\n{s}");
        assert!(
            s.contains("claude_ai_Dropbox"),
            "rows must survive Medium:\n{s}"
        );
        let s = draw(&mut app, 50, 15);
        assert!(!s.contains("pulls"), "Compact must drop the header:\n{s}");
        assert!(
            s.contains("claude_ai_Dropbox"),
            "rows must survive Compact:\n{s}"
        );
    }

    // ---- INSPECT mode (OVERVIEW `i`; the engine round-trip lives in
    //      ipc::tests::real_engine_peek_roundtrip) --------------------------

    /// `i` toggles INSPECT; ←/→ and j/k are CAPTURED (the segment cursor
    /// moves, the turn cursor doesn't); walk clamps at both ends; Esc exits;
    /// a map rebuild (rev change) exits with a log line; `i` is inert off
    /// the OVERVIEW tab.
    #[test]
    fn inspect_toggle_and_key_capture() {
        let mut app = demo_app();
        app.tab = 0;
        press(&mut app, KeyCode::Char('i'));
        assert!(app.inspect, "i must enter INSPECT");
        assert_eq!(app.inspect_idx, 0, "walk starts on the overhead seg");
        assert!(
            app.st.log.iter().any(|l| l.contains("INSPECT on")),
            "mode change must log"
        );
        // captured walk: ←/→ and j/k move the SEGMENT cursor…
        press(&mut app, KeyCode::Right);
        press(&mut app, KeyCode::Char('j'));
        assert_eq!(app.inspect_idx, 2);
        press(&mut app, KeyCode::Char('k'));
        press(&mut app, KeyCode::Left);
        press(&mut app, KeyCode::Left); // already at 0: clamps
        assert_eq!(app.inspect_idx, 0);
        // …and the TURN cursor never moves (Left would otherwise scrub)
        assert!(app.cursor.is_none(), "turn cursor must not move in INSPECT");
        assert!(app.seek_inflight.is_none(), "no seek may go out in INSPECT");
        // clamp at the far end
        let n = viz::eff_segs(&app.st).len();
        for _ in 0..2 * n {
            press(&mut app, KeyCode::Right);
        }
        assert_eq!(app.inspect_idx, n - 1, "walk must clamp at the last seg");
        // Esc exits INSPECT — captured, so the alert survives (no global
        // ack/go_live path may fire)
        press(&mut app, KeyCode::Esc);
        assert!(!app.inspect, "Esc must exit INSPECT");
        assert!(app.st.alert.is_some(), "captured Esc must not ack the alert");
        // a map rebuild while INSPECT is active exits it (seg ids from the
        // old rev may vanish — the documented simplest honest rule)
        press(&mut app, KeyCode::Char('i'));
        app.apply_update(Update::Map(MapMsg {
            rev: 3,
            alpha: 0.97,
            segs: vec![seg(0, "overhead", 18_000, None, 0, 600.0)],
        }));
        assert!(!app.inspect, "map rebuild must exit INSPECT");
        assert!(
            app.st.log.iter().any(|l| l.contains("INSPECT exited")),
            "the rebuild exit must log"
        );
        // `i` off the OVERVIEW tab is inert
        app.tab = 2;
        press(&mut app, KeyCode::Char('i'));
        assert!(!app.inspect);
    }

    /// The legend row becomes the segment identity line
    /// (`#id cat · path · born t · est N ×α = M tok`), the walked segment's
    /// MAP cells render REVERSED, and the footer carries the INSPECT hints.
    #[test]
    fn inspect_identity_line_and_highlight() {
        let mut app = demo_app();
        app.tab = 0;
        app.st.ack_alert(); // surface the footer hints
        press(&mut app, KeyCode::Char('i'));
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("#0 overhead"), "identity line missing:\n{s}");
        assert!(s.contains("18.0k tok"), "overhead tok missing:\n{s}");
        assert!(!s.contains("▪system overhead 18k"), "legend must be replaced:\n{s}");
        assert!(
            s.contains("←/→ walk · enter open/peek · p peek · esc exit"),
            "INSPECT footer hint missing:\n{s}"
        );
        // walk to seg #5 — file src/main.rs, born t23, 6.0k tok at α0.97
        for _ in 0..5 {
            press(&mut app, KeyCode::Right);
        }
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("#5 file"), "cat missing:\n{s}");
        assert!(s.contains("src/main.rs"), "file path missing:\n{s}");
        assert!(s.contains("born t23"), "born turn missing:\n{s}");
        assert!(
            s.contains("est 6.2k ×α0.97 = 6.0k tok"),
            "est ×α math missing:\n{s}"
        );
        // the walked segment's cells render REVERSED on the MAP
        // the walked segment ANIMATES on the blink clock: white flash on
        // the on-phase, REVERSED on the off-phase — a static inversion
        // vanishes into a dense map
        let fid = viz::eff_segs(&app.st)[app.inspect_idx]
            .file
            .expect("walked seg must be the file chunk");
        let dim = viz::rgb(viz::scale(app.st.hue_of(fid), 0.40));
        let count = |app: &mut App, want: ratatui::style::Color| -> usize {
            let buf = buffer_of(app, 110, 30);
            let mut n = 0usize;
            for y in 6..18u16 {
                for x in 7..110u16 {
                    if buf[(x, y)].style().fg == Some(want) {
                        n += 1;
                    }
                }
            }
            n
        };
        // breathing spotlight: white blaze on-phase, DEEP DIM off-phase —
        // never reverse (a fg/bg swap is invisible on a solid chunk)
        app.blink = true;
        let white_on = count(&mut app, viz::rgb(viz::C_WHITE));
        let dim_on = count(&mut app, dim);
        app.blink = false;
        let white_off = count(&mut app, viz::rgb(viz::C_WHITE));
        let dim_off = count(&mut app, dim);
        assert!(white_on > 0, "on-phase must flash the segment white");
        assert!(dim_off > 0, "off-phase must deep-dim the segment");
        assert!(
            white_off == 0 && dim_on == 0,
            "phases must be mutually exclusive — the alternation IS the animation"
        );
        // peek overlay open → the spotlight FREEZES to a steady white blaze
        // (the reader is on the text; a breathing map behind it is noise).
        // Found via the tmux scene harness (inspect-peek animated 103 cells).
        app.peek = Some(ipc::PeekMsg {
            seg: viz::eff_segs(&app.st)[app.inspect_idx].id,
            found: true,
            cat: cat("file"),
            kind: Some("user".into()),
            uuid: None,
            born: 32,
            est: 6_000,
            tok: 6_000,
            file: Some(fid),
            excerpt: "x".into(),
            truncated: false,
        });
        app.blink = true;
        let white_blink_on = count(&mut app, viz::rgb(viz::C_WHITE));
        app.blink = false;
        let white_blink_off = count(&mut app, viz::rgb(viz::C_WHITE));
        let dim_frozen = count(&mut app, dim);
        assert!(
            white_blink_on > 0 && white_blink_off == white_blink_on && dim_frozen == 0,
            "peek open must FREEZE the spotlight white on both blink phases \
             (no breathing behind the overlay)"
        );
        app.peek = None;
        // exiting INSPECT restores the legend
        press(&mut app, KeyCode::Esc);
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("▪system overhead 18k"), "legend must return:\n{s}");
    }

    /// Enter opens $EDITOR for file-backed chunks and peeks otherwise;
    /// `p` always peeks; walking never sends (explicit-request-only, SPEC c).
    #[test]
    fn inspect_enter_sends_peek() {
        let (tx, ctl_rx) = std::sync::mpsc::channel();
        let (keep_tx, rx) = std::sync::mpsc::channel::<Update>();
        std::mem::forget(keep_tx);
        let mut app = demo_app_on_channels(tx, rx);
        app.tab = 0;
        press(&mut app, KeyCode::Char('i'));
        for _ in 0..3 {
            press(&mut app, KeyCode::Right);
        }
        press(&mut app, KeyCode::Enter);
        let sent: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert_eq!(
            sent,
            vec![r#"{"type":"peek","seg":3}"#.to_string()],
            "Enter on a non-file chunk must peek exactly the walked seg"
        );
        press(&mut app, KeyCode::Right);
        press(&mut app, KeyCode::Left);
        assert!(
            ctl_rx.try_recv().is_err(),
            "cursor moves must never send peeks (explicit-request-only)"
        );
        // Enter on a FILE-backed chunk opens $EDITOR instead of peeking
        for _ in 0..2 {
            press(&mut app, KeyCode::Right); // idx 3 -> 5 = file src/main.rs
        }
        press(&mut app, KeyCode::Enter);
        assert!(
            ctl_rx.try_recv().is_err(),
            "Enter on a file chunk must not peek"
        );
        assert!(
            app.pending_editor
                .as_deref()
                .is_some_and(|p| p.ends_with("src/main.rs")),
            "Enter on a file chunk must open $EDITOR on it: {:?}",
            app.pending_editor
        );
        // `p` peeks unconditionally, even on file chunks
        app.pending_editor = None;
        press(&mut app, KeyCode::Char('p'));
        let sent: Vec<String> = std::iter::from_fn(|| ctl_rx.try_recv().ok())
            .map(|c| serde_json::to_string(&c).unwrap())
            .collect();
        assert_eq!(sent.len(), 1, "p must send exactly one peek");
        assert!(sent[0].contains(r#""type":"peek""#));
        assert!(app.pending_editor.is_none());
    }

    /// A peek reply lands only when it answers the WALKED seg (stale replies
    /// dropped); the match opens the overlay with the excerpt; the overlay
    /// parks the walk keys; Esc closes the overlay FIRST, INSPECT second.
    #[test]
    fn inspect_peek_stale_drop_and_overlay() {
        let mut app = demo_app();
        app.tab = 0;
        press(&mut app, KeyCode::Char('i'));
        for _ in 0..3 {
            press(&mut app, KeyCode::Right); // seg #3 (assistant)
        }
        // stale reply (the user walked on before it arrived) → dropped
        app.apply_update(Update::Peek(pk(
            7,
            true,
            "file",
            Some("user"),
            Some(2),
            "stale text",
            false,
        )));
        assert!(app.peek.is_none(), "mismatched seg must be dropped");
        // matching reply → overlay: title, header, excerpt, truncation mark
        app.apply_update(Update::Peek(pk(
            3,
            true,
            "assistant",
            Some("assistant"),
            None,
            "the actual reply text lives here",
            true,
        )));
        assert!(app.peek.is_some(), "matching reply must open the overlay");
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("PEEK — seg #3"), "overlay title missing:\n{s}");
        assert!(
            s.contains("#3 assistant assistant"),
            "overlay header missing:\n{s}"
        );
        assert!(
            s.contains("the actual reply text lives here"),
            "excerpt missing:\n{s}"
        );
        assert!(
            s.contains("…truncated at 2000 chars"),
            "truncation marker missing:\n{s}"
        );
        // the walk keys are parked while the overlay is open
        press(&mut app, KeyCode::Right);
        assert_eq!(app.inspect_idx, 3, "overlay must park the walk keys");
        assert!(app.peek.is_some());
        // Esc closes the overlay FIRST…
        press(&mut app, KeyCode::Esc);
        assert!(
            app.peek.is_none() && app.inspect,
            "Esc must close the overlay only"
        );
        // …and exits INSPECT second
        press(&mut app, KeyCode::Esc);
        assert!(!app.inspect, "second Esc must exit INSPECT");
    }

    /// found:false renders the eviction notice; the overhead seg's answer is
    /// the engine explainer; Enter and `i` also close the overlay while
    /// INSPECT stays on.
    #[test]
    fn inspect_overlay_evicted_and_overhead() {
        let mut app = demo_app();
        app.tab = 0;
        press(&mut app, KeyCode::Char('i')); // idx 0 = the overhead seg (#0)
        app.apply_update(Update::Peek(ipc::PeekMsg {
            tok: 18_000,
            est: 0,
            born: 0,
            ..pk(
                0,
                true,
                "overhead",
                Some("overhead"),
                None,
                "Server-side context the transcript cannot itemize: the \
                 system prompt, tool schemas, skill listings and MCP \
                 instructions.",
                false,
            )
        }));
        let s = draw(&mut app, 110, 30);
        assert!(s.contains("system prompt"), "overhead explainer missing:\n{s}");
        assert!(
            s.contains("#0 overhead overhead"),
            "overhead header missing:\n{s}"
        );
        press(&mut app, KeyCode::Enter); // Enter closes too
        assert!(
            app.peek.is_none() && app.inspect,
            "Enter must close the overlay only"
        );
        // an evicted seg answers found:false → the eviction notice
        press(&mut app, KeyCode::Right); // seg #1
        app.apply_update(Update::Peek(pk(1, false, "user", None, None, "", false)));
        let s = draw(&mut app, 110, 30);
        assert!(
            s.contains("evicted — no longer in the transcript window"),
            "eviction notice missing:\n{s}"
        );
        press(&mut app, KeyCode::Char('i')); // i closes the overlay, not INSPECT
        assert!(
            app.peek.is_none() && app.inspect,
            "i must close the overlay only"
        );
    }
}
