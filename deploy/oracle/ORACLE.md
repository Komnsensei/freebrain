# Free Brain on Oracle Cloud Always Free

The 1,000-cycle drift run (Q4 / `FREE-BRAIN.md` §3–4) needs a box that stays on.
The phone proved the loop is real — **32 cycles, coherence 1.00, graph hash
stable at `d477938e…`** — but it burns ~11 minutes per cycle (0.13 tok/s) and
gets OOM-killed whenever the phone is used. Oracle Cloud's Always Free Ampere
ARM VM is the cheapest honest fix: **$0, always on, 10–50× the throughput.**

Nothing about the loop changes. This is the same residence, same checkpoint,
same ledger — moved to a box that isn't being texted on.

> **Card-free alternative — start here if you don't want to hand over a card.**
> Oracle requires one at signup (never charged for Always Free, but required).
> If that is a non-starter, `deploy/actions/GITHUB-ACTIONS.md` runs the *same*
> loop on free GitHub Actions runners with no card and no billing at all. It
> works because of the design already in place: the runner is killed every few
> hours and the residence simply resumes. Use Oracle when you want a genuinely
> always-on box; use Actions when you want zero payment friction.

---

## What's in this directory

| File | What it does |
|---|---|
| `bootstrap.sh` | One-shot, idempotent installer (Ollama + model + systemd units + swap) |
| `sync-residence.sh` | The mirror itself, called by the timer's service (logic in bash, not in a unit line) |
| `freebrain-loop.service` | systemd **is** the supervisor: crash → restart → resume from `state.json` |
| `freebrain-sync.service` / `.timer` | Mirror the residence to Google Drive every 5 min |
| `migrate-residence.sh` | Move the phone's live residence to the VM **without losing cycles** |
| `freebrain.env.example` | The runtime env the service reads |

---

## Step 1 — the account (the only slow part, do it first)

1. Go to **<https://www.oracle.com/cloud/free/>** and sign up.
   A credit card is required for identity verification and **is not charged**
   for Always Free resources. Approval can take minutes to a couple of days.
2. **Pick your home region deliberately — it cannot be changed later.**
   Choose one physically near you; latency matters if you ever drive the box
   interactively.
3. When the console is available, **check the Always Free badge on the shape**,
   not the marketing page. Oracle changed the Ampere A1 allowance (the widely
   cited 4 OCPU / 24 GB was halved to **2 OCPU / 12 GB** at one point). Whatever
   the console says is the truth for your account.

**Known friction — "Out of host capacity" on A1:** the free ARM shape is
oversubscribed. Mitigations, in order of how well they work:

- Retry later, and try each **availability domain** (AD-1/2/3) separately —
  capacity differs per AD.
- Choose a quieter region if you're not attached to one.
- Upgrading the account to **Pay-As-You-Go** improves capacity priority. Always
  Free resources stay free under PAYG; you just get asked for a card up front.
  Set a **budget alert at $0** so any accidental paid resource screams at you.

> Honest caveat: "always free" is a vendor promise, not physics. The design
> here never depends on it — the residence is portable and the mirror keeps the
> run alive if the VM vanishes. That's what `state.json` is for.

**Note on permissions:** invoke these scripts as `bash deploy/oracle/<script>.sh`.
Android's emulated storage drops the execute bit, so a copy coming off the phone
may not be `+x` — running it through `bash` explicitly sidesteps that.

---

## Step 2 — create the VM

In the console: **Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Image | **Ubuntu 24.04** (aarch64) |
| Shape | **VM.Standard.A1.Flex** — as many OCPU/GB as the Always Free badge allows (aim 2–4 OCPU / 12–24 GB) |
| Boot volume | default (~47 GB) is plenty |
| SSH keys | **Paste a public key** (see below) |
| Networking | defaults are fine — **do not open any inbound ports** |

Generate the key **on your phone** (Termux):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle_freebrain -C freebrain
cat ~/.ssh/oracle_freebrain.pub          # paste this into the console
```

The loop is **outbound-only**: it talks to `127.0.0.1:11434`. No inbound rule,
no public port, no exposed API. SSH (22) is open by default in the VCN security
list and is all you need.

---

## Step 3 — connect from the phone

```bash
chmod 600 ~/.ssh/oracle_freebrain
ssh -i ~/.ssh/oracle_freebrain ubuntu@<VM_PUBLIC_IP>
```

First thing on the box — confirm it is what we think it is:

```bash
nproc; free -h; uname -m      # expect >=2 cores, ~12–24 GB, aarch64
```

---

## Step 4 — get the repo onto the box

From the **phone** (Termux), with the repo's parent as the cwd. Keep
`node_modules/` off the box — the harness is stdlib Python, it doesn't need it:

```bash
rsync -av --exclude node_modules --exclude .git --exclude __pycache__ \
  /mnt/sdcard/Download/builderbro/ ubuntu@<VM_PUBLIC_IP>:/opt/builderbro/
