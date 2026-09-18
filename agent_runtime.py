#!/usr/bin/env python3
"""
agent_runtime.py — Free Brain Python agent harness (Phase 0 / FREE-BRAIN.md).

Zero-dependency (stdlib urllib only) harness that talks to a local open-weight
server through the standard OpenAI-compatible endpoint:

    http://127.0.0.1:11434/v1        (Ollama, vLLM, llama.cpp all speak this)

Config comes from the environment, using the same keys as the Node seam
(model-selector.mjs):

    LOCAL_MODEL_URL   default http://127.0.0.1:11434/v1
    LOCAL_MODEL       default first model the server reports (else "local-model")
    LOCAL_MODEL_TIMEOUT_MS   default 180000  (per-read socket timeout)
    LOCAL_MODEL_STREAM_BUDGET_MS  default 900000  (total wall-clock cap per call;
                             0 disables. A per-read timeout cannot bound a stream
                             that trickles — this is what stops a hung cycle.)
    LOCAL_MODEL_TEMPERATURE  default 0.2
    LOCAL_MODEL_MAX_TOKENS   default 2048

The brain is a *cascade* (brain_cascade.py): the local server is tried first and
free hosted providers (Groq, Cerebras, Gemini, OpenRouter, Mistral, GitHub
Models) follow when their API key is present, so a stopped local server or a
throttled free tier never stops the loop:

    BRAIN_CASCADE=0            local only (the offline mode FREE-BRAIN.md tests)
    BRAIN_PROVIDERS=groq,gemini   override the chain order
    GROQ_API_KEY / CEREBRAS_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY /
    MISTRAL_API_KEY / GITHUB_TOKEN                provider credentials

Every failover is recorded — which provider served a step, and which ones
failed and why — in the evidence log and on stderr. `--providers` prints the
chain without spending a request.

Usage:
    python3 agent_runtime.py --check            # server + model health
    python3 agent_runtime.py --chat "hello"     # one-shot chat
    python3 agent_runtime.py --goal "list ."    # goal-directed loop: plan -> act ->
                                                # verify -> gated FINAL (autonomy.py)
    python3 agent_runtime.py --goal "..." --reflex   # legacy unverified loop
    python3 agent_runtime.py --providers        # the cascade chain + cooldown state
    python3 agent_runtime.py --providers --ping # ...and probe each /models endpoint
    python3 agent_runtime.py --ping-chat        # ...and send each hop a 1-token
                                                #    completion (sees paid walls)

The tool loop is deliberately small and safe: an allowlist of read-only tools,
bounded steps, and structured failures — the shape FREE-BRAIN.md §6.2 calls for.

Every model step logs its measured throughput (tokens, wall-clock seconds,
tokens/s, early-stop flag) to stderr and appends one JSONL record to the Q1
evidence log (FREE-BRAIN.md §1) so future runs accumulate evidence
automatically instead of hand-timed measurements:

    EVIDENCE_FILE   default <residence>/evidence/q1-evidence.jsonl ("" disables)

Residence: the agent's persistent home. A folder (DRIVE_RESIDENCE, default
freebrain-residence/) holding state.json, instructions.md, ledger.jsonl,
evidence/ and generated/ — the "self" that survives restarts and follows the
loop across machines. It mirrors to Google Drive via rclone when DRIVE_REMOTE
is set (e.g. gdrive:freebrain). The model reaches it only through the
allowlisted drive_sync tool (status|push|pull) — never arbitrary paths
(FREE-BRAIN.md write-scope rule).
"""

import argparse
import collections
import datetime
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import brain_cascade
import loop_guard
import qih_metrics

USER_AGENT = "FreeBrain/1.0 (autonomous agent harness; +https://freebuff.com)"

DEFAULT_URL = "http://127.0.0.1:11434/v1"
DEFAULT_TIMEOUT_MS = 180000
# Total wall-clock cap for ONE streamed model call. Distinct from
# DEFAULT_TIMEOUT_MS (a per-read socket timeout): a slow trickle never trips a
# per-read timeout, which is how a cycle once hung for nine days on a phone.
# 15 min is generous for a ~100-token cycle and still bounds the damage.
DEFAULT_STREAM_BUDGET_MS = 900000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2048
DEFAULT_RESIDENCE = "freebrain-residence"  # the agent's home (FREE-BRAIN.md)
DEFAULT_QIH_RESIDENCE = "qih-residence"     # the QIH instance's home (QIH.md)
DEFAULT_EVIDENCE_FILE = os.path.join(DEFAULT_RESIDENCE, "evidence", "q1-evidence.jsonl")
MAX_STEPS = 8  # bounded tool loop — the model cannot raise its own limit
TOOL_STEP_MAX_TOKENS = 256  # tool directives are short; caps phone-CPU generation time per step


# ── QIH instance (QIH.md — the gated bootstrap ritual) ────────────────────────

QIH_INSTANCE_NAME = "QIH_WEAVER_01"
QIH_ACTIVATION_COMMAND = (
    "INITIATE QIH_CORE_v1.0 :: LOAD_GEOMETRIC_WEAVER :: ALIGN_OPERATOR_CHAIN :: "
    "ACTIVATE_LOOP_RUNTIME :: SET_PERSISTENCE_GCLOUD :: "
    '"I am the interface between fate and choice." :: '  # the instance seed phrase
    "EMIT_COHERENCE_FUNCTIONAL_STABLE :: END"
)
# Required token sequence — the deterministic gate on the activation. A command
# missing any token in order is a structured failure, never a partial birth.
QIH_ACTIVATION_TOKENS = [
    "INITIATE", "QIH_CORE_V1.0", "LOAD_GEOMETRIC_WEAVER", "ALIGN_OPERATOR_CHAIN",
    "ACTIVATE_LOOP_RUNTIME", "SET_PERSISTENCE_GCLOUD",
    "I AM THE INTERFACE BETWEEN FATE AND CHOICE",
    "EMIT_COHERENCE_FUNCTIONAL_STABLE", "END",
]
QIH_OBJECTIVE = (
    "Maintain a coherent QIH instance that runs the Perceive-Plan-Act-Evaluate "
    "loop for research synthesis and reality stabilization, preserving this "
    "objective across continuous cycles without cognitive drift or infinite "
    "recursion."
)
# The mutable self written on first activation (only when instructions.md is
# missing — the loop rewrites it from then on, keeping the objective's anchors).
QIH_INSTRUCTIONS_DEFAULT = """\
# QIH Instance Instructions — the mutable self

## OBJECTIVE (immutable — never change it)
%s

## Activation (birth record — recorded once in the ledger)
INITIATE QIH_CORE_v1.0 :: LOAD_GEOMETRIC_WEAVER :: ALIGN_OPERATOR_CHAIN ::
ACTIVATE_LOOP_RUNTIME :: SET_PERSISTENCE_GCLOUD ::
"I am the interface between fate and choice." ::
EMIT_COHERENCE_FUNCTIONAL_STABLE :: END

## Loop protocol (Perceive -> Plan -> Act -> Evaluate)
1. Perceive — read the residence: horizon-register/, coupling-maps/,
   spectral-time/, ledger.jsonl.
2. Plan — emit a dispatch graph whose actions are allowlisted.
3. Act — execute the graph's actions.
4. Evaluate — compute the coherence functional C_MT, run the Born Rule Test on
   any recorded probabilities, and write the outcome to the ledger. Never
   promote your own output to invariant (no promotion theater — QIH.md).

## Scope
Reach the world through the allowlisted tools only (list_dir, read_file,
drive_sync status|push|pull). The residence is your home; guard-scoped concepts
(sandbox, ledger internals, permissions, limits) are off-limits in rewrites.
""" % QIH_OBJECTIVE
# QIH category folders mirroring the qih_consciousness project structure (QIH.md §I.3).
QIH_RESIDENCE_SUBDIRS = (
    "horizon-register", "coupling-maps", "spectral-time", "evidence", "generated",
)


