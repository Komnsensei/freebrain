#!/usr/bin/env python3
"""loop_guard_test.py — tests for the autonomy loop's instrumentation, limit
detection and self-improvement triggers.

The model is stubbed, so every test is deterministic and offline. Two classes of
test matter most:

* **Detection tests** assert each pathology gets its own named reason with the
  right stage — the whole point of the guard is that four different faults stop
  arriving as one bare "budget exceeded".
* **Trigger-policy tests** assert the self-improvement record fires for
  high/critical faults and *not* for legitimate runs. A trigger that fires on
  healthy runs is noise, and noise is ignored.

Run: python3 loop_guard_test.py
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime
import loop_guard

# Never write the shipped residence from a test (see test_support.py).
import test_support
test_support.isolate_residence()


def guard(max_steps=8, enabled=True, emit=False, thresholds=None, log_path=None,
          ledger_path=None):
    return loop_guard.LoopGuard(max_steps, thresholds=thresholds, enabled=enabled,
                                env={}, emit=emit, log_path=log_path,
                                ledger_path=ledger_path)


def messages(n=4, size=600):
    out = [{"role": "system", "content": "sys"}, {"role": "user", "content": "goal"}]
    for i in range(n):
        out.append({"role": "assistant", "content": "TOOL:list_dir ."})
        out.append({"role": "user", "content": "x" * size})
    return out


class ParsingTest(unittest.TestCase):
    def test_result_failed_accepts_the_canonical_and_legacy_prefixes(self):
        # Detection is deliberately liberal even though emission is now strict:
        # a legacy result already in a ledger, or returned by a tool this module
        # does not own, must still be counted as a failure. See
        # FailureProtocolTest for the emission side.
        self.assertTrue(loop_guard.result_failed("[gate-failed] nope"))
        self.assertTrue(loop_guard.result_failed("[error] not a file: x"))
        self.assertFalse(loop_guard.result_failed("ok: fine"))

    def test_parse_directive_accepts_both_forms(self):
        self.assertEqual(loop_guard.parse_directive("<<<TOOL:list_dir .>>>"), ("list_dir", "."))
        self.assertEqual(loop_guard.parse_directive("TOOL:read_file a/b.txt"),
                         ("read_file", "a/b.txt"))

    def test_parse_directive_on_prose_returns_none(self):
        name, arg = loop_guard.parse_directive("I will not call a tool here.")
        self.assertIsNone(name)

    def test_signature_is_stable_and_distinguishes_arguments(self):
        a = loop_guard.call_signature("list_dir", ".")
        self.assertEqual(a, loop_guard.call_signature("list_dir", "."))
        self.assertNotEqual(a, loop_guard.call_signature("list_dir", "./x"))
        self.assertNotEqual(a, loop_guard.call_signature("read_file", "."))


class DetectionTest(unittest.TestCase):
    def test_repeat_of_a_succeeding_call_is_no_progress(self):
        g = guard()
        verdict = {"action": "continue"}
        for step in (1, 2, 3):
            verdict = g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
        self.assertEqual(verdict["action"], "stop")
        self.assertEqual(verdict["diagnosis"]["reason"], "no_progress")
        self.assertEqual(verdict["diagnosis"]["stage"], "planning")
        self.assertEqual(verdict["diagnosis"]["severity"], "high")

    def test_repeat_of_a_failing_call_is_an_error_storm_not_no_progress(self):
        # Precedence matters: pointing the fix at the prompt when the tool is
        # unusable sends the improvement cycle to the wrong stage.
        g = guard()
        for step in (1, 2, 3):
            verdict = g.observe(step, "tool", [("read_file", "./no", True)], [True], messages())
        self.assertEqual(verdict["diagnosis"]["reason"], "tool_error_storm")
        self.assertEqual(verdict["diagnosis"]["stage"], "tool-use")

    def test_unknown_tool_storm(self):
        g = guard()
        first = g.observe(1, "tool", [("frobnicate", "x", True)], [True], messages())
        self.assertEqual(first["action"], "continue")  # one typo is tolerated
        second = g.observe(2, "tool", [("frobnicate", "y", True)], [True], messages())
        self.assertEqual(second["diagnosis"]["reason"], "unknown_tool_storm")

    def test_mixed_success_resets_the_error_streak(self):
        g = guard(thresholds=dict(loop_guard.DEFAULT_THRESHOLDS, tool_error_streak=2))
        g.observe(1, "tool", [("read_file", "a", True)], [True], messages())
        g.observe(2, "tool", [("list_dir", ".", False)], [False], messages())
        v = g.observe(3, "tool", [("read_file", "b", True)], [True], messages())
        self.assertEqual(v["action"], "continue", "streak must reset on a success")

    def test_progress_is_not_flagged(self):
        g = guard()
        for step in range(1, 9):
            v = g.observe(step, "tool", [("list_dir", "d%d" % step, False)], [False], messages())
            self.assertEqual(v["action"], "continue")

    def test_wall_clock_ceiling(self):
        g = guard(thresholds=dict(loop_guard.DEFAULT_THRESHOLDS, max_wall_clock_s=10))
        v = g.observe(1, "tool", [("list_dir", ".", False)], [False], messages(),
                      elapsed_s=11.0)
        self.assertEqual(v["diagnosis"]["reason"], "wall_clock_exceeded")

    def test_disabled_guard_never_stops_and_never_triggers(self):
        g = guard(enabled=False)
        for step in range(1, 9):
            v = g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
            self.assertEqual(v["action"], "continue")
        self.assertEqual(g.triggers, [])

    def test_budget_exhausted_and_empty_response_reasons_exist(self):
        g = guard()
        v = g.step_failed("budget_exhausted", {"max_steps": 8}, step=8)
        self.assertEqual(v["diagnosis"]["reason"], "budget_exhausted")
        g2 = guard()
        v2 = g2.step_failed("model_unavailable", {"error": "cascade exhausted"}, step=1)
        self.assertEqual(v2["diagnosis"]["severity"], "critical")


class CompactionTest(unittest.TestCase):
    def test_compaction_elides_middle_but_keeps_system_goal_and_recent(self):
        msgs = messages(n=6, size=2000)
        original_system, original_goal = msgs[0]["content"], msgs[1]["content"]
        recent = msgs[-1]["content"]
        elided = loop_guard.compact_messages(msgs, loop_guard.DEFAULT_THRESHOLDS)
        self.assertGreater(elided, 0)
        self.assertEqual(msgs[0]["content"], original_system)
        self.assertEqual(msgs[1]["content"], original_goal)
        self.assertEqual(msgs[-1]["content"], recent, "newest result must survive")
        self.assertTrue(any(m["content"].startswith(loop_guard.ELISION_NOTE) for m in msgs))

    def test_compaction_is_idempotent(self):
        msgs = messages(n=6, size=2000)
        first = loop_guard.compact_messages(msgs, loop_guard.DEFAULT_THRESHOLDS)
        second = loop_guard.compact_messages(msgs, loop_guard.DEFAULT_THRESHOLDS)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0, "already-elided messages must not be re-elided")

    def test_compaction_only_for_large_messages(self):
        msgs = messages(n=4, size=50)
        self.assertEqual(loop_guard.compact_messages(msgs, loop_guard.DEFAULT_THRESHOLDS), 0)

    def test_observe_reports_compact_action_over_budget(self):
        g = guard(thresholds=dict(loop_guard.DEFAULT_THRESHOLDS, context_budget_chars=2000))
        v = g.observe(1, "tool", [("list_dir", ".", False)], [False], messages(n=6, size=2000))
        self.assertEqual(v["action"], "compact")
        self.assertGreater(v["elided"], 0)
        self.assertEqual(g.compactions, 1)


class TriggerPolicyTest(unittest.TestCase):
    def test_high_and_critical_faults_trigger(self):
        self.assertTrue(loop_guard.LoopGuard(8).should_trigger_self_improvement("no_progress"))
        self.assertTrue(loop_guard.LoopGuard(8).should_trigger_self_improvement("model_unavailable"))
        self.assertTrue(loop_guard.LoopGuard(8).should_trigger_self_improvement("unknown_tool_storm"))

    def test_medium_faults_do_not_trigger(self):
        # These are ordinary outcomes (a short budget, a question answerable from
        # memory). Logging them as self-improvement events would drown the signal.
        g = loop_guard.LoopGuard(8)
        for reason in ("budget_exhausted", "no_tool_use", "context_over_budget"):
            self.assertFalse(g.should_trigger_self_improvement(reason))

    def test_emit_false_writes_nothing(self):
        tmp = tempfile.mkdtemp(prefix="lg-")
        try:
            log = os.path.join(tmp, "log.md")
            ledger = os.path.join(tmp, "l.jsonl")
            g = guard(emit=False, log_path=log, ledger_path=ledger)
            for step in (1, 2, 3):
                g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
            self.assertEqual(g.triggers, ["no_progress"], "bookkeeping still happens")
            self.assertFalse(os.path.exists(log))
            self.assertFalse(os.path.exists(ledger))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_emit_true_writes_an_opened_record_only(self):
        """A fault caught at runtime has done no RESEARCH/DESIGN/IMPLEMENT/TEST work,
        so the record must say `open` and list only the phases that happened.

        This used to assert a full six-phase record for every fault, which meant
        the machine-written log claimed work that had not been done — and named
        the loop_guard cycle's implementation for a fault in a different layer.
        The six-phase form is now reserved for cycles the machinery actually
        closed (see `emit_cycle`, exercised in autonomy_test).
        """
        tmp = tempfile.mkdtemp(prefix="lg-")
        try:
            log = os.path.join(tmp, "log.md")
            ledger = os.path.join(tmp, "l.jsonl")
            g = guard(emit=True, log_path=log, ledger_path=ledger)
            for step in (1, 2, 3):
                g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
            with open(ledger, "r", encoding="utf-8") as f:
                record = json.loads(f.readline())
            self.assertEqual(record["reason"], "no_progress")
            self.assertEqual(record["status"], "open")
            self.assertEqual(sorted(record["phases"]),
                             ["detect", "hypothesis", "next", "routed_to"])
            for phase in record["phases"]:
                self.assertTrue(record["phases"][phase])
            with open(log, "r", encoding="utf-8") as f:
                text = f.read()
            self.assertIn("no_progress", text)
            self.assertIn("(opened)", text)
            labels = re.findall(r"^\d+\.\s+\*\*([A-Z_]+)\*\*", text, re.MULTILINE)
            self.assertEqual(labels, ["DETECT", "ROUTED_TO", "HYPOTHESIS", "NEXT"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_emitter_never_raises_on_an_unwritable_path(self):
        g = guard(emit=True, log_path="/proc/definitely/not/writable.md",
                  ledger_path="/proc/definitely/not/writable.jsonl")
        for step in (1, 2, 3):
            g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
        self.assertEqual(g.triggers, ["no_progress"])


class ThresholdConfigTest(unittest.TestCase):
    def test_corrupt_config_degrades_to_defaults(self):
        tmp = tempfile.mkdtemp(prefix="lg-")
        try:
            path = os.path.join(tmp, "loop_guard.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(loop_guard.active_thresholds({"LOOP_GUARD_CONFIG": path}),
                             loop_guard.DEFAULT_THRESHOLDS)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_registered_thresholds_override_and_ignore_unknown_keys(self):
        tmp = tempfile.mkdtemp(prefix="lg-")
        try:
            path = os.path.join(tmp, "loop_guard.json")
            loop_guard.register_thresholds({"repeat_limit": 5, "bogus": 99}, path=path)
            got = loop_guard.active_thresholds({"LOOP_GUARD_CONFIG": path})
            self.assertEqual(got["repeat_limit"], 5)
            self.assertNotIn("bogus", got)
            self.assertEqual(got["tool_error_streak"], loop_guard.DEFAULT_THRESHOLDS["tool_error_streak"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _registered(self, params):
        tmp = tempfile.mkdtemp(prefix="lg-")
        path = os.path.join(tmp, "loop_guard.json")
        loop_guard.register_thresholds(params, path=path)
        self.addCleanup(shutil.rmtree, tmp, True)
        return loop_guard.active_thresholds({"LOOP_GUARD_CONFIG": path})

    def test_a_string_knob_is_accepted_when_it_is_an_enum_member(self):
        # `repeat_mode` is the first non-numeric knob; the validator has to let it
        # through without letting anything else.
        got = self._registered({"repeat_mode": "cumulative"})
        self.assertEqual(got["repeat_mode"], "cumulative")

    def test_a_value_outside_the_enum_is_rejected(self):
        got = self._registered({"repeat_mode": "banana"})
        self.assertEqual(got["repeat_mode"], loop_guard.DEFAULT_THRESHOLDS["repeat_mode"])

    def test_a_string_in_a_numeric_knob_is_rejected(self):
        # Otherwise it would be fed straight into arithmetic.
        got = self._registered({"context_budget_chars": "lots"})
        self.assertEqual(got["context_budget_chars"],
                         loop_guard.DEFAULT_THRESHOLDS["context_budget_chars"])

    def test_a_bool_is_rejected_even_though_python_calls_it_an_int(self):
        got = self._registered({"repeat_limit": True})
        self.assertEqual(got["repeat_limit"], loop_guard.DEFAULT_THRESHOLDS["repeat_limit"])


class FailureProtocolTest(unittest.TestCase):
    """One canonical failure marker, constructed in one place.

    Before this, the action tools emitted '[gate-failed]' while the read-only
    file tools emitted '[error]'. The audit showed the cost: a model looping on
    `read_file` of a missing path was reporting a failure the guard did not
    count, so it read as steady progress until the step budget ran out and the
    diagnosis blamed the budget instead of the tool.
    """

    def probes(self):
        """name -> failing argument, read from the registry the loop itself calls.

        This was a table in this file, which made it a second list someone had to
        remember to extend — the failure mode of a contract that lives beside the
        code instead of in it. The probe is now part of the tool entry itself
        (`agent_runtime.Tool.fn`, `.probe`), so this reads the one source and
        cannot disagree with what the loop runs. See
        `test_the_registry_is_the_only_place_probes_live`, which fails if a probe
        table is reintroduced here.
        """
        return {name: tool.probe for name, tool in agent_runtime.TOOLS.items()}

    def test_every_registered_tool_has_a_probe(self):
        self.assertEqual(agent_runtime._validate_tools(agent_runtime.TOOLS),
                         agent_runtime.TOOLS)
        for name, tool in sorted(agent_runtime.TOOLS.items()):
            self.assertIsInstance(tool, agent_runtime.Tool, name)
            self.assertIsInstance(tool.probe, str, name)

    def test_a_tool_cannot_be_defined_without_a_probe(self):
        # The definition-time guarantee, tested rather than trusted. Both fields
        # are required, so a new TOOLS entry that forgets its probe raises while
        # agent_runtime is importing — before the tool can ever run.
        with self.assertRaises(TypeError):
            agent_runtime.Tool(agent_runtime._tool_list_dir)

    def test_an_entry_that_loses_its_tool_shape_is_rejected(self):
        # A bare function is the other way the registry rots: it is still
        # callable, so the loop would keep working while the probe silently
        # vanished and every downstream check lost its evidence.
        with self.assertRaises(RuntimeError):
            agent_runtime._validate_tools({"list_dir": lambda path: "ok"})
        with self.assertRaises(RuntimeError):
            agent_runtime._validate_tools({"x": agent_runtime.Tool(lambda p: "ok", 7)})

    def test_every_tool_reports_failure_in_the_canonical_form(self):
        for name, arg in sorted(self.probes().items()):
            out = agent_runtime.TOOLS[name](arg)
            self.assertTrue(out.startswith(loop_guard.FAILED_PREFIX),
                            "%s emitted %r" % (name, out[:70]))
            self.assertTrue(loop_guard.result_failed(out), name)

    def test_no_tool_emits_a_legacy_prefix(self):
        for name, arg in sorted(self.probes().items()):
            out = agent_runtime.TOOLS[name](arg)
            self.assertFalse(out.startswith(loop_guard.LEGACY_FAILURE_PREFIXES),
                             "%s regressed to a legacy prefix: %r" % (name, out[:70]))

    def test_the_registry_is_the_only_place_probes_live(self):
        # Split so this assertion cannot match its own source.
        literal = "PROBES" + " = {"
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "loop_guard_test.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(literal, src)

    def test_the_tool_module_contains_no_legacy_literal(self):
        # Probe-independent: a failure path the probes do not reach (an exception
        # branch, a tool added later) must not reintroduce the old marker. This,
        # not the probes, is what keeps the protocol unified.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_runtime.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('"[error] ', src)
        self.assertNotIn("'[error] ", src)

    def test_fail_is_the_single_constructor(self):
        self.assertEqual(loop_guard.fail("a b"), "[gate-failed] a b")
        self.assertEqual(loop_guard.FAILURE_PREFIXES,
                         (loop_guard.FAILED_PREFIX,) + loop_guard.LEGACY_FAILURE_PREFIXES)

    def test_the_loop_s_failure_line_uses_the_canonical_prefix(self):
        g = guard()
        v = g.step_failed("budget_exhausted", {"max_steps": 8}, step=8)
        line = g.failure_line(v["diagnosis"], 8)
        self.assertTrue(line.startswith(loop_guard.FAILED_PREFIX))
        self.assertIn("reason=budget_exhausted", line)

    def test_every_declared_probe_is_the_failing_argument_it_claims_to_be(self):
        # A probe is a claim about the tool, so it is checked against the tool it
        # is attached to: the argument registered on the entry must be the one
        # that fails, not merely some argument that happens to fail elsewhere.
        for name, tool in sorted(agent_runtime.TOOLS.items()):
            self.assertTrue(loop_guard.result_failed(tool(tool.probe)), name)

    def test_a_storm_of_file_tool_failures_is_detected(self):
        # The exact shape the prefix split hid: one tool, always failing, reported
        # in what used to be the unrecognised prefix.
        g = guard()
        for step in (1, 2, 3):
            out = agent_runtime.TOOLS["read_file"]("./no/such/file.txt")
            v = g.observe(step, "tool", [("read_file", "./no/such/file.txt",
                                          loop_guard.result_failed(out),
                                          loop_guard.result_signature(out))],
                          [loop_guard.result_failed(out)], messages())
        self.assertEqual(v["diagnosis"]["reason"], "tool_error_storm")


class RepeatRuleTest(unittest.TestCase):
    """A repeated call is not by itself a stuck loop.

    The original rule counted a call signature across the *whole run*, so a model
    that returned to something it had already read was stopped as `no_progress`.
    The default rule asks the narrower question: same call, same result, on
    consecutive steps, with nothing in between. loop_audit.py measures both
    against the same pathologies.
    """

    def _observe(self, g, step, name, arg, result, failed=False):
        return g.observe(step, "tool",
                         [(name, arg, failed, loop_guard.result_signature(result))],
                         [failed], messages())

    def _run(self, mode, results):
        g = guard(thresholds=dict(loop_guard.DEFAULT_THRESHOLDS, repeat_mode=mode))
        for step, res in enumerate(results, 1):
            v = self._observe(g, step, "list_dir", "./state", res)
            if v["action"] == "stop":
                return step, v["diagnosis"]
        return None, None

    def test_the_default_rule_is_the_streak_rule(self):
        self.assertEqual(loop_guard.DEFAULT_THRESHOLDS["repeat_mode"], "streak")

    def test_streak_stops_a_call_repeating_with_the_same_result(self):
        step, diag = self._run("streak", ["same"] * 5)
        self.assertEqual(step, 3, "same timing as the cumulative rule it replaces")
        self.assertEqual(diag["reason"], "no_progress")
        self.assertEqual(diag["detail"]["rule"], "streak")

    def test_streak_does_not_stop_a_call_whose_result_changes(self):
        _, diag = self._run("streak", ["v1", "v2", "v3", "v4"])
        self.assertIsNone(diag, "a changing result is progress, not a loop")

    def test_cumulative_flags_the_changing_result_case(self):
        # The measured false positive this rule change removes.
        _, diag = self._run("cumulative", ["v1", "v2", "v3", "v4"])
        self.assertEqual(diag["reason"], "no_progress")
        self.assertEqual(diag["detail"]["rule"], "cumulative")

    def test_streak_ignores_interleaved_revisits(self):
        g = guard()
        for step, arg in enumerate(["./a", "./b", "./a", "./b", "./a"], 1):
            v = self._observe(g, step, "list_dir", arg, "listing")
            self.assertEqual(v["action"], "continue",
                             "alternating reads are exploration, not a loop")

    def test_cumulative_flags_interleaved_revisits(self):
        g = guard(thresholds=dict(loop_guard.DEFAULT_THRESHOLDS, repeat_mode="cumulative"))
        actions = [self._observe(g, step, "list_dir", arg, "listing")["action"]
                   for step, arg in enumerate(["./a", "./b", "./a", "./b", "./a"], 1)]
        self.assertIn("stop", actions)

    def test_streak_resets_when_the_model_does_something_else(self):
        g = guard()
        self._observe(g, 1, "list_dir", "./a", "x")
        self._observe(g, 2, "list_dir", "./a", "x")       # streak 1
        self._observe(g, 3, "read_file", "./b.txt", "y")   # different call: reset
        v = self._observe(g, 4, "list_dir", "./a", "x")
        self.assertEqual(v["action"], "continue")

    def test_a_duplicate_call_inside_one_response_is_a_fault(self):
        # Nothing can change between duplications within a single response, so
        # this is a fault under either rule.
        g = guard()
        calls = [("list_dir", ".", False, loop_guard.result_signature("x"))] * 3
        v = g.observe(1, "tool", calls, [False, False, False], messages())
        self.assertEqual(v["diagnosis"]["reason"], "no_progress")
        self.assertEqual(v["diagnosis"]["detail"]["rule"], "within_step")

    def test_streak_falls_back_to_signatures_without_result_hashes(self):
        # A caller passing 3-tuples gets the weaker rule, not a crash.
        g = guard()
        for step in (1, 2, 3):
            v = g.observe(step, "tool", [("list_dir", ".", False)], [False], messages())
        self.assertEqual(v["diagnosis"]["reason"], "no_progress")

    def test_result_signature_distinguishes_different_output(self):
        self.assertEqual(loop_guard.result_signature("a"), loop_guard.result_signature("a"))
        self.assertNotEqual(loop_guard.result_signature("a"), loop_guard.result_signature("b"))

    def test_summary_reports_the_rule_in_force(self):
        s = guard().summary("stopped", 3)
        self.assertEqual(s["repeat_mode"], "streak")
        self.assertIn("repeat_streak", s)


class RunGoalIntegrationTest(unittest.TestCase):
    """The guard must change what the loop *reports*, not just what it observes."""

    def _config(self):
        return {"url": "http://127.0.0.1:1/v1", "model": "stub", "temperature": 0.0,
                "max_tokens": 32, "timeout_ms": 1000}

    def _run(self, behaviour, max_steps=8, g=None):
        real = agent_runtime.chat_stream
        state = {"n": 0}

        def fake(config, messages, **kw):
            state["n"] += 1
            return {"content": behaviour(state["n"]), "elapsed": 0.05, "tokens": 8,
                    "early_stop": True, "provider": "stub", "model": "stub"}

        try:
            agent_runtime.chat_stream = fake
            out = agent_runtime.run_goal(self._config(), "goal", max_steps=max_steps,
                                         guard=g or guard(max_steps=max_steps))
        finally:
            agent_runtime.chat_stream = real
        return out, state["n"]

    def test_healthy_run_returns_its_final_answer(self):
        out, _ = self._run(lambda n: "TOOL:list_dir ." if n == 1 else "FINAL: all good")
        self.assertNotIn("[gate-failed]", out)
        self.assertIn("FINAL: all good", out)

    def test_stuck_loop_stops_early_with_a_named_reason(self):
        out, steps = self._run(lambda n: "TOOL:list_dir .")
        self.assertIn("[gate-failed]", out)
        self.assertIn("reason=no_progress", out)
        self.assertIn("stage=planning", out)
        self.assertLess(steps, 8, "a detected stuck loop must not burn the whole budget")

    def test_failure_line_is_machine_parseable(self):
        out, _ = self._run(lambda n: "TOOL:frobnicate x")
        fields = {}
        for token in out.replace("[gate-failed]", "").split():
            if "=" in token:
                k, _, v = token.partition("=")
                fields[k] = v
        self.assertEqual(fields["reason"], "unknown_tool_storm")
        self.assertEqual(fields["severity"], "high")
        self.assertEqual(fields["stage"], "tool-use")
        self.assertIn("step", fields)
        self.assertIn("detail", fields)

    def test_budget_exhausted_when_progress_is_real(self):
        # Each step calls a *succeeding* tool with a different argument, so this
        # is a run that needs more steps, not a fault. The tool must be stubbed to
        # succeed: with the real list_dir, 'd1' does not exist, every call errors
        # and the correct diagnosis is an error storm instead.
        g = guard(max_steps=4)
        real_chat = agent_runtime.chat_stream
        real_list = agent_runtime.TOOLS["list_dir"]
        state = {"n": 0}

        def fake(config, messages, **kw):
            state["n"] += 1
            return {"content": "TOOL:list_dir d%d" % state["n"], "elapsed": 0.05,
                    "tokens": 8, "early_stop": True, "provider": "stub", "model": "stub"}

        try:
            # Still wrapped in a Tool: a bare lambda here would be callable but
            # probe-less, dodging the shape the registry guarantees.
            agent_runtime.TOOLS["list_dir"] = agent_runtime.Tool(
                lambda path: "file_a.txt\nfile_b.txt", real_list.probe)
            agent_runtime.chat_stream = fake
            out = agent_runtime.run_goal(self._config(), "goal", max_steps=4, guard=g)
        finally:
            agent_runtime.chat_stream = real_chat
            agent_runtime.TOOLS["list_dir"] = real_list
        self.assertIn("reason=budget_exhausted", out)
        self.assertNotIn("reason=no_progress", out)

    def test_guard_off_reproduces_the_pre_hardening_behaviour(self):
        # Every pathology used to arrive as the same bare budget failure.
        out, steps = self._run(lambda n: "TOOL:list_dir .", g=guard(max_steps=8, enabled=False))
        self.assertIn("reason=budget_exhausted", out)
        self.assertEqual(steps, 8)

    def test_a_library_run_never_writes_to_the_repo_log(self):
        # Regression: running the test suite appended a fabricated fault to the
        # real SELF_IMPROVEMENT_LOG.md before emission became opt-in.
        log = loop_guard.SELF_IMPROVEMENT_LOG
        before = open(log, "r", encoding="utf-8").read() if os.path.exists(log) else None
        self._run(lambda n: "TOOL:list_dir .")
        after = open(log, "r", encoding="utf-8").read() if os.path.exists(log) else None
        self.assertEqual(before, after, "a stubbed library run must not touch the repo log")

    def test_summary_reports_the_loop_evidence(self):
        g = guard(max_steps=8)
        out, steps = self._run(lambda n: "TOOL:list_dir .", g=g)
        summary = g.summary("stopped", steps)
        for key in ("outcome", "steps_used", "max_steps", "tool_steps",
                    "distinct_calls", "peak_context_chars",
                    "self_improvement_triggers"):
            self.assertIn(key, summary)
        self.assertEqual(summary["self_improvement_triggers"], ["no_progress"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
