# Anchor Integrity & Hard Circuit Breakers — cli.mjs (BRO)

**Status:** design/spec — rev 0.2 (implementation follows; audit corrections
incorporated below)
**Target:** `cli.mjs` (the BRO agent runtime). The components are designed as a
portable module (`integrity.mjs`) so the same primitives can later be reused by
`cli12.mjs` or the `presence/` pipeline without changes to their semantics.

The primary failure mode to eradicate is **conversational continuity
corruption**: an early unverified hypothesis, speculative guess, or incomplete
inspection becomes a permanent anchor that downstream modules defend,
reconcile with, or patch around — instead of discarding.

---

## 1. Threat model — how corruption actually happens in this codebase today

These are concrete, code-grounded paths (line refs approximate; the file
shifts):

| # | Path | Code evidence |
| --- | --- | --- |
| T1 | **Unverified model text enters shared context.** Every model reply is pushed straight into `chatLog` (`agentLoop`) and, worse, memorized as an *observation* (`memorize("observation","BRO: ...")`) in `~/.bro/memory.json`. | `buildMemoryContext()` injects the last 5 observations into the **system prompt of every later call** as "Recent context:", with the same presentation weight as the `Known facts:` block (which is *always empty* today — nothing ever writes facts). A guess stated once survives as ambient context forever. |
| T2 | **Raw tool output is fed to synthesis with no gate.** `agentLoop` pushes `"Results:\n" + results` (up to 30k chars) into `chatLog` and asks the model to continue. Nothing verifies that the tool output parsed, that a test ran green, or that the data shape matches before the model builds conclusions on it. | `runT(calls)` returns `{tool,args,result,ms}`; only the `"ERR"` string prefix is treated as failure. Everything else is trusted. |
| T3 | **Failure recovery is conversational, not programmatic.** A failed tool call's `ERR` result stays in `chatLog`; the model then writes a "salvage" explanation that also stays in `chatLog`. The only guard is prose inside `SYS_BASE` ("Repeating an identical failing call twice ends the turn early") plus a **per-turn** `failureSignatures` counter that resets every turn. | `agentLoop` `failureSignatures` / `repeatedFailure` block. Nothing is purged, no checkpoint exists, cross-turn failures are invisible. |
| T4 | **Synthesis shortcuts skip inspection.** The `/review /explain /doc /test` shortcuts read a file and send it to the model *without running lint/parse/test first*, so the model can "smooth over" broken code with fluent vocabulary. | `handleInput` shortcut branch (around line 2936). |
| T5 | **No output validation.** `cln(text)` strips tool tags and prints whatever remains — empty replies, evasion-flooded prose, unsupported future promises, and leftover `<<<TOOL:` fragments are all emitted as if equal. | `cln(t)` + final `console.log(fin)` in `agentLoop`. |
| T6 | **Cross-instance memory is shared but untyped in practice, and the verified channel is dead.** Every `memorize()` call site in `cli.mjs` writes type `"observation"` only (terminal + Telegram paths). `memory.facts` — the one bucket that *should* hold invariants — is never written by anyone, so BRO has no verified-fact channel at all, and the observations bucket silently mixes the user's words with BRO's own claims. | All 7 call sites use `"observation"`; `buildMemoryContext()` reads `.facts` (always empty) and `.observations`. |

**Design non-negotiables** (drawn from the repo's own ethos — see
`presence/consensus.mjs`: honest tallies, never forced to 100%):

1. A structured failure is always preferable to a fluent fabrication.
2. Deterministic checks outrank model self-reports. If the machine can check
   it, the machine checks it — the model never gets to be the source of truth
   for something a command can verify.
3. Never drop the user's objective or a verified invariant — those are the
   only truly permanent state.
4. Every component degrades to a *direct, honest statement* when it fires —
   no apology loops, no qualifier walls, no pretending.

---

## 2. Core schema — the tagged state ledger

Everything hangs off one plain-JS object shape (this file is zero-dependency
ESM; no classes required, plain objects + pure functions).

