#!/usr/bin/env python3
"""rag_skill.py — the `builderbro-rag` skill's single entry point.

Contract for a calling agent (see SKILL.md for the full definition):

    python3 .agents/skills/builderbro-rag/rag_skill.py <command> [args] [--json|--human]

Every command prints exactly one JSON object on stdout when `--json` is used
(the default), so a caller parses a result instead of scraping prose. Every
response carries `ok: true|false`, and every failure carries `error` plus a
machine-readable `reason` — a caller can branch on `ok` without pattern-matching
strings, which is the whole point of exposing RAG as a tool rather than as a
paragraph of instructions.

Commands
    build                       rebuild the index from the corpus
    search  <query>  [-k N]     hybrid retrieval only (no model call)
    ask     <question> [-k N]   retrieve -> confidence gate -> generate -> audit
    eval    [--generator G]     measured retrieval + grounding report
    diagnose [--cycle] [--register]  stage-attributed findings; optionally run
                                a full self-building cycle
    contract                    print the interface contract as JSON

`ok` means "the grounding verdict passed", not "the command ran". A refusal is
`ok: true` with `refused: true` — refusing correctly is a success, and a caller
that treats refusal as an error will learn to distrust the gate that prevents
hallucination.

Generators for `ask`:
    live        the brain cascade (local server, then free providers)
    extractive  copies real source sentences; hermetic, no network, no keys
    hallucinate adversarial self-test: fabricates on purpose, must be rejected
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
for _p in (PROJECT_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rag_core      # noqa: E402
import rag_diagnose  # noqa: E402
import rag_eval      # noqa: E402


def _load_index(env=None):
    """Load the saved index, or build one on the fly. Never raises for a missing
    index — a caller that has not run `build` still gets an answer."""
    index = rag_core.RagIndex.load(env=env)
    if index is None:
        index = rag_core.build_index(env=env, save=True)
    return index


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_build(args, env=None):
    index = rag_core.build_index(env=env)
    path = index.save(env=env)
    meta = index.corpus_meta or {}
    return {
        "ok": True,
        "command": "build",
        "index_path": path,
        "files": len(meta.get("files", [])),
        "chunks": len(index.chunks),
        "bytes": meta.get("total_bytes", 0),
        "skipped": len(meta.get("skipped", [])),
        "config": {k: index.config[k] for k in
                   ("fusion", "chunk_budget_words", "chunk_overlap_words",
                    "dense_weight", "min_query_support")},
    }


def cmd_search(args, env=None):
    index = _load_index(env)
    hits = index.search(args.query, k=args.k)
    if not hits:
        return {"ok": True, "command": "search", "query": args.query,
                "n_hits": 0, "hits": [], "note": "no query or corpus terms matched"}
    return {
        "ok": True,
        "command": "search",
        "query": args.query,
        "k": args.k or index.config["top_k"],
        "n_hits": len(hits),
        "hits": [{
            "rank": h["rank"],
            # The citation a generated answer must use to point at this chunk.
            "citation": "[%d]" % h["rank"],
            "chunk_id": h["chunk"]["id"],
            "source": h["chunk"]["source"],
            "heading": h["chunk"]["heading"],
            "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]],
            "score": round(h["score"], 6),
            "channels": ("both" if h["both"] else
                         "lexical" if h["lexical_only"] else
                         "dense" if h["dense_only"] else "neither"),
            "text": h["chunk"]["text"],
        } for h in hits],
    }


def _generator(name):
    if name == "extractive":
        return rag_eval.ExtractiveGenerator()
    if name == "hallucinate":
        return rag_eval.HallucinatingGenerator()
    if name == "live":
        return rag_eval.LiveGenerator()
    raise ValueError("unknown generator %r" % name)


def cmd_ask(args, env=None):
    index = _load_index(env)
    hits = index.search(args.query, k=args.k)
    cfg = index.config
    support = rag_core.query_support(args.query, hits, index=index, config=cfg)
    if not hits:
        return {"ok": True, "command": "ask", "refused": True, "answer": rag_core.REFUSAL_TOKEN,
                "citation_fidelity": 1.0, "query_support": 0.0,
                "reasons": ["no_hits"], "sources": [],
                "note": "the corpus returned nothing for this query"}
    try:
        generate = _generator(args.generator)
    except ValueError as e:
        return {"ok": False, "command": "ask", "error": "bad_generator", "reason": str(e)}
    try:
        audit = rag_core.answer(args.query, hits, generate, cfg, index=index)
    except Exception as e:  # a dead brain must be a structured failure, not a crash
        return {"ok": False, "command": "ask", "error": "generation_failed",
                "reason": "%s: %s" % (type(e).__name__, e), "query_support": round(support, 4)}

    return {
        # ok = the HARD gate. Fabrication is exact and decidable: a citation
        # either resolves to a retrieved chunk or it is invented. The entailment
        # proxy is advisory (see strict_verdict / warnings) because it penalises
        # correct paraphrase, and failing release on it would make the gate
        # unusable on real model output.
        "ok": audit["verdict"] == "grounding:ok",
        "command": "ask",
        "generator": args.generator,
        "question": args.query,
        "refused": audit["refused"],
        "answer": audit["answer"],
        "citations": audit["citations"],
        "invalid_citations": audit["invalid_citations"],
        "citation_fidelity": audit["citation_fidelity"],
        "groundedness": audit["groundedness"],
        "unsupported_rate": audit["unsupported_rate"],
        "claims": audit["claims"],
        "query_support": audit.get("query_support"),
        "verdict": audit["verdict"],
        "strict_verdict": audit["strict_verdict"],
        "warnings": audit["soft_reasons"],
        "reasons": audit["reasons"],
        "sources": [{
            "citation": "[%d]" % h["rank"],
            "source": h["chunk"]["source"],
            "heading": h["chunk"]["heading"],
            "span": [h["chunk"]["char_start"], h["chunk"]["char_end"]],
            "chunk_id": h["chunk"]["id"],
        } for h in hits],
    }


def cmd_eval(args, env=None):
    config = rag_core.active_config(env)
    index = rag_core.build_index(config=config, env=env)
    generate = None
    name = "none"
    if args.generator != "none":
        generate = _generator(args.generator)
        name = {"extractive": "extractive (reference)",
                "hallucinate": "hallucinating (adversarial control)",
                "live": "live brain cascade"}[args.generator]
    report = rag_eval.evaluate(index, generate, k=args.k, config=config,
                               generator_name=name)
    checks = report["checks"]
    return {
        # ok = every pre-registered floor met.
        "ok": all(c["pass"] for c in checks.values()),
        "command": "eval",
        "config": {k: config[k] for k in
                   ("fusion", "chunk_budget_words", "chunk_overlap_words",
                    "dense_weight", "min_query_support")},
        "retrieval": {k: report["retrieval"][k] for k in
                      ("k", "recall_at_k", "mrr", "precision_at_k",
                       "top1_source_accuracy", "attribution", "n_scored")},
        "span_coverage": report["coverage"]["coverage"],
        "missing_phrases": report["coverage"]["missing_phrases"],
        "grounding": (None if report["grounding"] is None else {
            k: report["grounding"][k] for k in
            ("generator", "citation_fidelity", "groundedness", "unsupported_rate",
             "verdict_pass_rate", "strict_pass_rate", "fabricated_citation_markers",
             "false_answer_rate", "false_refusal_rate", "refusal_accuracy")
        }),
        "checks": checks,
        "markdown": rag_eval.render_markdown(report["retrieval"], report["coverage"],
                                            report["grounding"], report["checks"]),
    }


def cmd_diagnose(args, env=None):
    config = rag_core.active_config(env)
    index = rag_core.build_index(config=config, env=env)
    coverage = rag_eval.span_coverage(index.chunks)
    retrieval = rag_eval.run_retrieval_eval(index, config=config)
    grounding = rag_eval.run_grounding_eval(index, rag_eval.ExtractiveGenerator(),
                                           config=config,
                                           name="extractive (reference)")
    findings = rag_diagnose.attribute(index, retrieval, coverage, grounding)
    out = {
        "ok": True,
        "command": "diagnose",
        "findings": findings,
        "n_findings": len(findings),
        "stages": sorted({f["stage"] for f in findings}),
        "incumbent": {
            "span_coverage": round(coverage["coverage"], 4),
            "recall_at_k": round(retrieval["recall_at_k"], 4),
            "mrr": round(retrieval["mrr"], 4),
            "refusal_accuracy": round(grounding["refusal_accuracy"], 4),
            "false_answer_rate": round(grounding["false_answer_rate"], 4),
        },
    }
    if args.cycle:
        cycle = rag_diagnose.run_cycle(
            incumbent=config, generate=rag_eval.ExtractiveGenerator(),
            log_path=None if args.no_log else rag_diagnose.SELF_IMPROVEMENT_LOG,
            register=args.register)
        out["cycle"] = {
            "n_candidates": cycle["n_candidates"],
            "best": cycle["best"],
            "support_chosen": cycle["support_chosen"],
            "changed": cycle["changed"],
            "action": cycle["action"],
        }
        # A cycle that found nothing to change is still a successful diagnosis.
        out["ok"] = True
    return out


CONTRACT = {
    "skill": "builderbro-rag",
    "version": "1.0",
    "entry_point": "python3 .agents/skills/builderbro-rag/rag_skill.py <command> [args]",
    "output": "one JSON object on stdout; `ok` is always present",
    "commands": {
        "build": {"args": [], "returns": ["index_path", "files", "chunks", "config"]},
        "search": {"args": ["<query>", "-k N"], "returns": ["hits[].citation", "hits[].source", "hits[].span", "hits[].text"]},
        "ask": {"args": ["<question>", "-k N", "--generator live|extractive|hallucinate"],
                "returns": ["ok", "refused", "answer", "citations", "citation_fidelity",
                            "groundedness", "query_support", "verdict", "strict_verdict",
                            "warnings", "reasons", "sources"]},
        "eval": {"args": ["--generator G"], "returns": ["ok", "retrieval", "span_coverage", "grounding", "checks"]},
        "diagnose": {"args": ["--cycle", "--register"], "returns": ["findings[].stage", "findings[].lever", "cycle"]},
        "contract": {"args": [], "returns": ["commands"]},
    },
    "semantics": {
        "ok": "the HARD grounding gate passed (no fabricated or missing citations); for build/eval/diagnose it means the operation succeeded / floors were met",
        "strict_verdict": "hard gate AND the lexical-entailment proxy; stricter, advisory, and reported separately because the proxy penalises correct paraphrase",
        "warnings": "soft findings (e.g. unsupported_rate) that did not fail the gate",
        "refused": "the corpus cannot answer; ok is still true — refusal is a correct outcome",
        "citation": "an [n] marker in `answer` maps to sources[n-1], whose span is a real char range in a real file",
        "failure": "ok=false always carries `error` and `reason`",
    },
    "limits": [
        "Retrieval is lexical (BM25) fused with a non-semantic hashing vectoriser; a pure paraphrase with no shared vocabulary will not be retrieved.",
        "Grounding support is a lexical-entailment proxy and can be fooled by a sentence that reuses vocabulary while inverting meaning.",
        "The confidence gate was calibrated on 3 negative examples; its margin is narrow (min positive support 0.557 vs max negative 0.530).",
    ],
}


def cmd_contract(args, env=None):
    out = dict(CONTRACT)
    out["ok"] = True
    out["command"] = "contract"
    return out


def run(argv, env=None):
    """Programmatic entry point used by the autonomy agent's `rag` tool."""
    args = _parse(argv)
    env = env if env is not None else os.environ
    if args.command == "build":
        return cmd_build(args, env)
    if args.command == "search":
        if not args.query:
            return {"ok": False, "command": "search", "error": "missing_argument",
                    "reason": "search needs a query"}
        return cmd_search(args, env)
    if args.command == "ask":
        if not args.query:
            return {"ok": False, "command": "ask", "error": "missing_argument",
                    "reason": "ask needs a question"}
        return cmd_ask(args, env)
    if args.command == "eval":
        return cmd_eval(args, env)
    if args.command == "diagnose":
        return cmd_diagnose(args, env)
    if args.command == "contract":
        return cmd_contract(args, env)
    return {"ok": False, "command": args.command or None, "error": "unknown_command",
            "reason": "command must be one of build|search|ask|eval|diagnose|contract"}


