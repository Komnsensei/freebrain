# Free Brain — OpenChat + Indexing Plugin

**Open-weight, local-first, grounded.** Chat with the brain, or use the corpus as a plugin — every answer is retrieved, cited, and audited. No guess is returned as fact: insufficient context yields `INSUFFICIENT_CONTEXT` instead of hallucination.

> **One command to run it all**
```bash
python3 openchat.py            # UI on http://localhost:8080 + plugin + OpenAI compat
```
That's it. No build step, no npm, no Docker. `stdlib` only. The index is built on first run and cached at `freebrain-residence/rag/index.json`.

---

## What you get

| Surface | URL | What it does |
|---------|-----|--------------|
| **OpenChat UI** | `GET /` | Single-page chat + live indexing pane + audit display. Toggle grounded (RAG) and streaming. |
| **Chat (grounded)** | `POST /api/chat` | `{messages, stream, use_rag, temperature}` → `{content, audit, hits, query_support, provider}`. SSE when `stream:true`. Falls back to extractive retrieval when no model is reachable, never hallucinates. |
| **Search (indexing plugin)** | `POST /api/search` | `{query, k}` → `hits[{citation [n], source, heading, span [start,end], score, channels, text}]`. Hybrid BM25 + signed-hash dense, fused linearly. |
| **Ask (grounded QA)** | `POST /api/ask` | `{question, k, generator}` → `{answer, citations, citation_fidelity, groundedness, verdict, refused, sources}`. Hard gate: fabricated citation → `grounding:fail`. |
| **Health** | `GET /api/health` | `{ok, index{chunks, files, config}, cascade{chain}, version}` |
| **Build** | `POST /api/build` | Rebuild index from corpus. Also `python3 openchat.py --build`. |
| **OpenAI compat** | `POST /v1/chat/completions` | Same as `/api/chat` with OpenAI envelope + `audit` + `provider`. Point any OpenAI SDK at it. |
| **Plugin discovery** | `GET /.well-known/ai-plugin.json`, `GET /openapi.json` | ChatGPT plugin manifest + OpenAPI 3.0 spec. `GET /api/contract` mirrors the `builderbro-rag` skill contract. |

All responses are CORS-enabled (`*`).

---

## Quick start

### 0 · Prerequisites

Python 3.10+ (no packages). Optional: an OpenAI-compatible model server.

```bash
# Optional local brain (Ollama is the easiest — any OpenAI-compatible endpoint works)
ollama pull qwen2.5-coder:3b
ollama serve  # on http://127.0.0.1:11434/v1
```

Without a local server the system still answers — it just falls back to **extractive retrieval** (copies real source sentences and cites them) instead of generating. You always get cited, audited answers; you never get a made-up one.

### 1 · Run the server

```bash
python3 openchat.py                 # :8080
python3 openchat.py --port 3000     # custom port
PORT=3000 python3 openchat.py       # also via env
python3 openchat.py --build         # rebuild index then exit
python3 openchat.py --rebuild       # rebuild then serve
```

Open `http://localhost:8080/` — chat on the left, indexing/search + audit on the right.

### 2 · Use it as a plugin (indexing)

Every host that speaks HTTP + OpenAPI can call it.

```bash
# Search — the indexing entrypoint
curl -s localhost:8080/api/search -H 'Content-Type: application/json' \
  -d '{"query":"dispatch-graph hashing"}' | jq .hits[0]

# Grounded ask — answer + audit (extractive, no model needed)
curl -s localhost:8080/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"What is the drift loop?","generator":"extractive"}' | jq .

# Grounded ask — live generation (needs a brain: local Ollama or hosted cascade)
curl -s localhost:8080/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"What is the phase-clock law?","generator":"live"}' | jq .

# Rebuild the index after editing docs
curl -s -X POST localhost:8080/api/build | jq .
```

### 3 · Use it as an OpenAI backend

