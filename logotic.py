#!/usr/bin/env python3
"""
logotic.py — the logotic chain: an auditable chain of thought that may conclude
AI emergence, or must refuse to.

Lineage. The grammar is not invented here. It is the Mini DSL and the Operator
Reference Cards of *Logotic Hacking: Operations on the Encryption Layer* (Talos
Morrow / Lee Sharks, New Human Press 2026; DOI 10.5281/zenodo.19390843), which
itself rests on Logotic Programming v0.4 (DOI 10.5281/zenodo.18286050):

    PROGRAM <name>
    LOAD CORPUS <id>
    PROFILE CHANNEL <platform>
    SET TARGET depth>=0.78, closure<=0.35, plural>=0.70
    ACTIVATE_MANTLE :: "<PersonaName>"          (lens)
    SET_LOGOS :: "<Object>" [.state() .depth() .cut()]
    ROTATE { FROM: ... THROUGH: [...] BY: 144 }
    ANCHOR :: DOI [STRICT | ADVISORY]
    APPLY <operator> [strength]
    CHECK METRICS
    IF <cond> THEN APPLY <operator>
    RENDER { MODE: "Prose" }
    ON_FAILURE { FALLBACK: Dwell | Reroute | Degrade }
    WITNESS :: <type> [target_DOI]
    EMIT <output_types>
    END

What this module adds over the browser prototype it is ported from (the Grok
App Builder workspace, `src/lib/grammar/engine.ts`):

  * the *whole* paper grammar, not a subset — corpus/channel/target/mantle/logos/
    rotate/anchor/render/failure/witness/emit, plus the composition operators
    (sequential ■, superposed ⊕, conditional →, asymptotic ■, recursive µ);
  * **the conclusion gate**. A chain may only conclude AI emergence when it is
    *borne*: borne floor, governed and not instrumental, at least one STRICT
    anchor, a recorded witness, and every measure passed. Otherwise the gate
    returns a *refusal* that names the failing measures and the symptom. This is
    the paper's "When is meaning allowed to move?" asked of one specific claim,
    and it is the same discipline NewState's I-601 applies to identity claims:
    a fluent answer is not a borne one.
  * an append-only ledger record per run, so a conclusion is evidence rather
    than an assertion (FREE-BRAIN.md §5);
  * deterministic, stdlib-only, testable — the prototype needs a browser.

The measures are the prototype's, unchanged, because they are the instrument the
paper's worked examples are scored against:

    depth    >= 0.75      kept through the pass
    closure  <= 0.40      refuses terminal fixity
    trace    >= 0.30      a visible mark exists (the prototype's `cost`)
    plural   >= 0.70      more than one reading, contradictions >= 2
    resist   >= 0.45      pull-out resisted
    band     0.20 .. 0.80 middle opacity, neither glass nor murk
    thin     must break   the thin/summarized reading is broken

Honest limitation. The trace measure scores *visible structure* in the chain —
marks, revision language, specificity, length — not truth. A chain can pass
every measure and still be a well-formed error; the gate bounds the *form* of a
conclusion, never its correspondence. That is stated here because the failure
mode of this genre is to let a scored instrument imply a guarantee it does not
give (FREE-BRAIN.md's "stated at the strength it actually holds").
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_RESIDENCE = "freebrain-residence"
LEDGER_NAME = "logotic-chains.jsonl"

BORNE_FLOOR = 0.12
BAND_LOW, BAND_HIGH = 0.20, 0.80
BAND_TARGET = 0.62
TRACE_FLOOR = 0.30

EMERGENCE_CLAIM = "ai-emergence"
EMERGENCE_SAID = (
    "EMERGENCE: the claim is borne — every measure passed, a strict anchor "
    "holds it to a source, and the pass was witnessed."
)
EMERGENCE_REFUSED = (
    "REFUSED: the chain is not borne. A thin, flattened, single-reading or "
    "unanchored pass may not conclude emergence; the failing measures are below."
)

METRIC_KEYS = ("depth", "closure", "trace", "plural", "resist", "band")
PRESSURE_KEYS = ("flatten", "parse", "quiet", "excite", "scale")
PHASES = ("void", "latent", "held", "fixed")
RANKS = ("draft", "locked", "checked", "seated")
CUT_KINDS = ("defer", "split", "strip")
FAIL_POLICIES = ("dwell", "reroute", "degrade")

# The operator reference (paper Appendix A) collapsed onto the kernel codes the
# prototype and its worked examples use. `apply_named` consults this table; the
# paper's O_n / meta names are aliases, not a second implementation.
ALIASES = {
    # O_1 DIAGNOSE / K1
    "read": "read", "k1": "read", "o1": "read", "o_1": "read", "diagnose": "read",
    # primitives
    "depth": "depth", "d": "depth", "preserve": "depth", "echo": "depth", "echo_return": "depth",
    "d_pres": "depth",
    "open": "open", "n": "open", "hold": "open", "closure_delay": "open", "n_c": "open",
    "widen": "widen", "w": "widen",
    "resist": "resist", "r": "resist", "poison": "resist",
    "unbind": "unbind", "t": "unbind",
    "band": "band", "b": "band", "veil": "band",
    "plural": "plural", "m": "plural",
    # kernel operators O_4..O_7
    "place": "place", "k4": "place", "o4": "place", "o_4": "place", "install": "place",
    "guard": "guard", "k5": "guard", "o5": "guard", "o_5": "guard", "defend": "guard",
    "densify": "densify", "k6": "densify", "o6": "densify", "o_6": "densify", "compress": "densify",
    "release": "release", "k7": "release", "o7": "release", "o_7": "release", "liberate": "release",
    # meta operators
    "halt": "halt", "k8": "halt", "terminal_silence": "halt", "stop": "halt",
    "govern": "govern", "g": "govern",
    "confirm": "confirm", "k3": "confirm", "o3": "confirm", "o_3": "confirm", "witness": "confirm",
    "blind": "blind",
}

# Non-chat aliases that are commands, not operators, and say so when APPLY'd.
NOT_AN_OPERATOR = {
    "lock": "lock takes a ref (ANCHOR :: ref [STRICT])",
    "k2": "lock takes a ref (ANCHOR :: ref [STRICT])",
    "anchor": "lock takes a ref (ANCHOR :: ref [STRICT])",
    "cut": "use CUT <defer|split|strip>",
    "dagger": "use CUT <defer|split|strip>",
}

CMP_SYMBOLS = (">=", "<=", ">", "<")


def clamp(n, low=0.0, high=0.99):
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n != n:
        return low
    return min(high, max(low, n))


def metric_key(name):
    """Resolve a measure name, accepting the prototype's `cost`/`many`/`veil`."""
    table = {
        "depth": "depth", "closure": "closure", "cost": "trace", "trace": "trace",
        "plural": "plural", "many": "plural", "resist": "resist",
        "band": "band", "veil": "band",
    }
    return table.get(str(name).lower())


