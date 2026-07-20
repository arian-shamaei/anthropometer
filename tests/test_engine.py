#!/usr/bin/env python3
"""amtr v2 engine tests (SPEC.md section d) — real numeric assertions against
tests/fixtures/golden.jsonl.

Every expected constant below is derived by hand from the fixture:
estimator est(s) = ceil(len(s)/3.8); dict inputs are estimated on their
json.dumps(ensure_ascii=False) text (default separators); images are 1200 flat.

Fixture content lengths -> ests (chars -> tok):
  u1 prompt                  72 -> 19      a1 thinking      63 -> 17
  a1 text                    46 -> 12      u2 prompt        47 -> 13
  a2 text                    30 ->  8      Read input json  48 -> 13
  read#1 file.content      1520 -> 400     Bash input json  59 -> 16
  bash stdout                17 ->  5      attachment json  46 -> 13
  system-reminder text       84 -> 22      a4 text          30 ->  8
  Edit input json           110 -> 29      edit ack         59 -> 16
  u7 text                    23 ->  7      image          1200 flat
  read#2 file.content      1604 -> 423     Agent input json 127 -> 34
  agent ret block json       85 -> 23      a7 text          14 ->  4
  Write input json          112 -> 30      write ack        58 -> 15
  compact summary           122 -> 33      a8 text          28 ->  8
  a8b text                   15 ->  4

Hidden reasoning (SPEC a): when turn t+1 OPENS, turn t is charged one
synthetic "reasoning" segment hid(t) = max(0, out(t) - visible assistant
est of t), uuid "reasoning-t<t>", born t, ts = turn t's open timestamp:
  t0: out 120 - (think 17 + text 12)   = 91
  t1: out  90 - (text 8 + Read in 13)  = 69
  t2: out  60 - (Bash in 16)           = 44
  t3: out  75 - (text 8 + Edit in 29)  = 38
  t4: out  88 - (Read in 13)           = 75
  t5: out  95 - (Agent in 34)          = 61
  t6: out  70 - (text 4 + Write in 30) = 36   (allocated post-boundary,
      when turn 7 opens, so it SURVIVES the compaction)
  t7: out 150 - (a8 text 8 + a8b 4 = 12): never allocated — no turn 8
      ever opens (last turn of the session).
The compaction evicts reasoning-t0..t5 (their synthetic uuids are never in
preservedMessages): 91+69+44+38+75+61 = 378 dropped as cat "reasoning".
"""
import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import amtr_engine as ce  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "golden.jsonl")
PY = sys.executable or "python3"

# authoritative per-turn ledger, straight from the fixture usage objects
EXP_R = [9004, 9356, 10290, 10960, 14000, 14300, 15200, 2960]
EXP_C = [0, 9000, 9350, 9870, 10350, 12500, 14260, 0]


def load_session(path=FIX, ckpt_every=200, stop_at_turn=None):
    """Linear replay. stop_at_turn=t => state at the END of 0-based turn t
    (records up to, excluding, the record that opens turn t+1) — the same
    definition Session.state_at_turn implements via checkpoints."""
    sess = ce.Session(path, budget=200_000, budget_pinned=True,
                      ckpt_every=ckpt_every)
    off = 0
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8")
            if stop_at_turn is not None:
                try:
                    d = json.loads(line)
                except ValueError:
                    d = None
                if isinstance(d, dict) and sess.is_new_turn(d) \
                        and len(sess.turns) >= stop_at_turn + 1:
                    break
            sess.feed_line(line, off)
            off += len(raw)
    return sess


class TestTurnLedger(unittest.TestCase):
    def setUp(self):
        self.s = load_session()

    def test_resident_and_waterline(self):
        # R = in + cache_read + cache_creation of each turn's LAST usage
        self.assertEqual(len(self.s.turns), 8)
        self.assertEqual([t["resident"] for t in self.s.turns], EXP_R)
        self.assertEqual([t["waterline"] for t in self.s.turns], EXP_C)
        self.assertEqual(self.s.resident(), 2960)

    def test_streamed_requestid_last_usage_wins(self):
        # a-a8 and a-a8b share req_008: one turn, a8b's usage wins
        t7 = self.s.turns[7]
        self.assertEqual((t7["in"], t7["cr"], t7["cc"], t7["out"]),
                         (10, 0, 2950, 150))
        self.assertEqual(t7["stop"], "end_turn")
        self.assertEqual(t7["dur_ms"], 5100)   # from the trailing turn_duration

    def test_cache_creation_split_and_fallback(self):
        t2 = self.s.turns[2]     # no nested cache_creation: all -> cc_5m
        self.assertEqual((t2["cc"], t2["cc_5m"], t2["cc_1h"]), (520, 520, 0))
        t4 = self.s.turns[4]     # nested 5m/1h split honoured
        self.assertEqual((t4["cc"], t4["cc_5m"], t4["cc_1h"]), (2150, 400, 1750))

    def test_cost_u_and_hit(self):
        # cost_u = (in + 0.1*cr + 1.25*cc_5m + 2*cc_1h + 5*out)/1000, 1 decimal
        p0 = self.s.turn_payload(0)   # (4 + 0 + 11250 + 0 + 600)/1000 = 11.854
        self.assertEqual(p0["cost_u"], 11.9)
        p4 = self.s.turn_payload(4)   # (1500+1035+500+3500+440)/1000 = 6.975
        self.assertEqual(p4["cost_u"], 7.0)
        p7 = self.s.turn_payload(7)   # (10+0+3687.5+0+750)/1000 = 4.4475
        self.assertEqual(p7["cost_u"], 4.4)
        # hit = cr / (cr + cc + in)
        self.assertEqual(self.s.turn_payload(1)["hit"], 0.9619)  # 9000/9356
        self.assertEqual(self.s.turn_payload(2)["hit"], 0.9086)  # 9350/10290
        self.assertEqual(self.s.turn_payload(4)["hit"], 0.7393)  # 10350/14000
        self.assertEqual(self.s.turn_payload(7)["hit"], 0.0)

    def test_tools_and_dur(self):
        self.assertEqual([t["tools"] for t in self.s.turns],
                         [0, 1, 1, 1, 1, 1, 1, 0])
        self.assertEqual(self.s.turns[0]["dur_ms"], 3200)


