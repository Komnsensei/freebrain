---
name: builderbro-rag
description: Grounded retrieval over the BuilderBro corpus. Answers questions from FREE-BRAIN.md, AGENT-INTEGRITY.md, QIH.md and the residences, with machine-checked citations, or refuses when the corpus cannot answer. Also measures and self-diagnoses its own retrieval quality.
version: 1.0
entry_point: python3 .agents/skills/builderbro-rag/rag_skill.py
runtime: python3 (stdlib only — no packages, no network required)
---

# Skill: builderbro-rag

Answer a question from this project's own documents, with citations you can
verify, **or** say it does not know. That second half is the point: a retriever
that always returns something is not a knowledge tool, it is a confident
guesser. Measured, before this skill had a confidence gate, it answered **3 of 3**
unanswerable questions (false-answer rate 1.00). It now answers 0 of 3 while
still answering 14 of 14 answerable ones.

## When to use it

- You need a fact that is written down in this repo and you want the exact file
  and character span it came from.
- You are about to assert something about BuilderBro's design and need to check
  it against the source instead of recalling it.
- You need to know *why* retrieval is failing — use `diagnose`, which names the
  failing stage rather than guessing at the fix.
- **Do not** use it for general knowledge, arithmetic, or anything the corpus
  does not contain. It will refuse, and that refusal is correct — do not
  re-ask with a rephrased question hoping for a different answer.

## Interface

One entry point. JSON on stdout by default; `--human` for reading.

```bash
S=.agents/skills/builderbro-rag/rag_skill.py

python3 $S build                      # (re)index the corpus
python3 $S search "phase clock law"   # retrieval only — no model, no network
python3 $S ask "what is the phase clock law"              # live geneation + audit
python3 $S ask "..." --generator extractive                # offline, no keys
python3 $S eval                       # measured quality report + floors
python3 $S diagnose                   # stage-attributed failure findings
python3 $S diagnose --cycle --register # run + register a self-building cycle
python3 $S contract                   # machine-readable interface contract
```

### Response shape

Every command returns one JSON object containing `ok`.

| command | key fields |
| --- | --- |
| `build` | `index_path`, `files`, `chunks`, `config` |
| `search` | `hits[]` — `citation` (`[n]`), `source`, `heading`, `span`, `score`, `channels`, `text` |
| `ask` | `ok`, `refused`, `answer`, `citations`, `citation_fidelity`, `groundedness`, `query_support`, `verdict`, `reasons`, `sources[]` |
| `eval` | `ok`, `retrieval`, `span_coverage`, `grounding`, `checks`, `markdown` |
| `diagnose` | `findings[]` (`stage`, `severity`, `symptom`, `evidence`, `lever`, `hypothesis`), `incumbent`, `cycle` |

### Two rules a caller must respect

1. **`ok: false` always carries `error` and `reason`.** Branch on `ok`, never on
   the text of a message.
2. **A refusal is `ok: true` with `refused: true`.** Refusing is a correct
   outcome, not an error. A caller that retries on refusal is defeating the exact
   mechanism that prevents hallucination.

## Citing what you get back

`answer` contains `[n]` markers. `sources[n-1]` gives the file and the character
span that marker refers to. Every marker is checked against the chunks that were
actually retrieved: a marker pointing at a source that was never retrieved is
reported in `invalid_citations` and fails the verdict. Positions are real — you
can open the file and read the span.

If you need to quote the corpus yourself, use `search` and cite `hits[n-1]`.

## How it works (short version)

1. **Ingest** — corpus files (`RAG_CORPUS`, default: the design docs + residences).
2. **Chunk** — heading-aware, overlapping, with real character spans.
3. **Index** — BM25 over stemmed tokens, plus a signed hashing vectoriser
   ("embedding" is a generous word for it — see Limits).
4. **Retrieve** — the two channels are score-normalised and fused.
5. **Gate** — idf-weighted query support. If the retrieved context does not
   contain the question's information, it refuses *before* the model is called.
6. **Generate** — a prompt that mandates a citation on every factual sentence
   and names an exact refusal token.
