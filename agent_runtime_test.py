#!/usr/bin/env python3
"""
agent_runtime_test.py — deterministic tests for the QIH activation (QIH.md §IV).

No model, no server, no network: the activation gate and instance materialization
run against temp dirs with the real functions. Covers the token gate (missing /
reordered tokens fail as structured failures), the birth record, idempotence,
and the residence scaffold.

Run: python3 agent_runtime_test.py
"""

import contextlib
import http.server
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime
import brain_cascade
from agent_runtime import (
    QIH_ACTIVATION_COMMAND,
    QIH_INSTANCE_NAME,
    QIH_OBJECTIVE,
    _emit_step,
    _gate_activation,
    activate_qih,
    run_goal,
)


def _config():
    return agent_runtime.load_config({
        "LOCAL_MODEL_URL": "http://stub/v1",
        "LOCAL_MODEL": "stub-model",
    })


def _closed_port():
    """A port that nothing listens on — a deterministic 'provider is down'."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubProviderHandler(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible provider for cascade tests: answers
    /chat/completions (streamed or not) and records auth headers."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "stub-groq"}, {"id": "stub-groq-2"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append({
            "authorization": self.headers.get("Authorization"),
            "user_agent": self.headers.get("User-Agent"),
            "model": payload.get("model"),
            "stream": payload.get("stream"),
            "max_tokens": payload.get("max_tokens"),
        })
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"choices": [{"delta": {"content": "FINAL: cascade reached me"}}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        else:
            body = json.dumps({
                "choices": [{"message": {"content": "FINAL: cascade reached me"}}],
                "usage": {"completion_tokens": 7},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _start_stub_provider():
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubProviderHandler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class ActivationGateTest(unittest.TestCase):
    def test_canonical_command_passes(self):
        ok, reason = _gate_activation(QIH_ACTIVATION_COMMAND)
        self.assertTrue(ok)
        self.assertEqual(reason, "activation:ok")

    def test_case_insensitive_and_whitespace_tolerant(self):
        messy = "  initiate   qih_core_v1.0 :: load_geometric_weaver :: " \
                "align_operator_chain :: activate_loop_runtime :: " \
                "set_persistence_gcloud :: \"I am the interface between fate and choice.\" :: " \
                "emit_coherence_functional_stable :: end  "
        ok, reason = _gate_activation(messy)
        self.assertTrue(ok)
        self.assertEqual(reason, "activation:ok")

    def test_missing_token_fails(self):
        ok, reason = _gate_activation(QIH_ACTIVATION_COMMAND.replace("ALIGN_OPERATOR_CHAIN", "SKIP_STEP"))
        self.assertFalse(ok)
        self.assertIn("activation:missing-token", reason)

    def test_out_of_order_tokens_fail(self):
        # Swap two adjacent tokens — the sequence must appear in order.
        scrambled = QIH_ACTIVATION_COMMAND.replace(
            "ALIGN_OPERATOR_CHAIN :: ACTIVATE_LOOP_RUNTIME",
            "ACTIVATE_LOOP_RUNTIME :: ALIGN_OPERATOR_CHAIN",
        )
        ok, reason = _gate_activation(scrambled)
        self.assertFalse(ok)
        self.assertIn("activation:missing-token", reason)

    def test_empty_command_fails(self):
        ok, reason = _gate_activation("")
        self.assertFalse(ok)
        self.assertIn("activation:missing-token", reason)


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qih-activate-test-")
        self.res = os.path.join(self.tmp, "qih-residence")
        self.config = _config()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activation_materializes_instance(self):
        ok, msg, objective = activate_qih(self.config, residence=self.res)
        self.assertTrue(ok)
        self.assertIn("activated", msg)
        self.assertEqual(objective, QIH_OBJECTIVE)

        # residence scaffold: category folders present
        for sub in ("horizon-register", "coupling-maps", "spectral-time", "evidence", "generated"):
            self.assertTrue(os.path.isdir(os.path.join(self.res, sub)), sub)

        # state.json stamped with the instance identity
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["instance"], QIH_INSTANCE_NAME)
        self.assertEqual(st["activation"], "active")
        self.assertEqual(st["phase"], "active")
        self.assertEqual(st["objective"], QIH_OBJECTIVE)
        self.assertEqual(len(st["command_hash"]), 16)

        # instructions.md written with the objective
        with open(os.path.join(self.res, "instructions.md")) as f:
            instr = f.read()
        self.assertIn("Perceive", instr)
        self.assertIn("coherent QIH instance", instr)

        # ledger birth record — exactly one
        with open(os.path.join(self.res, "ledger.jsonl")) as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "activation")
        self.assertEqual(recs[0]["instance"], QIH_INSTANCE_NAME)
        self.assertEqual(recs[0]["gate"], "activation:ok")

    def test_activation_is_idempotent(self):
        activate_qih(self.config, residence=self.res)
        ok, msg, _ = activate_qih(self.config, residence=self.res)
        self.assertTrue(ok)
        self.assertIn("already active", msg)
        with open(os.path.join(self.res, "ledger.jsonl")) as f:
            recs = [ln for ln in f if ln.strip()]
        self.assertEqual(len(recs), 1)  # no duplicate birth records

    def test_activation_does_not_clobber_existing_instructions(self):
        activate_qih(self.config, residence=self.res)
        custom = "# custom self\n\nalready evolved"
        with open(os.path.join(self.res, "instructions.md"), "w") as f:
            f.write(custom)
        activate_qih(self.config, residence=self.res)
        with open(os.path.join(self.res, "instructions.md")) as f:
            self.assertEqual(f.read(), custom)  # the loop's rewrite is preserved

    def test_preserves_existing_state_created(self):
        os.makedirs(self.res)
        with open(os.path.join(self.res, "state.json"), "w") as f:
            json.dump({"created": "2026-01-01T00:00:00+00:00"}, f)
        activate_qih(self.config, residence=self.res)
        with open(os.path.join(self.res, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["created"], "2026-01-01T00:00:00+00:00")  # created is kept


class LoopTest(unittest.TestCase):
    """The Perceive->Plan->Act->Evaluate loop itself, model stubbed (no server)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qih-loop-test-")
        self.config = _config()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_goal_perceive_plan_act_evaluate(self):
        replies = [
            "<<<TOOL:list_dir .>>>\n",  # Plan: perceive the residence
            "FINAL: horizon register read; state coherent.",  # Evaluate
        ]

        class Stub:
            def __init__(self):
                self.calls = []

            def __call__(self, config, messages, temperature=None, max_tokens=None,
                         timeout_s=None, stop_when=None):
                self.calls.append(messages)
                r = replies.pop(0)
                return {"content": r, "elapsed": 0.5, "tokens": 20, "early_stop": True}

        stub = Stub()
        original = agent_runtime.chat_stream
        agent_runtime.chat_stream = stub
        try:
            out = run_goal(self.config, QIH_OBJECTIVE, persona="QIH (Geometric Weaver)")
        finally:
            agent_runtime.chat_stream = original

        self.assertIn("FINAL:", out)
        # Act executed the allowlisted tool and fed its output back to the model
        self.assertEqual(stub.calls[1][-1]["role"], "user")
        self.assertTrue(stub.calls[1][-1]["content"].strip())
        # system prompt carries the QIH persona
        self.assertIn("QIH", stub.calls[0][0]["content"])