class TestOverheadAlpha(unittest.TestCase):
    def test_overhead0_first_turn(self):
        # measured at t0, BEFORE a1's own content allocates:
        # overhead0 = R0 - est(u1) = 9004 - 19 = 8985
        s = ce.Session(FIX, budget=200_000, budget_pinned=True)
        with open(FIX, "rb") as fh:
            for raw in fh:
                s.feed_line(raw.decode("utf-8"))
                if s.overhead0 is not None:
                    break
        self.assertEqual(s.overhead0, 8985)
        self.assertEqual(s.alpha, 1.0)

    def test_rebase_after_compaction(self):
        # first post-compaction usage (a-a8: R=2910) re-measures. a-a8 OPENS
        # turn 7, so reasoning-t6 (36) is allocated first:
        # est_live there = survivors(34+23+4+30+15=106) + summary(33)
        #                + reasoning-t6(36) = 175
        # overhead0' = 2910 - 175 = 2735
        s = load_session()
        self.assertEqual(s.overhead0, 2735)
        self.assertEqual(s.alpha, 1.0)
        # overhead at the final usage (a-a8b, R=2960, est_live=175+8=183):
        self.assertEqual(s.overhead, 2960 - 183)   # = 2777
        # final live estimate includes a8b's own text: 183 + 4 = 187
        self.assertEqual(s.est_live, 187)

    def test_alpha_scales_when_estimates_exceed_R(self):
        # synthetic: user est 1000 (3800 chars), turn R=500
        # overhead0 = max(0, 500-1000) = 0; alpha = (500-0)/1000 = 0.5
        s = ce.Session("/nonexistent.jsonl", budget=200_000, budget_pinned=True)
        s.feed_obj({"type": "user", "uuid": "x-u1",
                    "timestamp": "2026-07-17T10:00:00.000Z",
                    "message": {"role": "user", "content": "x" * 3800}})
        s.feed_obj({"type": "assistant", "uuid": "x-a1", "requestId": "req_x1",
                    "timestamp": "2026-07-17T10:00:01.000Z",
                    "message": {"role": "assistant", "model": "claude-fable-5",
                                "content": [], "stop_reason": "end_turn",
                                "usage": {"input_tokens": 500,
                                          "output_tokens": 1,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}})
        self.assertEqual(s.overhead0, 0)
        self.assertAlmostEqual(s.alpha, 0.5)
        self.assertEqual(s.overhead, 0)
        segs = s.build_map_segs()
        self.assertEqual(sum(x["tok"] for x in segs), 500)  # sums to exactly R
        self.assertEqual(segs[0]["cat"], "overhead")

    def test_map_sums_to_R_exactly(self):
        s = load_session()
        m = s.map_payload()
        self.assertEqual(m["rev"], 1)              # one compaction rebuild
        self.assertEqual(m["alpha"], 1.0)
        self.assertEqual(m["segs"][0]["cat"], "overhead")
        self.assertEqual(sum(x["tok"] for x in m["segs"]), 2960)

    def test_cats_payload(self):
        # live ests post-fixture: tool = agent_in 34 + agent_ret 23 = 57;
        # file = write_in 30 + write_ack 15 = 45; assistant = 4+8+4 = 16;
        # summary = 33; reasoning = reasoning-t6 = 36 (t0..t5 evicted at the
        # compaction); overhead as measured at last usage = 2960-183 = 2777.
        s = load_session()
        self.assertEqual(s.cats_payload(),
                         {"overhead": 2777, "user": 0, "assistant": 16,
                          "thinking": 0, "reasoning": 36, "file": 45,
                          "bash": 0, "tool": 57, "attach": 0, "summary": 33})


class TestFilesAndAccess(unittest.TestCase):
    CFG = "/Users/tester/proj/src/config.py"
    NOTES = "/Users/tester/proj/notes.md"

    def setUp(self):
        self.s = load_session()

    def test_file_waste(self):
        # config.py: read#1 400 -> edit input 29 (live 429) -> read#2 resets
        # live to 423. cum = 400+29+423 = 852; waste = 852-423 = 429.
        fid = self.s.path2id[self.CFG]
        f = self.s.file_payload(fid)
        self.assertEqual((f["tok"], f["waste"]), (423, 429))
        self.assertEqual((f["reads"], f["writes"], f["edits"]), (2, 0, 1))
        self.assertFalse(f["resident"])            # evicted at compaction
        nid = self.s.path2id[self.NOTES]
        n = self.s.file_payload(nid)
        # notes.md: one Write, input est 30, no waste, survives compaction
        self.assertEqual((n["tok"], n["waste"]), (30, 0))
        self.assertEqual((n["reads"], n["writes"], n["edits"]), (0, 1, 0))
        self.assertTrue(n["resident"])

    def test_faccess_stream(self):
        fid = self.s.path2id[self.CFG]
        nid = self.s.path2id[self.NOTES]
        got = [(a["turn"], a["file"], a["op"], a["tok"])
               for a in self.s.faccess]
        self.assertEqual(got, [(1, fid, "r", 400),   # read#1 result
                               (3, fid, "e", 29),    # Edit input
                               (4, fid, "r", 423),   # read#2 result
                               (6, nid, "w", 30)])   # Write input


class TestCompaction(unittest.TestCase):
    def setUp(self):
        self.s = load_session()

    def test_compaction_record(self):
        self.assertEqual(len(self.s.compactions), 1)
        c = self.s.compactions[0]
        self.assertEqual((c["n"], c["turn"], c["trigger"]), (1, 6, "auto"))
        self.assertEqual((c["pre"], c["post"], c["dropped"]),
                         (15200, 2600, 12600))
        self.assertEqual(c["cum_dropped"], 12600)
        self.assertEqual(c["dur_ms"], 8400)
        self.assertEqual(c["preserved_msgs"], 4)
        # evicted estimates by category (sums of the per-record ests above):
        # user 19+13+7+1200=1239 · thinking 17 · assistant 12+8+8=28
        # file 13+400+29+16+13+423=894 · bash 16+5=21 · attach 13+22=35
        # reasoning 91+69+44+38+75+61=378 (hid(t0..t5); synthetic uuids
        # never appear in preservedMessages, so compaction evicts them all)
        self.assertEqual(c["dropped_cats"],
                         {"user": 1239, "thinking": 17, "assistant": 28,
                          "file": 894, "bash": 21, "attach": 35,
                          "reasoning": 378})
        fid = self.s.path2id["/Users/tester/proj/src/config.py"]
        self.assertEqual(c["dropped_files"], [{"file": fid, "tok": 894}])

    def test_eviction_set(self):
        # ring keeps only allUuids survivors + post-boundary records
        # (reasoning-t6 is post-boundary: allocated when turn 7 opens)
        live = set(seg["uuid"] for seg in self.s.ring.values())
        self.assertEqual(live, {"a-a6", "u-u9", "a-a7", "u-u10",
                                "u-cs", "a-a8", "a-a8b", "reasoning-t6"})
        self.assertEqual(self.s.cat_est["summary"], 33)  # isCompactSummary rec

    def test_t_auto_not_lowered(self):
        # auto compaction at 15200/200000 = 0.076 must NOT lower T_auto
        self.assertEqual(self.s.t_auto, 0.85)

    def test_events_ledger(self):
        kinds = [e["kind"] for e in self.s.events]
        self.assertEqual(kinds, ["api_error", "compaction"])
        err = list(self.s.events)[0]
        self.assertEqual(err["severity"], "error")
        self.assertIn("retry 3/10", err["msg"])
        self.assertEqual(self.s.api_errors, 1)
        self.assertEqual(self.s.last_retry_ms, 8000)


class TestAgent(unittest.TestCase):
    def test_agent_completion(self):
        s = load_session()
        self.assertEqual(list(s.agents), ["ab12cd34ef56ab78c"])
        a = s.agent_payload("ab12cd34ef56ab78c")
        self.assertEqual(a["state"], "done")
        self.assertEqual(a["own_tok"], 55000)          # totalTokens
        self.assertEqual(a["ret_tok"], 23)             # est of returned block
        self.assertEqual(a["tools"], {"r": 3, "s": 2, "b": 1, "e": 0})
        self.assertEqual(a["dur_ms"], 42000)
        self.assertEqual(a["agent_type"], "general-purpose")
        self.assertEqual(a["desc"], "survey tests")
        self.assertEqual((a["turn0"], a["turn1"]), (5, 5))


class TestMeta(unittest.TestCase):
    def test_meta_payload(self):
        s = load_session()
        m = s.meta_payload()
        self.assertEqual(m["session_id"], "feedbeef-0000-4000-8000-1234567890ab")
        self.assertEqual(m["title"], "golden: engine fixture")
        self.assertEqual(m["model"], "claude-fable-5")
        self.assertEqual(m["budget"], 200_000)
        self.assertEqual(m["t_auto"], 0.85)
        self.assertEqual(m["cc_version"], "2.1.205")
        self.assertEqual(m["project"], "/Users/tester/proj")


class TestBudgetAndSignals(unittest.TestCase):
    def _turn(self, s, rid, r_in, cr, cc):
        s.feed_obj({"type": "assistant", "uuid": "u-" + rid, "requestId": rid,
                    "timestamp": "2026-07-17T11:00:00.000Z",
                    "message": {"role": "assistant", "model": "m", "content": [],
                                "stop_reason": "end_turn",
                                "usage": {"input_tokens": r_in,
                                          "output_tokens": 1,
                                          "cache_read_input_tokens": cr,
                                          "cache_creation_input_tokens": cc}}})

    def test_budget_auto_bump(self):
        s = ce.Session("/x.jsonl", budget=200_000)
        self._turn(s, "req_1", 250_000, 0, 0)          # R exceeds the rung
        self.assertEqual(s.budget, 1_000_000)
        self.assertTrue(any("budget bumped" in m for m in s.pending["logs"]))

    def test_t_auto_refined_by_auto_compaction(self):
        s = ce.Session("/x.jsonl", budget=200_000)
        self._turn(s, "req_1", 100, 149_900, 0)
        s.feed_obj({"type": "system", "subtype": "compact_boundary",
                    "timestamp": "2026-07-17T11:00:01.000Z", "uuid": "cb",
                    "compactMetadata": {"trigger": "auto", "preTokens": 190_000,
                                        "postTokens": 9_000, "durationMs": 5}})
        self.assertEqual(s.t_auto, 0.95)               # max(0.85, 190k/200k)

    def test_thrash_on_waterline_drop(self):
        s = ce.Session("/x.jsonl", budget=200_000)
        self._turn(s, "req_1", 100, 50_000, 0)
        self._turn(s, "req_2", 100, 10_000, 0)         # C drops 40k > 1024
        self.assertIn("thrash", [e["kind"] for e in s.events])

    def test_malformed_and_unknown_tolerated(self):
        s = ce.Session("/x.jsonl", budget=200_000)
        s.feed_line("{not json", 0)
        s.feed_line('"just a string"', 0)
        s.feed_line('{"type":"totally_new_record_kind","x":1}', 0)
        s.feed_line('{"type":"user","message":{"content":null}}', 0)
        self.assertEqual(s.malformed, 1)
        self.assertEqual(s.est_live, 0)


class TestCheckpointSeek(unittest.TestCase):
    def assert_states_equal(self, a, b, turn):
        self.assertEqual(len(a.turns), len(b.turns), "turn %d" % turn)
        self.assertEqual(a.resident(), b.resident(), "turn %d" % turn)
        self.assertEqual(a.cats_payload(), b.cats_payload(), "turn %d" % turn)
        self.assertEqual([a.turn_payload(i) for i in range(len(a.turns))],
                         [b.turn_payload(i) for i in range(len(b.turns))])
        self.assertEqual([a.file_payload(f) for f in a.files],
                         [b.file_payload(f) for f in b.files])
        self.assertEqual(list(a.ring.keys()), list(b.ring.keys()))
        self.assertEqual(a.map_payload(), b.map_payload(), "turn %d" % turn)
        self.assertEqual(list(a.faccess), list(b.faccess))
        self.assertEqual((a.overhead0, a.alpha, a.overhead),
                         (b.overhead0, b.alpha, b.overhead))

    def test_seek_equals_linear_replay(self):
        live = load_session(ckpt_every=2)
        self.assertGreaterEqual(len(live.checkpoints), 3)  # ckpts exist
        for t in (0, 1, 3, 5, 6, 7):
            via_seek = live.state_at_turn(t)
            linear = load_session(stop_at_turn=t)
            self.assert_states_equal(via_seek, linear, t)

    def test_seek_specific_values(self):
        live = load_session(ckpt_every=2)
        st3 = live.state_at_turn(3)
        self.assertEqual(st3.resident(), 10960)
        self.assertEqual(len(st3.turns), 4)
        self.assertEqual(len(st3.compactions), 0)
        st7 = live.state_at_turn(7)
        self.assertEqual(st7.resident(), 2960)
        self.assertEqual(len(st7.compactions), 1)

    def test_seek_does_not_mutate_live(self):
        live = load_session(ckpt_every=2)
        before = (live.resident(), len(live.turns), live.cats_payload(),
                  len(live.ring), live.map_payload())
        live.state_at_turn(2)
        after = (live.resident(), len(live.turns), live.cats_payload(),
                 len(live.ring), live.map_payload())
        self.assertEqual(before, after)


class TestSelftestStream(unittest.TestCase):
    def test_selftest_message_sequence(self):
        out = subprocess.run(
            [PY, os.path.join(ROOT, "amtr_engine.py"), "--selftest",
             "--session", FIX, "--budget", "200000"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        self.assertEqual(out.returncode, 0)
        lines = out.stdout.decode("utf-8").strip().splitlines()
        msgs = []
        for ln in lines:
            d = json.loads(ln)                      # every line: valid JSON
            self.assertIsInstance(d, dict)
            self.assertIn("type", d)                # every line: typed
            msgs.append(d)
        types = [m["type"] for m in msgs]
        # attach handshake order is normative
        self.assertEqual(types[:6],
                         ["init", "meta", "files", "map", "backfill", "ready"])
        by = {}
        for m in msgs:
            by.setdefault(m["type"], []).append(m)
        # backfill holds turns 0..5 (cut = total-2); incremental covers 6,7
        bf = by["backfill"][0]
        self.assertEqual(len(bf["turns"]), 6)
        self.assertEqual(bf["turns"][-1]["resident"], 14300)
        self.assertEqual(len(bf["agents"]), 1)
        self.assertEqual(bf["agents"][0]["state"], "done")
        self.assertEqual([e["kind"] for e in bf["events"]], ["api_error"])
        self.assertEqual(bf["compactions"], [])
        rd = by["ready"][0]
        self.assertEqual((rd["turns"], rd["resident"], rd["budget"]),
                         (6, 14300, 200000))
        # incremental flow after ready
        post = types[6:]
        for needed in ("turn", "faccess", "files", "cats", "compaction",
                       "event", "map", "map_add", "log"):
            self.assertIn(needed, post, "missing incremental %r" % needed)
        turn_ids = sorted(set(m["turn"] for m in by["turn"]))
        self.assertEqual(turn_ids, [6, 7])
        self.assertEqual(by["turn"][-1]["resident"], 2960)
        comp = by["compaction"][0]
        self.assertEqual((comp["pre"], comp["post"]), (15200, 2600))
        # exactly one map rebuild after ready, rev bumped to 1
        self.assertEqual(types[6:].count("map"), 1)
        self.assertEqual([m for m in msgs[6:] if m["type"] == "map"][0]["rev"], 1)
        self.assertTrue(any("cross-check" in m["msg"] for m in by["log"]))


class TestReviewFixes(unittest.TestCase):
    """Regressions for the confirmed adversarial-review findings."""

    def test_map_rebuild_fires_before_ui_ring_cap(self):
        # SPEC (b): a fresh coalesced map (rev+1) must be requested BEFORE
        # base + map_add segs since the last rebuild reach MAP_CAP.
        sess = ce.Session(FIX, budget=200_000, budget_pinned=True)
        base = sess.map_payload()          # sets map_base_n, resets counter
        rev0 = sess.map_rev
        old_cap = ce.MAP_CAP
        ce.MAP_CAP = len(base["segs"]) + 4
        try:
            for i in range(6):
                sess._alloc("user", 10, "cadence-%d" % i,
                            "2026-07-17T10:00:0%d.000Z" % i)
        finally:
            ce.MAP_CAP = old_cap
        self.assertTrue(sess.pending["map_rebuild"])
        self.assertEqual(sess.map_rev, rev0 + 1)   # bumped exactly once
        rebuilt = sess.map_payload()
        self.assertEqual(rebuilt["rev"], rev0 + 1)
        self.assertEqual(sess.map_adds_since, 0)   # cadence counter reset

    def test_preserved_segment_fallback_spans_arrival_order(self):
        # headUuid = the api_error system record (parses but allocates NO
        # segment); the old ring-walk fallback silently kept nothing.
        lines = [l for l in open(FIX, encoding="utf-8")]
        recs = [json.loads(l) for l in lines]
        cut = next(i for i, d in enumerate(recs)
                   if d.get("subtype") == "compact_boundary")
        cb = recs[cut]
        cb["compactMetadata"].pop("preservedMessages", None)
        cb["compactMetadata"]["preservedSegment"] = {
            "headUuid": "s-err1", "tailUuid": "u-u10"}
        sess = ce.Session(FIX, budget=200_000, budget_pinned=True)
        for i in range(cut):
            sess.feed_line(lines[i], 0)
        sess.feed_line(json.dumps(cb), 0)
        kept = set(s["uuid"] for s in sess.ring.values())
        # records arriving between s-err1 and u-u10 survive...
        self.assertIn("u-u10", kept)
        self.assertTrue(any(u.startswith("a-a7") for u in kept))
        # ...records before the head are evicted, and it is NOT keep-nothing
        self.assertNotIn("u-u1", kept)
        self.assertGreater(len(kept), 0)

    def test_preserved_segment_unknown_anchors_keeps_nothing(self):
        lines = [l for l in open(FIX, encoding="utf-8")]
        recs = [json.loads(l) for l in lines]
        cut = next(i for i, d in enumerate(recs)
                   if d.get("subtype") == "compact_boundary")
        cb = recs[cut]
        cb["compactMetadata"].pop("preservedMessages", None)
        cb["compactMetadata"]["preservedSegment"] = {
            "headUuid": "nope-1", "tailUuid": "nope-2"}
        sess = ce.Session(FIX, budget=200_000, budget_pinned=True)
        for i in range(cut):
            sess.feed_line(lines[i], 0)
        sess.feed_line(json.dumps(cb), 0)
        self.assertEqual(len(sess.ring), 0)
        self.assertTrue(any("anchors unknown" in m
                            for m in sess.pending["logs"]))

    def test_pump_shrink_without_reset_is_left_to_caller(self):
        # SPEC (d): the main transcript path must re-attach on shrink, so
        # _pump(reset_on_shrink=False) must not silently re-feed from 0.
        import tempfile
        eng = ce.Engine.__new__(ce.Engine)   # _pump is self-contained
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write('{"type":"x"}\n')
            path = fh.name
        try:
            big_off = os.path.getsize(path) + 100
            hits = []
            off, buf, grew = eng._pump(path, big_off, b"",
                                       lambda raw, o: hits.append(raw),
                                       reset_on_shrink=False)
            self.assertEqual((off, buf, grew, hits),
                             (big_off, b"", False, []))
            # default behavior (subagent tails) still resets and re-reads
            off, buf, grew = eng._pump(path, big_off, b"",
                                       lambda raw, o: hits.append(raw))
            self.assertEqual(len(hits), 1)
            self.assertTrue(grew)
        finally:
            os.unlink(path)

    def test_snapshot_carries_turn_usage(self):
        # SPEC (b): snapshot resident/waterline/cc are the SOUGHT turn's
        # values (fed to replay renders so they never fall back to live).
        live = load_session(ckpt_every=2)
        st = live.state_at_turn(3)
        last = st.turns[-1]
        self.assertEqual(st.resident(), EXP_R[3])
        self.assertEqual(last["waterline"], EXP_C[3])
        self.assertEqual(last["cc"], st.turn_payload(len(st.turns) - 1)["cc"])

    def test_cmd_feed_from_fixture(self):
        # SPEC (b) `cmd`: one entry per completed Bash execution
        sess = load_session()
        self.assertEqual(len(sess.cmds), 1)
        c = sess.cmds[0]
        self.assertEqual(c["cmd"], "cd /Users/tester/proj && python3 -m pytest -q")
        self.assertEqual(c["out"], "2 passed in 0.41s")
        self.assertEqual((c["ok"], c["interrupted"], c["bg"]), (True, False, False))
        self.assertEqual(c["turn"], 2)
        self.assertGreater(c["epoch"], 0)
        self.assertEqual(c["tok_out"], sess.cmds[0]["tok_out"])  # int, present
        self.assertIn("cmds", sess.backfill_payload())

    def test_cmd_flag_combinations(self):
        # dedicated shell fixture: ok / err / interrupted / bg in one turn
        path = os.path.join(ROOT, "tests", "fixtures", "shell.jsonl")
        sess = ce.Session(path, budget=200_000, budget_pinned=True)
        with open(path, "rb") as fh:
            for raw in fh:
                sess.feed_line(raw.decode("utf-8"), 0)
        self.assertEqual(len(sess.cmds), 4)
        by_cmd = {c["cmd"].split()[0]: c for c in sess.cmds}
        ok = by_cmd["git"]
        self.assertEqual((ok["ok"], ok["desc"]), (True, "check working tree"))
        self.assertIn("?? notes.txt", ok["out"])
        err = by_cmd["cargo"]
        self.assertFalse(err["ok"])
        self.assertIn("assertion failed", err["err"])
        self.assertNotIn("\x1b", err["err"])       # ANSI stripped
        self.assertIn(" boom", err["err"])         # content survives
        intr = by_cmd["npm"]
        self.assertTrue(intr["interrupted"])
        self.assertFalse(intr["ok"])
        bg = by_cmd["python3"]
        self.assertTrue(bg["bg"])
        self.assertTrue(bg["ok"])

    def test_peek(self):
        # INSPECT mode: seg -> the record's actual text, bounded
        sess = load_session()
        # overhead is always seg 0
        p0 = sess.peek_payload(0)
        self.assertTrue(p0["found"])
        self.assertEqual((p0["cat"], p0["kind"]), ("overhead", "overhead"))
        self.assertIn("system prompt", p0["excerpt"])
        # a surviving file segment (the Write input, est 30 — pre-compaction
        # reads were evicted, and peek on those correctly answers found:false)
        seg = next(s for s in sess.ring.values()
                   if s["cat"] == "file" and s["est"] == 30)
        pk = sess.peek_payload(seg["id"])
        self.assertTrue(pk["found"])
        self.assertEqual(pk["cat"], "file")
        self.assertEqual(pk["kind"], "assistant")  # tool_use carrier
        self.assertIn("Write", pk["excerpt"])
        self.assertFalse(pk["truncated"])
        # an evicted segment's id answers found:false
        evicted_ids = set(range(1, 20)) - set(sess.ring.keys())
        if evicted_ids:
            self.assertFalse(sess.peek_payload(min(evicted_ids))["found"])
        # an unknown segment answers found:false, never errors
        self.assertFalse(sess.peek_payload(999_999)["found"])

    def test_ret_feed(self):
        # SPEC (b) `ret`: external retrievals only, classified by kind/src
        path = os.path.join(ROOT, "tests", "fixtures", "shell.jsonl")
        sess = ce.Session(path, budget=200_000, budget_pinned=True)
        with open(path, "rb") as fh:
            for raw in fh:
                sess.feed_line(raw.decode("utf-8"), 0)
        self.assertEqual(len(sess.rets), 2)
        ws, mcp = sess.rets
        self.assertEqual((ws["kind"], ws["src"], ws["n"], ws["dur_ms"]),
                         ("search", "web", 5, 2400))
        self.assertEqual(ws["q"], "ratatui braille canvas")
        self.assertTrue(ws["ok"] and ws["tok"] > 0)
        self.assertEqual((mcp["kind"], mcp["src"]),
                         ("mcp", "claude_ai_Dropbox"))
        self.assertIn("search quest control panel", mcp["q"])
        # file tools never appear in the retrieval feed
        self.assertTrue(all(r["kind"] in ("search", "fetch", "toolsearch",
                                          "mcp") for r in sess.rets))
        self.assertIn("rets", sess.backfill_payload())

    def test_cmd_sanitization_and_clipping(self):
        self.assertEqual(ce.clean_text("a\x1b[31mred\x1b[0mb\rc"), "aredb\nc")
        self.assertEqual(ce.head_clip("x" * 300, 240)[-1], "…")
        self.assertEqual(len(ce.head_clip("x" * 300, 240)), 240)
        t = ce.tail_clip("y" * 700, 600)
        self.assertEqual((t[0], len(t)), ("…", 600))
        self.assertEqual(ce.tail_clip("short", 600), "short")

    def test_seek_equivalence_through_preserved_segment_fallback(self):
        # review fix: the seek replay path must maintain uuid_order, or a
        # preservedSegment-resolved compaction evicts everything in replay
        import tempfile
        lines = [l for l in open(FIX, encoding="utf-8")]
        recs = [json.loads(l) for l in lines]
        cut = next(i for i, d in enumerate(recs)
                   if d.get("subtype") == "compact_boundary")
        cb = recs[cut]
        cb["compactMetadata"].pop("preservedMessages", None)
        cb["compactMetadata"]["preservedSegment"] = {
            "headUuid": "s-err1", "tailUuid": "u-u10"}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            for i, l in enumerate(lines):
                fh.write(json.dumps(cb) + "\n" if i == cut else l)
            path = fh.name
        try:
            live = ce.Session(path, budget=200_000, budget_pinned=True,
                              ckpt_every=2)
            off = 0
            with open(path, "rb") as fh:
                for raw in fh:
                    live.feed_line(raw.decode("utf-8"), off)
                    off += len(raw)
            last = len(live.turns) - 1
            via_seek = live.state_at_turn(last)
            self.assertEqual(sorted(s["uuid"] for s in via_seek.ring.values()),
                             sorted(s["uuid"] for s in live.ring.values()))
            self.assertEqual(via_seek.est_live, live.est_live)
            self.assertGreater(len(via_seek.ring), 0,
                               "replay must not evict everything")
        finally:
            os.unlink(path)

    def test_turn_at_epoch(self):
        sess = load_session()
        first = sess.turn_epochs[0]
        self.assertEqual(sess.turn_at_epoch(first - 100), 0)
        self.assertEqual(sess.turn_at_epoch(sess.turn_epochs[-1] + 999),
                         len(sess.turns) - 1)
        mid = sess.turn_epochs[3]
        self.assertEqual(sess.turn_at_epoch(mid + 0.5), 3)

    def test_synth_event_ts_is_utc(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            got = ce.now_hhmmss()
            want_h = time.strftime("%H", time.gmtime())
            local_h = time.strftime("%H")
            self.assertEqual(got[:2], want_h)
            if local_h != want_h:      # always true in this TZ except DST edge
                self.assertNotEqual(got[:2], local_h)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


class TestReasoningAndRebuild(unittest.TestCase):
    """SPEC (a): hidden reasoning category + server context rebuild."""

    def _turn(self, s, rid, r_in, cr, cc, out=1,
              ts="2026-07-17T11:00:00.000Z"):
        s.feed_obj({"type": "assistant", "uuid": "u-" + rid, "requestId": rid,
                    "timestamp": ts,
                    "message": {"role": "assistant", "model": "m",
                                "content": [], "stop_reason": "end_turn",
                                "usage": {"input_tokens": r_in,
                                          "output_tokens": out,
                                          "cache_read_input_tokens": cr,
                                          "cache_creation_input_tokens": cc}}})

    def test_fixture_reasoning_allocation(self):
        # state at end of turn 5: reasoning-t0..t4 live (t5's seg only
        # arrives when turn 6 opens). hid values per the module docstring.
        s = load_session(stop_at_turn=5)
        segs = {g["uuid"]: g for g in s.ring.values()
                if g["cat"] == "reasoning"}
        self.assertEqual(
            set((u, g["est"], g["born"]) for u, g in segs.items()),
            {("reasoning-t0", 91, 0), ("reasoning-t1", 69, 1),
             ("reasoning-t2", 44, 2), ("reasoning-t3", 38, 3),
             ("reasoning-t4", 75, 4)})
        self.assertEqual(s.cat_est["reasoning"], 317)  # 91+69+44+38+75
        # seg ts = epoch of the record that OPENED the charged turn
        self.assertEqual(segs["reasoning-t0"]["ts"],
                         ce.ts_epoch("2026-07-17T10:00:14.000Z"))  # a-a1
        self.assertEqual(segs["reasoning-t1"]["ts"],
                         ce.ts_epoch("2026-07-17T10:00:35.000Z"))  # a-a2
        # full replay: only reasoning-t6 (36) survives the compaction; t7
        # never gets a seg (the session ends before turn 8 opens)
        full = load_session()
        rs = [g for g in full.ring.values() if g["cat"] == "reasoning"]
        self.assertEqual([(g["uuid"], g["est"], g["born"]) for g in rs],
                         [("reasoning-t6", 36, 6)])

    def test_peek_reasoning(self):
        # synthetic uuid names no record: peek must answer BEFORE the disk
        # lookup with found:true, kind "reasoning" and the explainer
        s = load_session()
        seg = next(g for g in s.ring.values() if g["cat"] == "reasoning")
        p = s.peek_payload(seg["id"])
        self.assertTrue(p["found"])
        self.assertEqual((p["cat"], p["kind"]), ("reasoning", "reasoning"))
        self.assertEqual((p["born"], p["est"], p["tok"]), (6, 36, 36))
        for phrase in ("encrypted", "signature", "turn 6", "cached input",
                       "output_tokens"):
            self.assertIn(phrase, p["excerpt"])
        self.assertFalse(p["truncated"])

    def test_rebuild_detection(self):
        # R: 50_000 -> 55_000 -> 30_000 (falls 25k, NO compact_boundary)
        s = ce.Session("/x.jsonl", budget=200_000, budget_pinned=True)
        self._turn(s, "r1", 100, 0, 49_900, out=500,
                   ts="2026-07-17T11:00:00.000Z")
        self._turn(s, "r2", 100, 49_900, 5_000, out=400,
                   ts="2026-07-17T11:01:00.000Z")
        # turn 1 open charged turn 0: hid = 500 (content is empty)
        self.assertEqual(s.cat_est["reasoning"], 500)
        rev0 = s.map_rev
        self._turn(s, "r3", 100, 20_000, 9_900, out=10,
                   ts="2026-07-17T11:02:00.000Z")
        # at open, turn 1 was charged 400; then the rebuild evicted ALL
        # reasoning (500+400) and re-based overhead0 = max(0, R - 0)
        self.assertEqual(s.cat_est["reasoning"], 0)
        self.assertEqual([g for g in s.ring.values()
                          if g["cat"] == "reasoning"], [])
        self.assertEqual(s.est_live, 0)
        self.assertEqual(s.overhead0, 30_000)
        self.assertEqual(s.overhead, 30_000)
        self.assertFalse(s.rebase_pending)      # consumed by the re-base
        self.assertEqual(s.map_rev, rev0 + 1)
        self.assertTrue(s.pending["map_rebuild"])
        ev = [e for e in s.events if e["kind"] == "rebuild"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["severity"], ev[0]["turn"]), ("warn", 2))
        self.assertIn("55k -> 30k", ev[0]["msg"])
        self.assertIn(ev[0], s.pending["events"])   # queued for drain
        # a small fall (<10k) must NOT fire again
        self._turn(s, "r4", 100, 20_000, 4_900, out=1,
                   ts="2026-07-17T11:03:00.000Z")   # R=25_000: falls 5k
        self.assertEqual(
            [e["kind"] for e in s.events].count("rebuild"), 1)
        self.assertEqual(s.cat_est["reasoning"], 10)  # turn 2's hid survives

    def test_rebuild_does_not_fire_at_compaction(self):
        # a compaction drops R too; its boundary must consume the guard
        s = ce.Session("/x.jsonl", budget=200_000, budget_pinned=True)
        self._turn(s, "r1", 100, 0, 49_900, out=500)
        s.feed_obj({"type": "system", "subtype": "compact_boundary",
                    "timestamp": "2026-07-17T11:03:00.000Z", "uuid": "cb1",
                    "compactMetadata": {"trigger": "auto",
                                        "preTokens": 50_000,
                                        "postTokens": 30_000,
                                        "durationMs": 5}})
        self._turn(s, "r2", 100, 0, 29_900, out=50)   # R=30_000: falls 20k
        kinds = [e["kind"] for e in s.events]
        self.assertIn("compaction", kinds)
        self.assertNotIn("rebuild", kinds)
        # the guard is consumed by that one turn: a LATER >10k fall with no
        # new boundary DOES fire
        self._turn(s, "r3", 100, 0, 14_900, out=1)    # R=15_000: falls 15k
        self.assertEqual(
            [e["kind"] for e in s.events].count("rebuild"), 1)

    def test_read_decoration_uses_larger_est(self):
        # when both the message tool_result content AND
        # toolUseResult.file.content exist, the LARGER est wins (the API
        # prompt carries the decorated, line-numbered block)
        def sess_with(msg_chars, file_chars):
            s = ce.Session("/x.jsonl", budget=200_000, budget_pinned=True)
            s.feed_obj({"type": "assistant", "uuid": "a1", "requestId": "r1",
                        "timestamp": "2026-07-17T11:00:00.000Z",
                        "message": {"role": "assistant", "model": "m",
                                    "content": [{"type": "tool_use",
                                                 "id": "t1", "name": "Read",
                                                 "input": {"file_path":
                                                           "/f.py"}}],
                                    "usage": {"input_tokens": 10,
                                              "output_tokens": 1,
                                              "cache_read_input_tokens": 0,
                                              "cache_creation_input_tokens":
                                                  0}}})
            s.feed_obj({"type": "user", "uuid": "u1",
                        "timestamp": "2026-07-17T11:00:01.000Z",
                        "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "t1",
                             "content": "x" * msg_chars}]},
                        "toolUseResult": {"file": {"filePath": "/f.py",
                                                   "content":
                                                       "y" * file_chars}}})
            return s
        # decorated message block bigger: 3800 chars -> 1000 tok wins
        s = sess_with(3800, 380)
        self.assertEqual(s.files[s.path2id["/f.py"]]["tok"], 1000)
        # raw structured copy bigger: 1520 chars -> 400 tok wins
        s = sess_with(38, 1520)
        self.assertEqual(s.files[s.path2id["/f.py"]]["tok"], 400)