```js
// Tag taxonomy — one axis: how much may downstream code trust this?
//   invariant -> may be consumed as a constraint anywhere
//   observed  -> real tool output, shaped but not cross-checked
//   volatile  -> model claim, guess, parse, or apology — must be gated
const TAG = {
  USER_GOAL:  "invariant",  // what the user asked — never dropped
  VERIFIED:   "invariant",  // passed a deterministic gate
  OBSERVED:   "observed",   // tool output, real but unchecked
  HYPOTHESIS: "volatile",   // model claim / plan / guess
  RECOVERY:   "volatile",   // model apology/recovery text — compaction fodder
};

// One run ledger, per window/instance (parallels chatLog; persisted with it).
ledger = {
  runId: "…",                  // randomUUID, same id the session file gets
  objective: "…",              // first user message of the run — invariant
  invariants: [                // TAG.VERIFIED items, append-only
    { id, text, src, at, howVerified } // howVerified: e.g. "exit0" | "parse" | "testGreen"
  ],
  volatile: [                  // TAG.HYPOTHESIS / RECOVERY items
    { id, text, src, at, kind: "claim|guess|parse|apology" }
  ],
  blockers: [                  // what a breaker found; fed to the user
    { id, text, at, key, consecutiveFailures }
  ],
  checkpoint: { chatLogIndex, volatileLen }, // last known-invariant boundary
  breaker: { key, consecutiveFailures, lastTripAt, trips }
};

// chatLog entries gain an optional provenance marker (backwards-compatible:
// missing meta => treated as volatile HYPOTHESIS if role:"model", OBSERVED if
// it is a "Results:" user turn).
//   { role:"user", parts:[{text}], meta:{ tag, src, id } }
```

### Enforcement rules (Component 1)

- **All creation is volatile.** Model text enters as `HYPOTHESIS`. Tool output
  enters as `OBSERVED`. Nothing is ever born invariant.
- **Promotion is gated.** `observed -> invariant` only via
  `promote(ledger, id, { howVerified })` where the caller must name the
  deterministic check that ran. Model claims cannot self-promote by being
  repeated — repetition is a volatile signal, not a verification.
- **Consumption is hard-blocked.** `assertInvariant(ledger, ref)` throws
  `VolatileAsInvariantError` if a `volatile`/`observed` item is used as a
  constraint. The two legitimate consumers that *may* read volatile state —
  `buildMemoryContext` and compaction — must explicitly pass
  `{ allowVolatile: true }` and must label the items as unverified in what
  they emit.
- **Memory gets a tag column and a real verified channel.** `memory.json`
  entries gain `tag: "fact"|"observation"` (default `"observation"` when
  absent — so all pre-existing entries land as observations; safe demotion,
  never silent promotion). Because today *no* call site writes `"fact"`, the
  promotion path is added as an explicit new function, `memorizeVerified(text,
  howVerified)` — not as a loosened `memorize` — and only deterministic
  verification sites may call it (e.g. a tool result that passed a gate with
  `howVerified: "exit0"`). `buildMemoryContext` emits two clearly separated
  blocks: `Known facts:` (invariants only) and `Recent context (unverified):`
  (observations). This directly kills T1 and revives T6's dead facts bucket.

---

## 3. Component 2 — Deterministic inspection pre-check

**Decouple inspection from synthesis.** The model may only synthesize a
conclusion from a data source after a deterministic pass has produced a
structured, checkable summary of that source. If inspection yields null, low
confidence, or a failing check, the synthesis step is **locked out** and a
structured failure is returned.

### Design

A per-tool inspection table, resolved *before* tool results are pushed into
`chatLog` for the model to continue on:

