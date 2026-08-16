//! The welcome tour — a nearly wordless, animated walkthrough of the whole
//! instrument, in the instrument (the amtrino help philosophy: the graphics
//! do the talking, one caption per scene).
//!
//! Sixteen stops. Each stop DRIVES the real UI (switches the tab, turns
//! INSPECT on, rewinds the cursor, opens SESSIONS / the wall / a post-mortem)
//! and then keeps driving it in a loop — the lenses cycle, the segment
//! cursor sweeps, the playhead rewinds and snaps back, views flip, a tile
//! quicklooks — while everything outside the focused region is darkened and
//! a two-row caption strip sits beside it: what you are looking at, the keys
//! that do it, progress dots, nav. Navigation is owned by the tour
//! (`→ ⏎ ␣` next · `←` back · `esc` skip · `q` quit · `?` help); each stop
//! passes its own keys through so the reader can take over — the first
//! pass-through key parks that stop's animation. Opens by itself once
//! (`~/.claude/amtr/welcomed` absent); `w` / `--tour` reopen it any time.
//!
//! Content lives here, in one table, so a UI change has one place to keep
//! honest. Rendering is `viz::render_tour`; the marker is in main.rs.

use ratatui::crossterm::event::KeyCode;
use ratatui::layout::Rect;

use crate::viz::{self, MapMode, ShellView, TourCard};
use crate::{App, Panes};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Stop {
    Welcome,
    Ribbon,
    Tabs,
    Map,
    Inspect,
    Trend,
    Rewind,
    Files,
    Turns,
    Agents,
    Events,
    Postmortem,
    Shell,
    Sessions,
    Wall,
    Finish,
}

/// The tour, in order. `App.tour` indexes into this.
pub const TOUR: [Stop; 16] = [
    Stop::Welcome,
    Stop::Ribbon,
    Stop::Tabs,
    Stop::Map,
    Stop::Inspect,
    Stop::Trend,
    Stop::Rewind,
    Stop::Files,
    Stop::Turns,
    Stop::Agents,
    Stop::Events,
    Stop::Postmortem,
    Stop::Shell,
    Stop::Sessions,
    Stop::Wall,
    Stop::Finish,
];

/// How far the REWIND animation pulls the cursor back from the tail.
const REWIND_TURNS: u64 = 8;

impl Stop {
    /// Keys this stop lets THROUGH to the normal dispatch chain (so the
    /// reader can take over the thing being shown). Everything else — except
    /// the tour's own nav keys and `q`/`?` — is swallowed while the tour is
    /// open.
    pub fn allows(self, code: KeyCode) -> bool {
        use KeyCode::*;
        let jk = matches!(code, Char('j') | Char('k') | Up | Down);
        match self {
            Stop::Tabs => matches!(code, Char('1'..='6')),
            Stop::Map => matches!(code, Char('m' | 't' | '+' | '=' | '-')),
            Stop::Inspect => matches!(code, Char('j' | 'k')),
            Stop::Files => jk || matches!(code, Char('s' | 'v')),
            Stop::Agents => jk || matches!(code, Char('v' | 's' | 'a')),
            Stop::Events => jk,
            Stop::Shell => jk || matches!(code, Char('v' | 'a' | 'G')),
            Stop::Sessions | Stop::Wall => matches!(code, Up | Down),
            _ => false,
        }
    }

    /// Animation period (ms) — None = a still stop.
    pub fn period_ms(self) -> Option<u64> {
        match self {
            Stop::Tabs => Some(700),
            Stop::Map => Some(1100),
            Stop::Inspect => Some(450),
            Stop::Rewind => Some(380),
            Stop::Files => Some(650),
            Stop::Agents => Some(2200),
            Stop::Events => Some(800),
            Stop::Shell => Some(2600),
            Stop::Sessions => Some(550),
            Stop::Wall => Some(650),
            _ => None,
        }
    }