class TestReport(unittest.TestCase):
    """SPEC (f) REPORT mode — a RENDERING of the same Session accounting.
    Aggregates derived by hand from the golden fixture:
      Σin  = 4+6+420+610+1500+40+320+10                      = 2,910
      Σcr  = 0+9000+9350+9870+10350+12500+14260+0            = 65,330
      Σcc5m= 9000+350+520+480+400+1760+620+2950              = 16,080
      Σcc1h= 1750 (turn 4)   Σcc = 17,830
      Σout = 120+90+60+75+88+95+70+150                       = 748
      hit  = 65330/(65330+17830+2910) = 0.759
      cost_u/turn = [11.9,1.8,2.3,2.6,7.0,4.0,2.9,4.4] → Σ 36.9,
      mean 4.6, p95 (ceil(.95·8)=8th sorted) = 11.9
      peak R = 15,200 at turn 6; final R 2,960; compaction dropped
      preTokens−postTokens = 15,200−2,600 = 12,600 (authoritative)."""

    @classmethod
    def setUpClass(cls):
        out = subprocess.run(
            [PY, os.path.join(ROOT, "amtr_engine.py"), "--report",
             "--session", FIX, "--budget", "200000"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        cls.rc = out.returncode
        cls.md = out.stdout.decode("utf-8")

    def test_exit_and_real_stdout(self):
        # the report lands on REAL stdout (not the protocol fd / stderr),
        # titled by the distinct session NAME with the description beneath
        self.assertEqual(self.rc, 0)
        self.assertTrue(self.md.startswith("# amtr report — "))
        self.assertIn("*golden: engine fixture*", self.md)     # the title

    def test_header(self):
        # offline fixture → a memorable adjective-noun name, deterministic
        name = ce.memorable_name("feedbeef-0000-4000-8000-1234567890ab")
        self.assertRegex(name, r"^[a-z]+-[a-z]+$")
        for line in ("- session: %s (feedbeef-0000-4000-8000-1234567890ab)" % name,
                     "- project: /Users/tester/proj",
                     "- model: claude-fable-5",
                     "- cc version: 2.1.205",
                     "- turns: 8",
                     "- entrypoint: cli"):
            self.assertIn(line, self.md)
        self.assertIn("(2m 48s)", self.md)     # 10:00:07 → 10:02:55

    def test_memorable_name_stable_and_distinct(self):
        # deterministic per uuid, and distinct across many
        a = ce.memorable_name("aaaa-1111")
        self.assertEqual(a, ce.memorable_name("aaaa-1111"))  # stable
        names = {ce.memorable_name("s-%d" % i) for i in range(200)}
        self.assertGreater(len(names), 190)   # ~unique across 200 sessions

    def test_context_authoritative(self):
        self.assertIn("## CONTEXT (authoritative)", self.md)
        self.assertIn("- final R: 2,960 / 200,000 (1.5% of budget)", self.md)
        self.assertIn("- peak R: 15,200 (turn 6)", self.md)
        self.assertIn("- compactions: 1 · 12,600 tokens dropped cumulatively",
                      self.md)
        self.assertIn("- #1 t6 10:02:27 auto: 15,200 → 2,600 "
                      "(dropped 12,600)", self.md)
        self.assertIn("- server rebuilds: 0", self.md)
        # composition == cats_payload: overhead 2,777; reasoning 36 (only
        # reasoning-t6 of the hid table survives the compaction); summary 33
        self.assertIn("| overhead | 2,777 |", self.md)
        self.assertIn("| reasoning | 36 |", self.md)
        self.assertIn("| summary | 33 |", self.md)

    def test_economics(self):
        self.assertIn("## ECONOMICS (authoritative)", self.md)
        self.assertIn("| 2,910 | 65,330 | 16,080 | 1,750 | 748 |", self.md)
        self.assertIn("- overall hit rate: 75.9%", self.md)
        self.assertIn("- total cost: 36.9 u", self.md)
        self.assertIn("- cost/turn: mean 4.6 u · p95 11.9 u", self.md)
        self.assertIn("- thrash events: 0", self.md)

    def test_files(self):
        self.assertIn("## FILES (estimated)", self.md)
        self.assertIn("| 423 | — | 2 | 0 | 1 | 429 | "
                      "✝ /Users/tester/proj/src/config.py |", self.md)
        self.assertIn("| 30 | 1.0 | 0 | 1 | 0 | 0 | "
                      "/Users/tester/proj/notes.md |", self.md)
        self.assertIn("- total waste: 429 tokens", self.md)
        self.assertIn("- evicted files: 1", self.md)

    def test_shell_no_failures(self):
        # 1 completed Bash execution, ok; est("2 passed in 0.41s") = 5
        self.assertIn("- 1 command(s): 1 ok · 0 failed · 0 interrupted "
                      "· 0 bg", self.md)
        self.assertIn("- Σ tok_out: 5", self.md)
        shell = self.md.split("## SHELL")[1].split("\n## ")[0]
        self.assertNotIn("- failures:", shell)

    def test_agents(self):
        # own 55,000 / final R 2,960 = 18.58× · amp 55,000/23 = 2391.3
        self.assertIn("- fan-out 55,000 ≡ 18.58× main · Σ ret 23 "
                      "· median amp 2391.3", self.md)
        self.assertIn("general-purpose · survey tests · own 55,000 / "
                      "ret 23 / amp 2391.3 / dur 42s", self.md)

    def test_events_errors_first(self):
        block = self.md.split("## EVENTS")[1].split("\n## ")[0]
        self.assertLess(block.index("api_error"), block.index("compaction"))
        self.assertIn("API overloaded (retry 3/10 in 8s)", block)

    def test_timeline_markdown(self):
        # scaled to the session's OWN peak (15,200): 9004/15200=.59→▅ …
        # 15200→█, 2960/15200=.19→▂. (Budget-scaling floored every headless
        # run to a useless flat ▁ row — fixed.)
        self.assertIn("t0    ▅▅▆▆███▂", self.md)
        self.assertIn("the session peak", self.md)
        self.assertIn("- t6 10:02:27 compaction:", self.md)

    def test_diagnostics(self):
        blk = self.md.split("## DIAGNOSTICS")[1]
        # config.py waste 429 of 852 traffic = 50% > 25%
        self.assertIn("waste hot-spot: /Users/tester/proj/src/config.py",
                      blk)
        self.assertIn("sub-50% cache-hit turns: t0, t7", blk)
        self.assertNotIn("no findings", blk)

    def test_json_report(self):
        out = subprocess.run(
            [PY, os.path.join(ROOT, "amtr_engine.py"), "--report",
             "--session", FIX, "--budget", "200000", "--json"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        self.assertEqual(out.returncode, 0)
        d = json.loads(out.stdout.decode("utf-8"))
        self.assertEqual(list(d), ["header", "context", "economics", "files",
                                   "shell", "retrieval", "agents", "events",
                                   "timeline", "diagnostics"])
        self.assertEqual(d["context"]["final_r"], 2960)
        self.assertEqual(d["context"]["peak_r"], 15200)
        self.assertEqual(d["context"]["label"], "authoritative")
        self.assertEqual(d["economics"]["label"], "authoritative")
        self.assertEqual(d["economics"]["out"], 748)
        self.assertEqual(d["files"]["label"], "estimated")
        self.assertTrue(d["files"]["table"])
        self.assertIsInstance(d["diagnostics"], list)
        tl = d["timeline"]
        self.assertEqual(tl["spark"], "▅▅▆▆███▂")  # scaled to peak 15,200
        self.assertEqual(tl["peak"], 15200)
        self.assertEqual(len(tl["marks"]), 8)
        self.assertEqual(tl["marks"].index("▼"), 6)  # the compaction turn
        self.assertEqual(d["header"]["turns"], 8)
        self.assertFalse(d["header"]["interrupted"])

    def test_shell_fixture_report(self):
        path = os.path.join(ROOT, "tests", "fixtures", "shell.jsonl")
        sess = ce.Session(path, budget=200_000, budget_pinned=True)
        with open(path, "rb") as fh:
            for raw in fh:
                sess.feed_line(raw.decode("utf-8"), 0)
        rep = ce.build_report(sess)
        s = rep["shell"]
        self.assertEqual((s["n"], s["ok"], s["failed"], s["interrupted"],
                          s["bg"]), (4, 2, 1, 1, 1))
        self.assertEqual(len(s["failures"]), 1)
        f = s["failures"][0]
        self.assertEqual(f["cmd"], "cargo test -q")
        self.assertIn("assertion failed: `(left == right)`", f["err"])
        self.assertIn(" boom", f["err"])           # verbatim, ANSI stripped
        r = rep["retrieval"]
        self.assertEqual(r["n"], 2)
        self.assertEqual({k["kind"]: k["n"] for k in r["by_kind"]},
                         {"search": 1, "mcp": 1})
        self.assertEqual({k["src"] for k in r["by_src"]},
                         {"web", "claude_ai_Dropbox"})
        md = ce.render_report_md(rep)
        self.assertIn("- 4 command(s): 2 ok · 1 failed · 1 interrupted "
                      "· 1 bg", md)
        self.assertIn("$ cargo test -q", md)
        self.assertIn("assertion failed", md)

    def test_watch_reports_on_idle(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            cp = os.path.join(tmp, "watch-fixture.jsonl")
            shutil.copy(FIX, cp)
            t0 = time.time()
            out = subprocess.run(
                [PY, os.path.join(ROOT, "amtr_engine.py"), "--report",
                 "--session", cp, "--budget", "200000",
                 "--watch", "--idle-secs", "1"],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30)
            dt = time.time() - t0
            self.assertEqual(out.returncode, 0)
            self.assertLess(dt, 10)    # the ~1s idle rule ended it, not 60s
            md = out.stdout.decode("utf-8")
            self.assertIn("- final R: 2,960 / 200,000", md)
            self.assertNotIn("INTERRUPTED", md)
            self.assertIn("watching feedbeef",
                          out.stderr.decode("utf-8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_watch_sigint_partial_report(self):
        import shutil
        import signal
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            cp = os.path.join(tmp, "watch-sigint.jsonl")
            shutil.copy(FIX, cp)
            p = subprocess.Popen(
                [PY, os.path.join(ROOT, "amtr_engine.py"), "--report",
                 "--session", cp, "--budget", "200000",
                 "--watch", "--idle-secs", "300"],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(2.0)            # past the initial parse, into watch
            p.send_signal(signal.SIGINT)
            out, _ = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 130)
            md = out.decode("utf-8")
            self.assertIn("**INTERRUPTED — partial run**", md)
            self.assertIn("- final R: 2,960 / 200,000", md)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