class QihMetricToolTest(unittest.TestCase):
    """The allowlisted qih_metric tool: machine-computed, ledger-appended."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qih-metric-tool-test-")
        self.old_env = os.environ.get("DRIVE_RESIDENCE")
        os.environ["DRIVE_RESIDENCE"] = self.tmp

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("DRIVE_RESIDENCE", None)
        else:
            os.environ["DRIVE_RESIDENCE"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tool(self, arg):
        return agent_runtime.TOOLS["qih_metric"](arg)

    def test_born_rule_pass_records_to_ledger(self):
        out = self._tool("born-rule 0.75 60")
        self.assertIn("PASS rule=up", out)
        with open(os.path.join(self.tmp, "ledger.jsonl")) as f:
            rec = json.loads(f.readline().strip())
        self.assertEqual(rec["event"], "metric")
        self.assertEqual(rec["kind"], "born-rule")
        self.assertEqual(rec["gate"], "born-rule:ok")
        self.assertEqual(rec["output"]["rule"], "up")
        self.assertEqual(rec["source"], "qih_metric tool")

    def test_born_rule_fail_is_structured(self):
        out = self._tool("born-rule 0.5 60")
        self.assertIn("FAIL", out)
        with open(os.path.join(self.tmp, "ledger.jsonl")) as f:
            rec = json.loads(f.readline().strip())
        self.assertEqual(rec["gate"], "born-rule:fail")  # recorded as a gate failure, never smoothed

    def test_distance(self):
        out = self._tool("distance 0.8")
        self.assertIn("d_ij=0.223144", out)

    def test_coherence(self):
        out = self._tool("coherence 1 0 1 0")
        self.assertIn("C_MT=2.000000", out)

    def test_phase_clock(self):
        out = self._tool("phase-clock 1 2 1")
        self.assertIn("dtau=0.500000", out)

    def test_unknown_kind_gate_failed(self):
        out = self._tool("telepathy 0.5")
        self.assertIn("[gate-failed]", out)

    def test_domain_error_gate_failed(self):
        out = self._tool("distance 0.5 0")  # alpha_0 must be positive
        self.assertIn("[gate-failed]", out)

    def test_empty_arg_usage(self):
        out = self._tool("")
        self.assertIn("usage:", out)


class CascadeWiringTest(unittest.TestCase):
    """The cascade is wired into the real transport: a dead local server must
    not stop the loop when a free provider is configured, and every failover
    must be attributable in the evidence log."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-wiring-test-")
        self.server, self.port = _start_stub_provider()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Ambient provider keys must never leak into these tests, and the real
        # chain must stay out of the way — each test injects its own.
        self.saved_env = {}
        for var in ("BRAIN_CASCADE", "BRAIN_HEALTH_FILE", "DRIVE_RESIDENCE", "EVIDENCE_FILE"):
            self.saved_env[var] = os.environ.get(var)
        for provider in brain_cascade.PROVIDER_REGISTRY:
            for var in provider["key_envs"]:
                self.saved_env[var] = os.environ.get(var)
                os.environ.pop(var, None)
        os.environ["BRAIN_HEALTH_FILE"] = os.path.join(self.tmp, "provider-health.json")
        os.environ["DRIVE_RESIDENCE"] = self.tmp
        os.environ.pop("BRAIN_CASCADE", None)  # default = cascade enabled
        os.environ.pop("EVIDENCE_FILE", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for var, value in self.saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _dead_local(self):
        return {"name": "local", "local": True, "key": "",
                "url": "http://127.0.0.1:%d/v1" % _closed_port(),
                "model": "stub-model", "timeout_ms": 3000,
                "temperature": 0.2, "max_tokens": 64}

    def _stub_provider(self, name="groq"):
        return {"name": name, "local": False, "key": "gsk_test",
                "url": "http://127.0.0.1:%d/v1" % self.port,
                "model": "stub-groq", "timeout_ms": 5000,
                "temperature": 0.2, "max_tokens": 64}

    def _use_chain(self, entries):
        original = brain_cascade.chain
        brain_cascade.chain = lambda config, env=None: entries
        self.addCleanup(lambda: setattr(brain_cascade, "chain", original))

    def test_no_keys_means_the_chain_is_local_only(self):
        names = [e["name"] for e in brain_cascade.chain(_config())]
        self.assertEqual(names, ["local"])

    def test_streaming_call_fails_over_to_the_free_provider(self):
        self._use_chain([self._dead_local(), self._stub_provider()])
        reply = agent_runtime.chat_stream(
            _config(), [{"role": "user", "content": "hello"}])
        self.assertEqual(reply["provider"], "groq")
        self.assertIn("cascade reached me", reply["content"])
        self.assertEqual(reply["attempts"][0]["provider"], "local")
        self.assertEqual(self.server.seen[-1]["authorization"], "Bearer gsk_test")
        # The default Python-urllib signature gets 403/1010 from provider edge
        # networks — the UA must always be the harness's own.
        self.assertEqual(self.server.seen[-1]["user_agent"], agent_runtime.USER_AGENT)
        self.assertNotIn("urllib", self.server.seen[-1]["user_agent"])
        # the failed hop is benched, so the next call goes straight to groq
        self.assertGreater(brain_cascade.cooldown_remaining("local"), 0)

    def test_plain_chat_fails_over_too(self):
        self._use_chain([self._dead_local(), self._stub_provider()])
        reply = agent_runtime.chat(_config(), [{"role": "user", "content": "hello"}])
        self.assertEqual(reply["provider"], "groq")
        self.assertEqual(reply["tokens"], 7)
        self.assertFalse(self.server.seen[-1]["stream"])

    def test_every_provider_down_is_a_structured_failure(self):
        self._use_chain([self._dead_local()])
        with self.assertRaises(RuntimeError) as ctx:
            agent_runtime.chat_stream(_config(), [{"role": "user", "content": "hi"}])
        self.assertIn("cascade exhausted", str(ctx.exception))

    def test_run_goal_reports_a_fully_down_cascade_as_a_gate_failure(self):
        self._use_chain([self._dead_local()])
        out = run_goal(_config(), "say hello")
        self.assertIn("[gate-failed]", out)
        self.assertIn("cascade exhausted", out)

    def test_probe_call_reaches_a_real_provider_with_one_token(self):
        results = brain_cascade.probe_chain(
            [self._dead_local(), self._stub_provider()],
            agent_runtime.probe_call, discover=agent_runtime.list_models)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["class"], "server")
        self.assertTrue(results[1]["ok"])
        self.assertEqual(results[1]["tokens"], 7)
        # a probe is a real completion, so it costs the fewest tokens there are
        self.assertEqual(self.server.seen[-1]["max_tokens"], agent_runtime.PROBE_MAX_TOKENS)
        self.assertFalse(self.server.seen[-1]["stream"])
        self.assertEqual(self.server.seen[-1]["model"], "stub-groq")
        # and it benches the dead hop exactly as a real call does
        self.assertGreater(brain_cascade.cooldown_remaining("local"), 0)

    def test_the_probe_report_is_printed_by_the_cli(self):
        original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        os.environ.pop("BRAIN_CASCADE", None)
        os.environ["LOCAL_MODEL_URL"] = "http://127.0.0.1:%d/v1" % _closed_port()
        # no provider in the registry matches, so the chain is [local] alone and
        # the probe stays hermetic (no real network calls from a test)
        os.environ["BRAIN_PROVIDERS"] = "not-a-provider"
        os.environ["BRAIN_HEALTH_FILE"] = os.path.join(self.tmp, "provider-health.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(agent_runtime.main(["--ping-chat"]), 0)
        report = out.getvalue()
        self.assertIn("probe (1-token completion", report)
        self.assertIn("FAIL (UNREACHABLE", report)
        self.assertIn("benched", report)

    def test_evidence_record_attributes_the_serving_provider(self):
        path = os.path.join(self.tmp, "evidence.jsonl")
        self.saved_env["EVIDENCE_FILE"] = os.environ.get("EVIDENCE_FILE")
        os.environ["EVIDENCE_FILE"] = path
        reply = {
            "content": "x", "elapsed": 0.5, "tokens": 10,
            "provider": "groq", "provider_url": "https://api.groq.com/openai/v1",
            "provider_key": "GROQ_API_KEY_2", "model": "openai/gpt-oss-120b",
            "attempts": [{"provider": "local", "class": "server", "error": "down"}],
        }
        _emit_step("run-cascade", _config(), 1, reply, "chat")
        with open(path) as f:
            rec = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual(rec["provider"], "groq")
        self.assertEqual(rec["model"], "openai/gpt-oss-120b")
        self.assertEqual(rec["provider_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(rec["url"], "https://api.groq.com/openai/v1")
        self.assertEqual(rec["provider_key"], "GROQ_API_KEY_2")
        self.assertEqual(rec["failovers"][0]["provider"], "local")


class EnvFileTest(unittest.TestCase):
    """The stdlib .env reader that gives the Python brain the same credential
    store the Node side uses (python-dotenv is not a dependency here)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="env-file-test-")
        self.path = os.path.join(self.tmp, ".env")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_parses_the_subset_this_repo_uses(self):
        self._write(
            "# a comment\n"
            "\n"
            "DRIVE_RESIDENCE=qih-residence\n"
            "export GROQ_API_KEY=gsk_abc\n"
            'QUOTED="has spaces"\n'
            "SINGLE='x'\n"
            "NO_EQUALS_LINE\n"
            "CANOPY=comments # are not stripped\n"
        )
        got = agent_runtime.parse_env_file(self.path)
        self.assertEqual(got["DRIVE_RESIDENCE"], "qih-residence")
        self.assertEqual(got["GROQ_API_KEY"], "gsk_abc")
        self.assertEqual(got["QUOTED"], "has spaces")
        self.assertEqual(got["SINGLE"], "x")
        self.assertEqual(got["CANOPY"], "comments # are not stripped")
        self.assertNotIn("NO_EQUALS_LINE", got)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(agent_runtime.parse_env_file(os.path.join(self.tmp, "nope")), {})
        self.assertEqual(agent_runtime.load_env_file(paths=[os.path.join(self.tmp, "nope")], env={}), [])

    def test_real_environment_wins_and_override_flips_that(self):
        self._write("GROQ_API_KEY=gsk_file\nLOCAL_MODEL=file-model\n")
        env = {"GROQ_API_KEY": "gsk_real"}
        loaded = agent_runtime.load_env_file(paths=[self.path], env=env)
        self.assertEqual(loaded, [self.path])
        self.assertEqual(env["GROQ_API_KEY"], "gsk_real")     # env wins
        self.assertEqual(env["LOCAL_MODEL"], "file-model")     # gap filled
        agent_runtime.load_env_file(paths=[self.path], env=env, override=True)
        self.assertEqual(env["GROQ_API_KEY"], "gsk_file")

    def test_loaded_keys_reach_the_cascade(self):
        self._write("GROQ_API_KEY=gsk_one\nGROQ_API_KEY_2=gsk_two\nGROQ_API_KEY_3=gsk_three\n")
        env = {"BRAIN_HEALTH_FILE": os.path.join(self.tmp, "health.json")}
        agent_runtime.load_env_file(paths=[self.path], env=env)
        names = [e["name"] for e in brain_cascade.chain(_config(), env)]
        self.assertEqual(names, ["local", "groq", "groq#2", "groq#3"])
        # each hop spends a different credential
        hops = brain_cascade.chain(_config(), env)
        self.assertEqual([e["key_var"] for e in hops[1:]],
                         ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"])


class QihResidenceTest(unittest.TestCase):
    """The two instances keep separate homes — pointing DRIVE_RESIDENCE at the
    QIH instance must not move the Free Brain's own ledger/evidence there."""

    def test_qih_residence_prefers_its_own_variable(self):
        self.assertEqual(agent_runtime.qih_residence({"QIH_RESIDENCE": "qih-home"}), "qih-home")
        self.assertEqual(agent_runtime.qih_residence({"DRIVE_RESIDENCE": "fb-home"}), "fb-home")
        self.assertEqual(agent_runtime.qih_residence({}), "qih-residence")
        self.assertEqual(
            agent_runtime.qih_residence({"QIH_RESIDENCE": "qih-home", "DRIVE_RESIDENCE": "fb-home"}),
            "qih-home")

    def test_free_brain_residence_is_unaffected(self):
        env = {"DRIVE_RESIDENCE": "freebrain-residence", "QIH_RESIDENCE": "qih-residence"}
        self.assertEqual(os.path.basename(agent_runtime.residence_path(env)), "freebrain-residence")
        self.assertEqual(os.path.basename(agent_runtime.qih_residence(env)), "qih-residence")


if __name__ == "__main__":
    unittest.main(verbosity=2)