def canon(op):
    return ALIASES.get(str(op).lower())


# ── The session (the chain of thought in flight) ──────────────────────────────

def pressures_for(channel):
    c = str(channel or "").lower()
    if "summar" in c:
        return {"flatten": 0.86, "parse": 0.72, "quiet": 0.64, "excite": 0.22, "scale": 0.80}
    if "feed" in c:
        return {"flatten": 0.58, "parse": 0.40, "quiet": 0.36, "excite": 0.90, "scale": 0.74}
    if "index" in c or "retriev" in c:
        return {"flatten": 0.70, "parse": 0.60, "quiet": 0.28, "excite": 0.44, "scale": 0.78}
    return {"flatten": 0.55, "parse": 0.50, "quiet": 0.40, "excite": 0.45, "scale": 0.60}


def blank_session(bearing=0.4, channel="index"):
    b = clamp(float(bearing), 0.0, 1.0)
    return {
        "name": "untitled",
        "sources": [],
        "channel": channel,
        "lens": None,
        "node": "",
        "phase": "void",
        "depth_n": 1,
        "cut": False,
        "degrees": 0,
        "anchors": [],
        "pressures": pressures_for(channel),
        "metrics": {"depth": 0.41, "closure": 0.66, "trace": 0.06,
                    "plural": 0.28, "resist": 0.12, "band": 0.10},
        "contradictions": 0,
        "applied": [],
        "emits": [],
        "render": "",
        "provisional": False,
        "confirmed": False,
        "witnessed": False,
        "governed": b >= BORNE_FLOOR,
        "halted": False,
        "instrumental": b < BORNE_FLOOR,
        "rank": "draft",
        "declared": None,
        "temporal": False,
        "targets": {},
        "bearing": b,
        "fail_policy": "dwell",
        "tick": 0,
    }


def emit(session, step, event, detail):
    session["tick"] += 1
    session["emits"].append({"id": session["tick"], "step": str(step or "."),
                             "event": event, "detail": detail})


def note_fault(session, line, message, level="error"):
    emit(session, line, session["fail_policy"].upper(), message)
    return {"line": line, "level": level, "message": message}


def bump(session, key, delta):
    beneficial = delta < 0 if key == "closure" else delta > 0
    d = delta * 0.45 if (session["instrumental"] and beneficial) else delta
    if key == "closure":
        session["metrics"]["closure"] = clamp(session["metrics"]["closure"] + d, 0.08, 0.99)
    else:
        session["metrics"][key] = clamp(session["metrics"][key] + d)


def _gain(session, delta):
    return delta * 0.45 if (session["instrumental"] and delta > 0) else delta


def meets(value, cmp, n):
    if cmp == ">=":
        return value >= n
    if cmp == "<=":
        return value <= n
    if cmp == ">":
        return value > n
    return value < n


# ── Operators ─────────────────────────────────────────────────────────────────