def _parse(argv):
    p = argparse.ArgumentParser(prog="rag_skill", add_help=False)
    p.add_argument("command", nargs="?", default=None)
    p.add_argument("query", nargs="?", default=None)
    p.add_argument("extra", nargs="*")
    p.add_argument("-k", type=int, default=None)
    # No `choices=` here on purpose: argparse answers a bad value by calling
    # sys.exit(2) with no JSON, which breaks this skill's own contract that every
    # failure is a structured {ok:false, error, reason}. Validation happens in
    # _generator() so a bad value comes back as data instead of a process exit.
    p.add_argument("--generator", default="extractive")
    p.add_argument("--cycle", action="store_true")
    p.add_argument("--register", action="store_true")
    p.add_argument("--no-log", action="store_true")
    p.add_argument("--human", action="store_true")
    p.add_argument("--json", action="store_true")
    args, unknown = p.parse_known_args(argv)
    # Allow `search "some query"` as one argument, and tolerate stray flags.
    if args.query is None and args.extra:
        args.query = args.extra[0]
    elif args.query and args.extra and args.command in ("search", "ask"):
        args.query = " ".join([args.query] + args.extra)
    args.unknown = unknown
    return args


def _human(out):
    cmd = out.get("command")
    if cmd == "search":
        lines = ["%s %s  %s  [%d,%d]"
                 % (h["citation"], h["source"], h["channels"], h["span"][0], h["span"][1])
                 for h in out.get("hits", [])]
        return "\n".join(lines) if lines else "(no hits)"
    if cmd == "ask":
        head = "REFUSED" if out.get("refused") else "ANSWERED"
        warn = "  warnings=%s" % out["warnings"] if out.get("warnings") else ""
        return ("%s  verdict=%s strict=%s support=%s fidelity=%s%s\n\n%s"
                % (head, out.get("verdict"), out.get("strict_verdict"),
                   out.get("query_support"), out.get("citation_fidelity"), warn,
                   out.get("answer", "")))
    if cmd == "diagnose":
        lines = ["%s" % out.get("action")] if "cycle" in out else []
        for f in out.get("findings", []):
            lines.append("[%s/%s] %s\n    evidence: %s\n    lever: %s"
                         % (f["stage"], f["severity"], f["symptom"], f["evidence"], f["lever"]))
        return "\n".join(lines) if lines else "(no findings)"
    if cmd == "eval":
        return out.get("markdown", "")
    if cmd == "build":
        return "indexed %d chunks from %d files -> %s" % (
            out.get("chunks", 0), out.get("files", 0), out.get("index_path"))
    return json.dumps(out, indent=1, sort_keys=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse(argv)
    if args.human:
        pass
    out = run(argv)
    if getattr(args, "human", False):
        print(_human(out))
    else:
        print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
