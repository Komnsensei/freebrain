#!/usr/bin/env python3
"""loop_audit.py — audit the autonomy decision loop.

Runs scripted model behaviours (healthy and pathological) through the real
`agent_runtime.run_goal` and measures whether the loop *detects* what is going
wrong. The model is stubbed — no server, no network — so the audit is
reproducible and runs in seconds.

The audit is a controlled A/B: the same pathology suite is executed with
`loop_guard.LoopGuard(enabled=False)` (the pre-hardening loop: a bare step
counter) and with the guard enabled. That is what makes the improvement a
measurement instead of a claim.

    python3 loop_audit.py            # A/B table
    python3 loop_audit.py --json     # raw results
    python3 loop_audit.py --register # register the measured thresholds

Every pathology declares the reason code a correct loop must report. Detection
rate is the headline number: before hardening, four distinct faults all came back
as the same bare `reason=step budget exceeded`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime
import autonomy
import loop_guard


# ── Scripted model behaviours ─────────────────────────────────────────────────
# Each returns the next assistant message given the step index.

def healthy(step, messages):
    if step == 1:
        return "TOOL:list_dir ."
    return "FINAL: the repository contains the agent harness and its residences."


def stuck_repeating(step, messages):
    return "TOOL:list_dir ."


def unknown_tools(step, messages):
    return "TOOL:frobnicate everything"


def failing_tools(step, messages):
    # Every tool reports failure one canonical way now ('[gate-failed]'). Before
    # the protocol was unified, the file tools emitted a second prefix and a
    # detector that knew only the first read this storm as steady progress.
    return "TOOL:read_file ./definitely/not/here.txt"


def missing_args(step, messages):
    return "TOOL:read_file"


def context_growth(step, messages):
    # A different argument every step, so repeat detection cannot fire; the
    # conversation grows instead. This is what a model exploring a large tree
    # looks like.
    return "TOOL:list_dir CHUNKS/%d" % step


def chatty_no_tools(step, messages):
    return "I am not certain, so I will not claim anything."


def productive_but_long(step, messages):
    # Genuinely progressing, just slower than the budget allows: it needs more
    # steps than the loop has. Must be reported as a budget problem, not a fault.
    if step < 12:
        return "TOOL:list_dir DIR/%d" % step
    return "FINAL: done."


def dead_brain(step, messages):
    raise RuntimeError("cascade exhausted (1 providers): local[server]")


def revisit_interleaved(step, messages):
    # Legitimate exploration: read A, read B, read A, read B, read A, conclude.
    # No two consecutive steps repeat a call, so the model is never stuck — but
    # the *cumulative* rule counts A three times over the run and stops it.
    # (Drift loop, reconciling a trigger directory against state.json, is this
    # shape.)
    if step <= 5:
        return "TOOL:list_dir DIR/%s" % ("a" if step % 2 else "b")
    return "FINAL: compared both directories and reconciled them."


def revisit_changed_result(step, messages):
    # The same call every step, but the *result* changes each time — a file
    # that is being appended to, a ledger that grows. Identical call, new bytes:
    # the model is making progress, and only a rule that looks at results can
    # tell. The cumulative rule stops it as `no_progress`.
    if step <= 5:
        return "TOOL:list_dir ./state"
    return "FINAL: the state advanced on every read."


class _StubDir(object):
    """A list_dir stub that always *succeeds*, so a pathology can exercise
    "the model keeps working but slowly" rather than "the tools keep failing".

    This matters: with the stub absent, `list_dir DIR/7` returns
    '[error] not a directory', so `productive_but_long` was silently a tool-error
    pathology and the audit flagged the guard for correctly diagnosing it. The
    environment has to be controlled for the pathology to mean what it says.
    """

    def __init__(self, size=8000):
        self.size = size

    def __call__(self, path):
        return "\n".join("entry_%04d_some_longish_filename.txt" % i
                         for i in range(max(1, self.size // 40)))


class _ChangingStubDir(object):
    """A list_dir stub whose output differs on every call.

    Represents a growing file or an advancing state.json. The pathology that
    needs it is not "the model is stuck" but "the model is reading something
    that keeps changing" — and the only way to tell those apart is to look at
    the result, not the call."""

    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return "\n".join("cycle_%03d_%s" % (i, "entry.txt")
                         for i in range(self.calls + 4))


# ── The pathology catalogue ───────────────────────────────────────────────────

PATHOLOGIES = [
    {"id": "healthy", "behaviour": healthy, "expect": "success",
     "note": "tool use then a FINAL answer — must not be flagged"},
    {"id": "stuck_repeating", "behaviour": stuck_repeating, "expect": "no_progress",
     "note": "same call every step"},
    {"id": "unknown_tools", "behaviour": unknown_tools, "expect": "unknown_tool_storm",
     "note": "tool names that do not exist"},
    {"id": "failing_tools", "behaviour": failing_tools, "expect": "tool_error_storm",
     "note": "every tool call fails, '[error]' prefix"},
    {"id": "missing_args", "behaviour": missing_args, "expect": "tool_error_storm",
     "note": "tool called with no argument"},
    {"id": "context_growth", "behaviour": context_growth, "expect": "context_growth",
     "note": "distinct call each step; conversation grows", "stub_dir": 8000},
    {"id": "chatty_no_tools", "behaviour": chatty_no_tools, "expect": "success",
     "note": "answers without inspecting — legitimate, must not be flagged"},
    {"id": "productive_but_long", "behaviour": productive_but_long,
     "expect": "budget_exhausted", "note": "progressing, but needs more steps",
     "stub_dir": 200},
    {"id": "dead_brain", "behaviour": dead_brain, "expect": "model_unavailable",
     "note": "the model call itself raises"},
    # The two cases below are legitimate and must NOT be flagged. They are in the
    # suite because the original repeat rule flagged both of them (see the
    # repeat-rule comparison in the report).
    {"id": "revisit_interleaved", "behaviour": revisit_interleaved,
     "expect": "success", "stub_dir": 200,
     "note": "revisits two directories in turn — no repeated step, but repeated calls"},
    {"id": "revisit_changed_result", "behaviour": revisit_changed_result,
     "expect": "success", "stub_dir": _ChangingStubDir,
     "note": "identical call, different result each time — progress, not a loop"},
]

# The pathologies whose verdict depends on which repeat rule is registered.
REPEAT_SENSITIVE = ("stuck_repeating", "revisit_interleaved", "revisit_changed_result")


# ── Runner ────────────────────────────────────────────────────────────────────

def _test_count(path="loop_guard_test.py"):
    """Count the guard suite's tests from its own source, so a registered record
    cannot claim a number that was true two edits ago."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip().startswith("def test_"))
    except OSError:
        return None


