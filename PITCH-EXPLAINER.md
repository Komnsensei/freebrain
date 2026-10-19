# Free Brain — plain-English explainer (pitch deck source)

*Non-technical companion to `FREE-BRAIN.md` / `PLAN-P1.md`. Every number below is
one the repo already measures; where a number has a condition attached, the
condition is on the slide too. Items in `⟦ brackets ⟧` are blanks for you to fill —
they are deliberately not invented.*

---

## Slide 1 — Title

**Free Brain — an AI that refuses to guess, and can't lie about what it did.**

> One-liner: *Everyone is racing to make AI agents smarter. We're making them
> auditable — answers that cite or refuse, and self-written code that runs inside a
> box the AI cannot open.*

Three alternates, pick one:
- *"An agent you can put in front of a regulator."*
- *"AI that says 'I don't know' — and proves it didn't guess."*
- *"The safety layer for AI-written code."*

---

## Slide 2 — The problem

AI agents fail in a specific, expensive way: **they are fluent when they are wrong.**

1. **They guess and you can't tell.** A confident paragraph and a correct one look
   identical on screen. For anyone signing off on the output, that's the whole
   problem.
2. **They run code with your privileges.** "Let the agent write a script and run it"
   means an AI's mistakes run on your machine, with your keys, your files, your
   network. The industry default is *trust the model*.
3. **Their proof is their own self-report.** When an agent says "done, tests pass,"
   there is usually nothing independent checking that claim.

**The cost is not a wrong answer. It's that you cannot tell the difference — so you
either block the tool or accept the risk.** Both are expensive.

---

## Slide 3 — Why now

- Open-weight models are finally good enough to call tools and write small programs.
  **The model is no longer the bottleneck.**
- Running them locally, with no cloud account and no per-token bill, is now ordinary.
- Which means the risk has moved: the danger is no longer "can it write code?" but
  **"who checks the code, and where is it allowed to run?"**
- Nobody has solved that layer as a *product* — the popular agent frameworks all
  solve the loop and leave safety to the user.

---

## Slide 4 — The idea, in one picture

Think **lab notebook**, not chatbot.

Four rules, in plain words:

| Rule | Plain English |
| --- | --- |
| **Cite or refuse** | It answers from your documents and points at the exact passage. If the documents don't support an answer, it says "not enough information" instead of improvising. |
| **Declare before you run** | Anything the AI wants to execute must come with a written declaration: what it does, how much memory, how long, whether it needs the network. No declaration, no run. |
| **Run it in a box** | The code runs with hard limits and no access to the rest of the machine. Output is treated as *data*, never as new instructions. |
| **Only the box certifies** | The AI never gets to mark its own work as good. A separate, deterministic checker does that — and records *how* it was checked. |

---

## Slide 5 — Product 1: answers you can audit

Same interface as a normal chat tool — but every reply carries a receipt.

- Each answer is built **only** from passages retrieved from your own material, and
  each claim links back to the passage it came from.
- A citation that points at a passage that was never retrieved is treated as a
  fabricated citation and **fails the answer**.
- "I don't know" is a *successful* outcome, not an error: the system refuses before
  the model is even asked.
- Every reply ships with a machine-readable audit: where it came from, how confident
  the retrieval was, whether it refused.

**Plain-English business value:** you can ship AI output into a workflow — compliance
review, customer support, internal search — because every sentence is traceable, and
a wrong-but-confident sentence is a build failure rather than a surprise.

*Measured today on the project's own documentation: every citation pointed at a real
retrieved passage (100% citation fidelity); the right source was ranked first for 64%
of test questions and appeared among the six passages it showed for 86%; when it
refused, it was right to refuse 100% of the time, with zero refusals of things it
actually knew.*

---

## Slide 6 — Product 2: actions you can audit

This is the harder half, and the part nobody else treats as a guarantee.

A normal agent is told "here are your tools." **Ours can write new tools — and then
has to get them past a checkpoint before they count as real.**

Plain-English walkthrough of what happens to an AI-written program:

1. **Door 1 — the declaration.** Is the paperwork complete and sane? Anything not
   declared is denied. A request for more memory or time than allowed is *refused*,
   never silently granted.
2. **Door 2 — the read-through.** Is it valid code? Does it import anything it isn't
   allowed to? Is it too large? Escapes blocked here.
3. **The box.** It runs under hard limits: CPU-seconds, file size, number of open
   files, no ability to spawn child processes, wall-clock timeout, memory watched and
   killed on breach.
