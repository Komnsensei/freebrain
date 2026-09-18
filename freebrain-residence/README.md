# Free Brain Residence

This folder is the agent's home — its persistent self. It lives on this machine
and mirrors to a Google Drive remote (rclone) when DRIVE_REMOTE is set
(e.g. DRIVE_REMOTE=gdrive:freebrain).

  models/         local model store target (OLLAMA_MODELS) — the weights that make this brain run
  instructions.md the mutable instruction set (what the self-rewrite loop rewrites, Q4)
  state.json      checkpoint for resume-on-crash
  ledger.jsonl    invariant/observed ledger records
  evidence/       measured throughput: q1-evidence.jsonl, drift-curve.jsonl
  generated/      model-built tool-chains (gated, sandboxed from P1)

The model can only touch this folder through the allowlisted drive_sync tool
(status|push|pull). It can never sync arbitrary paths.
