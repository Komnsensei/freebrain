#!/usr/bin/env python3
"""
qih_metrics.py — QIH metrics as machine-checked ledger records (QIH.md §II).

Stdlib-only mirrors of the four QIH formulas, cross-referenced to their
implementations in `NewState/qih_consciousness/`. The residence runtime
(agent_runtime.py / drift_loop.py) is deliberately zero-dependency — no numpy —
so these are faithful pure-function ports, not imports of the numpy package:

    born_rule               P↑=cos²(θ/2), P↓=sin²(θ/2)     (QIH_WORKFLOW.md; math_utils/trigonometry.py lineage)
    entanglement_distance   d_ij = −α₀·log(E_ij)             (math_utils/entanglement.py)
    coherence_functional    C_MT = |Σm_j|² / Σ|m_j|²         (bio_readout/coherence.py)
    phase_clock             dτ = (Ω₀ / Ω)·dt                 (rendering/spectral_time.py)

Every function is a deterministic gate or metric: the machine computes it from
declared inputs, and the result (plus the inputs and the gate outcome) is what
may enter the ledger. Model prose never becomes a metric — QIH.md §II, the same
discipline as AGENT-INTEGRITY.md non-negotiable #2.
"""

import datetime
import math


def born_rule(p, theta_deg, tol=1e-3):
    """The Born Rule Test gate (QIH.md §II.1).

    Checks that a recorded bit probability `p` matches the orientation-angle
    law of the Hilbert space: P↑ = cos²(θ/2), P↓ = sin²(θ/2). Returns
    (ok, rule, expected) where rule ∈ {"up", "down"} and `expected` is the
    theoretical probability matched. (ok=False, rule=None, expected=None) when
    no rule matches within `tol` — a structured gate failure, never a smoothed
    "approximately right".
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be a probability in [0, 1]")
    if not 0.0 <= float(theta_deg) <= 360.0:
        raise ValueError("theta_deg must be in [0, 360]")
    if tol < 0:
        raise ValueError("tol must be non-negative")
    theta = math.radians(float(theta_deg))
    up = math.cos(theta / 2.0) ** 2
    down = math.sin(theta / 2.0) ** 2
    if abs(p - up) <= tol:
        return True, "up", round(up, 10)
    if abs(p - down) <= tol:
        return True, "down", round(down, 10)
    return False, None, None


def entanglement_distance(e_ij, alpha_0=1.0):
    """Emergent distance between data nodes (QIH.md §II.2).

    d_ij = −α₀·log(E_ij) on the entanglement strength E_ij, mirroring
    `math_utils/entanglement.py`: a self-connection (E_ij ≥ 1) is distance 0,
    and a zero-strength edge yields the large capped distance −α₀·log(1e-10).
    """
    if alpha_0 <= 0:
        raise ValueError("alpha_0 must be a positive constant")
    if e_ij < 0:
        raise ValueError("E_ij is an entanglement strength in [0, 1]")
    if e_ij >= 1.0:
        return 0.0
    return round(-alpha_0 * math.log(e_ij + 1e-10), 10)


def coherence_functional(states):
    """Coherence Functional C_MT (QIH.md §II.3).

    C_MT = |Σ m_j|² / Σ|m_j|² over the complex state components m_j, mirroring
    `bio_readout/coherence.py`. `states` is a list of (re, im) pairs. Returns
    0.0 when there is no activity (all-zero states) — no coherence without
    activity, exactly as the source implementation does.
    """
    if not states:
        raise ValueError("states must be a non-empty list of (re, im) pairs")
    total_re = sum(re for re, _ in states)
    total_im = sum(im for _, im in states)
    numerator = total_re ** 2 + total_im ** 2
    denominator = sum(re ** 2 + im ** 2 for re, im in states)
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 10)


def phase_clock(omega_0, omega, dt=1.0):
    """Phase-Clock Law: subjective time dτ = (Ω₀ / Ω)·dt (QIH.md §II).

    Mirror of `rendering/spectral_time.py` `proper_time`. Frequencies must be
    positive (a zero/negative rate is a domain error, not a measurement).
    """
    if omega <= 0 or omega_0 <= 0:
        raise ValueError("frequencies must be positive")
    return round((float(omega_0) / float(omega)) * float(dt), 10)


def metric_record(kind, inputs, output, gate, source="qih_metric"):
    """One machine-checked metric record, ready to append to the residence
    ledger (ledger.jsonl). The ledger is machine-written only; every record
    carries its inputs, the computed output, and the gate outcome so it can be
    reproduced from the ledger alone (QIH.md §V: reproducible, falsifiable)."""
    return {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "event": "metric",
        "kind": kind,
        "inputs": inputs,
        "output": output,
        "gate": gate,
        "source": source,
    }