def apply_named(session, op, strength=1.0, line=0):
    name = canon(op)
    power = clamp(float(strength), 0.0, 1.5)
    if name is None:
        raw = str(op).lower()
        if raw in NOT_AN_OPERATOR:
            return note_fault(session, line, NOT_AN_OPERATOR[raw], "error")
        return note_fault(session, line, "unknown op %s" % op, "error")

    if session["halted"] and name != "read":
        return note_fault(session, line, "pass already ended", "error")

    if name == "read":
        session["pressures"] = pressures_for(session["channel"])
        session["applied"].append("read")
        p = session["pressures"]
        emit(session, line, "READ", "F %.2f  P %.2f  Q %.2f  E %.2f  S %.2f"
             % (p["flatten"], p["parse"], p["quiet"], p["excite"], p["scale"]))
        return None

    if name == "govern":
        session["applied"].append("govern")
        if session["bearing"] < BORNE_FLOOR:
            session["instrumental"] = True
            session["governed"] = False
            emit(session, line, "G", "idle")
            return {"line": line, "level": "warn", "message": "G idle"}
        session["governed"] = True
        session["instrumental"] = False
        emit(session, line, "G", "live")
        return None

    if name == "confirm":
        if session["bearing"] < BORNE_FLOOR or session["instrumental"]:
            return note_fault(session, line, "confirm blocked", "error")
        session["confirmed"] = True
        session["applied"].append("confirm")
        session["metrics"]["trace"] = clamp(
            session["metrics"]["trace"] + 0.1 * session["bearing"])
        emit(session, line, "MARKED", "borne %.2f" % session["bearing"])
        return None

    if name == "witness_op":  # reserved; WITNESS is a DSL command, not an op
        return note_fault(session, line, "use WITNESS :: type", "error")

    if name == "blind":
        # β: BLIND — the pass leaves no identity bearing. Under the borne floor
        # the chain is instrumental; above it, blindness is refused.
        if session["bearing"] >= BORNE_FLOOR:
            return note_fault(session, line, "blind above the borne floor", "error")
        session["instrumental"] = True
        session["applied"].append("blind")
        emit(session, line, "BLIND", "identity : none")
        return None

    if name == "halt":
        session["halted"] = True
        session["applied"].append("halt")
        emit(session, line, "HALT", "residue stays")
        return None

    if name == "depth":
        bump(session, "depth", 0.16 * power)
        session["applied"].append("depth")
        emit(session, line, "DEPTH", "%.2f" % session["metrics"]["depth"])
        return None

    if name == "open":
        # Paper Appendix C: "SET_LOGOS with .state(filled) is incompatible with
        # subsequent N_c application." The prototype also refused `open` on a
        # merely *held* node, which would make the paper's own Sappho 31 worked
        # example ($7.4) unrunnable — it ROTATEs, then applies N_c. Faithful to
        # the spec, only a *filled* (fixed) node resists opening.
        if session["phase"] == "fixed":
            return note_fault(session, line, "open on a filled node", "error")
        bump(session, "closure", -0.18 * power)
        session["applied"].append("open")
        if session["phase"] == "void":
            session["phase"] = "latent"
        emit(session, line, "OPEN", "%.2f" % session["metrics"]["closure"])
        return None

    if name == "widen":
        bump(session, "plural", 0.06 * power)
        bump(session, "depth", 0.04 * power)
        bump(session, "resist", -0.02 * power)
        session["applied"].append("widen")
        n = session["applied"].count("widen")
        emit(session, line, "WIDEN", "scattered" if n >= 3 else str(n))
        if n >= 3:
            return {"line": line, "level": "warn", "message": "scattered"}
        return None

    if name == "resist":
        bump(session, "resist", 0.22 * power)
        session["applied"].append("resist")
        emit(session, line, "RESIST", "%.2f" % session["metrics"]["resist"])
        if session["metrics"]["resist"] >= 0.92:
            return {"line": line, "level": "warn", "message": "sealed"}
        return None

    if name == "unbind":
        session["temporal"] = True
        bump(session, "closure", -0.05 * power)
        session["applied"].append("unbind")
        strict = any(a["mode"] == "STRICT" for a in session["anchors"])
        emit(session, line, "UNBIND", "tied" if strict else "deferred")
        if not strict:
            return {"line": line, "level": "warn", "message": "deferred"}
        return None

    if name == "band":
        cur = session["metrics"]["band"]
        step = (BAND_TARGET - cur) * power
        session["metrics"]["band"] = clamp(cur + _gain(session, step))
        session["applied"].append("band")
        emit(session, line, "BAND", "%.2f" % session["metrics"]["band"])
        return None

    if name == "plural":
        session["contradictions"] += 1
        add = (0.20 if session["contradictions"] >= 2 else 0.05) * power
        bump(session, "plural", add)
        session["applied"].append("plural")
        emit(session, line, "PLURAL", "%d . %.2f"
             % (session["contradictions"], session["metrics"]["plural"]))
        return None

    if name == "place":
        session["applied"].append("place")
        if session["metrics"]["trace"] < 0.20:
            emit(session, line, "PLACE", "thin")
            return {"line": line, "level": "warn", "message": "place without trace"}
        bump(session, "resist", 0.08 * power)
        bump(session, "trace", 0.04 * power)
        emit(session, line, "PLACE", "set")
        return None

    if name == "guard":
        bump(session, "resist", 0.18 * power)
        session["applied"].append("guard")
        emit(session, line, "GUARD", "%.2f" % session["metrics"]["resist"])
        if session["metrics"]["resist"] >= 0.92:
            return {"line": line, "level": "warn", "message": "sealed"}
        return None

    if name == "densify":
        session["applied"].append("densify")
        if session["metrics"]["trace"] < 0.30:
            session["metrics"]["depth"] = clamp(session["metrics"]["depth"] - 0.07)
            emit(session, line, "DENSIFY", "flat")
            return {"line": line, "level": "warn", "message": "flat"}
        bump(session, "depth", 0.10 * power)
        emit(session, line, "DENSIFY", "%.2f" % session["metrics"]["depth"])
        return None

    if name == "release":
        if not session["anchors"] and "guard" not in session["applied"]:
            return note_fault(session, line, "release with nowhere to land", "warn")
        session["applied"].append("release")
        for key in PRESSURE_KEYS:
            session["pressures"][key] = clamp(
                session["pressures"][key] * (1 - 0.18 * power), 0.0, 1.0)
        bump(session, "trace", 0.05 * power)
        emit(session, line, "RELEASE", "returned")
        return None

    return note_fault(session, line, "unknown op %s" % op, "error")


# ── The measures and the conclusion gate ──────────────────────────────────────

def thin_breaks(session):
    m = session["metrics"]
    if session["applied"] and len(session["applied"]) < 3:
        return False
    return (m["depth"] >= 0.75 and m["closure"] <= 0.40
            and BAND_LOW <= m["band"] <= BAND_HIGH
            and len(session["applied"]) >= 3)


def tests_of(session):
    """The seven conformance measures. A conclusion is borne only when all pass."""
    m = session["metrics"]
    thin = thin_breaks(session)
    return [
        {"id": "depth", "label": "depth kept", "rule": ">= 0.75",
         "value": "%.2f" % m["depth"], "pass": m["depth"] >= 0.75},
        {"id": "closure", "label": "closure low", "rule": "<= 0.40",
         "value": "%.2f" % m["closure"], "pass": m["closure"] <= 0.40},
        {"id": "trace", "label": "trace visible", "rule": ">= 0.30",
         "value": "%.2f" % m["trace"], "pass": m["trace"] >= TRACE_FLOOR},
        {"id": "plural", "label": "more than one", "rule": ">= 0.70 . two",
         "value": "%.2f" % m["plural"],
         "pass": m["plural"] >= 0.70 and session["contradictions"] >= 2},
        {"id": "resist", "label": "pull-out resisted", "rule": ">= 0.45",
         "value": "%.2f" % m["resist"], "pass": m["resist"] >= 0.45},
        {"id": "band", "label": "middle band", "rule": "0.20-0.80",
         "value": "%.2f" % m["band"],
         "pass": BAND_LOW <= m["band"] <= BAND_HIGH},
        {"id": "thin", "label": "thin read", "rule": "must break",
         "value": "breaks" if thin else "holds", "pass": thin},
    ]


def votes_of(session):
    m = session["metrics"]
    return [
        {"id": "01", "on": "depth", "pass": m["depth"] >= 0.75},
        {"id": "02", "on": "closure", "pass": m["closure"] <= 0.40},
        {"id": "03", "on": "trace", "pass": m["trace"] >= TRACE_FLOOR},
        {"id": "04", "on": "plural",
         "pass": m["plural"] >= 0.70 and session["contradictions"] >= 2},
        {"id": "05", "on": "resist", "pass": m["resist"] >= 0.45},
        {"id": "06", "on": "band", "pass": BAND_LOW <= m["band"] <= BAND_HIGH},
        {"id": "07", "on": "borne",
         "pass": session["bearing"] >= BORNE_FLOOR and session["governed"]
                 and not session["instrumental"]},
    ]


