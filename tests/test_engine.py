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

    def test_local_backend_without_requestid(self):
        # Ollama-served sessions write NO requestId; message.id is the turn
        # key. Two streamed upserts of msg_a = one turn (last usage wins),
        # msg_b opens the second; model reaches meta.
        s = ce.Session("/x.jsonl", budget=200_000, budget_pinned=True)
        def rec(uuid, mid, out):
            return {"type": "assistant", "uuid": uuid,
                    "timestamp": "2026-07-17T10:00:00.000Z",
                    "message": {"role": "assistant", "model": "qwen3.8",
                                "id": mid, "content": [],
                                "usage": {"input_tokens": 500,
                                          "output_tokens": out,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}}
        self.assertTrue(s.is_new_turn(rec("u1", "msg_a", 0)))
        s.feed_obj(rec("u1", "msg_a", 0))
        self.assertFalse(s.is_new_turn(rec("u2", "msg_a", 40)))
        s.feed_obj(rec("u2", "msg_a", 40))      # upsert, same turn
        s.feed_obj(rec("u3", "msg_b", 7))
        self.assertEqual(len(s.turns), 2)
        self.assertEqual(s.turns[0]["out"], 40)  # last usage won
        self.assertEqual(s.turns[1]["out"], 7)
        self.assertEqual(s.meta_payload()["model"], "qwen3.8")
        # synthetics stay filtered even though they also lack requestId
        syn = rec("u4", "msg_c", 0)
        syn["message"]["model"] = "<synthetic>"
        self.assertFalse(s.is_new_turn(syn))
        s.feed_obj(syn)
        self.assertEqual(len(s.turns), 2)


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

    def test_interim_map_sized_to_post_not_pre(self):
        # feed the fixture only THROUGH the compact_boundary: turns[-1] still
        # holds the pre-cut R (15200), so the cut-time map rebuild must size
        # itself to postTokens (2600) — not dump the whole dropped span into
        # the overhead seg (field-found: a giant overhead slab after /compact
        # that outlived the legend's corrected totals)
        s = ce.Session(FIX, budget=200_000, budget_pinned=True)
        with open(FIX, "rb") as fh:
            for raw in fh:
                s.feed_line(raw.decode("utf-8"))
                if s.compactions:
                    break
        self.assertEqual(s.resident(), 15200)      # ledger still pre-cut
        self.assertEqual(s.interim_R, 2600)
        segs = s.build_map_segs()
        self.assertEqual(sum(x["tok"] for x in segs), 2600)
        # interim overhead follows the rebase rule (R − E), not stale pre-cut
        self.assertEqual(s.overhead, 2600 - s.est_live)

    def test_map_reemitted_at_first_post_compaction_usage(self):
        # after the cut-time rebuild is drained, the first usage record must
        # flag ANOTHER full map emission (the interim map was built against
        # interim numbers) with a bumped rev so stale map_adds cannot append
        s = ce.Session(FIX, budget=200_000, budget_pinned=True)
        lines = open(FIX, "rb").read().splitlines(keepends=True)
        i = 0
        while not s.compactions:
            s.feed_line(lines[i].decode("utf-8"))
            i += 1
        self.assertTrue(s.pending["map_rebuild"])
        rev0 = s.map_rev
        s.pending = ce._fresh_pending()            # simulate the drain
        for raw in lines[i:]:
            s.feed_line(raw.decode("utf-8"))
        self.assertTrue(s.pending["map_rebuild"])
        self.assertEqual(s.map_rev, rev0 + 1)
        self.assertIsNone(s.interim_R)
        self.assertEqual(sum(x["tok"] for x in s.build_map_segs()), 2960)

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
        # two map rebuilds after ready: the interim one at the cut (sized to
        # postTokens — the ledger's R is still pre-cut there) and the
        # corrected one when the first post-compaction usage re-measures
        maps = [m for m in msgs[6:] if m["type"] == "map"]
        self.assertEqual([m["rev"] for m in maps], [1, 2])
        self.assertEqual(sum(s["tok"] for s in maps[0]["segs"]), 2600)
        # corrected map is built at a-a8's usage (R=2910); a-a8b's
        # same-request upsert to 2960 streams incrementally afterwards
        self.assertEqual(sum(s["tok"] for s in maps[1]["segs"]), 2910)
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


class TestAgentMap(unittest.TestCase):
    """Per-agent context map (SPEC b `agent.map`): a subagent's own
    sidechain transcript, laid out as its OVERVIEW map."""

    def _write_sidechain(self, path):
        # a minimal but real agent sidechain: a sidechain user prompt and one
        # assistant turn carrying usage + a Read tool_use (a file segment)
        recs = [
            {"type": "user", "isSidechain": True, "uuid": "su1",
             "timestamp": "2026-07-17T10:00:00.000Z",
             "message": {"role": "user", "content": "survey the schema"}},
            {"type": "assistant", "isSidechain": True, "uuid": "sa1",
             "timestamp": "2026-07-17T10:00:05.000Z", "requestId": "sreq1",
             "message": {"role": "assistant", "model": "claude-fable-5",
                         "content": [
                             {"type": "text",
                              "text": "reading the loader now " * 40},
                             {"type": "tool_use", "id": "tu1", "name": "Read",
                              "input": {"file_path": "/proj/loader.py"}}],
                         "usage": {"input_tokens": 500,
                                   "cache_read_input_tokens": 4000,
                                   "cache_creation_input_tokens": 200,
                                   "output_tokens": 120}}},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_build_agent_map(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "agent-x.jsonl")
            self._write_sidechain(p)
            mp = ce.build_agent_map(p, 200_000)
            self.assertIsNotNone(mp)
            self.assertEqual(mp["budget"], 200_000)
            # R = 500 + 4000 + 200 = 4700
            self.assertEqual(mp["resident"], 4700)
            self.assertTrue(mp["segs"], "map must carry segments")
            # segs sum to R, each carries the stripped {cat,tok,file} shape
            self.assertEqual(sum(s["tok"] for s in mp["segs"]), 4700)
            for s in mp["segs"]:
                self.assertEqual(set(s), {"cat", "tok", "file"})
            self.assertEqual(mp["segs"][0]["cat"], "overhead")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_transcript_yields_no_map(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "agent-empty.jsonl")
            open(p, "w").close()
            self.assertIsNone(ce.build_agent_map(p, 200_000))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_agent_payload_includes_map(self):
        # a Session's agent_payload surfaces a built map under `map`
        s = ce.Session(FIX, budget=200_000, budget_pinned=True)
        s.agents["a1"] = {
            "id": "a1", "state": "done", "turn0": 1, "ts0": "10:00:00",
            "own_tok": 4700, "t0": 0.0, "ts_last": 0.0,
            "map": {"resident": 4700, "budget": 200_000,
                    "segs": [{"cat": "overhead", "tok": 700, "file": None},
                             {"cat": "file", "tok": 4000, "file": 0}]}}
        pay = s.agent_payload("a1")
        self.assertIn("map", pay)
        self.assertTrue(pay["map"]["segs"])
        self.assertEqual(pay["map"]["resident"], 4700)
        # an agent with no map omits the key entirely (wire-null)
        s.agents["a2"] = {"id": "a2", "state": "done", "turn0": 1,
                          "ts0": "10:00:00", "own_tok": 10, "t0": 0.0,
                          "ts_last": 0.0, "map": None}
        self.assertNotIn("map", s.agent_payload("a2"))


def synth_session(turns=60, cpt=None, fit_on=True, budget=1_000_000):
    """Build a Session from a synthetic transcript whose categories tokenize at
    KNOWN, different rates. Each turn adds one user prompt, one assistant text
    and one Bash tool_use+result, and the authoritative usage R is computed
    from the true rates + a fixed server-side overhead — exactly the world the
    fit is supposed to recover."""
    cpt = cpt or {"user": 4.5, "assistant": 4.0, "bash": 2.5}
    OH = 12_000
    s = ce.Session("/nonexistent.jsonl", budget=budget, budget_pinned=True)
    s.fit_on = fit_on
    chars = {"user": 0.0, "assistant": 0.0, "bash": 0.0}
    for t in range(turns):
        ts = "2026-07-17T10:%02d:%02d.000Z" % (t // 60, t % 60)
        # varying mixes so the columns are not collinear multiples
        up = "u" * (200 + 37 * (t % 7))
        ap = "a" * (300 + 53 * (t % 5))
        bo = "b" * (900 + 613 * (t % 11))
        chars["user"] += len(up)
        chars["assistant"] += len(ap)
        R = int(OH + sum(chars[c] / cpt[c] for c in chars))
        s.feed_obj({"type": "user", "uuid": "s-u%d" % t, "timestamp": ts,
                    "message": {"role": "user", "content": up}})
        s.feed_obj({"type": "assistant", "uuid": "s-a%d" % t,
                    "requestId": "req_%d" % t, "timestamp": ts,
                    "message": {"role": "assistant", "model": "claude-fable-5",
                                "content": [{"type": "text", "text": ap}],
                                "usage": {"input_tokens": R,
                                          "output_tokens": 0,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}})
        # the bash result lands AFTER the turn's usage: it is priced by R(t+1)
        s.feed_obj({"type": "assistant", "uuid": "s-b%d" % t,
                    "requestId": "req_%d" % t, "timestamp": ts,
                    "message": {"role": "assistant", "model": "claude-fable-5",
                                "content": [{"type": "tool_use", "id": "tu%d" % t,
                                             "name": "Bash",
                                             "input": {"command": "echo"}}],
                                "usage": {"input_tokens": R,
                                          "output_tokens": 0,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}})
        s.feed_obj({"type": "user", "uuid": "s-r%d" % t, "timestamp": ts,
                    "message": {"role": "user",
                                "content": [{"type": "tool_result",
                                             "tool_use_id": "tu%d" % t,
                                             "content": bo}]}})
        # the tool_result's own JSON framing counts too: take the engine's
        # own char tally for bash rather than guessing it
        chars["bash"] = s.cat_chars["bash"]
    return s


class TestCategoryFit(unittest.TestCase):
    """The per-category ratio fit (SPEC d): estimated numbers get better, the
    authoritative ones never move."""

    def test_solve_small_system(self):
        A = [[2.0, 1.0, -1.0], [-3.0, -1.0, 2.0], [-2.0, 1.0, 2.0]]
        x = ce._solve(A, [8.0, -11.0, -3.0])
        for got, want in zip(x, [2.0, 3.0, -1.0]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertIsNone(ce._solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]))

    def test_fit_recovers_known_ratios(self):
        # noiseless design with independent variation per category
        true = {"user": 1 / 4.5, "file": 1 / 2.8, "bash": 1 / 3.2}
        rows = []
        for t in range(60):
            ch = {"user": 1000.0 * (1 + (t % 7)), "file": 900.0 * (1 + (t % 5)),
                  "bash": 700.0 * (1 + (t % 11))}
            rows.append((int(9000 + sum(ch[c] * true[c] for c in true)), ch))
        f = ce._fit_rows(rows, 3.8, 0.0, lam=1e-3)   # ridge out of the way
        self.assertIsNotNone(f)
        for c, r in true.items():
            self.assertAlmostEqual(1.0 / f["inv"][c], 1.0 / r, delta=0.25)
        self.assertAlmostEqual(f["intercept"], 9000, delta=600)
        # a category that never appears stays on the prior, untouched
        self.assertAlmostEqual(f["inv"]["thinking"],
                               ce._scale_fit(rows, 3.8)["k"], places=9)
        # with the shipped ridge the same fit shrinks toward the session's own
        # global rate instead — never past it, never anywhere absurd
        g = ce._fit_rows(rows, 3.8, 0.0)
        k = ce._scale_fit(rows, 3.8)["k"]
        for c in true:
            self.assertLessEqual(abs(g["inv"][c] - k),
                                 abs(f["inv"][c] - k) + 1e-9)

    def test_fit_bounds_are_enforced(self):
        rows = []
        for t in range(40):
            ch = {"file": 1000.0 * (1 + t % 9), "user": 300.0 * (1 + t % 4)}
            # absurd rate: 0.5 chars/token, far outside the plausible band
            rows.append((int(500 + ch["file"] / 0.5 + ch["user"] / 6.0), ch))
        f = ce._fit_rows(rows, 3.8, 0.0)
        self.assertIsNotNone(f)
        for c in ("file", "user"):
            cpt = 1.0 / f["inv"][c]
            self.assertGreaterEqual(cpt, ce.FIT_MIN_CPT - 1e-9)
            self.assertLessEqual(cpt, ce.FIT_MAX_CPT + 1e-9)
        self.assertIn("file", f["clamped"])

    def test_gate_falls_back_when_too_few_turns(self):
        rows = [(1000 + 10 * t, {"user": 100.0 * t}) for t in range(5)]
        st = ce.fit_cats(rows)
        self.assertFalse(st["active"])
        self.assertEqual(st["mode"], "prior")
        self.assertIn("too few turns", st["reason"])

    def test_gate_reports_holdout_scores(self):
        _, rows = None, [(int(9000 + 1000.0 * (1 + t % 7) / 4.5),
                          {"user": 1000.0 * (1 + t % 7)}) for t in range(60)]
        st = ce.fit_cats(rows)
        self.assertTrue(st["active"])
        self.assertIn("holdout_prior_pct", st)
        self.assertIn("holdout_fit_pct", st)
        self.assertLessEqual(st["holdout_fit_pct"], st["holdout_prior_pct"])
        rep = ce.fit_report(st)
        self.assertIn("cpt", rep)
        self.assertIn(rep["mode"], ("cats", "scale"))

    def test_session_fit_activates_and_beats_the_constant(self):
        s = synth_session()
        self.assertTrue(s.fit_state["active"], s.fit_state["reason"])
        self.assertLess(s.fit_state["holdout_fit_pct"],
                        s.fit_state["holdout_prior_pct"])
        self.assertGreater(s.fit_state["r2"], 0.99)

    def test_map_still_sums_to_R_exactly_with_a_fit(self):
        s = synth_session()
        self.assertIsNotNone(s.fit)
        segs = s.build_map_segs()
        self.assertEqual(sum(x["tok"] for x in segs), s.resident())
        self.assertEqual(segs[0]["cat"], "overhead")

    def test_fit_off_is_the_old_behaviour_exactly(self):
        on, off = synth_session(), synth_session(fit_on=False)
        self.assertIsNone(off.fit)
        self.assertEqual(off.fit_state["reason"], "disabled")
        # pre-fit calibration path, unchanged: live_est() IS est_live and the
        # honesty rule reproduces R from overhead + est x alpha
        self.assertEqual(off.live_est(), off.est_live)
        R = off.resident()
        self.assertEqual(sum(x["tok"] for x in off.build_map_segs()), R)
        # same authoritative numbers either way — only the SPLIT moves
        self.assertEqual(on.resident(), off.resident())
        self.assertEqual([t["resident"] for t in on.turns],
                         [t["resident"] for t in off.turns])
        self.assertNotEqual(on.cats_payload(), off.cats_payload())

    def test_fit_is_deterministic_under_replay(self):
        a, b = synth_session(), synth_session()
        self.assertEqual(a.fit_payload(), b.fit_payload())
        # a clone (checkpoint/seek path) carries the fit forward untouched
        c = a.clone()
        self.assertEqual(c.fit_payload(), a.fit_payload())
        self.assertEqual(c.build_map_segs(), a.build_map_segs())

    def test_cal_sets_the_prior(self):
        old = ce.Est.chars_per_tok
        try:
            ce.Est.chars_per_tok = 3.0
            s = synth_session()
            self.assertEqual(s.fit_state["prior_cpt"], 3.0)
        finally:
            ce.Est.chars_per_tok = old

    def test_report_carries_the_calibration(self):
        s = synth_session()
        rep = ce.build_report(s)
        ft = rep["context"]["fit"]
        self.assertTrue(ft["active"])
        self.assertIn("cpt", ft)
        self.assertIn("overhead", ft)
        md = ce.render_report_md(rep)
        self.assertIn("token ratios:", md)
        self.assertIn("chars/tok", md)

    def test_map_payload_carries_the_fit(self):
        s = synth_session()
        m = s.map_payload()
        self.assertIn("fit", m)
        self.assertTrue(m["fit"]["active"])
        self.assertEqual(sum(x["tok"] for x in m["segs"]), s.resident())


class TestFleetPeek(unittest.TestCase):
    """transcript_tail_msgs — the fleet quicklook's conversation-tail read."""

    def _write(self, records):
        import tempfile
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in records:
            fh.write(json.dumps(r) + "\n")
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    @staticmethod
    def _u(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    @staticmethod
    def _a(text):
        return {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}]}}

    def test_tail_roles_and_order(self):
        p = self._write([self._u("fix the bug"), self._a("done — tests pass")])
        msgs = ce.transcript_tail_msgs(p)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["text"], "fix the bug")

    def test_wrappers_and_tool_records_skipped(self):
        p = self._write([
            {"type": "user", "isMeta": True,
             "message": {"role": "user", "content": "caveat"}},
            self._u("<command-name>/help</command-name>"),
            # a real prompt sharing its record with a system-reminder block
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>noise</system-reminder>"},
                {"type": "text", "text": "the actual prompt"}]}},
            # tool-use-only assistant record: no text blocks
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {}}]}},
            self._a("reply"),
        ])
        msgs = ce.transcript_tail_msgs(p)
        self.assertEqual([m["text"] for m in msgs], ["the actual prompt", "reply"])

    def test_consecutive_same_role_merge(self):
        p = self._write([self._u("go"), self._a("part one"), self._a("part two")])
        msgs = ce.transcript_tail_msgs(p)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["text"], "part one\npart two")

    def test_unreadable_is_none_and_cap(self):
        self.assertIsNone(ce.transcript_tail_msgs("/nonexistent/x.jsonl"))
        p = self._write([self._u("m%d" % i) if i % 2 else self._a("r%d" % i)
                         for i in range(40)])
        self.assertEqual(len(ce.transcript_tail_msgs(p, max_msgs=12)), 12)