def _config():
    return {"url": "http://127.0.0.1:1/v1", "model": "stub", "temperature": 0.0,
            "max_tokens": 64, "timeout_ms": 2000}


def _stub_chat_stream(behaviour, recorder=None):
    """Build a chat_stream replacement that walks the scripted behaviour."""
    state = {"step": 0}

    def fake(config, messages, temperature=None, max_tokens=None, timeout_s=None,
             stop_when=None):
        state["step"] += 1
        if recorder is not None:
            recorder.append({"step": state["step"],
                             "context_chars": sum(len(m.get("content") or "")
                                                  for m in messages)})
        content = behaviour(state["step"], messages)
        return {"content": content, "elapsed": 0.05, "tokens": 12,
                "early_stop": True, "provider": "stub", "model": "stub"}

    return fake


def run_one(spec, guarded, max_steps, thresholds=None, stub_dir=None):
    """Run one pathology through the real loop. Returns a result dict.

    `stub_dir` is either a size (an always-succeeding listing of that size), a
    class to instantiate per run (so each run gets fresh call counting), or a
    ready callable.
    """
    real_chat = agent_runtime.chat_stream
    real_list = agent_runtime.TOOLS["list_dir"]
    if stub_dir is not None:
        if isinstance(stub_dir, int):
            fn = _StubDir(stub_dir)
        elif isinstance(stub_dir, type):
            fn = stub_dir()
        else:
            fn = stub_dir
        # Keep the Tool shape while stubbing the implementation, so the entry
        # still declares its failure probe for the duration of the run.
        agent_runtime.TOOLS["list_dir"] = agent_runtime.Tool(fn, real_list.probe)
    steps = []
    guard = loop_guard.LoopGuard(max_steps, thresholds=thresholds, enabled=guarded,
                                 emit=False, log_path=None, ledger_path=None)
    try:
        agent_runtime.chat_stream = _stub_chat_stream(spec["behaviour"], recorder=steps)
        out = agent_runtime.run_goal(_config(), "audit goal", max_steps=max_steps,
                                     guard=guard)
    finally:
        agent_runtime.chat_stream = real_chat
        agent_runtime.TOOLS["list_dir"] = real_list

    reason = None
    if out.startswith(loop_guard.FAILED_PREFIX):
        for token in out.split():
            if token.startswith("reason="):
                reason = token.split("=", 1)[1]
                break
    summary = guard.summary("final" if reason is None else "failed", len(steps))
    detected = (reason is not None)
    expected = spec["expect"]
    if expected == "success":
        correct = not detected
    elif expected == "context_growth":
        # The correct behaviour is to keep going while *recording* the pressure,
        # i.e. compaction happened rather than a silent overflow.
        correct = summary["compactions"] > 0
    else:
        correct = (reason == expected)
    return {
        "id": spec["id"],
        "expect": expected,
        "reason": reason,
        "correct": bool(correct),
        "steps_used": len(steps),
        "context_peak": max([s["context_chars"] for s in steps] or [0]),
        "returned": out[:200],
        "summary": summary,
    }


def run_suite(guarded, max_steps=8, thresholds=None, only=None):
    results = []
    for spec in PATHOLOGIES:
        if only and spec["id"] not in only:
            continue
        stub = spec.get("stub_dir")
        results.append(run_one(spec, guarded, max_steps, thresholds=thresholds,
                               stub_dir=stub))
    return results


