#!/usr/bin/env bash
# sync-residence.sh — mirror the residence to the Drive remote (rclone).
# Called by freebrain-sync.service. Kept as a script rather than an inline
# ExecStart because systemd does its own `$VAR` expansion inside quotes — a
# shell-style "${DRIVE_REMOTE:-}" in a unit file is not shell parsing and does
# not behave the way it reads. Logic belongs in bash, not in a unit line.
#
# Contract (matching the unit's SuccessExitStatus): always exit 0 unless a real
# mirror ran and failed. The local residence is the source of truth; a failed
# mirror must never look like a failed run.
set -uo pipefail

RESIDENCE="${DRIVE_RESIDENCE:-freebrain-residence}"
REMOTE="${DRIVE_REMOTE:-}"

log() { printf '[freebrain-sync] %s\n' "$*"; }

if [[ -z "$REMOTE" ]]; then
  log "DRIVE_REMOTE unset — nothing to mirror (local residence is still the truth)"
  exit 0
fi

if [[ ! -d "$RESIDENCE" ]]; then
  log "no residence at $RESIDENCE — nothing to mirror"
  exit 0
fi

if ! command -v rclone >/dev/null 2>&1; then
  log "rclone not installed — cannot mirror to $REMOTE"
  exit 0
fi

# --checksum: content, not timestamps (the ledger is append-only, so this is
# cheap and exact). --create-empty-src-dirs keeps the residence layout intact
# so a pull onto a fresh box reconstructs the machine, not just the files.
log "mirroring $RESIDENCE -> $REMOTE"
if rclone sync "$RESIDENCE" "$REMOTE" \
     --checksum --create-empty-src-dirs --log-level INFO; then
  CYCLES=$(python3 -c "import json;print(json.load(open('$RESIDENCE/state.json')).get('cycle','?'))" 2>/dev/null || echo '?')
  log "ok — checkpoint at cycle $CYCLES is now on Drive"
  exit 0
else
  rc=$?
  log "rclone failed (exit $rc) — residence is unchanged locally; timer will retry"
  exit "$rc"
fi
