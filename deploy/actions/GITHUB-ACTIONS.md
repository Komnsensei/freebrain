# Free Brain on GitHub Actions (no card, no billing)

The card-free path to the 1,000-cycle run. Same residence, same checkpoint, same
ledger — a different box that dies every few hours, which the loop was already
built to survive.

**Why this fits better than it looks:** a CI runner is exactly the phone's
failure mode, made predictable instead of random. The phone got OOM-killed
mid-cycle whenever it was used; a runner gets killed mid-run at a known wall. We
already solved that — `state.json` is the checkpoint, `ledger.jsonl` is the
record, and git is the transport. Nothing about the loop changes.

---

## Free-tier facts (verified 2026-09-18)

| | Public repo | Private repo |
|---|---|---|
| Runner | **4 vCPU / 16 GB** / 14 GB SSD | **2 vCPU / 8 GB** / 14 GB SSD |
| Minutes | **Unlimited, free** | **2,000 min/month** (Free plan) |
| `schedule` (cron) | ✅ works | ❌ **disabled on free personal accounts** |
| Card required | no | no |
| Artifact storage | 500 MB | 500 MB |
| Actions cache | 10 GB per repo | 10 GB per repo |
| Job wall | **6 h hard kill** | **6 h hard kill** |

Two of those rows decide the whole design:

1. **`schedule` does not fire on private repos for free personal accounts.**
   It is a deliberate, poorly-documented limitation (scheduled workflows need a
   public repo or GitHub Pro). `workflow_dispatch` still works, so a private
   repo needs something external to pull the trigger.
2. **The 6-hour kill is the real engineering constraint**, and it is why
   `drift_loop.py` grew a `--max-seconds` budget (see below).

---

## Which repo? Read this before picking

The safest split, and the one that serves the project: **a dedicated repository
for the Free Brain harness and its residence — not this one.** This repo carries
unrelated private work; the harness is self-contained (stdlib Python, no deps).

A dedicated public repo is not a compromise here — it is the deliverable. Q4's
stated output is *a drift curve reported honestly*, pass or fail. Publishing the
harness that produces it, and the raw curve it produced, is the publication.

Minimum contents of that repo:

```
drift_loop.py            the loop
agent_runtime.py         config, streaming, per-step evidence capture
FREE-BRAIN.md            the record it is producing
freebrain-residence/     the machine: state.json, ledger.jsonl, instructions.md
.github/workflows/drift-runner.yml
```

If you would rather keep it private, you still have two ways to run it — see
*Making a private repo actually run* below.

---

## The `--max-seconds` budget — why it exists

A 6-hour wall with no warning is fatal to a loop that only persists at the end:
a kill mid-run means the commit step never executes, and **every cycle of that
run is lost**. So the loop now stops *itself*, on a cycle boundary, before the
wall:

```bash
python3 drift_loop.py --cycles 1000 --max-seconds 19200   # 5 h 20 m
```

- The budget is checked **before starting each cycle**, never inside one — a
  cycle is never torn, because a half-written cycle would corrupt the ledger.
- **It is a start gate, so the run can overrun the budget by up to one full
  cycle.** That overrun is the reason for the margin: 5 h 20 m inside a 6 h wall
  leaves 40 minutes, which is many cycles of slack.
- On expiry it returns **0** — a clean stop, not an error — so the persist step
  still runs and the next host resumes at the right cycle.
- Exit **0** complete-or-budgeted · **2** breaker trip · **3** determinism fail.

The workflow's persist step is `if: always()` on purpose: a breaker trip and a
determinism failure are *results*, and losing the record of a failure is worse
than the failure.

---

## Setup

1. **Create the repo** and push the harness files above (main branch).

2. **Enable Actions** (Actions tab → enable workflows if prompted).

3. **Check the runner shape and wallet are as expected.** The workflow prints
   `nproc` / RAM and whether the repo is private in its first step, and the job
   summary shows the real exit code — so the run's conditions are in the record,
   not assumed.