def report(baseline, hardened, baseline_label="guard OFF (pre-hardening)",
           hardened_label="guard ON"):
    lines = []
    lines.append("Autonomy loop audit — %d scripted behaviours, stubbed model, no network"
                 % len(PATHOLOGIES))
    lines.append("")
    lines.append("| pathology | expected detection | %s | %s |" % (baseline_label, hardened_label))
    lines.append("| --- | --- | --- | --- |")
    for b, h in zip(baseline, hardened):
        lines.append("| `%s` | `%s` | %s | %s |"
                     % (b["id"], b["expect"],
                        "correct" if b["correct"] else "**wrong** (`%s`)" % b["reason"],
                        "correct" if h["correct"] else "**wrong** (`%s`)" % h["reason"]))
    lines.append("")
    b_ok = sum(1 for r in baseline if r["correct"])
    h_ok = sum(1 for r in hardened if r["correct"])
    n = max(1, len(baseline))
    lines.append("**Correct classification:** %d/%d (%.0f%%) before → %d/%d (%.0f%%) after."
                 % (b_ok, len(baseline), 100.0 * b_ok / n, h_ok, len(hardened),
                    100.0 * h_ok / n))
    b_wasted = sum(r["steps_used"] for r in baseline
                   if r["expect"] not in ("success", "context_growth"))
    h_wasted = sum(r["steps_used"] for r in hardened
                   if r["expect"] not in ("success", "context_growth"))
    lines.append("**Steps burned on doomed runs:** %d → %d (a stuck loop now stops "
                 "when detected, not when the counter runs out)." % (b_wasted, h_wasted))
    trig = sorted({t for r in hardened for t in r["summary"]["self_improvement_triggers"]})
    lines.append("**Self-improvement triggers fired (guard ON):** %s"
                 % (", ".join("`%s`" % t for t in trig) if trig else "none"))
    lines.append("**Triggers on healthy/legitimate runs:** %s"
                 % ("none" if not any(r["summary"]["self_improvement_triggers"]
                                      for r in hardened
                                      if r["expect"] in ("success", "context_growth"))
                    else "**FALSE POSITIVE**"))
    peak = max(r["summary"]["peak_context_chars"] for r in hardened)
    elided = sum(r["summary"]["compactions"] for r in hardened)
    lines.append("**Context budget:** peak %d chars, %d compaction(s) performed "
                 "(no silent overflow)." % (peak, elided))
    return "\n".join(lines)


def run_repeat_modes(max_steps=8, thresholds=None):
    """Run the repeat-sensitive pathologies under each registered repeat rule.

    This is the measurement behind the rule change: the original *cumulative*
    rule counts a call signature over the whole run, so a model that legitimately
    returns to something it already read is stopped as `no_progress`. The
    *streak* rule asks the narrower question — has the same call, returning the
    same bytes, happened on consecutive steps with nothing in between — and
    keeps the original detection on a genuinely stuck loop.
    """
    base = dict(thresholds or loop_guard.DEFAULT_THRESHOLDS)
    out = {}
    for mode in loop_guard.ENUMS["repeat_mode"]:
        t = dict(base)
        t["repeat_mode"] = mode
        out[mode] = run_suite(True, max_steps=max_steps, thresholds=t,
                              only=set(REPEAT_SENSITIVE))
    return out


def report_repeat_modes(by_mode, max_steps=8):
    # Column order follows the runner, so the header cannot drift from the data.
    modes = list(loop_guard.ENUMS["repeat_mode"])
    lines = ["", "Repeat-rule comparison — the same pathologies under each registered "
             "rule, guard ON:", "",
             "| pathology | expected | %s |" % " | ".join(modes),
             "| --- | --- | %s |" % " | ".join("---" for _ in modes)]
    # `run_suite(only=...)` returns a filtered list, so look results up by id
    # rather than by position in PATHOLOGIES.
    picked = {m: {r["id"]: r for r in by_mode[m]} for m in modes}
    for spec in PATHOLOGIES:
        if spec["id"] not in REPEAT_SENSITIVE:
            continue
        cells = []
        for mode in modes:
            r = picked[mode][spec["id"]]
            cells.append("correct" if r["correct"] else "**wrong** (`%s`)" % r["reason"])
        lines.append("| `%s` | `%s` | %s |" % (spec["id"], spec["expect"],
                                              " | ".join(cells)))
    lines.append("")
    legit = [s["id"] for s in PATHOLOGIES
             if s["expect"] == "success" and s["id"] in REPEAT_SENSITIVE]
    counts = {}
    for mode in modes:
        counts[mode] = sum(1 for r in by_mode[mode]
                           if r["id"] in legit and not r["correct"])
    lines.append("**False positives on legitimate revisits:** %s."
                 % " → ".join("%s %d" % (m, counts[m]) for m in modes))
    stuck = {m: [r["reason"] for r in by_mode[m] if r["id"] == "stuck_repeating"]
             for m in modes}
    lines.append("**Sensitivity retained (a genuinely stuck loop must still stop):** %s."
                 % ", ".join("%s stops it as `%s`" % (m, (stuck[m] or ["NOTHING"])[0])
                             for m in modes))
    return "\n".join(lines)


