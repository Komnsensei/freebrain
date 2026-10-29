# FreeBrain + BuilderBro Worker

## Purpose

This worker joins FreeBrain orchestration to BuilderBro's existing verification,
provenance-memory, grounded-retrieval, and evidence-audit tools. It deliberately
does not copy or reimplement those policies. BuilderBro remains the source of
truth for each check.

## Current source-of-truth repositories

- FreeBrain (GitLab canonical integration target): https://gitlab.com/Komnsensei/freebrain
- FreeBrain (GitHub mirror/work surface): https://github.com/Komnsensei/freebrain
- BuilderBro CLI and MCP implementation: https://github.com/Komnsensei/builderbro-cli
- Earlier BuilderBRO app repository: https://github.com/Komnsensei/builderBRO
- Video renderer and MP4 validation: https://github.com/Komnsensei/AI_Creative_Studio
- Evidence verification: https://gitlab.com/komnsensei-group/Verifyd
- Omega run ledger: https://gitlab.com/komnsensei-group/bunny-log-stream

The builderbro-cli repository is the implementation to integrate first: it
contains the Python verifier, provenance-gated memory, evidence hygiene
classifier, grounded RAG skill, and builderbro_mcp.py. The smaller builderBRO
repository is an older separate app and is not treated as the canonical
verification engine.

## Worker contract

builderbro_worker.py provides a narrow CLI boundary:

- python builderbro_worker.py --status
- python builderbro_worker.py --call rag_ask --json '{"question":"..."}'
- python builderbro_worker.py --call verify_claim --json '{"tool":"...","output":"...","expect":"contains:..."}'

Set BUILDERBRO_ROOT to the local builderbro-cli checkout, or pass
--builderbro-root. By default the worker expects builderbro-cli beside the
FreeBrain checkout. It invokes the existing builderbro_mcp.py using an argument
array, shell=False, a timeout, a 1 MB output limit, and a restricted child
environment. It never accepts arbitrary tool names or arbitrary commands.

The worker's RAG mode is extractive-only. generator=live is rejected so this
integration cannot accidentally spend a hosted-model budget. Missing checkout,
unknown tool, malformed response, timeout, and subprocess failure are fail-closed.

Allowlisted BuilderBro tools:
verify_claim, memory_recall, memory_record, rag_ask, evidence_audit.

## Integration sequence

1. FreeBrain calls the worker for grounded retrieval and verification.
2. FreeBrain proposes work; BuilderBro checks claims, records provenance, and audits evidence.
3. Video generation remains behind FREEBRAIN_ALLOW_VIDEO_GENERATION=1, stable
   idempotency keys, and the zero-cost provider preflight.
4. The real MP4 and its exact SHA-256/manifest are passed through Omega and Verifyd.
5. Sentinel records governed events only through its explicit authenticated adapter.
6. End-to-end status is reported only when the actual render and all evidence receipts verify.

This worker is an initial local integration layer, not proof that the full
FreeBrain → video renderer → Omega → Verifyd → Sentinel chain is deployed or live.
No tokens, keys, or provider secrets belong in this repository.