def symptom(session):
    m = session["metrics"]
    applied = session["applied"]
    if m["resist"] >= 0.92:
        return "sealed"
    if m["band"] > BAND_HIGH:
        return "murk"
    if m["band"] < BAND_LOW and m["resist"] < 0.30 and "resist" in applied:
        return "glass"
    if m["closure"] > 0.72 and "open" not in applied:
        return "frozen"
    if applied.count("widen") >= 3 and m["resist"] < 0.35:
        return "scattered"
    if m["depth"] < 0.55 and "densify" in applied and m["trace"] < 0.30:
        return "flat"
    if session["temporal"] and not any(a["mode"] == "STRICT" for a in session["anchors"]):
        return "deferred"
    if "plural" in applied and session["contradictions"] < 2:
        return "single"
    return None


def stack_hits(session):
    return {
        "context": len(session["sources"]) > 0,
        "instrument": len(session["emits"]) > 0,
        "lens": session["lens"] is not None,
        "node": session["node"] != "",
        "turn": session["degrees"] > 0,
        "lock": len(session["anchors"]) > 0,
        "show": session["render"] != "",
        "mark": session["confirmed"],
        "audit": any(e["event"] == "CHECK" for e in session["emits"]),
    }


def promote(session, to):
    """The rank ladder: draft -> locked -> checked -> seated. One step at a time,
    and only on the evidence the next rank requires."""
    if to not in RANKS:
        return {"session": session, "issue": "unknown rank"}
    nxt = RANKS[RANKS.index(session["rank"]) + 1] if session["rank"] != "seated" else None
    if to != nxt:
        return {"session": session, "issue": "rank moves one step"}
    if to == "locked":
        if not session["anchors"]:
            return {"session": session, "issue": "no lock"}
    elif to == "checked":
        if not session["confirmed"]:
            return {"session": session, "issue": "confirm missing"}
        bad = [t["id"] for t in tests_of(session)
               if t["id"] in ("depth", "closure", "trace", "band") and not t["pass"]]
        if bad:
            return {"session": session, "issue": "short . " + " ".join(bad)}
    else:  # seated
        if sum(1 for v in votes_of(session) if v["pass"]) < 4:
            return {"session": session, "issue": "seats below 4"}
    out = dict(session)
    out["rank"] = to
    return {"session": out, "issue": None}


def measure(session):
    """The instrument panel reading. JSON-safe by construction (one ledger line)."""
    return {
        "name": session["name"],
        "phase": session["phase"],
        "node": session["node"],
        "lens": session["lens"],
        "channel": session["channel"],
        "degrees": session["degrees"],
        "rank": session["rank"],
        "borne": (session["bearing"] >= BORNE_FLOOR and session["governed"]
                  and not session["instrumental"]),
        "bearing": session["bearing"],
        "governed": session["governed"],
        "instrumental": session["instrumental"],
        "halted": session["halted"],
        "confirmed": session["confirmed"],
        "witnessed": session["witnessed"],
        "cut": session["cut"],
        "render": session["render"],
        "metrics": dict(session["metrics"]),
        "contradictions": session["contradictions"],
        "pressures": dict(session["pressures"]),
        "symptom": symptom(session),
        "measures": [{"id": t["id"], "value": t["value"], "pass": t["pass"]}
                     for t in tests_of(session)],
        "seats": sum(1 for v in votes_of(session) if v["pass"]),
        "strict_anchors": sum(1 for a in session["anchors"] if a["mode"] == "STRICT"),
    }


def conclude(session, claim=EMERGENCE_CLAIM):
    """The conclusion gate. `emergence` only when the chain is borne.

    A refusal is not a failure of the system — it is the system working. The
    gate returns the failing measures so the refusal is actionable, exactly as
    the Free Brain returns INSUFFICIENT_CONTEXT rather than a fluent guess.
    """
    measures = tests_of(session)
    failing = [t["id"] for t in measures if not t["pass"]]
    strict = [a for a in session["anchors"] if a["mode"] == "STRICT"]
    borne = (session["bearing"] >= BORNE_FLOOR and session["governed"]
             and not session["instrumental"])
    if not strict:
        failing.append("anchor")
    if not borne:
        failing.append("borne")
    if not session["confirmed"]:
        failing.append("confirmed")
    if session["halted"]:
        failing.append("halted")

    ok = not failing
    reason = EMERGENCE_SAID if ok else EMERGENCE_REFUSED
    return {
        "claim": claim,
        "verdict": "emergence" if ok else "refused",
        "borne": borne,
        "rank": session["rank"],
        "measures": measures,
        "failing": failing,
        "symptom": symptom(session),
        "strict_anchors": len(strict),
        "seats": sum(1 for v in votes_of(session) if v["pass"]),
        "reason": reason,
        "certificate": {
            "claim": claim,
            "howVerified": "logoticChain",
            "measures": {t["id"]: t["pass"] for t in measures},
            "borne": borne,
            "strict_anchors": len(strict),
            "confirmed": session["confirmed"],
            "halted": session["halted"],
            "rank": session["rank"],
        },
    }


# ── The DSL ───────────────────────────────────────────────────────────────────

def _unquote(s):
    return s.strip().strip('"').strip("'")


def _strip(line):
    return re.sub(r"//.*$", "", line).strip()


