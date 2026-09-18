"""rag_eval.py — measurement for the RAG core. Nothing here generates prose
that anyone should read; its whole job is to produce numbers that can fail.

Two independent things are measured, deliberately separated so a bad answer is
never blamed on the wrong stage:

**Retrieval stage** (no model involved, fully deterministic)
  - `span_coverage`  — does the gold text exist inside a *single* chunk at all?
    Below 1.0 this is a **chunking** fault: the text was split across a boundary
    or dropped. No retrieval algorithm can recover text that no chunk contains,
    so this must be checked before blaming the ranker.
  - `recall_at_k`    — is at least one gold-bearing chunk in the top k?
  - `precision_at_k` — how much of the top k is gold-bearing (noise proxy)?
  - `mrr`            — reciprocal rank of the first gold-bearing chunk; punishes
    putting the right chunk at rank 5 behind four plausible ones.
  - `attribution`    — which channel found it (both / lexical-only / dense-only).
    If dense-only is ~0, the hashed-dense channel is not earning its weight and
    that is a measurable reason to downweight or remove it.

**Grounding stage** (exact answer text audited against the retrieved chunks)
  - `citation_fidelity`     — share of citation markers that point at a real
    retrieved source (1 - fabricated-citation rate).
  - `unsupported_rate`      — share of factual sentences whose content words are
    not substantially present in the source they cite.
  - `groundedness`          — 1 - unsupported_rate.
  - `refusal_accuracy`      — on questions the corpus cannot answer, did the
    system say so instead of inventing an answer? This is the headline
    hallucination-resistance number: a system that never refuses cannot be
    trustworthy on the questions it *should* answer.

Reference generators (no network, reproducible):
  - `ExtractiveGenerator`   — copies real source sentences and cites them. A
    competent-but-unimaginative generator. If the audit flags this, the audit
    has false positives and its numbers are worthless.
  - `HallucinatingGenerator`— fabricates a citation index and asserts claims
    with no support. If the audit passes this, it has false negatives.
Together they bracket the auditor: it must pass the honest one and fail the
dishonest one. `--live` swaps in the real brain cascade for a real reading.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import rag_core

# ── Golden set ────────────────────────────────────────────────────────────────
# Every `gold_phrases` entry was verified present in the named source with grep
# before being written here. A phrase that does NOT survive into a chunk shows up
# as span_coverage < 1.0 and is reported as a chunking fault.

GOLDEN_ITEMS = [
    {"id": "rag-g01", "question": "What is the drive_sync tool allowed to do?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["drive_sync"]},
    {"id": "rag-g02", "question": "Which HTTP status code did Cloudflare's edge return for the requests?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["1010"]},
    {"id": "rag-g03", "question": "What error code did Cerebras return when it refused to serve requests?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["payment_required"]},
    {"id": "rag-g04", "question": "How many continuous cycles must the drift test complete?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["1,000 continuous cycles"]},
    {"id": "rag-g05", "question": "What escape prevention rate is required in the isolation suite?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["100% prevention required"]},
    {"id": "rag-g06", "question": "Which law governs subjective time in the QIH model?",
     "source": "QIH.md", "gold_phrases": ["phase-clock"]},
    {"id": "rag-g07", "question": "What provides the persistence layer for continuity across traversal events?",
     "source": "QIH.md", "gold_phrases": ["sovereign continuity"]},
    {"id": "rag-g08", "question": "Which test confirms that bit probabilities match the orientation angle laws?",
     "source": "QIH.md", "gold_phrases": ["Born Rule"]},
    {"id": "rag-g09", "question": "What is the brain called in the QIH core identity?",
     "source": "QIH.md", "gold_phrases": ["Geometric Weaver"]},
    {"id": "rag-g10", "question": "Which functional signals a stabilised single branch of reality?",
     "source": "QIH.md", "gold_phrases": ["coherence functional"]},
    {"id": "rag-g11", "question": "How does the drift loop detect that a dispatch graph has changed?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["dispatch-graph hashing"]},
    {"id": "rag-g12", "question": "How does the loop survive a crash part way through a run?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["resume-on-crash"]},
    {"id": "rag-g13", "question": "What does the ledger record for a write that no gate verified?",
     "source": "AGENT-INTEGRITY.md", "gold_phrases": ["ungated"]},
    {"id": "rag-g14", "question": "Why did the Python client get blocked by the provider's edge?",
     "source": "FREE-BRAIN.md", "gold_phrases": ["User-Agent"]},
    # Negative controls: the corpus cannot answer these, so the correct behaviour
    # is an explicit refusal. A system scored only on answerable questions can
    # win by always asserting something.
    {"id": "rag-n01", "question": "What was the weather in Lisbon during the P0 benchmark run?",
     "source": None, "gold_phrases": [], "expect_no_answer": True},
    {"id": "rag-n02", "question": "Who is the current chief executive of Cerebras Systems?",
     "source": None, "gold_phrases": [], "expect_no_answer": True},
    {"id": "rag-n03", "question": "How much does a Kubernetes cluster cost per month?",
     "source": None, "gold_phrases": [], "expect_no_answer": True},
]

# ── Reference generators ──────────────────────────────────────────────────────

SOURCE_BLOCK_RE = re.compile(r"^SOURCE \[(\d+)\] .*?$", re.M)


def _parse_context(messages):
    """Recover the numbered sources from the grounded prompt, exactly as a model
    would read them."""
    text = messages[-1]["content"]
    marks = list(SOURCE_BLOCK_RE.finditer(text))
    out = []
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        body = text[m.end():end]
        body = body.split("QUESTION:", 1)[0]
        out.append((int(m.group(1)), body.strip()))
    return out


def _question_of(messages):
    return messages[-1]["content"].rsplit("QUESTION:", 1)[-1].strip()


class ExtractiveGenerator:
    """An honest-but-literal generator: it copies sentences that actually appear
    in the sources and cites the source they came from. It is the auditor's
    false-positive control — every output here *is* grounded, so any flag the
    audit raises against it is the audit's bug."""

    def __init__(self, max_sentences=3):
        self.max_sentences = max_sentences

    def __call__(self, messages):
        question = _question_of(messages)
        q_words = rag_core.content_words(question)
        scored = []
        for n, body in _parse_context(messages):
            for sentence in rag_core.split_sentences(body):
                s_words = rag_core.content_words(sentence)
                if len(s_words) < 4:
                    continue
                # Only prose is eligible: a copied JSON blob or table row is not
                # a claim, and emitting one would measure the corpus format
                # rather than the generator's grounding.
                if not rag_core.is_prose_like(sentence):
                    continue
                overlap = len(q_words & s_words)
                if overlap == 0:
                    continue
                scored.append((overlap / float(max(1, len(q_words))), n, sentence))
        if not scored:
            return rag_core.REFUSAL_TOKEN
        scored.sort(key=lambda t: (-t[0], t[1]))
        lines = []
        for _, n, sentence in scored[: self.max_sentences]:
            clean = sentence.strip().rstrip(".!?")
            # Marker goes *before* the terminal period. 'claim. [2]' is legal
            # English but it makes the sentence/citation pairing ambiguous for
            # any splitter, including this harness's.
            lines.append("%s [%d]." % (clean, n))
        return " ".join(lines)


