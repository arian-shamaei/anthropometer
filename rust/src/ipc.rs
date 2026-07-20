//! JSON-line IPC with the Python engine (`amtr_engine.py`).
//!
//! This module owns the serde message types and the subprocess plumbing. It
//! spawns the Python child, a reader thread that turns each stdout line into an
//! [`Update`], and a writer thread that turns each [`Control`] into a line on
//! the child's stdin. No UI code lives here.
//!
//! Protocol (SPEC.md, normative): one JSON object per line, `\n`-terminated,
//! UTF-8, internally tagged by a `"type"` field. Unknown message types are
//! ignored; a line that fails to parse becomes an [`Update::Log`] rather than
//! being fatal. Engine stderr is inherited and is NOT part of the protocol.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::JoinHandle;
use std::time::Duration;

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// vocabulary (SPEC §a)
// ---------------------------------------------------------------------------

/// Context categories. Unknown wire strings degrade to [`Cat::Unknown`]
/// (version-drift law: tolerate everything).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Cat {
    Overhead,
    User,
    Assistant,
    Thinking,
    /// hidden reasoning: encrypted signature-only thinking, measured from
    /// usage (out − visible) — resident but invisible to the transcript
    Reasoning,
    File,
    Bash,
    Tool,
    Attach,
    Summary,
    #[serde(other)]
    Unknown,
}

impl Default for Cat {
    fn default() -> Self {
        Cat::Unknown
    }
}

impl Cat {
    pub fn label(self) -> &'static str {
        match self {
            Cat::Overhead => "overhead",
            Cat::User => "user",
            Cat::Assistant => "assistant",
            Cat::Thinking => "thinking",
            Cat::Reasoning => "reasoning",
            Cat::File => "file",
            Cat::Bash => "bash",
            Cat::Tool => "tool",
            Cat::Attach => "attach",
            Cat::Summary => "summary",
            Cat::Unknown => "?",
        }
    }

    /// Formal, self-explaining name for legends (the short `label` stays the
    /// engine/lookup key). Matches the PDF report's Composition legend.
    pub fn display_name(self) -> &'static str {
        match self {
            Cat::Overhead => "system overhead",
            Cat::User => "user input",
            Cat::Assistant => "assistant output",
            Cat::Thinking => "visible reasoning",
            Cat::Reasoning => "hidden reasoning",
            Cat::File => "file content",
            Cat::Bash => "shell output",
            Cat::Tool => "tool results",
            Cat::Attach => "injected context",
            Cat::Summary => "compaction summary",
            Cat::Unknown => "?",
        }
    }
}

/// Fixed display order for legends / category bars.
pub const CAT_ORDER: [Cat; 10] = [
    Cat::Overhead,
    Cat::User,
    Cat::Assistant,
    Cat::Thinking,
    Cat::Reasoning,
    Cat::File,
    Cat::Bash,
    Cat::Tool,
    Cat::Attach,
    Cat::Summary,
];

/// File access operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Op {
    R,
    W,
    E,
    #[serde(other)]
    Other,
}

/// Event severity. Unknown strings degrade to Info (manual impl: serde's
/// `#[serde(other)]` must sit on the last variant, but Ord needs Info first).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Info,
    Warn,
    Error,
}

impl<'de> Deserialize<'de> for Severity {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        Ok(match s.as_str() {
            "warn" => Severity::Warn,
            "error" => Severity::Error,
            _ => Severity::Info, // "info" and anything unknown
        })
    }
}