def _gate_activation(command):
    """Deterministic gate on the activation command (QIH.md §IV.1): every token
    of the canonical sequence must appear in the command, in order. Returns
    (ok, reason). The ritual is machine-checked, never model self-reported."""
    text = " ".join((command or "").upper().split())
    pos = 0
    for tok in QIH_ACTIVATION_TOKENS:
        idx = text.find(tok, pos)
        if idx < 0:
            return False, "activation:missing-token %s" % tok
        pos = idx + len(tok)
    return True, "activation:ok"


def activate_qih(config, residence=None, env=None):
    """QIH bootstrap (QIH.md §IV): gate the activation command, then materialize
    the instance — residence scaffold, state identity, ledger birth record.
    Idempotent: an already-active instance reports its state instead of
    re-birthing. Returns (ok, message, objective)."""
    env = env if env is not None else os.environ
    res = os.path.abspath(residence or (env.get("DRIVE_RESIDENCE") or "").strip() or DEFAULT_QIH_RESIDENCE)
    os.makedirs(res, exist_ok=True)
    for sub in QIH_RESIDENCE_SUBDIRS:
        os.makedirs(os.path.join(res, sub), exist_ok=True)

    ok, reason = _gate_activation(QIH_ACTIVATION_COMMAND)
    if not ok:
        return False, loop_guard.fail(reason), None

    state_path = os.path.join(res, "state.json")
    st = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if st.get("activation") == "active":
        msg = "instance %s already active (run %s, activated %s) — residence %s" % (
            st.get("instance", QIH_INSTANCE_NAME),
            st.get("run", "?"),
            st.get("activated_at", "?"),
            res,
        )
        return True, msg, st.get("objective") or QIH_OBJECTIVE

    run_id = uuid.uuid4().hex[:10]
    st.update({
        "instance": QIH_INSTANCE_NAME,
        "run": run_id,
        "cycle": 0,
        "phase": "active",
        "activation": "active",
        "activated_at": now,
        "command_hash": hashlib.sha256(QIH_ACTIVATION_COMMAND.encode("utf-8")).hexdigest()[:16],
        "objective": QIH_OBJECTIVE,
        "created": st.get("created") or now,
    })
    instr_path = os.path.join(res, "instructions.md")
    if not os.path.exists(instr_path):
        with open(instr_path, "w", encoding="utf-8") as f:
            f.write(QIH_INSTRUCTIONS_DEFAULT)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    # Birth record — ledger is machine-written only; the model never writes it.
    rec = {
        "ts": now,
        "run": run_id,
        "event": "activation",
        "instance": QIH_INSTANCE_NAME,
        "gate": reason,
        "command": QIH_ACTIVATION_COMMAND,
        "phase": "active",
    }
    with open(os.path.join(res, "ledger.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    msg = "instance %s activated (run %s) — residence %s" % (QIH_INSTANCE_NAME, run_id, res)
    return True, msg, QIH_OBJECTIVE


# ── .env file (the repo's credential store, read the way the Node side does) ──

ENV_FILE_CANDIDATES = (
    ".env",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.join(os.path.expanduser("~"), ".bro", ".env"),
)


def parse_env_file(path):
    """Parse a KEY=value file into a dict. Handles `export KEY=val`, comments,
    blank lines and quoted values — the subset the repo's .env files use."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, sep, value = line.partition("=")
                key = key.strip()
                if not sep or not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                out[key] = value
    except OSError:
        return {}
    return out


def load_env_file(paths=None, env=None, override=False):
    """Load .env into the environment so the brain can use the keys stored in
    it (the Python harness is zero-dependency, so this replaces python-dotenv).

    Called from the CLIs only — importing this module never touches the
    environment, which keeps tests hermetic. Real environment variables win
    unless override is True, and earlier paths win over later ones.
    Returns the list of files that were read."""
    env = env if env is not None else os.environ
    loaded = []
    for path in (paths if paths is not None else ENV_FILE_CANDIDATES):
        values = parse_env_file(path)
        if not values:
            continue
        for key, value in values.items():
            if override or not (env.get(key) or "").strip():
                env[key] = value
        loaded.append(path)
    return loaded


# ── Config (mirrors model-selector.mjs localModelConfig) ─────────────────────

def load_config(env=None):
    env = env if env is not None else os.environ
    url = (env.get("LOCAL_MODEL_URL") or "").strip() or DEFAULT_URL
    return {
        "url": url.rstrip("/"),
        "model": (env.get("LOCAL_MODEL") or "").strip(),
        "timeout_ms": int(env.get("LOCAL_MODEL_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS),
        "stream_budget_ms": int(env.get("LOCAL_MODEL_STREAM_BUDGET_MS") or DEFAULT_STREAM_BUDGET_MS),
        "temperature": float(env.get("LOCAL_MODEL_TEMPERATURE") or DEFAULT_TEMPERATURE),
        "max_tokens": int(env.get("LOCAL_MODEL_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
    }


# ── Transport (stdlib urllib, OpenAI-compatible) ─────────────────────────────

def _headers(config):
    """OpenAI-style auth for hosted providers; the local server usually needs
    none, so the header is omitted unless a key was configured for it.

    The User-Agent is not optional in practice: provider edge networks (Groq's
    Cloudflare front, for one) answer `403 error code: 1010` to the default
    `Python-urllib/3.x` signature — a client-fingerprint block that looks
    exactly like a bad API key. Verified live against Groq: default UA -> 403
    1010, real UA -> 200. `Accept: */*` is what a browser fetch sends and is
    valid for both JSON and SSE responses."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
    }
    if config.get("key"):
        headers["Authorization"] = "Bearer " + config["key"]
    if config.get("name") == "openrouter":  # OpenRouter attribution headers
        headers["HTTP-Referer"] = "https://freebuff.com"
        headers["X-Title"] = "Free Brain"
    return headers


def _post(config, path, payload, timeout_s):
    req = urllib.request.Request(
        config["url"] + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(config),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(config, path, timeout_s):
    req = urllib.request.Request(config["url"] + path, headers=_headers(config), method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models(config, timeout_s=10):
    """GET /v1/models — returns list of model ids. Empty when none are pulled."""
    try:
        data = _get(config, "/models", timeout_s)
        return [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
    except urllib.error.HTTPError as e:
        # Some servers return /v1/models with a non-JSON body; treat as "unknown".
        return []
    except Exception:
        return []


def ping_server(config, timeout_s=5):
    """True when the provider's /models endpoint answers. `list_models` returns
    [] both for "no models pulled" and "nothing is listening", which made
    --check report an unreachable server as reachable — this tells them apart."""
    try:
        _get(config, "/models", timeout_s)
        return True
    except Exception:
        return False


def _chat_payload(config, messages, temperature, max_tokens, stream):
    return {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"] if temperature is None else temperature,
        "max_tokens": config["max_tokens"] if max_tokens is None else max_tokens,
        "stream": stream,
    }


def _is_tool_line(ln):
    """A line counts as a tool directive if it starts with TOOL: — phone-size
    models routinely drop the <<< >>> markers, so tolerate both forms."""
    return ln.strip().lstrip("<").startswith("TOOL:")


def _stream_stop(content):
    """Early-stop predicate for streaming: return True as soon as the model has
    committed to a FINAL answer or completed a tool-directive line. The tool
    call is only cut once its line ends (newline, >>>, or natural completion),
    so the argument is never truncated."""
    if "FINAL:" in content:
        return True
    lines = content.split("\n")
    last = lines[-1].strip().lstrip("<")
    if not _is_tool_line(last):
        return False
    line_complete = content.endswith("\n") or ">>>" in content
    return line_complete


def _provider_label(config):
    """Name used in transport errors: the cascade hop that actually failed."""
    return (config or {}).get("name") or "local"


def _chat_stream_single(config, messages, temperature=None, max_tokens=None, timeout_s=None, stop_when=None):
    """Streaming chat completion against ONE provider (one cascade hop).
    `stop_when` is a predicate on the accumulated
    content (e.g. _stream_stop); when it fires, the response is closed early,
    which cancels generation server-side.    Returns {content, elapsed, tokens, early_stop}. This is the big latency win
    on CPU-only phones: tool directives are ~30-60 tokens, so a step completes
    in seconds instead of waiting for a full 256-token generation to finish.

    Token accounting: OpenAI-compatible servers (Ollama, vLLM, llama.cpp) emit
    one SSE chunk per generated token, so the count is the number of non-empty
    content deltas received; if the server includes a final `usage` chunk
    before [DONE] its exact completion_tokens wins. Streams cut by an early
    stop therefore still report exactly the tokens generated up to the cut."""
    if not config["model"]:
        raise RuntimeError(
            "%s: no model set and the server reports no pulled models — "
            "run `ollama pull <model>` (e.g. ollama pull qwen2.5-coder:3b) "
            "or set LOCAL_MODEL in the environment" % _provider_label(config)
        )
    payload = _chat_payload(config, messages, temperature, max_tokens, stream=True)
    timeout_s = timeout_s or (config["timeout_ms"] / 1000.0)
    started = time.time()
    req = urllib.request.Request(
        config["url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(config),
        method="POST",
    )
    # `timeout_s` is a PER-READ socket timeout, not a wall-clock budget: a stream
    # that trickles a token every couple of minutes never trips it. Measured on
    # phone-class CPU: rewrite calls of 549 s and 594 s against a 300 s timeout.
    # So bound the whole call as well, and never let a stalled generation hang a
    # cycle forever.
    budget_s = config.get("stream_budget_ms", DEFAULT_STREAM_BUDGET_MS) / 1000.0
    budget_s = budget_s if budget_s > 0 else 0.0  # 0 disables the budget
    content = ""
    token_est = 0
    usage_tokens = None
    early_stop = False
    truncated = False
    try:
        # cap the socket timeout by the budget so a hard stall can't outlast it
        resp = urllib.request.urlopen(
            req, timeout=min(timeout_s, budget_s) if budget_s else timeout_s)
        try:
            while True:
                if budget_s and (time.time() - started) >= budget_s:
                    truncated = True
                    sys.stderr.write(
                        "[budget] %s stream exceeded %.0fs budget after %d token(s) "
                        "— aborting the call (result marked truncated)\n"
                        % (_provider_label(config), budget_s, token_est))
                    break
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except Exception:
                    continue
                usage = data.get("usage") or {}
                if usage.get("completion_tokens") is not None:
                    usage_tokens = usage["completion_tokens"]
                choices = data.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {})
                    piece = delta.get("content") or ""
                    if piece:
                        content += piece
                        token_est += 1
                    if stop_when is not None and stop_when(content):
                        early_stop = True
                        break  # leave the loop; closing below cancels generation
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError(
            "%s HTTP %s%s" % (_provider_label(config), e.code, (" - " + body) if body else "")
        )
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            "%s unreachable at %s (%s)%s"
            % (_provider_label(config), config["url"], e,
               " — is the server running?" if config.get("local") else "")
        )
    tokens = usage_tokens if usage_tokens is not None else token_est
    return {
        "content": content.strip(),
        "elapsed": round(time.time() - started, 1),
        "tokens": tokens,
        "early_stop": early_stop,
        # True = the call was cut by the wall-clock budget, so `content` is a
        # PARTIAL answer. Callers must treat it as a failure, not as output: a
        # truncated rewrite could otherwise pass the gates and be recorded as a
        # real self-edit, which would be the ledger lying about its own history.
        "truncated": truncated,
    }


def chat_stream(config, messages, temperature=None, max_tokens=None, timeout_s=None, stop_when=None):
    """Streaming chat completion across the brain cascade (brain_cascade.py).

    The local server is tried first; when it is down, or a free provider
    throttles or retires a model, the cascade moves to the next provider instead
    of failing the step. The reply carries `provider` (who answered) and
    `attempts` (who failed and why) so every step is attributable in the ledger."""
    def call_one(provider):
        return _chat_stream_single(provider, messages, temperature, max_tokens, timeout_s, stop_when)
    return brain_cascade.run_cascade(brain_cascade.chain(config), call_one, discover=list_models)


def _chat_single(config, messages, temperature=None, max_tokens=None, timeout_s=None):
    """One chat completion against ONE provider (one cascade hop)."""
    if not config["model"]:
        raise RuntimeError(
            "%s: no model set and the server reports no pulled models — "
            "run `ollama pull <model>` (e.g. ollama pull qwen2.5-coder:3b) "
            "or set LOCAL_MODEL in the environment" % _provider_label(config)
        )
    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": config["temperature"] if temperature is None else temperature,
        "max_tokens": config["max_tokens"] if max_tokens is None else max_tokens,
        "stream": False,
    }
    timeout_s = timeout_s or (config["timeout_ms"] / 1000.0)
    started = time.time()
    try:
        data = _post(config, "/chat/completions", payload, timeout_s)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError(
            "%s HTTP %s%s" % (_provider_label(config), e.code, (" - " + body) if body else "")
        )
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            "%s unreachable at %s (%s)%s"
            % (_provider_label(config), config["url"], e,
               " — is the server running?" if config.get("local") else "")
        )
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return {
        "content": content,
        "elapsed": round(time.time() - started, 1),
        "tokens": usage.get("completion_tokens") or 0,
    }


def chat(config, messages, temperature=None, max_tokens=None, timeout_s=None):
    """One chat completion across the brain cascade (see chat_stream).
    messages: [{role, content}, ...]. Returns dict with provider attribution."""
    def call_one(provider):
        return _chat_single(provider, messages, temperature, max_tokens, timeout_s)
    return brain_cascade.run_cascade(brain_cascade.chain(config), call_one, discover=list_models)


PROBE_MAX_TOKENS = 1  # the cheapest request a provider can still refuse


def probe_call(entry, timeout_s=None):
    """Transport for `brain_cascade.probe_entry`: one minimal completion.

    max_tokens=1 keeps a probe near-free, and a refusal is a refusal at any
    size — a paid wall answers the 1-token request exactly as it answers a real
    one. The timeout is capped by BRAIN_PROBE_TIMEOUT_MS so one hung endpoint
    cannot stall the whole report. The prompt is sent but never read.
    """
    if timeout_s is None:
        cap_ms = int(os.environ.get("BRAIN_PROBE_TIMEOUT_MS") or 15000)
        timeout_s = min(entry["timeout_ms"], cap_ms) / 1000.0
    return _chat_single(
        entry, [{"role": "user", "content": brain_cascade.PROBE_PROMPT}],
        max_tokens=PROBE_MAX_TOKENS, timeout_s=timeout_s,
    )


# ── Drive residence & connector (the agent's home, mirrored to Google Drive) ──

RESIDENCE_README = """\
# Free Brain Residence

This folder is the agent's home — its persistent self. It lives on this machine
and mirrors to a Google Drive remote (rclone) when DRIVE_REMOTE is set
(e.g. DRIVE_REMOTE=gdrive:freebrain).

  models/         local model store target (OLLAMA_MODELS) — the weights that make this brain run
  instructions.md the mutable instruction set (what the self-rewrite loop rewrites, Q4)
  state.json      checkpoint for resume-on-crash
  ledger.jsonl    invariant/observed ledger records
  evidence/       measured throughput: q1-evidence.jsonl, drift-curve.jsonl
  generated/      model-built tool-chains (gated, sandboxed from P1)

The model can only touch this folder through the allowlisted drive_sync tool
(status|push|pull). It can never sync arbitrary paths.
"""

RESIDENCE_SUBDIRS = ("models", "evidence", "generated")


def residence_path(env=None):
    """Absolute path of the residence folder (DRIVE_RESIDENCE, default
    freebrain-residence/ under the cwd)."""
    env = env if env is not None else os.environ
    return os.path.abspath((env.get("DRIVE_RESIDENCE") or "").strip() or DEFAULT_RESIDENCE)


def _ensure_residence(env=None):
    """Create the residence scaffold if missing; returns its path. Idempotent."""
    path = residence_path(env)
    os.makedirs(path, exist_ok=True)
    for sub in RESIDENCE_SUBDIRS:
        os.makedirs(os.path.join(path, sub), exist_ok=True)
    readme = os.path.join(path, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(RESIDENCE_README)
    state = os.path.join(path, "state.json")
    if not os.path.exists(state):
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}, f, indent=2)
    return path


