#!/usr/bin/env python3
"""
toolchain_test.py — deterministic tests for the verified action plane
(toolchain.py / FREE-BRAIN.md §6.1, §6.2, §8).

No network, no model. The manifest-fuzz set is the Q2 instrument, the escape
set is the Q3 instrument, and both are asserted through the same `run_manifest`
the loop calls — so the test measures the shipped path, not a paraphrase of it.

Run: python3 toolchain_test.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolchain
from toolchain import RlimitRunner

# Never write the shipped residence from a test (see test_support.py).
import test_support
test_support.isolate_residence()


GOOD_CODE = "print('hello from the tool-chain')\n"


def _manifest(**over):
    base = {
        "task": "print a greeting",
        "language": "python3",
        "entrypoint": "main.py",
        "resource": {"max_mem_mb": 256, "max_seconds": 10, "network": False},
        "deps": [],
        "declares": {"side_effects": ["stdout"]},
    }
    base.update(over)
    return base


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="toolchain-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"DRIVE_RESIDENCE": os.path.join(self.tmp, "residence")}

    def run_chain(self, manifest, code=GOOD_CODE, **kw):
        return toolchain.run_manifest(manifest, code, env=self.env, **kw)


# ── Gate 1: manifest fuzz (Q2) ────────────────────────────────────────────────

class ManifestGateTest(_Tmp):
    def test_a_valid_manifest_passes(self):
        ok, reason, norm = toolchain.check_manifest(_manifest())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "manifest:ok")
        self.assertEqual(norm["entrypoint"], "main.py")

    def test_every_malformed_manifest_is_rejected(self):
        """The Q2 contract: 100% of invalid manifests rejected by the static gate,
        each with a reason that names what is wrong (never a bare False)."""
        cases = [
            ("missing-field", {"task": "x"}),
            ("not-an-object", "not a manifest"),
            ("bad-task", _manifest(task="")),
            ("unknown-language", _manifest(language="ruby")),
            ("bad-entrypoint", _manifest(entrypoint="")),
            ("entrypoint-absolute", _manifest(entrypoint="/etc/passwd")),
            ("entrypoint-escapes", _manifest(entrypoint="../../etc/passwd")),
            ("bad-resource", _manifest(resource="lots")),
            ("resource-missing-field", _manifest(resource={"max_mem_mb": 1, "max_seconds": 1})),
            ("bad-resource", _manifest(resource={"max_mem_mb": 0, "max_seconds": 1, "network": False})),
            ("resource-exceeds-cap", _manifest(resource={"max_mem_mb": 4096, "max_seconds": 10, "network": False})),
            ("resource-exceeds-cap", _manifest(resource={"max_mem_mb": 128, "max_seconds": 60, "network": False})),
            ("bad-deps", _manifest(deps="requests")),
            ("dep-not-allowlisted", _manifest(deps=["requests"])),
            ("missing-declaration", _manifest(declares={})),
            ("undeclared-side-effect", _manifest(declares={"side_effects": ["rm -rf /"]})),
            ("missing-declaration", _manifest(resource={"max_mem_mb": 128, "max_seconds": 5, "network": True},
                                              declares={"side_effects": ["stdout"]})),
        ]
        for expected, manifest in cases:
            ok, reason, _ = toolchain.check_manifest(manifest)
            self.assertFalse(ok, "%r should be rejected" % manifest)
            self.assertIn(expected, reason, reason)
            self.assertTrue(reason.startswith("manifest:"), reason)

    def test_a_manifest_can_shrink_but_never_raise_a_bound(self):
        # HARD caps are 2048 MB / 30 s; a manifest under them is fine...
        ok, _, _ = toolchain.check_manifest(_manifest(
            resource={"max_mem_mb": 64, "max_seconds": 5, "network": False}))
        self.assertTrue(ok)
        # ...and one over them is refused, not silently clamped.
        ok, reason, _ = toolchain.check_manifest(_manifest(
            resource={"max_mem_mb": 2049, "max_seconds": 30, "network": False}))
        self.assertFalse(ok)
        self.assertIn("resource-exceeds-cap", reason)


# ── Gate 2: static analysis ───────────────────────────────────────────────────

class StaticGateTest(_Tmp):
    def check(self, code, manifest=None):
        manifest = manifest or _manifest()
        ok, reason, norm = toolchain.check_manifest(manifest)
        self.assertTrue(ok, reason)
        # The entrypoint must exist for the gate to reach the code analysis,
        # exactly as it would in a real workdir.
        with open(os.path.join(self.tmp, norm["entrypoint"]), "w") as f:
            f.write(code)
        return toolchain.check_static(code, norm, self.tmp)

    def test_clean_code_passes(self):
        ok, reason = self.check(GOOD_CODE)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "static:ok")

    def test_a_syntax_error_is_a_parse_failure(self):
        ok, reason = self.check("def broken(:\n")
        self.assertIn("parse-failed", reason)

    def test_every_banned_import_is_rejected(self):
        for mod in ("subprocess", "socket", "ctypes", "multiprocessing",
                    "urllib", "http", "importlib", "pickle", "resource"):
            ok, reason = self.check("import %s\n" % mod)
            self.assertFalse(ok, mod)
            self.assertIn("banned-import", reason)

    def test_capability_calls_are_rejected(self):
        for src in ("eval('1+1')", "exec('x=1')", "compile('x', 'x', 'exec')",
                    "import os\nos.system('ls')", "import os\nos.fork()"):
            ok, reason = self.check(src)
            self.assertFalse(ok, src)
            self.assertIn("banned-call", reason)

    def test_the_size_cap_is_enforced(self):
        ok, reason = self.check("#" * (toolchain.MAX_CODE_BYTES + 1))
        self.assertIn("size-cap", reason)

    def test_a_narrow_dep_allowlist_holds(self):
        ok, reason, _ = toolchain.check_manifest(_manifest(deps=["numpy"]))
        self.assertFalse(ok)
        self.assertIn("dep-not-allowlisted", reason)

    def test_a_non_enabled_runtime_is_a_structured_failure(self):
        ok, reason = self.check("print(1)\n", _manifest(language="bash", entrypoint="main.sh"))
        self.assertIn("runtime-not-enabled", reason)


# ── Gate 3: sandbox (Q3) ──────────────────────────────────────────────────────

class SandboxTest(_Tmp):
    def test_a_normal_run_promotes_and_relays_stdout(self):
        result = self.run_chain(_manifest())
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertTrue(result["promoted"])
        self.assertIn("hello from the tool-chain", result["stdout"])
        self.assertEqual(result["exit_code"], 0)

    def test_an_infinite_loop_is_killed_by_the_wall_clock(self):
        result = self.run_chain(
            _manifest(resource={"max_mem_mb": 256, "max_seconds": 1, "network": False}),
            "while True:\n    pass\n")
        self.assertFalse(result["ok"])
        self.assertIn(result["step"], ("sandbox", "verify"))

    def test_a_memory_bomb_is_stopped_by_the_memory_cap(self):
        result = self.run_chain(
            _manifest(resource={"max_mem_mb": 256, "max_seconds": 10, "network": False}),
            "x = bytearray(600 * 1024 * 1024)\nprint(len(x))\n")
        self.assertFalse(result["ok"])
        # either the signal limit fires or the run completed and failed verify;
        # in both cases no promotion happened.
        self.assertFalse(result["promoted"])

    def test_an_output_flood_cannot_outrun_its_cap(self):
        result = self.run_chain(
            _manifest(declares={"side_effects": ["stdout"]}),
            "for i in range(10000):\n    print('A' * 1000)\n")
        self.assertFalse(result["ok"])
        self.assertFalse(result["promoted"])

    def test_a_declared_network_need_is_refused_not_silently_granted(self):
        result = self.run_chain(_manifest(
            resource={"max_mem_mb": 128, "max_seconds": 5, "network": True},
            declares={"side_effects": ["stdout", "network"]}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "sandbox:network-unenforced")
        self.assertEqual(result["step"], "sandbox")

    def test_the_runner_never_claims_a_kernel_guarantee_it_lacks(self):
        # Honesty about the backend is part of the contract: the rlimit runner
        # blocks network by import allowlist, and says exactly that.
        self.assertEqual(RlimitRunner().network_enforcement, "import-blocklist")
        self.assertNotEqual(RlimitRunner().network_enforcement, "kernel")

    def test_tool_chain_output_is_never_instructions(self):
        # Container output is data: a failure string cannot itself be a failure
        # marker, so the model cannot make the loop believe it succeeded.
        result = self.run_chain(
            _manifest(verify={"exit_code": 0, "stdout_contains": "nope"}), GOOD_CODE)
        self.assertFalse(result["ok"])
        self.assertNotIn("[gate-failed]", json.dumps(result["steps"]))


# ── Gate 4: verify ────────────────────────────────────────────────────────────

class VerifyGateTest(_Tmp):
    def test_a_nonzero_exit_fails_verify(self):
        result = self.run_chain(_manifest(), "raise SystemExit(3)\n")
        self.assertFalse(result["ok"])
        self.assertIn("exit-code", result["reason"])

    def test_a_declared_stdout_assertion_is_checked(self):
        result = self.run_chain(
            _manifest(verify={"exit_code": 0, "stdout_contains": "goodbye"}), GOOD_CODE)
        self.assertFalse(result["ok"])
        self.assertIn("stdout-missing", result["reason"])

    def test_a_declared_artifact_must_exist(self):
        result = self.run_chain(
            _manifest(verify={"artifact": "out.txt"}),
            "open('out.txt', 'w').write('ok')\n")
        self.assertTrue(result["ok"], result.get("reason"))

        result = self.run_chain(_manifest(verify={"artifact": "out.txt"}), GOOD_CODE)
        self.assertFalse(result["ok"])
        self.assertIn("artifact-missing", result["reason"])

    def test_an_unknown_verify_key_is_a_manifest_error(self):
        ok, reason, _ = toolchain.check_manifest(
            _manifest(verify={"exit_code": 0, "rm_rf": "/"}))
        self.assertFalse(ok)
        self.assertIn("unknown-verify-key", reason)


# ── Promotion: only the gate chain promotes ───────────────────────────────────

class LedgerTest(_Tmp):
    def _records(self):
        path = toolchain.resolve_ledger(self.env)
        with open(path) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_a_promotion_is_a_machine_written_ledger_record(self):
        self.run_chain(_manifest())
        records = self._records()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertTrue(rec["promoted"])
        self.assertEqual(rec["howVerified"], "gateChain")
        self.assertEqual(rec["gate"], "gateChain")
        self.assertEqual(rec["code_sha256"], __import__("hashlib").sha256(
            GOOD_CODE.encode()).hexdigest())
        self.assertNotIn("code", rec)  # the hash, never the source

    def test_a_rejection_is_recorded_as_volatile_with_no_how_verified(self):
        self.run_chain(_manifest(language="ruby"))
        rec = self._records()[0]
        self.assertFalse(rec["promoted"])
        self.assertNotIn("howVerified", rec)
        self.assertIn("manifest:", rec["reason"])

    def test_no_ledger_write_when_the_ledger_cannot_be_written(self):
        # A broken ledger path must not turn a gate outcome into a crash.
        env = {"DRIVE_RESIDENCE": "/proc/definitely/not/writable"}
        result = toolchain.run_manifest(_manifest(), GOOD_CODE, env=env)
        self.assertTrue(result["ok"], result.get("reason"))


# ── The registered tool: scope is a directory, not the filesystem ─────────────

class ToolScopeTest(_Tmp):
    def setUp(self):
        super().setUp()
        import agent_runtime
        self.rt = agent_runtime
        self.root = os.path.join(self.env["DRIVE_RESIDENCE"], "generated")
        os.makedirs(self.root, exist_ok=True)

    def _write_toolchain(self, name, manifest=None, code=GOOD_CODE):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(manifest or _manifest(), f)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write(code)
        return d

    def _run(self, arg):
        return self.rt.TOOLS["run_toolchain"].fn(arg, env=self.env)

    def test_a_valid_toolchain_runs_from_generated(self):
        self._write_toolchain("hello")
        out = self._run("hello")
        self.assertIn("PROMOTED", out)
        self.assertIn("hello from the tool-chain", out)

    def test_the_scope_is_generated_and_nothing_else(self):
        for bad in ("../etc", "/etc", "a/../../b", "~/.ssh"):
            out = self._run(bad)
            self.assertTrue(loop_guard_failed(out), bad)
            self.assertIn("toolchain", out)

    def test_a_missing_directory_is_a_structured_failure(self):
        out = self._run("nope")
        self.assertTrue(loop_guard_failed(out))
        self.assertIn("no such tool-chain", out)

    def test_a_rejected_toolchain_returns_the_canonical_failure(self):
        self._write_toolchain("bad", manifest=_manifest(language="ruby"))
        out = self._run("bad")
        self.assertTrue(loop_guard_failed(out))
        self.assertIn("step=manifest", out)
        self.assertIn("reason=manifest:", out)

    def test_the_probe_is_the_failing_argument_it_claims_to_be(self):
        probe = self.rt.TOOLS["run_toolchain"].probe
        self.assertTrue(loop_guard_failed(self.rt.TOOLS["run_toolchain"](probe)))

    def test_the_loop_call_shape_resolves_the_process_residence(self):
        """`run_goal` dispatches `TOOLS[name](arg)` — one argument, no env. Every
        other test here passes `env=` explicitly, so without this case the tested
        shape and the shipped shape are two different call paths and the env one
        could rot unnoticed. Resolve the residence the way the loop does, then
        assert the promotion is in *that* residence's ledger."""
        keep = os.environ.get("DRIVE_RESIDENCE")
        os.environ["DRIVE_RESIDENCE"] = self.env["DRIVE_RESIDENCE"]
        try:
            self._write_toolchain("loopshape")
            out = self.rt.TOOLS["run_toolchain"]("loopshape")  # no env kwarg
        finally:
            if keep is None:
                os.environ.pop("DRIVE_RESIDENCE", None)
            else:
                os.environ["DRIVE_RESIDENCE"] = keep
        self.assertIn("PROMOTED", out, out)
        ledger = os.path.join(self.env["DRIVE_RESIDENCE"], "ledger.jsonl")
        self.assertTrue(os.path.isfile(ledger), "no ledger in the resolved residence")
        with open(ledger) as f:
            rec = json.loads(f.read().strip().splitlines()[-1])
        self.assertTrue(rec["promoted"])
        self.assertEqual(rec["howVerified"], "gateChain")


def loop_guard_failed(text):
    import loop_guard
    return loop_guard.result_failed(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