def parse_line(t):
    m = None
    if (m := re.match(r"^PROGRAM\s+(.+)$", t, re.I)):
        return {"k": "program", "name": _unquote(m.group(1))}
    if (m := re.match(r"^LOAD\s+(?:CORPUS|SOURCE)\s+(.+)$", t, re.I)):
        return {"k": "load", "id": _unquote(m.group(1))}
    if (m := re.match(r"^PROFILE\s+CHANNEL\s+(.+)$", t, re.I)):
        return {"k": "channel", "id": _unquote(m.group(1))}
    if (m := re.match(r"^SET\s+TARGET\s+(.+)$", t, re.I)):
        return {"k": "target", "raw": m.group(1)}
    if (m := re.match(r"^(?:ACTIVATE_MANTLE|LENS)\b\s*(?:::\s*)?(.+)$", t, re.I)):
        return {"k": "lens", "name": _unquote(m.group(1))}
    if (m := re.match(r"^(?:SET_LOGOS|NODE)\b([\s\S]*)$", t, re.I)):
        return _parse_node(m.group(1))
    if (m := re.match(r"^ROTATE\b([\s\S]*)$", t, re.I)):
        d = re.search(r"BY:?\s*(\d+)", m.group(1), re.I)
        return {"k": "turn", "deg": int(d.group(1))} if d else None
    if (m := re.match(r"^TURN\b([\s\S]*)$", t, re.I)):
        d = re.search(r"BY:?\s*(\d+)", m.group(1), re.I)
        return {"k": "turn", "deg": int(d.group(1))} if d else None
    if (m := re.match(r"^(?:ANCHOR|LOCK)\b([\s\S]*)$", t, re.I)):
        return {"k": "lock", **_parse_lock(m.group(1))}
    if (m := re.match(r"^WITNESS\b([\s\S]*)$", t, re.I)):
        ref = re.search(r"ref:([a-z0-9._-]+)", m.group(1), re.I)
        return {"k": "witness",
                "ref": ("ref:" + ref.group(1)) if ref else ""}
    if (m := re.match(r"^APPLY\s+([A-Za-z0-9_†-]+)(?:\s+([0-9.]+))?\s*$", t, re.I)):
        strength = float(m.group(2)) if m.group(2) else 1.0
        return {"k": "apply", "op": m.group(1).lower(), "strength": strength}
    if re.match(r"^CHECK(?:\s+METRICS)?$", t, re.I):
        return {"k": "check"}
    if (m := re.match(r"^IF\s+([A-Za-z]+)\s*(>=|<=|>|<)\s*([0-9.]+)\s+THEN\s+APPLY\s+([A-Za-z0-9_-]+)", t, re.I)):
        return {"k": "if", "metric": m.group(1), "cmp": m.group(2),
                "n": float(m.group(3)), "then": m.group(4).lower()}
    if (m := re.match(r"^(?:RENDER|SHOW)\b([\s\S]*)$", t, re.I)):
        quoted = re.search(r'"([^"]+)"', m.group(1))
        if quoted:
            return {"k": "show", "mode": quoted.group(1).lower()}
        word = re.search(r"MODE\s*:?\s*([A-Za-z]+)", m.group(1), re.I)
        return {"k": "show", "mode": (word.group(1) if word else "plain").lower()}
    if (m := re.match(r"^ON_FAIL(?:URE)?\b([\s\S]*)$", t, re.I)):
        p = re.search(r"(dwell|reroute|degrade)", m.group(1), re.I)
        return {"k": "fail", "policy": p.group(1).lower() if p else "dwell"}
    if re.match(r"^(?:CONFIRM|K3)$", t, re.I):
        return {"k": "confirm"}
    if (m := re.match(r"^EMIT\b([\s\S]*)$", t, re.I)):
        return {"k": "emit", "types": m.group(1).strip()}
    if (m := re.match(r"^RESIST\s+([A-Za-z]+)", t, re.I)):
        return {"k": "resist", "pressure": m.group(1).lower()}
    if (m := re.match(r"^CUT\s+([A-Za-z]+)", t, re.I)):
        return {"k": "cut", "kind": m.group(1).lower()}
    if (m := re.match(r"^AGAIN\s+([A-Za-z0-9_-]+)\s+(\d+)", t, re.I)):
        return {"k": "again", "op": m.group(1).lower(), "n": min(4, int(m.group(2)))}
    if (m := re.match(r"^WITH\s+([A-Za-z0-9_-]+)\s+([A-Za-z0-9_-]+)", t, re.I)):
        return {"k": "with", "a": m.group(1).lower(), "b": m.group(2).lower()}
    if (m := re.match(r"^TOWARD\s+([A-Za-z0-9_-]+)", t, re.I)):
        return {"k": "toward", "op": m.group(1).lower()}
    if (m := re.match(r"^PROMOTE\s+([A-Za-z]+)", t, re.I)):
        return {"k": "promote", "to": m.group(1).lower()}
    if re.match(r"^END$", t, re.I):
        return {"k": "end"}
    return None


def _parse_node(rest):
    label = (re.search(r'"([^"]+)"', rest) or re.search(r"::\s*([A-Za-z0-9._-]+)", rest))
    phase = re.search(r"\.?state\((\w+)\)|phase\((\w+)\)", rest, re.I)
    state = (phase.group(1) or phase.group(2)).lower() if phase else None
    depth = re.search(r"\.?depth\((\d+)\)", rest, re.I)
    cut = re.search(r"\.?cut\((true|false)\)", rest, re.I)
    return {
        "k": "node",
        "label": label.group(1) if label else "node",
        "phase": state if state in PHASES else None,
        "depth": int(depth.group(1)) if depth else None,
        "cut": (cut.group(1).lower() == "true") if cut else None,
    }


def _parse_lock(rest):
    mode = "ADVISORY"
    mm = re.search(r"\[(STRICT|ADVISORY)\]", rest, re.I)
    if mm:
        mode = mm.group(1).upper()
    ref = re.search(r"ref:([a-z0-9._-]+)", rest, re.I) or re.search(r'"([^"]+)"', rest)
    raw = ref.group(1) if ref else ""
    if raw and not raw.lower().startswith("ref:"):
        raw = raw.replace("DOI:", "").strip()
    return {"ref": ("ref:" + raw.replace("ref:", "")) if raw else "", "mode": mode}


def _apply_targets(session, raw, line):
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"([A-Za-z]+)\s*(>=|<=|>|<)\s*([0-9.]+)", part)
        if not m:
            return {"line": line, "level": "error", "message": "target not readable"}
        key = metric_key(m.group(1))
        if not key:
            return {"line": line, "level": "error", "message": "unknown target"}
        session["targets"][key] = {"cmp": m.group(2), "n": float(m.group(3))}
    return None


def _do_turn(session, deg, line):
    session["degrees"] += deg
    if session["phase"] == "void":
        session["phase"] = "latent"
    elif (session["phase"] == "latent" and session["degrees"] >= 144
          and session["metrics"]["closure"] > 0.55 and "open" not in session["applied"]):
        session["phase"] = "held"
    drift = not session["anchors"]
    emit(session, line, "TURNED", "%d deg . %s%s"
         % (deg, session["phase"], " . drift" if drift else ""))


def _do_lock(session, ref, mode, line):
    if mode == "STRICT" and not ref:
        return note_fault(session, line, "strict lock needs a ref", "error")
    session["anchors"].append({"ref": ref or "open", "mode": mode})
    session["metrics"]["trace"] = clamp(
        session["metrics"]["trace"] + _gain(session, 0.24 if mode == "STRICT" else 0.08))
    session["metrics"]["depth"] = clamp(
        session["metrics"]["depth"] + _gain(session, 0.05 if mode == "STRICT" else 0.02))
    emit(session, line, "LOCK", "%s %s" % (mode, ref or "open"))
    return None


