#!/usr/bin/env python3
"""
brain_cascade.py — the cascading free-provider brain (FREE-BRAIN.md §5 / P0).

The brain used to have exactly one voice: the local OpenAI-compatible server.
That is a single point of failure — a stopped Ollama, a throttled free tier, or
a provider silently deleting a model stops the loop. This module turns "the
brain" into an ordered *cascade*: local weights first (FREE-BRAIN.md's
local-first rule stays intact), then free hosted providers, each one tried in
turn until one answers.

  local  ->  groq  ->  cerebras  ->  gemini  ->  openrouter  ->  mistral  ->  github

Design rules (deliberate, matching the runtime's zero-dependency stance):

  * stdlib only. No SDKs, no new packages.
  * Every provider speaks the same OpenAI-compatible /chat/completions, so the
    transport in agent_runtime.py is reused unchanged — only base URL, key and
    model differ.
  * Providers with no API key in the environment are simply *not in the chain*;
    if no key is configured at all the chain is exactly [local], i.e. the
    original offline brain. Nothing changes for an offline user.
  * Failures are data, not noise. Each failed provider is put in a *cooldown*
    keyed by failure class (rate limit vs server error vs bad auth), persisted
    to the residence so a restart does not hammer a provider that just 429'd.
  * BRAIN_CASCADE=0 disables the whole thing — the switch the FREE-BRAIN.md
    "offline battery" test needs to prove the loop still runs with zero network.

Configuration (all optional):

    BRAIN_CASCADE=0                 disable cascade -> local only (offline mode)
    BRAIN_PROVIDERS=groq,gemini     override the chain order/filter
    BRAIN_HEALTH_FILE=<path>        default <residence>/provider-health.json
    BRAIN_RATE_LIMIT_COOLDOWN_S     default 60   (429 / quota)
    BRAIN_SERVER_COOLDOWN_S         default 30   (5xx, network unreachable)
    BRAIN_AUTH_COOLDOWN_S           default 3600 (401/403 — a bad key stays bad)

    GROQ_API_KEY / CEREBRAS_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY /
    MISTRAL_API_KEY / GITHUB_TOKEN          provider credentials (free tiers)

    GROQ_MODEL / CEREBRAS_MODEL / ...       per-provider model override

All of the providers below have a free tier and an OpenAI-compatible endpoint.
Free tiers are hard rate caps, not soft warnings (which is exactly why the
cooldown exists). Default model names drift — providers retire them — so on a
"model-missing" failure the cascade re-discovers that provider's model list
once and retries, instead of failing the whole chain.
"""

import datetime
import json
import os
import re
import sys
import time

DEFAULT_RESIDENCE = "freebrain-residence"

# ── The free-provider registry ────────────────────────────────────────────────
#
# Order here is the default chain order. Grouped by "how free": every entry
# below is usable without a payment method. `models` is the preferred-model
# order; `prefs` are regexes used to pick a replacement when the preferred name
# no longer exists (both are advisory — the env override always wins).

PROVIDER_REGISTRY = [
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1",
        "key_envs": ("GROQ_API_KEY",),
        # Verified against the live /models list on 2026-09-13 (llama-3.3-70b-versatile,
        # the previous default here, is retired). These names will drift again —
        # that is what the discovery retry exists for.
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
                   "groq/compound-mini"],
        "prefs": [r"gpt-oss-120b", r"^openai/gpt-oss", r"^qwen/qwen3", r"^groq/compound"],
        "note": "LPU inference, fastest free tier; hard daily request caps",
    },
    {
        "name": "cerebras",
        "url": "https://api.cerebras.ai/v1",
        "key_envs": ("CEREBRAS_API_KEY",),
        # Verified against the live /models list on 2026-09-13. The llama names
        # that used to be here (llama-3.3-70b, llama3.1-8b, qwen-3-coder-480b)
        # are all retired from the free listing — same drift the discovery
        # retry exists to absorb.
        "models": ["gpt-oss-120b", "qwen-3.8-27b", "gemma-4-31b"],
        "prefs": [r"gpt-oss-120b", r"^qwen", r"^gemma"],
        "note": "wafer-scale, very high throughput free tier",
    },
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_envs": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "models": ["gemini-2.5-flash", "gemini-2.0-flash"],
        "prefs": [r"gemini-2\.5-flash$", r"gemini-.*flash", r"gemini"],
        "note": "Google AI Studio free tier (not Vertex — that stays paid/GCP)",
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1",
        "key_envs": ("OPENROUTER_API_KEY",),
        "models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "qwen/qwen-2.5-72b-instruct:free",
        ],
        "prefs": [r":free$", r"/free$"],
        "note": "one key, many free model slots (:free suffix); routing fallbacks",
    },
    {
        "name": "mistral",
        "url": "https://api.mistral.ai/v1",
        "key_envs": ("MISTRAL_API_KEY",),
        "models": ["mistral-small-latest", "open-mistral-nemo"],
        "prefs": [r"mistral-small", r"open-mistral", r"mistral"],
        "note": "La Plateforme free experiment tier",
    },
    {
        "name": "github",
        "url": "https://models.github.ai/inference",
        "key_envs": ("GITHUB_TOKEN", "GITHUB_MODELS_TOKEN"),
        "models": ["openai/gpt-4o-mini", "openai/gpt-4.1-mini"],
        "prefs": [r"gpt-4o-mini", r"gpt-4\.1-mini", r"gpt", r"llama"],
        "note": "GitHub Models — free with a GitHub account",
    },
]

