//! Deterministic demo scene state — the reproducible source for `--demo`
//! (the visual/animation testbench) and every `#[cfg(test)]` render check.
//! `populate` builds a rich session (all categories on the MAP, 40 turns, a
//! compaction, a 12-child workflow, retrievals, a shell log) anchored at
//! `now`; pass real wall-time and the animation clocks play against live
//! recency, pass a fixed epoch and the state is byte-stable for snapshots.

use std::collections::HashMap;

use crate::App;
use crate::ipc::{
    AgentRec, CmdRec, Compaction, DroppedFile, EventRec, Faccess, FileRec, Health, MapMsg,
    Meta, Op, RetKind, RetRec, Seg, Sess, Severity, Tasks, ToolCounts, Turn, Update,
};

fn cat(s: &str) -> crate::ipc::Cat {
    serde_json::from_value(serde_json::Value::String(s.to_string())).unwrap()
}

fn rkind(s: &str) -> RetKind {
    serde_json::from_value(serde_json::Value::String(s.to_string())).unwrap()
}

/// Populate `app` with the demo scene, timestamps anchored at `now`.
pub(crate) fn populate(app: &mut App, now: f64) {
    let seg = |id: u64, c: &str, tok: u64, file: Option<u64>, born: u64, age_s: f64| -> Seg {
        Seg { id, cat: cat(c), tok, file, born, ts: now - age_s }
    };
    // per-agent context map (SPEC b `agent.map`): the stripped {cat,tok,file}
    // seg shape (id/born/ts default), resident summed from the composition so
    // the mini-map renders richly in `--demo` for screenshots/tests.
    let amap = |budget: u64, comp: &[(&str, u64, Option<u64>)]| -> MapMsg {
        let segs: Vec<Seg> = comp
            .iter()
            .map(|&(c, tok, file)| Seg { id: 0, cat: cat(c), tok, file, born: 0, ts: now })
            .collect();
        let resident: u64 = comp.iter().map(|&(_, tok, _)| tok).sum();
        MapMsg { rev: 0, alpha: 1.0, segs, resident: Some(resident), budget: Some(budget) }
    };
    let fa = |turn: u64, file: u64, op: Op, tok: u64| -> Faccess {
        Faccess { turn, ts: format!("16:{:02}:00", turn % 60), file, op, tok }
    };
    let cmdrec = |turn: u64, ts: &str, cmd: &str, desc: Option<&str>, out: &str, err: &str,
                  ok: bool, interrupted: bool, bg: bool, tok_out: u64| -> CmdRec {
        CmdRec {
            turn, ts: ts.into(), epoch: now - 300.0 + turn as f64, cmd: cmd.into(),
            desc: desc.map(str::to_string), out: out.into(), err: err.into(),
            ok, interrupted, bg, tok_out, seq: 0,
        }
    };
    let retrec = |turn: u64, ts: &str, kind: &str, src: &str, q: &str, n: Option<u64>,
                  bytes: Option<u64>, dur_ms: Option<u64>, tok: u64, ok: bool| -> RetRec {
        RetRec {
            turn, ts: ts.into(), epoch: now - 280.0 + turn as f64, kind: rkind(kind),
            src: src.into(), q: q.into(), n, bytes, dur_ms, tok, ok, seq: 0,
        }
    };

    let st = &mut app.st;
        st.apply(Update::Meta(Meta {
            session_id: "demo-4d2f".into(),
            attach_gen: 1,
            path: "~/.claude/projects/-Users-dev-code-webapp/demo-4d2f.jsonl".into(),
            project: "/Users/dev/code/webapp".into(),
            name: "brisk-otter".into(),
            title: Some("amtr-demo".into()),
            model: "claude-fable-5".into(),
            budget: 200_000,
            t_auto: 0.85,
            cc_version: Some("2.1.212".into()),
            started_at: None,
        }));
        st.ready = true;
        st.health = Some(Health {
            status: "busy".into(),
            last_activity_ts: now - 4.0,
            api_errors: 1,
            retry_in_ms: None,
            stalled: false,
        });
        st.tasks = Some(Tasks {
            total: 7,
            done: 3,
            in_progress: 1,
            active: Some("wire the EKG lanes".into()),
        });

        // 40 turns: fill to ~150k, compaction at t20 → 42k, refill to 121k.
        for i in 0u64..40 {
            let resident = if i < 20 {
                42_000 + i * 5_500
            } else if i == 20 {
                42_000
            } else {
                42_000 + (i - 20) * 4_158
            };
            let cc = if i % 7 == 3 { 9_000 } else { 1_400 };
            let in_tok = 1_500 + (i % 5) * 600;
            let wl = resident.saturating_sub(cc + in_tok + 2_000);
            // one thrash: the waterline falls hard at t30
            let wl = if i == 30 { wl.saturating_sub(30_000) } else { wl };
            st.apply(Update::Turn(Turn {
                turn: i,
                ts: format!("16:{:02}:{:02}", 10 + i / 2, (i * 13) % 60),
                // model switch at t35 (TURNS rail ◆ + ledger annotation)
                model: if i >= 35 {
                    "claude-opus-4".into()
                } else {
                    "claude-fable-5".into()
                },
                in_tok,
                cr: wl,
                cc,
                cc_5m: cc * 3 / 4,
                cc_1h: cc / 4,
                out: 200 + (i % 9) * 700,
                resident,
                waterline: wl,
                dur_ms: Some(2_500 + (i % 11) * 4_000),
                stop: Some(if i % 4 == 0 { "end_turn" } else { "tool_use" }.into()),
                tools: i % 6,
                cost_u: 2.0 + (i % 10) as f64 * 3.1,
                hit: 0.92 + (i % 5) as f64 * 0.01,
            }));
        }

        // 6 files — last_epoch relative to now drives the FILES now zones:
        // hot f1(3s) f4(20s) f2(50s) f5(100s, evicted → ✝ while hot);
        // cold f3(400s → 7m) and f6(epoch 0 → `—` unknown tail)
        let files = [
            (1u64, "src/main.rs", 12_000u64, 3u64, 1u64, 4u64, 9_000u64, true, 3.0f64),
            (2, "src/viz.rs", 13_000, 2, 1, 2, 4_200, true, 50.0),
            (3, "SPEC.md", 5_000, 2, 0, 0, 0, true, 400.0),
            (4, "amtr_engine.py", 24_000, 4, 2, 1, 22_000, true, 20.0),
            (5, "README.md", 1_200, 1, 0, 0, 1_200, false, 100.0), // evicted ✝
            (6, "tests/fixtures/golden.jsonl", 3_000, 1, 1, 0, 800, true, 0.0),
        ];
        st.apply(Update::Files {
            upserts: files
                .iter()
                .map(|&(id, path, tok, r, w, e, waste, resident, age)| FileRec {
                    id,
                    path: path.into(),
                    tok,
                    reads: r,
                    writes: w,
                    edits: e,
                    waste,
                    last_ts: format!("16:{:02}:11", 10 + id),
                    last_epoch: if age > 0.0 { now - age } else { 0.0 },
                    resident,
                })
                .collect(),
        });

        // faccess scatter across the turn axis (all intensity buckets)
        for (t, fid, op, tok) in [
            (2u64, 1u64, Op::R, 800u64),
            (3, 3, Op::R, 5_000),
            (4, 1, Op::E, 2_400),
            (5, 4, Op::R, 24_000),
            (6, 2, Op::R, 3_200),
            (8, 1, Op::E, 1_900),
            (9, 4, Op::W, 12_000),
            (10, 5, Op::R, 1_200),
            (12, 2, Op::E, 2_100),
            (14, 4, Op::R, 18_000),
            (16, 1, Op::W, 6_000),
            (18, 3, Op::R, 4_000),
            (21, 6, Op::R, 3_000),
            (23, 2, Op::W, 5_500),
            (25, 4, Op::E, 900),
            (27, 1, Op::R, 12_000),
            (29, 6, Op::W, 700),
            (31, 2, Op::R, 3_300),
            (33, 4, Op::R, 21_000),
            (35, 1, Op::E, 2_800),
            (36, 1, Op::R, 1_000), // turn 36: 2 reads + 1 write → `fa 2r/1w`
            (36, 3, Op::R, 2_000),
            (36, 4, Op::W, 3_000),
            (37, 3, Op::R, 4_100),
            (39, 2, Op::E, 6_500),
            (39, 1, Op::R, 950),
        ] {
            st.apply(Update::Faccess(fa(t, fid, op, tok)));
        }

        // MAP: every cat present; sums to 121_000
        let segs = vec![
            seg(0, "overhead", 18_000, None, 0, 600.0),
            seg(1, "user", 2_500, None, 21, 500.0),
            seg(2, "attach", 1_800, None, 21, 480.0),
            seg(3, "assistant", 5_200, None, 22, 460.0),
            seg(4, "thinking", 2_600, None, 22, 450.0),
            seg(5, "file", 6_000, Some(1), 23, 400.0),
            seg(6, "bash", 3_500, None, 24, 380.0),
            seg(7, "file", 9_000, Some(2), 25, 340.0),
            seg(8, "tool", 2_200, None, 25, 320.0),
            seg(9, "file", 16_000, Some(4), 26, 300.0),
            seg(10, "assistant", 4_800, None, 27, 280.0),
            seg(11, "file", 5_000, Some(3), 28, 240.0),
            seg(12, "user", 1_500, None, 29, 220.0),
            seg(13, "file", 3_000, Some(6), 30, 200.0),
            seg(14, "thinking", 2_000, None, 31, 170.0),
            seg(15, "file", 8_000, Some(4), 32, 140.0),
            seg(16, "assistant", 5_000, None, 33, 120.0),
            seg(17, "summary", 4_200, None, 21, 110.0),
            seg(18, "file", 4_000, Some(2), 34, 90.0),
            seg(19, "user", 1_200, None, 35, 70.0),
            seg(20, "assistant", 3_800, None, 36, 55.0),
            seg(21, "bash", 2_100, None, 37, 40.0),
            seg(22, "file", 4_000, Some(1), 38, 20.0),
            seg(23, "attach", 1_000, None, 39, 8.0),
            seg(24, "assistant", 4_600, None, 39, 2.0),
        ];
        st.apply(Update::Map(MapMsg {
            rev: 2,
            alpha: 0.97,
            segs,
            resident: None,
            budget: None,
        }));
        let cats: HashMap<String, u64> = [
            ("overhead", 18_000u64),
            ("user", 5_200),
            ("assistant", 23_400),
            ("thinking", 4_600),
            ("file", 55_000),
            ("bash", 5_600),
            ("tool", 2_200),
            ("attach", 2_800),
            ("summary", 4_200),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
        st.apply(Update::Cats { totals: cats });

        // one compaction at turn 20
        st.apply(Update::Compaction(Compaction {
            n: 1,
            turn: 20,
            ts: "16:20:00".into(),
            trigger: "auto".into(),
            pre: 152_000,
            post: 42_000,
            dropped: 110_000,
            cum_dropped: 110_000,
            dur_ms: 88_000,
            dropped_cats: [
                ("file", 60_000u64),
                ("assistant", 28_000),
                ("thinking", 9_000),
                ("user", 6_000),
                ("bash", 7_000),
            ]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
            dropped_files: vec![
                DroppedFile { file: 4, tok: 30_000 },
                DroppedFile { file: 1, tok: 15_000 },
                DroppedFile { file: 2, tok: 9_000 },
            ],
            preserved_msgs: 12,
        }));
        st.compact_sweep = 0; // keep shots stable

        // agents: three done solos (incl. the 302k deep-review) and a
        // 12-child workflow with 2 running + 1 failed (AGENTS design fixture)
        st.apply(Update::Agent(AgentRec {
            id: "agent-a1".into(),
            state: "done".into(),
            agent_type: Some("explore".into()),
            desc: Some("survey transcript schema".into()),
            wf: None,
            path: Some("/tmp/demo/subagents/agent-a1.jsonl".into()),
            turn0: 5,
            ts0: "16:12:30".into(),
            turn1: Some(12),
            own_tok: 220_000,
            ret_tok: Some(3_200),
            tools: Some(ToolCounts { r: 14, s: 6, b: 3, e: 0 }),
            dur_ms: Some(480_000),
            t0: now - 2_000.0,
            ts_last: now - 1_520.0,
            // explore: file-heavy survey, moderate fill
            map: Some(amap(
                200_000,
                &[
                    ("overhead", 14_000, None),
                    ("user", 3_000, None),
                    ("file", 40_000, Some(0)),
                    ("assistant", 30_000, None),
                    ("thinking", 8_000, None),
                    ("file", 20_000, Some(1)),
                    ("bash", 5_000, None),
                ],
            )),
        }));
        st.apply(Update::Agent(AgentRec {
            id: "agent-b2".into(),
            state: "done".into(),
            agent_type: Some("builder".into()),
            desc: Some("write the viz module".into()),
            wf: None,
            path: None,
            turn0: 30,
            ts0: "16:25:00".into(),
            turn1: Some(33),
            own_tok: 51_000,
            ret_tok: Some(2_400),
            tools: Some(ToolCounts { r: 4, s: 1, b: 2, e: 5 }),
            dur_ms: Some(90_000),
            t0: now - 800.0,
            ts_last: now - 710.0,
            // builder: assistant/tool weighted, lighter fill
            map: Some(amap(
                200_000,
                &[
                    ("overhead", 12_000, None),
                    ("assistant", 20_000, None),
                    ("file", 15_000, Some(0)),
                    ("tool", 8_000, None),
                    ("user", 5_000, None),
                ],
            )),
        }));
        st.apply(Update::Agent(AgentRec {
            id: "agent-cr".into(),
            state: "done".into(),
            agent_type: Some("code-review".into()),
            desc: Some("deep review viz.rs".into()),
            wf: None,
            path: None,
            turn0: 13,
            ts0: "16:16:00".into(),
            turn1: Some(19),
            own_tok: 302_000,
            ret_tok: Some(9_800),
            tools: Some(ToolCounts { r: 55, s: 0, b: 12, e: 4 }),
            dur_ms: Some(400_000),
            t0: now - 1_500.0,
            ts_last: now - 1_100.0,
            // code-review: massive multi-file read, nearly full context
            map: Some(amap(
                200_000,
                &[
                    ("overhead", 18_000, None),
                    ("file", 90_000, Some(0)),
                    ("file", 40_000, Some(1)),
                    ("assistant", 22_000, None),
                    ("thinking", 10_000, None),
                ],
            )),
        }));
        // wf_refactor ×12: k01..k09 done, k10 failed, k11 running (heated),
        // k12 running with t0/ts_last unknown (old-engine degrade row)
        for i in 1u64..=12 {
            let turn0 = 26 + i;
            let (state, turn1, own, ret, dur, t0, ts_last): (
                &str,
                Option<u64>,
                u64,
                Option<u64>,
                Option<u64>,
                f64,
                f64,
            ) = match i {
                10 => ("failed", Some(37), 12_300, Some(400), Some(31_000), now - 260.0, now - 230.0),
                11 => ("running", None, 41_000, None, None, now - 134.0, now - 30.0),
                12 => ("running", None, 35_000, None, None, 0.0, 0.0),
                _ => (
                    "done",
                    Some(turn0 + 1),
                    14_000,
                    Some(1_200),
                    Some(48_000 + i * 1_000),
                    now - 600.0 - i as f64,
                    now - 560.0 - i as f64,
                ),
            };
            // maps vary per child; k10 (failed) and k12 (old-engine, t0=0) are
            // left mapless to exercise the "no map" placeholder cell
            let map = if i == 10 || i == 12 {
                None
            } else {
                Some(amap(
                    200_000,
                    &[
                        ("overhead", 7_000, None),
                        ("file", 3_000 + i * 900, Some(i % 4)),
                        ("assistant", 2_500 + i * 400, None),
                        ("thinking", 800 + i * 150, None),
                        ("bash", 1_200, None),
                    ],
                ))
            };
            st.apply(Update::Agent(AgentRec {
                id: format!("agent-k{i:02}"),
                state: state.into(),
                agent_type: Some(
                    ["explore", "fix", "test-writer", "reviewer"][(i % 4) as usize].into(),
                ),
                desc: Some(format!("refactor step {i}")),
                wf: Some("wf_refactor".into()),
                path: None,
                turn0,
                ts0: format!("16:{:02}:30", 20 + i),
                turn1,
                own_tok: own,
                ret_tok: ret,
                tools: Some(ToolCounts { r: 2 + i, s: 1, b: 1, e: 0 }),
                dur_ms: dur,
                t0,
                ts_last,
                map,
            }));
        }

        // 3 events; the api_error lands last → alert present
        st.apply(Update::Event(EventRec {
            kind: "compaction".into(),
            severity: Severity::Info,
            ts: "16:20:00".into(),
            turn: 20,
            msg: "auto · 152k → 42k (−110k)".into(),
        }));
        st.apply(Update::Event(EventRec {
            kind: "thrash".into(),
            severity: Severity::Warn,
            ts: "16:25:39".into(),
            turn: 30,
            msg: "3 turns with cc/R > 0.2".into(),
        }));
        st.apply(Update::Event(EventRec {
            kind: "api_error".into(),
            severity: Severity::Warn,
            ts: "16:27:11".into(),
            turn: 27,
            msg: "429 rate_limited (retry 3/10 in 8s)".into(),
        }));
        // 6 cmds for the SHELL console: ok-small, ok+desc, err+stderr,
        // interrupted, bg, ok-18.2k (the design fixture; turns straddle
        // t=20 so the replay filter is provable)
        for (turn, ts, cmd, desc, out, err, ok, intr, bg, tok) in [
            (
                18u64,
                "16:47:02",
                "ls -la rust/src",
                None,
                "main.rs  state.rs  viz.rs  ipc.rs",
                "",
                true,
                false,
                false,
                214u64,
            ),
            (
                22,
                "16:47:18",
                "cargo build 2>&1 | tail -20",
                Some("build the workspace"),
                "Compiling amtr v0.1.3\nFinished dev [unoptimized] in 4.82s",
                "",
                true,
                false,
                false,
                2_100,
            ),
            (
                25,
                "16:47:41",
                "cargo test --workspace --quiet",
                None,
                "test result: FAILED. 41 passed; 1 failed",
                "assertion failed: `(left == right)`, viz.rs:1892",
                false,
                false,
                false,
                6_400,
            ),
            (27, "16:48:11", "npm run dev", None, "", "", false, true, false, 96),
            (29, "16:48:20", "python3 -m http.server 8000", None, "", "", true, false, true, 96),
            (
                38,
                "16:48:51",
                "python3 amtr_engine.py --selftest",
                None,
                "…\"} {\"type\":\"ready\",\"turns\":6}",
                "",
                true,
                false,
                false,
                18_200,
            ),
        ] {
            st.apply(Update::Cmd(cmdrec(turn, ts, cmd, desc, out, err, ok, intr, bg, tok)));
        }
        // 5 rets for the RETRIEVAL perspective: search ok, fetch with
        // bytes+dur, mcp Dropbox at 17.9k (the ramp's red-bold end),
        // toolsearch, one failed ✖ — turns straddle t=20 so the replay
        // filter is provable (same design as the cmds)
        for (turn, ts, kind, src, q, n, bytes, dur, tok, ok) in [
            (
                15u64,
                "16:46:12",
                "search",
                "web",
                "ratatui braille canvas api",
                Some(5u64),
                None,
                Some(2_400u64),
                900u64,
                true,
            ),
            (
                21,
                "16:47:05",
                "fetch",
                "docs.rs",
                "https://docs.rs/ratatui/latest/ratatui/widgets/canvas",
                None,
                Some(48_200),
                Some(1_850),
                2_600,
                true,
            ),
            (
                24,
                "16:47:33",
                "mcp",
                "claude_ai_Dropbox",
                "search quest control panel",
                Some(12),
                None,
                Some(3_100),
                17_900,
                true,
            ),
            (
                30,
                "16:48:02",
                "toolsearch",
                "tools",
                "select:WebFetch,WebSearch",
                Some(3),
                None,
                None,
                350,
                true,
            ),
            (
                36,
                "16:48:40",
                "fetch",
                "example.com",
                "https://example.com/big-page",
                None,
                None,
                Some(900),
                40,
                false,
            ),
        ] {
            st.apply(Update::Ret(retrec(turn, ts, kind, src, q, n, bytes, dur, tok, ok)));
        }

        st.write_pulse = 0;
        st.thrash_pulse = 0;
        st.turn_pulse = 0;
        st.touch_pulse = 0;
        st.cmd_pulse = 0;
        st.ret_pulse = 0;
        st.agent_pulse.clear();

        // fleet roster for the picker
        st.fleet = vec![
            Sess {
                id: "demo-4d2f".into(),
                path: "/tmp/demo.jsonl".into(),
                pid: Some(4242),
                name: Some("brisk-otter".into()),
                project: "/Users/dev/code/webapp".into(),
                status: "busy".into(),
                mtime: now - 3.0,
                live: true,
                resident: Some(121_000),
                budget: Some(200_000),
                last_prompt: Some("wire the EKG lanes".into()),
            },
            Sess {
                id: "aaaa-1111".into(),
                path: "/tmp/a.jsonl".into(),
                pid: Some(999),
                name: Some("ml-pipeline".into()),
                project: "/Users/dev/code/ml-pipeline".into(),
                status: "idle".into(),
                mtime: now - 700.0,
                live: true,
                resident: Some(64_000),
                budget: Some(200_000),
                last_prompt: Some("debug the training loop".into()),
            },
            Sess {
                id: "bbbb-2222".into(),
                path: "/tmp/b.jsonl".into(),
                pid: None,
                name: Some("notes-site".into()),
                project: "/Users/dev/code/notes-site".into(),
                status: "offline".into(),
                mtime: now - 90_000.0,
                live: false,
                resident: None,
                budget: None,
                last_prompt: Some("tune the gantt".into()),
            },
        ];
}