```js
// integrity.mjs
// Rules must match the tools' ACTUAL return contracts (audited against the
// TOOLS object in cli.mjs): exec -> "ERR <code>: …" | "BLOCKED" | "ERROR: …" |
// output | "(ok)"; read -> content | "NOT FOUND: …" | "TOO LARGE" |
// "BINARY: …" | directory listing; patch -> "PATCHED" | "NOT FOUND…" | "ERR …".
export const TOOL_INSPECT = {
  exec:   (res) => ({ ok: !/^(ERR |BLOCKED|ERROR:)/.test(res), note: /^(ERR |BLOCKED|ERROR:)/.test(res) ? res.slice(0,300) : "" }),
  read:   (res) => ({ ok: !/^(NOT FOUND|TOO LARGE|BINARY:)/.test(res), note: res.slice(0,120) }),
  patch:  (res) => ({ ok: res.startsWith("PATCHED"), note: res.startsWith("PATCHED") ? "" : res.slice(0,200) }),
  write:  (res) => ({ ok: res.startsWith("OK"), note: res.startsWith("OK") ? "" : res.slice(0,200) }),
  // web_*: require non-empty page text and [N] indexes before form-filling is
  // allowed to continue (no model guessing field numbers from nothing).
  web_text: (res) => ({ ok: !!res && res.trim().length > 0, note: "empty page" }),
  // etc. Unknown tools default to { ok:true } — absence of a gate is recorded
  // as `ungated` in the ledger, never as verified.
};

export function inspectGate(tool, args, result) {
  const rule = TOOL_INSPECT[tool];
  if (!rule) return { ok: true, ungated: true };
  const v = rule(result);
  return { ok: !!v.ok, note: v.note || "", ungated: false };
}
```

### Gate-failure semantics (decided, deterministic)

A tool loop can call several tools at once, so the gate result is applied per
result, not all-or-nothing:

- **All results fail inspection** → full lockout: no `Results:` block is fed
  to the model at all; the run goes straight to the breaker path (Component
  3). The model is never asked to synthesize from a wholly failed batch.
- **Some pass, some fail** → the failing results are *replaced* in the
  `Results:` block by a short structured note (`[inspection-failed]
  tool=… reason=…`); the passing results feed synthesis normally. Each
  failure still increments the breaker counters (Component 3), so a model
  that keeps mixing one broken call into every batch trips the objective
  breaker instead of grinding.
- **`ungated` tools** (no rule) pass through, but are recorded as ungated in
  the ledger — visible in the session file, never mistaken for verified.

### Hooks into cli.mjs

1. **Tool loop** (`agentLoop`): after `runT(calls)` and before
   `chatLog.push({role:"user", parts:[{text:"Results:\n"+rpt}]})`, run
   `inspectGate` per result and apply the per-result semantics above. A failed
   result is never fed raw to the model; it becomes a structured note or
   triggers lockout, and every failure feeds the breaker.
2. **Shortcut actions** (`review|explain|doc|test` in `handleInput`): insert a
   real pre-check before `askChat`:
   - `test <file>` → run the file (or its matching test) with `node` and feed
     the actual pass/fail output; a failing run locks out the "here are tests"
     synthesis.
   - `review <file>` → run `node --check`/parse when the file is JS; if it
     fails to parse, return the parse error directly instead of asking the
     model to review the code.
   - `doc <file>` / `explain <file>` → `TOOLS.read` must return content (the
     `NOT`/`TOO` prefixes already return early today — keep that, and treat
     it as the structured failure it already is).
3. **Provider errors are already a gate** — keep the deterministic `askChat`
   401-refresh / 429-backoff and the new 403 diagnostic; those are inspection,
   not synthesis, and must not change.

---

## 4. Component 3 — Algorithmic circuit breakers & hard resets

**Replace soft repair loops with programmatic breakers.** A validation check,
test, or assertion that fails **twice consecutively** triggers the hard
breaker sequence. Persisted state replaces the current per-turn
`failureSignatures`, which forgets everything between turns.

### Design (pure logic in `integrity.mjs`, state on disk)

**Breaker state is dual-level**, because a per-key counter alone has a blind
spot: a model that *keeps varying its approach* (different tool, different
args each time) never trips any single key — each sits at `n = 1` forever —
so a genuinely stuck goal could burn the whole 5-deep tool loop on every user
turn without ever breaking. Two counters therefore share the same persisted
state and window:

