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

## Live setup — completed 2026-09-18

This is not a plan; it is the state of the account.

| | |
|---|---|
| Project | <https://gitlab.com/Komnsensei/freebrain> (public) |
| Project id | `86614316` (needed for API calls) |
| Default branch | `main` |
| `GITLAB_PUSH_TOKEN` | set, **masked** — runs can persist the residence |
| Schedule | `0 */6 * * *` UTC, id `4448457`, **paused on purpose** |
| Smoke schedule | id `4448474`, carries `SMOKE=1`, inactive — *play* it by hand |
| Shared runners | enabled; job timeout 1 h (`build_timeout: 3600`) |

`deploy/gitlab/setup.sh` did all of that in a single run — token verified,
project created, branch pushed, variable created, schedule created — so it is
idempotent and safe to re-run.

**The schedule is paused deliberately.** The phone is at **cycle 66**; the pushed
branch is a snapshot at **cycle 42**. Triggering a run now would start a *second*
lineage from cycle 43 and diverge from the phone's record — and two lineages
cannot be merged afterwards. See *Handing off* below for the one-step swap.

### Smoke it before trusting it

The GitHub failure was **account-level**, and from inside a repository that looks
exactly like a broken pipeline. So the question worth answering first is not "is
our YAML right" but "does a job start here at all". The pipeline has a job for
that — but on this project it **cannot** be started the usual way.

**Do not use CI/CD → Run pipeline with a variable.** GitLab refuses it:

```json
{"message":{"base":["Insufficient permissions to set pipeline variables"]}}
```

That is not a token problem — the same token created the project, the CI variable
and the schedules. It is a project setting:

| Setting | Value |
|---|---|
| `ci_pipeline_variables_minimum_override_role` | `no_one_allowed` |
| `restrict_user_defined_variables` | `true` |

GitLab's defaults block *pipeline-level* overrides for everyone. **Schedule**
variables are a different object and still work — so the smoke run is a
dedicated schedule:

| | |
|---|---|
| Description | `freebrain smoke (manual only)` — id `4448474` |
| Variable | `SMOKE=1` (on the schedule, not the pipeline) |
| Cron | `0 0 29 2 *` — Feb 29, so it only fires in a leap year |
| Active | no — it exists to be *played* |

Play it from **CI/CD → Schedules → ▶**, or
`POST /projects/86614316/pipeline_schedules/4448474/play`.
(To use the normal UI path instead, raise **Settings → CI/CD → General pipelines →
Minimum role to override variables** to Developer or above.)

It reports the runner spec, installs Ollama, pulls (and *caches*) the model,
verifies `GITLAB_PUSH_TOKEN` can actually reach the repo, runs all four test
suites, and then runs **three real cycles against a copy of the residence in
`/tmp`** — `DRIVE_RESIDENCE` is what selects the home, so nothing in the record
is touched. It closes by asserting the tracked residence is byte-identical, so a
smoke run is *incapable* of forking the drift lineage. The `drift` job is
explicitly skipped when `SMOKE=1`.

This also settles the identity-verification caveat above empirically: either the
job starts, or it does not.

### Handing off from the phone

One host at a time. To move the study from the phone to GitLab:

1. Stop the phone's watchdog (or two writers keep going).
2. Copy the phone's **current** residence over this repo's, so the branch is
   ahead of the record rather than behind it.
3. Commit and push to `main`.
4. **Then** unpause the schedule (CI/CD → Schedules).

The reverse direction is the same swap. `state.json` is the resume point either
way, so no cycles are lost in transit — but a *forked* record is not recoverable,
which is why pausing is the default rather than a suggestion.

---

## Setup

Only the **account** needs doing by hand — everything after it is one command.

### 1. Create the account (the part that cannot be automated)

<https://gitlab.com/users/sign_up> — no card. Then **confirm the emailed
verification link**: GitLab blocks API access until it is confirmed, which looks
like a broken token rather than an unverified email. `setup.sh` detects that case
and says so explicitly.

### 2. Create a Personal Access Token

**User settings → Access tokens**, with **both** scopes:
- `api` — create the project, variables and schedules
- `write_repository` — push the harness and let runs commit the residence

### 3. Run the setup script

```bash
cd /path/to/harness
GITLAB_TOKEN=glpat-xxxxxxxx bash deploy/gitlab/setup.sh
```