```

`/opt` is root-owned, so if rsync can't write there, either `sudo mkdir -p /opt/builderbro && sudo chown ubuntu /opt/builderbro`
first, or land it in `~/builderbro` and pass `FREE_BRAIN_DIR=$HOME/builderbro`
to the bootstrap.

---

## Step 5 — bootstrap the box

On the VM:

```bash
cd /opt/builderbro
sudo FREE_BRAIN_MODEL=qwen2.5-coder:7b bash deploy/oracle/bootstrap.sh
```

That script, in order: installs base packages → **adds a 4 GB swapfile** (the
default 1 GB will OOM a 7b model load) → installs **Ollama** with
`OLLAMA_KEEP_ALIVE=-1` (a continuous loop should never pay a reload) → pulls the
model → verifies the repo and residence → installs the three systemd units →
runs `agent_runtime.py --check` against the live server.

Model choice: **`qwen2.5-coder:7b`** if the box has ≥12 GB (best coherence per
cycle), `qwen2.5-coder:3b` if it has less. The phone ran `1.5b` and still
reached coherence 1.00 — model size is not what was limiting us.

---

## Step 6 — move the residence (don't start from zero)

**From the phone.** This ships the live run — 32 cycles of history, the evolved
`instructions.md`, the breaker state — so the VM resumes mid-wave:

```bash
cd /mnt/sdcard/Download/builderbro
bash deploy/oracle/migrate-residence.sh ubuntu@<VM_PUBLIC_IP>
```

It refuses to overwrite a target that is ahead of the source unless you pass
`--force` — protecting you from silently rewinding a run.

Before starting the service, make sure the box agrees on where the residence
is:

```bash
grep DRIVE_RESIDENCE /etc/freebrain.env
# must be /opt/builderbro/freebrain-residence
sudo systemctl restart ollama    # picks up KEEP_ALIVE from the drop-in
```

*(Or work in Drive-land instead of phone→VM: `--drive gdrive:freebrain`, then
`rclone sync gdrive:freebrain $DRIVE_RESIDENCE` on the box.)*

---

## Step 7 — start the wave

```bash
sudo systemctl start freebrain-loop
journalctl -fu freebrain-loop              # live
tail -f /opt/builderbro/freebrain-residence/ledger.jsonl
```

`systemd` is the supervisor — the role `/tmp/drift-supervisor.sh` played on the
phone. `Restart=on-failure` + resume-from-`state.json` means an OOM kill or a
reboot costs one cycle, not the run. Exit codes **2** (breaker trip / drift
alarm) and **3** (determinism failure) are declared as *success* on purpose:
those are **deliberate terminal states**, not crashes. A breaker trip stops the
run and stays stopped, which is exactly what `AGENT-INTEGRITY.md` says it should
do — restarting into a tripped breaker would be the loop lying to itself.

---

## Step 8 — watch it from the phone

Two ways, both read-only:

```bash
# 1. straight over SSH — no setup
ssh -i ~/.ssh/oracle_freebrain ubuntu@<VM_PUBLIC_IP> \
  'tail -f /opt/builderbro/freebrain-residence/ledger.jsonl'

# 2. the Drive mirror — survives the VM, readable from any screen
ssh -i ~/.ssh/oracle_freebrain ubuntu@<VM_PUBLIC_IP> 'rclone config'   # once
ssh -i ~/.ssh/oracle_freebrain ubuntu@<VM_PUBLIC_IP> \
  'sudo sed -i "s|^# DRIVE_REMOTE=.*|DRIVE_REMOTE=gdrive:freebrain|" /etc/freebrain.env && sudo systemctl enable --now freebrain-sync.timer'
```

The mirror pushes `state.json`, `ledger.jsonl`, `instructions.md` and
`evidence/` every 5 minutes. Then you can read the drift curve on the phone
without ever touching the box.

---

## What to expect (and record)

The phone measured **0.13 tok/s**; a cycle was ~94–96 tokens ≈ 11 minutes. On
the A1, a 7b Q4 model on 2–4 ARM cores should land in the **single-digit
tok/s** range — but that is an *expectation*, not a measurement, and this
project's whole point is not to publish expectations as results.

So: open a shell on the box, run one real cycle, and put the number in
`FREE-BRAIN.md` §1 with the rig documented (cores, RAM, model, quant). The
harness already does the capture — every step appends a
`[perf]` line and a JSONL record with `tokens`, `elapsed_s`, `tokens_per_s`:

```bash
python3 drift_loop.py --cycles 1        # then read the [perf] line
```

Rough scaling, for planning only:

| Box | Model | Cycle time (est.) | 1,000 cycles |
|---|---|---|---|
| Phone (measured) | 1.5b | ~11 min | ~8 days |
| A1, 2 cores | 3b | ~1–3 min | ~1–2 days |
| A1, 4 cores | 7b | ~20–60 s | ~6–16 hours |

Also worth running on real hardware, now that it's affordable: the **Test 2
determinism battery** —

```bash
python3 drift_loop.py --determinism 5
```

Identical inputs + pinned temperature must yield identical dispatch-graph
hashes. If they don't, Test 2 is falsified and the doc says so.

---

## The honest part

- **This does not change what the loop is.** It is the same file-driven state
  machine; the VM only removes the two things that were killing it — CPU
  starvation and Android's process reaper.
- **Convergence ≠ success.** Cycles 3–40 on the phone all produced the *same*
  graph hash with coherence 1.00. That is either a stable self-model or a loop
  that has stopped exploring. The 1,000-cycle run is what tells the two apart,
  and a **flat line must be reported as a flat line**, not as "stability."
- **The residence is the only irreplaceable thing.** Any box can be thrown
  away and rebuilt from it in minutes. That's why every step above that touches
  the host is disposable, and every step that touches the residence is guarded.
