# QIH Residence

This folder is the agent's home — its persistent self, "the wave that creates the
instance" (QIH.md §I.3). It lives on this machine and mirrors to a Google Drive
remote (rclone) when DRIVE_REMOTE is set (e.g. DRIVE_REMOTE=gdrive:qih), so the
instance's memory is portable and survives any box dying — sovereign continuity
in the repo's honest sense: crash → pull → resume from state.json.

```
  instructions.md      the mutable self — what the self-rewrite loop rewrites
  state.json           checkpoint {instance, run, cycle, phase, activation, objective}
  ledger.jsonl         append-only records: activation birth, gates, coherence, throughput
  horizon-register/    |ψ⟩_screen — the screen register (mirrors core/horizon.py)
  coupling-maps/       |ψ⟩_obs — coupling to the local register (bio_readout/coupling.py)
  spectral-time/       phase-clock dτ=(Ω₀/Ω)dt records (rendering/spectral_time.py)
  evidence/            measured throughput: q1-evidence.jsonl, drift curves
  generated/           model-built tool-chains (gated, sandboxed from P1)
```

The model can only touch this folder through the allowlisted drive_sync tool
(status|push|pull). It can never sync arbitrary paths.

Activate: `python3 agent_runtime.py --activate` (gated bootstrap, QIH.md §IV).