class TestFleetRows(unittest.TestCase):
    """SPEC f2: fleet-row status ladder and per-row budget."""

    def test_status_ladder(self):
        now = 1000.0
        # dead pid wins over everything
        self.assertEqual(ce.fleet_row_status("busy", False, now, now), "dead")
        # roster value verbatim while fresh
        self.assertEqual(ce.fleet_row_status("busy", True, now - 10, now),
                         "busy")
        self.assertEqual(ce.fleet_row_status("idle", True, now - 999, now),
                         "idle")
        self.assertEqual(ce.fleet_row_status("shell", True, now - 999, now),
                         "shell")
        # busy + transcript quiet > 120 s => stalled
        self.assertEqual(ce.fleet_row_status("busy", True, now - 121, now),
                         "stalled")
        # unknown transcript (mtime 0) never stalls
        self.assertEqual(ce.fleet_row_status("busy", True, 0.0, now), "busy")
        # absent roster status defaults idle
        self.assertEqual(ce.fleet_row_status(None, True, now, now), "idle")

    def test_row_budget_bumps_to_fit_resident(self):
        b200, b1m = ce.BUDGET_RUNGS
        self.assertEqual(ce.fleet_budget(b200, None), b200)
        self.assertEqual(ce.fleet_budget(b200, 150_000), b200)
        self.assertEqual(ce.fleet_budget(b200, 600_000), b1m)   # the fix
        self.assertEqual(ce.fleet_budget(b200, 2_000_000), b1m)  # clamp
        self.assertEqual(ce.fleet_budget(b1m, 100_000), b1m)     # never down

    def test_codex_tail_parse(self):
        # SPEC f2 providers: event-precise status + usage from rollout tails
        def ev(ts, ptype, **kw):
            return json.dumps({"timestamp": ts, "type": "event_msg",
                               "payload": dict({"type": ptype}, **kw)})
        busy = [ev("T1", "task_started"),
                ev("T2", "token_count", info={
                    "last_token_usage": {"input_tokens": 19070},
                    "model_context_window": 258400}),
                ev("T3", "user_message", message="fix the bug")]
        got = ce.codex_tail_parse(busy)
        self.assertEqual(got["status"], "busy")
        self.assertEqual(got["resident"], 19070)
        self.assertEqual(got["budget"], 258400)
        self.assertEqual(got["last_prompt"], "fix the bug")
        # task_complete after the start => idle
        idle = busy + [ev("T4", "task_complete")]
        self.assertEqual(ce.codex_tail_parse(idle)["status"], "idle")
        # malformed lines and unknown events are skipped, not fatal
        noisy = ["not json", ev("T5", "web_search_end")] + busy
        self.assertEqual(ce.codex_tail_parse(noisy)["status"], "busy")

    def test_fleet_mode_streams_json(self):
        # smoke: --fleet emits at least one parseable fleet line, then dies
        # cleanly within one poll of its consumer closing the pipe (SPEC f2:
        # quiet ticks heartbeat, so the closed pipe is always noticed).
        proc = subprocess.Popen(
            [PY, os.path.join(ROOT, "amtr_engine.py"),
             "--fleet", "--poll-secs", "0.5", "--live-only"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            line = proc.stdout.readline()
            msg = json.loads(line)
            self.assertEqual(msg["type"], "fleet")
            self.assertIsInstance(msg["sessions"], list)
            for s in msg["sessions"][:5]:
                self.assertIn("status", s)
                self.assertIn("budget", s)
                self.assertTrue(s["live"])
        finally:
            proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("--fleet did not exit on closed pipe")


# ------------------------------------------------------------ providers
CX_FIX = os.path.join(ROOT, "tests", "fixtures", "codex-rollout.jsonl")
GM_FIX = os.path.join(ROOT, "tests", "fixtures", "gemini-session.jsonl")


def _feed(path):
    s = ce.Session(path)
    with open(path, encoding="utf-8") as fh:
        off = 0
        for line in fh:
            s.feed_line(line, off)
            off += len(line.encode("utf-8"))
    return s


class TestProviders(unittest.TestCase):
    """Codex CLI and Gemini CLI transcripts through the adapters (SPEC f2 →
    attach). Fixtures mirror the real recorders' shapes with synthetic
    content; every number below is derived by hand:
      codex turn0 usage: input 1000 / cached 800 / out 50 → in 200 · cr 800
      codex turn1: 1500 / 1100 / 80 → 400 · 1100 · 80;  turn2: 400 / 0 / 10
      gemini turn0 tokens input 5000 cached 4000 output 40 thoughts 20 →
        in 1000 · cr 4000 · out 60 (output + thoughts);  last turn 900/0/4
    """

    def test_detect_provider(self):
        self.assertEqual(ce.detect_provider(CX_FIX), "claude") if False else None
        # by path
        self.assertEqual(ce.detect_provider("/x/.codex/sessions/2026/08/01/rollout-2026-08-01T10-00-00-abc.jsonl"), "codex")
        self.assertEqual(ce.detect_provider("/x/.gemini/tmp/proj/chats/session-2026-08-01T10-00-abcdefgh.jsonl"), "gemini")
        self.assertEqual(ce.detect_provider("/x/.claude/projects/-p/uuid.jsonl"), "claude")
        # by first line (fixtures live outside the CLIs' dirs)
        self.assertEqual(ce.detect_provider(CX_FIX), "codex")
        self.assertEqual(ce.detect_provider(GM_FIX), "gemini")
        self.assertEqual(ce.detect_provider(FIX), "claude")

    def test_codex_turns_and_meta(self):
        s = _feed(CX_FIX)
        self.assertEqual(s.provider, "codex")
        self.assertEqual(s.session_id, "cx-fixture-0001")
        self.assertEqual(s.project, "/home/dev/proj")
        self.assertEqual(s.model, "gpt-5.6-sol")
        self.assertEqual(s.budget, 258400)          # model_context_window
        self.assertEqual(s.display_name(), "proj-cx")
        self.assertEqual(s.meta_payload()["provider"], "codex")
        # three requests → three turns; the identical rate-limit refresh
        # (second token_count, nothing buffered) opened none
        got = [(t["in"], t["cr"], t["cc"], t["out"], t["resident"]) for t in s.turns]
        self.assertEqual(got, [(200, 800, 0, 50, 1000),
                               (400, 1100, 0, 80, 1500),
                               (400, 0, 0, 10, 400)])
        self.assertEqual(s.resident(), 400)

    def test_codex_tools_files_shell_retrieval_agents(self):
        s = _feed(CX_FIX)
        # exec_command → SHELL console entry, exit parsed from the output text
        self.assertEqual(len(s.cmds), 1)
        c = s.cmds[0]
        self.assertEqual(c["cmd"], "ls")
        self.assertTrue(c["ok"])
        self.assertEqual(c["out"], "Makefile\nsrc")
        # apply_patch "Add File" → a Write on the file
        f = list(s.files.values())
        self.assertEqual([(x["path"], x["writes"], x["edits"], x["reads"]) for x in f],
                         [("/home/dev/proj/build.sh", 1, 0, 0)])
        # web_search_end → retrieval feed entry with the result count
        self.assertEqual([(r["kind"], r["q"], r["n"]) for r in s.rets],
                         [("search", "make tutorial", 2)])
        # sub_agent_activity started → a running agent named by its path
        self.assertEqual([(a["id"], a["state"], a["agent_type"]) for a in s.agents.values()],
                         [("cx-agent-0001", "running", "reviewer")])
        # view_image output: the base64 payload is priced as ONE image, not
        # as 5k tokens of text (20000 chars / 3.8 would be 5263)
        img = [g for g in s.ring.values() if g["uuid"] == "fco-2"]
        self.assertEqual(len(img), 0)   # evicted by the compaction below
        # …so check it in the compaction's dropped tool total instead: the
        # tool_result blocks that were dropped sum well under 5k
        self.assertLess(s.compactions[0]["dropped_cats"].get("tool", 0), 2000)

    def test_codex_compaction(self):
        s = _feed(CX_FIX)
        self.assertEqual(len(s.compactions), 1)
        c = s.compactions[0]
        # pre = R at the cut (turn1's resident); survivors = the two ids in
        # replacement_history; post = the kept history's estimate
        self.assertEqual(c["pre"], 1500)
        self.assertEqual(c["preserved_msgs"], 2)
        self.assertGreater(c["post"], 0)
        self.assertLess(c["post"], 100)
        self.assertEqual(c["dropped"], c["pre"] - c["post"])
        live = sorted(g["uuid"] for g in s.ring.values())
        # kept: both user prompts, the post-cut assistant text, and turn1's
        # hidden reasoning (allocated when turn2 opened, after the cut)
        self.assertEqual(live, ["msg-a-3", "msg-u-1", "msg-u-2", "reasoning-t1"])
        # the compaction event landed in the ledger
        self.assertTrue(any(e["kind"] == "compaction" for e in s.events))

    def test_codex_replay(self):
        s = _feed(CX_FIX)
        # state at the end of turn 0: one request, its prompt + assistant
        # text + shell result all resident, no compaction yet
        c0 = s.state_at_turn(0)
        self.assertEqual(len(c0.turns), 1)
        self.assertEqual(c0.resident(), 1000)
        self.assertEqual(c0.compactions, [])
        self.assertIn("msg-u-1", [g["uuid"] for g in c0.ring.values()])
        self.assertIn("fco-1", [g["uuid"] for g in c0.ring.values()])
        # end of turn 1 = everything up to turn 2's open, which is AFTER
        # the compaction cut (the Claude replay law, unchanged)
        c1 = s.state_at_turn(1)
        self.assertEqual(c1.resident(), 1500)
        self.assertEqual(len(c1.compactions), 1)
        self.assertNotIn("fco-1", [g["uuid"] for g in c1.ring.values()])

    def test_gemini_turns_and_meta(self):
        s = _feed(GM_FIX)
        self.assertEqual(s.provider, "gemini")
        self.assertEqual(s.session_id, "gm-fixture-0001")
        self.assertEqual(s.model, "gemini-2.5-pro")
        self.assertEqual(s.budget, 1048576)         # tokenLimit(gemini-2.5-pro)
        got = [(t["in"], t["cr"], t["out"], t["resident"]) for t in s.turns]
        self.assertEqual(got, [(1000, 4000, 60, 5000), (100, 5000, 12, 5100),
                               (100, 5100, 20, 5200), (100, 5200, 9, 5300),
                               (900, 0, 4, 900)])

    def test_gemini_upsert_tools_and_compression(self):
        s = _feed(GM_FIX)
        # the tool-call message was appended twice (executing, then success
        # + tokens): ONE turn, ONE shell entry, the result priced once
        self.assertEqual(len(s.cmds), 1)
        self.assertEqual((s.cmds[0]["cmd"], s.cmds[0]["ok"], s.cmds[0]["out"]),
                         ("ls", True, "Makefile\nsrc"))
        self.assertEqual([(x["path"], x["reads"]) for x in s.files.values()],
                         [("/home/dev/proj/Makefile", 1)])
        # $set.messages replaced the history: everything before is gone,
        # the synthetic summary pair + what followed is what remains
        self.assertEqual(len(s.compactions), 1)
        c = s.compactions[0]
        self.assertEqual(c["pre"], 5300)
        self.assertEqual(c["preserved_msgs"], 0)
        self.assertEqual(c["dropped"], c["pre"] - c["post"])
        live = sorted(g["uuid"] for g in s.ring.values())
        self.assertEqual(live, ["g-a-5", "g-s-1", "g-s-2", "g-u-3", "reasoning-t3"])

    def test_gemini_rewind(self):
        # $rewindTo truncates the history from that id: a manual compaction
        # whose survivors are the ids before it
        lines = [json.dumps(x) for x in [
            {"sessionId": "gm-rw", "projectHash": "h", "startTime": "2026-08-01T00:00:00Z", "kind": "main"},
            {"id": "u1", "timestamp": "2026-08-01T00:00:01Z", "type": "user", "content": "one"},
            {"id": "a1", "timestamp": "2026-08-01T00:00:02Z", "type": "gemini", "content": "reply one",
             "tokens": {"input": 100, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 105}, "model": "gemini-2.5-flash"},
            {"id": "u2", "timestamp": "2026-08-01T00:00:03Z", "type": "user", "content": "two"},
            {"id": "a2", "timestamp": "2026-08-01T00:00:04Z", "type": "gemini", "content": "reply two",
             "tokens": {"input": 120, "output": 5, "cached": 100, "thoughts": 0, "tool": 0, "total": 125}, "model": "gemini-2.5-flash"},
            {"$rewindTo": "u2"},
        ]]
        s = ce.Session("/tmp/nowhere/session-x.jsonl", provider="gemini")
        for ln in lines:
            s.feed_line(ln, 0)
        self.assertEqual(len(s.compactions), 1)
        self.assertEqual(s.compactions[0]["trigger"], "manual")
        self.assertEqual(sorted(g["uuid"] for g in s.ring.values()), ["a1", "u1"])

    def test_tail_parsers_and_quicklook(self):
        with open(GM_FIX, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        info = ce.gemini_tail_parse(lines)
        self.assertEqual(info["status"], "idle")     # last record: gemini + tokens
        self.assertEqual(info["resident"], 900)
        self.assertEqual(info["budget"], 1048576)
        self.assertEqual(info["last_prompt"], "thanks")
        # a trailing user message → busy
        info2 = ce.gemini_tail_parse(lines + [json.dumps(
            {"id": "u9", "timestamp": "t", "type": "user", "content": "more?"})])
        self.assertEqual(info2["status"], "busy")
        with open(CX_FIX, encoding="utf-8") as fh:
            cx = ce.codex_tail_parse(fh.read().splitlines())
        self.assertEqual(cx["status"], "idle")
        self.assertTrue(cx["ended"])
        self.assertEqual(cx["resident"], 400)
        # quicklook tails, provider-aware
        msgs = ce.transcript_tail_msgs(CX_FIX)
        # both prompts and both answers, harness injections (the developer
        # record) filtered; the two assistant chunks of task 1 merged
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "user", "assistant"])
        self.assertIn("build script", msgs[0]["text"])
        self.assertIn("Done: build.sh added.", msgs[1]["text"])
        self.assertEqual(msgs[3]["text"], "Compacted.")
        gm = ce.transcript_tail_msgs(GM_FIX)
        self.assertEqual(gm[-1], {"role": "assistant", "text": "Any time."})
        self.assertEqual(gm[-2], {"role": "user", "text": "thanks"})

    def test_provider_agent_map(self):
        # a Codex rollout as an agent's own transcript builds a mini-map
        mp = ce.build_agent_map(CX_FIX, 200_000)
        self.assertIsNotNone(mp)
        self.assertEqual(mp["resident"], 400)
        self.assertEqual(mp["budget"], 258400)
        self.assertTrue(any(sg["cat"] == "user" for sg in mp["segs"]))


class TestLocalBackendProbe(unittest.TestCase):
    def test_env_kv_ignores_args(self):
        kv = ce._env_kv(["/usr/bin/claude", "--model", "qwen3.8",
                         "ANTHROPIC_BASE_URL=http://h:11434",
                         "path=/lower/case/skipped",
                         "ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3.8"])
        self.assertEqual(kv["ANTHROPIC_BASE_URL"], "http://h:11434")
        self.assertEqual(kv["ANTHROPIC_DEFAULT_SONNET_MODEL"], "qwen3.8")
        self.assertNotIn("path", kv)

    def test_ollama_pick_matches_tag_stripped(self):
        models = [{"name": "llama3:8b"}, {"name": "qwen3.8:latest"}]
        self.assertEqual(ce._ollama_pick(models, "qwen3.8")["name"],
                         "qwen3.8:latest")
        self.assertIsNone(ce._ollama_pick(models, "mistral"))

    def test_backend_from_ps_entry(self):
        # /api/ps shape: served (effective) context_length at top level
        e = {"name": "qwen3.8:latest",
             "details": {"parameter_size": "27.3B",
                         "quantization_level": "Q4_K_M"},
             "context_length": 65536}
        b = ce._backend_from_entry("http://h:11434", e, True)
        self.assertEqual(b, {"kind": "ollama", "url": "http://h:11434",
                             "params": "27.3B", "quant": "Q4_K_M",
                             "ctx": 65536, "loaded": True})

    def test_backend_from_show_entry(self):
        # /api/show shape: max window under model_info.<family>.context_length
        e = {"details": {"parameter_size": "8B", "quantization_level": "Q8_0"},
             "model_info": {"qwen3.context_length": 40960}}
        b = ce._backend_from_entry("http://h:11434", e, False)
        self.assertEqual((b["ctx"], b["loaded"]), (40960, False))

    def test_truncation_event_at_served_window(self):
        # served window 65536 -> margin 1310 (2%): warn crossing 64226,
        # once (hysteresis), re-armed only after relief below 90%
        s = ce.Session("/x.jsonl", budget=65536, budget_pinned=True)
        s.backend = {"kind": "ollama", "url": "u", "params": "27.3B",
                     "quant": "Q4_K_M", "ctx": 65536, "loaded": True}
        def turn(rid, r_in):
            s.feed_obj({"type": "assistant", "uuid": "u-" + rid,
                        "requestId": rid,
                        "timestamp": "2026-07-17T11:00:00.000Z",
                        "message": {"role": "assistant", "model": "qwen3.8",
                                    "content": [],
                                    "usage": {"input_tokens": r_in,
                                              "output_tokens": 1,
                                              "cache_read_input_tokens": 0,
                                              "cache_creation_input_tokens": 0}}})
        def truncs():
            return [e for e in s.events if e["kind"] == "truncation"]
        turn("r1", 60_000)
        self.assertEqual(len(truncs()), 0)
        turn("r2", 64_300)              # inside the margin -> event
        self.assertEqual(len(truncs()), 1)
        turn("r3", 65_000)              # still inside -> no duplicate
        self.assertEqual(len(truncs()), 1)
        turn("r4", 50_000)              # relief below 90% re-arms
        turn("r5", 65_000)
        self.assertEqual(len(truncs()), 2)

    def test_no_truncation_without_backend(self):
        # an Anthropic session at 99% of budget must NOT truncation-warn:
        # pressure covers it, compaction resolves it
        s = ce.Session("/x.jsonl", budget=200_000, budget_pinned=True)
        s.feed_obj({"type": "assistant", "uuid": "u-r1", "requestId": "r1",
                    "timestamp": "2026-07-17T11:00:00.000Z",
                    "message": {"role": "assistant", "model": "claude-fable-5",
                                "content": [],
                                "usage": {"input_tokens": 199_000,
                                          "output_tokens": 1,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}})
        self.assertEqual(
            [e for e in s.events if e["kind"] == "truncation"], [])

    def test_tail_usage_returns_model_and_accepts_local(self):
        import tempfile
        rec = {"type": "assistant", "uuid": "u1",
               "message": {"role": "assistant", "model": "qwen3.8",
                           "id": "msg_a",
                           "usage": {"input_tokens": 500, "output_tokens": 9,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0}}}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write(json.dumps(rec) + "\n")
            p = fh.name
        try:
            self.assertEqual(ce.tail_usage(p), (500, "qwen3.8"))
        finally:
            os.unlink(p)

    def test_row_budget_inherits_served_window(self):
        import types
        eng = types.SimpleNamespace(budget=200_000, budget_pinned=False,
                                    _backend_ctx={"qwen3.8": 65536})
        rb = types.MethodType(ce.Engine._row_budget, eng)
        self.assertEqual(rb(57_000, "qwen3.8"), 65536)      # probed window
        self.assertEqual(rb(57_000, "claude-fable-5"), 200_000)
        self.assertEqual(rb(57_000, ""), 200_000)
        self.assertEqual(rb(57_000, "mistral"), 200_000)    # never probed

    def test_proxy_compose_and_itemization(self):
        body = json.dumps({
            "model": "qwen3.8", "max_tokens": 100,
            "system": "S" * 4000,
            "tools": [{"name": "Bash", "input_schema": {}},
                      {"name": "Read", "input_schema": {}}],
            "messages": [{"role": "user", "content": "hi there"}],
        }).encode()
        rec = ce.proxy_compose(body)
        self.assertEqual(rec["model"], "qwen3.8")
        self.assertEqual(rec["system_chars"], 4000)
        self.assertEqual(rec["tools_n"], 2)
        self.assertEqual(rec["total_chars"],
                         rec["system_chars"] + rec["tools_chars"]
                         + rec["msgs_chars"])
        # itemization scales parts by the one measured chars/token ratio
        rec["input_tokens"] = rec["total_chars"] // 4
        line = ce.proxy_itemization(rec)
        self.assertIn("system prompt ≈1.0k tok", line)
        self.assertIn("2 tool schemas", line)
        # non-message bodies (e.g. /api/show probes) never record
        self.assertIsNone(ce.proxy_compose(b'{"model": "x"}'))
        self.assertIsNone(ce.proxy_compose(b"not json"))

    def test_latest_proxy_record_filters_model_and_age(self):
        import tempfile
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        rows = [
            {"ts": now.isoformat(), "model": "other", "total_chars": 1},
            {"ts": (now - timedelta(hours=2)).isoformat(),
             "model": "qwen3.8", "total_chars": 2},   # stale
            {"ts": now.isoformat(), "model": "qwen3.8", "total_chars": 3},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
            p = fh.name
        old = ce.PROXY_LOG
        ce.PROXY_LOG = p
        try:
            self.assertEqual(
                ce.latest_proxy_record("qwen3.8")["total_chars"], 3)
            self.assertIsNone(ce.latest_proxy_record("mistral"))
        finally:
            ce.PROXY_LOG = old
            os.unlink(p)

    def test_meta_carries_backend(self):
        s = ce.Session("/x.jsonl", budget=200_000)
        self.assertIsNone(s.meta_payload()["backend"])
        s.backend = {"kind": "ollama", "url": "u", "params": "27.3B",
                     "quant": "Q4_K_M", "ctx": 65536, "loaded": True}
        self.assertEqual(s.meta_payload()["backend"]["ctx"], 65536)


if __name__ == "__main__":
    unittest.main(verbosity=2)