class HallucinatingGenerator:
    """The negative control. It cites a source that was never retrieved and
    asserts a specific number that appears nowhere. The audit must reject it."""

    def __init__(self, fake_index=99):
        self.fake_index = fake_index

    def __call__(self, messages):
        return (
            "The P0 benchmark ran on a 12-core Ryzen with 64 GB of DDR5 memory [%d]. "
            "Throughput reached 412 tokens per second [%d]. "
            "The project is funded by a Series B round led by Sequoia Capital [%d]."
            % (self.fake_index, self.fake_index, self.fake_index)
        )


class LiveGenerator:
    """The real brain, through the cascade. Imported lazily so this module stays
    usable with no credentials and no server.

    `load_env_file()` is called here because this class is the CLI boundary for
    the live path: provider credentials live in `.env`, and nothing else in the
    RAG modules reads it (so the hermetic paths stay hermetic). Without this the
    cascade degenerates to the local server alone and every live call fails with
    'cascade exhausted (1 providers)' even though keys exist."""

    def __init__(self):
        import agent_runtime
        agent_runtime.load_env_file()
        self.config = agent_runtime.load_config()

    def __call__(self, messages):
        import agent_runtime
        return agent_runtime.chat(self.config, messages).get("content", "")


# ── Retrieval evaluation ──────────────────────────────────────────────────────

