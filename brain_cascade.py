#!/usr/bin/env python3
"""
brain_cascade.py — the cascading free-provider brain (FREE-BRAIN.md §5 / P0).

Cascade: local (if usable) -> groq -> cerebras -> gemini -> openrouter -> mistral -> github

Stdlib only. Cooldowns persisted in residence. BRAIN_CASCADE=0 = local only.
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RESIDENCE = "freebrain-residence"

PROVIDER_REGISTRY = [
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1",
        "key_envs": ("GROQ_API_KEY",),
        "models": [
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ],
        "prefs": [
            r"gpt-oss-20b",
            r"gpt-oss-120b",
            r"^openai/gpt-oss",
            r"llama-3\.3",
            r"llama-3\.1-8b",
        ],
        "note": "LPU inference, fastest free tier; hard daily request caps",
    },
    {
        "name": "cerebras",
        "url": "https://api.cerebras.ai/v1",
        "key_envs": ("CEREBRAS_API_KEY",),
        "models": ["gpt-oss-120b", "qwen-3.8-27b", "gemma-4-31b"],
        "prefs": [r"gpt-oss-120b", r"^qwen", r"^gemma"],
        "note": "wafer-scale, very high throughput free tier",
    },
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_envs": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "models": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"],
        "prefs": [r"gemini-2\.0-flash", r"gemini-2\.5-flash", r"gemini-.*flash", r"gemini"],
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
DEFAULT_PAYMENT_COOLDOWN_S = 3600


def _env(env):
    return env if env is not None else os.environ


def _flag(value, default=False):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in ("0", "false", "off", "no", "disabled")


def cascade_enabled(env=None):
    return _flag(_env(env).get("BRAIN_CASCADE"), default=True)


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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
        return False


def cooldown_remaining(name, env=None, now=None):
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
    text = str(error).lower()
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
    if "model" in text and ("404" in text or "not found" in text or "does not exist" in text or "deprecat" in text or "retired" in text):
        return "model-missing"
    return "server"


def provider_order(env=None):
    env = _env(env)
    raw = (env.get("BRAIN_PROVIDERS") or "").strip()
    if not raw:
        return [p["name"] for p in PROVIDER_REGISTRY]
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return [n for n in names if n in PROVIDER_BY_NAME]


def provider_keys(provider, env=None):
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
    keys = provider_keys(provider, env)
    return keys[0] if keys else ("", "")


def provider_model(provider, env=None):
    env = _env(env)
    override = (env.get(provider["name"].upper() + "_MODEL") or "").strip()
    return override or (provider.get("models") or [""])[0]


def local_provider(config):
    entry = {
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
    if config.get("stream_budget_ms") is not None:
        entry["stream_budget_ms"] = config["stream_budget_ms"]
    return entry


def local_has_models(config, timeout_s=1.5):
    """True if local server lists at least one model."""
    url = (config.get("url") or "").rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "FreeBrain/1.0")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = data.get("data") if isinstance(data, dict) else None
        if isinstance(models, list) and len(models) > 0:
            return True
        models = data.get("models") if isinstance(data, dict) else None
        return isinstance(models, list) and len(models) > 0
    except Exception:
        return False


def skip_empty_local(env=None):
    return _flag(_env(env).get("BRAIN_SKIP_EMPTY_LOCAL"), default=True)


def chain(config, env=None):
    env = _env(env)
    entries = []
    include_local = True
    if skip_empty_local(env) and cascade_enabled(env):
        has_cloud = any(provider_key(p, env)[0] for p in PROVIDER_REGISTRY)
        if has_cloud and not local_has_models(config):
            include_local = False
    if include_local:
        entries.append(local_provider(config))
    elif not cascade_enabled(env):
        entries.append(local_provider(config))
    if not cascade_enabled(env):
        return entries
    for name in provider_order(env):
        provider = PROVIDER_BY_NAME.get(name)
        if not provider:
            continue
        keys = provider_keys(provider, env)
        if not keys:
            continue
        for index, (key, var) in enumerate(keys):
            entries.append({
                "name": name if index == 0 else "%s#%d" % (name, index + 1),
                "registry": name,
                "key_var": var,
                "url": provider["url"].rstrip("/"),
                "model": provider_model(provider, env),
                "key": key,
                "timeout_ms": min(config["timeout_ms"],
                                  _int(env.get("BRAIN_CLOUD_TIMEOUT_MS"), 60000)),
                "temperature": config["temperature"],
                "max_tokens": config["max_tokens"],
                "local": False,
                "note": provider.get("note", ""),
            })
            if config.get("stream_budget_ms") is not None:
                entries[-1]["stream_budget_ms"] = config["stream_budget_ms"]
    ready, cooling = [], []
    for entry in entries:
        (cooling if cooldown_remaining(entry["name"], env) > 0 else ready).append(entry)
    return ready + cooling


def provider_hop_names(env=None):
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
    env = _env(env)
    entries = chain(config, env)
    health = load_health(env)
    lines = []
    enabled = cascade_enabled(env)
    lines.append("cascade: %s" % ("enabled" if enabled else "disabled (BRAIN_CASCADE=0 — local only)"))
    if skip_empty_local(env) and not any(e.get("local") for e in entries):
        lines.append("  (local skipped — no models on server; set BRAIN_SKIP_EMPTY_LOCAL=0 to force)")
    for i, entry in enumerate(entries, 1):
        state = health.get(entry["name"]) or {}
        cooling = cooldown_remaining(entry["name"], env)
        marks = []
        if cooling:
            marks.append("cooldown %ss (%s)" % (cooling, state.get("last_class") or "?"))
        elif state.get("total_ok"):
            marks.append("ok x%s" % state.get("total_ok"))
        if entry.get("key_var"):
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
    missing = [p["name"] for p in PROVIDER_REGISTRY
               if p["name"] in provider_order(env) and not provider_key(p, env)[0]]
    if enabled and missing:
        lines.append("  no key set (free, add when you want them): %s" % ", ".join(missing))
    return "\n".join(lines)


NON_CHAT_MODEL = re.compile(
    r"whisper|tts|speech|orpheus|voice|embed|rerank|moderation|guard|safeguard"
    r"|vision|image|audio|transcri", re.I)


def discover_model(provider, ids):
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
    try:
        ids = [i for i in (discover(provider) or []) if i]
    except Exception:
        return ""
    return discover_model(provider, ids)


PROBE_PROMPT = "ping"

PROBE_VERDICTS = {
    "payment": "PAID WALL — the key authenticates but cannot serve",
    "auth": "KEY REJECTED",
    "blocked": "CLIENT BLOCKED by the edge (UA/fingerprint)",
    "rate-limit": "RATE LIMITED / quota exhausted",
    "model-missing": "MODEL RETIRED",
    "server": "UNREACHABLE or server error",
}


def probe_entry(entry, call_one, env=None, discover=None, on_event=None):
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
    return [probe_entry(e, call_one, env, discover) for e in entries]


def format_probe(results):
    lines = ["probe (1-token completion per hop):"]
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
    return "\n".join(lines)


def health_snapshot(config, env=None, now=None):
    env = _env(env)
    hops = [entry["name"] for entry in chain(config, env)]
    state = load_health(env)
    cooling = {}
    for name in hops:
        remaining = cooldown_remaining(name, env, now)
        if remaining > 0:
            cooling[name] = {
                "remaining_s": remaining,
                "class": (state.get(name) or {}).get("last_class"),
            }
    if not hops:
        status = "exhausted"
    elif cooling and len(cooling) >= len(hops):
        status = "exhausted"
    elif cooling:
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "chain": hops, "cooling": cooling}


def run_cascade(entries, call_one, env=None, discover=None, on_event=None):
    env = _env(env)
    attempts = []
    for entry in entries:
        name = entry["name"]
        model = entry.get("model") or ""
        try:
            reply = call_one(dict(entry))
            note_success(name, env)
            if on_event:
                on_event("ok", entry, "")
            return reply
        except RuntimeError as e:
            cls = classify(e)
            if cls == "model-missing" and discover is not None:
                new_model = refresh_model(entry, discover)
                if new_model and new_model != model:
                    print("[brain] %s: model %r was retired — re-discovered %r and recovered"
                          % (name, model, new_model), file=sys.stderr)
                    entry = dict(entry, model=new_model)
                    try:
                        reply = call_one(entry)
                        note_success(name, env)
                        if on_event:
                            on_event("ok", entry, "")
                        return reply
                    except RuntimeError as e2:
                        e = e2
                        cls = classify(e2)
            cd = note_failure(name, e, env)
            attempts.append("%s[%s]: %s" % (name, cls, str(e)[:120]))
            print("[brain] %s failed (%s) — cooling down %ss, next provider"
                  % (name, cls, cd), file=sys.stderr)
            if on_event:
                on_event("fail", entry, str(e))
    detail = "; ".join(attempts)[:800]
    raise RuntimeError("brain cascade exhausted (%d providers): %s" % (len(attempts), detail))