`deploy/gitlab/setup.sh` does the rest, idempotently — safe to re-run:

| Step | What it does |
|---|---|
| verify | calls `/user`; explains a rejected token or an unconfirmed email |
| project | creates `freebrain` (or reuses the existing one) |
| push | pushes the current branch to `gitlab.com/<you>/freebrain` |
| variable | sets `GITLAB_PUSH_TOKEN` as a **masked** CI variable |
| schedule | creates a pipeline schedule (`0 */6 * * *` UTC) |
| trigger | starts the first pipeline and prints its URL |

Useful flags: `--public`, `--project NAME`, `--cron '0 */4 * * *'`,
`--no-trigger`, `--source DIR`.

> **Why the token goes in the environment, not the command line:** `--token`
> exists, but an argument is visible to `ps`. `GITLAB_TOKEN` (or `--token-file`)
> keeps it out of the process table. The push itself uses an inline credential
> URL, and the resulting remote is stored **without** the token, so nothing
> sensitive lands in `.git/config`.

### 4. Let the job push the residence back

Step 3 already set the `GITLAB_PUSH_TOKEN` variable, which is what allows the
pipeline to commit `freebrain-residence/` after every run so the record survives
the ephemeral runner. Without it the job still runs and still stores artifacts
(ledger, state, evidence) for 30 days — it just cannot write the branch, and the
log says so rather than pretending it succeeded.

### 5. Raise the job timeout (if your plan allows)

**Settings → CI/CD → General pipelines → Timeout**. If you can raise it past
1 h, also raise `DRIFT_MAX_SECONDS` in `.gitlab-ci.yml` — always leaving
**~10 minutes of margin**, because `--max-seconds` is a *start* gate and a cycle
that begins just before the deadline still runs to completion.

### 6. Check the schedule

Step 3 already created one (`CI/CD → Schedules`). Edit or pause it there if you
want to run by hand first — scheduled pipelines are supported on the free tier,
so the study advances unattended once it is on.

> **Run the study on ONE host at a time.** The phone watchdog and this pipeline
> would both write the same residence; two writers interleave cycles and corrupt
> the drift record — worse than no record. Pause the schedule while the phone is
grinding, and vice versa.

---

## Running it

**Manually:** CI/CD → Pipelines → **Run pipeline** (this is what the `web` rule
allows). To first prove the *runner* works without touching the record, run with
`SMOKE=1` — see *Smoke it before trusting it* above.

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

## Three mistakes this pipeline is built to avoid

All three were caught by testing, and each would have silently cost the study:

1. **`exit 0` after the loop.** GitHub Actions runs each `run:` in its own
   shell, so exiting early there is harmless. **GitLab concatenates every
   `script:` line into one shell script** — an `exit 0` after the loop would end
   the job and skip the persist step entirely, discarding every cycle the run
   produced. The pipeline therefore captures the exit code (`cmd && A || B`),
   writes it to a file, and applies the verdict in a *final* step.
2. **The default runner image has no `python3`.** GitLab's default image is a
   Ruby one. The job pins `ubuntu:24.04` and installs `python3`/`curl` first, or
   it would die on line one.
3. **`resource_group` written at the top level.** It is a *job* keyword. At root
   level GitLab parses it as a job named `resource_group` with no script — which
   makes the **entire config invalid**, and not loudly: every pipeline then
   produces **zero jobs**, which reads exactly like "the runner never started".
   That is the failure that actually bit this project: a hand-rolled validator
   passed the file (stages declared, every embedded shell block `bash -n`-clean)
   while GitLab's own parser rejected it in one call. `setup.sh` now lints against
   `POST /projects/:id/ci/lint` **before** anything depends on the config, and
   an invalid config is a hard failure rather than a silent no-op. The general
   lesson: validate CI config with the CI system's own parser, not a local
   approximation of it.

---

## The phone is also running

As of 2026-09-18 the loop is live on the phone at **~6–7 tok/s** (not the
0.13 tok/s originally measured — that figure was contention, not a hardware
ceiling), i.e. ~15 s/cycle, and the phone is at **cycle 66**. Two writers on one
residence would interleave cycles and corrupt the ledger, so **run the study on
one host at a time**: while the phone is grinding, the GitLab schedule stays
paused, and vice versa. The residence is portable — `state.json` is the resume
point either way.
