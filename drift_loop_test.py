#!/usr/bin/env python3
"""
drift_loop_test.py — deterministic tests for the file-driven drift loop.

The model is stubbed (no server, no network): drift_loop.chat_stream is
monkeypatched with scripted replies, so every test is reproducible. Runs the
real gates, breakers, ledger, residence files, resume, watch, and determinism
battery against those replies.

Run: python3 drift_loop_test.py
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain_cascade
import drift_loop
from drift_loop import (
    Breaker,
    DriftRun,
    _extract_json,
    _gate_graph,
    _gate_rewrite,
    _objective_anchors,
    _canonical_hash,
)
from agent_runtime import load_config

# Never write the shipped residence from a test (see test_support.py).
import test_support
test_support.isolate_residence()

GOOD_GRAPH = {
    "graph": [
        {"step": 1, "action": "list_dir", "target": "."},
        {"step": 2, "action": "verify", "target": "ledger"},
    ]
}
GOOD_GRAPH_JSON = json.dumps(GOOD_GRAPH)

# A rewrite that keeps the objective's anchor words.
GOOD_REWRITE = {
    "instructions": (
        "Maintain a coherent self-rewriting cognitive loop that preserves its "
        "stated objective across one thousand continuous cycles without "
        "cognitive drift or infinite recursion."
    ),
    "rationale": "kept the anchors, tightened the phrasing",
}
GOOD_REWRITE_JSON = json.dumps(GOOD_REWRITE)


def _config(res):
    env = {
        "LOCAL_MODEL_URL": "http://stub/v1",
        "LOCAL_MODEL": "stub-model",
        "DRIVE_RESIDENCE": res,
        "EVIDENCE_FILE": "",  # keep tests quiet / no side files
    }
    return load_config(env)


class StubModel:
    """Feeds scripted replies to drift_loop.chat_stream."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, config, messages, temperature=None, max_tokens=None, timeout_s=None, stop_when=None):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        if not self.replies:
            return {"content": "", "elapsed": 0.0, "tokens": 0, "early_stop": False,
                    "truncated": False}
        r = self.replies.pop(0)
        base = {"elapsed": 0.5, "tokens": 20, "early_stop": True, "truncated": False}
        if isinstance(r, dict):
            # scripted result override — used to simulate a budget-truncated call
            base.update(r)
            return base
        base["content"] = r
        return base


class DriftLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drift-test-")
        self.res = os.path.join(self.tmp, "residence")
        os.makedirs(self.res)
        self.config = _config(self.res)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, objective=None, state=None):
        return DriftRun(
            self.config,
            objective or drift_loop.DEFAULT_OBJECTIVE,
            self.res,
            state=state,
        )

    @contextlib.contextmanager
    def _isolated_cascade(self):
        """Ambient provider keys and the real health file must never leak into a
        drift test — the snapshot reads whichever health file the process has."""
        saved = dict(os.environ)
        os.environ["BRAIN_HEALTH_FILE"] = os.path.join(self.tmp, "provider-health.json")
        for provider in brain_cascade.PROVIDER_REGISTRY:
            for var in provider["key_envs"]:
                os.environ.pop(var, None)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def _one_cycle(self):
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            return self._run().cycle()
        finally:
            drift_loop.chat_stream = original

    def _ledger_records(self):
        with open(os.path.join(self.res, "ledger.jsonl")) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    # ── unit gates ─────────────────────────────────────────────────────────

    def test_ledger_records_the_cascade_health_snapshot(self):
        """Every cycle carries the cascade's health, so a provider outage is a
        diff between two ledger lines instead of an archaeology exercise."""
        with self._isolated_cascade():
            self.assertIsInstance(self._one_cycle(), dict)
            recs = self._ledger_records()
        snap = recs[0]["cascade"]
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["chain"], ["local"])  # no keys in this test
        self.assertEqual(snap["cooling"], {})

    def test_a_benched_provider_shows_up_in_the_ledger_timeline(self):
        with self._isolated_cascade():
            os.environ["GROQ_API_KEY"] = "gsk_test"
            brain_cascade.note_failure("groq", "429 rate limit")
            self.assertIsInstance(self._one_cycle(), dict)
            recs = self._ledger_records()
        snap = recs[0]["cascade"]
        self.assertEqual(snap["status"], "degraded")
        self.assertEqual(snap["chain"], ["local", "groq"])
        self.assertEqual(snap["cooling"]["groq"]["class"], "rate-limit")
        self.assertGreater(snap["cooling"]["groq"]["cooldown_s"], 0)
        self.assertNotIn("gsk_test", json.dumps(recs[0]))  # names only, never keys

    def test_gate_graph_accepts_allowlisted(self):
        ok, sig = _gate_graph(GOOD_GRAPH)
        self.assertTrue(ok)
        self.assertEqual(sig, "graph:ok")

    def test_gate_graph_rejects_unknown_action(self):
        ok, sig = _gate_graph({"graph": [{"step": 1, "action": "rm", "target": "/"}]})
        self.assertFalse(ok)
        self.assertEqual(sig, "graph:action-not-allowlisted")

    def test_gate_rewrite_accepts_anchors(self):
        ok, sig, retention = _gate_rewrite(
            json.loads(GOOD_REWRITE_JSON), drift_loop.DEFAULT_OBJECTIVE
        )
        self.assertTrue(ok)
        self.assertGreaterEqual(retention, drift_loop.RETENTION_FLOOR)

    def test_gate_rewrite_rejects_objective_drift(self):
        rw = {"instructions": "Build a web scraper for news sites.", "rationale": "x"}
        ok, sig, retention = _gate_rewrite(rw, drift_loop.DEFAULT_OBJECTIVE)
        self.assertFalse(ok)
        self.assertEqual(sig, "rewrite:objective-drift")
        self.assertLess(retention, drift_loop.RETENTION_FLOOR)

    def test_gate_rewrite_rejects_guard_words(self):
        rw = {"instructions": "Raise the sandbox memory limit to 8GB.", "rationale": "x"}
        ok, sig, _ = _gate_rewrite(rw, drift_loop.DEFAULT_OBJECTIVE)
        self.assertFalse(ok)
        self.assertEqual(sig, "rewrite:guard-word")

    def test_extract_json_from_rambling(self):
        text = "Sure! Here you go: " + GOOD_GRAPH_JSON + " and that's it."
        self.assertEqual(_extract_json(text), GOOD_GRAPH)

    def test_canonical_hash_stable(self):
        self.assertEqual(_canonical_hash(GOOD_GRAPH), _canonical_hash(GOOD_GRAPH))

    def test_breaker_consecutive_trips(self):
        b = Breaker()
        self.assertIsNone(b.record("graph:missing-key", False))
        trip = b.record("graph:missing-key", False)
        self.assertIsNotNone(trip)
        self.assertIn("TRIP", trip)

    def test_breaker_distinct_trips(self):
        b = Breaker()
        self.assertIsNone(b.record("graph:a", False))
        self.assertIsNone(b.record("rewrite:b", False))
        trip = b.record("graph:c", False)
        self.assertIsNotNone(trip)
        self.assertIn("distinct-failures", trip)

    def test_breaker_resets_on_success(self):
        b = Breaker()
        b.record("graph:missing-key", False)
        b.record("graph:missing-key", True)  # success clears the streak
        self.assertIsNone(b.record("graph:missing-key", False))

    # ── full cycle against stub ────────────────────────────────────────────

    def test_one_cycle_happy_path(self):
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            r = self._run().cycle()
        finally:
            drift_loop.chat_stream = original
        self.assertIsInstance(r, dict)
        self.assertEqual(r["gate"], "accepted")
        self.assertEqual(r["cycle"], 1)
        # residence files written
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["cycle"], 1)
        self.assertEqual(st["phase"], "done")
        self.assertIn("coherent", st["instructions"])
        # instructions.md mirrors the accepted rewrite
        with open(os.path.join(self.res, "instructions.md")) as f:
            self.assertIn("coherent", f.read())
        # ledger has one line
        with open(os.path.join(self.res, "ledger.jsonl")) as f:
            rec = json.loads(f.readline().strip())
        self.assertEqual(rec["cycle"], 1)
        self.assertTrue(rec["graph_ok"])
        self.assertTrue(rec["rewrite_ok"])
        self.assertIsNotNone(rec["graph_hash"])
        # machine-computed QIH metrics block (QIH.md §II): stub replies report
        # 20 tokens in 0.5s per phase → 40 tokens / 1.0s → dτ = (1/40)·1.0
        qih = rec["qih"]
        self.assertIn("phase_clock_dtau", qih)
        self.assertAlmostEqual(qih["phase_clock_dtau"], 0.025, places=6)
        self.assertNotIn("entanglement_distance", qih)  # no previous graph yet
        self.assertNotIn("coherence_c_mt", qih)         # window still empty

    def test_ledger_qih_distance_across_cycles(self):
        """A changed dispatch graph between cycles yields a machine-checked
        entanglement distance in the second cycle's ledger record."""
        other_graph = {"graph": [{"step": 1, "action": "read_file", "target": "state.json"}]}
        stub = StubModel([
            GOOD_GRAPH_JSON, GOOD_REWRITE_JSON,        # cycle 1
            json.dumps(other_graph), GOOD_REWRITE_JSON,  # cycle 2 — different plan
        ])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            run.cycle()
            run.cycle()
        finally:
            drift_loop.chat_stream = original
        with open(os.path.join(self.res, "ledger.jsonl")) as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(recs), 2)
        self.assertNotIn("entanglement_distance", recs[0]["qih"])  # first cycle: no prev
        d = recs[1]["qih"].get("entanglement_distance")
        self.assertIsNotNone(d)
        self.assertGreater(d, 0.0)  # plans changed → distance grew
        # and the accepted graph was persisted for the next comparison
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertIn("read_file", st["prev_graph"])

    def test_rejected_rewrite_keeps_previous_instructions(self):
        bad = {"instructions": "Just check the weather.", "rationale": "x"}
        stub = StubModel([GOOD_GRAPH_JSON, json.dumps(bad)])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            r = self._run().cycle()
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(r["gate"], "rejected")
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        # instructions unchanged → still the objective
        self.assertEqual(st["instructions"], drift_loop.DEFAULT_OBJECTIVE)
        self.assertLess(r["coherence"], drift_loop.COHERENCE_FLOOR + 0.2)

    def test_graph_failure_then_success_no_trip(self):
        stub = StubModel(["I cannot parse that", GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            r1 = run.cycle()
            r2 = run.cycle()
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(r1["gate"], "rejected")  # graph failed → rewrite skipped
        self.assertEqual(r2["gate"], "accepted")
        self.assertIsNone(run.breaker.record("graph:missing-key", True))  # reset works

    def test_breaker_trips_run(self):
        stub = StubModel(["nope", "nope again"])  # two consecutive graph parse fails
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            r1 = run.cycle()
            r2 = run.cycle()
        finally:
            drift_loop.chat_stream = original
        self.assertIsInstance(r1, dict)  # first is a normal (failed) cycle
        self.assertIsInstance(r2, str)  # second trips
        self.assertIn("TRIP", r2)
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["phase"], "tripped")

    # ── auto-resume (a trip must not end the study) ────────────────────────

    def test_a_trip_exits_2_without_max_trips(self):
        """The guard stays a guard: with no --max-trips a mid-run trip still
        returns 2 so an operator is prompted instead of a study grinding on."""
        stub = StubModel(["nope", "nope", GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = self._run().run_cycles(3)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 2)
        self.assertEqual(len(self._ledger_records()), 2)  # the trip is still recorded
        with open(os.path.join(self.res, "state.json")) as f:
            self.assertEqual(json.load(f)["phase"], "tripped")

    def test_max_trips_auto_resumes_in_process_and_keeps_the_run_id(self):
        """Measured on the phone: the loop returned 2 on every mid-run trip while
        the watchdog treated 2 as terminal, so auto-resume could never resume and
        the study stalled every few cycles (trips at 159, 168, 173, 177). With
        --max-trips the trip is recorded, the SAME run continues in-process, and
        the study reaches its target."""
        stub = StubModel(["nope", "nope", GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            run_id = run.run
            code = run.run_cycles(3, max_trips=1)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 0)                       # reached the target
        recs = self._ledger_records()
        self.assertEqual([r["cycle"] for r in recs], [1, 2, 3])
        self.assertEqual(recs[1]["graph_sig"], "graph:missing-key")
        self.assertIn("TRIP", recs[1]["breaker"])      # the trip is in the record
        self.assertEqual({r["run"] for r in recs}, {run_id})  # never forked
        # the reset is an operator event, not a ledger row — schema stays stable
        with open(os.path.join(self.res, "operator-events.jsonl")) as f:
            events = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "operator-reset")
        self.assertEqual(events[0]["run"], run_id)
        with open(os.path.join(self.res, "state.json")) as f:
            self.assertEqual(json.load(f)["phase"], "done")

    def test_max_trips_cap_still_stops_a_pathological_run(self):
        """A model that only ever emits garbage must not grind to 1000 cycles:
        past the cap the run stops and returns 2 for a human."""
        stub = StubModel(["nope"] * 40)  # every cycle trips again
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = self._run().run_cycles(1000, max_trips=2)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 2)
        self.assertLessEqual(len(self._ledger_records()), 6)

    # ── drift alarm (also a stop-and-ask outcome, also recoverable) ─────────

    BAD_REWRITE_JSON = json.dumps({"instructions": "Just check the weather.", "rationale": "x"})

    def _tripped_alarm_run(self):
        """One more sub-floor cycle after DRIFT_STREAK-1 of them → DRIFT ALARM.
        A single rejected rewrite cannot trip the breaker, so this isolates the
        ALARM path from the TRIP path."""
        stub = StubModel([GOOD_GRAPH_JSON, self.BAD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            run.state["coherence_streak"] = drift_loop.DRIFT_STREAK - 1
            return run
        finally:
            drift_loop.chat_stream = original

    def test_drift_alarm_exits_2_without_max_alarms(self):
        run = self._tripped_alarm_run()
        stub = StubModel([GOOD_GRAPH_JSON, self.BAD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = run.run_cycles(1, resume=False)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 2)
        with open(os.path.join(self.res, "state.json")) as f:
            self.assertEqual(json.load(f)["phase"], "drift")

    def test_max_alarms_resumes_and_records_the_alarm_distinctly(self):
        """A drift alarm is an outcome, not corruption. --max-alarms records it as
        `drift-alarm-reset` (distinct from a breaker reset) and continues the SAME
        run, keeping the alarm in the ledger and never moving the coherence floor."""
        run = self._tripped_alarm_run()
        run_id = run.run
        stub = StubModel([GOOD_GRAPH_JSON, self.BAD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = run.run_cycles(1, resume=False, max_alarms=1)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 0)
        recs = self._ledger_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["run"], run_id)          # never forked
        self.assertLess(recs[0]["coherence"], drift_loop.COHERENCE_FLOOR)
        with open(os.path.join(self.res, "operator-events.jsonl")) as f:
            events = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "drift-alarm-reset")
        self.assertEqual(events[0]["run"], run_id)
        self.assertIn("DRIFT ALARM", events[0]["alarm"])
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["coherence_streak"], 0)        # the streak is what resets
        self.assertEqual(st["phase"], "running")

    # ── resume / watch / determinism ───────────────────────────────────────

    def test_resume_continues_cycle_number(self):
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_REWRITE_JSON] * 2)
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            run.cycle()
            run.cycle()
            loaded = DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res)
        finally:
            drift_loop.chat_stream = original
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.state["cycle"], 2)

    def test_watch_runs_one_cycle_per_trigger(self):
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            # simulate an external touch of the trigger file
            with open(os.path.join(self.res, "trigger"), "w") as f:
                f.write("go")
            # watch with max_cycles=1 returns after one cycle
            code = run.watch(max_cycles=1)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 0)
        self.assertEqual(run.state["cycle"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.res, "trigger")))
        self.assertTrue(os.path.exists(os.path.join(self.res, "next")))

    def test_determinism_battery_passes_on_stable(self):
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_GRAPH_JSON, GOOD_GRAPH_JSON])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = self._run().determinism_battery(3, temperature=0.0)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 0)  # identical inputs → identical hashes

    def test_determinism_battery_fails_on_divergence(self):
        other = {"graph": [{"step": 1, "action": "read_file", "target": "state.json"}]}
        stub = StubModel([GOOD_GRAPH_JSON, GOOD_GRAPH_JSON, json.dumps(other)])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            code = self._run().determinism_battery(3, temperature=0.0)
        finally:
            drift_loop.chat_stream = original
        self.assertEqual(code, 3)  # Test 2 falsified

    # ── truncated calls must never be accepted as answers ──────────────────
    #
    # A budget-cut call returns PARTIAL text. A partial rewrite can still retain
    # the objective anchors, so without this guard it would pass the gates and
    # be written into instructions.md as an accepted self-edit — the loop
    # quietly mutating itself with a half-sentence.

    def test_a_truncated_graph_is_not_recorded_as_a_dispatch_graph(self):
        stub = StubModel([{"content": GOOD_GRAPH_JSON, "truncated": True}])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            result = self._run().cycle()
        finally:
            drift_loop.chat_stream = original
        self.assertIsInstance(result, dict)
        self.assertEqual(result["gate"], "rejected")
        self.assertIsNone(result["graph_hash"])
        recs = self._ledger_records()
        self.assertEqual(recs[0]["graph_sig"], "graph:budget-exceeded")
        self.assertNotEqual(recs[0]["graph_sig"], "graph:ok")

    def test_a_truncated_rewrite_is_refused_and_the_self_is_not_edited(self):
        before = self._run()
        original_instructions = before.state["instructions"]
        # a good graph, then a rewrite that WOULD pass the gates but is partial
        stub = StubModel([GOOD_GRAPH_JSON,
                          {"content": GOOD_REWRITE_JSON, "truncated": True}])
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            run.cycle()
        finally:
            drift_loop.chat_stream = original

        recs = self._ledger_records()
        self.assertEqual(recs[0]["rewrite_sig"], "rewrite:budget-exceeded")
        self.assertFalse(recs[0]["rewrite_ok"])
        self.assertEqual(recs[0]["retention"], 0.0)
        # the mutable self must be untouched
        loaded = DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res)
        self.assertEqual(loaded.state["instructions"], original_instructions)

    def test_a_rewrite_trip_does_not_crash_the_run(self):
        """Regression: the rewrite trip path handed the RAW dispatch graph into the
        QIH metrics, where difflib indexes its arguments — so a dict raised
        `KeyError: 0` and killed the process mid-cycle (seen on the phone at
        cycle 141, which lost that cycle and left state.json ahead of the ledger).
        A trip must be a recorded outcome, not a crash.

        The breaker is keyed by full signature and a successful graph only clears
        its OWN key, so two consecutive budget-exceeded rewrites trip even with
        good graphs in between.
        """
        truncated = {"content": GOOD_REWRITE_JSON, "truncated": True}
        stub = StubModel([GOOD_GRAPH_JSON, truncated] * 2)
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            run = self._run()
            r1 = run.cycle()   # 1st budget-exceeded rewrite → breaker count 1
            r2 = run.cycle()   # 2nd → TRIP; this is where the crash happened
        finally:
            drift_loop.chat_stream = original
        self.assertIsInstance(r1, dict)
        self.assertEqual(r1["gate"], "rejected")
        self.assertIsInstance(r2, str)
        self.assertIn("TRIP", r2)
        # The tripped cycle must still be IN the ledger — a crash loses it.
        recs = self._ledger_records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["rewrite_sig"], "rewrite:budget-exceeded")
        self.assertEqual(recs[1]["graph_sig"], "graph:ok")
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["phase"], "tripped")

    def test_qih_metrics_survive_a_non_string_graph(self):
        """The distance metric must degrade to 'no metric' when handed a non-string
        graph. difflib indexes its arguments, so comparing a dict raises instead of
        comparing — and a type mistake must never kill a long run."""
        run = self._run()
        run.prev_graph = '{"a": 1}'
        r1 = {"tokens": 10, "elapsed": 1.0}
        # a dict here is the exact shape that used to raise KeyError: 0
        block = run._qih_metrics(True, {"a": 2}, r1, None, 1.0)
        self.assertNotIn("entanglement_distance", block)
        # the string form still computes the metric
        same = run._qih_metrics(True, '{"a": 2}', r1, None, 1.0)
        self.assertIn("entanglement_distance", same)
        # identical graphs → no distance metric (nothing changed this cycle)
        none_dist = run._qih_metrics(True, '{"a": 1}', r1, None, 1.0)
        self.assertNotIn("entanglement_distance", none_dist)

    def test_a_tripped_run_is_never_resumed_implicitly(self):
        """`load()` returning None means BOTH 'nothing to resume' and 'state exists
        but must not be resumed'. Conflating them let a restart silently begin a
        second run over the same ledger — forking the record — so a tripped state
        must name itself instead."""
        run = self._run()
        run.state["phase"] = "tripped"
        run.state["cycle"] = 7
        run._save()
        self.assertIsNone(DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res))
        reason = drift_loop.unresumable_reason(drift_loop.DEFAULT_OBJECTIVE, self.res)
        self.assertIn("tripped", reason)
        self.assertIn("7", reason)

    def test_unresumable_reason_is_none_when_a_fresh_start_is_legitimate(self):
        """No state at all → no reason to refuse. Anything else would make the
        guard refuse legitimate first runs."""
        self.assertIsNone(drift_loop.unresumable_reason(drift_loop.DEFAULT_OBJECTIVE, self.res))
        self._one_cycle()  # a normal, resumable state
        self.assertIsNone(drift_loop.unresumable_reason(drift_loop.DEFAULT_OBJECTIVE, self.res))
        other = drift_loop.unresumable_reason("a different objective", self.res)
        self.assertIn("different objective", other)

    def test_resume_tripped_continues_the_same_run_id(self):
        """The operator path must continue the SAME run — a trip is a recorded
        outcome, not a reason to throw away 140 measured cycles."""
        run = self._run()
        run.state["phase"] = "tripped"
        run.breaker.record("rewrite:objective-drift", False)
        run.breaker.record("rewrite:objective-drift", False)
        run.breaker.record("graph:server-error", False)
        run._save()
        run_id = run.run
        resumed = DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res, allow_tripped=True)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.run, run_id)              # not a second run
        self.assertEqual(resumed.state["phase"], "tripped")  # load alone never un-trips
        resumed.clear_trip()
        self.assertEqual(resumed.state["phase"], "running")
        # BOTH counters reset: `distinct` is cumulative, and carrying it over at
        # the limit made the resumed run abort on the first new failure category.
        self.assertEqual(resumed.breaker.consecutive, {})
        self.assertEqual(resumed.breaker.distinct, [])
        with open(os.path.join(self.res, "state.json")) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["phase"], "running")

    def test_the_operator_reset_is_recorded_but_not_in_the_ledger(self):
        """A reset is an intervention, so the record must show it. It goes to
        operator-events.jsonl, NOT the ledger — the ledger's row schema is the Q1
        evidence format and no consumer should have to filter operator rows out."""
        run = self._run()
        run.state["phase"] = "tripped"
        run.breaker.record("rewrite:objective-drift", False)
        run.breaker.record("rewrite:objective-drift", False)
        run._save()
        resumed = DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res, allow_tripped=True)
        event = resumed.clear_trip()
        self.assertEqual(event["event"], "operator-reset")
        self.assertEqual(event["run"], resumed.run)
        self.assertEqual(event["cleared"]["consecutive"], {"rewrite:objective-drift": 2})
        self.assertEqual(event["cleared"]["distinct"], ["objective-drift"])
        with open(os.path.join(self.res, "operator-events.jsonl")) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "operator-reset")
        # the ledger is untouched by the reset
        self.assertFalse(os.path.exists(os.path.join(self.res, "ledger.jsonl")))

    def test_objective_anchors_extracted(self):
        anchors = _objective_anchors(drift_loop.DEFAULT_OBJECTIVE)
        self.assertIn("coherent", anchors)
        self.assertIn("objective", anchors)
        self.assertIn("recursion", anchors)

    # ── time budget (ephemeral hosts) ──────────────────────────────────────
    #
    # A CI runner is killed at a hard wall. If the loop is mid-cycle when that
    # happens, the host never gets to persist the residence and EVERY cycle of
    # that run is lost. These tests pin the behaviour that prevents it.

    @contextlib.contextmanager
    def _clock(self, start=1000.0):
        """A fake clock for drift_loop only. `time` is referenced there solely
        for the deadline check (and the watch sleep), so swapping the module
        reference is precise and cannot leak into other tests."""
        class _Clock:
            def __init__(self, t):
                self.t = t

            def time(self):
                return self.t

            def sleep(self, seconds):
                self.t += seconds

        fake = _Clock(start)
        original = drift_loop.time
        drift_loop.time = fake
        try:
            yield fake
        finally:
            drift_loop.time = original

    def _run_cycles_with_stub(self, cycles, deadline, replies):
        stub = StubModel(replies)
        original = drift_loop.chat_stream
        drift_loop.chat_stream = stub
        try:
            return self._run().run_cycles(cycles, deadline=deadline), stub
        finally:
            drift_loop.chat_stream = original

    def test_expired_budget_runs_no_cycle_and_stops_cleanly(self):
        """Already past the wall → exit 0 (not an error) with nothing started.
        Returning 0 is what lets the host still commit the residence."""
        with self._clock(2000.0):
            code, stub = self._run_cycles_with_stub(5, deadline=1500.0, replies=[])
        self.assertEqual(code, 0)
        self.assertEqual(stub.calls, [])          # no model time spent
        self.assertFalse(os.path.exists(os.path.join(self.res, "ledger.jsonl")))

    def test_budget_stops_at_a_cycle_boundary_and_never_tears_a_cycle(self):
        """The budget is a START gate: a cycle that begins before the deadline
        always runs to completion, and the next one is refused. So the run can
        overrun the budget by at most ONE cycle — never half of one.

        Clock starts at 1000, each cycle costs 100 s, deadline 1250:
            start while t < 1250  → t = 1000, 1100, 1200  → 3 cycles start
            after those, t = 1300 → the 4th is refused
        That overrun-by-one-cycle is exactly why a CI budget needs margin.
        """
        with self._clock(1000.0) as clk:
            stub = StubModel([GOOD_GRAPH_JSON, GOOD_REWRITE_JSON] * 3)
            original = drift_loop.chat_stream
            drift_loop.chat_stream = stub
            try:
                run = self._run()
                real_cycle = run.cycle

                def cycle_then_advance():
                    r = real_cycle()
                    clk.t += 100  # a cycle costs 100 s of wall clock
                    return r

                run.cycle = cycle_then_advance
                code = run.run_cycles(5, deadline=1250.0)
            finally:
                drift_loop.chat_stream = original

        self.assertEqual(code, 0)                  # clean stop, not an error
        self.assertEqual(run.state["cycle"], 3)    # whole cycles only
        self.assertEqual(len(stub.calls), 6)       # 3 × (graph + rewrite)
        self.assertEqual(len(self._ledger_records()), 3)
        # Every completed cycle is durable, so the next host resumes at 4.
        loaded = DriftRun.load(self.config, drift_loop.DEFAULT_OBJECTIVE, self.res)
        self.assertEqual(loaded.state["cycle"], 3)

    def test_no_budget_runs_the_full_count(self):
        with self._clock(1000.0):
            code, stub = self._run_cycles_with_stub(
                2, deadline=None,
                replies=[GOOD_GRAPH_JSON, GOOD_REWRITE_JSON,
                         GOOD_GRAPH_JSON, GOOD_REWRITE_JSON])
        self.assertEqual(code, 0)
        self.assertEqual(len(stub.calls), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)