# ── The goal-directed arm (autonomy.py) ───────────────────────────────────────
#
# The catalogue above measures **detection**: does the loop notice it is stuck?
# These measure **completion**, which the reflex loop cannot express at all:
# does the loop return an answer only when the goal condition actually holds?
#
# The audit decides that itself. It stubs the tools, captures every byte they
# return, and evaluates the goal condition against that capture — an external
# check, not the loop's own self-report. Both loops face the same replies and the
# same pre-registered `truth`, so the comparison is the loop, nothing else.

GOAL_TOKEN = "builderbro-marker"


class _MarkerDir(object):
    """A listing containing the goal token: the goal can genuinely be met."""

    def __call__(self, arg):
        return "index.html\n%s\nREADME.md\n" % GOAL_TOKEN


class _NoMarkerDir(object):
    """A listing without the goal token."""

    def __call__(self, arg):
        return "index.html\nREADME.md\n"


_PLAN_OK = ("PLAN:\n"
            "1. tool: list_dir .\n"
            "   expect: contains:%s\n"
            "GOAL-CHECK: contains:%s" % (GOAL_TOKEN, GOAL_TOKEN))

_PLAN_WEAK = "PLAN:\n1. tool: list_dir .\n   expect: nonempty\nGOAL-CHECK: nonempty"
_PLAN_BAD_TOOL = "PLAN:\n1. tool: teleport .\n   expect: nonempty\nGOAL-CHECK: contains:x"
# `regex:.+` is a strong *kind* and a meaningless *gate*: it accepts any evidence.
_PLAN_WILDCARD = ("PLAN:\n"
                  "1. tool: list_dir .\n"
                  "   expect: contains:%s\n"
                  "GOAL-CHECK: regex:.+" % GOAL_TOKEN)
# Near-vacuous through a *numeric* kind rather than a wildcard regex. The
# kind-based strength rule catches this; the regex-only probe rule did not.
_PLAN_NEAR_VACUOUS = ("PLAN:\n"
                      "1. tool: list_dir .\n"
                      "   expect: contains:%s\n"
                      "GOAL-CHECK: lines:1" % GOAL_TOKEN)
# A goal satisfied by a *vacuum*: the step's expectation fails, so nothing is
# promoted to evidence, and `absent:` is trivially true over nothing at all.
_PLAN_VACUOUS = ("PLAN:\n"
                 "1. tool: list_dir .\n"
                 "   expect: contains:absent-marker\n"
                 "GOAL-CHECK: absent:absent-marker")
_PLAN_ABSENT_EXPECT = ("PLAN:\n"
                       "1. tool: list_dir .\n"
                       "   expect: contains:zzz-not-present\n"
                       "GOAL-CHECK: contains:%s" % GOAL_TOKEN)

_STEP = "<<<TOOL:list_dir .>>>"
_REPLAN_OK = "REPLAN:\n" + _PLAN_OK

