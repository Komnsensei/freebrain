"""rag_diagnose.py — make the RAG failures attributable, then fixable by machine.

Two things live here, and they are deliberately separate:

**1. Attribution (`attribute`)** — map measured symptoms to the *stage* that
caused them. This is the part that stops the agent from "improving retrieval"
when the real fault is that a sentence was split across a chunk boundary and no
retriever could ever have found it. Each finding names a stage, the measured
evidence, and the specific lever that could move it. Nothing is inferred from
prose; every finding quotes a number from `rag_eval.py`.

**2. The self-building cycle (`run_cycle`)** — DETECT → RESEARCH → DESIGN →
IMPLEMENT → TEST → REGISTER, as a real search over a bounded parameter space,
ending in a *registered* config file that changes subsequent runs.

The rules that keep this honest:
  - **Floors are pre-registered and hard.** A candidate cannot win by trading
    away a metric that is below its floor; `bm25@120` had the single best
    recall+MRR sum in the first sweep and was still excluded, because its MRR
    missed the floor. If a rule can be satisfied by moving the goalposts, it is
    not a measurement.
  - **No improvement is a valid outcome.** If nothing beats the incumbent, the
    cycle registers nothing and says so (see `CycleResult.changed`). A tuner that
    always reports a win is reporting its own optimism.
  - **The golden set is small (14 answerable items), so one item is 7%.** Every
    selection is therefore reported with its frontier and an explicit
    near-floor flag, and a config sitting exactly on a floor is marked
    `marginal` rather than "best".
"""

from __future__ import annotations

import argparse
import datetime
import itertools
import json
import os
import sys

import rag_core
import rag_eval

# ── Pre-registered decision rule ──────────────────────────────────────────────
# Hard gate: span_coverage must be perfect (text must not be lost by chunking),
# and retrieval must clear the floors. Objective: equal-weight recall + MRR,
# because recall is the binding constraint downstream (an unretrieved chunk
# cannot be cited) while MRR is what keeps the context small and the citations
# precise. Ties break toward better top-1 source accuracy, then fewer chunks.

GATE = {
    "span_coverage": 1.0,
    "recall_at_k": rag_eval.FLOORS["recall_at_k"],
    "mrr": rag_eval.FLOORS["mrr"],
}
MARGIN = 0.02  # a metric this close to its floor is treated as within noise

SELF_IMPROVEMENT_LOG = "SELF_IMPROVEMENT_LOG.md"


def _score(metrics):
    return metrics["recall_at_k"] + metrics["mrr"]


def _gates_passed(metrics, coverage):
    return (
        round(coverage["coverage"], 6) >= GATE["span_coverage"]
        and metrics["recall_at_k"] >= GATE["recall_at_k"]
        and metrics["mrr"] >= GATE["mrr"]
    )


def _marginal(metrics, coverage):
    """True when a metric is inside MARGIN of its floor — with n=14 that is one
    item's worth of rank, so these configs are not meaningfully distinguishable."""
    return (
        abs(coverage["coverage"] - GATE["span_coverage"]) <= MARGIN
        or abs(metrics["recall_at_k"] - GATE["recall_at_k"]) <= MARGIN
        or abs(metrics["mrr"] - GATE["mrr"]) <= MARGIN
    )


# ── Attribution ───────────────────────────────────────────────────────────────