PROVIDER_BY_NAME = dict((p["name"], p) for p in PROVIDER_REGISTRY)

DEFAULT_RATE_LIMIT_COOLDOWN_S = 60
DEFAULT_SERVER_COOLDOWN_S = 30
DEFAULT_AUTH_COOLDOWN_S = 3600
DEFAULT_BLOCKED_COOLDOWN_S = 300
# A paid wall is not a soft throttle: retrying in a minute helps nobody, and
# hammering it every minute is how a free key gets noticed. Same window as auth.
DEFAULT_PAYMENT_COOLDOWN_S = 3600


def _env(env):
    return env if env is not None else os.environ


def _flag(value, default=False):
    """BRAIN_CASCADE style flag: 0/false/off/no disable; unset keeps default."""
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in ("0", "false", "off", "no", "disabled")


def cascade_enabled(env=None):
    """False when BRAIN_CASCADE=0 — the offline-mode switch."""
    return _flag(_env(env).get("BRAIN_CASCADE"), default=True)


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ── Health state (cooldowns, persisted in the residence) ──────────────────────

def health_path(env=None):
    env = _env(env)
    custom = (env.get("BRAIN_HEALTH_FILE") or "").strip()
    if custom:
        return custom
    res = (env.get("DRIVE_RESIDENCE") or "").strip() or DEFAULT_RESIDENCE
    return os.path.join(res, "provider-health.json")


def load_health(env=None):
    path = health_path(env)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_health(state, env=None):
    path = health_path(env)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        return True
    except OSError:
        return False  # a broken health file must never break a run


def cooldown_remaining(name, env=None, now=None):
    """Seconds until `name` may be tried again (0 = usable right now)."""
    entry = load_health(env).get(name) or {}
    until = entry.get("until")
    if until is None:
        return 0
    now = time.time() if now is None else now
    return max(0, int(round(float(until) - now)))


def note_success(name, env=None):
    state = load_health(env)
    prev = state.get(name) or {}
    state[name] = {
        "ok": True,
        "fails": 0,
        "last_ok": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "total_ok": int(prev.get("total_ok") or 0) + 1,
        "total_fail": int(prev.get("total_fail") or 0),
    }
    save_health(state, env)
    return state[name]


def note_failure(name, error, env=None, cooldown_s=None):
    """Record a failure and return the applied cooldown in seconds."""
    state = load_health(env)
    prev = state.get(name) or {}
    cls = classify(error)
    if cooldown_s is None:
        cooldown_s = {
            "rate-limit": _int(_env(env).get("BRAIN_RATE_LIMIT_COOLDOWN_S"), DEFAULT_RATE_LIMIT_COOLDOWN_S),
            "auth": _int(_env(env).get("BRAIN_AUTH_COOLDOWN_S"), DEFAULT_AUTH_COOLDOWN_S),
            "blocked": _int(_env(env).get("BRAIN_BLOCKED_COOLDOWN_S"), DEFAULT_BLOCKED_COOLDOWN_S),
            "payment": _int(_env(env).get("BRAIN_PAYMENT_COOLDOWN_S"), DEFAULT_PAYMENT_COOLDOWN_S),
            "model-missing": 300,
        }.get(cls, _int(_env(env).get("BRAIN_SERVER_COOLDOWN_S"), DEFAULT_SERVER_COOLDOWN_S))
    state[name] = {
        "ok": False,
        "fails": int(prev.get("fails") or 0) + 1,
        "last_class": cls,
        "last_error": str(error)[:300],
        "last_fail": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "until": round(time.time() + max(0, cooldown_s), 3),
        "cooldown_s": max(0, cooldown_s),
        "total_ok": int(prev.get("total_ok") or 0),
        "total_fail": int(prev.get("total_fail") or 0) + 1,
    }
    save_health(state, env)
    return max(0, cooldown_s)