    /// Drive the instrument to the view this stop talks about (frame 0).
    pub fn enter(self, app: &mut App) {
        match self {
            Stop::Welcome | Stop::Ribbon | Stop::Tabs | Stop::Finish => {
                app.tab = 0;
            }
            Stop::Map => {
                app.tab = 0;
                app.map_mode = MapMode::Class;
                app.reset_inspect(None);
            }
            Stop::Inspect => {
                app.tab = 0;
                app.map_mode = MapMode::Class;
                app.inspect = true;
                app.peek = None;
                app.inspect_idx = 0;
            }
            Stop::Trend => {
                app.tab = 0;
                app.reset_inspect(None);
            }
            Stop::Rewind => app.tab = 0,
            Stop::Files => {
                app.tab = 1;
                app.files_view = viz::FilesView::History;
                app.file_sel = 0;
            }
            Stop::Turns => app.tab = 2,
            Stop::Agents => {
                app.tab = 3;
                app.agent_grid = true;
            }
            Stop::Events => {
                app.tab = 4;
                app.event_sel = 0;
            }
            Stop::Postmortem => {
                app.tab = 4;
                if !app.st.compactions.is_empty() {
                    app.postmortem = Some(app.st.compactions.len() - 1);
                }
            }
            Stop::Shell => {
                app.tab = 5;
                app.shell_view = ShellView::Console;
                app.shell_follow = true;
            }
            Stop::Sessions => {
                app.show_fleet = true;
                app.system_wide = false;
                app.fleet_sel = 0;
                app.fleet_query.clear();
                if !app.demo {
                    app.send(crate::ipc::Control::FleetRefresh);
                }
            }
            Stop::Wall => {
                app.show_fleet = true;
                app.system_wide = true;
                app.fleet_sel = 0;
                app.fleet_peek = None;
            }
        }
    }

    /// One animation frame (`frame` ≥ 1, monotonic since `enter`). Each
    /// stop loops its own little demonstration of the feature.
    pub fn tick(self, app: &mut App, frame: u32) {
        let f = frame as usize;
        match self {
            Stop::Tabs => app.tab = f % 6,
            Stop::Map => {
                app.map_mode = [MapMode::Class, MapMode::Heat, MapMode::Age, MapMode::Cache]
                    [f % 4];
            }
            Stop::Inspect => {
                let n = viz::eff_segs(&app.st).len().max(1);
                app.inspect_idx = f % n;
                app.peek = None;
            }
            Stop::Rewind => {
                // ← × 8 (one per frame), hold, End, hold, again
                let Some(last) = app.st.last_turn() else { return };
                let back = REWIND_TURNS.min(last / 2);
                if back == 0 {
                    return;
                }
                let cycle = back as usize + 8;
                let k = f % cycle;
                if k == 0 {
                    app.go_live();
                } else if k <= back as usize {
                    app.set_cursor(last as i64 - k as i64);
                }
                // else: hold at the rewound turn
            }
            Stop::Files => {
                // walk the HISTORY rows, then a few beats of NOW, again
                let n = app.current_file_order().len().max(1);
                let cycle = n + 4;
                let k = f % cycle;
                if k < n {
                    app.files_view = viz::FilesView::History;
                    app.file_sel = k;
                } else {
                    app.files_view = viz::FilesView::Now;
                    app.file_sel = 0;
                }
            }
            Stop::Agents => app.agent_grid = f.is_multiple_of(2),
            Stop::Events => {
                let n = app.st.events.len().max(1);
                app.event_sel = f % n;
            }
            Stop::Shell => {
                app.shell_view = if f.is_multiple_of(2) {
                    ShellView::Console
                } else {
                    ShellView::Retrieval
                };
                app.shell_follow = true;
            }
            Stop::Sessions => {
                let n = viz::fleet_rows_filtered(&app.st, "").len().max(1);
                app.fleet_sel = f % n;
            }
            Stop::Wall => {
                // walk the tiles, quicklook one, hold, close, again
                let live = viz::fleet_live_rows(&app.st);
                let n = live.len().clamp(1, 6); // a few tiles, then the quicklook
                let cycle = n + 6;
                let k = f % cycle;
                if k < n {
                    app.fleet_peek = None;
                    app.fleet_sel = k;
                } else if k == n {
                    if let Some(s) = live.get(app.fleet_sel.min(n - 1)) {
                        let id = s.id.clone();
                        app.request_fleet_peek(id);
                    }
                } else if k == cycle - 1 {
                    app.fleet_peek = None;
                }
            }
            _ => {}
        }
    }

