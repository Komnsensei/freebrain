# Free Brain on GitLab CI (no card)

The fallback that actually works: GitHub Actions turned out to be blocked by an
account-wide billing lock (see *Why not GitHub* below), and GitLab's free tier
runs CI **without a card**.

Same loop, same residence, same tests. Pipeline file: [`.gitlab-ci.yml`](../../.gitlab-ci.yml).

---

## Why not GitHub

Every one of the 9 prior Actions runs on the account had concluded
`startup_failure` with **0 minutes used**, and every new job was refused with:

```
The job was not started because your account is locked due to a billing issue.
```

Because 0 minutes were ever consumed, this is not an unpaid bill — it is the
widely-reported **stale failed-authorization-hold** state (GitHub's own threads:
*"persisted even after adding a valid payment method"*, *"my billing page shows
no payment issues"*). It blocks Actions account-wide, **including public repos
where the minutes are free**, and it cannot be cleared from the CLI.

The public repo stays as the record — <https://github.com/Komnsensei/freebrain> —
and one free support ticket can lift the lock. This runbook is the way to keep
the study moving without waiting for that.

---

## Free tier, verified 2026-09-18

| | GitLab free tier |
|---|---|
| Compute minutes | **400 / month** (shared runners) |
| Card required | **no** |
| Scheduled pipelines | **yes** — works on free |
| Job timeout | 1 h default; raise in project settings if your plan allows |
| Cache | available (used here for the model) |

**The budget math.** The loop's own measurements bracket a cycle at roughly
15–60 s on a CPU runner. At a conservative 20 s/cycle, 400 minutes is about
**1,200 cycles** — so the complete 1,000-cycle study fits inside one month of
free minutes. Each job is capped at 50 minutes of loop time by
`DRIFT_MAX_SECONDS` (inside a 1 h job timeout), which is ~150 cycles per job;
the run resumes from `state.json` on the next pipeline, so nothing is lost
between jobs.

> Honest caveat: GitLab may ask a *flagged* new account for identity
> verification (usually a phone number). That is a GitLab abuse-control step, not
> a payment step — but it can appear, and it is worth knowing before you start.

---

## Setup

### 1. Account and project

Create an account at <https://gitlab.com/users/sign_up> (no card), then a new
project. Push the harness — the same file set as the GitHub repo:

```bash
cd /path/to/harness
git init -b main
git add -A
git commit -m "Free Brain: file-driven self-rewriting drift loop (Q4 harness)"
git remote add origin https://gitlab.com/<you>/freebrain.git
git push -u origin main
```

### 2. Let the job push the residence back

The pipeline commits `freebrain-residence/` after every run, so the record
survives the ephemeral runner. That needs a token:

1. **Settings → Access tokens** → create a **project access token** with the
   `write_repository` scope (role: Maintainer).
2. **Settings → CI/CD → Variables** → add a **masked** variable:
   - key: `GITLAB_PUSH_TOKEN`
   - value: the token
   - **masked**: yes · **protected**: no (schedule runs on the default branch)

Without this the job still runs and still stores artifacts (ledger, state,
evidence) for 30 days — it just can't write the branch, and the log says so
rather than pretending it succeeded.

### 3. Raise the job timeout (if your plan allows)

**Settings → CI/CD → General pipelines → Timeout**. If you can raise it past
1 h, also raise `DRIFT_MAX_SECONDS` in `.gitlab-ci.yml` — always leaving
**~10 minutes of margin**, because `--max-seconds` is a *start* gate and a cycle
that begins just before the deadline still runs to completion.

### 4. Schedule it

Once you are happy running it by hand: **CI/CD → Schedules → New schedule**,
target branch `main`, interval e.g. every 6 hours. Scheduled pipelines are
supported on the free tier, so the study advances unattended.

---

## Running it

**Manually:** CI/CD → Pipelines → **Run pipeline** (this is what the `web` rule
allows).

**Watch:** the job log carries the `[perf]` line per step (`tokens`, `elapsed_s`,
`tok/s`, `early_stop`) and the `[drift] cycle N gate=… coherence=… hash=…`
summary per cycle. The ledger, checkpoint and evidence are attached as artifacts
on every run, **including failures**.

**Exit codes** (from `drift_loop.py`): `0` complete or clean budget stop ·
`2` breaker trip / drift alarm · `3` determinism failure (Test 2 falsified).
All three are **results** — the pipeline persists them and stays green. A
genuine crash (anything else) is flagged red *after* the persist step, because
losing the record of a failure is worse than the failure.

---

## Two mistakes this pipeline is built to avoid

Both were caught by testing, and both would have cost a whole run's cycles:

1. **`exit 0` after the loop.** GitHub Actions runs each `run:` in its own
   shell, so exiting early there is harmless. **GitLab concatenates every
   `script:` line into one shell script** — an `exit 0` after the loop would end
   the job and skip the persist step entirely, discarding every cycle the run
   produced. The pipeline therefore captures the exit code (`cmd && A || B`),
   writes it to a file, and applies the verdict in a *final* step.
2. **The default runner image has no `python3`.** GitLab's default image is a
   Ruby one. The job pins `ubuntu:24.04` and installs `python3`/`curl` first, or
   it would die on line one.

---

## The phone is also running

As of 2026-09-18 the loop is live on the phone at **~6–7 tok/s** (not the
0.13 tok/s originally measured — that figure was contention, not a hardware
ceiling), i.e. ~15 s/cycle. Two writers on one residence would corrupt the
ledger, so **run the study on one host at a time**: if the phone is grinding,
leave the GitLab schedule paused, and vice versa. The residence is portable —
`state.json` is the resume point either way.