def classify(error):
    """Map a provider error to a cooldown class. Deterministic string match on
    the error text the transports raise — never on model prose.

    WAF/fingerprint blocks are checked before auth on purpose: an edge network
    answering 403 to the client's signature (`error code: 1010`) is a client
    bug, and reporting it as a rejected credential would bench a perfectly good
    key for an hour (this actually happened — see agent_runtime.USER_AGENT)."""
    text = str(error).lower()
    # Checked before rate-limit on purpose: a paid wall answers 402 whose body even
    # says `"param": "quota"`, so the rate-limit patterns below would swallow it and
    # the provider would be retried every 60s forever. A key can authenticate fine
    # (live /models -> 200) and still be unable to serve a single completion — the
    # two are different failures (Cerebras free trial, observed live 2026-09-13).
    if ("402" in text or "payment required" in text or "payment_required" in text
            or "insufficient credits" in text or "billing" in text):
        return "payment"
    if "rate limit" in text or "429" in text or "quota" in text or "too many requests" in text:
        return "rate-limit"
    if ("1010" in text or "cloudflare" in text or "banned your access" in text
            or "just a moment" in text or "captcha" in text):
        return "blocked"
    if "401" in text or "403" in text or "unauthorized" in text or "invalid api key" in text or "api key" in text:
        return "auth"
    if "model" in text and ("404" in text or "not found" in text or "does not exist" in text or "deprecat" in text):
        return "model-missing"
    return "server"


# ── Chain construction ────────────────────────────────────────────────────────

def provider_order(env=None):
    """Registry order, or the BRAIN_PROVIDERS csv subset/order when set."""
    env = _env(env)
    raw = (env.get("BRAIN_PROVIDERS") or "").strip()
    if not raw:
        return [p["name"] for p in PROVIDER_REGISTRY]
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return [n for n in names if n in PROVIDER_BY_NAME]


def provider_keys(provider, env=None):
    """Every key configured for one provider, in order, as (key, env_var) pairs.

    Free tiers cap *per key*, so a single key is a single point of failure even
    when the provider is fine. Supported forms, checked in this order:

        GROQ_API_KEY=<k1>          the plain registry variable
        GROQ_API_KEY_2=<k2> ...    numbered siblings (2..9)
        GROQ_API_KEYS=k1,k2,k3     csv (suffixes the registry variable with S)

    Identical keys are de-duplicated so a key pasted twice is not two hops."""
    env = _env(env)
    found, seen = [], set()

    def add(value, var):
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            found.append((value, var))

    for var in provider.get("key_envs", ()):
        add(env.get(var), var)
        for i in range(2, 10):
            add(env.get("%s_%d" % (var, i)), "%s_%d" % (var, i))
        for i, value in enumerate((env.get(var + "S") or "").split(",")):
            add(value, "%sS[%d]" % (var, i))
    return found


def provider_key(provider, env=None):
    """First configured key — the provider's primary credential."""
    keys = provider_keys(provider, env)
    return keys[0] if keys else ("", "")


def provider_model(provider, env=None):
    env = _env(env)
    override = (env.get(provider["name"].upper() + "_MODEL") or "").strip()
    return override or (provider.get("models") or [""])[0]