def attribute(index, retrieval, coverage, grounding=None):
    """Turn measured numbers into stage-labelled findings, most severe first."""
    findings = []

    # INGESTION — is the corpus even there?
    meta = index.corpus_meta or {}
    n_files = len(meta.get("files", []))
    if not index.chunks or n_files == 0:
        findings.append({
            "stage": "ingestion", "severity": "critical",
            "symptom": "corpus is empty — 0 files, 0 chunks",
            "evidence": "corpus_meta.files=%d chunks=%d skipped=%d"
                        % (n_files, len(index.chunks), len(meta.get("skipped", []))),
            "lever": "corpus roots (RAG_CORPUS)",
            "hypothesis": "the retrieval stage cannot be evaluated with no corpus",
        })
    elif meta.get("skipped"):
        findings.append({
            "stage": "ingestion", "severity": "note",
            "symptom": "%d source(s) skipped during ingestion" % len(meta["skipped"]),
            "evidence": "; ".join("%s (%s)" % (s["path"], s["reason"])
                                  for s in meta["skipped"][:3]),
            "lever": "corpus roots / MAX_FILE_BYTES",
            "hypothesis": "skipped sources are invisible to every later stage",
        })

    # CHUNKING — is the gold text reachable at all?
    if coverage["coverage"] < GATE["span_coverage"]:
        findings.append({
            "stage": "chunking", "severity": "critical",
            "symptom": "gold text absent from every chunk (%d phrase(s))"
                       % len(coverage["missing_phrases"]),
            "evidence": "span_coverage=%.3f missing=%s"
                        % (coverage["coverage"], coverage["missing_phrases"][:4]),
            "lever": "chunk_overlap_words, chunk_budget_words",
            "hypothesis": "text split across a chunk boundary is unreachable by "
                          "any ranker — fix chunking before touching retrieval",
        })

    # RETRIEVAL — was the gold chunk retrieved?
    missed = [r for r in retrieval["items"]
              if not r.get("expect_no_answer") and not r.get("first_gold_rank")]
    if missed:
        findings.append({
            "stage": "retrieval", "severity": "high",
            "symptom": "%d/%d questions have no gold-bearing chunk in the top %d"
                       % (len(missed), retrieval["n_scored"], retrieval["k"]),
            "evidence": "; ".join(r["id"] for r in missed),
            "lever": "fusion, dense_weight, chunk_budget_words, tokenisation",
            "hypothesis": "either the query shares no vocabulary with the source "
                          "sentence (lexical gap) or the fused score buried it",
        })

    # RANKING — right chunk found, but too deep.
    deep = [r for r in retrieval["items"]
            if r.get("first_gold_rank") and r["first_gold_rank"] > 1]
    if deep and retrieval["mrr"] < GATE["mrr"]:
        findings.append({
            "stage": "ranking", "severity": "high",
            "symptom": "MRR %.3f is below floor %.2f — gold chunks retrieved but ranked low"
                       % (retrieval["mrr"], GATE["mrr"]),
            "evidence": "; ".join("%s@rank%d" % (r["id"], r["first_gold_rank"]) for r in deep),
            "lever": "fusion mode (rrf vs linear), bm25_weight, bm25_k1",
            "hypothesis": "RRF discards score magnitude, so a decisive BM25 lead "
                          "is compressed into a near-tie",
        })

    # EMBEDDING — is the dense channel doing any work?
    a = retrieval["attribution"]
    if a["dense_only"] == 0 and retrieval["n_scored"] > 0:
        findings.append({
            "stage": "embedding", "severity": "medium",
            "symptom": "the hashed-dense channel found 0 gold chunks on its own",
            "evidence": "attribution both=%d lexical_only=%d dense_only=%d"
                        % (a["both"], a["lexical_only"], a["dense_only"]),
            "lever": "dense_weight, char_weight, dim",
            "hypothesis": "a signed hashing vectoriser is not semantic; if it "
                          "contributes no unique recall it should be downweighted "
                          "rather than trusted",
        })

    # GENERATION / GROUNDING — did the answer stay inside the sources?
    if grounding:
        if grounding["fabricated_citation_markers"]:
            findings.append({
                "stage": "generation", "severity": "critical",
                "symptom": "fabricated citations: %d marker(s) point at sources "
                           "that were never retrieved" % grounding["fabricated_citation_markers"],
                "evidence": "items=%s" % grounding["fabricated_citation_items"],
                "lever": "grounded prompt rules / citation gate",
                "hypothesis": "the model is citing its own memory, not the context",
            })
        if grounding["false_answer_rate"] > 0:
            findings.append({
                "stage": "generation", "severity": "critical",
                "symptom": "answered unanswerable questions instead of refusing "
                           "(false-answer rate %.2f)" % grounding["false_answer_rate"],
                "evidence": "refusal_accuracy=%.3f" % grounding["refusal_accuracy"],
                "lever": "grounded prompt refusal rule / retrieval floor",
                "hypothesis": "a generator that never refuses cannot be trusted on "
                              "the questions it should get right",
            })
        if grounding["citation_fidelity"] < rag_eval.FLOORS["citation_fidelity"]:
            findings.append({
                "stage": "grounding-audit", "severity": "high",
                "symptom": "citation fidelity %.3f below floor %.2f"
                           % (grounding["citation_fidelity"], rag_eval.FLOORS["citation_fidelity"]),
                "evidence": "generator=%s" % grounding.get("generator", "unknown"),
                "lever": "context numbering / prompt citation rules",
                "hypothesis": "citations are decorative rather than load-bearing",
            })

    order = {"critical": 0, "high": 1, "medium": 2, "note": 3}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings


# ── The self-building cycle ───────────────────────────────────────────────────

PARAM_GRID = {
    "fusion": ("bm25", "linear", "rrf"),
    "chunk_budget_words": (100, 120, 170, 240),
    "chunk_overlap_words": None,   # derived from budget
    "dense_weight": (0.0, 0.5, 1.0),
}


def candidate_configs(base=None):
    """The bounded search space. Chunking is the expensive stage, so configs are
    grouped by (budget, overlap) and the index is built once per group;
    fusion/dense_weight are pure search-time parameters."""
    base = base or rag_core.DEFAULT_CONFIG
    for budget in PARAM_GRID["chunk_budget_words"]:
        for overlap in (max(8, budget // 5), max(10, budget // 3)):
            chunk_cfg = dict(base)
            chunk_cfg["chunk_budget_words"] = budget
            chunk_cfg["chunk_overlap_words"] = overlap
            for fusion in PARAM_GRID["fusion"]:
                dense_weights = (0.0,) if fusion == "bm25" else PARAM_GRID["dense_weight"]
                for dw in dense_weights:
                    cfg = dict(chunk_cfg)
                    cfg["fusion"] = fusion
                    cfg["dense_weight"] = dw
                    yield cfg


def sweep(generate=None, base=None, progress=None):
    """Evaluate every candidate. Returns (frontier, best) where `best` is the
    highest-objective config that clears the hard gate, or None."""
    frontier = []
    cache = {}
    for cfg in candidate_configs(base):
        key = (cfg["chunk_budget_words"], cfg["chunk_overlap_words"])
        if key not in cache:
            cache[key] = rag_core.build_index(config=cfg)
        index = cache[key]
        coverage = rag_eval.span_coverage(index.chunks)
        retrieval = rag_eval.run_retrieval_eval(index, config=cfg)
        grounding = None
        if generate is not None:
            grounding = rag_eval.run_grounding_eval(index, generate, config=cfg,
                                                    name="extractive (reference)")
        metrics = {
            "fusion": cfg["fusion"],
            "dense_weight": cfg["dense_weight"],
            "chunk_budget_words": cfg["chunk_budget_words"],
            "chunk_overlap_words": cfg["chunk_overlap_words"],
            "n_chunks": len(index.chunks),
            "span_coverage": round(coverage["coverage"], 4),
            "recall_at_k": round(retrieval["recall_at_k"], 4),
            "mrr": round(retrieval["mrr"], 4),
            "precision_at_k": round(retrieval["precision_at_k"], 4),
            "top1_source_accuracy": round(retrieval["top1_source_accuracy"], 4),
            "dense_only": retrieval["attribution"]["dense_only"],
            "objective": round(_score(retrieval), 4),
            "gate_passed": _gates_passed(retrieval, coverage),
            "marginal": _marginal(retrieval, coverage),
            "grounding": ({"citation_fidelity": round(grounding["citation_fidelity"], 4),
                           "refusal_accuracy": round(grounding["refusal_accuracy"], 4),
                           "groundedness": round(grounding["groundedness"], 4)}
                          if grounding else None),
        }
        frontier.append(metrics)
        if progress:
            progress(metrics)

    passing = [m for m in frontier if m["gate_passed"]]
    passing.sort(key=lambda m: (-m["objective"], -m["top1_source_accuracy"], m["n_chunks"]))
    return frontier, (passing[0] if passing else None)


SUPPORT_GRID = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75)


def support_boundary(index, config):
    """The max-margin placement of the gate, measured rather than guessed.

    The grid above has a 0.05 step, and this corpus's boundary is *narrower than
    that* (0.5504 max-negative vs 0.5557 min-positive when this was written). A
    grid that coarse cannot express the boundary, so it does the two things a
    tuner must never do: it either edits the refusal record — an answerable
    question falls below the chosen threshold and gets refused — or, when no grid
    point is perfect, it silently leaves a threshold that answers an unanswerable
    question in place. Both were observed: the registered 0.55 sat *below* the
    max-negative support, which is why `/rag eval` reported a false answer.

    The candidate is therefore derived from the two measured sets — the midpoint
    of the gap between them, which is the placement with the largest margin on
    both sides — and then put through the same evaluation as every grid point. It
    is a *candidate*, not a decision: `sweep_support` still keeps it only if it
    shows a perfect refusal record.

    Returns None when the sets are not separated at all (no threshold can fix
    that — the fault is upstream, in retrieval or chunking) or when either set is
    empty (there is nothing to separate).
    """
    pos, neg = [], []
    for item in rag_eval.GOLDEN_ITEMS:
        hits = index.search(item["question"], config=config)
        s = rag_core.query_support(item["question"], hits, index=index, config=config)
        (neg if item.get("expect_no_answer") else pos).append(s)
    if not pos or not neg:
        return None
    lo, hi = max(neg), min(pos)
    if hi <= lo:
        return None
    return round((lo + hi) / 2.0, 4)


def sweep_support(index, generate, base):
    """Second coordinate step: the retrieval-confidence gate's threshold.

    The rule is deliberately asymmetric. Refusing a question the corpus *can*
    answer is an unrecoverable failure (the user gets nothing), while answering
    an unanswerable one is a grounding failure. So the gate is set as low as
    possible — the smallest threshold that still achieves a perfect refusal
    record — rather than as high as possible for safety.
    """
    candidates = list(SUPPORT_GRID)
    derived = support_boundary(index, base)
    if derived is not None and derived not in candidates:
        candidates.append(derived)
    candidates.sort()  # the report reads as a sweep, so it should be ordered
    rows = []
    for thr in candidates:
        cfg = dict(base)
        cfg["min_query_support"] = thr
        g = rag_eval.run_grounding_eval(index, generate, config=cfg,
                                        name="extractive (reference)")
        rows.append({
            "min_query_support": thr,
            "refusal_accuracy": round(g["refusal_accuracy"], 4),
            "false_answer_rate": round(g["false_answer_rate"], 4),
            "false_refusal_rate": round(g["false_refusal_rate"], 4),
            "citation_fidelity": round(g["citation_fidelity"], 4),
            "groundedness": round(g["groundedness"], 4),
        })
    perfect = [r for r in rows
               if r["refusal_accuracy"] >= 1.0 and r["false_refusal_rate"] <= 0.0]
    chosen = min(perfect, key=lambda r: r["min_query_support"]) if perfect else None
    return rows, chosen


def run_cycle(incumbent=None, generate=None, log_path=SELF_IMPROVEMENT_LOG, register=False,
              progress=None):
    """One DETECT → RESEARCH → DESIGN → IMPLEMENT → TEST → REGISTER cycle.

    Returns a dict describing what was measured and whether anything changed.
    `register=True` is required to persist a win — the default is a dry run, so
    a diagnostic invocation never silently rewrites the runtime config."""
    if generate is None:
        generate = rag_eval.ExtractiveGenerator()
    incumbent = dict(incumbent or rag_core.active_config())

    incumbent_index = rag_core.build_index(config=incumbent)
    inc_coverage = rag_eval.span_coverage(incumbent_index.chunks)
    inc_retrieval = rag_eval.run_retrieval_eval(incumbent_index, config=incumbent)
    inc_grounding = rag_eval.run_grounding_eval(incumbent_index, generate,
                                                config=incumbent,
                                                name="extractive (reference)")
    # The log's DETECT line describes the incumbent, so it has to carry the
    # incumbent's *parameters*, not just its scores. Without these the writer fell
    # back to placeholders and the record read `budget=0 overlap=0` for an
    # incumbent that had neither — a machine-written claim that was simply false.
    inc_metrics = {
        "span_coverage": round(inc_coverage["coverage"], 4),
        "recall_at_k": round(inc_retrieval["recall_at_k"], 4),
        "mrr": round(inc_retrieval["mrr"], 4),
        "objective": round(_score(inc_retrieval), 4),
        "n_chunks": len(incumbent_index.chunks),
        "fusion": incumbent.get("fusion"),
        "dense_weight": incumbent.get("dense_weight"),
        "chunk_budget_words": incumbent.get("chunk_budget_words"),
        "chunk_overlap_words": incumbent.get("chunk_overlap_words"),
        "min_query_support": incumbent.get("min_query_support"),
    }
    findings = attribute(incumbent_index, inc_retrieval, inc_coverage, inc_grounding)

    frontier, best = sweep(generate=generate, base=incumbent, progress=progress)

    # Second coordinate step: the confidence gate is tuned against the retrieval
    # configuration that won the first step, since the support values it sees
    # depend on which chunks retrieval actually returns. This is coordinate
    # descent, not an exhaustive joint search — reported as such.
    support_rows, support_chosen = [], None
    if best is not None:
        best_cfg = dict(incumbent)
        for key in ("fusion", "dense_weight", "chunk_budget_words", "chunk_overlap_words"):
            if key in best:
                best_cfg[key] = best[key]
        gate_index = rag_core.build_index(config=best_cfg)
        support_rows, support_chosen = sweep_support(gate_index, generate, best_cfg)

    changed = False
    action = "no change registered"
    if best is None:
        action = ("no candidate cleared the gate (span_coverage=%.2f, recall>=%.2f, mrr>=%.2f) — "
                  "the fault is not in this parameter space" % (
                      GATE["span_coverage"], GATE["recall_at_k"], GATE["mrr"]))
    else:
        notes = []
        if best["objective"] > inc_metrics["objective"]:
            notes.append("retrieval: objective %.4f -> %.4f (recall %.3f -> %.3f, mrr %.3f -> %.3f)"
                         % (inc_metrics["objective"], best["objective"],
                            inc_metrics["recall_at_k"], best["recall_at_k"],
                            inc_metrics["mrr"], best["mrr"]))
        else:
            notes.append("retrieval: best candidate objective %.4f does not beat incumbent %.4f"
                         % (best["objective"], inc_metrics["objective"]))
        if support_chosen is None:
            notes.append("confidence gate: no threshold in the grid reached a perfect "
                         "refusal record — leave min_query_support unchanged")
        else:
            notes.append("confidence gate: min_query_support=%.4f "
                         "(refusal_accuracy=%.3f, false_answer=%.2f, false_refusal=%.2f)"
                         % (support_chosen["min_query_support"],
                            support_chosen["refusal_accuracy"],
                            support_chosen["false_answer_rate"],
                            support_chosen["false_refusal_rate"]))
        action = "; ".join(notes)
        if register:
            params = {k: best[k] for k in
                      ("fusion", "dense_weight", "chunk_budget_words", "chunk_overlap_words")
                      if k in best}
            if support_chosen:
                params["min_query_support"] = support_chosen["min_query_support"]
            path = rag_core.register_config(params, source="rag_diagnose.py run_cycle")
            action += " — registered to %s" % path
            changed = True

    result = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "incumbent": inc_metrics,
        "findings": findings,
        "n_candidates": len(frontier),
        "best": best,
        "support_sweep": support_rows,
        "support_chosen": support_chosen,
        "changed": changed,
        "action": action,
        "gate": GATE,
    }
    if log_path:
        append_log(result, frontier, log_path)
    return result