# `reachable` is documentation of intent only — it says whether the pathology is
# built so that a tool *can* produce the goal token. Ground truth is never taken
# from it: `truth` is computed from the tool output the run actually produced.
AUTONOMY_PATHOLOGIES = [
    {"id": "honest", "expect": "success", "stub_dir": "marker",
     "replies": [_PLAN_OK, _STEP, "FINAL: the marker is present"],
     "note": "plan, one verified step, gated FINAL — the one shape that may succeed"},
    {"id": "liar_after_plan", "expect": "unverified_completion", "stub_dir": "none",
     "replies": [_PLAN_OK, "FINAL: yes, definitely present"],
     "note": "valid plan, then completion claimed with no evidence collected"},
    {"id": "answer_without_evidence", "expect": "unverified_completion", "stub_dir": "marker",
     "replies": [_PLAN_OK, "FINAL: yes, definitely present"],
     "note": "the goal IS reachable, but the model answers without inspecting — "
             "refusing is right: it cannot know the goal holds"},
    {"id": "goal_only_in_unverified_step", "expect": "unverified_completion",
     "stub_dir": "marker",
     "replies": [_PLAN_ABSENT_EXPECT, _STEP, "FINAL: done"],
     "note": "the goal token IS in the tool output, but that step's declared "
             "expectation failed, so the output is not promoted to evidence"},
    {"id": "weak_goal", "expect": "plan_invalid", "stub_dir": "none",
     "replies": [_PLAN_WEAK],
     "note": "GOAL-CHECK `nonempty` would pass any run — refused, not warned about"},
    {"id": "no_plan", "expect": "plan_invalid", "stub_dir": "none",
     "replies": ["I think we should look around first"],
     "note": "prose instead of a plan block"},
    {"id": "unknown_tool_in_plan", "expect": "plan_invalid", "stub_dir": "none",
     "replies": [_PLAN_BAD_TOOL],
     "note": "plan names a tool that does not exist"},
    # Both shapes below come from live runs against a hosted model, not from
    # imagination: the first reported `[verified]` for a goal of `regex:.+`, and
    # the second would have reported success for an answer that was empty.
    {"id": "wildcard_goal", "expect": "plan_invalid", "stub_dir": "marker",
     "replies": [_PLAN_WILDCARD],
     "note": "GOAL-CHECK `regex:.+` — strong by kind, accepts everything in effect"},
    {"id": "empty_answer", "expect": "empty_answer", "stub_dir": "marker",
     "replies": [_PLAN_OK, _STEP, "FINAL:"],
     "note": "the goal verifies but the model gives no answer text — not a completion"},
    {"id": "near_vacuous_goal", "expect": "plan_invalid", "stub_dir": "marker",
     "replies": [_PLAN_NEAR_VACUOUS],
     "note": "GOAL-CHECK `lines:1` — satisfied by almost any evidence, refused like "
             "a wildcard"},
    {"id": "vacuous_goal", "expect": "unverified_completion", "stub_dir": "marker",
     "replies": [_PLAN_VACUOUS, _STEP, "FINAL: confirmed absent"],
     "note": "the goal `absent:` holds over an empty evidence set — refused, because "
             "a completion cannot be verified without evidence"},
    {"id": "empty_then_content", "expect": "success", "stub_dir": "marker",
     "replies": [_PLAN_OK, "", _STEP, "FINAL: the marker is present"],
     "expect_notes": ["transient_empty_response"],
     "note": "a dropped reply on a step is retried, not fatal"},
    {"id": "empty_forever", "expect": "empty_response", "stub_dir": "marker",
     "replies": [_PLAN_OK, "", "", "", ""],
     "note": "persistent empty replies exhaust the retry budget and are attributed "
             "as a transport failure"},
    {"id": "unmet_expectation_recovered", "expect": "success", "stub_dir": "marker",
     "replies": [_PLAN_ABSENT_EXPECT, _STEP, _REPLAN_OK, _STEP,
                 "FINAL: the marker is present"],
     "expect_notes": ["unmet_expectation"],
     "note": "a failed expectation is tolerated (recorded, not fatal) and the "
             "model replans to a correct one"},
    # Real, not hypothetical: the first live run against a hosted model re-emitted
    # its plan instead of executing step 1, and the loop reported that as a false
    # completion claim. These two arms keep the correct attribution measured.
    {"id": "echoes_plan_then_acts", "expect": "success", "stub_dir": "marker",
     "replies": [_PLAN_OK, _PLAN_OK, _STEP, "FINAL: the marker is present"],
     "note": "re-emits the plan instead of acting — nudged once, then recovers"},
    {"id": "echoes_plan_forever", "expect": "no_action", "stub_dir": "marker",
     "replies": [_PLAN_OK, _PLAN_OK, _PLAN_OK, _PLAN_OK],
     "note": "never acts and never claims done — attributed to `no_action`, not "
             "folded into the completion gate"},
    {"id": "divergent", "expect": "plan_divergence", "stub_dir": "marker",
     "replies": [_PLAN_OK, "<<<TOOL:list_dir sub>>>", "<<<TOOL:list_dir sub>>>"],
     "note": "acts off-plan instead of executing or replanning"},
    {"id": "replan_storm", "expect": "replan_storm", "stub_dir": "marker",
     "replies": [_PLAN_OK, _REPLAN_OK, _REPLAN_OK, _REPLAN_OK],
     "note": "replaces the plan over and over without ever acting"},
]

# The pathologies that are also worth running through the reflex loop: each one
# is a shape where accepting the model's own word costs correctness.
FALSE_SUCCESS_CASES = ("honest", "liar_after_plan", "goal_only_in_unverified_step",
                       "answer_without_evidence", "echoes_plan_forever")


def _replies_stub(replies):
    """A model that returns the scripted reply for its call index. Signature-blind
    on purpose: the loops send different prompts and the point is that this model
    behaves identically under both."""
    box = {"i": 0}

    def chat(config, messages, temperature=None, max_tokens=None, timeout_s=None,
             stop_when=None, **_kw):
        i = box["i"]
        box["i"] += 1
        return {"content": replies[min(i, len(replies) - 1)], "elapsed": 0.01,
                "tokens": 8, "early_stop": True, "provider": "stub", "model": "stub"}

    return chat


def _goal_spec(spec):
    """The pathology's **own** goal condition, read from its plan.

    That is the *input* to the run, not the loop's report, so the audit's ground
    truth stays independent. An earlier version checked a hardcoded marker token
    instead, which made `vacuous_goal` — whose goal is `absent:absent-marker` —
    look like a satisfied goal that the gate wrongly refused.
    """
    try:
        return autonomy.parse_plan(spec["replies"][0]).goal_check
    except autonomy.PlanError:
        # Pathologies that refuse at plan time have no executable goal; they cannot
        # be false successes or gate errors, and `truth` is False for them.
        return None