4. **The check.** Did it exit cleanly? Did it produce the output it promised?
5. **The stamp.** Only now is the result written into the permanent record — with a
   note saying *how* it was verified, not just *that* it was.

**The AI can draft. Only the checkpoint can publish.** That sentence is the product.

---

## Slide 7 — Why this is different

Framing for the "why won't a big framework do this?" question — categories, not
sneering:

| What exists today | What it answers | What it leaves to you |
| --- | --- | --- |
| Agent frameworks / orchestration tools | "How do I chain model calls and tools?" | Isolation, verification, and honesty are your problem |
| Sandboxed code execution services | "How do I run untrusted code?" | Not connected to an agent's goal or a durable record |
| Chat-with-your-docs products | "How do I search my files?" | They answer; they rarely refuse, and rarely audit |
| **This** | All three joined in one loop, with **refusal as a first-class result** and an execution record that names its own limits | — |

Two things nobody else has demonstrated:

- **A self-rewriting loop, measured.** The agent rewrites its own instructions and
  its own execution plan, offline, with no human in the loop — and we ran it for
  **1,000 consecutive cycles** and published what happened (see Slide 8, honestly).
- **A sandbox that reports its own weakness.** Most "sandboxed" claims are
  marketing. Ours writes down which protection is a hard wall and which is a fence.

---

## Slide 8 — Proof (measured, with conditions attached)

| Claim | The measurement | Condition it holds under |
| --- | --- | --- |
| Runs anywhere, cheaply | One command to start; no cloud account, no build step, no container runtime required | Standard library only — nothing to install |
| Works on very small hardware | A full rewrite of its own instructions in ~15 seconds on a **phone's** processor | Phone left idle; under load the same chip drops to a fraction of that (measured both ways) |
| Long-run stability | **1,000-cycle study completed** — the agent rewrote its own instructions for 1,000 cycles unattended; it held its intent steady (0.99 of 1.00) through the first 100 cycles | The loop stayed coherent; it later lost its model supply when the free cloud tiers ran out — recorded as *vendor exhaustion*, not as drift |
| Doesn't drift, doesn't crash | 258 safety-guard trips and 12 drift alarms during that study were caught, absorbed and automatically resumed — the run recorded cycles 1–1000 under **one run identity** | One cycle (141) is missing from the record; the gap is stated, not smoothed |
| Honest at scale | Every automated check in the project passes today: **221 checks, 5 suites** | They include the adversarial sets that try to break the box |
| Refuses correctly | 100% refusal accuracy, 0 false refusals | On the project's own reference corpus |

**The pattern to point at:** each of these is a *record*, not a claim. The repo keeps
raw logs and recomputes its own tables from them.

---

## Slide 9 — The trust slide (our best differentiator)

We audited our own evidence and it was wrong — **98% of the rows in our published
"performance log" were test data that had leaked in**, not measurements. Anyone
reading it would have computed a confident, fictional number.

What we did: found it, quarantined the bad rows (moved, not deleted), fixed the cause
so tests *cannot* write the published record, and documented the whole thing in the
release notes — including the earlier throughput figure we had to correct.

**Why this belongs in a pitch:** the value proposition is "you can check our work."
This is the receipt that we do it to ourselves. It is also exactly the failure our
product exists to prevent, so we can speak about it from experience rather than
theory.

---

## Slide 10 — Who it's for

- **Regulated and high-trust teams** who currently cannot let an AI near their
  documents or systems because output is unverifiable.
- **Edge and offline deployments** — the thing runs on a phone-class device with no
  account and no network.
- **Agent builders** who want to let their agents write code without handing over the
  machine.
- **Long-running autonomy research** — the drift study is the proof point that the
  loop can survive a thousand unattended cycles.

Business shape options to pick from: a drop-in auditing layer for existing agents
(`⟦API / plug-in form⟧`), a self-hosted appliance for regulated environments, or a
research partnership with whoever owns the workload.

---

## Slide 11 — What we have **not** proven (read this one out loud)

Investors trust a limitation slide more than a capability slide. Ours is specific:

1. **One language runs today.** The declaration format accepts four; only the first is
   actually executed. Others fail *loudly*, not silently — but they don't work yet.
2. **The box is a strong fence, not a vault.** Time, CPU and file size are enforced by
   the operating system. Memory is watched from outside (a real limit, with a real
   race window) and network access is blocked by a deny-list, not by a hardware wall.
   The record says which is which — we do not call it a guarantee we don't have.
3. **Our adversarial tests are ones we wrote.** A 100% pass rate over a set we designed
   is a floor, not a ceiling. The hostile test set needs to grow, and eventually the AI
   should be made to attack its own box.