    /// Undo whatever this stop set up that should not outlive it.
    pub fn leave(self, app: &mut App) {
        match self {
            Stop::Inspect => app.reset_inspect(None),
            Stop::Rewind => app.go_live(),
            Stop::Files => {
                app.files_view = viz::FilesView::History;
                app.file_sel = 0;
            }
            Stop::Agents => app.agent_grid = true,
            Stop::Shell => app.shell_view = ShellView::Console,
            Stop::Postmortem => app.postmortem = None,
            Stop::Sessions | Stop::Wall => {
                app.show_fleet = false;
                app.system_wide = false;
                app.fleet_peek = None;
                app.fleet_kill = None;
            }
            _ => {}
        }
    }

    /// The region the reader should look at: everything else is darkened
    /// and the strip is placed beside it. `Rect::ZERO` = nothing singled out
    /// (strip centered, whole frame darkened); the full `area` = do not
    /// darken (the stop's own overlay already fills the screen).
    pub fn focus(self, app: &App, area: Rect, panes: &Panes) -> Rect {
        let body = panes.body.unwrap_or(area);
        let overview = |i: usize| -> Rect {
            if panes.big || panes.tier == viz::Tier::Compact || body.height < 10 {
                return body;
            }
            let [hdr, map, legend, ekg] = crate::overview_layout(app, panes.tier, body);
            match i {
                0 => hdr.union(map).union(legend),
                _ => ekg,
            }
        };
        match self {
            Stop::Welcome | Stop::Finish => Rect::ZERO,
            Stop::Ribbon => panes.ribbon.unwrap_or(area),
            Stop::Tabs => panes.tabs.unwrap_or(area),
            Stop::Map | Stop::Inspect => overview(0),
            Stop::Trend => overview(1),
            Stop::Rewind => match (panes.tabs, panes.scrubber) {
                (Some(t), Some(s)) => t.union(s),
                (Some(t), None) => t,
                _ => area,
            },
            // TURNS keeps its footer legend lit: it IS the explanation
            Stop::Turns => match panes.footer {
                Some(f) => body.union(f),
                None => body,
            },
            Stop::Files | Stop::Agents | Stop::Events | Stop::Shell => body,
            Stop::Postmortem | Stop::Sessions | Stop::Wall => area,
        }
    }

    /// Strip anchored to the TOP of the focus instead of below/above it —
    /// for views whose content hugs the bottom (a tail-following console,
    /// a chart with its ledger under it).
    pub fn prefer_top(self) -> bool {
        matches!(self, Stop::Shell | Stop::Turns)
    }