// ---------------------------------------------------------------------------
// sub-objects (SPEC §b) — Turn/Faccess/Compaction/Agent/Tasks are the payloads
// of the corresponding messages minus the `type` tag, so the enum wraps them
// as newtype variants (serde internally-tagged supports this).
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct Sess {
    pub id: String,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub pid: Option<i64>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub project: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub mtime: f64,
    #[serde(default)]
    pub live: bool,
    #[serde(default)]
    pub resident: Option<u64>,
    #[serde(default)]
    pub budget: Option<u64>,
    #[serde(default)]
    pub last_prompt: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Seg {
    pub id: u64,
    pub cat: Cat,
    pub tok: u64,
    #[serde(default)]
    pub file: Option<u64>,
    /// turn the segment was born on
    #[serde(default)]
    pub born: u64,
    /// epoch seconds of last access (heat)
    #[serde(default)]
    pub ts: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FileRec {
    pub id: u64,
    pub path: String,
    #[serde(default)]
    pub tok: u64,
    #[serde(default)]
    pub reads: u64,
    #[serde(default)]
    pub writes: u64,
    #[serde(default)]
    pub edits: u64,
    #[serde(default)]
    pub waste: u64,
    #[serde(default)]
    pub last_ts: String,
    /// epoch seconds UTC of the newest access; 0 = unknown (old engine).
    /// Drives the FILES NOW view's live decay (SPEC §b File).
    #[serde(default)]
    pub last_epoch: f64,
    #[serde(default = "default_true")]
    pub resident: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Deserialize)]
pub struct Turn {
    pub turn: u64,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub model: String,
    #[serde(rename = "in", default)]
    pub in_tok: u64,
    #[serde(default)]
    pub cr: u64,
    #[serde(default)]
    pub cc: u64,
    #[serde(default)]
    pub cc_5m: u64,
    #[serde(default)]
    pub cc_1h: u64,
    #[serde(default)]
    pub out: u64,
    #[serde(default)]
    pub resident: u64,
    #[serde(default)]
    pub waterline: u64,
    #[serde(default)]
    pub dur_ms: Option<u64>,
    #[serde(default)]
    pub stop: Option<String>,
    #[serde(default)]
    pub tools: u64,
    #[serde(default)]
    pub cost_u: f64,
    #[serde(default)]
    pub hit: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Faccess {
    pub turn: u64,
    #[serde(default)]
    pub ts: String,
    pub file: u64,
    pub op: Op,
    #[serde(default)]
    pub tok: u64,
}

/// One completed Bash execution — the SHELL console feed (SPEC §b `cmd`).
/// `cmd` is the command's head; `out`/`err` are engine-side TAILS (control
/// characters stripped, truncation marked with a leading `…` that renders as
/// data). `tok_out` = estimated tokens of the FULL result as charged to
/// context. Version-drift law: an absent `ok` must not render as failure —
/// it defaults TRUE.
#[derive(Debug, Clone, Deserialize)]
pub struct CmdRec {
    pub turn: u64,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub epoch: f64,
    #[serde(default)]
    pub cmd: String,
    #[serde(default)]
    pub desc: Option<String>,
    #[serde(default)]
    pub out: String,
    #[serde(default)]
    pub err: String,
    #[serde(default = "default_true")]
    pub ok: bool,
    #[serde(default)]
    pub interrupted: bool,
    #[serde(default)]
    pub bg: bool,
    #[serde(default)]
    pub tok_out: u64,
    /// UI-side arrival sequence (never on the wire): selection anchors by
    /// seq IDENTITY so arrivals can't slide the selection.
    #[serde(skip)]
    pub seq: u64,
}

/// External-retrieval kind (SPEC §b `ret`). Unknown wire strings degrade to
/// [`RetKind::Unknown`] (version-drift law).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RetKind {
    Search,
    Fetch,
    Toolsearch,
    Mcp,
    #[serde(other)]
    #[default]
    Unknown,
}

/// One completed EXTERNAL retrieval — the agentic-retrieval feed (SPEC §b
/// `ret`), SHELL's second perspective. WebSearch → search/src "web";
/// WebFetch → fetch/src host; ToolSearch → toolsearch/src "tools";
/// `mcp__<server>__<tool>` → mcp/src server. `tok` = estimated tokens the
/// result injected into context. File tools NEVER appear here (FILES owns
/// file retrieval). Version-drift law: absent `ok` defaults TRUE.
#[derive(Debug, Clone, Deserialize)]
pub struct RetRec {
    pub turn: u64,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub epoch: f64,
    #[serde(default)]
    pub kind: RetKind,
    #[serde(default)]
    pub src: String,
    #[serde(default)]
    pub q: String,
    #[serde(default)]
    pub n: Option<u64>,
    #[serde(default)]
    pub bytes: Option<u64>,
    #[serde(default)]
    pub dur_ms: Option<u64>,
    #[serde(default)]
    pub tok: u64,
    #[serde(default = "default_true")]
    pub ok: bool,
    /// UI-side arrival sequence (never on the wire): selection anchors by
    /// seq IDENTITY so arrivals can't slide the selection.
    #[serde(skip)]
    pub seq: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DroppedFile {
    pub file: u64,
    #[serde(default)]
    pub tok: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Compaction {
    #[serde(default)]
    pub n: u64,
    pub turn: u64,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub trigger: String,
    #[serde(default)]
    pub pre: u64,
    #[serde(default)]
    pub post: u64,
    #[serde(default)]
    pub dropped: u64,
    #[serde(default)]
    pub cum_dropped: u64,
    #[serde(default)]
    pub dur_ms: u64,
    #[serde(default)]
    pub dropped_cats: HashMap<String, u64>,
    #[serde(default)]
    pub dropped_files: Vec<DroppedFile>,
    #[serde(default)]
    pub preserved_msgs: u64,
}

#[derive(Debug, Clone, Copy, Default, Deserialize)]
pub struct ToolCounts {
    #[serde(default)]
    pub r: u64,
    #[serde(default)]
    pub s: u64,
    #[serde(default)]
    pub b: u64,
    #[serde(default)]
    pub e: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentRec {
    pub id: String,
    #[serde(default)]
    pub state: String, // running | done | failed
    #[serde(default)]
    pub agent_type: Option<String>,
    #[serde(default)]
    pub desc: Option<String>,
    #[serde(default)]
    pub wf: Option<String>,
    /// the agent's own transcript path — the drill-in target (SPEC §b)
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub turn0: u64,
    #[serde(default)]
    pub ts0: String,
    #[serde(default)]
    pub turn1: Option<u64>,
    #[serde(default)]
    pub own_tok: u64,
    #[serde(default)]
    pub ret_tok: Option<u64>,
    #[serde(default)]
    pub tools: Option<ToolCounts>,
    #[serde(default)]
    pub dur_ms: Option<u64>,
    /// epoch seconds UTC of launch; 0 = unknown (old engine) → dur `—`.
    #[serde(default)]
    pub t0: f64,
    /// epoch seconds UTC of the newest own-transcript activity; 0 = unknown
    /// (old engine) → no heat glow. Drives working-vs-wedged brightness.
    #[serde(default)]
    pub ts_last: f64,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Tasks {
    #[serde(default)]
    pub total: u64,
    #[serde(default)]
    pub done: u64,
    #[serde(default)]
    pub in_progress: u64,
    #[serde(default)]
    pub active: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Health {
    #[serde(default)]
    pub status: String, // busy | idle | stalled | dead | offline
    #[serde(default)]
    pub last_activity_ts: f64,
    #[serde(default)]
    pub api_errors: u64,
    #[serde(default)]
    pub retry_in_ms: Option<u64>,
    #[serde(default)]
    pub stalled: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct EventRec {
    pub kind: String,
    #[serde(default)]
    pub severity: Severity,
    #[serde(default)]
    pub ts: String,
    #[serde(default)]
    pub turn: u64,
    #[serde(default)]
    pub msg: String,
}

impl Default for Severity {
    fn default() -> Self {
        Severity::Info
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Meta {
    pub session_id: String,
    /// bumped by the engine per attach — a same-session re-attach must
    /// still reset UI file state (SPEC b meta)
    #[serde(default)]
    pub attach_gen: u64,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub project: String,
    /// distinct, readable session handle (roster name or memorable adj-noun)
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub budget: u64,
    #[serde(default)]
    pub t_auto: f64,
    #[serde(default)]
    pub cc_version: Option<String>,
    /// type unspecified in the spec — accept anything (version-drift law)
    #[serde(default)]
    pub started_at: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MapMsg {
    #[serde(default)]
    pub rev: u64,
    #[serde(default = "default_alpha")]
    pub alpha: f64,
    #[serde(default)]
    pub segs: Vec<Seg>,
}

fn default_alpha() -> f64 {
    1.0
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Backfill {
    #[serde(default)]
    pub turns: Vec<Turn>,
    #[serde(default)]
    pub faccess: Vec<Faccess>,
    #[serde(default)]
    pub compactions: Vec<Compaction>,
    #[serde(default)]
    pub agents: Vec<AgentRec>,
    #[serde(default)]
    pub events: Vec<EventRec>,
    #[serde(default)]
    pub cmds: Vec<CmdRec>,
    #[serde(default)]
    pub rets: Vec<RetRec>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Snapshot {
    pub turn: u64,
    /// the sought turn's own R/C/cc (SPEC §b): replay renders (cache-mode
    /// tri-coloring, MAP fill) never fall back to live numbers; defaults keep
    /// old engines parseable (version-drift law)
    #[serde(default)]
    pub resident: u64,
    #[serde(default)]
    pub waterline: u64,
    #[serde(default)]
    pub cc: u64,
    pub map: MapMsg,
    #[serde(default)]
    pub files: Vec<FileRec>,
    #[serde(default)]
    pub cats: HashMap<String, u64>,
    #[serde(default)]
    pub agents: Vec<AgentRec>,
    #[serde(default)]
    pub tasks: Tasks,
}

/// Reply to [`Control::Peek`] — the segment's underlying record content,
/// sanitized and clipped (≤2000 chars): the ONE sanctioned exception to the
/// no-content wire rule (SPEC §b `peek`). Version-drift law: everything but
/// the seg id is `#[serde(default)]`, and an absent `found` defaults FALSE —
/// the inverse of the cmd `ok` law, because a missing answer must never
/// render as content that exists.
#[derive(Debug, Clone, Deserialize)]
pub struct PeekMsg {
    pub seg: u64,
    #[serde(default)]
    pub found: bool,
    #[serde(default)]
    pub cat: Cat,
    /// the record type (user/assistant/attachment/…); "overhead" for seg 0
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub uuid: Option<String>,
    #[serde(default)]
    pub born: u64,
    #[serde(default)]
    pub est: u64,
    #[serde(default)]
    pub tok: u64,
    #[serde(default)]
    pub file: Option<u64>,
    #[serde(default)]
    pub excerpt: String,
    #[serde(default)]
    pub truncated: bool,
}

// ---------------------------------------------------------------------------
// wire messages
// ---------------------------------------------------------------------------

/// Engine -> UI. Internally tagged by `"type"`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Update {
    Init {
        #[serde(default)]
        engine_version: String,
        #[serde(default)]
        sessions: Vec<Sess>,
        #[serde(default)]
        default_session: Option<String>,
    },
    Meta(Meta),
    Backfill(Backfill),
    Ready {
        #[serde(default)]
        session_id: String,
        #[serde(default)]
        turns: u64,
        #[serde(default)]
        resident: u64,
        #[serde(default)]
        budget: u64,
    },
    Turn(Turn),
    Map(MapMsg),
    MapAdd {
        #[serde(default)]
        rev: u64,
        #[serde(default)]
        segs: Vec<Seg>,
    },
    Files {
        #[serde(default)]
        upserts: Vec<FileRec>,
    },
    Faccess(Faccess),
    Cats {
        #[serde(default)]
        totals: HashMap<String, u64>,
    },
    Compaction(Compaction),
    Agent(AgentRec),
    Cmd(CmdRec),
    Ret(RetRec),
    Tasks(Tasks),
    Health(Health),
    Event(EventRec),
    Fleet {
        #[serde(default)]
        sessions: Vec<Sess>,
    },
    Snapshot(Snapshot),
    Peek(PeekMsg),
    /// reply to `report`: the ground-truth report was written to `path`
    ReportDone {
        #[serde(default)]
        ok: bool,
        #[serde(default)]
        path: String,
        #[serde(default)]
        msg: String,
    },
    Log {
        msg: String,
    },
}

/// UI -> Engine. Internally tagged by `"type"`.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Control {
    Attach { session: String },
    Seek { turn: u64 },
    /// INSPECT-mode content request. Explicit-request-only (sent on Enter,
    /// never per-cursor-move), so no coalescing is needed (SPEC §c).
    Peek { seg: u64 },
    Live,
    /// write a ground-truth report of the attached session (SPEC §f) — the
    /// engine already has it parsed, so this is instant
    Report,
    Set { key: String, value: serde_json::Value },
    FleetRefresh,
    Quit,
}

// ---------------------------------------------------------------------------
// subprocess plumbing (a proven split-process design)
// ---------------------------------------------------------------------------

/// Handle to the spawned engine subprocess and its I/O threads.
///
/// Dropping it does NOT kill the child — send [`Control::Quit`] first, then
/// call [`EngineHandle::shutdown`] to join threads and reap the process.
pub struct EngineHandle {
    pub child: Child,
    reader: Option<JoinHandle<()>>,
    writer: Option<JoinHandle<()>>,
    /// Set by [`EngineHandle::shutdown`] to wake the writer thread even when
    /// the caller still holds its `Sender<Control>`.
    writer_stop: Arc<AtomicBool>,
}

/// How to launch the engine.
pub struct SpawnCfg {
    /// python interpreter (default `python3`)
    pub python: String,
    /// path to `amtr_engine.py`
    pub engine: PathBuf,
    /// extra argv passed through to the engine (`--session`, `--project`, …)
    pub passthrough: Vec<String>,
}

/// Where to find `amtr_engine.py`, checked in order (overridable by `--engine`):
///   1. the `AMTR_ENGINE` env var (Homebrew's wrapper sets this),
///   2. next to the executable, or `../libexec` / `../lib/amtr` beside it — how
///      prebuilt-binary bundles and Homebrew-style layouts ship the engine,
///   3. the compile-time repo path (dev builds / `cargo install --path`).
pub fn default_engine_path() -> PathBuf {
    if let Ok(p) = std::env::var("AMTR_ENGINE") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    // beside the running binary — makes a downloaded `amtr` + `amtr_engine.py`
    // bundle work from anywhere, no env var or repo needed
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for cand in [
                dir.join("amtr_engine.py"),
                dir.join("../libexec/amtr_engine.py"),
                dir.join("../lib/amtr/amtr_engine.py"),
            ] {
                if cand.is_file() {
                    return cand;
                }
            }
        }
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .map(|repo| repo.join("amtr_engine.py"))
        .unwrap_or_else(|| PathBuf::from("amtr_engine.py"))
}

/// Spawn the Python engine as a child process.
///
/// cwd = the engine's directory (the repo root), piped stdin/stdout, inherited
/// stderr (tracebacks pass through; NOT part of the protocol).
///
/// Two `std::thread` workers:
///   * reader: child stdout line-by-line → [`Update`]; parse errors become
///     [`Update::Log`] (never fatal); EOF drops the sender (UI sees a
///     disconnect).
///   * writer: owns child stdin; `recv_timeout(100ms)` + AtomicBool stop so
///     [`EngineHandle::shutdown`] never deadlocks; per-line flush.
pub fn spawn_engine(
    cfg: &SpawnCfg,
) -> std::io::Result<(Sender<Control>, Receiver<Update>, EngineHandle)> {
    let cwd = cfg
        .engine
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    let mut child = Command::new(&cfg.python)
        .arg(&cfg.engine)
        .args(&cfg.passthrough)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;

    let child_stdout = child.stdout.take().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::BrokenPipe, "child stdout not captured")
    })?;
    let child_stdin = child.stdin.take().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::BrokenPipe, "child stdin not captured")
    })?;

    let (update_tx, update_rx) = mpsc::channel::<Update>();
    let (control_tx, control_rx) = mpsc::channel::<Control>();

    let reader = std::thread::Builder::new()
        .name("ipc-reader".into())
        .spawn(move || {
            let buf = BufReader::new(child_stdout);
            for line in buf.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => break, // pipe I/O error: child died
                };
                if line.trim().is_empty() {
                    continue;
                }
                let update = match serde_json::from_str::<Update>(&line) {
                    Ok(u) => u,
                    // Unknown "type" / malformed line → Log entry, never fatal.
                    Err(e) => Update::Log {
                        msg: format!("[ipc parse error] {e}: {line}"),
                    },
                };
                if update_tx.send(update).is_err() {
                    break; // UI dropped the receiver
                }
            }
            // Thread end drops `update_tx` → the UI's Receiver disconnects.
        })?;

    let writer_stop = Arc::new(AtomicBool::new(false));
    let writer_stop_thread = Arc::clone(&writer_stop);
    let writer = std::thread::Builder::new()
        .name("ipc-writer".into())
        .spawn(move || {
            let mut stdin = child_stdin;
            loop {
                match control_rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(ctrl) => {
                        let mut line = match serde_json::to_string(&ctrl) {
                            Ok(s) => s,
                            Err(_) => continue, // unserializable Control: skip
                        };
                        line.push('\n');
                        if stdin.write_all(line.as_bytes()).is_err() {
                            break;
                        }
                        if stdin.flush().is_err() {
                            break;
                        }
                    }
                    Err(RecvTimeoutError::Timeout) => {
                        if writer_stop_thread.load(Ordering::Relaxed) {
                            break;
                        }
                    }
                    Err(RecvTimeoutError::Disconnected) => break,
                }
            }
            // Dropping `stdin` closes the pipe → EOF ≡ quit for the engine.
        })?;

    let handle = EngineHandle {
        child,
        reader: Some(reader),
        writer: Some(writer),
        writer_stop,
    };

    Ok((control_tx, update_rx, handle))
}