7. **Audit** — citations re-checked against retrieved chunks; every claim
   sentence scored for lexical support against the source it cites.

## Measured quality

`python3 $S eval` reproduces this. Floors are pre-registered in `rag_eval.FLOORS`
and the gate fails if any is missed.

| metric | measured | floor |
| --- | --- | --- |
| span coverage (gold text present in a chunk) | 1.000 | 1.00 |
| recall@k | 0.929 | 0.85 |
| MRR | 0.729 | 0.70 |
| top-1 source accuracy | 0.643 | 0.60 |
| citation fidelity | 1.000 | 0.95 |
| refusal accuracy | 1.000 | 1.00 |
| false-refusal rate | 0.000 | ≤ 0.00 |

The auditor is validated in both directions, which is the only reason to trust
its numbers: the **extractive** reference generator (copies real sentences)
scores fidelity 1.000 / groundedness 1.000 / 0 fabricated citations, while the
**hallucinate** control (fabricates on purpose) scores 0.000 / 0.000 / 14
fabricated markers and **fails the gate**. An auditor that passes everything or
fails everything proves nothing; this one separates the two controls.

## Self-diagnosis and self-building

`diagnose` attributes each failing metric to a **stage** — ingestion, chunking,
retrieval, ranking, embedding, generation — and names the lever that could move
it. This is what stops a confident wrong fix: if gold text was split across a
chunk boundary, the fault is **chunking** and no ranker change can recover it.

`diagnose --cycle` runs DETECT → RESEARCH → DESIGN → IMPLEMENT → TEST → REGISTER
as a bounded search (fusion × chunk budget × overlap × dense weight, then the
confidence threshold), evaluates every candidate against the golden set, and with
`--register` persists the winner to `rag_config.json`, which changes every later
run. The cycle's first real run took recall from 0.857 → 0.929 and MRR from
0.667 → 0.729. Every cycle is appended to `SELF_IMPROVEMENT_LOG.md`, never
edited — including cycles that found nothing, which are the ones worth keeping.

## Limits — read these before trusting an answer

- **Retrieval is not semantic.** BM25 plus a hashing vectoriser has no notion of
  meaning. A paraphrase that shares no vocabulary with the source will not be
  found. This is measured, not hidden.
- **Support is a lexical proxy.** A sentence that reuses the source's vocabulary
  while inverting its meaning can pass. It is a cheap detector for fabrication,
  not a proof of entailment.
- **The gate is calibrated on three negative examples, and it is fragile.** The
  margin between the weakest answerable question (0.560) and the strongest
  unanswerable one (0.548) is **0.012** — barely wider than the measurement's own
  noise. It works on this corpus; it is not a general-purpose confidence model,
  and it must be re-measured when the corpus or golden set changes.
- **A negative result worth keeping.** Excluding interrogatives (`what`, `who`,
  `how`...) from the tokeniser looks obviously correct, and it slightly improved
  MRR (0.729 → 0.735). It also **destroyed** the gate: minimum positive support
  fell to 0.267 against a 0.512 negative maximum, so no threshold could separate
  the sets and a question the corpus answers started being refused. The old
  margin had been propped up by common question words occurring all over the
  corpus and lending spurious support to *unanswerable* questions. The change is
  reverted and the reasoning is recorded in `rag_core.STOPWORDS`.
- **Two verdict layers, deliberately.** `verdict` / `ok` is the hard gate and
  covers only exact, decidable failures: a citation that resolves to nothing, or
  a claim with no citation at all. `strict_verdict` adds the lexical-entailment
  proxy, which penalises correct paraphrase — a live answer that cites correctly
  but rephrases an equation scored `unsupported_rate` 0.50. Failing release on
  the proxy would make the gate unusable on real model output; ignoring it would
  hide a real gap. Both are reported.
- **The golden set is 17 items (14 answerable).** One item is ~7% of recall, so
  a config sitting on a floor is marked `near-floor`, not proven better. Do not
  treat small deltas as real.
- **A registered config changes behaviour globally** via `rag_config.json`. That
  is the point of registration, but it means an eval run and a `--cycle` run can
  disagree until the index is rebuilt with `build`.