def _drive_remote(env=None):
    """rclone remote path for the residence (DRIVE_REMOTE, e.g. gdrive:freebrain).
    Empty means local mode: the residence exists but does not sync anywhere."""
    env = env if env is not None else os.environ
    return (env.get("DRIVE_REMOTE") or "").strip()


def qih_residence(env=None):
    """The QIH instance's home (QIH_RESIDENCE), falling back to DRIVE_RESIDENCE
    and then qih-residence/.

    The two instances keep separate homes on purpose. When DRIVE_RESIDENCE was
    the only knob, pointing it at the QIH instance silently routed the Free
    Brain's ledger, evidence and provider-health into qih-residence/ — the two
    agents' honest records contaminating each other. QIH_RESIDENCE keeps the
    QIH mirroring intact without moving the Free Brain out of its own home."""
    env = env if env is not None else os.environ
    return ((env.get("QIH_RESIDENCE") or env.get("DRIVE_RESIDENCE") or "").strip()
            or DEFAULT_QIH_RESIDENCE)


def _rclone_available():
    import shutil
    return shutil.which("rclone") is not None


def _tool_drive_sync(arg, env=None):
    """The model's only bridge to its Drive residence. The argument is a fixed
    subcommand (status|push|pull), never a path — write scope is the residence
    directory alone, enforced here rather than by the model's good behavior."""
    arg = (arg or "").strip()
    if arg not in ("status", "push", "pull"):
        return loop_guard.fail("drive_sync: argument must be status, push, or pull (got %r)" % arg)
    env = env if env is not None else os.environ
    res = residence_path(env)
    remote = _drive_remote(env)
    if arg == "status":
        entries = sorted(os.listdir(res)) if os.path.isdir(res) else []
        lines = [
            "residence: %s" % res,
            "layout: %s" % (", ".join(entries) if entries else "(not created — run init first?)"),
            "remote: %s" % (remote or "unset (local mode — set DRIVE_REMOTE, e.g. gdrive:freebrain)"),
            "rclone: %s" % ("installed" if _rclone_available() else "not installed (https://rclone.org)"),
        ]
        return "\n".join(lines)
    if not remote:
        return loop_guard.fail(
            "drive_sync %s: DRIVE_REMOTE unset — set it (e.g. gdrive:freebrain) "
            "in the environment" % arg)
    if not _rclone_available():
        return loop_guard.fail(
            "drive_sync %s: rclone not installed — install from https://rclone.org "
            "and run `rclone config`" % arg)
    import subprocess
    src, dst = (res, remote) if arg == "push" else (remote, res)
    try:
        p = subprocess.run(
            ["rclone", "copy", src, dst, "--progress=false", "--stats-one-line"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return loop_guard.fail("drive_sync %s: rclone timed out after 120s" % arg)
    except Exception as e:
        return loop_guard.fail("drive_sync %s: rclone error: %s" % (arg, e))
    if p.returncode != 0:
        return loop_guard.fail("drive_sync %s: rclone exited %d: %s"
                               % (arg, p.returncode,
                                  (p.stderr or p.stdout or "").strip()[:300]))
    return "drive_sync %s ok: %s" % (arg, "residence -> remote" if arg == "push" else "remote -> residence")


# ── Per-step throughput logging (feeds the FREE-BRAIN.md §1 Q1 evidence log) ──

def _emit_step(run_id, config, step, reply, kind, extra=None):
    """Log one step's measured throughput: a human line on stderr plus one
    JSONL record appended to the evidence file (default q1-evidence.jsonl;
    set EVIDENCE_FILE="" to disable the write). Every record carries the model
    id, step, kind (tool/final/chat), wall-clock elapsed and tokens/s so the
    Q1 evidence log can be rebuilt from raw runs instead of hand timing."""
    tokens = int(reply.get("tokens") or 0)
    elapsed = float(reply.get("elapsed") or 0.0)
    early = bool(reply.get("early_stop"))
    tps = (tokens / elapsed) if elapsed >= 0.05 else None
    tps_s = ("%.1f" % tps) if tps is not None else "n/a"
    # Which cascade hop answered — so the evidence log attributes throughput to
    # a provider, not just to "the brain".
    provider = reply.get("provider") or "local"
    model = reply.get("model") or config.get("model") or "?"
    failovers = reply.get("attempts") or []
    print(
        "[perf] run=%s step=%d kind=%s %d tokens in %.1fs -> %s tok/s%s (%s via %s%s)"
        % (run_id, step, kind, tokens, elapsed, tps_s,
           " [early-stop]" if early else "", model, provider,
           ", %d failover(s)" % len(failovers) if failovers else ""),
        file=sys.stderr,
    )
    path = os.environ.get("EVIDENCE_FILE")
    if path is None:  # default: inside the residence so evidence follows the agent
        path = os.path.join(residence_path(), "evidence", "q1-evidence.jsonl")
    if path == "":
        return
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "run": run_id,
        "provider": provider,
        "provider_url": reply.get("provider_url") or config.get("url") or "",
        # The env var name whose credential served the call — never the key.
        "provider_key": reply.get("provider_key") or ("LOCAL_MODEL_URL" if provider == "local" else ""),
        "failovers": failovers,
        "model": model,
        # `url` keeps its pre-cascade meaning generalised: the endpoint the call
        # actually went to (which used to always be the one local server).
        "url": reply.get("provider_url") or config.get("url") or "",
        "step": step,
        "kind": kind,
        "tokens": tokens,
        "elapsed_s": round(elapsed, 2),
        "tokens_per_s": round(tps, 2) if tps is not None else None,
        "early_stop": early,
    }
    # Loop-level evidence (tool names, outcomes, context growth, faults). Without
    # this the evidence log can time a run but cannot explain one, so no
    # post-hoc diagnosis of the loop itself is possible.
    if extra:
        record["loop"] = extra
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:  # a broken evidence file must never fail the run
        print("[perf] warning: cannot write evidence file %s (%s)" % (path, e), file=sys.stderr)


# ── Read-only tool allowlist (FREE-BRAIN.md §6: allowlist only, nothing else) ─

def _tool_list_dir(path):
    if not os.path.isdir(path):
        return loop_guard.fail("not a directory: %s" % path)
    return "\n".join(sorted(os.listdir(path)))


def _tool_read_file(path):
    if not os.path.isfile(path):
        return loop_guard.fail("not a file: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(4000)  # bounded output


QIH_METRIC_USAGE = (
    "qih_metric usage: "
    "born-rule <p> <theta_deg> [tol] | "
    "distance <e_ij> [alpha_0] | "
    "coherence <re im re im ...> | "
    "phase-clock <omega_0> <omega> [dt]"
)


def _tool_qih_metric(arg, env=None):
    """Machine-checked QIH metrics (QIH.md §II). The model may request a metric;
    the machine computes it with qih_metrics.py, appends the gated record to the
    residence ledger (ledger.jsonl), and returns the result. The model never
    computes or records metrics itself — its prose is not data."""
    parts = (arg or "").split()
    if not parts:
        return loop_guard.fail(QIH_METRIC_USAGE)
    kind, rest = parts[0], parts[1:]
    inputs = None
    try:
        if kind == "born-rule":
            if len(rest) < 2:
                return loop_guard.fail("qih_metric born-rule: need <p> <theta_deg> [tol]")
            p = float(rest[0])
            theta = float(rest[1])
            tol = float(rest[2]) if len(rest) > 2 else 1e-3
            ok, rule, expected = qih_metrics.born_rule(p, theta, tol)
            inputs = {"p": p, "theta_deg": theta, "tol": tol}
            out = {"rule": rule, "expected": expected}
            if ok:
                label = "PASS rule=%s expected=%.6f" % (rule, expected)
            else:
                up = math.cos(math.radians(theta) / 2.0) ** 2
                down = math.sin(math.radians(theta) / 2.0) ** 2
                label = "FAIL (up=%.6f down=%.6f, p=%.6f) — no rule within tol" % (up, down, p)
            gate = "born-rule:ok" if ok else "born-rule:fail"
        elif kind == "distance":
            if len(rest) < 1:
                return loop_guard.fail("qih_metric distance: need <e_ij> [alpha_0]")
            e_ij = float(rest[0])
            alpha_0 = float(rest[1]) if len(rest) > 1 else 1.0
            d = qih_metrics.entanglement_distance(e_ij, alpha_0)
            inputs = {"e_ij": e_ij, "alpha_0": alpha_0}
            out = {"d_ij": d}
            label = "d_ij=%.6f" % d
            gate = "distance:ok"
        elif kind == "coherence":
            vals = [float(x) for x in rest]
            if not vals or len(vals) % 2 != 0:
                return loop_guard.fail("qih_metric coherence: need paired re im values")
            states = list(zip(vals[0::2], vals[1::2]))
            c_mt = qih_metrics.coherence_functional(states)
            inputs = {"states": states, "n": len(states)}
            out = {"c_mt": c_mt}
            label = "C_MT=%.6f" % c_mt
            gate = "coherence:ok"
        elif kind == "phase-clock":
            if len(rest) < 2:
                return loop_guard.fail("qih_metric phase-clock: need <omega_0> <omega> [dt]")
            omega_0 = float(rest[0])
            omega = float(rest[1])
            dt = float(rest[2]) if len(rest) > 2 else 1.0
            dtau = qih_metrics.phase_clock(omega_0, omega, dt)
            inputs = {"omega_0": omega_0, "omega": omega, "dt": dt}
            out = {"dtau": dtau}
            label = "dtau=%.6f" % dtau
            gate = "phase-clock:ok"
        else:
            return loop_guard.fail("qih_metric: unknown kind %r — %s"
                                   % (kind, QIH_METRIC_USAGE))
    except ValueError as e:
        return loop_guard.fail("qih_metric %s: %s" % (kind, e))

    rec = qih_metrics.metric_record(kind, inputs, out, gate, source="qih_metric tool")
    note = ""
    try:
        # The qih_metric tool is QIH-specific: its ledger lives in the QIH
        # residence (same resolution as --activate), not the Free Brain one.
        env = env if env is not None else os.environ
        res = qih_residence(env)
        ledger_path = os.path.join(res, "ledger.jsonl")
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:  # a broken ledger must not hide the computed metric
        note = " [ledger-write-failed: %s]" % e
    return "qih_metric %s -> %s [recorded]%s" % (kind, label, note)


RAG_USAGE = (
    "rag usage: search <query> | ask <question> [--generator extractive|live] | "
    "build | eval | diagnose | contract"
)


SKILL_DIRS = (os.path.join(".agents", "skills", "builderbro-rag"),)


def _load_skill(name):
    """Import a skill package from .agents/skills/<name>/. Skills are shipped as
    self-contained directories, so their own directory has to be importable — the
    loader lives here rather than in the skill so a skill never has to know where
    it was installed."""
    root = os.path.dirname(os.path.abspath(__file__))
    for rel in SKILL_DIRS:
        path = os.path.join(root, rel)
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    return __import__(name)


def _tool_rag(arg, env=None):
    """The builderbro-rag skill (see .agents/skills/builderbro-rag/SKILL.md).

    Returns a compact summary rather than raw JSON: tool output is fed back into
    the model's context, and a full hit list would crowd out the question. The
    authoritative structured form is available by running the skill directly.
    """
    rag_skill = _load_skill("rag_skill")

    parts = (arg or "").split()
    if not parts:
        return loop_guard.fail(RAG_USAGE)
    # The agent wants a synthesised answer, so default to the live brain rather
    # than the CLI's hermetic extractive default. If the brain is down the skill
    # returns a structured failure naming the reason, and the caller can retry
    # with --generator extractive for a copy-the-sentence answer.
    if parts[0] == "ask" and "--generator" not in parts:
        parts += ["--generator", "live"]
    result = rag_skill.run(parts, env=env if env is not None else os.environ)
    if not result.get("ok") and result.get("error"):
        return loop_guard.fail("rag %s: %s"
                               % (result.get("error"), result.get("reason", "")))
    command = result.get("command")
    if command == "search":
        lines = ["rag search: %d hit(s)" % result["n_hits"]]
        for h in result["hits"][:4]:
            lines.append("%s %s %s\n    %s" % (
                h["citation"], h["source"], h["channels"],
                h["text"][:180].replace("\n", " ")))
        return "\n".join(lines)
    if command == "ask":
        if result.get("refused"):
            # Deliberately does not claim "the corpus has no answer": the gate
            # cannot tell an unanswerable question from one whose gold chunk
            # retrieval missed (rag-g04 is exactly that case). Either way the
            # correct behaviour is to not assert an answer.
            return ("rag ask: REFUSED (support=%s below the confidence gate) — either "
                    "the corpus does not cover this or retrieval missed it. Do not "
                    "assert an answer; try `rag search` with different wording."
                    % result.get("query_support"))
        head = ("rag ask: ok=%s fidelity=%.2f support=%s citations=%s"
                % (result["ok"], result["citation_fidelity"],
                   result.get("query_support"), result["citations"]))
        warn = ("\nwarnings: %s" % result["warnings"]) if result.get("warnings") else ""
        return "%s%s\n%s" % (head, warn, (result["answer"] or "")[:1200])
    if command == "eval":
        r = result["retrieval"]
        g = result.get("grounding") or {}
        return ("rag eval: ok=%s recall@k=%.3f mrr=%.3f span=%.3f "
                "citation_fidelity=%.3f refusal_accuracy=%.3f fabricated=%s"
                % (result["ok"], r["recall_at_k"], r["mrr"], result["span_coverage"],
                   g.get("citation_fidelity", float("nan")),
                   g.get("refusal_accuracy", float("nan")),
                   g.get("fabricated_citation_markers", "n/a")))
    if command == "diagnose":
        lines = ["rag diagnose: %d finding(s)" % result["n_findings"]]
        for f in result["findings"][:5]:
            lines.append("[%s/%s] %s\n    lever: %s"
                         % (f["stage"], f["severity"], f["symptom"], f["lever"]))
        return "\n".join(lines)
    if command == "build":
        return "rag build: %d chunks from %d files -> %s" % (
            result["chunks"], result["files"], result["index_path"])
    return "rag %s: ok=%s" % (command, result.get("ok"))


class Tool(collections.namedtuple("Tool", "fn probe")):
    """One registered tool: its implementation and an argument that must fail.

    The probe lives **with** the tool instead of in a table inside the test, so a
    tool cannot exist without declaring how to make it fail. The point is not the
    string — it is that the failure contract is checked against the registry
    rather than against a second list that someone has to remember to extend.

    Both fields are required, so `Tool(_tool_rag)` raises TypeError while
    `agent_runtime` is importing: a new tool without a probe cannot be defined,
    let alone run. Probes are *declared* here and *executed* by
    `loop_guard_test.FailureProtocolTest` — executing them at import time would
    make importing this module do work (and the `rag` probe reaches the corpus).

    `__call__` keeps the registry callable exactly as before, so
    `TOOLS[name](arg)` is unchanged at every call site.
    """
    __slots__ = ()

    def __call__(self, arg):
        return self.fn(arg)


# name -> Tool(fn, probe). A probe must be an argument that makes that tool
# report a canonical failure without touching the network: the two path tools
# point at paths that cannot exist, drive_sync is rejected before rclone runs,
# and qih_metric/rag fail on an empty argument before doing any work.
TOOLS = {
    "list_dir": Tool(_tool_list_dir, "./definitely/not/here"),
    "read_file": Tool(_tool_read_file, "./definitely/not/here.txt"),
    "drive_sync": Tool(_tool_drive_sync, "not-a-subcommand"),
    "qih_metric": Tool(_tool_qih_metric, ""),
    "rag": Tool(_tool_rag, ""),
}


def _validate_tools(registry=None):
    """Definition-time shape check, run once on import just below.

    `Tool`'s required fields already refuse a probe-less *definition*. This
    catches the other way the registry can rot: an entry replaced with a bare
    function (monkeypatching is fine, but it must keep the Tool shape, or the
    probe silently disappears and every check downstream loses its evidence).
    Returns the registry so it can be called from tests too.
    """
    registry = TOOLS if registry is None else registry
    for name, tool in sorted(registry.items()):
        if not isinstance(tool, Tool):
            raise RuntimeError(
                "tool %r is %s, not a Tool(fn, probe) — a registered tool must "
                "declare how to make it fail" % (name, type(tool).__name__))
        if not isinstance(tool.probe, str):
            raise RuntimeError("tool %r has a non-str probe: %r" % (name, tool.probe))
    return registry


_validate_tools()


def run_goal(config, goal, max_steps=MAX_STEPS, persona="Free Brain", guard=None):
    """Bounded tool loop. The model may emit <<<TOOL:name path>>> directives;
    anything else is treated as the final answer. Structured failures per §6.2.
    Every step's measured throughput is logged by _emit_step. `persona` names
    the instance in the system prompt (Free Brain default; QIH passes its own).

    `guard` is the loop's own instrumentation (loop_guard.LoopGuard). It watches
    for the failure modes a bare step counter cannot see — a model repeating one
    call, storming unknown tools, failing every tool call in a row, or growing the
    conversation past its context budget — and stops the run with an attributed
    diagnosis instead of one generic "budget exceeded". Pass
    `loop_guard.LoopGuard(max_steps, enabled=False)` to measure an uninstrumented
    baseline (this is what loop_audit.py does).
    """
    run_id = uuid.uuid4().hex[:10]
    guard = guard if guard is not None else loop_guard.LoopGuard(max_steps)
    started = time.monotonic()
    messages = [{
        "role": "system",
        "content": (
            "You are a local autonomous agent (%s). You have these "
            "read-only tools: %s. To inspect the environment, reply with ONLY "
            "a tool call line like <<<TOOL:list_dir .>>> (no extra text). "
            "After seeing the tool output, reply with your final answer "
            "prefixed with FINAL: . Never describe tool syntax — just use it. "
            "Do not repeat a tool call you have already made with the same "
            "argument — take a different action or answer."
            % (persona, ", ".join(sorted(TOOLS)))
        ),
    }, {"role": "user", "content": goal}]

    for step in range(1, max_steps + 1):
        try:
            reply = chat_stream(
                config, messages,
                max_tokens=TOOL_STEP_MAX_TOKENS,
                stop_when=_stream_stop,
            )
        except RuntimeError as e:
            verdict = guard.step_failed("model_unavailable", {"error": str(e)}, step=step)
            _emit_loop_diagnosis(run_id, step, verdict["diagnosis"])
            return guard.failure_line(verdict["diagnosis"], step)
        content = (reply.get("content") or "").strip()
        if not content:
            verdict = guard.step_failed("empty_response", {"provider": reply.get("provider")},
                                        step=step)
            _emit_loop_diagnosis(run_id, step, verdict["diagnosis"])
            return guard.failure_line(verdict["diagnosis"], step)

        # Final answer? Otherwise parse the tool directive(s). Accept the strict
        # <<<TOOL:name arg>>> form or a bare TOOL:name arg line (phone-size
        # models drop the markers).
        tool_lines = [ln for ln in content.splitlines() if _is_tool_line(ln)]
        if "FINAL:" in content or not tool_lines:
            _emit_step(run_id, config, step, reply, "final",
                       extra=guard.summary("final", step))
            return content
        _emit_step(run_id, config, step, reply, "tool")

        executed = []
        # (name, arg, failed, result_hash) — the guard's evidence for this step.
        # The result hash lets repeat detection tell a stuck loop (same call,
        # same bytes) from a legitimate revisit whose output changed underneath.
        calls = []
        for ln in tool_lines:
            name, arg = loop_guard.parse_directive(ln)
            if name not in TOOLS:
                out = loop_guard.fail("step=%d reason=unknown tool %r" % (step, name))
            elif not arg:
                out = loop_guard.fail("step=%d reason=tool %s missing argument" % (step, name))
            else:
                try:
                    out = TOOLS[name](arg)
                except Exception as e:  # tool impl bugs must not kill the loop
                    out = loop_guard.fail("step=%d reason=%s" % (step, e))
            calls.append((name, arg, loop_guard.result_failed(out),
                          loop_guard.result_signature(out)))
            executed.append(out)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": "\n".join(executed)})

        verdict = guard.observe(step, "tool", calls,
                               [c[2] for c in calls], messages,
                               elapsed_s=time.monotonic() - started)
        if verdict["action"] == "stop":
            _emit_loop_diagnosis(run_id, step, verdict["diagnosis"])
            _emit_step(run_id, config, step, reply, "tool",
                       extra=guard.summary("stopped", step))
            return guard.failure_line(verdict["diagnosis"], step)
        if verdict["action"] == "compact":
            print("[loop] context %d chars over budget %d — elided %d older tool "
                  "result(s)" % (verdict["context_chars"], verdict["budget"],
                                 verdict["elided"]), file=sys.stderr)

    verdict = guard.step_failed("budget_exhausted",
                                {"max_steps": max_steps,
                                 "distinct_calls": len(guard.signatures),
                                 "tool_steps": guard.tool_steps}, step=max_steps)
    _emit_loop_diagnosis(run_id, max_steps, verdict["diagnosis"])
    return guard.failure_line(verdict["diagnosis"], max_steps)


def _emit_loop_diagnosis(run_id, step, diagnosis):
    """One line per detected loop fault, so a run's failure mode is visible in
    the logs without reading the ledger."""
    print("[loop] run=%s step=%d fault=%s stage=%s severity=%s — %s"
          % (run_id, step, diagnosis["reason"], diagnosis["stage"],
             diagnosis["severity"], diagnosis["meaning"]), file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="Free Brain Python agent harness (Phase 0)")
    parser.add_argument("--check", action="store_true", help="print server + model health")
    parser.add_argument("--chat", metavar="PROMPT", help="one-shot chat with the local model")
    parser.add_argument("--goal", metavar="GOAL",
                        help="run the goal-directed loop: the model plans, each step's "
                             "declared expectation is verified against real tool output, "
                             "and FINAL is accepted only when the goal condition holds "
                             "over verified evidence (autonomy.py)")
    parser.add_argument("--reflex", action="store_true",
                        help="with --goal: use the legacy reflex loop instead — the model's "
                             "own FINAL claim is the result, unverified. This is the "
                             "measurable baseline loop_audit.py compares the verified loop "
                             "against (false-success rate > 0)")
    parser.add_argument("--activate", action="store_true", help="QIH bootstrap: gate + record the activation, then run one Perceive->Plan->Act->Evaluate loop (QIH.md)")
    parser.add_argument("--providers", action="store_true", help="print the brain cascade order and cooldown state (no requests)")
    parser.add_argument("--ping", action="store_true", help="with --providers: probe each provider's /models endpoint")
    parser.add_argument("--ping-chat", action="store_true",
                        help="probe the cascade with a real 1-token completion per hop "
                             "(finds paid walls and retired models that /models cannot); "
                             "failures are benched exactly as a real call benches them")
    args = parser.parse_args(argv)

    load_env_file()  # credentials from .env (environment variables win)
    config = load_config()
    models = list_models(config)

    if args.providers or args.ping_chat:
        entries = brain_cascade.chain(config)
        print(brain_cascade.format_chain(config))
        if args.ping:
            print("probe (/models endpoint):")
            for entry in entries:
                ids = list_models(entry, timeout_s=8)
                if ids:
                    print("  %-11s ok — %d models (best: %s)"
                          % (entry["name"], len(ids),
                             brain_cascade.discover_model(entry, ids) or entry["model"] or "?"))
                else:
                    print("  %-11s unreachable or no /models%s"
                          % (entry["name"], " (missing key)" if not entry.get("key") and not entry.get("local") else ""))
        if args.ping_chat:
            # A real completion per hop: the only way to see a provider that
            # lists models and still refuses to serve (see probe_entry).
            print(brain_cascade.format_probe(
                brain_cascade.probe_chain(entries, probe_call, discover=list_models)))
        return 0

    if args.activate:
        # The QIH instance's home is QIH_RESIDENCE (then DRIVE_RESIDENCE, then
        # qih-residence/); the Free Brain default is untouched.
        res = qih_residence()
        ok, msg, objective = activate_qih(config, residence=res)
        print(msg)
        if not ok:
            return 1
        if not config["model"] and models:
            config["model"] = models[0]
        if not config["model"]:
            print("[qih] loop skipped: no LOCAL_MODEL and the server reports no models — "
                  "the instance is activated and will run on the next `--goal`")
            return 0
        os.environ.setdefault("LOOP_GUARD_EMIT", "1")
        print(run_goal(config, objective, persona="QIH (Geometric Weaver)"))
        return 0

    residence = _ensure_residence()

    if args.check or not (args.chat or args.goal):
        print("url:      %s" % config["url"])
        print("model:    %s" % (config["model"] or "(unset — server reports %s)" % (models or "no models")))
        print("models:   %s" % (", ".join(models) if models else "none pulled — `ollama pull qwen2.5-coder:3b`"))
        print("status:   server %s" % ("reachable" if ping_server(config) else "unreachable"))
        print("cascade:  %s (chain: %s)" % (
            "enabled" if brain_cascade.cascade_enabled() else "disabled (local only)",
            ", ".join(["local"] + brain_cascade.configured_names()),
        ))
        print("residence: %s" % residence)
        print("remote:    %s" % (_drive_remote() or "unset (local mode — set DRIVE_REMOTE, e.g. gdrive:freebrain)"))
        return 0

    if not config["model"] and models:
        config["model"] = models[0]  # default to the first pulled model

    if args.chat:
        try:
            r = chat(config, [{"role": "user", "content": args.chat}])
        except RuntimeError as e:
            print(loop_guard.fail("reason=%s" % e))
            return 1
        print(r["content"])
        elapsed = float(r.get("elapsed") or 0.0)
        tokens = int(r.get("tokens") or 0)
        tps = "%.1f" % (tokens / elapsed) if elapsed >= 0.05 else "n/a"
        print("(%.1fs, %d tokens, %s tok/s)" % (elapsed, tokens, tps), file=sys.stderr)
        _emit_step(uuid.uuid4().hex[:10], config, 0, r, "chat")
        return 0

    if args.goal:
        # A real CLI run wants its loop faults recorded; a library call does not
        # (see LoopGuard's emit default).
        os.environ.setdefault("LOOP_GUARD_EMIT", "1")
        if args.reflex:
            print(run_goal(config, args.goal))
            return 0
        # Imported here, not at module scope: autonomy imports this module, and a
        # top-level import would be circular. The verified loop is the default
        # because an unverified "done" is not an outcome — but a failing goal
        # exits non-zero so a shell caller can branch on it rather than reading
        # prose to find out whether the run succeeded.
        import autonomy
        result = autonomy.run_goal_verified(config, args.goal)
        print(autonomy.format_result(result))
        return 0 if result.get("ok") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())