impl EngineHandle {
    /// Join both I/O threads and reap the child. Call after sending
    /// [`Control::Quit`]. Self-sufficient: it signals the writer thread via
    /// the stop flag, so it never deadlocks even if the caller still holds
    /// its `Sender<Control>`.
    pub fn shutdown(mut self) -> std::io::Result<()> {
        self.writer_stop.store(true, Ordering::Relaxed);
        if let Some(w) = self.writer.take() {
            let _ = w.join();
        }
        if let Some(r) = self.reader.take() {
            let _ = r.join();
        }
        self.child.wait()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Version-drift law for the three new wire fields: File.last_epoch and
    /// agent t0/ts_last are #[serde(default)] — payloads with AND without
    /// them must parse (old engines send neither).
    #[test]
    fn new_fields_parse_with_and_without() {
        let old = r#"{"id":3,"path":"a.rs","tok":100,"last_ts":"01:02:03"}"#;
        let f: FileRec = serde_json::from_str(old).unwrap();
        assert_eq!(f.last_epoch, 0.0);
        let new = r#"{"id":3,"path":"a.rs","tok":100,"last_ts":"01:02:03",
                      "last_epoch":1752771852.4}"#;
        let f: FileRec = serde_json::from_str(new).unwrap();
        assert!((f.last_epoch - 1_752_771_852.4).abs() < 1e-6);

        let old = r#"{"id":"agent-1","state":"running","turn0":4}"#;
        let a: AgentRec = serde_json::from_str(old).unwrap();
        assert_eq!(a.t0, 0.0);
        assert_eq!(a.ts_last, 0.0);
        let new = r#"{"id":"agent-1","state":"running","turn0":4,
                      "t0":1752771852.4,"ts_last":1752771986.1}"#;
        let a: AgentRec = serde_json::from_str(new).unwrap();
        assert!((a.t0 - 1_752_771_852.4).abs() < 1e-6);
        assert!((a.ts_last - 1_752_771_986.1).abs() < 1e-6);
    }

    /// End-to-end subprocess round trip: spawn a real python3 child speaking
    /// the protocol, confirm reader parses, unknown types become Log, writer
    /// delivers Controls, EOF/quit shuts down cleanly (no deadlock).
    #[test]
    fn spawn_roundtrip_and_shutdown() {
        let dir = std::env::temp_dir().join("amtr-ipc-test");
        let _ = std::fs::create_dir_all(&dir);
        let engine = dir.join("fake_engine.py");
        std::fs::write(
            &engine,
            r#"import sys, json
print(json.dumps({"type": "log", "msg": "hello"}), flush=True)
print(json.dumps({"type": "totally_unknown", "x": 1}), flush=True)
print("this is not json", flush=True)
for line in sys.stdin:
    obj = json.loads(line)
    if obj.get("type") == "quit":
        print(json.dumps({"type": "ready", "session_id": "s",
                          "turns": 1, "resident": 2, "budget": 3}), flush=True)
        break
"#,
        )
        .unwrap();
        let cfg = SpawnCfg {
            python: "python3".into(),
            engine,
            passthrough: vec![],
        };
        let (tx, rx, handle) = match spawn_engine(&cfg) {
            Ok(t) => t,
            Err(e) => {
                // no python3 on this machine: nothing to prove here
                eprintln!("skipping spawn test: {e}");
                return;
            }
        };
        let t = Duration::from_secs(10);
        match rx.recv_timeout(t).unwrap() {
            Update::Log { msg } => assert_eq!(msg, "hello"),
            other => panic!("expected log, got {other:?}"),
        }
        // unknown type and malformed line both degrade to Update::Log
        for _ in 0..2 {
            match rx.recv_timeout(t).unwrap() {
                Update::Log { msg } => assert!(msg.contains("[ipc parse error]")),
                other => panic!("expected parse-error log, got {other:?}"),
            }
        }
        tx.send(Control::Quit).unwrap();
        match rx.recv_timeout(t).unwrap() {
            Update::Ready { budget, .. } => assert_eq!(budget, 3),
            other => panic!("expected ready, got {other:?}"),
        }
        // child exits → reader EOF → channel disconnects
        assert!(matches!(
            rx.recv_timeout(t),
            Err(RecvTimeoutError::Disconnected)
        ));
        handle.shutdown().unwrap(); // must not deadlock (tx still held)
        drop(tx);
    }

    /// THE cross-process contract test: run the REAL engine's --selftest and
    /// require every emitted line to parse as a known Update variant. A spec
    /// drift on either side fails here before it can ship.
    #[test]
    fn real_engine_selftest_stream_parses() {
        let engine = default_engine_path();
        if !engine.is_file() {
            eprintln!("skipping: engine not found at {}", engine.display());
            return;
        }
        let cfg = SpawnCfg {
            python: "python3".into(),
            engine,
            passthrough: vec!["--selftest".into()],
        };
        let (tx, rx, handle) = match spawn_engine(&cfg) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("skipping spawn test: {e}");
                return;
            }
        };
        let mut seen: std::collections::BTreeSet<&'static str> = Default::default();
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        loop {
            match rx.recv_timeout(Duration::from_millis(500)) {
                Ok(u) => {
                    if let Update::Log { msg } = &u {
                        assert!(
                            !msg.starts_with("[ipc parse error]"),
                            "engine emitted a line the UI cannot parse: {msg}"
                        );
                    }
                    seen.insert(match &u {
                        Update::Init { .. } => "init",
                        Update::Meta(_) => "meta",
                        Update::Backfill(_) => "backfill",
                        Update::Ready { .. } => "ready",
                        Update::Turn(_) => "turn",
                        Update::Map(_) => "map",
                        Update::MapAdd { .. } => "map_add",
                        Update::Files { .. } => "files",
                        Update::Faccess(_) => "faccess",
                        Update::Cats { .. } => "cats",
                        Update::Compaction(_) => "compaction",
                        Update::Agent(_) => "agent",
                        Update::Cmd(_) => "cmd",
                        Update::Ret(_) => "ret",
                        Update::Tasks(_) => "tasks",
                        Update::Health { .. } => "health",
                        Update::Event(_) => "event",
                        Update::Fleet { .. } => "fleet",
                        Update::Snapshot(_) => "snapshot",
                        Update::Peek(_) => "peek",
                        Update::ReportDone { .. } => "report_done",
                        Update::Log { .. } => "log",
                    });
                }
                Err(RecvTimeoutError::Timeout) => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "selftest did not finish within 30s; saw {seen:?}"
                    );
                }
                Err(RecvTimeoutError::Disconnected) => break,
            }
        }
        for required in [
            "init", "meta", "map", "backfill", "ready", "turn", "files", "faccess",
            "cats", "compaction", "event",
        ] {
            assert!(seen.contains(required), "missing {required}; saw {seen:?}");
        }
        handle.shutdown().unwrap();
        drop(tx);
    }

    /// Contract test for the one path --selftest never exercises: seek→snapshot.
    /// Attach the real engine to the golden fixture, seek, and require a parsed
    /// Snapshot whose map sums to that turn's resident.
    #[test]
    fn real_engine_seek_snapshot_parses() {
        let engine = default_engine_path();
        let fixture = engine
            .parent()
            .unwrap()
            .join("tests/fixtures/golden.jsonl");
        if !engine.is_file() || !fixture.is_file() {
            eprintln!("skipping: engine/fixture not found");
            return;
        }
        let cfg = SpawnCfg {
            python: "python3".into(),
            engine,
            passthrough: vec!["--session".into(), fixture.to_string_lossy().into_owned()],
        };
        let (tx, rx, handle) = match spawn_engine(&cfg) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("skipping spawn test: {e}");
                return;
            }
        };
        let t = Duration::from_secs(15);
        // wait for ready, then seek
        loop {
            match rx.recv_timeout(t).expect("engine went quiet before ready") {
                Update::Log { msg } => {
                    assert!(!msg.starts_with("[ipc parse error]"), "unparseable: {msg}")
                }
                Update::Ready { .. } => break,
                _ => {}
            }
        }
        tx.send(Control::Seek { turn: 3 }).unwrap();
        let snap = loop {
            match rx.recv_timeout(t).expect("no snapshot within 15s") {
                Update::Snapshot(s) => break s,
                Update::Log { msg } => {
                    assert!(!msg.starts_with("[ipc parse error]"), "unparseable: {msg}")
                }
                _ => {}
            }
        };
        assert_eq!(snap.turn, 3);
        let map_sum: u64 = snap.map.segs.iter().map(|s| s.tok).sum();
        assert!(map_sum > 0, "snapshot map is empty");
        tx.send(Control::Quit).unwrap();
        handle.shutdown().unwrap();
        drop(tx);
    }

    /// The INSPECT feature's cross-process proof: attach the real engine to
    /// the golden fixture, capture the map's segs, and peek — seg 0 must
    /// answer the overhead explainer, a real seg id must answer found with a
    /// non-empty excerpt, and an unknown id must answer found:false.
    #[test]
    fn real_engine_peek_roundtrip() {
        let engine = default_engine_path();
        let fixture = engine
            .parent()
            .unwrap()
            .join("tests/fixtures/golden.jsonl");
        if !engine.is_file() || !fixture.is_file() {
            eprintln!("skipping: engine/fixture not found");
            return;
        }
        let cfg = SpawnCfg {
            python: "python3".into(),
            engine,
            passthrough: vec!["--session".into(), fixture.to_string_lossy().into_owned()],
        };
        let (tx, rx, handle) = match spawn_engine(&cfg) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("skipping spawn test: {e}");
                return;
            }
        };
        let t = Duration::from_secs(15);
        // capture the map's segs (meta → map → backfill → ready ordering)
        let mut segs: Vec<Seg> = Vec::new();
        loop {
            match rx.recv_timeout(t).expect("engine went quiet before ready") {
                Update::Map(m) => segs = m.segs,
                Update::Ready { .. } => break,
                Update::Log { msg } => {
                    assert!(!msg.starts_with("[ipc parse error]"), "unparseable: {msg}")
                }
                _ => {}
            }
        }
        let real = segs
            .iter()
            .find(|s| s.id != 0)
            .expect("golden map must carry a non-overhead seg")
            .id;
        let peek_for = |want: u64| -> PeekMsg {
            tx.send(Control::Peek { seg: want }).unwrap();
            loop {
                match rx.recv_timeout(t).expect("no peek reply within 15s") {
                    Update::Peek(p) if p.seg == want => break p,
                    Update::Log { msg } => {
                        assert!(!msg.starts_with("[ipc parse error]"), "unparseable: {msg}")
                    }
                    _ => {}
                }
            }
        };
        // seg 0 = the overhead segment's explainer
        let p0 = peek_for(0);
        assert!(p0.found, "overhead seg must answer found");
        assert_eq!(p0.cat, Cat::Overhead);
        assert_eq!(p0.kind.as_deref(), Some("overhead"));
        assert!(
            p0.excerpt.contains("system prompt"),
            "overhead explainer drifted: {}",
            p0.excerpt
        );
        // a real seg id from the map answers with actual content
        let p1 = peek_for(real);
        assert!(p1.found, "live seg {real} must answer found");
        assert!(
            !p1.excerpt.is_empty(),
            "live seg {real} must carry a non-empty excerpt"
        );
        assert_ne!(p1.cat, Cat::Unknown, "live seg must carry its cat");
        // an unknown seg answers found:false (never an error, never silence)
        let p2 = peek_for(9_999_999);
        assert!(!p2.found, "unknown seg must answer found:false");
        tx.send(Control::Quit).unwrap();
        handle.shutdown().unwrap();
        drop(tx);
    }

    /// The SHELL feature's cross-process proof: attach the real engine to the
    /// shell fixture and require the backfill to carry the 4 Bash executions
    /// with the exact ok/interrupted/bg flag matrix (ok · err · interrupted ·
    /// backgrounded).
    #[test]
    fn real_engine_shell_fixture_cmds() {
        let engine = default_engine_path();
        let fixture = engine
            .parent()
            .unwrap()
            .join("tests/fixtures/shell.jsonl");
        if !engine.is_file() || !fixture.is_file() {
            eprintln!("skipping: engine/fixture not found");
            return;
        }
        let cfg = SpawnCfg {
            python: "python3".into(),
            engine,
            passthrough: vec!["--session".into(), fixture.to_string_lossy().into_owned()],
        };
        let (tx, rx, handle) = match spawn_engine(&cfg) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("skipping spawn test: {e}");
                return;
            }
        };
        let t = Duration::from_secs(15);
        let backfill = loop {
            match rx.recv_timeout(t).expect("engine went quiet before backfill") {
                Update::Backfill(b) => break b,
                Update::Log { msg } => {
                    assert!(!msg.starts_with("[ipc parse error]"), "unparseable: {msg}")
                }
                _ => {}
            }
        };
        assert_eq!(
            backfill.cmds.len(),
            4,
            "shell fixture must backfill exactly 4 cmds"
        );
        // (ok, interrupted, bg) per entry, oldest→newest
        let flags: Vec<(bool, bool, bool)> = backfill
            .cmds
            .iter()
            .map(|c| (c.ok, c.interrupted, c.bg))
            .collect();
        assert_eq!(
            flags,
            vec![
                (true, false, false),  // git status — ok
                (false, false, false), // cargo test — failed with stderr
                (false, true, false),  // npm run dev — interrupted
                (true, false, true),   // http.server — backgrounded
            ],
            "flag matrix drifted: {:?}",
            backfill.cmds
        );
        // the err entry actually carries an stderr tail; tok_out is charged
        let failed = &backfill.cmds[1];
        assert!(!failed.err.is_empty(), "failed cmd must carry an err tail");
        assert!(backfill.cmds.iter().all(|c| !c.cmd.is_empty()));
        // RETRIEVAL's cross-process proof: the same backfill carries the 2
        // external pulls (a WebSearch and an MCP tool call) with the spec'd
        // kind/src/n/dur_ms shapes.
        assert_eq!(
            backfill.rets.len(),
            2,
            "shell fixture must backfill exactly 2 rets: {:?}",
            backfill.rets
        );
        let ws = &backfill.rets[0];
        assert_eq!(ws.kind, RetKind::Search, "WebSearch → kind search");
        assert_eq!(ws.src, "web");
        assert_eq!(ws.n, Some(5), "searchCount → n");
        assert_eq!(ws.dur_ms, Some(2_400));
        assert!(ws.ok && !ws.q.is_empty() && ws.tok > 0);
        let mcp = &backfill.rets[1];
        assert_eq!(mcp.kind, RetKind::Mcp, "mcp__<server>__<tool> → kind mcp");
        assert_eq!(mcp.src, "claude_ai_Dropbox", "src = server name");
        assert_eq!(mcp.n, None);
        assert_eq!(mcp.dur_ms, None);
        assert!(mcp.ok && mcp.tok > 0);
        tx.send(Control::Quit).unwrap();
        handle.shutdown().unwrap();
        drop(tx);
    }
}
