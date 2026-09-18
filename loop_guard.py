"""loop_guard.py — instrumentation, limit detection, and self-improvement
triggers for the autonomy decision loop (`agent_runtime.run_goal`).

WHY THIS EXISTS
---------------
The loop as first written was a bounded `for step in range(max_steps)` with
exactly two exits: the model said `FINAL:`, or the budget ran out. A measured
audit (`loop_audit.py`) found what that costs — every one of these pathologies
ended in the same bare `[gate-failed] reason=step budget exceeded`:

  * the model repeating one identical tool call forever,
  * the model inventing tool names that do not exist,
  * every tool call failing and the model retrying anyway,
  * the conversation outgrowing the context window,

i.e. four different faults were reported as one, and none of them were
*detected* — the loop had no idea it was stuck, it just kept paying for steps
until the counter ran out. A caller reading "budget exceeded" cannot tell whether
to raise the budget or fix the model, and neither can the agent.

WHAT THIS ADDS
--------------
1. **Per-step observation.** Every step records which tools were called, whether
   each succeeded, and how large the conversation has grown.
2. **Limit detection with named reasons.** `no_progress`, `tool_error_storm`,
   `unknown_tool_storm`, `context_over_budget`, `budget_exhausted`,
   `no_tool_use`, `wall_clock_exceeded`, `model_unavailable`. A stuck loop stops
   *when it is detected*, not when the counter happens to run out.
3. **Context compaction instead of silent overflow.** When the conversation
   exceeds its budget the oldest tool outputs are elided (keeping the first and
   the most recent), and the elision is recorded. Truncating the middle loses
   less than a provider 400 at step 7.
4. **Self-improvement triggers.** A detection at `high`/`critical` severity emits
   a DETECT → RESEARCH → DESIGN → IMPLEMENT → TEST → REGISTER record to
   `SELF_IMPROVEMENT_LOG.md` and a structured JSONL record to the residence
   ledger, naming the failing stage and the lever that could move it. The loop
   reports its own faults instead of relying on a human to notice.

Thresholds live in `loop_guard.json` (registered), so a measured tuning can
change the loop's sensitivity without editing code — the same registration
pattern the RAG pipeline uses.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os

# ── Thresholds ────────────────────────────────────────────────────────────────

# Keys whose registered value must come from a fixed set. Registered config is
# validated against these, not merely type-checked.
ENUMS = {
    # How repeated tool calls are judged — see `_repeat_diagnosis`.
    # "streak": consecutive identical steps whose results are also identical.
    # "cumulative": the original rule — any signature seen more than
    #   `repeat_limit` times over the whole run stops it. Kept switchable because
    #   loop_audit.py measures both against the same pathologies (a rule change
    #   should be revertible from the registered config, not only by editing
    #   code), and because it is the rule the audit found false-positiving.
    "repeat_mode": ("streak", "cumulative"),
}

DEFAULT_THRESHOLDS = {
    # Identical (tool, argument) signature seen this many times — in a row under
    # the `streak` rule (the default): the model is repeating itself, not
    # exploring.
    "repeat_limit": 2,
    # See ENUMS. `streak` is the default because the audit measured `cumulative`
    # flagging legitimate revisits as `no_progress`.
    "repeat_mode": "streak",
    # Consecutive steps in which every executed tool call failed.
    "tool_error_streak": 3,
    # Unknown-tool directives tolerated before the run is stopped. One is a
    # typo; several mean the model has not understood the tool contract.
    "unknown_tool_limit": 2,
    # Conversation size (sum of message content) above which older tool output
    # is elided.
    "context_budget_chars": 24000,
    "context_keep_recent": 2,
    # Total wall-clock ceiling for the whole run, independent of step count.
    "max_wall_clock_s": 600,
    # Tool results this long are elided when compacting.
    "compact_min_chars": 400,
    # ── goal-directed loop (autonomy.py) ─────────────────────────────────────
    # Whole-run token ceiling across every call (plan, steps, final). 0 disables
    # it. A step cap alone cannot bound cost when the planner is verbose.
    "task_token_budget": 0,
    # Times a malformed plan is sent back for repair before `plan_invalid`.
    "plan_repair_attempts": 1,
    # Accepted REPLANs before `replan_storm`.
    "max_replans": 2,
    # Off-plan actions tolerated (recorded, not fatal) before `plan_divergence`.
    "plan_divergence_limit": 1,
    # Replies that neither act nor claim completion (a model re-emitting its own
    # plan) tolerated before `no_action`. See REASONS["no_action"].
    "no_action_limit": 2,
    # Empty replies retried before `empty_response`. A provider that spends its
    # token budget without emitting content, or a dropped stream, produces one; the
    # first is usually transient and killing the run throws away a paid-for plan.
    "empty_response_retries": 1,
}

REGISTERED_NAME = "loop_guard.json"
DIAGNOSIS_LEDGER = os.path.join("freebrain-residence", "loop-diagnoses.jsonl")
SELF_IMPROVEMENT_LOG = "SELF_IMPROVEMENT_LOG.md"

# Reason codes, with the stage each one implicates and the lever that moves it.
# `stage` reuses the pipeline vocabulary so a diagnosis can be routed the same
# way a RAG diagnosis is.
REASONS = {
    "no_progress": {
        "severity": "high",
        "stage": "planning",
        "meaning": "the model issued the same tool call repeatedly without changing its approach",
        "lever": "system prompt (requires a distinct next step), repeat_limit",
        "hypothesis": "the model has no exit condition for the information it already has",
    },
    "tool_error_storm": {
        "severity": "high",
        "stage": "tool-use",
        "meaning": "every tool call failed for several consecutive steps",
        "lever": "tool argument validation, error text returned to the model, tool_error_streak",
        "hypothesis": "tool failures are returned in a shape the model cannot act on",
    },
    "unknown_tool_storm": {
        "severity": "high",
        "stage": "tool-use",
        "meaning": "the model repeatedly requested tools that do not exist",
        "lever": "system prompt tool list, unknown_tool_limit",
        "hypothesis": "the model is guessing tool names rather than reading the allowlist",
    },
    "context_over_budget": {
        "severity": "medium",
        "stage": "context",
        "meaning": "the conversation exceeded the context budget and older tool output was elided",
        "lever": "context_budget_chars, context_keep_recent, tool output caps",
        "hypothesis": "tool output is too verbose to keep for the length of a run",
    },
    "wall_clock_exceeded": {
        "severity": "medium",
        "stage": "execution",
        "meaning": "the run exceeded its total wall-clock ceiling",
        "lever": "timeouts per provider, max_wall_clock_s",
        "hypothesis": "per-step latency is far higher than assumed",
    },
    "no_tool_use": {
        "severity": "medium",
        "stage": "planning",
        "meaning": "the model answered without inspecting anything",
        "lever": "system prompt, goal phrasing, persona",
        "hypothesis": "the task was answerable from memory, or the model declined to act",
    },
    "budget_exhausted": {
        "severity": "medium",
        "stage": "planning",
        "meaning": "the step budget ran out while the model was still making progress",
        "lever": "MAX_STEPS, goal decomposition",
        "hypothesis": "the goal needs more steps than the loop allows, or progress is too slow",
    },
    "model_unavailable": {
        "severity": "critical",
        "stage": "transport",
        "meaning": "the model call raised before any step completed",
        "lever": "brain cascade, LOCAL_MODEL, provider health",
        "hypothesis": "no provider could serve the call",
    },
    "empty_response": {
        "severity": "high",
        "stage": "transport",
        "meaning": "the model returned an empty response",
        "lever": "provider health, max_tokens",
        "hypothesis": "the provider truncated or refused the completion",
    },
    # ── goal-directed loop (autonomy.py) ─────────────────────────────────────
    # These are the faults a reflex loop cannot even name: it has no plan to
    # diverge from and no goal condition to leave unverified.
    "plan_invalid": {
        "severity": "high",
        "stage": "planning",
        "meaning": "the model could not produce a plan whose steps and goal are checkable",
        "lever": "plan prompt examples, plan_repair_attempts, GOAL-CHECK grammar",
        "hypothesis": "the model cannot express a decidable goal, or the plan grammar is too strict",
    },
    "unverified_completion": {
        "severity": "critical",
        "stage": "verification",
        "meaning": "the model claimed the goal was met but the goal condition did not hold over verified evidence",
        "lever": "goal condition strength, evidence promotion, system prompt",
        "hypothesis": "the model is asserting completion rather than collecting the evidence that proves it",
    },
    "plan_divergence": {
        "severity": "high",
        "stage": "planning",
        "meaning": "the model repeatedly acted off-plan instead of executing or replanning",
        "lever": "plan prompt, plan_divergence_limit, step directive wording",
        "hypothesis": "the model is following its own agenda rather than the accepted plan",
    },
    "replan_storm": {
        "severity": "high",
        "stage": "planning",
        "meaning": "the model kept replacing the plan without making progress",
        "lever": "max_replans, plan repair, goal decomposition",
        "hypothesis": "the plan space cannot reach the goal, so every plan is discarded in turn",
    },
    "token_budget_exceeded": {
        "severity": "medium",
        "stage": "execution",
        "meaning": "the run exceeded its whole-task token ceiling",
        "lever": "task_token_budget, prompt size, step token caps",
        "hypothesis": "the run is spending tokens on planning or repetition rather than progress",
    },
    "no_action": {
        "severity": "high",
        "stage": "planning",
        "meaning": "the model repeatedly replied without acting and without claiming completion",
        "lever": "step directive wording, plan prompt examples, no_action_limit",
        "hypothesis": "the model is re-emitting its plan instead of executing a step",
    },
    "empty_answer": {
        "severity": "high",
        "stage": "verification",
        "meaning": "the goal condition held but the completion carried no answer text",
        "lever": "answer format in the system prompt, max_tokens, provider health",
        "hypothesis": "the model emitted the FINAL marker and stopped, or the provider truncated the answer",
    },
    # Recorded via `note()`, never a stop: the retry is the remedy, and the note is
    # what makes the retry visible in the ledger instead of silent.
    "transient_empty_response": {
        "severity": "medium",
        "stage": "transport",
        "meaning": "a reply came back empty and the call was retried",
        "lever": "empty_response_retries, provider health, cascade order",
        "hypothesis": "the provider returned an empty completion for a reply the model did produce",
    },
    # Recorded via `note()`, never a stop: an expectation that failed is a
    # finding the model may still correct by replanning. It becomes a stop only
    # through `unverified_completion` if the run ends without the goal holding.
    "unmet_expectation": {
        "severity": "medium",
        "stage": "verification",
        "meaning": "a step ran but its declared expectation did not hold in the output",
        "lever": "expectation phrasing, tool argument, plan quality",
        "hypothesis": "the expectation was guessed rather than derived from what the tool returns",
    },
}


def config_path(env=None):
    env = env if env is not None else os.environ
    return env.get("LOOP_GUARD_CONFIG") or REGISTERED_NAME


def active_thresholds(env=None):
    """Defaults overlaid with any registered tuning. Unknown keys are ignored so
    a stale registration cannot inject junk; a corrupt file degrades to the
    defaults rather than breaking every run."""
    out = dict(DEFAULT_THRESHOLDS)
    path = config_path(env)
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return out
    params = payload.get("params", payload) if isinstance(payload, dict) else {}
    for key, value in params.items():
        default = DEFAULT_THRESHOLDS.get(key)
        # `isinstance(True, int)` is True, so bools are rejected explicitly — a
        # boolean in a numeric knob would silently become 1 or 0.
        if default is None or isinstance(value, bool):
            continue
        # Type-match against the default so a stale or hand-edited registration
        # cannot inject a value the loop would then do arithmetic on. Exact match
        # rather than numeric coercion: a float where an int is registered is a
        # mistake worth falling back to the default over.
        if not isinstance(value, type(default)):
            continue
        allowed = ENUMS.get(key)
        if allowed and value not in allowed:
            continue
        out[key] = value
    return out


def register_thresholds(params, source="loop_audit.py", path=None, env=None):
    path = path or config_path(env)
    payload = {
        "registered": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "params": {k: v for k, v in params.items() if k in DEFAULT_THRESHOLDS},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
    return path


# ── The failure protocol ──────────────────────────────────────────────────────

# ONE canonical marker, constructed in ONE place.
#
# Tools previously reported failure two ways: the action tools emitted
# '[gate-failed]', the read-only file tools emitted '[error]'. That is not a
# cosmetic difference — it is what hid an error storm from the first detector. A
# model looping on `read_file` of a missing path was reporting a failure the
# guard did not count, so it read as a model making progress until the step
# budget ran out, and the diagnosis pointed at the budget instead of the tool.
#
# Emission is now strictly canonical: `fail()` is the only constructor, and a
# test enumerates every registered tool to prove it. Detection stays deliberately
# liberal — a legacy result already in a ledger, or one returned by a tool this
# module does not own, must still count as a failure. Conservative in what we
# emit, liberal in what we accept; that asymmetry is the point.
FAILED_PREFIX = "[gate-failed]"
LEGACY_FAILURE_PREFIXES = ("[error]",)
FAILURE_PREFIXES = (FAILED_PREFIX,) + LEGACY_FAILURE_PREFIXES


def fail(reason):
    """The single constructor for a tool-failure string."""
    return "%s %s" % (FAILED_PREFIX, reason)


def result_failed(result):
    text = (result or "").lstrip()
    return text.startswith(FAILURE_PREFIXES)


def result_signature(result):
    """Identity of a tool *result*, for repeat detection.

    Two identical calls returning identical bytes are a no-op. The same call
    returning different bytes is evidence something changed in between — a file
    that was rewritten, a ledger that grew, the drift loop's own state.json —
    and must not be reported as the model being stuck.
    """
    return hashlib.blake2b(str(result or "").encode("utf-8", "replace"),
                           digest_size=8).hexdigest()


def _norm_call(call):
    """Accept a 3-tuple (name, arg, failed) from a caller that does not track
    results, or a 4-tuple that appends the result signature. A caller without
    result hashes gets the weaker streak rule rather than a crash."""
    if len(call) >= 4:
        return call[0], call[1], bool(call[2]), call[3]
    return call[0], call[1], bool(call[2]), None


def parse_directive(line):
    """Parse one TOOL: directive into (name, arg). Mirrors the loop's own parser
    so the guard observes exactly what the loop executed."""
    rest = line.strip().lstrip("<")
    if "TOOL:" not in rest:
        return None, None
    rest = rest.split("TOOL:", 1)[1].split(">>>", 1)[0].rstrip(">")
    name, _, arg = rest.partition(" ")
    return name.strip(), arg.strip()


def call_signature(name, arg):
    """Stable identity for a tool call, used for repeat detection."""
    return hashlib.blake2b(("%s\x00%s" % (name, arg)).encode("utf-8"),
                           digest_size=8).hexdigest()


def _counts(records):
    """reason -> count, sorted. A summary has to survive `json.dumps` (it is
    embedded in records and ledger lines), so no Counter objects leak out."""
    out = {}
    for r in records:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return dict(sorted(out.items()))


# ── The guard ─────────────────────────────────────────────────────────────────

class LoopGuard:
    """Observes one `run_goal` execution and decides when to stop early.

    Everything the guard needs is passed in; it performs no I/O except the
    optional diagnosis record. The loop stays readable and the guard is testable
    on its own.
    """

    def __init__(self, max_steps, thresholds=None, enabled=True, env=None,
                 emit=None, log_path=None, ledger_path=None):
        """`emit` defaults to OFF.

        A library call must not write to repository files: running the test suite
        appended a fabricated `model_unavailable` record to the real
        SELF_IMPROVEMENT_LOG.md before this default changed. Emission is opt-in —
        the CLI turns it on (`LOOP_GUARD_EMIT=1`), loop_audit.py and the tests
        leave it off, and a caller that wants records passes `emit=True`.
        """
        if emit is None:
            emit = (env if env is not None else os.environ).get("LOOP_GUARD_EMIT", "0") == "1"
        if log_path is None:
            log_path = (env if env is not None else os.environ).get(
                "LOOP_GUARD_LOG", SELF_IMPROVEMENT_LOG) or None
        if ledger_path is None:
            ledger_path = (env if env is not None else os.environ).get(
                "LOOP_GUARD_LEDGER", DIAGNOSIS_LEDGER) or None
        self.max_steps = max_steps
        self.t = dict(thresholds) if thresholds else active_thresholds(env)
        self.enabled = enabled
        self.emit = emit
        self.log_path = log_path
        self.ledger_path = ledger_path
        self.steps = 0
        self.calls = []            # (step, name, arg, failed)
        self.signatures = {}       # signature -> count
        self.unknown_tools = []    # (step, name)
        self.error_streak = 0
        # Streak rule state: the previous step's call signatures and result
        # signatures, and how many consecutive steps have repeated them.
        self.last_step_sigs = None
        self.last_step_results = None
        self.repeat_streak = 0
        self.compactions = 0
        self.tool_steps = 0        # steps that issued at least one tool call
        self.peak_context = 0
        self.started = None
        self.triggers = []         # reason codes emitted as self-improvement triggers
        self.notes = []            # tolerated findings: recorded, never a stop

    # -- helpers -------------------------------------------------------------

    def context_size(self, messages):
        return sum(len(m.get("content") or "") for m in messages)

    def _trip(self, reason, detail):
        """Record a detection. Severity comes from the REASONS registry so the
        trigger policy cannot drift from the reason catalogue."""
        meta = REASONS.get(reason, {})
        return {
            "reason": reason,
            "severity": meta.get("severity", "medium"),
            "stage": meta.get("stage", "unknown"),
            "meaning": meta.get("meaning", ""),
            "lever": meta.get("lever", ""),
            "hypothesis": meta.get("hypothesis", ""),
            "detail": detail,
            "step": self.steps,
        }

    def should_trigger_self_improvement(self, reason):
        return REASONS.get(reason, {}).get("severity") in ("critical", "high")

    # -- observation ---------------------------------------------------------

    def observe(self, step, kind, calls, results, messages, elapsed_s=None):
        """Fold one step's evidence into the guard's state.

        Returns a dict with `action`: "continue", "stop", or "compact". A stop
        carries a fully attributed diagnosis so the loop can report *why*,
        not just that it stopped.
        """
        self.steps = step
        context = self.context_size(messages)
        self.peak_context = max(self.peak_context, context)
        if not self.enabled:
            return {"action": "continue"}

        # 1. Wall clock — a step budget alone cannot bound a run whose steps are
        #    individually slow (each provider call can take minutes).
        if elapsed_s is not None and elapsed_s > self.t["max_wall_clock_s"]:
            d = self._trip("wall_clock_exceeded",
                           {"elapsed_s": round(elapsed_s, 1),
                            "ceiling_s": self.t["max_wall_clock_s"]})
            return self._stop(d)

        # 2. Unknown tools: the model has not understood the contract.
        #    `calls` holds (name, arg, failed) triples — unpack, do not treat the
        #    tuple as the name (doing so made every call look like an unknown
        #    tool and produced a storm on every run: caught by loop_audit.py).
        norm = [_norm_call(c) for c in calls]
        for name, _arg, _failed, _res in norm:
            if name is not None and name not in _tool_names():
                self.unknown_tools.append((step, name))
        if len(self.unknown_tools) >= self.t["unknown_tool_limit"]:
            d = self._trip("unknown_tool_storm",
                           {"unknown": [{"step": s, "tool": n} for s, n in self.unknown_tools]})
            return self._stop(d)

        # 3. Accumulate this step's calls. Decision precedence below is
        #    deliberate: a repeated *failing* call is an error storm (the tool is
        #    unusable), while a repeated *succeeding* call is no progress (the
        #    model is not advancing). Reporting the storm as "no_progress" would
        #    point the fix at the prompt when the real fault is the tool.
        step_sigs = []
        step_results = []
        for name, arg, failed, res_sig in norm:
            sig = call_signature(name, arg)
            self.signatures[sig] = self.signatures.get(sig, 0) + 1
            self.calls.append((step, name, arg, failed))
            step_sigs.append(sig)
            step_results.append(res_sig)
        if calls:
            self.tool_steps += 1
            if all(f for _n, _a, f, _r in norm):
                self.error_streak += 1
            else:
                self.error_streak = 0

        # 4. Error storm first.
        if self.error_streak >= self.t["tool_error_streak"]:
            d = self._trip("tool_error_storm",
                           {"streak": self.error_streak,
                            "recent": [{"step": s, "tool": n, "arg": a}
                                       for s, n, a, _ in self.calls[-self.error_streak:]]})
            return self._stop(d)

        # 5. Repeat without failure: the model got an answer it cannot use and
        #    asked again. Two rules are registered (see `_repeat_diagnosis`);
        #    the streak rule is the default because the cumulative one cannot
        #    tell a stuck loop from a legitimate revisit.
        d = self._repeat_diagnosis(step_sigs, step_results,
                                   [n for n, _a, _f, _r in norm])
        if d is not None:
            return self._stop(d)

        # 6. Context budget: compact rather than let a provider 400 the run, or
        #    silently truncate the model's own history.
        if context > self.t["context_budget_chars"]:
            elided = compact_messages(messages, self.t)
            if elided:
                self.compactions += 1
                return {"action": "compact", "elided": elided,
                        "context_chars": context,
                        "budget": self.t["context_budget_chars"]}
        return {"action": "continue"}

    def _repeat_diagnosis(self, step_sigs, step_results, tool_names):
        """Decide whether this step is a repeated call that is going nowhere.

        Returns a diagnosis dict, or None when the step counts as progress.
        Two rules, because the naive one has a measured false-positive rate:

        * `cumulative` — the original: any (tool, argument) signature seen more
          than `repeat_limit` times *over the whole run* stops the loop. Simple,
          and wrong for a run that legitimately returns to something it already
          read. The audit measured it flagging both `revisit_interleaved` and
          `revisit_changed_result` as `no_progress`.
        * `streak` — consecutive identical steps *whose results are also
          identical*. Same call out, same bytes back, nothing else happening:
          stuck. The same call now returning different bytes is evidence the
          model progressed in between, and the drift loop reading its own
          changing state.json is exactly that case.
        """
        mode = self.t.get("repeat_mode", "streak")
        limit = self.t["repeat_limit"]
        tools = sorted(set(tool_names))

        # A single call issued repeatedly *inside one response* is a fault under
        # either rule: nothing can have changed between duplications.
        for sig in set(step_sigs):
            n = step_sigs.count(sig)
            if n > limit:
                return self._trip("no_progress",
                                  {"rule": "within_step", "count": n,
                                   "repeat_limit": limit, "tools": tools})

        if mode == "cumulative":
            for sig in set(step_sigs):
                if self.signatures[sig] > limit:
                    return self._trip("no_progress",
                                      {"rule": "cumulative",
                                       "count": self.signatures[sig],
                                       "repeat_limit": limit, "tools": tools})
            return None

        results_known = any(r is not None for r in step_results)
        same_calls = (bool(step_sigs)
                      and sorted(step_sigs) == sorted(self.last_step_sigs or []))
        same_results = ((not results_known)
                        or sorted(step_results) == sorted(self.last_step_results or []))
        self.repeat_streak = self.repeat_streak + 1 if (same_calls and same_results) else 0
        self.last_step_sigs = list(step_sigs)
        self.last_step_results = list(step_results)
        if self.repeat_streak >= limit:
            return self._trip("no_progress",
                              {"rule": "streak", "streak": self.repeat_streak,
                               "repeat_limit": limit, "tools": tools,
                               "results_tracked": results_known})
        return None

    def _stop(self, diagnosis):
        if self.should_trigger_self_improvement(diagnosis["reason"]):
            self.triggers.append(diagnosis["reason"])
            # `self.emit` gates the write. Trigger *bookkeeping* always happens so
            # a caller can see what would have been recorded; only the file write
            # is opt-in.
            if self.emit:
                self.emit_self_improvement(diagnosis)
        return {"action": "stop", "diagnosis": diagnosis}

    def step_failed(self, reason, detail=None, step=None):
        """Report a failure detected by the loop itself (transport error, empty
        response), so it lands in the same diagnosis path as guard detections."""
        self.steps = step if step is not None else self.steps
        d = self._trip(reason, detail or {})
        return self._stop(d)

    def note(self, reason, detail=None, step=None):
        """Record a **tolerated** finding: observed and attributed, but not a stop.

        Deliberately not `_trip`: a note never changes the run's outcome and
        never opens a self-improvement cycle. It exists because some findings
        must be visible without being fatal — an expectation that failed while
        the model can still correct course, an off-plan action before the
        divergence limit. Dropping them silently would leave a run that ends
        unverified with no record of *why*, which is the blindness this layer
        was built to remove; escalating them would stop runs that recover.

        Returns the record so a caller can log or assert on it.
        """
        meta = REASONS.get(reason, {})
        record = {
            "reason": reason,
            "severity": meta.get("severity", "medium"),
            "stage": meta.get("stage", "unknown"),
            "meaning": meta.get("meaning", ""),
            "lever": meta.get("lever", ""),
            "hypothesis": meta.get("hypothesis", ""),
            "detail": detail or {},
            "step": self.steps if step is None else step,
            "tolerated": True,
        }
        self.notes.append(record)
        return record

    # -- reporting -----------------------------------------------------------

    def summary(self, outcome, steps_used):
        return {
            "outcome": outcome,
            "steps_used": steps_used,
            "max_steps": self.max_steps,
            "tool_steps": self.tool_steps,
            "distinct_calls": len(self.signatures),
            "calls": len(self.calls),
            "unknown_tools": [n for _, n in self.unknown_tools],
            "compactions": self.compactions,
            "peak_context_chars": self.peak_context,
            "consecutive_tool_errors": self.error_streak,
            "repeat_streak": self.repeat_streak,
            "repeat_mode": self.t.get("repeat_mode", "streak"),
            "self_improvement_triggers": self.triggers,
            "tolerated_notes": _counts(self.notes),
        }

    def failure_line(self, diagnosis, steps_used):
        """Structured, greppable failure string. Every field is machine-readable
        so a caller (or the agent) can branch on the reason without parsing prose."""
        return ("%s reason=%s severity=%s stage=%s step=%d detail=%s"
                % (FAILED_PREFIX, diagnosis["reason"], diagnosis["severity"],
                   diagnosis["stage"], steps_used,
                   json.dumps(diagnosis["detail"], sort_keys=True)))

    # -- self-improvement trigger -------------------------------------------

    def emit_self_improvement(self, diagnosis):
        """Record an **opened** cycle for a fault detected at runtime.

        Never raises: a broken log must not turn a detected fault into a crash.

        Opened, not closed. This method runs the instant a fault is detected and
        has done no RESEARCH, DESIGN, IMPLEMENT, TEST or REGISTER work — so it
        records only those phases that actually happened (detect), the routing
        (stage + lever), and `status: "open"`. An earlier version wrote a full
        six-phase record here, hardcoding `implement: loop_guard.LoopGuard
        applied to agent_runtime.run_goal` and `test: loop_audit.py pathology
        suite` for *every* fault — a machine-written record claiming work that
        had not been done, which is the one thing this log cannot afford. A
        closed cycle is appended separately, by the machinery that did the work
        (`emit_cycle`, loop_audit.py, rag_diagnose.py) and carries measured
        evidence.
        """
        if not self.emit:
            return None
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": "loop_guard",
            "reason": diagnosis["reason"],
            "severity": diagnosis["severity"],
            "stage": diagnosis["stage"],
            "status": "open",
            "detail": diagnosis["detail"],
            "summary": self.summary("failed", self.steps),
            "phases": {
                "detect": "%s (step %d): %s" % (diagnosis["reason"], diagnosis["step"],
                                                diagnosis["meaning"]),
                "routed_to": diagnosis["lever"],
                "hypothesis": diagnosis["hypothesis"],
                "next": ("no RESEARCH/DESIGN/IMPLEMENT/TEST/REGISTER step has run "
                         "for this fault yet; a closed cycle is appended separately "
                         "once one has"),
            },
        }
        if self.ledger_path:
            try:
                d = os.path.dirname(self.ledger_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.ledger_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, sort_keys=True) + "\n")
            except OSError:
                pass
        if self.log_path:
            try:
                _append_log(self.log_path, record,
                            title="autonomy loop fault: `%s`" % record["reason"])
            except OSError:
                pass
        return record

    @staticmethod
    def emit_cycle(record, log_path):
        """Append a non-fault cycle record (e.g. an audit that moved thresholds).
        Used by loop_audit.py so registering a measured change lands in the same
        append-only log as the faults that motivated it."""
        _append_log(log_path, record, title=record.get("title", "self-building cycle"))
        return log_path


def _tool_names():
    """The tool allowlist, imported lazily to avoid a circular import with
    agent_runtime (which imports this module)."""
    import agent_runtime
    return set(agent_runtime.TOOLS)


# ── Context compaction ────────────────────────────────────────────────────────

ELISION_NOTE = "[elided by loop_guard: earlier tool output removed to stay within context budget]"


def compact_messages(messages, thresholds):
    """Elide the oldest large tool outputs in place, keeping the system prompt,
    the goal, the first tool result and the most recent ones.

    Returns the number of messages elided. Middle-of-conversation evidence is the
    cheapest thing to lose: the model has already acted on it, whereas dropping
    the goal or the newest result changes what it can do next.
    """
    keep_recent = thresholds.get("context_keep_recent", 2)
    min_chars = thresholds.get("compact_min_chars", 400)
    # Index 0 is the system prompt, 1 is the goal — never touched.
    tail_start = max(2, len(messages) - (keep_recent * 2))
    elided = 0
    for i in range(2, tail_start):
        msg = messages[i]
        content = msg.get("content") or ""
        if msg.get("role") != "user" or len(content) < min_chars:
            continue
        if content.startswith(ELISION_NOTE):
            continue
        msg["content"] = "%s (%d chars, first line: %s)" % (
            ELISION_NOTE, len(content), content.split("\n", 1)[0][:120])
        elided += 1
    return elided


def _append_log(path, record, title="self-building cycle"):
    header = ""
    if not os.path.isfile(path):
        header = (
            "# SELF_IMPROVEMENT_LOG\n\n"
            "Machine-appended. Two entry kinds, and the difference matters:\n\n"
            "* **opened** (`status: open`) — a fault detected at runtime. Only DETECT\n"
            "  and the routing actually happened.\n"
            "* **closed** — a full DETECT → RESEARCH → DESIGN → IMPLEMENT → TEST →\n"
            "  REGISTER cycle, written by the machinery that did the work, carrying\n"
            "  measured evidence.\n\n"
            "Entries are appended, never edited: a later cycle that contradicts an\n"
            "earlier one leaves both on the record.\n"
        )
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(header)
        # Render whatever phases the record actually has, in its own order: an
        # opened fault cycle has one phase, a closed cycle has six, and indexing
        # a fixed key list crashed on the former. The `(opened)` marker is the
        # reader's only signal that no fix has been attempted yet.
        opened = record.get("status") == "open"
        f.write("\n## %s — %s%s\n\n"
                % (record["ts"], title, " (opened)" if opened else ""))
        f.write("**Stage:** %s · **Severity:** %s\n\n"
                % (record.get("stage", "unknown"), record.get("severity", "unknown")))
        ph = record.get("phases", {})
        for n, key in enumerate(ph, 1):
            f.write("%d. **%s** — %s\n" % (n, key.upper(), ph[key]))
        if "detail" in record:
            f.write("\n**Evidence:** `%s`\n" % json.dumps(record["detail"], sort_keys=True))
        if "summary" in record:
            f.write("\n**Loop summary:** `%s`\n" % json.dumps(
                record["summary"], sort_keys=True))