def local_provider(config):
    """The local OpenAI-compatible server as cascade entry #0. Always present
    when configured — local weights keep priority per FREE-BRAIN.md."""
    return {
        "name": "local",
        "url": config["url"],
        "model": config.get("model") or "",
        "key": "",
        "timeout_ms": config["timeout_ms"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
        "local": True,
        "note": "local open weights (Ollama/vLLM/llama.cpp) — zero cloud calls",
    }


def chain(config, env=None):
    """Ordered provider configs to try, cooldown-eligible ones first.

    Entry 0 is always the local server. Cloud entries appear only when their API
    key is present in the environment. Providers currently in cooldown are moved
    to the back (not dropped) so a run can still use them as a last resort.
    """
    env = _env(env)
    entries = [local_provider(config)]
    if not cascade_enabled(env):
        return entries
    for name in provider_order(env):
        provider = PROVIDER_BY_NAME.get(name)
        if not provider:
            continue
        keys = provider_keys(provider, env)
        if not keys:
            continue  # no key -> not in the chain at all (offline stays offline)
        for index, (key, var) in enumerate(keys):
            entries.append({
                # Second and later keys of one provider are separate hops:
                # `groq`, `groq#2`, ... so a per-key rate limit benches only
                # that key while its siblings keep serving.
                "name": name if index == 0 else "%s#%d" % (name, index + 1),
                "registry": name,
                "key_var": var,
                "url": provider["url"].rstrip("/"),
                "model": provider_model(provider, env),
                "key": key,
                # Snappier failover than the local default: a hosted provider that
                # has not answered in a minute is not going to keep the loop alive.
                "timeout_ms": min(config["timeout_ms"],
                                  _int(env.get("BRAIN_CLOUD_TIMEOUT_MS"), 60000)),
                "temperature": config["temperature"],
                "max_tokens": config["max_tokens"],
                "local": False,
                "note": provider.get("note", ""),
            })
    ready, cooling = [], []
    for entry in entries:
        (cooling if cooldown_remaining(entry["name"], env) > 0 else ready).append(entry)
    return ready + cooling


def provider_hop_names(env=None):
    """Base provider names in chain order (one per provider, not per key)."""
    return provider_order(_env(env))


def configured_names(env=None):
    env = _env(env)
    names = []
    for name in provider_order(env):
        provider = PROVIDER_BY_NAME.get(name)
        if provider and provider_key(provider, env)[0]:
            names.append(name)
    return names


def format_chain(config, env=None):
    """Human-readable cascade listing for the CLI (no network calls)."""
    env = _env(env)
    entries = chain(config, env)
    health = load_health(env)
    lines = []
    enabled = cascade_enabled(env)
    lines.append("cascade: %s" % ("enabled" if enabled else "disabled (BRAIN_CASCADE=0 — local only)"))
    for i, entry in enumerate(entries, 1):
        state = health.get(entry["name"]) or {}
        cooling = cooldown_remaining(entry["name"], env)
        marks = []
        if cooling:
            marks.append("cooldown %ss (%s)" % (cooling, state.get("last_class") or "?"))
        elif state.get("total_ok"):
            marks.append("ok x%s" % state.get("total_ok"))
        if entry.get("key_var"):  # which credential this hop spends (never the value)
            marks.append("key=" + entry["key_var"])
        if not entry.get("local") and not entry.get("key"):
            marks.append("no key")
        lines.append(
            "  [%d] %-11s %-52s model=%s%s%s"
            % (
                i, entry["name"], entry["url"],
                entry["model"] or "(server default)",
                ("  " + " | ".join(marks)) if marks else "",
                "" if not entry.get("note") else "  — " + entry["note"],
            )
        )
    skipped = [p["name"] for p in PROVIDER_REGISTRY if p["name"] not in provider_order(env)]
    if skipped:
        lines.append("  (excluded by BRAIN_PROVIDERS: %s)" % ", ".join(skipped))
    missing = [p["name"] for p in PROVIDER_REGISTRY
               if p["name"] in provider_order(env) and not provider_key(p, env)[0]]
    if enabled and missing:
        lines.append("  no key set (free, add when you want them): %s" % ", ".join(missing))
    return "\n".join(lines)


# ── The cascade policy ────────────────────────────────────────────────────────

# Model ids that are never a chat backend, however tempting the ordering looks.
NON_CHAT_MODEL = re.compile(
    r"whisper|tts|speech|orpheus|voice|embed|rerank|moderation|guard|safeguard"
    r"|vision|image|audio|transcri", re.I)


def discover_model(provider, ids):
    """Pick the best model id from a provider's live /models list: first
    preferred name that still exists, else the first preferred *pattern* that
    matches, else the first usable id. Model names drift; this is the drift fix.

    Non-chat endpoints (whisper, guards, embeddings) are filtered out first —
    a live Groq listing is mostly those, and a discovery fallback that picked
    `whisper-large-v3` for a tool loop would be worse than failing."""
    registry = PROVIDER_BY_NAME.get(provider.get("registry") or provider["name"])
    if not registry or not ids:
        return ""
    chat_ids = [i for i in ids if not NON_CHAT_MODEL.search(i)]
    for want in registry.get("models", []):
        if want in chat_ids:
            return want
    for pattern in registry.get("prefs", []):
        for mid in chat_ids:
            if re.search(pattern, mid, re.I):
                return mid
    return chat_ids[0] if chat_ids else ""


def refresh_model(provider, discover):
    """Re-resolve a provider's model from live discovery. Returns the new model
    (possibly unchanged); empty string when discovery is unavailable."""
    try:
        ids = [i for i in (discover(provider) or []) if i]
    except Exception:
        return ""
    return discover_model(provider, ids)


# ── Probing a hop with a real completion ──────────────────────────────────────

# The prompt is irrelevant — nothing reads the answer. It exists so the call is a
# real completion rather than a metadata request.
PROBE_PROMPT = "ping"

# What each failure class means in words. `/models` cannot see any of this: a key
# can list models happily while every completion is refused.
PROBE_VERDICTS = {
    "payment": "PAID WALL — the key authenticates but cannot serve",
    "auth": "KEY REJECTED",
    "blocked": "CLIENT BLOCKED by the edge (UA/fingerprint)",
    "rate-limit": "RATE LIMITED / quota exhausted",
    "model-missing": "MODEL RETIRED",
    "server": "UNREACHABLE or server error",
}


def probe_entry(entry, call_one, env=None, discover=None, on_event=None):
    """Probe ONE hop with a real (1-token) completion.

    A `/models` probe only answers "is this endpoint listening, and does the key
    authenticate" — but a provider can authenticate fine and still refuse every
    completion (a paid wall, a suspended project, a retired model). This sends an
    actual minimal completion so that failure is visible before a run depends on
    the hop. The transport stays injected (`call_one`), like run_cascade.

    Failures are recorded exactly as a real call records them (note_failure), so
    a probe produces the same cooldowns a run would — a hop that cannot serve is
    benched by being probed, not merely reported. Returns a result dict:
    {name, model, ok, class, error, cooldown_s, tokens, elapsed_s, rediscovered}.
    """
    env = _env(env)
    name = entry["name"]
    model = entry.get("model") or ""
    result = {"name": name, "model": model, "ok": False, "class": "",
              "error": "", "cooldown_s": 0, "tokens": 0, "elapsed_s": 0.0,
              "rediscovered": ""}
    try:
        reply = call_one(dict(entry))
    except RuntimeError as e:
        cls = classify(e)
        if cls == "model-missing" and discover is not None:
            # Same one-shot self-heal a real call gets, so a probe reports a
            # usable hop instead of benching one that only needs a new model name.
            new_model = refresh_model(entry, discover)
            if new_model and new_model != model:
                entry = dict(entry, model=new_model)
                try:
                    reply = call_one(entry)
                except RuntimeError as e2:
                    e, cls = e2, classify(e2)
                else:
                    result["rediscovered"] = new_model
                    model = new_model
        if not result["rediscovered"]:
            result["class"] = cls
            result["error"] = str(e)[:200]
            result["cooldown_s"] = note_failure(name, e, env)
            if on_event:
                on_event("probe-fail", entry, str(e))
            return result
    result["ok"] = True
    result["model"] = model or (reply.get("model") or "")
    result["tokens"] = int(reply.get("tokens") or 0)
    result["elapsed_s"] = float(reply.get("elapsed") or 0.0)
    note_success(name, env)
    if on_event:
        on_event("probe-ok", entry, "")
    return result


def probe_chain(entries, call_one, env=None, discover=None):
    """Probe every hop in chain order. Never raises: the report is the result."""
    return [probe_entry(e, call_one, env, discover) for e in entries]


def format_probe(results):
    """Human-readable probe report for the CLI (mirrors format_chain's style)."""
    lines = ["probe (1-token completion per hop — a paid wall is invisible to /models):"]
    for i, r in enumerate(results, 1):
        if r.get("ok"):
            note = ""
            if r.get("rediscovered"):
                note = "  (model retired -> re-discovered %s)" % r["rediscovered"]
            lines.append("  [%d] %-11s ok — %.1fs, %d tok, model=%s%s"
                         % (i, r["name"], r.get("elapsed_s") or 0.0,
                            r.get("tokens") or 0, r.get("model") or "?", note))
        else:
            cls = r.get("class") or "?"
            cooled = "  (benched %ss)" % r["cooldown_s"] if r.get("cooldown_s") else ""
            lines.append("  [%d] %-11s FAIL (%s) — %s%s"
                         % (i, r["name"], PROBE_VERDICTS.get(cls, cls),
                            r.get("error") or "", cooled))
    down = [r["name"] for r in results if not r.get("ok")]
    if down:
        lines.append("  benched until cooldown expires: %s" % ", ".join(down))
    return "\n".join(lines)


# ── Cascade health for the ledger (drift_loop.py) ─────────────────────────────

def health_snapshot(config, env=None, now=None):
    """Compact cascade health for one moment — the block the drift loop writes
    into every ledger record (drift_loop.py), so a provider outage is a visible
    change in the timeline instead of something inferred from a latency column.

    `chain` order is included because it *is* the degradation: a benched hop is
    moved to the back, so the same chain list with a different order is the
    outage. `cooling` carries only hops that cannot serve right now, with the
    class that benched them and the seconds left. Hop *names* only — never a key
    or any part of one.

    status:
      ok        every hop is usable
      degraded  some hops are benched; the next calls must fail over
      exhausted every hop is benched — the next call has nowhere to go
    """
    env = _env(env)
    hops = [entry["name"] for entry in chain(config, env)]
    state = load_health(env)
    cooling = {}
    for name in hops:
        remaining = cooldown_remaining(name, env, now)
        if remaining > 0:
            record = state.get(name) or {}
            cooling[name] = {
                "class": record.get("last_class") or "?",
                "cooldown_s": remaining,
                "fails": int(record.get("total_fail") or 0),
            }
    ready = [n for n in hops if n not in cooling]
    if not hops:
        status = "exhausted"  # unreachable in practice: `local` is always a hop
    elif not ready:
        status = "exhausted"
    elif cooling:
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "chain": hops, "cooling": cooling}


