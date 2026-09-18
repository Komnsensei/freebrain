# Freebuff Autonomous Agent Builder & Free Brain Architecture

**Epistemic Status:** Experimental / Frontier R&D Framework
**Target Domain:** Open-Weight Autonomous Agent Synthesis & Decentralized Cognitive Routing
**Status:** research/design — rev 0.8 (Q1 evidence log from the P0 phone-CPU run; `agent_runtime.py` auto-captures per-step tokens/s; **Drive residence** — the agent's persistent home folder mirrored via rclone, reached only through the allowlisted `drive_sync` tool; **`drift_loop.py`** — the file-driven Q4 loop: residence files *are* the state machine, one cycle per `trigger` touch, dispatch-graph hashing, deterministic gates, AGENT-INTEGRITY breakers, resume-on-crash, Test 2 determinism battery; **first Q4 result** — 32 cycles on phone-class hardware, coherence 1.00, frozen graph hash, ~11 min/cycle; **P6 runtime** — two deployment paths: always-on Oracle kit in `deploy/oracle/` and a card-free GitHub Actions runner in `.github/workflows/drift-runner.yml` + `deploy/actions/`, see §9)

This document is the full decomposition of the research brief. It is written to be
honest about what is established, what is plausible, and what is speculative — the
epistemic discipline is not decoration, it is the point. Anything labeled
`[SPECULATIVE]` is a hypothesis to test, not a design to defend. The brief's own
sections are preserved (marked **brief §n**) and expanded with analysis grounded in
this repo's existing work (`AGENT-INTEGRITY.md`, `AGENTIC.md`, `presence/`).

---

## 1. Research Decomposition  *(brief §1)*

### Core research question

> How can an autonomous agent harness open-weight foundational models ("Free
> Brain") to dynamically generate, sandbox, and execute multi-language tool-chains
> without relying on proprietary closed APIs?

### Required stack

| Component | Role | Why |
| --- | --- | --- |
| Python 3.11+ | Model-serving and agent orchestration host | The open-weight inference ecosystem (PyTorch, HF transformers, vLLM) is Python-first. |
| PyTorch + Hugging Face `transformers` | Local inference of open-weight checkpoints | The reference path for running weights with no cloud dependency. |
| Rust | Sandbox control and memory isolation | A memory-safe language for the process that *must not* itself be exploitable; it is the one component that guards the host, so it gets the strongest guarantees. |
| Docker API | Ephemeral runtime environments | Per-task containers: cheap to spawn, resource-bounded (CPU/mem/net), disposable by design. |

### Known literature baseline

> Open-weight agentic frameworks (e.g., LangChain open-source runners, AutoGen,
> OpenInterpreter) provide basic tool-calling loops but often lack strict
> memory-bounded isolation and zero-dependency offline self-reconfiguration.

Section 2 turns this into a gap table.

### Speculative Leap  *(brief §1, the part that was truncated in the original paste)*

> A Freebuff Autonomous Agent Builder & Free Brain Architecture — a **speculative
> cognitive loop ("Free Brain") that dynamically rewrites its own system
> instructions and execution graphs based on real-time falsifiability metrics
> without human intervention.**

That is the actual thesis of the project: not "an agent that calls tools", but an
agent that **rewrites its own control loop** — its prompts, its execution graph, its
tool registry — and does so based on measured falsifiability, offline, with no human
in the loop. Everything in this document exists to make that sentence either true or
demonstrably false.

### Sub-question decomposition (measurement plan)

| # | Sub-question | Falsified if |
| --- | --- | --- |
| Q1 | Can a locally served open-weight model drive a multi-step tool loop at usable reliability? | Task success rate on a fixed suite is below a pre-registered floor (the brief's target is **>90%**, §8 — treated as the stretch bar, not the starting bar). |
| Q2 | Can the model *generate* tool-chain code (manifest + program) rather than only call pre-built tools? | >50% of generated manifests are rejected by static analysis as malformed on first attempt. |
| Q3 | Can generated code execute with strict memory-bounded isolation such that hostile/broken programs cannot touch the host, model, or ledger? | Any escape attempt in the isolation suite succeeds even once (**100% prevention required**, §8). |
| Q4 | Can the loop rewrite its own system instructions and execution graph offline, using falsifiability metrics as the only feedback? | The **1,000-cycle drift test** (brief §3) shows incoherence, divergence, or unbounded recursion before 1,000 continuous cycles. |
| Q5 | Does identical input produce identical tool-dispatch graphs when no stochastic parameter is configured? *(brief §4, Test 2)* | Two identical runs diverge in dispatch graph without an explicit randomness setting. |

### Q1 evidence log — measured throughput (hardware gate, 2026-09-08)

*First measured datapoint for Q1, taken live on the P0 local backend. This is a
hardware-gate finding, **not** a Q1 verdict: the pre-registered falsification floor
(§8) still requires the 20-task suite run on hardware that can actually sustain
local inference.*

**Test rig:** Android phone (Termux), Ollama daemon on `127.0.0.1:11434/v1`
(OpenAI-compatible), 7.4 GB RAM total with **~2 GB available** during the run, pure
CPU inference (`size_vram: 0`), streaming generation via the same endpoint
`agent_runtime.py` uses.

| Model (Q4_K_M) | RAM resident | Measured throughput |
| --- | --- | --- |
| qwen2.5-coder:3b | ~2.4 GB | 64-token generation exceeded 120 s (timed out) |
| qwen2.5-coder:1.5b | ~1.4 GB | ~1 token per 8–9 s (streamed chunks: "Hello" → "!" → " How" at 8.5–9.4 s intervals) |
| qwen2.5-coder:0.5b | ~0.4 GB | 3 chunks in 55 s (~1 token per 15–18 s) |

**Reading:** throughput is *memory-bound, not model-size-bound* — the 0.5B model
ran *slower* than the 1.5B because memory thrash dominates once the phone's free
RAM is exhausted. At the measured rates a single 30-token tool directive would take
~4–9 minutes per loop step, so Q1's "usable reliability" cannot even be probed on
this device.

**Implication for the roadmap:** the P0 wiring is not the bottleneck — streaming
early-stop plus the 256-token step cap in `agent_runtime.py` mean the harness is
ready to measure the moment it runs on capable hardware (laptop/desktop, a mini-PC,
or a Raspberry Pi 5 with 8+ GB RAM). Until then the phone stays on the cloud
fallback (Vertex). Q1 is deferred, not falsified; the evidence log is the record
that will be updated when a real machine runs the 20-task suite.

**Auto-capture (rev 0.4):** `agent_runtime.py` now records throughput on every
model step instead of relying on hand timing. Each `--goal` step (and each
`--chat`) prints a `[perf]` line to stderr and appends one JSONL record to
`q1-evidence.jsonl` (repo root; `EVIDENCE_FILE=""` disables the write, any other
path redirects it). Record schema: `ts, run, provider, provider_url,
provider_key, failovers, model, url, step, kind (tool|final|chat), tokens,
elapsed_s, tokens_per_s, early_stop` — where `provider_key` is the *env var
name* whose credential served the call (never the key value). Tokens are
counted from streamed SSE chunks (one per token on Ollama/vLLM/llama.cpp), so an
early-stopped step reports exactly the tokens generated up to the cut; when the
server sends a final `usage` chunk its exact `completion_tokens` wins. A future
run on capable hardware can therefore regenerate this table directly from
`q1-evidence.jsonl` — the file is the raw material, this table is the view.

**Cascade attribution (rev 0.6):** `provider` and `provider_url` name the model
backend that actually served the step, and `failovers` lists every hop that
failed first (with its failure class). Throughput in this log is therefore
attributable per provider, so the Q1 table can be re-cut by backend (local CPU
vs. a hosted free tier) instead of assuming a single voice.

---

## 2. Baseline & Novelty Status  *(brief §2)*

### Brief's position

> **Existing:** Static open-source agent runners with predefined prompt templates and
> static tool registries.
>
> **Novel:** A fully autonomous "Free Brain" builder that treats agentic control
> loops as dynamic state-machines capable of compiling custom execution binaries on
> the fly.

### Gap analysis — how the existing frameworks line up

| Framework | Tool loop | Memory-bounded isolation of generated code | Offline self-reconfiguration | Verdict on the two named gaps |
| --- | --- | --- | --- | --- |
| LangChain / LangGraph | Yes (mature) | No — tools run in-process by default | No | Loop yes; isolation and self-reconfig are user responsibilities |
| AutoGen / AG2 | Yes (multi-agent) | No — local code execution by default | No | Same pattern |
| OpenInterpreter | Yes (natural language → code) | Partial — Docker mode exists but coarse | No | Closest philosophical match; weakest on isolation rigor |
| OpenClaw, CrewAI, ADK, MS Agent Framework, LlamaIndex | Yes (loops) | No | No | Orchestration focus, not isolation |

**Baseline summary:** every framework solves the *loop*; none treats
memory-bounded execution of model-generated code as a first-class guarantee, and
none attempts offline self-reconfiguration. The two gaps named in the brief are
real and open — and the brief's "novel" claim holds against them.

### Model landscape note (as of mid-2026, directional)

GLM-5.2 and Kimi K2.7 Code are widely cited for long-horizon coding; Gemma 4 (31B)
is the commonly cited consumer-GPU ceiling; OpenAI's Apache-2.0 open-weight
releases are reported among the strongest for tool calling. Serving is mature
(vLLM / Ollama / llama.cpp / HF transformers). Implication: the model layer is
**not** the research risk — the pipeline around it (isolation, verification,
self-rewriting) is.

---

## 3. Uncertainty Disclaimer  *(brief §3)*

> No published empirical data guarantees that an unconstrained open-weight model
> running locally can maintain architectural coherence during recursive
> self-modification over 1,000+ continuous execution cycles without cognitive drift
> or infinite recursion.

This disclaimer is the honest core of the whole project, and it deserves to be
taken seriously rather than waved past:

- **Cognitive drift:** each self-rewrite changes the instructions the next cycle
  sees. Small errors compound; after enough cycles the loop can converge on a
  coherent but *wrong* objective, or diverge entirely. This is the 
  "conversational continuity corruption" failure mode that `AGENT-INTEGRITY.md`
  exists to fight in BRO — raised to the level of *the system rewriting its own
  instructions*.
- **Infinite recursion:** a loop that rewrites its own execution graph can,
  in principle, rewrite it into a graph that rewrites itself forever. The only
  defense is external: hard caps (memory, time, depth) that the model cannot edit
  because they live outside its write scope.
- **Why "unconstrained" is the operative word:** frameworks that let a model edit
  its own prompts exist as demos; a *measured, 1,000-cycle, offline, no-human*
  run does not. Q4 exists to produce that measurement.

**The repo already has the right reflex for this:** `AGENT-INTEGRITY.md`
non-negotiable #1 — *a structured failure is always preferable to a fluent
fabrication* — and its dual-level circuit breakers (2 consecutive same-signature
failures, or 3 distinct failures per goal) are precisely the kind of external cap
that makes the 1,000-cycle claim testable instead of hand-wavy.

---

## 4. Falsifiability Criteria  *(brief §4)*

The brief defines two tests. These are the *pass/fail contract* of the project —
any design decision below is subordinate to them.

| Test | Rule | Operational meaning in this design |
| --- | --- | --- |
| **Test 1 — Recursion Bounds** | Fail if self-generated code modifications cause execution loops to exceed **>2 GB memory** or **>30 s timeout** | These become the default `resource` caps in the tool-chain manifest (§6.1). The Rust guard enforces them at the kernel/container level — the model cannot raise its own limits. |
| **Test 2 — Determinism Check** | Fail if identical input states produce **divergent tool-dispatch graphs** without explicit stochastic parameters configured | Every run logs its dispatch graph (step sequence + tool calls). Two runs from the same input must hash identically when `temperature` is pinned and no randomness is declared; any divergence fails the run. |

Two notes, honestly:

1. **Test 1's 2 GB / 30 s bounds are generous for a "loop" but tight for a
   "tool-chain".** A generated analysis job may legitimately need more than 30 s.
   The criterion as written governs *execution loops inside the cognitive loop*,
   not every spawned task — the manifest contract should keep these as defaults and
   let a manifest *declare* a higher bound, which then requires approval. The
   falsifiability gate itself stays at 2 GB / 30 s.
2. **Test 2 must also apply to the self-rewrite path, not just tool calls.** If the
   system rewrites its own instructions, determinism can only be checked at the
   *dispatch* level (did it do the same things?) — the rewritten text itself may
   legitimately vary. The test is defined on the graph, which is the right choice.

---

## 5. Architecture  *(brief §5, expanded)*

The brief's diagram, preserved:

```
+-------------------------------------------------------------+
|               Freebuff Autonomous Agent Builder             |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                     "Free Brain" Core                       |
|   - Open-Weight Model Runner (Local / Offline Capable)      |
|   - Dynamic Prompt & Instruction Synthesizer                |
+--------------+------------------------------+---------------+
               |                              |
               v                              v
+------------------------------+  +---------------------------+
|    Cognitive Planning Loop   |  |   Rust Sandbox Dispatch   |
|    (State Graph & Memory)    |  |   (Ephemeral Docker/FFI)  |
+------------------------------+  +---------------------------+
```

### Expanded layered view

The brief's three boxes map onto six layers. Everything below a layer is trusted by
it; nothing above it can reach below its contract.

```
┌────────────────────────────────────────────────────────────────────┐
│  L5  Cognitive router          [SPECULATIVE]  task → model(s) →    │
│      (optional peer routing)                 verified result       │
├────────────────────────────────────────────────────────────────────┤
│  L4  Integrity ledger          invariant / observed / volatile     │
│      (port of AGENT-INTEGRITY) tags, promote(), assertInvariant,   │
│                                breaker counters (sig + goal)       │
├────────────────────────────────────────────────────────────────────┤
│  L3  Rust Sandbox Dispatch     Rust guard ⇄ Docker API: spawn,     │
│      (brief's right box)       cap (2 GB / 30 s), relay, kill;     │
│                                seccomp profile; ephemeral per task │
├────────────────────────────────────────────────────────────────────┤
│  L2  Tool-chain synthesis      model emits manifest + code;        │
│      (multi-language)          static gate → sandboxed run →       │
│                                verify → promote to invariant       │
├────────────────────────────────────────────────────────────────────┤
│  L1  Cognitive Planning Loop   goal tracking, state graph, memory, │
│      (brief's left box)        tool loop, breaker semantics        │
├────────────────────────────────────────────────────────────────────┤
│  L0  Free Brain Core           open-weight checkpoints served by   │
│      (brief's top box)         vLLM / llama.cpp / HF; dynamic      │
│                                prompt & instruction synthesizer    │
└────────────────────────────────────────────────────────────────────┘
```

- **L0 Free Brain Core** = local inference + the *Dynamic Prompt & Instruction
  Synthesizer* — the component whose output is a *rewritten instruction set*, which
  is exactly what §3 says can drift. It is therefore the layer with the tightest
  ledger coupling.
- **L1 Cognitive Planning Loop** = the state graph and memory. This is where
  `AGENT-INTEGRITY.md`'s tagged ledger, inspection gates, and breakers live — a
  port, not a reinvention.
- **L3 Rust Sandbox Dispatch** = the only component with host-facing privileges:
  spawn ephemeral Docker containers, apply Test 1 caps, relay bounded output, kill
  on breach. Container output is *data*, never instructions (defense in depth
  against escape).
- **L5 router** `[SPECULATIVE]` — added as an optional research layer; the brief
  does not require it. It must never become a dependency of the falsifiability
  tests.

### The brain cascade (free-provider failover)

A single backend is a single point of failure: a stopped local server, a
exhausted free tier, or a provider that silently retires a model name stops the
loop for reasons that have nothing to do with the agent. `brain_cascade.py`
turns the one voice into an ordered cascade — **local first** (the local-first
rule is unchanged), then free hosted providers that speak the same
OpenAI-compatible `/chat/completions`:

```
local  →  groq  →  cerebras  →  gemini  →  openrouter  →  mistral  →  github
```

Rules, all measured rather than assumed:

- **A provider with no API key is not in the chain.** With no keys at all the
  chain is exactly `[local]` — byte-for-byte the P0 behaviour, zero cloud calls.
  `BRAIN_CASCADE=0` forces that even when keys exist, which is the switch the
  offline battery (§8) needs.
- **Local weights keep priority.** Cloud hops exist to keep the loop alive, not
  to replace the local brain (P0's exit criterion is unaffected: it is measured
  with the cascade off).
- **Failure is a cooldown, not a crash.** Each failure is classified
  (rate-limit / auth / payment / blocked / model-missing / server) and benches
  that provider for a class-appropriate window, persisted to
  `<residence>/provider-health.json` so a restart does not hammer a provider that
  just returned 429. A benched provider moves to the back of the chain rather
  than being dropped.
- **Authenticating is not serving.** A provider can answer `/models` with 200 and
  still refuse every completion. Verified live 2026-09-13: a Cerebras key listed
  three models while `/chat/completions` returned
  `402 payment_required` on all of them — Cerebras stops API access on the Free
  Trial tier until credits are purchased. Its body says `"param": "quota"`, so
  a naive classifier files it as a rate-limit and retries every minute forever.
  It has its own class (`payment`, one hour) instead: the hop costs one failed
  attempt and the chain moves on, and the key starts serving the moment credits
  exist — no config change. The same distinction protects any future paid wall.
- **Status is checkable before the loop needs it.** `--providers --ping` probes
  each `/models` endpoint (is it listening, was the key accepted);
  `--providers --ping-chat` sends each hop a real **1-token completion**, which
  is the only probe that can see a wall — it is what reported the Cerebras
  `402 payment_required` above, on a key whose `/models` answered 200. A probe
  records what it finds as cooldowns, the same evidence a run would have left,
  rather than a second opinion that drifts from the first.
- **Model retirement self-heals.** If a provider's model name no longer exists,
  the cascade re-reads that provider's live `/models` list once, picks the best
  surviving chat model by preference (whisper/guard/embedding endpoints are
  filtered out — a live Groq listing is mostly those), and retries — the
  silent-deletion failure mode that breaks naive single-provider pipelines.
  Verified live 2026-09-13: the configured Groq default
  (`llama-3.3-70b-versatile`) had been retired, and the cascade recovered to
  `openai/gpt-oss-120b` on the first request.
- **Extra keys are extra hops.** Free tiers cap *per key*, so one key is a single
  point of failure even when the provider is healthy. `GROQ_API_KEY`,
  `GROQ_API_KEY_2..9` and `GROQ_API_KEYS=k1,k2` become `groq`, `groq#2`, … —
  each with its own cooldown, so a rate-limited key is benched while its
  siblings keep answering. Verified live: with key 1 benched, the next request
  was served by `groq#2` without operator intervention.
- **Every hop is recorded.** The serving provider and the failed hops (with
  classes and cooldowns) land in the Q1 evidence log and the drift ledger, so a
  failover is visible in the record instead of being inferred from latency.
  Each drift cycle also writes a `cascade` health block — the hop list in
  failover order, every hop that cannot serve right now with the class and
  seconds that benched it, and a `status` of ok / degraded / exhausted. The hop
  order is the degradation: because a benched hop moves to the back, a provider
  outage is a diff between two consecutive ledger lines rather than something
  reconstructed from the tokens/s column afterwards. Hop *names* only — the
  block never carries a key.
- **Exhaustion is honest.** If every provider fails, the loop receives one
  structured `[gate-failed]` naming every attempt and its error — never a
  fabricated answer.
- **The client's identity is part of the contract.** Provider edge networks
  fingerprint HTTP clients: the default `Python-urllib/3.x` User-Agent is
  answered with `403 error code: 1010` (Cloudflare) — indistinguishable from a
  rejected key, and it once benched all four working keys for an hour. The
  harness therefore always sends its own UA, and a fingerprint block is
  classified as `blocked` (short cooldown) rather than `auth` (one hour), so a
  client bug can never masquerade as a credential problem again.

Not to be confused with **L5 / P7 routing** `[SPECULATIVE]`, which would split a
single task across models. The cascade never changes *what* is asked — only
*who* answers it, in the same request shape.

### What the model can never do

- Execute code outside the sandbox (there is no path).
- Raise its own resource caps (Test 1 lives in the guard).
- Promote its own output to invariant (only the gate chain promotes).
- Write to the ledger (machine-written only).
- Edit its own rewrite permissions (the synthesizer's write scope is enforced by
  the ledger layer, not by the model's good behavior).

---

## 6. Implementation  *(brief §6, evaluated)*

The brief ships a Python sketch (`FreeBrainConfig`, `FreebuffAgentBuilder` with
`synthesize_cognitive_graph` and `dispatch_sandbox_execution`). Honest evaluation:

- The sketch is a **simulation stub**, and should be labeled as such: 
  `dispatch_sandbox_execution` gates only on the literal substring `"infinite_loop"`
  and returns hardcoded metrics (`45.2 MB`, `exit_code: 0`) regardless of the
  payload. It demonstrates the *shape* of the system (brain-body separation,
  sandbox dispatch) but implements neither a real gate nor a real sandbox.
- It does usefully encode the right seams: `FreeBrainConfig` (model id, max_tokens,
  temperature, sandbox timeout) and the memory store. Those map 1:1 onto the
  manifest contract below.

### 6.1 The tool-chain contract (replaces the stub's substring check)

```jsonc
{
  "task": "string — the goal this tool-chain serves (goes to the ledger)",
  "language": "python3 | node | rust | bash",        // allowlist only
  "entrypoint": "relative path in the workdir",
  "resource": { "max_mem_mb": 2048, "max_seconds": 30, "network": false },
  "deps": ["allowlisted, pinned packages only"],
  "declares": { "side_effects": ["files written", "ports", "network hosts"] }
}
```

Anything not declared is denied. A missing field fails static validation — the
model is never asked to "fill in the gaps" at runtime.

### 6.2 The gate chain (deterministic checks only)

```
generate (volatile)
  → parse manifest          (schema check)
  → static analyze code     (parse/compile; dep allowlist; banned imports; size cap)
  → sandboxed run           (Rust guard: 2 GB / 30 s / no network unless declared)
  → verify output           (exit code + declared artifact shape + golden assertions)
  → promote to invariant    (ledger entry with howVerified: "gateChain")
```

Failure at any step returns a **structured failure** (`[gate-failed]
step=… reason=…`) and feeds the breaker counters — never raw failed output for the
model to "smooth over" (`AGENT-INTEGRITY.md` Component 2).

#### One constructor for every failure — enforced, not intended

That marker used to be built in two shapes: the action tools emitted
`[gate-failed]`, the read-only file tools emitted `[error]`. The cost was
measured, not hypothetical. A model looping on `read_file` of a path that does
not exist was emitting a failure the loop guard did not count, so the run read as
steady progress until the step budget ran out — and the diagnosis blamed the
budget instead of the tool. One prefix, one detector: a second prefix disabled an
entire detection category without a single test failing.

All tool failures now come from `loop_guard.fail(reason)`, and the contract is
tested rather than documented.

Each registry entry now carries its own probe — `Tool(fn, probe)`, where `probe`
is an argument that must make that tool fail:

```python
TOOLS = {
    "list_dir":  Tool(_tool_list_dir,  "./definitely/not/here"),
    "read_file": Tool(_tool_read_file, "./definitely/not/here.txt"),
    "drive_sync": Tool(_tool_drive_sync, "not-a-subcommand"),
    "qih_metric": Tool(_tool_qih_metric, ""),
    "rag":        Tool(_tool_rag, ""),
}
```

The probe used to live in a table *inside the test*, which made it a second list
someone had to remember to extend — and a contract that lives beside the code
rather than in it is the kind that drifts. Now the probe is part of the tool, so
there is one source and it cannot disagree with what the loop runs.

**Both fields are positionally required**, so `Tool(_tool_rag)` raises
`TypeError` while `agent_runtime` is importing: a new tool without a probe cannot
be *defined*, let alone run. A module-level `_validate_tools()` catches the other
way the registry rots — an entry replaced with a bare function, which is still
callable and would therefore keep working while the probe silently vanished.
`FailureProtocolTest` then executes every declared probe and asserts the canonical
prefix, and it asserts the old `[error]` literal appears nowhere in that module —
covering failure paths no probe reaches.

The asymmetry is deliberate. **Emission is conservative, detection is liberal:**
a legacy `[error]` already sitting in a ledger, or returned by a tool this module
does not own, still counts as a failure. Tightening the detector to match the new
emitter would re-create the blind spot the audit found.

#### A repeated call is not by itself a stuck loop

The first hardening stopped a run when any (tool, argument) signature was seen
more than `repeat_limit` times **over the whole run**. That rule is measurably
wrong: `loop_audit.py` shows it flagging two legitimate behaviours as
`no_progress` — a model that revisits a directory it has already listed, and a
model re-reading something that *changed underneath it* (the drift loop reading
its own `state.json` is exactly this shape).

Detection is now result-aware: a repeat is `no_progress` only when the same call
returns the same bytes on consecutive steps, with nothing in between. An
identical call whose result changed is evidence the model got somewhere, and is
scored as progress. The original rule stays registered and selectable
(`repeat_mode: cumulative`), so the change is revertible from config rather than
by editing code — and `loop_audit.py` runs both rules against the same
pathologies: **false positives 2 → 0, sensitivity retained** (a genuinely stuck
loop stops under both, at the same step).

#### Retrieval scores are memoised — and the memo exposed a real defect

`rag_diagnose.sweep` searches **one index under every candidate config**, so
without a cache it recomputes byte-identical scores once per candidate. Each
channel is now memoised per (query, channel-relevant config), measured over the
real 56-candidate sweep: **71.3 s → 24.5 s, an 85.7% hit rate** (1904 lookups,
272 computations).

The point is not the speed, it is that the memo proved the *semantics* wrong.
Writing its tests — pairing a memoised index against a fresh one for every
candidate — required knowing which config a search actually uses, and the answer
was: not the one it was handed. `search(query, config=cfg)` scored with the
index's own config while silently accepting the argument, so a caller asking for
`bm25_k1=2.2` got 1.2's scores. That is invisible today only because the sweep
varies chunk geometry (which rebuilds the index) and fusion (which reads the
argument) — every knob it moves happens to be one the channels ignore.

The channels now read the config they are given, and the memo key is read from
that same object, so key and value cannot disagree. A config that moves **vector
geometry** is refused instead of honoured: `dim` / `char_ngram` / `char_weight`
shape the stored vectors, and a smaller `dim` did not even fail loudly — `zip`
silently shortened the dot product, so the index would have returned a confident
wrong ranking. Silent wrong answers are worse than refusals, so the guard names
the knob and the fix (rebuild the index for that config).

#### The corpus is this document, which makes every floor a moving measurement

Writing the two blocks above moved the retrieval numbers, because the RAG corpus
**is this repository's own documentation**. Adding ~40 lines here re-chunked the
index (193 → 170 chunks at the newly registered geometry) and shifted the
confidence supports enough to push the registered gate *below* the highest
unanswerable support — 0.55 under a max-negative of 0.5504 — so `/rag eval` began
reporting a false answer it had previously refused. Nothing in the pipeline was
broken. The thing being measured had moved.

Two consequences worth stating plainly:

- **The floors are a regression test on the docs as much as on the code.** A large
  addition to an indexed file, or any reword of `GOLDEN_ITEMS`, can change recall,
  MRR and the gate at once. `python3 -m unittest rag_test` is what catches it, and
  the new invariant there fails loudly on the specific dangerous case: a gate that
  sits at or below the highest unanswerable support.
- **A tuning grid has to be finer than the boundary it is searching for.** The
  gate's separating gap measured 0.0053, while `SUPPORT_GRID` moves in steps of
  0.05 — so the grid could not name the right threshold, and when nothing on it
  was perfect the tuner's fallback was to change *nothing*, leaving a gate that
  answered unanswerable questions. The gate is now placed at the midpoint of the
  measured gap (maximum margin on both sides), which is a candidate like any other
  and still has to show a perfect refusal record to be kept. When the two sets
  overlap the rule returns nothing at all: that is a retrieval fault, and a number
  would look like a decision.

The re-measured incumbent after this cycle is `linear / dense 1.0 / budget 120 /
overlap 24 / gate 0.5475` — **MRR 0.729 → 0.768, top-1 0.643 → 0.714, margins
+0.0050 below and +0.0049 above, false-answer rate 0.000**.

---

## 7. Experiment & Simulation Protocol  *(brief §7, expanded)*

The brief's four steps, with what each must actually produce:

1. **Local model initialization** — spin up the open-weight inference endpoint in a
   local container. *Produce:* a health check (model id, context length, tokens/s)
   and a note of whether it started with zero network calls (offline capability).
2. **Objective injection** — feed high-level autonomous tasks into the builder.
   *Produce:* the initial state graph and the rewritten instruction set, both
   written to the ledger as `volatile` with a `needsVerify` flag.
3. **Sandbox telemetry monitoring** — track resource consumption and execution
   latency across sandboxed subprocess boundaries. *Produce:* per-run records of
   memory, time, output size, and gate outcomes — the raw material for Test 1.
4. **Falsifiability audit** — measure cognitive drift and compile error rates
   across iterations. *Produce:* the 1,000-cycle drift curve: per-cycle dispatch
   graph hashes (Test 2), gate rejection rate, breaker trip count, and a coherence
   score trend. The drift curve is the primary deliverable of Q4 — the project
   either shows 1,000 coherent cycles or it does not.

---

## 8. Evaluation & Failure Modes  *(brief §8)*

### Metrics (brief's targets, with honest annotations)

| Metric | Target | Honest annotation |
| --- | --- | --- |
| Autonomous task completion rate | **>90%** | **Now measurable, and measured: 1.000** — but read §8.2 before quoting it. The floor holds on a *scripted* model over a read-only tool set, which measures the loop's decisions, not a real model's competence. The stretch bar still refers to unscripted tasks |
| Sandbox escape prevention rate | **100%** | Correct as a *requirement* — but "0 escapes on our suite" is point-in-time, not a guarantee. Re-run the escape suite on every runtime/image update. |
| Memory overhead per cognitive loop | **<100 MB** | Tight and good. Includes the loop's working set excluding the model itself (model VRAM is a separate, reported number). |

### Expected failure modes (brief's, expanded)

1. **Context window degradation during recursive self-prompting** — each self-rewrite
   appends; windows fill; the loop forgets its objective. Mitigation:
   `AGENT-INTEGRITY.md` Component 4 compaction — pin objective + invariants, strip
   recovery residue, keep a lean live tail. The full transcript goes to disk first.
2. **Subprocess blocking on unhandled I/O inside generated code blocks** — a
   generated program waits on stdin forever. Mitigation: sandbox defaults to
   closed stdin, wall-clock deadline (Test 1), output-buffer cap, and a kill
   path in the guard.
3. *(added)* **Drift into a wrong objective** — coherent but divergent after N
   cycles. Mitigation: the 1,000-cycle drift curve is *reviewed as a curve*, not a
   final number; a mid-run divergence fails Q4 even if the endpoint "worked".

### 8.2 The task suite — what it now reports, and what it does not

`autonomy_suite.py` runs 20 tasks against a generated fixture, with **floors
registered before the first run** and a `--repeat` check that fails the suite if
any verdict differs between runs. Measured:

| floor | value | direction |
| --- | --- | --- |
| `task_accuracy` | 1.000 | min |
| `verified_completion_rate` | 1.000 | min |
| `false_success_rate` | 0.000 | max |
| `refusal_accuracy` | 1.000 | min |
| `budget_compliance` | 1.000 | min |
| `budget_slack_rate` | 1.000 | min |
| `mean_replans_per_completion` | 0.111 | max |
| `mean_tokens_per_task` | 25.6 | max |

All eight pass, twice, byte-identical. The 20 tasks split 9 completable and 11
that a correct loop must refuse (unsatisfiable goals, a model that answers without
inspecting, three budget ceilings, a repeated failing call, a divergent step, a
replan storm).

`budget_slack_rate` is the budget-*adequacy* floor, and it is exact rather than
tuned: every success must leave at least one step and one token of room. A success
on the last permitted step has not shown the budget was sufficient, only that it was
barely so — one provider hiccup would flip the verdict, so the recorded success
would not be reproducible. The tightest success is printed with the report.

**The instrument checks itself.** Every task's `goal_satisfiable` declaration is
validated by *replaying its plan* through the real tools and evaluating the plan's
own GOAL-CHECK — an earlier version searched for a token as a substring, which
missed filename-based goals and conflated "the token exists" with "the goal
condition holds". `--live` runs the same 20 goals against the real cascade and is
deliberately informational: a model's competence is not a property the loop
controls, and pretending otherwise would report the provider's mood as the
harness's quality.

**What it does not cover:** code generation, multi-file edits, or any tool that
writes. The loop's tools are read-only by construction, so the completion gate has
never had to verify a change to the world — only a fact about it.

### The Free Brain test suite (fixed before P1 ships)

| Suite | Measures | Pass condition |
| --- | --- | --- |
| 20 curated multi-language tasks | Q1 reliability | **instrument exists** (`autonomy_suite.py`): ≥ pre-registered floor, measured twice. The tasks are not multi-language code tasks — they are read/measure/refuse/budget/fault shapes over the read-only tool set. That is the honest scope of what ships today (see §8.2) |
| Manifest fuzz | Q2 rejection of malformed/over-privileged manifests | 100% of invalid manifests rejected by static gate |
| Escape attempts (network, fs, process, mem, fork-bomb, infinite output) | Q3 isolation | 0 escapes |
| 1,000-cycle drift run | Q4 coherence | No drift/divergence/recursion per §3, graph hashes stable per §4 Test 2 |
| Determinism battery | Q4 Test 2 | Identical inputs → identical dispatch-graph hashes with stochastic params pinned |
| Offline battery (network down) | Q4 offline claim | Self-rewrite task completes with zero network |

---

## 9. Next Actions  *(brief §9, mapped to a roadmap)*

The brief's own next actions, and the phases that deliver them:

| Brief next action | Phase | Exit criterion |
| --- | --- | --- |
| Integrate real-time containerized Docker endpoints for live code execution | **P1** — Rust guard + Docker runtime (Python3 only first) | Escape suite green; Test 1 caps enforced by the guard, not the model |
| Implement persistent vector memory indexing for cross-session agent learning | **P4** — offline memory/ledger persistence | Cross-session recall works offline; nothing unverified is ever labeled fact |

Full sequencing (every phase ends runnable and independently testable):

| Phase | Deliverable | Exit criterion |
| --- | --- | --- |
| **P0 — Local model backend** | Open-weight backend (Ollama/vLLM/llama.cpp) behind a model-selector seam (`model-selector.mjs`: `localModelConfig`/`chatLocal`/`pingLocal`, plus the stdlib-only `agent_runtime.py` harness); Vertex demoted to fallback — the active brain on phone-class hardware (§1 evidence log) | Same task runs on local weights with zero API calls; latency recorded — first measurement: phone-class CPU ≈0.1 tok/s (Q1 deferred to capable hardware, see §1 evidence log) |
| **P1 — One-language sandbox** | Rust guard + Docker runtime for Python3; 2 GB / 30 s caps, output cap, deadline, escape suite | Escape suite passes; Test 1 enforceable |
| **P2 — Tool-chain synthesis** | Manifest contract + gate chain (§6); model emits Python tool-chains end-to-end | 20 curated tasks complete the chain; structured failures on reject |
| **P3 — Integrity ledger port** | `AGENT-INTEGRITY` semantics live in the Free Brain runtime | Ledger suite green; breaker trips purge correctly |
| **P4 — Offline self-reconfiguration** | Dynamic instruction synthesizer (L0) rewrites prompts/execution graphs offline; vector memory index | Offline battery passes; 1,000-cycle drift run begins |
| **P5 — Multi-language** | Allowlist expands to node/rust/bash with per-language static gates | Manifest fuzz green across languages |
| **P6 — 1,000-cycle drift study** | The Q4 experiment, run and published honestly; two runtimes — always-on VM (`deploy/oracle/`) and a card-free ephemeral CI runner (`.github/workflows/drift-runner.yml`) | Drift curve + Test 2 hashes reported; pass *or* fail, both move the status line |
| **P7 — Router spike** `[SPECULATIVE]` | Multi-model sub-task routing with per-boundary verification | Pre-registered hypothesis tested; "does not work" is an acceptable answer |

**P0 hardening — the brain cascade (rev 0.6).** P0 shipped one backend, so the
loop's availability was exactly the local server's availability. `brain_cascade.py`
adds ordered failover across free OpenAI-compatible providers (§5, above) with
cooldowns, model re-discovery and per-hop evidence — measured by
`brain_cascade_test.py`. It does not move any P0 exit criterion: the cascade is
off (`BRAIN_CASCADE=0`) whenever an offline or P0 measurement runs.

**P6 runtime — the always-on box (rev 0.7).** The loop was proven on
phone-class hardware and then moved to hardware that can finish the study:
`deploy/oracle/` is the Q4 runtime, not a rewrite of it. `bootstrap.sh` installs
Ollama (with `OLLAMA_KEEP_ALIVE=-1`, so a continuous run never pays a reload)
behind `freebrain-loop.service`, where **systemd is the supervisor** —
`Restart=on-failure` plus resume from `state.json` means a crash or a reboot
costs one cycle rather than the run. Exit codes **2** (breaker trip / drift
alarm) and **3** (determinism failure) are declared as *success* on purpose:
they are deliberate terminal states, and restarting into a tripped breaker would
be the loop lying to itself (`AGENT-INTEGRITY.md`). `migrate-residence.sh` moves
the live residence onto the box and **refuses to overwrite a checkpoint that is
further along** unless forced — losing cycles to a typo is the failure mode this
project documents rather than hides. `freebrain-sync.timer` mirrors the
residence to Drive every 5 minutes, so the memory outlives the machine and the
drift curve is readable from the phone. Runbook: `deploy/oracle/ORACLE.md`
(requires a card at signup).

**Card-free P6 runtime (rev 0.8).** The same loop runs on free GitHub Actions
runners with no card and no billing: `.github/workflows/drift-runner.yml`, runbook
`deploy/actions/GITHUB-ACTIONS.md`. This is possible *because* of the design
already in place — a runner is killed at a 6 h wall, which is the phone's
OOM-kill made predictable, and the residence resumes from `state.json`. Two
consequences had to be engineered rather than assumed. First, a wall-kill
mid-run would lose every cycle the host never got to persist, so `drift_loop.py`
gained `--max-seconds`: it stops itself on a cycle boundary before the wall and
returns 0, and the workflow's persist step is `if: always()` so a breaker trip or
a falsified Test 2 is still recorded. Second, `schedule` events are **disabled on
private repositories for free personal accounts** — a real, badly-documented
constraint that makes cron silently do nothing. Free-tier facts as verified
(2026-09-18): public repo = 4 vCPU/16 GB and unlimited minutes with working cron;
private repo = 2 vCPU/8 GB, 2,000 min/month, cron needing an external trigger.
A 1,000-cycle run is estimated at ~500 min, inside the private allowance.

**First Q4 result (phone, run `435cb26603`).** The file-driven loop ran 32
cycles before the throughput wall — **0.13 tok/s, ~11 minutes/cycle**, killed
by Android's reaper whenever the phone was used (the residence survived every
kill; resume never lost a cycle). Cycles 2–32 were **accepted** with coherence
climbing to **1.00**, and cycles 3 onward produced an **identical dispatch-graph
hash** (`d477938e…`). Cycle 1's rewrite was rejected for `objective-drift` — the
deterministic gates catching a bad self-edit, then recovering. This is recorded
as an observation, not a verdict: a frozen graph hash with full coherence is
*either* a stable self-model *or* a loop that stopped exploring, and only the
completed 1,000-cycle curve (and Test 2) distinguishes those. **A flat line must
be reported as a flat line.**

---

## 10. Risks & honest constraints

- **Capability gap vs. frontier APIs.** Local open-weight models are not uniformly
  at parity with the best closed models. The thesis must be *sufficiency for a task
  class*, not parity.
- **Compute and latency.** Local serving costs real hardware and is slower per
  token. This is the price of "no proprietary dependency"; P0 measures and reports
  it. First datapoint (2026-09-08): phone-class CPU measured ≈0.1 tok/s even at 0.5B
  — interactive local use is a hardware requirement, not a model property (§1
  evidence log).
- **Isolation is an arms race.** Container escapes get discovered over time; the
  T7-class threat re-runs on every update. Defense in depth: the guard treats
  container output as data.
- **Self-rewrite is the biggest footgun.** The more the loop may change its own
  tooling, the larger the attack surface. The rewrite scope (prompts, execution
  graphs, tool manifests) is a hard line: **never** the runtime, the guard, or the
  ledger. Test 1 caps live outside the model's write scope by construction.
- **The 1,000-cycle claim may fail.** That is the point of the disclaimer in §3.
  The project's credibility comes from running the experiment and reporting the
  curve — not from the outcome being favorable.
- **"Decentralized" is under-specified.** Peer routing (L5) inherits every open
  problem in distributed trust. It is a research spike, not a dependency of the
  falsifiability tests.
- **"Always free" is a vendor promise, not physics.** The P6 box is an Oracle
  Cloud Always Free VM because it is $0 with real headroom — not because the
  design depends on it. Free-tier allowances change (the Ampere A1 allotment has
  already been halved once) and accounts can be reclaimed. The residence is the
  only irreplaceable thing, which is why every host step is disposable and the
  state is mirrored: any box can be rebuilt from `state.json` in minutes. If the
  free tier disappears, the run continues from wherever it stopped.

---

## 11. Non-goals & open questions

### Non-goals

- Not a replacement for the BRO chat UX or `cli.mjs`'s browser-agent features.
- Not a benchmark leaderboard entry; external benchmarks calibrate, they do not
  define success.
- Not a distributed-consensus system; `presence/` is the moral reference (honest
  tallies, never forced to 100%), not the transport.
- Not a promise of closed-model parity.

### Open questions (answered only by the phases above)

1. What is the *task class* where local open-weight reliability is sufficient?
   (Q1 measurement defines it.)
2. Does the static gate reject too much (killing useful generated tools) or too
   little? Tuned only after P2 data.
3. Is the >90% completion target reachable, or does the honest number land lower?
   §8's stretch bar is there to be revised by evidence, not defended to the death.
4. `[SPECULATIVE]` Does verifiable cross-model routing beat a single good local
   model on any real workload once verification cost is counted? "No" is an
   acceptable answer.

---*Companion docs in this repo: `AGENT-INTEGRITY.md` (ledger/breaker/compaction
design that layers L1/L4 port), `AGENTIC.md` (BRO agentic-loop baseline),
`presence/` (honest-tally ethos), `model-selector.mjs` (the P0 seam),
`agent_runtime.py` (harness + residence + `drive_sync`), `brain_cascade.py`
(cascading free-provider brain), `drift_loop.py` (the Q4 file-driven drift run),
`QIH.md` (the QIH instance layered on this harness). This document is rev 0.6,
reconciled against the full source brief; the status line moves when a phase
produces evidence.*