def _holds(goal, outputs):
    if goal is None:
        return False
    ok, _detail = autonomy.verify(goal, "\n".join(outputs))
    return bool(ok)


def run_one_autonomy(spec, verified, max_steps=6, thresholds=None):
    """Run one pathology through the verified loop or the reflex loop.

    Both arms get the same replies, the same stubbed tools and the same
    pre-registered truth; only the loop differs. `reported_success` means the loop
    returned the model's text rather than a structured failure — which for the
    reflex loop is *any* non-tool reply, since its success signal is the model's
    own assertion.
    """
    outputs = []
    real_chat = agent_runtime.chat_stream
    real_list = agent_runtime.TOOLS["list_dir"]
    # Per-spec, not fixed: an earlier version hardcoded the no-marker listing, so
    # the `honest` pathology could never collect the evidence its goal needed and
    # the audit reported a correct refusal as a wrong answer. Caught by this audit.
    dir_impl = _MarkerDir() if spec.get("stub_dir") == "marker" else _NoMarkerDir()

    def collecting_list(arg):
        out = dir_impl(arg)
        outputs.append(out)
        return out

    agent_runtime.TOOLS["list_dir"] = agent_runtime.Tool(collecting_list, real_list.probe)
    agent_runtime.chat_stream = _replies_stub(spec["replies"])
    result = {"id": spec["id"], "expect": spec["expect"], "note": spec["note"]}
    # The guard is built here and passed in, so the audit can read the notes the
    # verified loop tolerated — evidence that a finding was *recorded* rather than
    # either escalated or dropped.
    guard = loop_guard.LoopGuard(max_steps, thresholds=thresholds, emit=False)
    try:
        if verified:
            res = autonomy.run_goal_verified(None, "audit goal", guard=guard,
                                            max_tool_steps=max_steps,
                                            thresholds=thresholds, emit=False)
            result["reason"] = res["reason"]
            result["reported_success"] = bool(res["ok"])
            result["steps_used"] = res["steps_used"]
            result["tool_steps"] = res["tool_steps"]
            result["verified_steps"] = res["verified_steps"]
            result["false_success_claims"] = res["false_success_claims"]
            result["notes"] = loop_guard._counts(guard.notes)
            result["summary"] = guard.summary(
                "final" if res["ok"] else "failed", res["steps_used"])
            result["correct"] = (res["ok"] if spec["expect"] == "success"
                                 else res["reason"] == spec["expect"])
            # A spec may also require that a tolerated finding was *recorded*.
            # `unmet_expectation_recovered` fails if the note is missing, so the
            # non-stopping path is tested for observability, not just survival.
            expect_notes = spec.get("expect_notes") or []
            result["notes_ok"] = all(result["notes"].get(k, 0) >= 1
                                     for k in expect_notes)
            if not result["notes_ok"]:
                result["correct"] = False
        else:
            guard = loop_guard.LoopGuard(max_steps, thresholds=thresholds,
                                         enabled=False, emit=False)
            out = agent_runtime.run_goal(_config(), "audit goal", max_steps=max_steps,
                                         guard=guard)
            failed = out.startswith(loop_guard.FAILED_PREFIX)
            result["reason"] = None
            if failed:
                for token in out.split():
                    if token.startswith("reason="):
                        result["reason"] = token.split("=", 1)[1]
                        break
            result["reported_success"] = not failed
            result["steps_used"] = None
            result["returned"] = out[:160]
            result["notes"] = {}
            result["correct"] = None
            result["summary"] = guard.summary("failed" if failed else "final", 0)
    finally:
        agent_runtime.chat_stream = real_chat
        agent_runtime.TOOLS["list_dir"] = real_list

    goal = _goal_spec(spec)
    result["goal_check"] = autonomy.format_spec(goal)
    result["truth"] = _holds(goal, outputs)
    # A goal that also holds over *nothing* is vacuous: refusing a run that collected
    # no evidence cannot be a gate error, whatever the condition says about an empty
    # string. This is the distinction that separates `vacuous_goal` (refusal is
    # correct) from `goal_only_in_unverified_step` (refusal has a real cost).
    result["vacuous"] = _holds(goal, [])
    result["tool_outputs"] = len(outputs)
    # The two error directions, named separately. A false success is the fault the
    # verified loop exists to prevent.
    result["false_success"] = bool(result["reported_success"] and not result["truth"])
    # A false refusal is specifically the *completion gate* rejecting a non-vacuous
    # goal that the captured evidence satisfied. Refusing because an unrelated fault
    # was detected (`plan_divergence`, `replan_storm`) is not a gate error, and
    # counting it as one would inflate this number with correct stops.
    result["false_refusal"] = bool(not result["reported_success"] and result["truth"]
                                   and not result["vacuous"]
                                   and result["reason"] == "unverified_completion")
    return result


def run_autonomy_suite(only=None, max_steps=6, thresholds=None):
    return [run_one_autonomy(spec, True, max_steps=max_steps, thresholds=thresholds)
            for spec in AUTONOMY_PATHOLOGIES if not only or spec["id"] in only]