def run_cascade(entries, call_one, env=None, discover=None, on_event=None):
    """Try each provider until one answers. `call_one(provider)` performs the
    actual call (transport injected by agent_runtime.py) and raises RuntimeError
    on failure. Returns the reply dict with `provider` and `attempts` added.

    A provider whose failure classifies as "model-missing" gets exactly one
    re-discovery retry before it is put in cooldown. When every provider fails
    the error is structured and lists every attempt — the loop reports honest
    failure instead of pretending a call happened.
    """
    env = _env(env)
    if not entries:
        raise RuntimeError("brain cascade: no providers configured (no local server, no API keys)")
    attempts = []
    for entry in entries:
        name = entry["name"]
        try:
            reply = call_one(dict(entry))
        except RuntimeError as e:
            cls = classify(e)
            if cls == "model-missing" and discover is not None:
                new_model = refresh_model(entry, discover)
                if new_model and new_model != entry["model"]:
                    old_model = entry["model"]
                    if on_event:
                        on_event("rediscover", entry, "%s -> %s" % (old_model or "?", new_model))
                    entry = dict(entry, model=new_model)
                    try:
                        reply = call_one(entry)
                    except RuntimeError as e2:
                        e = e2
                    else:
                        note_success(name, env)
                        print("[brain] %s: model %r was retired — re-discovered %r and recovered"
                              % (name, old_model, new_model), file=sys.stderr)
                        reply = dict(reply, provider=name, provider_url=entry["url"],
                                     provider_key=entry.get("key_var") or "", model=new_model,
                                     attempts=attempts + [{"provider": name, "class": "model-missing",
                                                           "error": str(e)[:200], "cooldown_s": 0}])
                        return reply
            cooldown = note_failure(name, e, env)
            attempts.append({"provider": name, "class": classify(e), "error": str(e)[:200],
                             "cooldown_s": cooldown})
            if on_event:
                on_event("fail", entry, str(e))
            print("[brain] %s failed (%s) — cooling down %ss, next provider" % (name, classify(e), cooldown),
                  file=sys.stderr)
            continue
        note_success(name, env)
        if on_event:
            on_event("ok", entry, "")
        if attempts:
            print("[brain] failover -> %s (%s)" % (name, entry["model"] or "server default"), file=sys.stderr)
        return dict(reply, provider=name, provider_url=entry["url"],
                    provider_key=entry.get("key_var") or "",
                    model=entry["model"] or reply.get("model") or "", attempts=attempts)
    detail = "; ".join("%s[%s]: %s" % (a["provider"], a.get("class", "?"), a["error"]) for a in attempts)
    raise RuntimeError("brain cascade exhausted (%d providers): %s" % (len(attempts), detail))