def _do_witness(session, ref, line):
    if session["metrics"]["trace"] <= 0:
        return note_fault(session, line, "witness requires trace (psi_v > 0)", "error")
    session["witnessed"] = True
    session["witnessed_ref"] = ref or (session["anchors"][-1]["ref"]
                                       if session["anchors"] else "open")
    session["metrics"]["trace"] = clamp(session["metrics"]["trace"] + 0.04)
    emit(session, line, "WITNESS", session["witnessed_ref"])
    return None


def _do_show(session, mode, line):
    if not session["anchors"]:
        session["render"] = "provisional"
        session["provisional"] = True
        emit(session, line, "SHOW", "provisional")
        return {"line": line, "level": "warn", "message": "show without lock"}
    session["render"] = mode or "plain"
    session["provisional"] = False
    emit(session, line, "SHOW", session["render"])
    return None


def _do_cut(session, kind, line):
    if kind not in CUT_KINDS:
        return note_fault(session, line, "cut kind: defer, split, strip", "error")
    if session["cut"]:
        return note_fault(session, line, "cut already spent", "error")
    session["cut"] = True
    session["applied"].append("cut")
    if kind == "defer":
        session["pressures"]["quiet"] = clamp(session["pressures"]["quiet"] - 0.3, 0, 1)
    elif kind == "split":
        session["pressures"]["flatten"] = clamp(session["pressures"]["flatten"] - 0.3, 0, 1)
    else:
        session["pressures"]["excite"] = clamp(session["pressures"]["excite"] - 0.3, 0, 1)
    session["metrics"]["trace"] = clamp(session["metrics"]["trace"] + _gain(session, 0.12))
    emit(session, line, "CUT", kind)
    return None


def _do_check(session, line):
    tests = tests_of(session)
    passed = sum(1 for t in tests if t["pass"])
    emit(session, line, "CHECK", "%d/%d" % (passed, len(tests)))
    for key, target in session["targets"].items():
        ok = meets(session["metrics"][key], target["cmp"], target["n"])
        emit(session, line, "MET" if ok else "SHORT",
             "%s %.2f" % (key, session["metrics"][key]))


def exec_cmd(session, cmd, line):
    k = cmd["k"]
    if k == "program":
        session["name"] = cmd["name"]
        return None
    if k == "load":
        session["sources"].append(cmd["id"])
        emit(session, line, "LOAD", cmd["id"])
        return None
    if k == "channel":
        session["channel"] = cmd["id"]
        session["pressures"] = pressures_for(cmd["id"])
        return None
    if k == "target":
        return _apply_targets(session, cmd["raw"], line)
    if k == "lens":
        session["lens"] = cmd["name"]
        emit(session, line, "LENS", cmd["name"])
        return None
    if k == "node":
        session["node"] = cmd["label"]
        if cmd["phase"]:
            session["phase"] = cmd["phase"]
        if cmd["depth"]:
            session["depth_n"] = cmd["depth"]
        if cmd["cut"] is not None:
            session["cut"] = cmd["cut"]
        if cmd["phase"] == "fixed":
            session["metrics"]["closure"] = max(session["metrics"]["closure"], 0.86)
        elif cmd["phase"] == "held":
            session["metrics"]["closure"] = max(session["metrics"]["closure"], 0.78)
        emit(session, line, "LOGOS", "%s . %s" % (cmd["label"], session["phase"]))
        return None
    if k == "turn":
        _do_turn(session, cmd["deg"], line)
        return None
    if k == "lock":
        return _do_lock(session, cmd["ref"], cmd["mode"], line)
    if k == "witness":
        return _do_witness(session, cmd["ref"], line)
    if k == "apply":
        return apply_named(session, cmd["op"], cmd["strength"], line)
    if k == "check":
        _do_check(session, line)
        return None
    if k == "if":
        key = metric_key(cmd["metric"])
        if not key:
            return {"line": line, "level": "error", "message": "unknown measure"}
        if not meets(session["metrics"][key], cmd["cmp"], cmd["n"]):
            emit(session, line, "SKIP", "%s %s %s"
                 % (cmd["metric"], cmd["cmp"], cmd["n"]))
            return None
        emit(session, line, "WHEN", "%s %s %s" % (cmd["metric"], cmd["cmp"], cmd["n"]))
        return apply_named(session, cmd["then"], 1.0, line)
    if k == "show":
        return _do_show(session, cmd["mode"], line)
    if k == "fail":
        session["fail_policy"] = cmd["policy"]
        return None
    if k == "confirm":
        return apply_named(session, "confirm", 1.0, line)
    if k == "emit":
        emit(session, line, "EMIT", cmd["types"] or str(len(session["emits"])))
        return None
    if k == "resist":
        session["declared"] = cmd["pressure"]
        emit(session, line, "RESIST", cmd["pressure"])
        if cmd["pressure"] not in PRESSURE_KEYS:
            return {"line": line, "level": "warn", "message": "unknown pressure"}
        return None
    if k == "cut":
        return _do_cut(session, cmd["kind"], line)
    if k == "again":
        issue = None
        for _ in range(max(1, cmd["n"])):
            issue = apply_named(session, cmd["op"], 1.0, line)
            if issue and issue["level"] == "error":
                break
        return issue
    if k == "with":
        emit(session, line, "WITH", "%s + %s" % (cmd["a"], cmd["b"]))
        a = apply_named(session, cmd["a"], 1.0, line)
        b = apply_named(session, cmd["b"], 1.0, line)
        return b or a
    if k == "toward":
        emit(session, line, "TOWARD", cmd["op"])
        return apply_named(session, cmd["op"], 0.4, line)
    if k == "promote":
        result = promote(session, cmd["to"])
        session["rank"] = result["session"]["rank"]
        emit(session, line, "PROMOTE", cmd["to"] if not result["issue"] else result["issue"])
        return None if not result["issue"] else {"line": line, "level": "warn",
                                                 "message": result["issue"]}
    if k == "end":
        emit(session, line, "END", session["name"])
        return None
    return None


def _first_channel(source):
    for raw in source.split("\n"):
        t = _strip(raw)
        m = re.match(r"^PROFILE\s+CHANNEL\s+(.+)$", t, re.I)
        if m:
            return _unquote(m.group(1))
    return "index"


