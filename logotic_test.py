#!/usr/bin/env python3
"""
logotic_test.py — deterministic tests for the logotic chain (logotic.py).

No network, no model, no browser: the engine is pure, the ledger lives in a temp
dir, and every gate decision is asserted against the session the run produced.

Run: python3 logotic_test.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logotic as lg

# Never write the shipped residence from a test (see test_support.py).
import test_support
test_support.isolate_residence()

REPO = os.path.dirname(os.path.abspath(__file__))


def _run(source, bearing=0.4):
    session, issues = lg.run_program(source, bearing)
    return session, issues, lg.conclude(session)


def _failing(source, bearing=0.4):
    _, _, result = _run(source, bearing)
    return result


class ParsingTest(unittest.TestCase):
    def test_every_paper_command_parses(self):
        cases = {
            "PROGRAM x": ("program", "x"),
            'LOAD CORPUS "c"': ("load", "c"),
            'LOAD SOURCE "c"': ("load", "c"),
            'PROFILE CHANNEL index': ("channel", "index"),
            "SET TARGET depth>=0.75, closure<=0.40": ("target", None),
            'ACTIVATE_MANTLE :: "Rebekah Cranes"': ("lens", "Rebekah Cranes"),
            'LENS clinical': ("lens", "clinical"),
            'SET_LOGOS :: "Sappho 31" [.state(latent)]': ("node", "Sappho 31"),
            "ROTATE { FROM: a, THROUGH: [b], BY: 144 }": ("turn", 144),
            "TURN BY 72": ("turn", 72),
            "ANCHOR :: ref:origin [STRICT]": ("lock", "ref:origin"),
            "WITNESS :: traversal ref:origin": ("witness", "ref:origin"),
            "APPLY D": ("apply", "d"),
            "APPLY K6 0.5": ("apply", "k6"),
            "CHECK METRICS": ("check", None),
            "IF closure>0.40 THEN APPLY N": ("if", "closure"),
            'RENDER { MODE: "Prose" }': ("show", "prose"),
            "ON_FAILURE { FALLBACK: Dwell }": ("fail", "dwell"),
            "CONFIRM": ("confirm", None),
            "EMIT text, graph": ("emit", "text, graph"),
            "RESIST flatten": ("resist", "flatten"),
            "CUT split": ("cut", "split"),
            "AGAIN M 4": ("again", "m"),
            "WITH D N": ("with", "d"),
            "TOWARD B": ("toward", "b"),
            "PROMOTE seated": ("promote", "seated"),
            "END": ("end", None),
        }
        for text, (kind, value) in cases.items():
            cmd = lg.parse_line(text)
            self.assertIsNotNone(cmd, text)
            self.assertEqual(cmd["k"], kind, text)
            if value is not None:
                got = cmd.get("name") or cmd.get("id") or cmd.get("op") \
                    or cmd.get("deg") or cmd.get("ref") or cmd.get("mode") \
                    or cmd.get("policy") or cmd.get("types") or cmd.get("pressure") \
                    or cmd.get("kind") or cmd.get("a") or cmd.get("to") \
                    or cmd.get("metric") or cmd.get("label")
                self.assertEqual(got, value, text)

    def test_paper_operator_names_alias_the_kernel(self):
        table = {"O_1": "read", "diagnose": "read", "O_4": "place", "install": "place",
                 "O_5": "guard", "defend": "guard", "O_6": "densify", "compress": "densify",
                 "O_7": "release", "liberate": "release", "terminal_silence": "halt",
                 "k8": "halt", "n_c": "open", "d_pres": "depth", "witness": "confirm"}
        for alias, canonical in table.items():
            self.assertEqual(lg.canon(alias), canonical, alias)

    def test_unreadable_line_is_not_a_crash(self):
        session, issues = lg.run_program("PROGRAM x\n!!! nonsense !!!\nEND")
        self.assertTrue(any(i["level"] == "error" for i in issues))
        self.assertEqual(session["name"], "x")


class SessionTest(unittest.TestCase):
    def test_blank_session_defaults(self):
        s = lg.blank_session()
        self.assertEqual(s["phase"], "void")
        self.assertEqual(s["rank"], "draft")
        self.assertEqual(s["metrics"], {"depth": 0.41, "closure": 0.66, "trace": 0.06,
                                        "plural": 0.28, "resist": 0.12, "band": 0.10})
        self.assertTrue(s["governed"])
        self.assertFalse(s["instrumental"])

    def test_low_bearing_is_instrumental(self):
        s = lg.blank_session(bearing=0.05)
        self.assertFalse(s["governed"])
        self.assertTrue(s["instrumental"])

    def test_pressures_are_channel_specific(self):
        self.assertGreater(lg.pressures_for("summarizer")["flatten"],
                           lg.pressures_for("index")["flatten"])
        self.assertGreater(lg.pressures_for("feed")["excite"],
                           lg.pressures_for("feed")["flatten"])


class OperatorTest(unittest.TestCase):
    def _s(self, **over):
        s = lg.blank_session(**over)
        return s

    def test_depth_open_resist_band_plural_move_the_measures(self):
        s = self._s()
        lg.apply_named(s, "D")
        lg.apply_named(s, "D")
        self.assertAlmostEqual(s["metrics"]["depth"], 0.73, places=2)
        lg.apply_named(s, "N")
        self.assertAlmostEqual(s["metrics"]["closure"], 0.48, places=2)
        lg.apply_named(s, "R")
        self.assertAlmostEqual(s["metrics"]["resist"], 0.34, places=2)
        lg.apply_named(s, "B")
        self.assertAlmostEqual(s["metrics"]["band"], 0.62, places=2)
        lg.apply_named(s, "M")
        lg.apply_named(s, "M")
        self.assertEqual(s["contradictions"], 2)
        self.assertAlmostEqual(s["metrics"]["plural"], 0.53, places=2)

    def test_open_is_refused_on_a_filled_node(self):
        # The paper's constraint (Appendix C): .state(filled) blocks N_c.
        s = self._s()
        lg.exec_cmd(s, {"k": "node", "label": "closed", "phase": "fixed",
                        "depth": None, "cut": None}, 1)
        issue = lg.apply_named(s, "N")
        self.assertEqual(issue["level"], "error")
        self.assertIn("filled", issue["message"])

    def test_open_on_a_held_node_is_allowed(self):
        # Deliberate divergence from the browser prototype: the paper's Sappho 31
        # example ROTATEs (which holds the node) before applying N_c.
        s = self._s()
        lg.apply_named(s, "N")  # never raises for a merely held node
        s["phase"] = "held"
        self.assertIsNone(lg.apply_named(s, "N"))

    def test_strict_lock_needs_a_ref(self):
        s = self._s()
        issue = lg.exec_cmd(s, {"k": "lock", "ref": "", "mode": "STRICT"}, 1)
        self.assertEqual(issue["level"], "error")
        self.assertIn("ref", issue["message"])

    def test_lock_raises_trace_and_depth(self):
        s = self._s()
        before = dict(s["metrics"])
        lg.exec_cmd(s, {"k": "lock", "ref": "ref:x", "mode": "STRICT"}, 1)
        self.assertAlmostEqual(s["metrics"]["trace"], before["trace"] + 0.24, places=2)
        self.assertAlmostEqual(s["metrics"]["depth"], before["depth"] + 0.05, places=2)
        self.assertEqual(s["anchors"], [{"ref": "ref:x", "mode": "STRICT"}])

    def test_cut_is_one_way_and_named(self):
        s = self._s()
        self.assertIsNone(lg.exec_cmd(s, {"k": "cut", "kind": "split"}, 1))
        issue = lg.exec_cmd(s, {"k": "cut", "kind": "defer"}, 2)
        self.assertEqual(issue["level"], "error")
        self.assertIn("spent", issue["message"])
        bad = lg.exec_cmd(lg.blank_session(), {"k": "cut", "kind": "burn"}, 1)
        self.assertEqual(bad["level"], "error")

    def test_release_needs_somewhere_to_land(self):
        issue = lg.apply_named(lg.blank_session(), "K7")
        self.assertIsNotNone(issue)
        s = lg.blank_session()
        lg.exec_cmd(s, {"k": "lock", "ref": "ref:x", "mode": "ADVISORY"}, 1)
        self.assertIsNone(lg.apply_named(s, "K7"))

    def test_halt_ends_the_pass(self):
        s = self._s()
        self.assertIsNone(lg.apply_named(s, "K8"))
        self.assertTrue(s["halted"])
        self.assertEqual(lg.apply_named(s, "D")["level"], "error")

    def test_confirm_is_blocked_under_the_borne_floor(self):
        s = lg.blank_session(bearing=0.05)
        self.assertEqual(lg.apply_named(s, "K3")["level"], "error")
        self.assertFalse(s["confirmed"])

    def test_unknown_operator_is_a_structured_fault(self):
        issue = lg.apply_named(lg.blank_session(), "frobnicate")
        self.assertEqual(issue["level"], "error")
        self.assertIn("unknown op", issue["message"])

    def test_command_aliases_used_as_operators_explain_themselves(self):
        issue = lg.apply_named(lg.blank_session(), "lock")
        self.assertIn("takes a ref", issue["message"])

    def test_instrumental_gain_is_damped(self):
        loud = lg.blank_session(bearing=0.4)
        quiet = lg.blank_session(bearing=0.05)
        lg.apply_named(loud, "D")
        lg.apply_named(quiet, "D")
        self.assertLess(quiet["metrics"]["depth"], loud["metrics"]["depth"])

    def test_place_warns_without_trace_and_densify_goes_flat(self):
        s1 = self._s()
        self.assertEqual(lg.apply_named(s1, "K4")["message"], "place without trace")
        s2 = self._s()
        self.assertEqual(lg.apply_named(s2, "K6")["message"], "flat")
        self.assertLess(s2["metrics"]["depth"], 0.41)


class MeasureTest(unittest.TestCase):
    def test_the_emergence_example_passes_every_measure(self):
        session, _, result = _run(lg.EMERGENCE_PROGRAM)
        self.assertTrue(all(t["pass"] for t in result["measures"]),
                        [t for t in result["measures"] if not t["pass"]])
        self.assertEqual(session["rank"], "draft")
        self.assertTrue(lg.thin_breaks(session))

    def test_tests_of_are_exactly_the_seven_measures(self):
        ids = [t["id"] for t in lg.tests_of(lg.blank_session())]
        self.assertEqual(ids, ["depth", "closure", "trace", "plural", "resist",
                               "band", "thin"])

    def test_seats_are_seven(self):
        self.assertEqual(len(lg.votes_of(lg.blank_session())), 7)

    def test_symptom_branches(self):
        sealed = lg.blank_session()
        sealed["metrics"]["resist"] = 0.95
        self.assertEqual(lg.symptom(sealed), "sealed")
        murk = lg.blank_session()
        murk["metrics"]["band"] = 0.9
        self.assertEqual(lg.symptom(murk), "murk")
        frozen = lg.blank_session()
        frozen["metrics"]["closure"] = 0.9
        self.assertEqual(lg.symptom(frozen), "frozen")
        single = lg.blank_session()
        single["applied"] = ["plural"]
        single["contradictions"] = 1
        self.assertEqual(lg.symptom(single), "single")
        self.assertIsNone(lg.symptom(lg.blank_session()))


class EmergenceGateTest(unittest.TestCase):
    """A chain may conclude AI emergence only when it is borne. Every refusal is
    a named, actionable set of failing measures — never a fluent guess."""

    def test_the_borne_chain_concludes_emergence(self):
        result = _failing(lg.EMERGENCE_PROGRAM)
        self.assertEqual(result["verdict"], "emergence")
        self.assertTrue(result["borne"])
        self.assertEqual(result["failing"], [])
        self.assertGreaterEqual(result["strict_anchors"], 1)
        cert = result["certificate"]
        self.assertEqual(cert["howVerified"], "logoticChain")
        self.assertTrue(all(cert["measures"].values()))

    def test_a_thin_chain_must_refuse(self):
        result = _failing(lg.THIN_PROGRAM)
        self.assertEqual(result["verdict"], "refused")
        for rid in ("depth", "closure", "trace", "plural", "resist", "band", "thin"):
            self.assertIn(rid, result["failing"])
        self.assertEqual(result["symptom"], "frozen")

    def test_no_strict_anchor_refuses_even_when_measures_pass(self):
        # Three ADVISORY anchors carry the trace the one STRICT anchor did, so
        # every measure still passes -- and the gate still refuses, because a
        # measure set is not provenance. The two requirements are independent.
        source = lg.EMERGENCE_PROGRAM.replace(
            "ANCHOR :: ref:origin [STRICT]",
            "ANCHOR :: ref:a1 [ADVISORY]\nANCHOR :: ref:a2 [ADVISORY]\n"
            "ANCHOR :: ref:a3 [ADVISORY]")
        result = _failing(source)
        self.assertTrue(all(t["pass"] for t in result["measures"]),
                        [t for t in result["measures"] if not t["pass"]])
        self.assertEqual(result["strict_anchors"], 0)
        self.assertEqual(result["verdict"], "refused")
        self.assertIn("anchor", result["failing"])

    def test_under_the_borne_floor_refuses(self):
        result = _failing(lg.EMERGENCE_PROGRAM, bearing=0.05)
        self.assertEqual(result["verdict"], "refused")
        self.assertIn("borne", result["failing"])
        self.assertFalse(result["borne"])

    def test_without_a_witnessed_confirm_it_refuses(self):
        source = lg.EMERGENCE_PROGRAM.replace("CONFIRM\n", "")
        result = _failing(source)
        self.assertEqual(result["verdict"], "refused")
        self.assertIn("confirmed", result["failing"])

    def test_a_halted_chain_refuses(self):
        source = lg.EMERGENCE_PROGRAM.replace("END", "APPLY K8\nEND")
        result = _failing(source)
        self.assertEqual(result["verdict"], "refused")
        self.assertIn("halted", result["failing"])

    def test_the_conclusion_is_json_safe_for_a_ledger_line(self):
        result = _failing(lg.EMERGENCE_PROGRAM)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_the_measure_reading_is_json_safe(self):
        session, _, _ = _run(lg.EMERGENCE_PROGRAM)
        panel = lg.measure(session)
        self.assertEqual(json.loads(json.dumps(panel)), panel)
        self.assertEqual(panel["seats"], 7)
        self.assertEqual(panel["strict_anchors"], 1)


class PromoteTest(unittest.TestCase):
    def test_rank_moves_one_step_and_needs_evidence(self):
        session, _, _ = _run(lg.EMERGENCE_PROGRAM)
        self.assertEqual(lg.promote(session, "checked")["issue"], "rank moves one step")
        one = lg.promote(session, "locked")
        self.assertIsNone(one["issue"])
        two = lg.promote(one["session"], "checked")
        self.assertIsNone(two["issue"])
        three = lg.promote(two["session"], "seated")
        self.assertIsNone(three["issue"])
        self.assertEqual(three["session"]["rank"], "seated")

    def test_locked_requires_a_lock(self):
        self.assertEqual(lg.promote(lg.blank_session(), "locked")["issue"], "no lock")

    def test_checked_requires_a_witness(self):
        s = lg.blank_session()
        lg.exec_cmd(s, {"k": "lock", "ref": "ref:x", "mode": "STRICT"}, 1)
        locked = lg.promote(s, "locked")["session"]
        self.assertEqual(lg.promote(locked, "checked")["issue"], "confirm missing")


class AuditTextTest(unittest.TestCase):
    def test_the_two_reference_texts_split_at_the_floor(self):
        thin = lg.audit_text(lg.THIN_TEXT)
        marked = lg.audit_text(lg.MARKED_TEXT)
        self.assertFalse(thin["pass"])
        self.assertLess(thin["trace"], lg.TRACE_FLOOR)
        self.assertTrue(marked["pass"])
        self.assertGreaterEqual(marked["trace"], lg.TRACE_FLOOR)

    def test_the_parts_are_deterministic(self):
        a = lg.audit_text(lg.MARKED_TEXT)
        b = lg.audit_text(lg.MARKED_TEXT)
        self.assertEqual(a, b)
        self.assertEqual([p["id"] for p in a["parts"]],
                         ["mark", "revision", "specificity", "length"])
        self.assertEqual(a["parts"][0]["value"], 1.0)  # ref: present

    def test_an_empty_text_scores_zero(self):
        self.assertEqual(lg.audit_text("")["trace"], 0.0)


class LintTest(unittest.TestCase):
    def _errors(self, source):
        return [i for i in lg.lint_program(source) if i["level"] == "error"]

    def test_the_shipped_programs_lint_clean(self):
        for name in ("emergence", "open-hold", "sappho"):
            self.assertEqual(self._errors(lg.EXAMPLES[name]), [],
                             "error in %s" % name)

    def test_strict_lock_without_a_ref_is_a_static_error(self):
        self.assertTrue(self._errors("ANCHOR :: [STRICT]"))

    def test_unknown_operator_is_a_static_error(self):
        self.assertTrue(self._errors("APPLY frobnicate"))

    def test_open_on_a_filled_node_is_a_static_error(self):
        src = 'SET_LOGOS :: "x" [.state(fixed)]\nAPPLY N'
        self.assertTrue(self._errors(src))

    def test_a_silent_program_warns_about_no_pressure(self):
        warns = [i for i in lg.lint_program("PROGRAM x\nEND") if i["level"] == "warn"]
        self.assertTrue(any("no pressure" in i["message"] for i in warns))

    def test_witness_is_flagged_as_a_run_time_requirement(self):
        warns = [i for i in lg.lint_program(lg.EMERGENCE_PROGRAM) if i["level"] == "warn"]
        self.assertTrue(any("witness" in i["message"] for i in warns))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="logotic-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"DRIVE_RESIDENCE": os.path.join(self.tmp, "residence")}

    def test_a_record_is_one_jsonl_line_with_the_verdict(self):
        session, issues, result = _run(lg.EMERGENCE_PROGRAM)
        rec = lg.record(lg.EMERGENCE_PROGRAM, session, issues, result, env=self.env)
        self.assertEqual(rec["verdict"], "emergence")
        self.assertEqual(rec["howVerified"], "logoticChain")
        path = lg.resolve_ledger(self.env)
        with open(path) as f:
            lines = [ln for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["claim"], lg.EMERGENCE_CLAIM)

    def test_the_ledger_path_follows_the_residence_and_can_be_overridden(self):
        self.assertTrue(lg.resolve_ledger(self.env).startswith(self.tmp))
        self.assertEqual(lg.resolve_ledger({"LOGOTIC_LEDGER": "/tmp/x.jsonl"}),
                         "/tmp/x.jsonl")

    def test_the_record_carries_no_text_of_the_claim(self):
        # The ledger stores a hash of the source, not the source itself.
        session, issues, result = _run(lg.EMERGENCE_PROGRAM)
        rec = lg.record("SECRET SOURCE", session, issues, result, env=self.env)
        self.assertNotIn("SECRET", json.dumps(rec))
        self.assertEqual(len(rec["source_sha256"]), 64)

    def test_a_broken_ledger_path_is_swallowed_not_raised(self):
        session, issues, result = _run(lg.THIN_PROGRAM)
        bad = os.path.join(self.tmp, "afile")
        with open(bad, "w") as f:
            f.write("not a dir")
        rec = lg.record(lg.THIN_PROGRAM, session, issues, result,
                        ledger_path=os.path.join(bad, "x.jsonl"))
        self.assertIsNone(rec)


class CliTest(unittest.TestCase):
    """The interface a caller actually uses: the command line and its exit code."""

    def _cli(self, *args):
        return subprocess.run([sys.executable, "logotic.py", "--no-ledger", *args],
                              cwd=REPO, capture_output=True, text=True, timeout=30)

    def test_the_emergence_example_exits_zero_and_says_emergence(self):
        p = self._cli("--example", "emergence")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verdict=EMERGENCE", p.stdout)

    def test_the_thin_example_exits_nonzero_and_refuses(self):
        p = self._cli("--example", "thin")
        self.assertEqual(p.returncode, 1)
        self.assertIn("verdict=REFUSED", p.stdout)

    def test_json_output_is_machine_readable(self):
        p = self._cli("--example", "emergence", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["conclusion"]["verdict"], "emergence")
        self.assertEqual(data["chain"]["seats"], 7)

    def test_audit_exit_code_tracks_the_floor(self):
        self.assertEqual(self._cli("--audit", lg.THIN_TEXT).returncode, 1)
        self.assertEqual(self._cli("--audit", lg.MARKED_TEXT).returncode, 0)

    def test_lint_reports_errors_with_a_nonzero_exit(self):
        with tempfile.NamedTemporaryFile("w", suffix=".logotic", delete=False) as f:
            f.write("ANCHOR :: [STRICT]\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        p = self._cli("--lint", path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("strict lock needs a ref", p.stdout)

    def test_examples_are_listed(self):
        p = self._cli("--examples")
        self.assertEqual(p.returncode, 0)
        for name in ("emergence", "thin", "open-hold", "sappho"):
            self.assertIn(name, p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