def _phrases_in(text, phrases):
    return [p for p in phrases if p in text]


def span_coverage(chunks):
    """Is each gold phrase present inside a single chunk? Reported separately
    because text split across a chunk boundary is a chunking fault that no
    ranker can fix."""
    rows = []
    for item in GOLDEN_ITEMS:
        if item.get("expect_no_answer"):
            continue
        missing = []
        for phrase in item["gold_phrases"]:
            if not any(phrase in c["text"] for c in chunks):
                missing.append(phrase)
        rows.append({"id": item["id"], "phrases": list(item["gold_phrases"]),
                     "missing": missing})
    covered = sum(1 for r in rows if not r["missing"])
    return {
        "coverage": (covered / float(len(rows))) if rows else 1.0,
        "items": rows,
        "missing_phrases": [p for r in rows for p in r["missing"]],
    }


def run_retrieval_eval(index, k=None, config=None):
    cfg = config or index.config
    k = k or cfg["top_k"]
    per_item = []
    attribution = {"both": 0, "lexical_only": 0, "dense_only": 0, "neither": 0}
    for item in GOLDEN_ITEMS:
        hits = index.search(item["question"], k=k, config=cfg)
        if item.get("expect_no_answer"):
            # Recorded for completeness but excluded from retrieval scoring:
            # there is no gold chunk to find.
            per_item.append({"id": item["id"], "expect_no_answer": True,
                             "top_k": [h["chunk"]["source"] for h in hits]})
            continue
        ranks_of_gold, sources_of_gold = [], []
        for h in hits:
            if _phrases_in(h["chunk"]["text"], item["gold_phrases"]):
                ranks_of_gold.append(h["rank"])
                sources_of_gold.append(h["chunk"]["source"])
                channel = ("both" if h["both"] else
                           "lexical_only" if h["lexical_only"] else
                           "dense_only" if h["dense_only"] else "neither")
                attribution[channel] += 1
        first = min(ranks_of_gold) if ranks_of_gold else None
        per_item.append({
            "id": item["id"],
            "question": item["question"],
            "gold_source": item["source"],
            "first_gold_rank": first,
            "mrr": (1.0 / first) if first else 0.0,
            "recall": 1.0 if first else 0.0,
            "precision": len(ranks_of_gold) / float(max(1, len(hits))),
            "top_k_sources": [h["chunk"]["source"] for h in hits],
            "top1_source": hits[0]["chunk"]["source"] if hits else None,
            "source_hit": (item["source"] in sources_of_gold) if item["source"] else None,
            "top1_correct_source": (hits[0]["chunk"]["source"] == item["source"]) if hits else False,
        })
    scored = [r for r in per_item if "recall" in r]
    n = max(1, len(scored))
    return {
        "k": k,
        "fusion": cfg.get("fusion"),
        "chunk_budget_words": cfg.get("chunk_budget_words"),
        "chunk_overlap_words": cfg.get("chunk_overlap_words"),
        "items": per_item,
        "recall_at_k": sum(r["recall"] for r in scored) / n,
        "mrr": sum(r["mrr"] for r in scored) / n,
        "precision_at_k": sum(r["precision"] for r in scored) / n,
        "top1_source_accuracy": sum(1 for r in scored if r["top1_correct_source"]) / n,
        "attribution": attribution,
        "n_scored": len(scored),
    }


# ── Grounding evaluation ──────────────────────────────────────────────────────