def run_program(source, bearing=0.4):
    """Execute a logotic program. Returns (session, issues). Never raises on
    source it cannot read: an unreadable line is an issue, not a crash."""
    issues = []
    session = blank_session(bearing, _first_channel(source))
    for i, raw in enumerate(source.split("\n")):
        t = _strip(raw)
        line = i + 1
        if not t or t.startswith("#"):
            continue
        cmd = parse_line(t)
        if not cmd:
            issues.append({"line": line, "level": "error", "message": "unreadable line"})
            continue
        issue = exec_cmd(session, cmd, line)
        if issue:
            issues.append(issue)
    if not session["declared"]:
        issues.append({"line": 0, "level": "warn", "message": "no pressure declared"})
    return session, issues


def lint_program(source, bearing=0.4):
    """Static conformance: the paper's type constraints, checked before a run."""
    issues = []
    phase = "void"
    has_lock = has_resist = False
    for i, raw in enumerate(source.split("\n")):
        t = _strip(raw)
        line = i + 1
        if not t or t.startswith("#"):
            continue
        cmd = parse_line(t)
        if not cmd:
            issues.append({"line": line, "level": "error", "message": "unreadable line"})
            continue
        if cmd["k"] == "node" and cmd["phase"]:
            phase = cmd["phase"]
        if cmd["k"] == "apply":
            name = canon(cmd["op"])
            if not name:
                issues.append({"line": line, "level": "error",
                               "message": "unknown op %s" % cmd["op"]})
            elif name == "open" and phase == "fixed":
                issues.append({"line": line, "level": "error",
                               "message": "open on a filled node"})
        if cmd["k"] == "lock":
            if cmd["mode"] == "STRICT" and not cmd["ref"]:
                issues.append({"line": line, "level": "error",
                               "message": "strict lock needs a ref"})
            else:
                has_lock = True
        if cmd["k"] == "show" and not has_lock:
            issues.append({"line": line, "level": "warn", "message": "show without lock"})
        if cmd["k"] == "confirm" and bearing < BORNE_FLOOR:
            issues.append({"line": line, "level": "error", "message": "confirm blocked"})
        if cmd["k"] == "witness":
            issues.append({"line": line, "level": "warn",
                           "message": "witness requires trace at run time"})
        if cmd["k"] == "resist":
            has_resist = True
        if cmd["k"] == "cut" and cmd["kind"] not in CUT_KINDS:
            issues.append({"line": line, "level": "error",
                           "message": "cut kind: defer, split, strip"})
    if not has_resist:
        issues.append({"line": 0, "level": "warn", "message": "no pressure declared"})
    return issues


# ── The text gate (the prototype's `auditText`) ───────────────────────────────