Any OpenAI SDK works — just change the base URL.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="local")
resp = client.chat.completions.create(
    model="free-brain",
    messages=[{"role": "user", "content": "What is resume-on-crash?"}],
)
print(resp.choices[0].message.content)
# resp.audit  -> {verdict, citation_fidelity, query_support, refused, ...}
```

```bash
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What is the brain cascade?"}]}' | jq .
```

### 4 · Customize the corpus (indexed files)

By default the indexed corpus is `FREE-BRAIN.md`, `AGENT-INTEGRITY.md`, `QIH.md`, `AGENTIC.md`, `Freebuff Autonomous Agent Builder (1).md`, plus residence docs — everything the golden evaluation covers. Override with `RAG_CORPUS` (os.pathsep-separated files or directories):

```bash
RAG_CORPUS=docs:src python3 openchat.py
RAG_CORPUS=/path/to/my/docs python3 openchat.py --build
```

---

## Smart & Valid — what those words mean here

* **Smart:** grounded retrieval (BM25 + char-n-gram hashing, linear fusion), confidence-gated generation via the **brain cascade** (local → Groq → Cerebras → Gemini → OpenRouter → Mistral → GitHub Models, with cooldowns and failover), and transparent telemetry (tokens, latency, provider attribution) per step.
* **Valid:** every citation is re-checked against **retrieved** chunks only — a marker pointing at a chunk that was never retrieved is a `fabricated_citation` and fails the hard gate. The entailment proxy is reported separately (`strict_verdict`/`warnings`) because it penalizes correct paraphrase; failing release on it would make the gate unusable on real model output, ignoring it would hide the gap. Both are surfaced. **Refusal is a correct outcome:** when `query_support` is below the threshold the system returns `INSUFFICIENT_CONTEXT` before the model is even called.

Measured on the saved index (237 chunks, 9 files — full HEAD sources):

```
span_coverage 1.000  recall@k 0.857  mrr 0.714  top1 0.643
citation_fidelity 1.000  refusal_accuracy 1.000  false_refusal 0.000
```

(Built from `FREE-BRAIN.md` 65k + `AGENT-INTEGRITY.md` 30k + `QIH.md`/`AGENTIC.md` + residences; `rag_core.DEFAULT_CONFIG.min_query_support=0.60` is the measured gate for this corpus.)

(`python3 .agents/skills/builderbro-rag/rag_skill.py eval` reproduces this; `diagnose --cycle --register` runs a bounded tuning sweep.)

---

## Configuration — environment variables

```ini
# Server
PORT=8080
HOST=0.0.0.0

# Corpus
RAG_CORPUS=docs:src          # os.pathsep list of files/dirs to index
RAG_INDEX_DIR=freebrain-residence/rag

# Brain — local first, cascade when keys exist
LOCAL_MODEL_URL=http://127.0.0.1:11434/v1
LOCAL_MODEL=qwen2.5-coder:7b
GROQ_API_KEY=...             # https://console.groq.com/keys
CEREBRAS_API_KEY=...         # https://cloud.cerebras.ai
GEMINI_API_KEY=...           # https://aistudio.google.com/apikey
OPENROUTER_API_KEY=...       # https://openrouter.ai/keys
MISTRAL_API_KEY=...          # https://console.mistral.ai
GITHUB_TOKEN=...             # https://models.github.ai
BRAIN_CASCADE=0               # local-only (disables cascade)

# Residence (the agent's persistent home — survives restarts/machines)
DRIVE_RESIDENCE=freebrain-residence
DRIVE_REMOTE=gdrive:freebrain   # rclone remote, optional
```

See `.env.example` for the full list. `openchat.py` calls `agent_runtime.load_env_file()` so a `.env` file works too.

---

## API reference (minimal)

### POST /api/search  — indexing plugin

```json
{ "query": "string", "k": 6 }
→ { "ok": true, "query": "...", "k": 6, "n_hits": 6,
    "hits": [{"rank":1,"citation":"[1]","chunk_id":"...","source":"FREE-BRAIN.md",
               "heading":"§5 Architecture","span":[1234,1567],
               "score":0.82,"channels":"both","text":"..."}] }
