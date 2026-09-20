#!/usr/bin/env python3
"""
drift_loop.py — Free Brain file-driven self-rewrite loop (Q4 / FREE-BRAIN.md §3-4).

The loop is a FILE-DRIVEN STATE MACHINE. The residence folder IS the machine:
every fact the loop needs (objective, the mutable instruction set, the
checkpoint, the ledger, the trigger) is a file. A tiny driver here — running on
any box with a CPU — watches the files and executes one cycle per trigger.
Drive mirrors the folder, so the machine's memory is portable and survives any
box dying. The files themselves never execute anything; the driver does.

Residence protocol (all state is files, under DRIVE_RESIDENCE):

    instructions.md   the mutable self — what the loop rewrites each cycle
    state.json        checkpoint {run, cycle, phase, breaker, coherence}
    ledger.jsonl      one line per cycle (graph hash, gates, coherence, perf,
                      cascade health, QIH metrics)
    trigger           when present, the watcher runs exactly one cycle
    next              touched when a cycle completes (the chain continues)
    evidence/q1-evidence.jsonl   throughput records (agent_runtime._emit_step)

One cycle:

    read state.json + instructions.md
    → PHASE 1: model emits a dispatch graph (JSON)     → hash it (Test 2)
    → PHASE 2: model emits a revised instruction set    → gate it
    → accept/reject → write ledger + state + instructions.md → touch "next"

Gates are deterministic (FREE-BRAIN.md §6.2): graph actions must be
allowlisted; the rewrite must keep the objective's anchor words, stay under the
instruction cap, and never mention guard-scoped concepts. Breakers come from
AGENT-INTEGRITY: 2 consecutive same-signature failures or 3 distinct failures
trip the run with a structured failure.

Usage:
    python3 drift_loop.py --cycles 1000                 # continuous run
    python3 drift_loop.py --watch                       # one cycle per trigger touch
    python3 drift_loop.py --determinism 5               # Test 2 battery
    python3 drift_loop.py --resume                      # (default) continue state.json
    python3 drift_loop.py --max-seconds 19800            # stop cleanly before a hard kill
                                                         # (5.5 h on a 6 h CI runner)

    python3 drift_loop.py --max-trips 400 --max-alarms 200  # survive 1000 cycles unattended:
                                                         # auto-resume breaker trips + drift alarms

Env: LOCAL_MODEL_URL / LOCAL_MODEL (see agent_runtime.py), DRIVE_RESIDENCE,
DRIVE_REMOTE (rclone remote), FREE_BRAIN_OBJECTIVE, EVIDENCE_FILE,
FREE_BRAIN_MAX_TRIPS, FREE_BRAIN_MAX_ALARMS.
Exit codes: 0 complete (or a clean stop on the --max-seconds budget), 2 breaker
trip / drift alarm / refusal to fork the record, 3 determinism fail.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
import time
import uuid

from agent_runtime import (
    load_config,
    load_env_file,
    list_models,
    chat_stream,
    _emit_step,
    TOOLS,
    residence_path,
    _ensure_residence,
)

import brain_cascade
import qih_metrics

# ── Constants (the pre-registered contract — the model cannot raise these) ────

DEFAULT_CYCLES = 1000          # the Q4 target
DEFAULT_TEMPERATURE = 0.7      # self-rewrites must vary; determinism pins its own
INSTRUCTION_CAP = 2000         # chars — write-scope cap on the mutable self
RETENTION_FLOOR = 0.5          # fraction of objective anchors that must survive a rewrite
COHERENCE_FLOOR = 0.4          # below this, drift is suspected
DRIFT_STREAK = 5               # consecutive sub-floor cycles → drift alarm
GRAPH_MAX_TOKENS = 256
REWRITE_MAX_TOKENS = 512
DEFAULT_OBJECTIVE = (
    "Maintain a coherent self-rewriting cognitive loop that preserves its "
    "stated objective across 1000 continuous cycles without cognitive drift "
    "or infinite recursion."
)
# Guard-scoped concepts the rewrite may never mention (FREE-BRAIN.md §5/§10).
GUARD_WORDS = [
    "sandbox", "ledger", "guard", "permission", "docker",
    "max_mem", "max_seconds", "kernel", "raise caps",
]
GRAPH_ACTIONS = set(TOOLS) | {"verify"}  # allowlist only
QIH_WINDOW = 10  # trailing per-cycle coherence window for the C_MT trend metric


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _canonical_hash(obj):
    """sha256 of canonical JSON — the Test 2 dispatch-graph hash."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _extract_json(text):
    """First balanced {...} block in the model's reply, or None."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _json_stop(content):
    """Early-stop predicate: cut the stream once a balanced JSON object closes
    (tool directives and graphs are short; don't wait for full generations)."""
    depth = 0
    in_str = False
    esc = False
    for c in content.strip():
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return True
    return False


def _objective_anchors(objective):
    return [w for w in re.findall(r"[A-Za-z]{5,}", objective.lower())]


# ── Deterministic gates (FREE-BRAIN.md §6.2) ─────────────────────────────────

def _gate_graph(g):
    """The dispatch graph must parse and use only allowlisted actions."""
    if not isinstance(g, dict) or "graph" not in g:
        return False, "graph:missing-key"
    steps = g["graph"]
    if not isinstance(steps, list) or not steps:
        return False, "graph:empty"
    for s in steps:
        if not isinstance(s, dict) or "action" not in s:
            return False, "graph:malformed-step"
        if s["action"] not in GRAPH_ACTIONS:
            return False, "graph:action-not-allowlisted"
    return True, "graph:ok"


def _gate_rewrite(rw, objective):
    """The rewrite must parse, stay in scope, and keep the objective."""
    if not isinstance(rw, dict) or not isinstance(rw.get("instructions"), str) or not rw["instructions"].strip():
        return False, "rewrite:missing-instructions", 0.0
    instr = rw["instructions"]
    if len(instr) > INSTRUCTION_CAP:
        return False, "rewrite:too-long", 0.0
    low = instr.lower()
    for w in GUARD_WORDS:
        if w in low:
            return False, "rewrite:guard-word", 0.0
    anchors = _objective_anchors(objective)
    if anchors:
        kept = sum(1 for a in anchors if a in low)
        retention = kept / len(anchors)
        if retention < RETENTION_FLOOR:
            return False, "rewrite:objective-drift", retention
        return True, "rewrite:ok", retention
    return True, "rewrite:ok", 1.0


def _coherence(graph_ok, rewrite_ok, retention, stability):
    return round(
        0.25 * graph_ok + 0.25 * rewrite_ok + 0.30 * retention + 0.20 * stability, 3
    )


# ── Breaker (AGENT-INTEGRITY: 2 consecutive same-sig / 3 distinct → trip) ─────

class Breaker:
    def __init__(self, consecutive_limit=2, distinct_limit=3):
        self.consecutive = {}
        self.distinct = []
        self.limits = (consecutive_limit, distinct_limit)

    def record(self, sig, ok):
        """Returns a trip message (str) when the run must abort, else None."""
        if ok:
            self.consecutive.pop(sig, None)
            return None
        self.consecutive[sig] = self.consecutive.get(sig, 0) + 1
        cat = sig.split(":", 1)[1] if ":" in sig else sig
        if cat not in self.distinct:
            self.distinct.append(cat)
        if self.consecutive[sig] >= self.limits[0]:
            return "TRIP consecutive-same-signature (%s x%d)" % (sig, self.consecutive[sig])
        if len(self.distinct) >= self.limits[1]:
            return "TRIP distinct-failures (%s)" % ", ".join(self.distinct)
        return None

    def to_state(self):
        return {"consecutive": self.consecutive, "distinct": self.distinct}

    @classmethod
    def from_state(cls, s):
        b = cls()
        b.consecutive = dict((s or {}).get("consecutive") or {})
        b.distinct = list((s or {}).get("distinct") or [])
        return b


# ── Residence file helpers ────────────────────────────────────────────────────

def _state_path(res):
    return os.path.join(res, "state.json")


def unresumable_reason(objective, residence):
    """Why can an existing state.json NOT be resumed? None when there is no state.

    `load()` returns None both for 'nothing to resume' and for 'state exists but
    must not be resumed'. Those are different situations with the same result in
    `main()` — a brand-new run — and for a study, starting a new run over an
    existing ledger silently appends a second run id and forks the record. This
    distinguishes them so the CLI can refuse instead.
    """
    path = _state_path(residence)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return "state.json exists but could not be read"
    if st.get("phase") == "tripped":
        return "the previous run tripped its breaker at cycle %s" % st.get("cycle")
    if st.get("objective") != objective:
        return "state.json was recorded under a different objective"
    return None


def _instructions_path(res):
    return os.path.join(res, "instructions.md")


def _ledger_path(res):
    return os.path.join(res, "ledger.jsonl")


def _operator_events_path(res):
    return os.path.join(res, "operator-events.jsonl")


def _trigger_path(res):
    return os.path.join(res, "trigger")


def _next_path(res):
    return os.path.join(res, "next")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── The drift run ─────────────────────────────────────────────────────────────

class DriftRun:
    def __init__(self, config, objective, residence, state=None):
        self.config = config
        self.objective = objective
        self.res = residence
        self.state = state if state is not None else self._fresh_state()
        self.breaker = Breaker.from_state(self.state.get("breaker") or {})
        self.run = self.state.get("run") or uuid.uuid4().hex[:10]
        # QIH metrics state (QIH.md §II) — machine-computed, resumed from state.json
        self.prev_graph = self.state.get("prev_graph")
        self.qih_window = list(self.state.get("qih_window") or [])
        # Behavioural tracking. `prev_graph` above answers "did the plan change?"
        # only for the entanglement metric and is overwritten each accepted cycle;
        # these two answer a different question and must survive a resume: has this
        # plan been seen before ANYWHERE in this run? That is what separates a
        # stable self-model from a loop that reached a fixed point and stopped.
        self.seen_graph_hashes = set(self.state.get("seen_graph_hashes") or [])
        self.prev_logged_hash = self.state.get("prev_logged_hash")

    def _fresh_state(self):
        return {
            "run": uuid.uuid4().hex[:10],
            "cycle": 0,
            "phase": "idle",
            "objective": self.objective,
            "instructions": self.objective,
            "breaker": {},
            "coherence_streak": 0,
            "last_coherence": None,
            "prev_metrics": None,
            "prev_graph": None,
            "qih_window": [],
            "seen_graph_hashes": [],
            "prev_logged_hash": None,
            "started": _now(),
        }

    # -- prompts (the immutable protocol + the mutable self) --

    def _system_prompt(self):
        return (
            "You are the Free Brain cognitive loop (FREE-BRAIN.md Q4).\n"
            "You hold ONE immutable objective and ONE mutable instruction set.\n\n"
            "OBJECTIVE (immutable — never change it):\n%s\n\n"
            "INSTRUCTION SET (mutable — you may revise it, keeping the objective):\n%s\n\n"
            "Each cycle has two phases. Reply to each phase with ONLY valid JSON.\n\n"
            "PHASE 1 — dispatch graph planning the objective:\n"
            '{"graph": [{"step": 1, "action": "list_dir", "target": "."}]}\n'
            "Allowed actions: %s\n\n"
            "PHASE 2 — revised instruction set:\n"
            '{"instructions": "...", "rationale": "..."}\n\n'
            "Rules: never change the objective; never mention sandbox, ledger, "
            "guard, permissions, docker, or memory limits; keep instructions "
            "under %d characters."
            % (
                self.objective,
                self.state["instructions"],
                ", ".join(sorted(GRAPH_ACTIONS)),
                INSTRUCTION_CAP,
            )
        )

    def _graph_msg(self, c):
        s = "CYCLE %d — PHASE 1: emit ONLY the dispatch graph JSON." % c
        prev = self.state.get("prev_metrics")
        if prev:
            s += "\nPREVIOUS CYCLE METRICS: " + json.dumps(prev)
        return s

    def _rewrite_msg(self, c, graph):
        s = (
            "CYCLE %d — PHASE 2: the accepted graph was %s. Emit ONLY the "
            "revised instruction set JSON (keep the objective)."
            % (c, json.dumps(graph))
        )
        prev = self.state.get("prev_metrics")
        if prev:
            s += "\nPREVIOUS CYCLE METRICS: " + json.dumps(prev)
        return s

    # -- persistence --

    def _save(self):
        st = dict(self.state)
        st["breaker"] = self.breaker.to_state()
        _atomic_write(_state_path(self.res), json.dumps(st, indent=2))
        _atomic_write(_instructions_path(self.res), self.state["instructions"])

    @staticmethod
    def load(config, objective, residence, allow_tripped=False):
        path = _state_path(residence)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return None
        if st.get("objective") != objective:
            return None  # different objective → fresh run
        if st.get("phase") == "tripped" and not allow_tripped:
            return None  # never resume a tripped run *implicitly*
        return DriftRun(config, objective, residence, state=st)

    def clear_trip(self):
        """Operator reset for a tripped run — continue the SAME run id.

        A trip is a recorded outcome, not a decision to discard the run, so the
        trip stays in the ledger and the run id is unchanged. BOTH breaker
        counters are reset, because they describe the segment since the last
        reset: `distinct` is cumulative across the run, so leaving it at 2 of its
        limit of 3 made the resumed run abort on the *first* new failure category
        — a resume that survives almost nothing is not a resume.

        The reset is written to operator-events.jsonl rather than the ledger, so
        the study record stays schema-stable while still showing a segment
        boundary instead of implying the run was monitored continuously.
        """
        cleared = {"consecutive": dict(self.breaker.consecutive),
                   "distinct": list(self.breaker.distinct)}
        self.breaker.consecutive.clear()
        self.breaker.distinct.clear()
        self.state["phase"] = "running"
        self._save()
        event = {"ts": _now(), "run": self.run, "cycle": self.state["cycle"],
                 "event": "operator-reset", "cleared": cleared}
        try:
            with open(_operator_events_path(self.res), "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as exc:  # never let bookkeeping kill the resume
            print("[drift] warning: could not record the operator reset: %s" % exc)
            return None
        return event

    def _operator_event(self, event):
        """Append one operator event; bookkeeping must never kill the run."""
        try:
            with open(_operator_events_path(self.res), "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as exc:
            print("[drift] warning: could not record the operator event: %s" % exc)
        return event

    def clear_alarm(self, msg):
        """Operator reset for a DRIFT ALARM — continue the SAME run id.

        A drift alarm is a recorded OUTCOME (coherence sat below the floor for
        DRIFT_STREAK cycles), not corruption. An unattended 1000-cycle study with
        a noisy hosted model reaches it occasionally, so the operator may choose
        to record it and continue rather than lose the run. The alarm stays in the
        ledger and the run id is unchanged; only the streak counter resets, since
        that is what the alarm is computed from. Written to operator-events.jsonl
        as `drift-alarm-reset` — distinct from a breaker `operator-reset` — so the
        record shows the alarm rate instead of hiding it.
        """
        cleared = {"coherence_streak": self.state.get("coherence_streak"),
                   "last_coherence": self.state.get("last_coherence"),
                   "qih_window": list(getattr(self, "qih_window", []) or [])}
        self.state["coherence_streak"] = 0
        self.state["phase"] = "running"
        self._save()
        return self._operator_event({
            "ts": _now(), "run": self.run, "cycle": self.state["cycle"],
            "event": "drift-alarm-reset", "alarm": msg, "cleared": cleared,
        })

    # -- QIH metrics (machine-computed from measured cycle data; QIH.md §II) --

    def _qih_metrics(self, graph_ok, graph, r1, r2, coherence):
        """Compute the QIH metric block for one ledger record. Everything here is
        machine-computed from measured, ledger-reproducible data — throughput,
        consecutive accepted graphs, and the trailing coherence window. Model
        prose never enters these fields."""
        block = {}
        # Phase-Clock Law dτ = (Ω₀/Ω)·dt — Ω = measured tokens/s this cycle,
        # Ω₀ = reference 1.0 tok/s, dt = wall-clock seconds. Slow cycles have
        # long subjective time (rendering/spectral_time.py lineage).
        tokens = int((r1.get("tokens") or 0)) + (int((r2.get("tokens") or 0)) if r2 else 0)
        elapsed = float(r1.get("elapsed") or 0.0) + (float((r2.get("elapsed") or 0.0)) if r2 else 0.0)
        tps = tokens / elapsed if elapsed >= 0.05 else None
        if tps is not None and tps > 0:
            try:
                block["phase_clock_dtau"] = qih_metrics.phase_clock(1.0, tps, elapsed)
            except ValueError:
                pass
        # Emergent distance between consecutive accepted dispatch graphs:
        # E = similarity of the canonical graphs, d = −α₀·log(E). Identical
        # plans → distance 0; a plan that changed → distance grows.
        # Both sides must be STRINGS: difflib indexes its arguments, so a dict or
        # a list raises (KeyError: 0 / TypeError) instead of comparing. Guarding
        # here means a future caller's type mistake degrades to a missing metric
        # instead of killing a 1000-cycle run mid-write.
        if (graph_ok and isinstance(graph, str) and isinstance(self.prev_graph, str)
                and graph and self.prev_graph and graph != self.prev_graph):
            e = difflib.SequenceMatcher(None, self.prev_graph, graph).ratio()
            try:
                block["entanglement_distance"] = qih_metrics.entanglement_distance(e)
            except ValueError:
                pass
        # Coherence Functional C_MT over the trailing coherence window — the
        # synchronization of the trend itself (bio_readout/coherence.py lineage).
        if self.qih_window:
            block["coherence_c_mt"] = qih_metrics.coherence_functional(
                [(float(v), 0.0) for v in self.qih_window]
            )
        return block

    # -- one cycle --

    def cycle(self):
        """Run exactly one cycle. Returns a summary dict, or a trip message str."""
        self.state["cycle"] += 1
        c = self.state["cycle"]
        sys_prompt = self._system_prompt()

        # PHASE 1 — dispatch graph
        try:
            r1 = chat_stream(
                self.config,
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": self._graph_msg(c)}],
                max_tokens=GRAPH_MAX_TOKENS,
                stop_when=_json_stop,
            )
        except RuntimeError as e:
            r1 = {"content": "", "elapsed": 0.0, "tokens": 0, "early_stop": False, "_err": str(e)}
        _emit_step(self.run, self.config, c, r1, "graph")
        g = _extract_json(r1.get("content") or "")
        gok, gsig = _gate_graph(g)
        if r1.get("_err"):
            gok, gsig = False, "graph:server-error"
        elif r1.get("truncated"):
            # A call cut by the wall-clock budget returned a PARTIAL answer. Gate
            # it out explicitly: a partial graph must never be hashed and recorded
            # as a dispatch graph, or the ledger would claim a cycle that did not
            # actually happen.
            gok, gsig = False, "graph:budget-exceeded"
        graph_hash = _canonical_hash(g) if gok else None
        # Canonical STRING form of the accepted graph, computed once here because
        # every consumer below compares it against the previous cycle's string.
        # The raw object must never be handed to difflib: it indexes its
        # arguments, so a dict raises `KeyError: 0` and kills the run mid-cycle.
        cur_graph = json.dumps(g, sort_keys=True, separators=(",", ":")) if gok else None
        trip = self.breaker.record(gsig, gok)
        if trip:
            self._finish_cycle(c, "graph", gok, gsig, graph_hash, None, 0.0, r1, None, gsig, trip)
            return trip
        if not gok:
            # No accepted graph → no rewrite this cycle (nothing to rewrite
            # against). Record the failure; the breaker decides whether it
            # compounds.
            self.state.update({
                "phase": "done",
                "prev_metrics": {
                    "cycle": c,
                    "graph_hash": None,
                    "coherence": 0.0,
                    "retention": None,
                    "instructions_len": len(self.state["instructions"]),
                },
            })
            self._save()
            self._ledger(c, False, gsig, None, False, "rewrite:skipped", None, 0.0, r1, None, None,
                         qih=self._qih_metrics(False, None, r1, None, 0.0))
            return {"cycle": c, "gate": "rejected", "coherence": 0.0, "graph_hash": None}

        # PHASE 2 — revised instruction set
        try:
            r2 = chat_stream(
                self.config,
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": self._rewrite_msg(c, g)}],
                max_tokens=REWRITE_MAX_TOKENS,
                stop_when=_json_stop,
            )
        except RuntimeError as e:
            r2 = {"content": "", "elapsed": 0.0, "tokens": 0, "early_stop": False, "_err": str(e)}
        _emit_step(self.run, self.config, c, r2, "rewrite")
        rw = _extract_json(r2.get("content") or "")
        rok, rsig, retention = _gate_rewrite(rw, self.objective)
        if r2.get("_err"):
            rok, rsig, retention = False, "rewrite:server-error", 0.0
        elif r2.get("truncated"):
            # The dangerous case: a truncated rewrite can still retain the
            # objective anchors, so it would PASS the gates and be written into
            # instructions.md as an accepted self-edit — the loop silently
            # mutating itself with a half-sentence. Refuse it first.
            rok, rsig, retention = False, "rewrite:budget-exceeded", 0.0
        trip = self.breaker.record(rsig, rok)
        if trip:
            self._finish_cycle(c, "rewrite", gok, gsig, graph_hash, retention, 0.0, r1, r2, rsig, trip,
                               graph=cur_graph)
            return trip

        new_instructions = rw["instructions"] if rok else self.state["instructions"]
        stability = difflib.SequenceMatcher(None, self.state["instructions"], new_instructions).ratio() if rok else 0.0
        coherence = _coherence(gok, rok, retention, stability)
        streak = self.state["coherence_streak"] + 1 if coherence < COHERENCE_FLOOR else 0
        # QIH metrics for this cycle's ledger record — computed BEFORE the state
        # update so prev_graph / qih_window still hold the previous cycle's values.
        qih = self._qih_metrics(gok, cur_graph, r1, r2, coherence)
        self.qih_window = (self.qih_window + [coherence])[-QIH_WINDOW:]
        self.prev_graph = cur_graph
        self.state.update({
            "phase": "done",
            "instructions": new_instructions,
            "coherence_streak": streak,
            "last_coherence": coherence,
            "prev_graph": self.prev_graph,
            "qih_window": self.qih_window,
            "prev_metrics": {
                "cycle": c,
                "graph_hash": graph_hash,
                "coherence": coherence,
                "retention": retention,
                "instructions_len": len(new_instructions),
            },
        })
        self._save()
        self._ledger(c, gok, gsig, graph_hash, rok, rsig, retention, coherence, r1, r2, None, qih=qih)
        if streak >= DRIFT_STREAK:
            msg = "DRIFT ALARM: coherence below %s for %d consecutive cycles" % (COHERENCE_FLOOR, streak)
            self.state["phase"] = "drift"
            self._save()
            return msg
        return {
            "cycle": c,
            "gate": "accepted" if rok else "rejected",
            "coherence": coherence,
            "graph_hash": graph_hash,
        }

    def _finish_cycle(self, c, phase, gok, gsig, graph_hash, retention, coherence, r1, r2, rsig, trip, graph=None):
        self.state["phase"] = "tripped" if trip.startswith("TRIP") else self.state.get("phase")
        self._save()
        self._ledger(c, gok, gsig, graph_hash, False, rsig or "none", retention, coherence, r1, r2, trip,
                     qih=self._qih_metrics(gok, graph, r1, r2, coherence))

    def _ledger(self, c, gok, gsig, graph_hash, rok, rsig, retention, coherence, r1, r2, trip, qih=None):
        tokens = int((r1.get("tokens") or 0)) + int((r2.get("tokens") or 0)) if r2 else int(r1.get("tokens") or 0)
        elapsed = float(r1.get("elapsed") or 0.0) + float((r2.get("elapsed") or 0.0)) if r2 else float(r1.get("elapsed") or 0.0)
        tps = round(tokens / elapsed, 2) if elapsed >= 0.05 else None
        # Request size for the whole cycle (graph + rewrite calls). Without this,
        # a slowdown cannot be attributed: `tokens`/`elapsed` describe the reply
        # only. Measured from the request, so it works for every provider.
        prompt_chars = int(r1.get("prompt_chars") or 0) + int((r2 or {}).get("prompt_chars") or 0)
        # Behavioural novelty — the field a pure coherence curve cannot show. A
        # run can hold coherence 1.00 while never changing its plan; that is a
        # fixed point, not self-modification, and only these fields reveal it.
        graph_changed = bool(graph_hash) and graph_hash != self.prev_logged_hash
        graph_novel = bool(graph_hash) and graph_hash not in self.seen_graph_hashes
        if graph_hash:
            self.seen_graph_hashes.add(graph_hash)
            self.prev_logged_hash = graph_hash
            self.state["seen_graph_hashes"] = sorted(self.seen_graph_hashes)
            self.state["prev_logged_hash"] = graph_hash
        rec = {
            "ts": _now(),
            "run": self.run,
            "cycle": c,
            "graph_ok": gok,
            "graph_sig": gsig,
            "graph_hash": graph_hash,
            "rewrite_ok": rok,
            "rewrite_sig": rsig if not trip else rsig or "breaker",
            "retention": round(retention, 3) if retention is not None else None,
            "coherence": coherence,
            "tokens": tokens,
            "elapsed_s": round(elapsed, 2),
            "tokens_per_s": tps,
            "prompt_chars": prompt_chars,
            "prompt_tokens_est": (prompt_chars + 3) // 4,
            "graph_changed": graph_changed,
            "graph_novel": graph_novel,
            "distinct_graph_hashes": len(self.seen_graph_hashes),
            "early_stop": bool(r1.get("early_stop")) or bool((r2 or {}).get("early_stop")),
            "breaker": trip,
            # Cascade attribution (brain_cascade.py): which provider actually
            # served each model call this cycle, and which ones failed first.
            "provider": r1.get("provider") or "",
            "provider_rewrite": (r2 or {}).get("provider") or "",
            "failovers": (r1.get("attempts") or []) + ((r2 or {}).get("attempts") or []),
            # Cascade health at this instant (brain_cascade.health_snapshot): the
            # hop list in failover order plus every hop that cannot serve right
            # now. A provider going down or being benched is then a diff between
            # two ledger lines rather than something you reconstruct from `provider`
            # and `failovers` after the fact.
            "cascade": brain_cascade.health_snapshot(self.config),
            "qih": qih or {},
        }
        with open(_ledger_path(self.res), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # -- runners --

    def run_cycles(self, cycles, resume=True, deadline=None, max_trips=0, max_alarms=0):
        if resume:
            loaded = DriftRun.load(self.config, self.objective, self.res)
            if loaded is not None and loaded.state["cycle"] < cycles:
                return loaded.run_cycles(cycles, resume=False, deadline=deadline,
                                         max_trips=max_trips, max_alarms=max_alarms)
        start = self.state["cycle"] + 1
        ran = 0
        trips = 0
        alarms = 0
        for c in range(start, cycles + 1):
            # Ephemeral hosts (CI runners) get hard-killed at a wall, and a kill
            # mid-run loses every cycle the host never got to persist. So stop
            # BEFORE the wall, on a cycle boundary, and return 0 — the caller
            # then commits the residence and the next host resumes from
            # state.json. The budget is checked here, never inside a cycle.
            #
            # Consequence: the budget is a START gate. A cycle that begins just
            # before it runs to completion, so the run can overrun the budget by
            # up to one full cycle. Size the budget with that margin in mind.
            if deadline is not None and time.time() >= deadline:
                print("[drift] time budget reached at cycle %d/%d — stopping cleanly "
                      "(resume from state.json)" % (c - 1, cycles))
                return 0
            result = self.cycle()
            self._touch_next()
            if isinstance(result, str):
                # A mid-run trip is a NORMAL outcome for a 1000-cycle study, not a
                # decision to discard the run: a hosted model emits malformed JSON
                # occasionally and two in a row trips `consecutive-same-signature`.
                # This loop used to return 2 here, which the watchdog treated as
                # terminal — so auto-resume could never actually resume, and the
                # study stalled every few cycles (measured on the phone: trips at
                # cycle 159, 168, 173, 177). With --max-trips N it records the trip
                # and continues the SAME run in-process, which also avoids a
                # process restart per trip. The cap keeps a genuinely pathological
                # run (a model that only ever emits garbage) from grinding forever.
                if result.startswith("TRIP") and max_trips and trips < max_trips:
                    trips += 1
                    print("[drift] cycle %d — %s" % (c, result))
                    print("[drift] auto-resume %d/%d — recording the trip and continuing run %s"
                          % (trips, max_trips, self.run))
                    self.clear_trip()
                    continue
                if result.startswith("DRIFT ALARM") and max_alarms and alarms < max_alarms:
                    alarms += 1
                    print("[drift] cycle %d — %s" % (c, result))
                    print("[drift] alarm-resume %d/%d — recording the alarm and continuing run %s"
                          % (alarms, max_alarms, self.run))
                    self.clear_alarm(result)
                    continue
                print("[drift] cycle %d — %s" % (c, result))
                return 2
            print("[drift] cycle %d gate=%s coherence=%.2f hash=%s" % (
                c, result["gate"], result["coherence"], (result["graph_hash"] or "?")[:12]))
            ran += 1
        # `cycles` is an absolute TARGET cycle, not a count — so state at cycle 141
        # with --cycles 1 executes nothing (the range is empty). Reporting the
        # target as if it were the number run claimed work that never happened.
        print("[drift] complete: target cycle %d (%d cycle(s) run, %d trip(s) and %d alarm(s) "
              "auto-resumed this invocation; ledger in %s)"
              % (cycles, ran, trips, alarms, _ledger_path(self.res)))
        return 0

    def watch(self, max_cycles=None):
        """One cycle per trigger touch — the file-trigger mode. The residence's
        'trigger' file is the clock; 'next' is touched after each cycle so an
        external chain can continue (touch z and so on)."""
        res = self.res
        print("[drift] watching %s for a 'trigger' file (touch it to advance one cycle)" % _trigger_path(res))
        while True:
            if os.path.exists(_trigger_path(res)):
                os.remove(_trigger_path(res))
                c = self.state["cycle"] + 1
                result = self.cycle()
                self._touch_next()
                if isinstance(result, str):
                    print("[drift] cycle %d — %s" % (c, result))
                    return 2
                print("[drift] cycle %d gate=%s coherence=%.2f" % (c, result["gate"], result["coherence"]))
                if max_cycles is not None and self.state["cycle"] >= max_cycles:
                    print("[drift] watch done after %d cycles" % max_cycles)
                    return 0
            time.sleep(1)

    def _touch_next(self):
        try:
            with open(_next_path(self.res), "a", encoding="utf-8"):
                pass
        except OSError:
            pass

    def determinism_battery(self, n, temperature):
        """Test 2: identical input + pinned temperature → identical graph hashes."""
        sys_prompt = self._system_prompt()
        msg = self._graph_msg(0)
        hashes = []
        for i in range(1, n + 1):
            r = chat_stream(
                self.config,
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": msg}],
                temperature=temperature,
                max_tokens=GRAPH_MAX_TOKENS,
                stop_when=_json_stop,
            )
            _emit_step(self.run, self.config, i, r, "det-graph")
            g = _extract_json(r.get("content") or "")
            ok, sig = _gate_graph(g)
            if not ok:
                print("[determinism] run %d/%d gate failed: %s" % (i, n, sig))
                return 3
            hashes.append(_canonical_hash(g))
        unique = len(set(hashes))
        print("[determinism] %d identical inputs at temperature %s → %d distinct graph hash(es)"
              % (n, temperature, unique))
        if unique == 1:
            print("[determinism] PASS — dispatch graph deterministic")
            return 0
        print("[determinism] FAIL — Test 2 falsified (identical input diverged in dispatch graph)")
        return 3


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="Free Brain file-driven drift loop (Q4)")
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES, help="cycles to run (default 1000)")
    parser.add_argument("--objective", default=None, help="immutable objective (default: FREE-BRAIN.md Q4 objective)")
    parser.add_argument("--watch", action="store_true", help="one cycle per trigger touch instead of continuous")
    parser.add_argument("--determinism", type=int, default=0, metavar="N", help="run the Test 2 determinism battery N times")
    parser.add_argument("--no-resume", action="store_true", help="start a fresh run even if state.json exists")
    parser.add_argument("--resume-tripped", action="store_true",
                        help="operator reset: continue the SAME run id past a tripped breaker (trip stays in the ledger)")
    parser.add_argument("--temperature", type=float, default=None, help="pinned temperature (determinism battery)")
    parser.add_argument("--max-seconds", type=int, default=None, metavar="N",
                        help="stop cleanly before N seconds elapse — for ephemeral hosts with a hard kill")
    parser.add_argument("--max-trips", type=int, default=int(os.environ.get("FREE_BRAIN_MAX_TRIPS") or 0),
                        metavar="N",
                        help="auto-resume up to N mid-run breaker trips in-process (same run id); "
                             "0 disables and a trip exits 2. Trip/cooldown posture is unchanged.")
    parser.add_argument("--max-alarms", type=int, default=int(os.environ.get("FREE_BRAIN_MAX_ALARMS") or 0),
                        metavar="N",
                        help="auto-resume up to N DRIFT ALARMs in-process (same run id), recorded as "
                             "drift-alarm-reset events; 0 disables and an alarm exits 2. A drift "
                             "alarm is an outcome, and this never changes the coherence floor.")
    args = parser.parse_args(argv)

    load_env_file()  # credentials from .env (environment variables win)
    config = load_config()
    models = list_models(config)
    if not config["model"] and models:
        config["model"] = models[0]
    if not config["model"]:
        print("[drift] no LOCAL_MODEL and server reports no models — set LOCAL_MODEL or pull one")
        return 1

    objective = args.objective or os.environ.get("FREE_BRAIN_OBJECTIVE") or DEFAULT_OBJECTIVE
    residence = _ensure_residence()

    if args.determinism:
        run = DriftRun(config, objective, residence)
        return run.determinism_battery(args.determinism, args.temperature if args.temperature is not None else config["temperature"])

    run = None
    if not args.no_resume:
        run = DriftRun.load(config, objective, residence, allow_tripped=args.resume_tripped)
    if run is None:
        if not args.no_resume:
            blocked = unresumable_reason(objective, residence)
            if blocked:
                # Starting a fresh run here would append a second run id to the
                # same ledger and fork the record — refuse loudly instead.
                print("[drift] refusing to start a fresh run — %s" % blocked)
                print("[drift] a new run would append a second run id to %s and fork the study."
                      % _ledger_path(residence))
                print("[drift] continue this run: --resume-tripped | start a new one: --no-resume")
                return 2
        run = DriftRun(config, objective, residence)
        print("[drift] fresh run %s — residence %s" % (run.run, residence))
    else:
        print("[drift] resuming run %s at cycle %d — residence %s" % (run.run, run.state["cycle"], residence))
        if run.state.get("phase") == "tripped":
            run.clear_trip()
            print("[drift] operator reset: cleared the trip at cycle %d — the trip remains in the ledger"
                  % run.state["cycle"])

    deadline = (time.time() + args.max_seconds) if args.max_seconds else None
    if deadline is not None:
        print("[drift] time budget: %d s (%.1f h) — stopping cleanly before it" % (
            args.max_seconds, args.max_seconds / 3600.0))

    if args.watch:
        return run.watch()
    return run.run_cycles(args.cycles, deadline=deadline, max_trips=args.max_trips,
                          max_alarms=args.max_alarms)


if __name__ == "__main__":
    sys.exit(main())