4. **The safety numbers are instruments, not results.** We can now *measure* how often
   bad declarations are rejected and how often escapes fail. We haven't yet run those
   measurements at scale inside the live loop.
5. **No users, no revenue, no deployment.** Prototype stage, research-grade, one
   builder. The 1,000-cycle run happened on free tiers that then ran out — so
   "no drift observed" honestly means "no drift observed in the window we could
   afford."
6. **A known documentation gap:** one internal document cites a test file that is not
   in the repository, so some of its reported numbers currently have no producer. We
   recorded that instead of quietly dropping the claim.

*Compliance / IP / licensing status: `⟦fill in⟧`.*

---

## Slide 12 — What's next

| Stage | Plain-English goal | Done when |
| --- | --- | --- |
| **Now** | The two halves exist and are verified separately | ✅ 221 checks green, 1,000-cycle run recorded |
| **Next** | Put them in the same loop: let the agent write real code inside a live goal, and publish the safety numbers from that run | A safety metric from a live run, not a test set |
| **Then** | A second, stronger box: proper container-level isolation, so the memory and network promises stop being "fence" and start being "wall" | The record reads `wall`, not `fence` — verifiable by anyone |
| **Later** | Generated work must cite its sources the same way answers do | A fabricated result cannot be published |

Cheaper side-door if you'd rather ship sooner than finish the research: wrap the
audited-answering half as a plug-in that any agent can call. `⟦decide which you're
pitching⟧`

---

## Slide 13 — The ask

`⟦amount⟧` for `⟦months⟧`, to get from "verified components" to "verified in the live
loop on real hardware":

- `⟦£/€/$⟧` — hardware and paid inference so the long-run studies stop depending on
  free tiers (the reason the 1,000-cycle run ended).
- `⟦…⟧` — the second isolation backend (container-level).
- `⟦…⟧` — one design partner with a real, paperwork-heavy workload.

What we want beyond money: a workload where "a confident wrong answer" is genuinely
expensive. That's the test that makes the product.

---

# Appendix A — Tough questions, plain answers

**"Isn't this just an agent framework?"**
No. Frameworks give the model tools. This is the checkpoint the tools have to get
past, plus a durable record of what passed and how. It can sit under other frameworks.

**"Why not just use a container / existing sandbox?"**
We will — it's on the roadmap. Today's version exists because it has to run with
nothing installed, on a phone. The design takes the stronger box as a swap, not a
rewrite, and it already reports which protections are hard.

**"What stops the AI from escaping?"**
Several things stacked: it can't declare something it isn't allowed to do, the code
is read before it runs, the runtime blocks the known escape routes, the process can't
spawn children, and its output is never treated as instructions.

**"What if it just lies about being done?"**
It can't. Marking work as complete happens in the checker, not in the model, and the
record says how it was verified. A model's own claims are never sufficient.

**"Is this safe to put in front of customers?"**
The *answering* half is close — every sentence is traceable today. The *code-running*
half is a strong fence rather than a vault, and we say so. Selling it as anything
stronger would be exactly the dishonesty the product is built to catch.

**"Why hasn't a big lab done this?"**
They may. But the interesting part isn't a feature, it's the discipline: measured
limits, published failures, refusal as a valid result. That's a culture, and it's
harder to copy than code.

**"What's the demo?"**
One command. Ask it something your documents can't answer and watch it refuse. Then
show it building a small tool, running it under limits, and recording how it was
checked.

---

# Appendix B — Jargon swap table

Use the right-hand column on slides and in speech.

| We say internally | Say on the slide |
| --- | --- |
| RAG / grounded retrieval | Answers pulled from your documents, with receipts |
| Manifest / schema gate | A written declaration, checked before anything runs |
| Static analysis / banned imports | The code's read-through — what it's not allowed to touch |
| Sandbox / rlimits / seccomp | The box it runs in, with hard limits |
| `howVerified: gateChain` | The stamp that says *how* we checked it |
| Ledger | The permanent record |
| Drift loop / self-rewrite cycle | The agent rewriting its own instructions, one cycle at a time |
| Cascade / failover | It keeps working when one model provider stops answering |
| Q1–Q5, P1–P4 | The five things we're testing, in the order we're testing them |
| Citation fidelity / false refusal | It quotes real passages, and it doesn't say "I don't know" when it does |
| RSS poll / import blocklist | *(never say these on a slide)* — say "watched from outside" and "blocked by a deny-list" |