4. **Run it.** Actions → *Free Brain drift run* → **Run workflow**, or:

   ```bash
   gh workflow run drift-runner.yml -f cycles=1000 -f model=qwen2.5-coder:3b
   ```

5. **Watch it without watching it.** The job summary carries the last 10 cycles
   (gate, coherence, graph hash, tok/s) and the count of distinct graph hashes;
   the ledger and state are also uploaded as artifacts (30-day retention).

The workflow already wires what a hand-rolled setup forgets: a `concurrency`
group so two runs can never interleave cycles on one checkpoint, a model cache
so the ~2 GB weights aren't re-downloaded every run, and `BRAIN_CASCADE=0`,
which is the zero-API-calls configuration P6 is actually testing.

---

## Making a private repo actually run

`schedule` won't fire. Either:

- **External cron → `workflow_dispatch`.** A free scheduler (e.g. cron-job.org)
  calls the dispatch API with a fine-grained PAT scoped to *Actions: write*.
- **Self-chaining (no third party).** Set `FREE_BRAIN_CHAIN=1` as a repo
  *variable* and `FREE_BRAIN_PAT` as a *secret*, and each run dispatches its own
  successor. Falls back to a no-op when the secret is absent, so it can't
  half-configure itself.

  It is off by default for a reason: **an unattended loop that re-arms itself is
  also an unattended loop that can run forever.** Turn it on when you are
  watching the curve, not before.

---

## Budget math (estimates — measure, then correct this table)

The phone measured **0.13 tok/s**, ~700 s/cycle with a 1.5b. A cycle is ~96
tokens. A 4-vCPU runner with a 3b model should be **far** faster, but the honest
number does not exist until the first runner cycle is recorded — the harness
writes a `[perf]` line and a JSONL record for every step, so the first run
produces the measurement.

Planning ranges only:

| Host | Model | Cycle (est.) | 1,000 cycles |
|---|---|---|---|
| Phone (measured) | 1.5b | ~700 s | ~8 days |
| Actions runner, 2 vCPU | 3b | ~40–90 s | ~11–25 h |
| Actions runner, 4 vCPU | 3b–7b | ~15–40 s | ~4–11 h |

Against the private-repo allowance: 1,000 cycles at ~30 s is ~500 minutes, well
inside the **2,000 min/month**. At a pessimistic 60 s/cycle it is ~1,000 minutes,
still inside. On a **public** repo the minutes are unlimited, so the constraint
is only the work itself.

---

## Honest constraints

- **Terms of service.** GitHub's Actions terms restrict usage to work related to
  the repository's software — "any other activity unrelated to the production,
  testing, deployment, or publication of the software project". This loop
  generates, commits and publishes its own research artifact, which is a
  defensible reading; a project that merely borrowed a runner for unattended
  compute would not be. Publishing the repository (rather than hiding compute in
  it) is the honest way to stay on the right side of that line.
- **Scheduled runs are best-effort.** Cron can be delayed and is dropped under
  load. This is fine here — a late run resumes, it doesn't restart — but it is
  why the study's wall-clock time won't match a neat arithmetic prediction.
- **A run that is killed at the wall loses at most one cycle**, and 0 if the
  budget did its job. The budget, not luck, is what makes that true.
- **Convergence is still not success.** Across the phone's last 20 cycles there
  were **2 distinct graph hashes**, not 1 — `d477938e…` dominant, with a single
  excursion at cycle 28. So the graph is *sticky*, not frozen. Sticky-with-an-
  excursion is a different claim from "converged", and neither is evidence of a
  working mind. Only the full curve plus Test 2 settles it, and a flat line gets
  reported as a flat line.
- **`schedule` on private/free is the trap that wastes a day** if you don't know
  it. Everything works on `workflow_dispatch`, so it looks like a broken cron.
  It isn't — it's the plan tier.