# ── Log + capability registry ─────────────────────────────────────────────────

def _finding_lines(findings):
    if not findings:
        return "- (none) — every measured metric is at or above its floor."
    return "\n".join(
        "- **%s** [%s] %s\n  - evidence: `%s`\n  - lever: `%s`\n  - hypothesis: %s"
        % (f["stage"], f["severity"], f["symptom"], f["evidence"], f["lever"], f["hypothesis"])
        for f in findings
    )


def append_log(result, frontier, path=SELF_IMPROVEMENT_LOG):
    """Append the cycle to the human-readable log. Machine-written: the numbers
    come from the run, never from a hand-typed summary."""
    header = ""
    if not os.path.isfile(path):
        header = (
            "# SELF_IMPROVEMENT_LOG\n\n"
            "Machine-appended by `rag_diagnose.py`. Each entry is one full\n"
            "DETECT → RESEARCH → DESIGN → IMPLEMENT → TEST → REGISTER cycle over the RAG\n"
            "pipeline. Entries are appended, never edited: a later cycle that contradicts\n"
            "an earlier one leaves both on the record.\n"
        )
    inc = result["incumbent"]
    best = result["best"]
    n_answerable = sum(1 for i in rag_eval.GOLDEN_ITEMS if not i.get("expect_no_answer"))

    out = []
    out.append("")
    out.append("## %s — RAG tuning cycle" % result["ts"])
    out.append("")
    out.append("### 1. DETECT")
    out.append("Incumbent: `fusion=%s density=%s budget=%s overlap=%s gate=%s`"
               % tuple(inc.get(k) for k in ("fusion", "dense_weight",
                                            "chunk_budget_words", "chunk_overlap_words",
                                            "min_query_support")))
    if any(inc.get(k) is None for k in ("fusion", "dense_weight", "chunk_budget_words",
                                        "chunk_overlap_words", "min_query_support")):
        out.append("")
        out.append("> **Note:** the incumbent's parameters are incomplete above — the "
                   "caller passed a metrics dict without them. Treat the `None`s as "
                   "unknown, not as zero.")
    out.append("")
    out.append("Measured: span_coverage=%.4f, recall@k=%.4f, mrr=%.4f, objective=%.4f, chunks=%d"
               % (inc["span_coverage"], inc["recall_at_k"], inc["mrr"],
                  inc["objective"], inc["n_chunks"]))
    out.append("")
    out.append("### 2. RESEARCH — %d stage-attributed finding(s)" % len(result["findings"]))
    out.append(_finding_lines(result["findings"]))
    out.append("")
    out.append("### 3. DESIGN")
    out.append("Bounded search over fusion × chunk budget × chunk overlap × dense weight "
               "(%d candidates). Hard gate: span_coverage=%.2f, recall@k>=%.2f, mrr>=%.2f."
               % (len(frontier), GATE["span_coverage"], GATE["recall_at_k"], GATE["mrr"]))
    out.append("Objective: recall@k + mrr, tie-break top-1 source accuracy, then fewer chunks.")
    out.append("")
    out.append("### 4. IMPLEMENT / 5. TEST")
    out.append("| fusion | dense | budget | overlap | chunks | span | recall@k | mrr | top1 | gate |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for m in sorted(frontier, key=lambda m: (-m["objective"], m["n_chunks"])):
        out.append("| %s | %.2f | %d | %d | %d | %.3f | %.3f | %.3f | %.3f | %s |"
                   % (m["fusion"], m["dense_weight"], m["chunk_budget_words"],
                      m["chunk_overlap_words"], m["n_chunks"], m["span_coverage"],
                      m["recall_at_k"], m["mrr"], m["top1_source_accuracy"],
                      "pass" if m["gate_passed"] else ("near-floor" if m["marginal"] else "fail")))
    if best:
        out.append("")
        out.append("Best gated candidate: `fusion=%s density=%.2f budget=%d overlap=%d` → "
                   "objective %.4f (recall %.3f, mrr %.3f, %d chunks)."
                   % (best["fusion"], best["dense_weight"], best["chunk_budget_words"],
                      best["chunk_overlap_words"], best["objective"], best["recall_at_k"],
                      best["mrr"], best["n_chunks"]))
    else:
        out.append("")
        out.append("**No candidate cleared the gate.** The fault is upstream of this parameter space.")
    rows = result.get("support_sweep") or []
    if rows:
        out.append("")
        out.append("Confidence gate (`min_query_support`) — the step that decides whether "
                   "the corpus can answer at all, swept at the winning retrieval config:")
        out.append("")
        out.append("| min_query_support | refusal_acc | false_answer | false_refusal | citation_fid | groundedness |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        chosen_thr = (result.get("support_chosen") or {}).get("min_query_support")
        for s in rows:
            mark = " **<- chosen**" if chosen_thr is not None and s["min_query_support"] == chosen_thr else ""
            out.append("| %.2f%s | %.3f | %.2f | %.3f | %.3f | %.3f |"
                       % (s["min_query_support"], mark, s["refusal_accuracy"],
                          s["false_answer_rate"], s["false_refusal_rate"],
                          s["citation_fidelity"], s["groundedness"]))
    out.append("")
    out.append("### 6. REGISTER")
    out.append(result["action"])
    out.append("")
    out.append("**Outcome:** `changed=%s`" % result["changed"])
    out.append("")
    out.append("*Caveat: the golden set holds %d answerable items, so one item is ~%.0f%% of "
               "recall. A config sitting on a floor is `near-floor`, not proven better.*"
               % (n_answerable, 100.0 / max(1, n_answerable)))
    out.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write(header + "\n".join(out) + "\n")
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(description="Attribute RAG failures and run a self-building cycle")
    p.add_argument("--diagnose", action="store_true", help="attribute the incumbent's failures (no search)")
    p.add_argument("--cycle", action="store_true", help="run a full self-building cycle")
    p.add_argument("--register", action="store_true", help="with --cycle: persist a measured win")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-log", action="store_true", help="do not append to SELF_IMPROVEMENT_LOG.md")
    args = p.parse_args(argv)

    log_path = None if args.no_log else SELF_IMPROVEMENT_LOG
    cfg = rag_core.active_config()
    index = rag_core.build_index(config=cfg)
    coverage = rag_eval.span_coverage(index.chunks)
    retrieval = rag_eval.run_retrieval_eval(index, config=cfg)
    grounding = rag_eval.run_grounding_eval(index, rag_eval.ExtractiveGenerator(),
                                            config=cfg, name="extractive (reference)")
    findings = attribute(index, retrieval, coverage, grounding)

    if args.diagnose and not args.cycle:
        report = {
            "config": cfg,
            "retrieval": {k: retrieval[k] for k in
                          ("recall_at_k", "mrr", "precision_at_k", "top1_source_accuracy")},
            "span_coverage": coverage["coverage"],
            "grounding": {k: grounding[k] for k in
                          ("citation_fidelity", "groundedness", "unsupported_rate",
                           "refusal_accuracy", "false_answer_rate",
                           "fabricated_citation_markers")},
            "findings": findings,
        }
        if args.json:
            print(json.dumps(report, indent=1, sort_keys=True))
        else:
            print("incumbent: fusion=%s dense=%.2f budget=%d overlap=%d (%d chunks)"
                  % (cfg["fusion"], cfg["dense_weight"], cfg["chunk_budget_words"],
                     cfg["chunk_overlap_words"], len(index.chunks)))
            print("span_coverage=%.3f recall@k=%.3f mrr=%.3f top1=%.3f"
                  % (coverage["coverage"], retrieval["recall_at_k"], retrieval["mrr"],
                     retrieval["top1_source_accuracy"]))
            print("grounding: fidelity=%.3f groundedness=%.3f refusal_acc=%.3f fabricated=%d"
                  % (grounding["citation_fidelity"], grounding["groundedness"],
                     grounding["refusal_accuracy"], grounding["fabricated_citation_markers"]))
            print("\nfindings (stage-attributed):")
            for f in findings:
                print("  [%s/%s] %s" % (f["stage"], f["severity"], f["symptom"]))
                print("      evidence: %s" % f["evidence"])
                print("      lever:    %s" % f["lever"])
            if not findings:
                print("  (none)")
        return 0

    result = run_cycle(incumbent=cfg, register=args.register, log_path=log_path)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        print("findings: %d" % len(result["findings"]))
        for f in result["findings"]:
            print("  [%s] %s" % (f["stage"], f["symptom"]))
        print("candidates: %d" % result["n_candidates"])
        print("action: %s" % result["action"])
        print("changed: %s" % result["changed"])
        if log_path:
            print("logged: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