    /// What the cat says at this stop, plus the key line. Speech is short,
    /// in the guide's own voice, and asks the reader to try the keys that
    /// pass through on this stop. `idx`/`n` = progress.
    pub fn card(self, app: &App, idx: usize, n: usize) -> TourCard {
        let (speech, keys): (String, &str) = match self {
            Stop::Welcome => {
                let who = match app.st.meta.as_ref() {
                    Some(m) if !m.session_id.is_empty() => {
                        let name = if m.name.is_empty() {
                            m.session_id.chars().take(8).collect::<String>()
                        } else {
                            m.name.clone()
                        };
                        format!("you're attached to the session called {name} — this is its context window, live.")
                    }
                    _ => "no session attached yet — that's fine, follow me anyway.".to_string(),
                };
                (
                    format!("meow! I'm your amtr guide. {who} I'll walk you through the whole instrument — press → when you're ready."),
                    "→ next · esc skip · w brings me back",
                )
            }
            Stop::Ribbon => (
                "up top, the vital signs: R is the resident context against the budget (the ▮ light goes amber at 60%, red at 85%), then fill rate, compaction ETA, cost in ku, running agents and liveness.".into(),
                "",
            ),
            Stop::Tabs => (
                "six views, one key each. the right end says ● LIVE while you follow along and « REPLAY when you've rewound. try 1 through 6 — I'll wait!".into(),
                "1–6",
            ),
            Stop::Map => (
                "this box IS the budget: every cell a block of tokens, colored by what fills it (the legend names the colors). watch me flip the lens — then try m yourself!".into(),
                "m lens · t theme · +/- rung",
            ),
            Stop::Inspect => (
                "INSPECT lets me walk the map like a debugger — the line under it names each block. try j/k to walk it; ⏎ reads the actual text of a block.".into(),
                "i · j/k · ⏎ · p peek",
            ),
            Stop::Trend => (
                "resident context, turn by turn. the cyan line is the cache waterline, ▼ is a compaction cliff, and the dotted line is where you're headed.".into(),
                "",
            ),
            Stop::Rewind => (
                "time travel! I'm stepping the cursor back one turn at a time — every view rewinds with it. on your own: ←/→, ⇧ for ten, End to snap back LIVE.".into(),
                "←/→ · ⇧ ±10 · home · end LIVE",
            ),
            Stop::Files => (
                "every file this session touched: ▀ read, ▄ write. the waste column is what re-reads cost you. try j/k, and v for the NOW view!".into(),
                "v now/history · s sort · o $EDITOR",
            ),
            Stop::Turns => (
                "what each turn cost — the legend lives in the footer. steel is cache hits (cheap), red is uncached input (full price). ▼▲◆ on the rail are compaction, thrash, model switch.".into(),
                "←/→ scrub · c post-mortem",
            ),
            Stop::Agents => (
                "subagents, each in its own context window. amp is tokens burned per token returned to you. try v for the ledger; ⏎ dives into an agent, backspace comes back.".into(),
                "v grid/ledger · ⏎ drill in · bksp out",
            ),
            Stop::Events => (
                "the ledger of trouble: ▼ compactions, ✖ errors, ◆ model fallbacks. try j/k — and ⏎ on a compaction opens its autopsy, which is next.".into(),
                "j/k · ⏎ · c latest",
            ),
            Stop::Postmortem => {
                if app.st.compactions.is_empty() {
                    (
                        "no compaction in this session yet. when one fires, c opens its autopsy right here: what was dropped, by category and by file.".into(),
                        "c",
                    )
                } else {
                    (
                        "a compaction's autopsy — before, after, and exactly what was dropped, by category and by file. c opens the latest one from anywhere.".into(),
                        "c · ←/→ history",
                    )
                }
            }
            Stop::Shell => (
                "the console Claude never shows you: every command, its exit, its stderr. it follows the tail like a terminal. try v for the retrieval feed!".into(),
                "v · j/k browse · a errors · G follow",
            ),
            Stop::Sessions => (
                "every session on this machine — type to filter, ⏎ to attach. I'm read-only, promise. try ↑/↓!".into(),
                "f",
            ),
            Stop::Wall => (
                "the wall: every live session as a tank draining as it fills. ␣ quicklooks the chat, ⏎ attaches, x ends one. try ↑/↓!".into(),
                "f then tab",
            ),
            Stop::Finish => (
                "that's the instrument! R writes a PDF report, ? is the searchable help, and w brings me back any time. go play — meow.".into(),
                "R report · ? help · w tour",
            ),
        };
        TourCard {
            speech,
            keys: keys.to_string(),
            idx,
            n,
        }
    }
}