1. **Signature level** (the directive's rule): the *same* `tool::args` failing
   twice consecutively trips immediately.
2. **Objective level**: any gate failure / `ERR` result *of any signature*
   within one run of the same objective counts against the objective. Three
   distinct failures in a single run (deterministic cap, regardless of
   signature) trip the same hard breaker. Consecutiveness is not required
   here — what matters is that one goal consumed three failed attempts with
   nothing verified in between.

```js
// Breaker state persisted to ~/.bro/breaker.json — survives restarts and
// window instances. Key = sha1(objective text) + ("::" + sig | "::*goal").
// Success on a signature resets that signature's pair but NOT the goal count;
// a verified invariant (promote) resets the goal count — progress clears heat.
export function breakerStep(state, { goal, sig = "*goal", failed }) {
  const key = goal + "::" + sig;
  const e = state.counters[key] || { n: 0, lastAt: 0, trips: 0 };
  const now = Date.now();
  if (now - e.lastAt > 30 * 60_000) e.n = 0;        // window (env-tunable)
  e.n = failed ? e.n + 1 : 0;
  e.lastAt = now;
  const hardLimit = sig === "*goal" ? 3 : 2;        // goal: 3 distinct fails; sig: 2 consecutive
  if (e.n >= hardLimit) { e.n = 0; e.trips++; return { trip: true, trips: e.trips }; }
  return { trip: false, armed: e.n >= hardLimit - 1 };
}
```

On `trip: true`, execute the three mandated actions:

1. **Drop the current reasoning branch** — `break` the tool loop immediately
   (this replaces today's `repeatedFailure` message block).
2. **Purge back to the last invariant checkpoint** — `chatLog` is truncated to
   `ledger.checkpoint.chatLogIndex` and `ledger.volatile` is emptied. Failed
   iterations, model salvage text, and apology turns are gone — not patched.
3. **Cold restart of the node with clean context** — instead of printing a
   salvage explanation, re-issue **one** `askChat` with a minimal context:
   the run `objective`, the `invariants`, and the `blockers` list, plus an
   instruction to either attempt a *genuinely different* approach or state the
   blocker directly. The cold re-plan's context entries are tagged invariant
   (`USER_GOAL` / `VERIFIED`) and the checkpoint is re-established on them, so
   a later breaker cannot purge the re-plan's own starting state. The cold
   attempt counts against `MAX_DEPTH` so it cannot recurse forever.

**Own the final text.** Today, when the tool loop breaks early, `agentLoop`
falls through to `var fin = cln(text)` and prints the *stale* pre-failure
model text. On a breaker trip, that must not happen: the trip handler owns
what the user sees — the structured blocker message (below) or the cold
re-plan's output, never leftover salvage text.

Escalation (never loop forever): if a second trip fires for the same
objective within the window, stop re-planning and surface the blocker to the
user with the structured failure, e.g.:

```
x Blocked after repeated failures (log: ~/.bro/debug.log)
  What was tried and failed: <list from ledger.blockers>
  Suggested next move: <none fabricated — the user decides>
```

After escalation, the run ends with the blocker message as its final text
(still written to `chatLog` as an invariant `blocker` entry and saved by
`saveSession`). All trips go through the existing `dbg()` crash/event log and
a new `lg("breaker: …")` line.

### Hook

Replace the `failureSignatures`/`repeatedFailure` block in `agentLoop` with
calls to `breakerStep` + the reset sequence, and gate the final `fin` print
behind the trip handler. Each failure is recorded at **both** levels (the
specific `tool::args` signature and the `*goal` counter); a verified
`promote` or any tool success records `failed:false` at the goal level to
clear heat. The existing `if (ctrl.signal.aborted)` handling stays untouched.
Two windows running the same objective share the persisted counters — double
effort tripping sooner is the conservative, correct direction.

---

## 5. Component 4 — Active context compaction & history pruning

**Prevent memory dilution and momentum drift.** Today `chatLog` is trimmed to
the last 40 entries with no respect for what is inside them — a failed loop
and a verified invariant have equal right to the window. Compaction replaces
raw trimming.

### Design

`compactLedger(chatLog, ledger)` — deterministic only in v1 (a model-assisted
mode is a later opt-in; anything it produces must pass the same verify gates
before it may be called invariant).

Runs at three points: (a) right before `saveSession`, (b) when
`chatLog.length > 40` (replacing the plain `slice(-40)`), (c) immediately
after a breaker reset.

Compaction output is *the same shape* as the cold re-plan context that
Component 3 sends after a breaker (objective + invariants + blockers + lean
tail) — one shared routine (`compactLedger`) produces both, so the compacted
context and the re-plan context can never disagree about what is pinned.

What it does, in order:

1. **Strip recovery residue** — drop model turns tagged `RECOVERY` and failed
   tool iterations that were superseded by a later success or a breaker.
2. **Pin the invariant core** — the run `objective`, the `invariants` list
   (as one compact `[verified]` block), and the `blockers` list are rewritten
   as a single leading system/user segment that is *never* evicted.
3. **Keep only a lean live tail** — the most recent 6 exchanges (user goal +
   current work), everything else collapses into the pinned blocks above.
4. **Never invent** — compaction only deletes and relabels; it never
   summarizes a volatile item into a fact. Anything that looked like a
   "conclusion" during compaction is stored in `ledger.volatile` with a
   `needsVerify` flag, not in `invariants`.

The full untruncated transcript + `ledger` is always written by `saveSession`
to `~/.bro/sessions/<pid>-<ts>.json` before the in-memory `chatLog` is
compacted, so nothing is lost on disk.

---

## 6. Component 5 — Output validation guardrails (anti-evasion filter)

**Intercept evasions before delivery.** Two tiers, because a conversational
agent that hard-rejects every "I think" would be unusable, but an agent that
writes files, docs, and broadcasts must not emit walls of hedging.

```js
// Rules carry an explicit scope so no rule fires where it would be a false
// positive. Tier 1 (structural) is always on within its scope; Tier 2
// (lexical evasion) is gated by `strict` per kind.
//
//   kind: "chat" | "artifact" | "broadcast"   (what is being validated)
//   Note: future-promise is CHAT/BROADCAST ONLY — "we will be able to ship
//   X" is legitimate content in a generated doc, and evasion in a chat reply.
export const STRUCTURAL_RULES = [
  { id: "empty",            scope: ["chat","artifact","broadcast"], test: (t) => !t || !t.trim() },
  { id: "tool-tag-leftover", scope: ["chat","artifact","broadcast"], test: (t) => /<<<TOOL:/.test(t) }, // cln missed one
  { id: "mega-qualifier",   scope: ["chat","artifact","broadcast"], test: (t) => { /* >3 hedging terms per 300 chars */ } },
  { id: "future-promise",   scope: ["chat","broadcast"],            test: (t) => /\b(will be able to|in the future I can|one day I can)\b/i.test(t) },
];

// Tier 2 — lexical evasion. On for artifacts/broadcasts; on for chat only
// under BRO_STRICT_OUTPUT=1. Every pattern is a tight regex, reviewed per
// false positive, and listed here so users can edit.
export const EVASION_SIGNATURES = [
  { pattern: /^(as an ai|as a language model)/i, label: "deflection" },
  { pattern: /\b(i can't|i cannot) (do|access|help with that)/i, label: "refusal-dodge" },
  { pattern: /\bunfortunately,? i\b/i,           label: "false-sympathy" },
  // deliberately small, growing by false-positive reports only
];

export function validateOutput(text, { kind = "chat", strict = false } = {}) {
  // structural rules whose scope includes kind, then evasion rules when strict
}
```

### Where validation actually intercepts (audited against real execution)

There are **two** interception points, because artifacts and chat replies
reach the world through different paths:

1. **Artifact content = model tool arguments.** The model writes files by
   emitting `<<<TOOL:write PATH\nCONTENT>>>` (also `:append`, `:patch`), and
   `runT` executes those tools **immediately, straight to disk** — there is no
   later "print" step to catch. So a new `beforeToolRun(tool, args)` hook in
   `runT` validates the *content half* of write/append args (and patch
   replacements longer than 500 chars) with the artifact rule set **before
   the file system is touched**. Broadcasts (`tgSend`, `broadcast`) validate
   with Tier-1 always + Tier-2 under `strict`.
2. **Free-chat reply = model text.** Validated in `agentLoop` before it is
   pushed into `chatLog` as a model turn *and* before it is printed — so
   rejected output never pollutes the ledger (kills T5 at the source).

### Policy (reject → fix once → structured failure; never loop)

- **Artifact rejection (write/append/patch):** on any applicable rule hit,
   the tool is **not executed**. The model receives the tool result
   `OUTPUT_REJECTED: [rule ids]` — a structured, honest failure it can react
   to — and the rejection counts toward the breaker. **No automatic
   regenerate at the tool level**: auto-regen would let a model that keeps
   producing evasive content loop write/rewrite forever. The single
   regeneration path is the Component 3 cold re-plan, which is capped.
- **Free chat:** Tier-1 rules always apply (empty replies, leftover
   `<<<TOOL:` tags, mega-qualifier floods). Tier-2 evasion rules apply only
   under `BRO_STRICT_OUTPUT=1`. On a Tier-1 hit, regenerate exactly **once**
   with the strict template appended: *"Answer directly. No preface, no
   hedging, no promises about the future. If you cannot do it, say exactly
   what is blocking you."* If the retry still fails, the *original* text is
   shown with a one-line dim note naming the rule — a silent drop leaves the
   user with nothing, which is its own failure. A Tier-2 hit under strict
   mode behaves the same (one regen, then show-with-note).
- **Broadcasts:** Tier-1 always on; a Tier-1 rejection blocks the send and
   returns `OUTPUT_REJECTED` to the caller (no silent partial broadcast).
- **Never fabricate a replacement.** Every rejection path ends in either a
   genuinely regenerated text, the original text visibly marked, or a
   structured `OUTPUT_REJECTED` — never a "cleaned-up" paraphrase invented
   by the validator.

---

## 7. Module layout, tests, config

```
integrity.mjs        # new — zero-dependency ESM, pure functions only:
                     #   ledger helpers (newLedger, tagEntry, promote,
                     #   assertInvariant), TOOL_INSPECT + inspectGate,
                     #   breakerStep (dual-level) + persistence,
                     #   compactLedger, validateOutput + scoped rule tables,
                     #   beforeToolRun (artifact content interception)
integrity-test.mjs   # new — mirrors presence/test.mjs style (zero-dep
                     #   ok() counters, `node integrity-test.mjs`, exit 1 on
                     #   failure); no network, no disk writes outside a tmp dir
cli.mjs              # wired — imports from ./integrity.mjs (same pattern as
                     #   ./bro-web.mjs, ./model-selector.mjs)
```

`cli.mjs` is a 4,500-line layered file (note: two harmless duplicate
`DATA_DIR` declarations, an inner IIFE, and embedded legacy blocks). All new
state lives in `integrity.mjs`; `cli.mjs` gets only thin hook calls at the
sites named above. This keeps the logic unit-testable — `cli.mjs` itself has
no test harness and is a REPL, so untestable logic must not be added to it.

Config (env, all optional, read once at startup like `.env` today):

| Env | Default | Effect |
| --- | --- | --- |
| `BRO_STRICT_OUTPUT` | `0` | Enables Tier-2 evasion rules on free-chat replies |
| `BRO_BREAKER_WINDOW_MS` | `1800000` | Consecutive-failure window for breaker state |
| `BRO_COMPACT_TAIL` | `6` | Live tail length kept by compaction |
| `BRO_ARTIFACT_STRICT` | `1` | Tier-2 rules on artifact/file outputs |

---

## 8. Implementation sequencing (full-implementation plan)

Each stage ends runnable and independently testable.

1. **S1 — `integrity.mjs` + `integrity-test.mjs`: pure core.**
   Implement ledger helpers, `inspectGate` + table, `breakerStep`,
   `compactLedger`, `validateOutput` + scoped rule tables, `beforeToolRun`.
   Unit tests for: volatile items throw through `assertInvariant`; promotion
   requires a `howVerified`; breaker trips on 2 consecutive same-signature
   failures **and** on 3 distinct failures at the objective level, and resets
   on success/window-expiry/verified-promote; compaction never drops the
   objective or invariants; validator flags empty/tool-tag-leftover/
   mega-qualifier, flags future-promise for `chat` but passes it for
   `artifact`, and passes clean text.
2. **S2 — tag the pipeline (kills T1/T6).** Add `meta.tag` to chatLog pushes
   in `agentLoop`; add `tag` column to `memorize`/`memory.json` (default
   observation); split `buildMemoryContext` into verified vs unverified
   blocks; wire `promote` where deterministic facts are learned.
3. **S3 — inspection gate in the tool loop + shortcut pre-checks (kills T2/T4).**
   Hook `inspectGate` before the `Results:` push with per-result semantics
   (partial failure → structured note; all-fail → lockout); add real
   pre-checks to the `review|explain|doc|test` shortcuts. Lock-out returns
   structured failure.
4. **S4 — breakers (kills T3).** Replace the per-turn `failureSignatures`
   block with dual-level persisted `breakerStep` (signature + objective),
   purge-to-checkpoint, cold re-plan with re-established checkpoint, and the
   "own the final text" rule (no stale `fin` print on trip). Escalate after
   two trips per objective per window. Wire `dbg`/`lg` logging.
5. **S5 — compaction + output validation (kills T5).** Replace
   `chatLog.slice(-40)` with `compactLedger` at the three trigger points;
   extend `saveSession` to persist the ledger; add `beforeToolRun` so
   write-family content is validated before touching disk; validate free-chat
   text before chatLog push and print.
6. **S6 — soak.** Run the CLI on realistic multi-tool turns; confirm breaker
   resets produce clean re-plans, compaction preserves objective/invariants
   across a long turn, and no regression in the normal happy path. `node
   --check cli.mjs` and `node integrity-test.mjs` must both pass.

### Acceptance criteria

- A turn that fails the same tool call twice triggers the hard breaker, not a
  third identical retry and not an apology paragraph.
- After a breaker, the next model call contains no text from the failed
  branch (purge verified by reading the sent context).
- `memory.json` observations injected into the system prompt are explicitly
  labeled unverified; nothing unverified is ever labeled a fact.
- A `/test file.js` run against a failing test returns the real failure and
  locks out "tests written" synthesis.
- A model that varies its approach (three distinct failing tool calls on one
  objective) trips the objective breaker — not just identical repeats.
- After a breaker trip the user never sees stale pre-failure model text; the
  final output is the structured blocker message or the cold re-plan's reply.
- A rejected artifact write never touches disk (write-file interception
  tested against a temp dir), and `future-promise` phrasing passes when
  writing a doc but is flagged in a chat reply.
- No output containing `<<<TOOL:` or qualifying its way around a direct
  answer reaches the screen or a written file.
- `node integrity-test.mjs` green; `node --check cli.mjs` green.

---

## 9. Risks & honest product constraints

- **Over-blocking chat.** Tier-2 evasion detection on conversational replies
  is opt-in by default because qualifiers are sometimes honest uncertainty.
  The two-tier split exists precisely so the filter never turns BRO into a
  robot that refuses to hedge when hedging is true.
- **Compaction information loss.** v1 compaction is deletion-only and
  deterministic; the full transcript is always on disk first. Model-assisted
  compaction is explicitly deferred because it reintroduces the exact
  unverified-synthesis problem this design exists to remove.
- **Persisted breaker state can go stale.** The 30-minute window and the
  two-trip escalation bound it; counters are keyed per objective so unrelated
  work is never throttled by an old failure.
- **Breaker cost.** A cold re-plan is one extra model call. Escalation caps it
  at two trips per objective per window —  never an unbounded retry loop.
- **False positives in the evasion table.** The signature list starts tiny and
  only grows from real false-positive reports; every entry is a named,
  editable constant, not a hidden heuristic.
- **Artifact rejection false positives.** Validating model-generated file
  content can, rarely, block a legitimate write that merely contains a
  flagged pattern (that is why `future-promise` is scoped off artifacts). A
  rejection is a non-executed write plus a breaker count — never data loss;
  the user can always write the file directly via the shell, which bypasses
  the tool gate.
- **Shared breaker counters across windows.** Two windows pursuing the same
  objective share `breaker.json`, so combined effort can trip sooner. This is
  the conservative direction (the goal genuinely is being attempted twice);
  per-window isolation is a later knob if it ever false-positives.