```

### POST /api/ask  — grounded answer

```json
{ "question": "string", "k": 6, "generator": "live|extractive" }
→ { "ok": true, "refused": false, "answer": "… [1] … [2].",
    "citations":[1,2], "citation_fidelity":1.0, "groundedness":1.0,
    "query_support":0.82, "verdict":"grounding:ok", "strict_verdict":"grounding:ok",
    "sources": [{"citation":"[1]","source":"FREE-BRAIN.md","span":[1234,1567]}] }
```

When the confidence gate fires, `answer` is `INSUFFICIENT_CONTEXT` and `refused:true` — a correct, auditable outcome, not an error.

### POST /api/chat  — openchat

```json
{ "messages":[{"role":"user","content":"..."}],
  "stream": false, "use_rag": true, "temperature": 0.2, "max_tokens": 2048 }
→ { "ok": true, "content":"… [1].", "audit":{...}, "hits":[...],
    "query_support":0.82, "provider":"groq", "tokens":58, "elapsed":0.7 }
```

With `stream:true` the response is `text/event-stream` (SSE), OpenAI shape (`data: {"choices":[{"delta":{"content":"…"}}]}`) plus a trailing `audit` frame, then `data: [DONE]`. With `use_rag:false` the prompt is sent to the cascade verbatim (no retrieval).

### GET /.well-known/ai-plugin.json + GET /openapi.json

Standard ChatGPT plugin discovery — CORS included so a browser or plugin host can fetch them directly.

---

## Architecture — where the files live

```
openchat.py                     ← you are here: UI + all HTTP surfaces (stdlib only)
openapi.json / plugin.json      ← checked in, also served live (host-correct)
.well-known/ai-plugin.json      ← same manifest under the well-known path
rag_core.py                     ← ingester / chunker / BM25+hash index / audit
rag_eval.py / rag_diagnose.py   ← measurement + self-diagnosis (eval → diagnose → cycle → register)
freebrain-residence/rag/index.json ← derived index (chunks only; vectors rebuilt on load)
agent_runtime.py + brain_cascade.py ← brain: local OpenAI-compatible + free-tier failover + streaming
loop_guard.py / autonomy.py     ← tool loop instrumentation + goal-directed verify→gate
drift_loop.py                   ← file-driven Q4 loop (state.json / ledger.jsonl / instructions.md)
FREE-BRAIN.md / AGENT-INTEGRITY.md / QIH.md … ← indexed corpus (RAG_CORPUS)
```

---

## Why it stays honest

* No semantic encoder is claimed where none exists: `embed()` is a **hashing vectoriser** (signed buckets, weighted by tf-idf + char n-grams), not a neural embedding. A paraphrase with zero lexical overlap will not be retrieved — reported as `retrieval` misses, not hidden.
* The audit is adversarial: a copied JSON blob with no alphabetic density is not counted as a claim; a citation `[n]` standing alone is re-attached to the preceding sentence; a marker whose text appears verbatim in a source is a *quotation*, not a fabrication.
* The index is derived: `index.json` stores chunks and char spans; BM25 stats and vectors are rebuilt deterministically on load, so a stale vector can never silently disagree with the text.

---

## Companion docs

* `FREE-BRAIN.md` — the full decomposition of the brief (epistemics, falsifiability tests, roadmap).
* `AGENT-INTEGRITY.md` — ledger / breaker / compaction design.
* `.agents/skills/builderbro-rag/SKILL.md` — the RAG skill contract (`build | search | ask | eval | diagnose | contract`).
* `deploy/{oracle,gitlab,phone}` — long-running and CI runners for the 1,000-cycle study.