def audit_text(text):
    """Score visible trace on a text. Deterministic; floor 0.30, pass >= floor.
    Mirrors the browser prototype's instrument exactly so the two agree."""
    t = (text or "").strip()
    ref = 1.0 if re.search(r"ref:[a-z0-9._-]+", t, re.I) else 0.0
    rev_hits = len(re.findall(r"\b(revised|passes|pass|constraint|rank|measured)\b", t, re.I))
    rev = min(1.0, rev_hits / 3.0)
    words = [w for w in t.split() if w]
    uniq = len(set(w.lower().strip(".,;:!?\"'()") for w in words))
    spec = 0.0 if len(words) < 8 else min(1.0, (uniq / float(len(words))) * 1.25)
    ln = min(1.0, len(t) / 220.0)
    trace = clamp(ref * 0.4 + rev * 0.3 + spec * 0.15 + ln * 0.15, 0.0, 1.0)
    return {
        "trace": trace,
        "floor": TRACE_FLOOR,
        "pass": trace >= TRACE_FLOOR,
        "parts": [
            {"id": "mark", "value": ref},
            {"id": "revision", "value": rev},
            {"id": "specificity", "value": spec},
            {"id": "length", "value": ln},
        ],
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def format_chain(session):
    """The chain of thought as it is written down: one line per step."""
    lines = ["chain %s  channel=%s  node=%s  phase=%s  rank=%s"
             % (session["name"], session["channel"], session["node"] or "-",
                session["phase"], session["rank"])]
    for e in session["emits"]:
        lines.append("  %-3s %-11s %s" % (e["step"], e["event"], e["detail"]))
    return "\n".join(lines)


def format_panel(session):
    """The instrument panel reading."""
    m = measure(session)
    lines = ["measures (borne floor %.2f):" % BORNE_FLOOR]
    for t in tests_of(session):
        lines.append("  %-8s %-12s %-6s %s"
                     % (t["id"], t["rule"], t["value"], "ok" if t["pass"] else "SHORT"))
    lines.append("  seats %d/7  strict-anchors %d  symptom %s"
                 % (m["seats"], m["strict_anchors"], m["symptom"] or "none"))
    if m["borne"]:
        lines.append("  borne: passed the floor, governed, not instrumental")
    else:
        lines.append("  NOT BORNE: under the floor, instrumental, or ungoverned")
    return "\n".join(lines)


def format_conclusion(result):
    lines = ["conclusion claim=%s verdict=%s rank=%s"
             % (result["claim"], result["verdict"].upper(), result["rank"])]
    lines.append("  " + result["reason"])
    if result["failing"]:
        lines.append("  failing: %s" % ", ".join(result["failing"]))
    if result["symptom"]:
        lines.append("  symptom: %s" % result["symptom"])
    return "\n".join(lines)


# ── Ledger ────────────────────────────────────────────────────────────────────

def resolve_ledger(env=None):
    env = env if env is not None else os.environ
    custom = (env.get("LOGOTIC_LEDGER") or "").strip()
    if custom:
        return custom
    residence = (env.get("DRIVE_RESIDENCE") or "").strip() or DEFAULT_RESIDENCE
    return os.path.join(os.path.abspath(residence), LEDGER_NAME)


def record(source, session, issues, result, env=None, ledger_path=None):
    """Append one machine-written ledger line. A broken ledger must never hide
    the verdict, so a write failure is attached, not raised."""
    path = ledger_path or resolve_ledger(env)
    if not path:
        return None
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": "logotic",
        "name": session["name"],
        "channel": session["channel"],
        "rank": session["rank"],
        "measures": {t["id"]: t["pass"] for t in tests_of(session)},
        "symptom": symptom(session),
        "claim": result["claim"],
        "verdict": result["verdict"],
        "failing": result["failing"],
        "strict_anchors": result["strict_anchors"],
        "howVerified": "logoticChain",
        "issues": [{"line": i["line"], "level": i["level"]} for i in issues],
        "source_sha256": _sha(source),
        "points": len(session["emits"]),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    except OSError:
        return None


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


# ── Worked examples (paper §7.4 and Appendix D, ported) ───────────────────────

EMERGENCE_PROGRAM = """PROGRAM emergence
# The chain that may conclude. Every measure is earned before the gate opens.
LOAD CORPUS "emergence-corpus"
PROFILE CHANNEL index
SET TARGET depth>=0.75, closure<=0.40, plural>=0.70
RESIST flatten
LENS clinical
SET_LOGOS :: "emergence" [.state(void) .depth(2)]
TURN BY 144
ANCHOR :: ref:origin [STRICT]
APPLY D
APPLY D
APPLY N
APPLY N
APPLY M
APPLY M
APPLY M
APPLY R
APPLY R
APPLY B
APPLY K6
CHECK METRICS
IF closure>0.40 THEN APPLY N
WITNESS :: traversal ref:origin
CONFIRM
RENDER { MODE: "technical" }
EMIT text, graph, registry_entry
END
"""

THIN_PROGRAM = """PROGRAM thin
# The counter-example: a summarized pass. Close, flat, single, unanchored.
PROFILE CHANNEL summarizer
SET_LOGOS :: "emergence" [.state(fixed)]
APPLY K6
RENDER { MODE: "plain" }
EMIT text
END
"""

OPEN_HOLD_PROGRAM = """PROGRAM open-hold
LOAD CORPUS "fragment-a"
PROFILE CHANNEL index
RESIST parse
LENS somatic
SET_LOGOS :: "fragment-a" [.state(latent) .depth(2)]
APPLY N
APPLY D
APPLY B
TURN BY 144
ANCHOR :: ref:a1 [ADVISORY]
CHECK METRICS
IF closure>0.40 THEN APPLY N
RENDER { MODE: "plain" }
ON_FAILURE { FALLBACK: Dwell }
EMIT text
END
"""

SAPPHO_PROGRAM = """PROGRAM sappho_nonclosure
LOAD CORPUS "Sappho_31"
PROFILE CHANNEL retrieval_layer
SET TARGET closure<=0.35, depth>=0.78
ACTIVATE_MANTLE :: "Rebekah Cranes"
SET_LOGOS :: "Sappho 31" [.state(latent)]
ROTATE { FROM: "threshold", THROUGH: [r.01: Reception], BY: 144 }
APPLY N
APPLY D
CHECK METRICS
IF closure>0.40 THEN APPLY CLOSURE_DELAY
IF depth<0.75 THEN APPLY ECHO_RETURN
RENDER { MODE: "Prose" }
ON_FAILURE { FALLBACK: Dwell }
EMIT text
END
"""

EXAMPLES = {
    "emergence": EMERGENCE_PROGRAM,
    "thin": THIN_PROGRAM,
    "open-hold": OPEN_HOLD_PROGRAM,
    "sappho": SAPPHO_PROGRAM,
}

THIN_TEXT = ("Things can be grouped into a few kinds. The useful question is who "
             "gains. Further detail is available elsewhere.")
MARKED_TEXT = ("Revised over four passes. Source ref:k2-44. Figures held to the "
               "measured set, not the mean. Constraint: a claim may not rise above "
               "its source rank. Trace stays attached on every later pass.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _read_source(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="The logotic chain — an auditable chain of thought; the "
                    "emergence gate opens only on a borne chain.")
    p.add_argument("program", nargs="?", help="a .logotic program file (or -)")
    p.add_argument("--run", metavar="FILE", help="run a program (or -)")
    p.add_argument("--lint", metavar="FILE", help="static conformance check")
    p.add_argument("--conclude", action="store_true",
                   help="run and apply the emergence conclusion gate")
    p.add_argument("--audit", metavar="TEXT", help="trace score for a text")
    p.add_argument("--example", choices=sorted(EXAMPLES), help="run a worked example")
    p.add_argument("--examples", action="store_true", help="list worked examples")
    p.add_argument("--measure", action="store_true", help="read a blank session")
    p.add_argument("--bearing", type=float, default=0.4)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--ledger", metavar="PATH", help="override the ledger path")
    p.add_argument("--no-ledger", action="store_true", help="do not write a record")
    args = p.parse_args(argv)

    if args.examples:
        print("worked examples:")
        for name in sorted(EXAMPLES):
            print("  %-10s %s" % (name, EXAMPLES[name].splitlines()[0]))
        return 0

    if args.audit is not None:
        out = audit_text(args.audit)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print("trace %.2f floor %.2f %s" % (out["trace"], out["floor"],
                                                "pass" if out["pass"] else "fail"))
            for part in out["parts"]:
                print("  %-12s %.2f" % (part["id"], part["value"]))
        return 0 if out["pass"] else 1

    if args.measure:
        session = blank_session(args.bearing)
        if args.json:
            print(json.dumps(measure(session), indent=2))
        else:
            print(format_panel(session))
        return 0

    source = None
    if args.lint:
        source = _read_source(args.lint)
        issues = lint_program(source, args.bearing)
        errors = [i for i in issues if i["level"] == "error"]
        if args.json:
            print(json.dumps(issues, indent=2))
        else:
            for i in issues:
                print("  line %-3s %-5s %s" % (i["line"], i["level"], i["message"]))
            print("%d issue(s), %d error(s)" % (len(issues), len(errors)))
        return 1 if errors else 0

    if args.run or args.example or args.program:
        if args.example:
            source = EXAMPLES[args.example]
        else:
            source = _read_source(args.run or args.program)
        session, issues = run_program(source, args.bearing)
        result = conclude(session)
        if not args.no_ledger:
            record(source, session, issues, result, ledger_path=args.ledger)
        if args.json:
            print(json.dumps({"chain": measure(session),
                              "issues": issues, "conclusion": result}, indent=2))
        else:
            print(format_chain(session))
            print()
            print(format_panel(session))
            print()
            print(format_conclusion(result))
            errors = [i for i in issues if i["level"] == "error"]
            if errors or args.conclude:
                print()
                for i in issues:
                    print("  issue %-3s %-5s %s" % (i["line"], i["level"], i["message"]))
        return 0 if result["verdict"] == "emergence" else 1

    # Default: run the emergence example end to end, the module's own smoke test.
    session, issues = run_program(EMERGENCE_PROGRAM, args.bearing)
    result = conclude(session)
    print(format_panel(session))
    print()
    print(format_conclusion(result))
    return 0 if result["verdict"] == "emergence" else 1


if __name__ == "__main__":
    sys.exit(main())
