#!/usr/bin/env python3
"""
brain_cascade_test.py — deterministic tests for the cascading free-provider
brain (brain_cascade.py / FREE-BRAIN.md §5).

No network, no model: the transport is injected as a Python callable, the
health file lives in a temp dir, and every provider decision is asserted
against the env dict passed in.

Run: python3 brain_cascade_test.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain_cascade as bc


def _config(**over):
    cfg = {
        "url": "http://127.0.0.1:11434/v1",
        "model": "local-model",
        "timeout_ms": 180000,
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    cfg.update(over)
    return cfg


def _env(tmp, **over):
    env = {
        "DRIVE_RESIDENCE": os.path.join(tmp, "residence"),
        "BRAIN_HEALTH_FILE": os.path.join(tmp, "provider-health.json"),
    }
    env.update(over)
    return env


class RegistryTest(unittest.TestCase):
    def test_every_provider_is_openai_compatible_and_free(self):
        self.assertTrue(bc.PROVIDER_REGISTRY)
        for p in bc.PROVIDER_REGISTRY:
            self.assertTrue(p["url"].startswith("https://"), p["name"])
            self.assertTrue(p["key_envs"], p["name"])
            self.assertTrue(p["models"], p["name"])
            self.assertIn("note", p)
        names = [p["name"] for p in bc.PROVIDER_REGISTRY]
        self.assertEqual(len(names), len(set(names)))

    def test_expected_free_providers_present(self):
        names = set(p["name"] for p in bc.PROVIDER_REGISTRY)
        for expected in ("groq", "cerebras", "gemini", "openrouter", "mistral", "github"):
            self.assertIn(expected, names)


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_keys_means_local_only(self):
        chain = bc.chain(_config(), _env(self.tmp))
        self.assertEqual([e["name"] for e in chain], ["local"])
        self.assertTrue(chain[0]["local"])

    def test_keys_append_providers_after_local(self):
        env = _env(self.tmp, GROQ_API_KEY="gsk_x", GEMINI_API_KEY="gm_x")
        names = [e["name"] for e in bc.chain(_config(), env)]
        self.assertEqual(names, ["local", "groq", "gemini"])

    def test_braincascade_0_forces_local_only(self):
        env = _env(self.tmp, GROQ_API_KEY="gsk_x", BRAIN_CASCADE="0")
        self.assertEqual([e["name"] for e in bc.chain(_config(), env)], ["local"])
        self.assertFalse(bc.cascade_enabled(env))

    def test_brain_providers_overrides_order_and_filter(self):
        env = _env(self.tmp, GROQ_API_KEY="a", GEMINI_API_KEY="b",
                   BRAIN_PROVIDERS="gemini,groq")
        self.assertEqual([e["name"] for e in bc.chain(_config(), env)],
                         ["local", "gemini", "groq"])
        env2 = _env(self.tmp, GROQ_API_KEY="a", GEMINI_API_KEY="b",
                    BRAIN_PROVIDERS="groq,unknown-provider")
        self.assertEqual([e["name"] for e in bc.chain(_config(), env2)], ["local", "groq"])

    def test_model_env_override_and_alternate_key_var(self):
        env = _env(self.tmp, GROQ_API_KEY="a", GROQ_MODEL="gpt-oss-120b")
        groq = [e for e in bc.chain(_config(), env) if e["name"] == "groq"][0]
        self.assertEqual(groq["model"], "gpt-oss-120b")
        # Gemini accepts either GEMINI_API_KEY or GOOGLE_API_KEY.
        alt = _env(self.tmp, GOOGLE_API_KEY="gm_x")
        self.assertEqual([e["name"] for e in bc.chain(_config(), alt)], ["local", "gemini"])

    def test_cloud_timeout_is_capped_for_fast_failover(self):
        env = _env(self.tmp, GROQ_API_KEY="a")
        groq = [e for e in bc.chain(_config(), env) if e["name"] == "groq"][0]
        self.assertEqual(groq["timeout_ms"], 60000)
        self.assertEqual(bc.chain(_config(), env)[0]["timeout_ms"], 180000)

    def test_the_stream_budget_reaches_every_hop(self):
        """The wall-clock budget only works if it reaches the hop that actually
        streams. A per-read timeout cannot bound a trickling stream, so a budget
        left behind on the top-level config would silently do nothing."""
        cfg = _config()
        cfg["stream_budget_ms"] = 12345
        env = {p["key_envs"][0]: "k" for p in bc.PROVIDER_REGISTRY}
        for entry in bc.chain(cfg, env):
            self.assertEqual(entry.get("stream_budget_ms"), 12345,
                             "hop %s lost the stream budget" % entry["name"])

    def test_no_budget_key_when_the_config_has_none(self):
        """Hand-built configs (tests, other callers) must keep the module default
        rather than inheriting a None budget."""
        cfg = _config()
        cfg.pop("stream_budget_ms", None)
        self.assertNotIn("stream_budget_ms", bc.chain(cfg)[0])

    def test_configured_names_lists_only_ready_providers(self):
        env = _env(self.tmp, MISTRAL_API_KEY="m")
        self.assertEqual(bc.configured_names(env), ["mistral"])

    def test_extra_keys_become_extra_hops(self):
        env = _env(self.tmp, GROQ_API_KEY="k1", GROQ_API_KEY_2="k2", GROQ_API_KEY_3="k3")
        chain = bc.chain(_config(), env)
        self.assertEqual([e["name"] for e in chain], ["local", "groq", "groq#2", "groq#3"])
        self.assertEqual([e["key"] for e in chain[1:]], ["k1", "k2", "k3"])
        self.assertEqual([e["key_var"] for e in chain[1:]],
                         ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"])
        # every hop still resolves its registry entry (model re-discovery needs it)
        self.assertEqual([e["registry"] for e in chain[1:]], ["groq", "groq", "groq"])

    def test_duplicate_keys_are_not_duplicate_hops(self):
        env = _env(self.tmp, GROQ_API_KEY="same", GROQ_API_KEY_2="same", GROQ_API_KEY_3="other")
        self.assertEqual([e["name"] for e in bc.chain(_config(), env)],
                         ["local", "groq", "groq#2"])

    def test_csv_keys_are_supported(self):
        env = _env(self.tmp, GROQ_API_KEYS="a, b ,a")
        self.assertEqual([e["name"] for e in bc.chain(_config(), env)], ["local", "groq", "groq#2"])

    def test_one_key_cooling_does_not_bench_its_siblings(self):
        env = _env(self.tmp, GROQ_API_KEY="k1", GROQ_API_KEY_2="k2",
                   BRAIN_RATE_LIMIT_COOLDOWN_S="300")
        bc.note_failure("groq#2", "429 rate limit", env)
        names = [e["name"] for e in bc.chain(_config(), env)]
        self.assertEqual(names, ["local", "groq", "groq#2"])  # benched key last
        self.assertEqual(bc.cooldown_remaining("groq", env), 0)
        self.assertGreater(bc.cooldown_remaining("groq#2", env), 0)

    def test_health_file_defaults_into_the_residence(self):
        env = {"DRIVE_RESIDENCE": "my-residence"}
        self.assertEqual(bc.health_path(env),
                         os.path.join("my-residence", "provider-health.json"))
        self.assertEqual(bc.health_path({"BRAIN_HEALTH_FILE": "/tmp/x.json"}), "/tmp/x.json")


class ClassifyTest(unittest.TestCase):
    def test_failure_classes(self):
        cases = {
            "groq HTTP 429 - rate limit exceeded": "rate-limit",
            "quota exhausted for today": "rate-limit",
            "gemini HTTP 401 - invalid api key": "auth",
            "openrouter HTTP 403 forbidden": "auth",
            "groq HTTP 404 - model llama-x does not exist": "model-missing",
            "mistral unreachable at https://x (timed out)": "server",
            "github HTTP 500 - internal error": "server",
            # A fingerprint/WAF block is not a rejected credential: reporting it
            # as auth would bench a good key for an hour (this happened live).
            "groq HTTP 403 - error code: 1010": "blocked",
            "cloudflare: banned your access": "blocked",
            # A paid wall must not be read as a soft throttle just because its
            # body says `"param": "quota"` — or it is retried every minute
            # forever. Observed live on Cerebras, verbatim:
            "cerebras HTTP 402 - {\"message\": \"Payment required to access this "
            "resource.\", \"param\": \"quota\", \"code\": \"payment_required\"}": "payment",
            "HTTP 402 insufficient credits": "payment",
        }
        for text, expected in cases.items():
            self.assertEqual(bc.classify(text), expected, text)


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-health-test-")
        self.env = _env(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failure_writes_a_persisted_cooldown(self):
        applied = bc.note_failure("groq", "groq HTTP 429 - rate limit", self.env)
        self.assertEqual(applied, bc.DEFAULT_RATE_LIMIT_COOLDOWN_S)
        self.assertGreater(bc.cooldown_remaining("groq", self.env), 0)
        with open(bc.health_path(self.env)) as f:
            state = json.load(f)
        self.assertEqual(state["groq"]["last_class"], "rate-limit")
        self.assertEqual(state["groq"]["total_fail"], 1)
        self.assertFalse(state["groq"]["ok"])

    def test_rate_limit_cooldown_is_configurable(self):
        env = _env(self.tmp, BRAIN_RATE_LIMIT_COOLDOWN_S="7")
        self.assertEqual(bc.note_failure("groq", "429 too many requests", env), 7)

    def test_auth_failures_cool_down_much_longer(self):
        self.assertEqual(bc.note_failure("gemini", "HTTP 401 unauthorized", self.env),
                         bc.DEFAULT_AUTH_COOLDOWN_S)

    def test_a_paid_wall_cools_down_for_an_hour(self):
        # Cerebras free trial: /models 200, chat completions 402. One hour, not
        # the 60s a real rate-limit gets — the key cannot succeed until an
        # operator tops up, so relitigating it every minute is pure noise.
        err = ('cerebras HTTP 402 - {"message": "Payment required to access this '
               'resource.", "param": "quota", "code": "payment_required"}')
        self.assertEqual(bc.note_failure("cerebras", err, self.env),
                         bc.DEFAULT_PAYMENT_COOLDOWN_S)
        self.assertEqual(bc.load_health(self.env)["cerebras"]["last_class"], "payment")

    def test_waf_blocks_get_their_own_short_cooldown(self):
        self.assertEqual(bc.note_failure("groq", "HTTP 403 - error code: 1010", self.env),
                         bc.DEFAULT_BLOCKED_COOLDOWN_S)
        self.assertEqual(bc.load_health(self.env)["groq"]["last_class"], "blocked")

    def test_success_clears_the_cooldown(self):
        bc.note_failure("groq", "429 rate limit", self.env)
        bc.note_success("groq", self.env)
        self.assertEqual(bc.cooldown_remaining("groq", self.env), 0)
        state = bc.load_health(self.env)
        self.assertTrue(state["groq"]["ok"])
        self.assertEqual(state["groq"]["fails"], 0)
        self.assertEqual(state["groq"]["total_fail"], 1)  # the history is kept

    def test_cooling_provider_is_moved_to_the_back_not_dropped(self):
        env = _env(self.tmp, GROQ_API_KEY="a", CEREBRAS_API_KEY="b",
                   BRAIN_RATE_LIMIT_COOLDOWN_S="300")
        bc.note_failure("groq", "429 rate limit", env)
        names = [e["name"] for e in bc.chain(_config(), env)]
        self.assertEqual(names, ["local", "cerebras", "groq"])
        self.assertGreater(bc.cooldown_remaining("groq", env), 0)

    def test_expired_cooldown_restores_priority(self):
        env = _env(self.tmp, GROQ_API_KEY="a", CEREBRAS_API_KEY="b")
        bc.note_failure("groq", "429 rate limit", env, cooldown_s=0)
        self.assertEqual(bc.cooldown_remaining("groq", env), 0)
        self.assertEqual([e["name"] for e in bc.chain(_config(), env)],
                         ["local", "groq", "cerebras"])

    def test_a_broken_health_file_never_breaks_a_run(self):
        env = _env(self.tmp, BRAIN_HEALTH_FILE=os.path.join(self.tmp, "nope", "health.json"))
        os.makedirs(os.path.dirname(bc.health_path(env)), exist_ok=True)
        with open(bc.health_path(env), "w") as f:  # invalid JSON on disk
            f.write("{not json")
        self.assertEqual(bc.cooldown_remaining("groq", env), 0)
        self.assertEqual(bc.load_health(env), {})


class CascadePolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-run-test-")
        self.env = _env(self.tmp)
        self.chain = bc.chain(_config(), _env(self.tmp, GROQ_API_KEY="a", CEREBRAS_API_KEY="b"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_provider_wins_when_it_works(self):
        calls = []
        reply = bc.run_cascade(self.chain, lambda p: calls.append(p["name"]) or {
            "content": "hi", "elapsed": 0.1, "tokens": 3}, env=self.env)
        self.assertEqual(calls, ["local"])
        self.assertEqual(reply["provider"], "local")
        self.assertEqual(reply["attempts"], [])
        self.assertTrue(bc.load_health(self.env)["local"]["ok"])

    def test_failover_to_the_next_provider(self):
        calls = []

        def call_one(provider):
            calls.append(provider["name"])
            if provider["name"] == "local":
                raise RuntimeError("local unreachable at http://x (refused)")
            return {"content": "hi", "elapsed": 0.2, "tokens": 5}

        reply = bc.run_cascade(self.chain, call_one, env=self.env)
        self.assertEqual(calls, ["local", "groq"])
        self.assertEqual(reply["provider"], "groq")
        self.assertEqual(reply["provider_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(reply["model"], bc.PROVIDER_BY_NAME["groq"]["models"][0])
        self.assertEqual(reply["provider_key"], "GROQ_API_KEY")
        self.assertEqual(len(reply["attempts"]), 1)
        self.assertEqual(reply["attempts"][0]["provider"], "local")
        # the failed provider is cooling down, the winner is marked healthy
        self.assertGreater(bc.cooldown_remaining("local", self.env), 0)
        self.assertTrue(bc.load_health(self.env)["groq"]["ok"])

    def test_all_providers_failing_is_a_structured_error(self):
        def call_one(provider):
            raise RuntimeError("%s HTTP 429 - rate limit" % provider["name"])

        with self.assertRaises(RuntimeError) as ctx:
            bc.run_cascade(self.chain, call_one, env=self.env)
        msg = str(ctx.exception)
        self.assertIn("cascade exhausted (3 providers)", msg)
        for name in ("local", "groq", "cerebras"):
            self.assertIn(name, msg)
        # every failure is recorded with its class, not swallowed
        state = bc.load_health(self.env)
        self.assertEqual(state["groq"]["last_class"], "rate-limit")

    def test_model_retirement_triggers_one_rediscovery_retry(self):
        """The live case: a provider retires the configured model name (Groq did
        exactly this to llama-3.3-70b-versatile) — the cascade re-reads /models
        once and recovers instead of failing the chain."""
        retired = bc.PROVIDER_BY_NAME["groq"]["models"][0]
        replacement = bc.PROVIDER_BY_NAME["groq"]["models"][1]
        tried = []

        def call_one(provider):
            tried.append((provider["name"], provider["model"]))
            if provider["name"] == "local":
                raise RuntimeError("local unreachable")
            if provider["model"] == retired:
                raise RuntimeError("groq HTTP 404 - model does not exist")
            return {"content": "ok", "elapsed": 0.1, "tokens": 2}

        def discover(provider):
            # whisper is listed first: discovery must still pick a chat model
            return ["whisper-large-v3", replacement] if provider["name"] == "groq" else []

        reply = bc.run_cascade(self.chain, call_one, env=self.env, discover=discover)
        self.assertEqual(reply["provider"], "groq")
        self.assertEqual(reply["model"], replacement)
        self.assertEqual(tried[-1], ("groq", replacement))
        self.assertEqual(reply["attempts"][-1]["class"], "model-missing")
        # recovered provider is healthy, not benched
        self.assertEqual(bc.cooldown_remaining("groq", self.env), 0)

    def test_empty_chain_is_a_structured_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            bc.run_cascade([], lambda p: {}, env=self.env)
        self.assertIn("no providers configured", str(ctx.exception))

    def test_discover_model_prefers_existing_names_then_patterns(self):
        preferred = bc.PROVIDER_BY_NAME["groq"]["models"]
        # the configured first choice wins when it still exists
        self.assertEqual(bc.discover_model({"name": "groq"}, list(reversed(preferred))),
                         preferred[0])
        # otherwise the preference *patterns* pick a survivor
        self.assertEqual(bc.discover_model({"name": "groq"}, ["mixtral-8x7b", "openai/gpt-oss-20b"]),
                         "openai/gpt-oss-20b")
        # nothing recognisable on offer: take the first usable chat model
        self.assertEqual(bc.discover_model({"name": "groq"}, ["weird-model"]), "weird-model")
        self.assertEqual(bc.discover_model({"name": "groq"}, []), "")

    def test_openrouter_picks_a_free_slot(self):
        ids = ["meta-llama/llama-3.3-70b-instruct", "nvidia/nemotron-3.5-lightning:free"]
        self.assertEqual(bc.discover_model({"name": "openrouter"}, ids),
                         "nvidia/nemotron-3.5-lightning:free")

    def test_discovery_refuses_non_chat_endpoints(self):
        # A live Groq listing is mostly whisper/guards/embeddings — picking one
        # of those for a tool loop would be worse than failing.
        ids = ["whisper-large-v3", "meta-llama/llama-prompt-guard-2-86m", "allam-2-7b"]
        self.assertEqual(bc.discover_model({"name": "groq"}, ids), "allam-2-7b")
        self.assertEqual(bc.discover_model({"name": "groq"}, ["whisper-large-v3"]), "")

    def test_second_key_hop_still_resolves_its_registry_entry(self):
        entry = {"name": "groq#2", "registry": "groq"}
        self.assertEqual(bc.discover_model(entry, ["openai/gpt-oss-120b"]), "openai/gpt-oss-120b")

    def test_reply_records_which_credential_served_it(self):
        env = _env(self.tmp, GROQ_API_KEY="k1", GROQ_API_KEY_2="k2")
        chain = bc.chain(_config(), env)

        def call_one(provider):
            if provider["name"] != "groq#2":
                raise RuntimeError("429 rate limit")
            return {"content": "hi", "elapsed": 0.1, "tokens": 2}

        reply = bc.run_cascade(chain, call_one, env=env)
        self.assertEqual(reply["provider"], "groq#2")
        self.assertEqual(reply["provider_key"], "GROQ_API_KEY_2")
        self.assertNotIn(reply["provider_key"], ("k1", "k2"))  # var name, never the value


class ProbeTest(unittest.TestCase):
    """`--ping-chat`: a real 1-token completion per hop. The /models probe cannot
    see a paid wall — a key lists models fine and refuses every completion, which
    is exactly what a Cerebras free-trial key did live on 2026-09-13."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-probe-test-")
        self.env = _env(self.tmp)
        self.chain = bc.chain(_config(), _env(self.tmp, GROQ_API_KEY="a"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe(self, call_one, discover=None):
        return bc.probe_chain(self.chain, call_one, env=self.env, discover=discover)

    def test_a_healthy_hop_is_reported_ok(self):
        results = self._probe(lambda p: {"content": "", "elapsed": 0.4, "tokens": 1})
        first = results[0]
        self.assertTrue(first["ok"])
        self.assertEqual(first["name"], "local")
        self.assertEqual(first["tokens"], 1)
        self.assertEqual(first["elapsed_s"], 0.4)
        self.assertEqual(first["class"], "")
        self.assertTrue(bc.load_health(self.env)["local"]["ok"])

    def test_a_paid_wall_is_a_result_not_an_exception(self):
        def call_one(provider):
            if provider["name"] == "local":
                raise RuntimeError("local unreachable at http://x (refused)")
            raise RuntimeError('groq HTTP 402 - {"message": "Payment required to access '
                               'this resource.", "param": "quota", "code": "payment_required"}')

        results = self._probe(call_one)  # must not raise: the report is the result
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertEqual(results[1]["class"], "payment")
        self.assertEqual(results[1]["cooldown_s"], bc.DEFAULT_PAYMENT_COOLDOWN_S)
        self.assertIn("402", results[1]["error"])
        # probing benches the hop exactly as a real call would
        self.assertEqual(bc.load_health(self.env)["groq"]["last_class"], "payment")
        self.assertEqual([e["name"] for e in bc.chain(_config(), _env(self.tmp, GROQ_API_KEY="a"))],
                         ["local", "groq"])  # both benched -> order preserved, none dropped

    def test_a_flapped_hop_loses_priority(self):
        def call_one(provider):
            if provider["name"] == "local":
                return {"content": "", "elapsed": 0.1, "tokens": 1}
            raise RuntimeError("groq HTTP 429 - rate limit")

        self._probe(call_one)
        self.assertGreater(bc.cooldown_remaining("groq", self.env), 0)

    def test_a_retired_model_self_heals_during_the_probe(self):
        retired = bc.PROVIDER_BY_NAME["groq"]["models"][0]
        replacement = bc.PROVIDER_BY_NAME["groq"]["models"][1]

        def call_one(provider):
            if provider["name"] == "local":
                return {"content": "", "elapsed": 0.1, "tokens": 1}
            if provider["model"] == retired:
                raise RuntimeError("groq HTTP 404 - model does not exist")
            return {"content": "", "elapsed": 0.2, "tokens": 1, "model": replacement}

        results = self._probe(call_one, discover=lambda p: ["whisper-large-v3", replacement])
        self.assertTrue(results[1]["ok"])
        self.assertEqual(results[1]["rediscovered"], replacement)
        self.assertEqual(results[1]["model"], replacement)
        self.assertEqual(results[1]["cooldown_s"], 0)  # a healed hop is not benched

    def test_the_report_names_the_wall_and_the_bench(self):
        def call_one(provider):
            if provider["name"] == "local":
                return {"content": "", "elapsed": 0.3, "tokens": 1}
            raise RuntimeError('groq HTTP 402 - {"code": "payment_required"}')

        out = bc.format_probe(self._probe(call_one))
        self.assertIn("1-token completion", out)
        self.assertIn("[1] local", out)
        self.assertIn("ok —", out)
        self.assertIn("PAID WALL", out)
        self.assertIn("benched 3600s", out)
        self.assertIn("benched until cooldown expires: groq", out)

    def test_the_report_is_empty_of_false_alarms_when_all_hops_answer(self):
        out = bc.format_probe(self._probe(
            lambda p: {"content": "", "elapsed": 0.2, "tokens": 1}))
        self.assertNotIn("FAIL", out)
        self.assertNotIn("bench:", out)


class HealthSnapshotTest(unittest.TestCase):
    """The ledger's `cascade` block: an outage has to be a visible line in the
    timeline, not something reconstructed afterwards from the latency column."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-snapshot-test-")
        self.env = _env(self.tmp, GROQ_API_KEY="gsk_secret", CEREBRAS_API_KEY="csk_secret")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_healthy_chain_snapshots_ok(self):
        snap = bc.health_snapshot(_config(), self.env)
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["chain"], ["local", "groq", "cerebras"])
        self.assertEqual(snap["cooling"], {})

    def test_a_benched_hop_is_visible_and_moved_to_the_back(self):
        bc.note_failure("groq", 'groq HTTP 402 - {"code": "payment_required"}', self.env)
        snap = bc.health_snapshot(_config(), self.env)
        self.assertEqual(snap["status"], "degraded")
        # the chain order IS the degradation: the benched hop lost its priority
        self.assertEqual(snap["chain"], ["local", "cerebras", "groq"])
        self.assertEqual(snap["cooling"]["groq"]["class"], "payment")
        self.assertEqual(snap["cooling"]["groq"]["cooldown_s"], bc.DEFAULT_PAYMENT_COOLDOWN_S)
        self.assertEqual(snap["cooling"]["groq"]["fails"], 1)
        self.assertNotIn("cerebras", snap["cooling"])

    def test_a_hop_with_an_expired_cooldown_is_healthy_again(self):
        bc.note_failure("groq", "429 rate limit", self.env, cooldown_s=0)
        snap = bc.health_snapshot(_config(), self.env)
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["chain"], ["local", "groq", "cerebras"])

    def test_every_hop_benched_is_exhausted(self):
        for name in ("local", "groq", "cerebras"):
            bc.note_failure(name, "429 rate limit", self.env)
        snap = bc.health_snapshot(_config(), self.env)
        self.assertEqual(snap["status"], "exhausted")
        self.assertEqual(sorted(snap["cooling"]), ["cerebras", "groq", "local"])

    def test_the_snapshot_carries_no_credential_material(self):
        bc.note_failure("groq", "429 rate limit", self.env)
        text = json.dumps(bc.health_snapshot(_config(), self.env))
        self.assertNotIn("gsk_secret", text)
        self.assertNotIn("csk_secret", text)

    def test_the_snapshot_is_json_safe_for_a_ledger_record(self):
        bc.note_failure("cerebras", "429 rate limit", self.env)
        snap = bc.health_snapshot(_config(), self.env)
        self.assertEqual(json.loads(json.dumps(snap)), snap)  # one JSONL line


class FormatChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cascade-format-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_listing_shows_local_first_and_missing_keys(self):
        out = bc.format_chain(_config(), _env(self.tmp, GROQ_API_KEY="a"))
        self.assertIn("cascade: enabled", out)
        self.assertIn("[1] local", out)
        self.assertIn("[2] groq", out)
        self.assertIn("no key set", out)

    def test_listing_reports_cooldown_state(self):
        env = _env(self.tmp, GROQ_API_KEY="a", BRAIN_RATE_LIMIT_COOLDOWN_S="300")
        bc.note_failure("groq", "429 rate limit", env)
        out = bc.format_chain(_config(), env)
        self.assertIn("cooldown", out)
        self.assertIn("rate-limit", out)

    def test_listing_reports_disabled_cascade(self):
        out = bc.format_chain(_config(), _env(self.tmp, BRAIN_CASCADE="0", GROQ_API_KEY="a"))
        self.assertIn("disabled", out)
        self.assertNotIn("[2]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