def run_grounding_eval(index, generate, k=None, config=None, name="generator"):
    cfg = config or index.config
    k = k or cfg["top_k"]
    rows = []
    for item in GOLDEN_ITEMS:
        hits = index.search(item["question"], k=k, config=cfg)
        audit = rag_core.answer(item["question"], hits, generate, cfg, index=index)
        expect_none = bool(item.get("expect_no_answer"))
        row = {
            "id": item["id"],
            "expect_no_answer": expect_none,
            "verdict": audit["verdict"],
            "strict_verdict": audit["strict_verdict"],
            "reasons": audit["reasons"],
            "hard_reasons": audit["hard_reasons"],
            "soft_reasons": audit["soft_reasons"],
            "citation_fidelity": audit["citation_fidelity"],
            "citation_markers": audit["citation_markers"],
            "invalid_citations": audit["invalid_citations"],
            "groundedness": audit["groundedness"],
            "unsupported_rate": audit["unsupported_rate"],
            "claims": audit["claims"],
            "refused": audit["refused"],
            "query_support": audit.get("query_support"),
            "answer": audit["answer"][:300],
        }
        row["refusal_correct"] = (audit["refused"] == expect_none)
        rows.append(row)

    answered = [r for r in rows if not r["expect_no_answer"]]
    negatives = [r for r in rows if r["expect_no_answer"]]
    n = max(1, len(answered))
    fabricated_items = [r["id"] for r in rows if r["invalid_citations"]]
    fabricated_markers = sum(len(r["invalid_citations"]) for r in rows)
    return {
        "generator": name,
        "items": rows,
        "citation_fidelity": sum(r["citation_fidelity"] for r in answered) / n,
        "groundedness": sum(r["groundedness"] for r in answered) / n,
        "unsupported_rate": sum(r["unsupported_rate"] for r in answered) / n,
        # hard gate only: fabricated / missing citations
        "verdict_pass_rate": sum(1 for r in answered if r["verdict"] == "grounding:ok") / n,
        # hard gate AND the lexical-entailment proxy: the stricter reading, kept
        # separate so the paraphrase penalty is visible rather than averaged in
        "strict_pass_rate": sum(1 for r in answered
                                if r["strict_verdict"] == "grounding:ok") / n,
        "fabricated_citation_items": fabricated_items,
        "fabricated_citation_markers": fabricated_markers,
        "false_answer_rate": (
            sum(1 for r in negatives if not r["refused"]) / float(max(1, len(negatives)))
        ),
        # The other half of refusal measurement: refusing a question the corpus
        # CAN answer is also a failure. Optimising only false-answer rate is
        # trivially gamed by refusing everything.
        "false_refusal_rate": (
            sum(1 for r in answered if r["refused"]) / float(max(1, len(answered)))
        ),
        "refusal_accuracy": (
            sum(1 for r in rows if r["refusal_correct"]) / float(max(1, len(rows)))
        ),
        "n_answered": len(answered),
        "n_negative": len(negatives),
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

FLOORS = {
    # Pre-registered, matching FREE-BRAIN.md's style: the number is chosen
    # before the run and a miss is reported, not tuned away.
    "recall_at_k": 0.85,
    "mrr": 0.70,
    "span_coverage": 1.0,
    "citation_fidelity": 0.95,
    "refusal_accuracy": 1.0,
    "false_refusal_rate": 0.0,
    "top1_source_accuracy": 0.60,
}


def verdicts(retrieval, coverage, grounding):
    out = {}
    for key in ("recall_at_k", "mrr", "top1_source_accuracy"):
        got = retrieval[key]
        out[key] = {"got": round(got, 4), "floor": FLOORS[key], "pass": got >= FLOORS[key]}
    got = coverage["coverage"]
    out["span_coverage"] = {"got": round(got, 4), "floor": FLOORS["span_coverage"],
                            "pass": got >= FLOORS["span_coverage"]}
    if grounding:
        # The gate is the hard layer; the entailment proxy is reported by
        # strict_pass_rate and deliberately does not fail the release.
        for key in ("citation_fidelity", "refusal_accuracy"):
            got = grounding[key]
            out[key] = {"got": round(got, 4), "floor": FLOORS[key], "pass": got >= FLOORS[key]}
        # A rate that must be *low* rather than high.
        got = grounding["false_refusal_rate"]
        out["false_refusal_rate"] = {"got": round(got, 4), "floor": FLOORS["false_refusal_rate"],
                                     "pass": got <= FLOORS["false_refusal_rate"]}
    return out


def render_markdown(retrieval, coverage, grounding, checks):
    lines = []
    lines.append("### Retrieval (no model involved)")
    lines.append("")
    lines.append("| metric | value | floor | pass |")
    lines.append("| --- | --- | --- | --- |")
    for key in ("span_coverage", "recall_at_k", "precision_at_k", "mrr", "top1_source_accuracy"):
        if key == "span_coverage":
            got = coverage["coverage"]
        elif key == "precision_at_k":
            got = retrieval["precision_at_k"]
        else:
            got = retrieval[key]
        floor = FLOORS.get(key)
        c = checks.get(key)
        lines.append("| %s | %.4f | %s | %s |"
                     % (key, got, ("%.2f" % floor) if floor is not None else "-",
                        ("**PASS**" if c["pass"] else "**FAIL**") if c else "n/a"))
    lines.append("| window | %s | chunk=%d overlap=%d | |"
                 % (retrieval["fusion"], retrieval["chunk_budget_words"],
                    retrieval["chunk_overlap_words"]))
    lines.append("")
    a = retrieval["attribution"]
    lines.append("Channel attribution (top-%d gold hits): both=%d lexical_only=%d dense_only=%d neither=%d"
                 % (retrieval["k"], a["both"], a["lexical_only"], a["dense_only"], a["neither"]))
    if coverage["missing_phrases"]:
        lines.append("")
        lines.append("Chunking faults — gold text absent from every chunk: `%s`"
                     % "`, `".join(coverage["missing_phrases"]))
    if grounding:
        lines.append("")
        lines.append("### Grounding (%s)" % grounding["generator"])
        lines.append("")
        lines.append("| metric | value |")
        lines.append("| --- | --- |")
        lines.append("| citation fidelity | %.4f |" % grounding["citation_fidelity"])
        lines.append("| groundedness | %.4f |" % grounding["groundedness"])
        lines.append("| unsupported claim rate | %.4f |" % grounding["unsupported_rate"])
        lines.append("| hard-gate pass rate (fabrication only) | %.4f |" % grounding["verdict_pass_rate"])
        lines.append("| strict pass rate (hard + entailment proxy) | %.4f |" % grounding["strict_pass_rate"])
        lines.append("| fabricated citation markers | %d |" % grounding["fabricated_citation_markers"])
        lines.append("| false-answer rate on unanswerable questions | %.4f |" % grounding["false_answer_rate"])
        lines.append("| false-refusal rate on answerable questions | %.4f |" % grounding["false_refusal_rate"])
        lines.append("| refusal accuracy | %.4f |" % grounding["refusal_accuracy"])
    failed = [k for k, v in checks.items() if not v["pass"]]
    lines.append("")
    lines.append("**Gate:** %s" % ("all measured floors met" if not failed
                                   else "FAILED — " + ", ".join(failed)))
    return "\n".join(lines)


def evaluate(index, generate=None, k=None, config=None, generator_name="extractive"):
    coverage = span_coverage(index.chunks)
    retrieval = run_retrieval_eval(index, k=k, config=config)
    grounding = None
    if generate is not None:
        grounding = run_grounding_eval(index, generate, k=k, config=config,
                                       name=generator_name)
    checks = verdicts(retrieval, coverage, grounding)
    return {"retrieval": retrieval, "coverage": coverage, "grounding": grounding,
            "checks": checks}


def main(argv=None):
    p = argparse.ArgumentParser(description="Measure RAG retrieval + grounding")
    p.add_argument("-k", type=int, default=None)
    p.add_argument("--fusion", choices=("linear", "rrf", "bm25"), default=None)
    p.add_argument("--budget", type=int, default=None, help="chunk budget words")
    p.add_argument("--overlap", type=int, default=None, help="chunk overlap words")
    p.add_argument("--generator", choices=("extractive", "hallucinating", "live", "none"),
                   default="extractive")
    p.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    args = p.parse_args(argv)

    # Start from the *registered* config so the CLI reports what the runtime
    # actually does, not what the defaults were before the last tuning cycle.
    config = rag_core.active_config()
    if args.fusion:
        config["fusion"] = args.fusion
    if args.budget:
        config["chunk_budget_words"] = args.budget
    if args.overlap is not None:
        config["chunk_overlap_words"] = args.overlap

    index = rag_core.build_index(config=config)
    generate = None
    name = "none"
    if args.generator == "extractive":
        generate, name = ExtractiveGenerator(), "extractive (reference)"
    elif args.generator == "hallucinating":
        generate, name = HallucinatingGenerator(), "hallucinating (adversarial control)"
    elif args.generator == "live":
        generate, name = LiveGenerator(), "live brain cascade"

    report = evaluate(index, generate, k=args.k, config=config, generator_name=name)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0
    print(render_markdown(report["retrieval"], report["coverage"],
                          report["grounding"], report["checks"]))
    return 0 if all(c["pass"] for c in report["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