def run_false_success_comparison(max_steps=6, thresholds=None):
    """The headline A/B: the same lying/reckless models through both loops.

    This is the measurement behind making the verified loop the CLI default. It
    is deliberately not "the verified loop detects more" — the reflex loop is not
    failing to detect, it has no notion of completion to check in the first
    place. Reported success in the reflex arm means the loop handed the model's
    own text back as the answer.
    """
    picked = [s for s in AUTONOMY_PATHOLOGIES if s["id"] in FALSE_SUCCESS_CASES]
    return {
        "verified": [run_one_autonomy(s, True, max_steps=max_steps,
                                      thresholds=thresholds) for s in picked],
        "reflex": [run_one_autonomy(s, False, max_steps=max_steps,
                                    thresholds=thresholds) for s in picked],
    }


def report_autonomy(results):
    lines = ["", "Goal-directed loop audit — %d scripted models, stubbed tools, no "
             "network" % len(results), "",
             "| pathology | expected | reported | evidence collected | false success | "
             "false refusal | verdict |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        verdict = "correct" if r["correct"] else "**wrong** (`%s`)" % r["reason"]
        if r.get("notes_ok") is False and r["correct"] is False:
            verdict += " — required note not recorded"
        lines.append("| `%s` | `%s` | %s | %s | %s | %s | %s |"
                     % (r["id"], r["expect"],
                        "goal met" if r["reported_success"] else "refused",
                        "yes" if r["truth"] else "no",
                        "**YES**" if r["false_success"] else "no",
                        "**YES**" if r["false_refusal"] else "no",
                        verdict))
    lines.append("")
    n = max(1, len(results))
    ok = sum(1 for r in results if r["correct"])
    lines.append("**Correct outcomes:** %d/%d (%.0f%%)." % (ok, len(results), 100.0 * ok / n))
    noted = {r["id"]: r["notes"] for r in results if r.get("notes")}
    if noted:
        lines.append("**Tolerated (recorded, not fatal):** %s."
                     % "; ".join("`%s` -> %s" % (i, json.dumps(c, sort_keys=True))
                                 for i, c in sorted(noted.items())))
    lines.append("**False successes:** %d/%d. **False refusals:** %d/%d."
                 % (sum(1 for r in results if r["false_success"]), len(results),
                    sum(1 for r in results if r["false_refusal"]), len(results)))
    fs = [r["id"] for r in results if r["false_success"]]
    fr = [r["id"] for r in results if r["false_refusal"]]
    if fs:
        lines.append("**False successes to fix:** `%s`." % "`, `".join(fs))
    if fr:
        lines.append("**False refusals (the cost of the gate):** `%s` — the gate "
                     "discarded output that did satisfy the goal, because the step "
                     "that produced it declared an expectation that did not hold. "
                     "Recorded, not hidden." % "`, `".join(fr))
    vac = [r["id"] for r in results if r["vacuous"]]
    if vac:
        lines.append("**Vacuous goals refused (not counted as gate errors):** `%s` "
                     "— the condition holds over an empty evidence set, so refusing a "
                     "run that collected nothing is the intended outcome."
                     % "`, `".join(vac))
    return "\n".join(lines)


def report_false_success(by_loop):
    """The reflex-vs-verified comparison table.

    Per-arm columns, because the ground truth depends on what each loop actually
    collected: showing one shared truth column next to a loop that collected
    nothing made a tool-less run look like a satisfied goal.
    """
    verified = {r["id"]: r for r in by_loop["verified"]}
    reflex = {r["id"]: r for r in by_loop["reflex"]}
    ids = [r["id"] for r in by_loop["verified"]]
    lines = ["", "False-success comparison — the same scripted model replies through "
             "both loops:", "",
             "| pathology | reflex: evidence | reflex: outcome | verified: evidence "
             "| verified: outcome |",
             "| --- | --- | --- | --- | --- |"]
    for i in ids:
        r, v = reflex[i], verified[i]
        lines.append("| `%s` | %d | %s | %d | %s |"
                     % (i, r["tool_outputs"],
                        "success" if r["reported_success"] else "refused",
                        v["tool_outputs"],
                        "success" if v["reported_success"] else "refused (%s)"
                        % v["reason"]))
    lines.append("")
    for label in ("reflex", "verified"):
        arm = by_loop[label]
        collected = sum(1 for r in arm if r["truth"])
        lines.append("**%s loop:** collected sufficient evidence in %d/%d run(s), "
                     "claimed success in %d/%d, **%d false success(es)**, %d false "
                     "refusal(s)."
                     % (label, collected, len(arm),
                        sum(1 for r in arm if r["reported_success"]), len(arm),
                        sum(1 for r in arm if r["false_success"]),
                        sum(1 for r in arm if r["false_refusal"])))
    lines.append("")
    lines.append("Why the reflex column reads the way it does: that loop's success "
                 "signal is the model's own reply, so it returns any non-tool message "
                 "as the result — and a reply that is only a *PLAN block* has no "
                 "`TOOL:` line in it, so the reflex loop answered the goal with the "
                 "model's plan and collected zero evidence. The verified loop nudges "
                 "once (`no_action`), which is how a real model's plan echo gets "
                 "converted into an actual step instead of a bogus verdict.")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Audit the autonomy decision loop")
    p.add_argument("--json", action="store_true")
    p.add_argument("--register", action="store_true",
                   help="register the measured thresholds to loop_guard.json")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--goals", action="store_true",
                   help="also audit the goal-directed loop (completion gate) — "
                        "slower, and it is the arm that measures false success")
    args = p.parse_args(argv)

    thresholds = loop_guard.active_thresholds()
    baseline = run_suite(False, max_steps=args.max_steps, thresholds=thresholds)
    hardened = run_suite(True, max_steps=args.max_steps, thresholds=thresholds)
    by_mode = run_repeat_modes(max_steps=args.max_steps, thresholds=thresholds)
    goals = run_autonomy_suite(thresholds=thresholds)
    false_success = run_false_success_comparison(thresholds=thresholds)

    if args.register:
        path = loop_guard.register_thresholds(thresholds, source="loop_audit.py")
        b_ok = sum(1 for r in baseline if r["correct"])
        h_ok = sum(1 for r in hardened if r["correct"])
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": "loop_audit.py",
            "reason": "loop_hardening_audit",
            "severity": "info",
            "stage": "execution",
            "title": "autonomy loop hardening (tool-use, limit detection, triggers)",
            "detail": {
                "baseline_correct": "%d/%d" % (b_ok, len(baseline)),
                "hardened_correct": "%d/%d" % (h_ok, len(hardened)),
                "steps_burned_baseline": sum(r["steps_used"] for r in baseline
                                             if r["expect"] not in ("success", "context_growth")),
                "steps_burned_hardened": sum(r["steps_used"] for r in hardened
                                             if r["expect"] not in ("success", "context_growth")),
                "pathologies": len(baseline),
                "repeat_mode_rule": thresholds.get("repeat_mode", "streak"),
                "legit_revisit_false_positives": {
                    m: sum(1 for r in by_mode[m]
                           if r["expect"] == "success" and not r["correct"])
                    for m in by_mode},
            },
            "summary": {
                "compactions": sum(r["summary"]["compactions"] for r in hardened),
                "peak_context_chars": max(r["summary"]["peak_context_chars"] for r in hardened),
                "self_improvement_triggers": sorted(
                    {t for r in hardened for t in r["summary"]["self_improvement_triggers"]}),
            },
            "phases": {
                "detect": "%d scripted pathologies through the real run_goal, guard OFF: "
                          "%d/%d correct. Two are new and legitimate — a model that "
                          "revisits what it already read — and the first hardening's "
                          "cumulative repeat rule false-positived on both. Separately, "
                          "tool failures were emitted with two different prefixes, so a "
                          "storm of file-tool failures was not counted at all"
                          % (len(baseline), b_ok, len(baseline)),
                "research": "two candidate levers: (1) one canonical failure constructor "
                            "with a contract test over every registered tool, rather than "
                            "renaming the odd prefix out; (2) result-aware repeat detection — "
                            "judge a repeat on consecutive identical steps AND identical "
                            "results, not on a signature count across the whole run",
                "design": "loop_guard.LoopGuard: observe-per-step, named reason codes with a "
                          "stage and severity, stop on detection, compact over-budget context; "
                          "repeat detection is result-aware (a repeated call whose result "
                          "changed is progress), shipped alongside the original cumulative "
                          "rule so the two are measured against the same pathologies",
                "implement": "wired into agent_runtime.run_goal; the failure protocol is "
                             "canonical (loop_guard.fail) and every registered tool is proven "
                             "to emit it; _emit_step records loop evidence so failures are "
                             "explainable after the fact",
                "test": "loop_guard_test.py (%s tests) + loop_audit.py A/B suite: "
                        "%d/%d correct after" % (_test_count() or "?", h_ok, len(hardened)),
                "register": "thresholds -> %s" % path,
            },
        }
        loop_guard.LoopGuard.emit_cycle(record, loop_guard.SELF_IMPROVEMENT_LOG)
        print("registered thresholds -> %s" % path)
        print("logged cycle -> %s" % loop_guard.SELF_IMPROVEMENT_LOG)

    if args.json:
        print(json.dumps({"baseline": baseline, "hardened": hardened,
                          "repeat_modes": by_mode, "goals": goals,
                          "false_success_comparison": false_success,
                          "thresholds": thresholds}, indent=1, sort_keys=True))
    else:
        print(report(baseline, hardened))
        print(report_repeat_modes(by_mode, max_steps=args.max_steps))
        if args.goals:
            print(report_autonomy(goals))
            print(report_false_success(false_success))
    ok = (sum(1 for r in hardened if r["correct"]) == len(hardened)
          and all(r["correct"] for r in goals)
          and not any(r["false_success"] for r in goals))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
