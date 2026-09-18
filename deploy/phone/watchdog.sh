#!/usr/bin/env bash
# watchdog.sh — run the Free Brain drift loop on a phone (Termux), surviving the
# three ways it actually dies there.
#
# WHY THIS REPLACES A PLAIN RESTART LOOP
# The previous supervisor was `while true; do python3 drift_loop.py; sleep 10; done`
# and it did nothing for nine days while a cycle sat hung, because it only
# restarted a process that had EXITED. A process stuck inside a stalled model
# call is still alive, so a death-watch sees nothing wrong. This watches for
# PROGRESS instead:
#
#   1. hung loop      → no new ledger bytes for STALL_SECONDS → kill -9, restart
#   2. dead loop      → process gone → restart
#   3. ollama down    → API unreachable → restart the daemon, then the loop
#
# Usage:
#   bash deploy/phone/watchdog.sh                 # foreground
#   setsid nohup bash deploy/phone/watchdog.sh >/dev/null 2>&1 &
#
# Note: the loop now ALSO has its own wall-clock budget per model call
# (LOCAL_MODEL_STREAM_BUDGET_MS), so a stuck call self-aborts. This watchdog is
# the second line of defence — it also covers a wedged process, a dead daemon,
# and a phone that killed the tree.
set -uo pipefail

APP_DIR="${FREE_BRAIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL="${LOCAL_MODEL:-qwen2.5-coder:1.5b}"
URL="${LOCAL_MODEL_URL:-http://127.0.0.1:11434/v1}"
CYCLES="${FREE_BRAIN_CYCLES:-1000}"
# A cycle here takes ~10-12 min, so 30 min of silence means it is wedged.
STALL_SECONDS="${FREE_BRAIN_STALL_SECONDS:-1800}"
CHECK_SECONDS="${FREE_BRAIN_CHECK_SECONDS:-60}"
LOG="${FREE_BRAIN_LOG:-$APP_DIR/drift-run.log}"
RESIDENCE="${DRIVE_RESIDENCE:-freebrain-residence}"
[[ "$RESIDENCE" = /* ]] || RESIDENCE="$APP_DIR/$RESIDENCE"
LEDGER="$RESIDENCE/ledger.jsonl"

log() { printf '%s  [watchdog] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG"; }

cd "$APP_DIR" || { echo "cannot cd to $APP_DIR"; exit 1; }
: >> "$LOG"

# Android kills backgrounded Termux trees without a wake-lock.
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock 2>/dev/null && log "wake-lock acquired"
else
  log "termux-wake-lock not found — the phone may kill this tree when backgrounded"
fi

export LOCAL_MODEL_URL="$URL"
export LOCAL_MODEL="$MODEL"
export DRIVE_RESIDENCE="$RESIDENCE"
export EVIDENCE_FILE="$RESIDENCE/evidence/q1-evidence.jsonl"
# Total wall-clock cap per model call. This is the fix for the nine-day hang:
# the old 300 s LOCAL_MODEL_TIMEOUT_MS is only a PER-READ timeout, which a
# trickling stream never trips.
export LOCAL_MODEL_STREAM_BUDGET_MS="${LOCAL_MODEL_STREAM_BUDGET_MS:-900000}"

ensure_ollama() {
  curl -fsS -m 5 "${URL%/v1}/api/tags" >/dev/null 2>&1 && return 0
  log "ollama API unreachable — starting the daemon"
  if command -v ollama >/dev/null 2>&1; then
    setsid nohup ollama serve >>"$APP_DIR/ollama.log" 2>&1 &
  fi
  for i in $(seq 1 45); do
    curl -fsS -m 3 "${URL%/v1}/api/tags" >/dev/null 2>&1 && { log "ollama is up"; return 0; }
    sleep 2
  done
  log "ollama did NOT come up — check $APP_DIR/ollama.log"
  return 1
}

progress_marker() {
  # Bytes in the ledger: append-only, so growth is real progress. Falls back to
  # the checkpoint's cycle count before the first line exists.
  if [ -f "$LEDGER" ]; then
    wc -c < "$LEDGER" 2>/dev/null | tr -d ' '
  else
    echo "0"
  fi
}

consecutive_fast_exits=0
log "watchdog starting — model=$MODEL cycles=$CYCLES stall=${STALL_SECONDS}s budget=${LOCAL_MODEL_STREAM_BUDGET_MS}ms"

while true; do
  ensure_ollama || { sleep 60; continue; }

  log "starting drift_loop (cycle $(python3 -c "
import json
try: print(json.load(open('$RESIDENCE/state.json'))['cycle'])
except Exception: print('?')
" 2>/dev/null || echo '?'))"
  started=$(date +%s)
  python3 -u drift_loop.py --cycles "$CYCLES" >>"$LOG" 2>&1 &
  LOOP_PID=$!
  last_marker="$(progress_marker)"
  last_change=$(date +%s)

  while kill -0 "$LOOP_PID" 2>/dev/null; do
    sleep "$CHECK_SECONDS"
    kill -0 "$LOOP_PID" 2>/dev/null || break
    marker="$(progress_marker)"
    if [ "$marker" != "$last_marker" ]; then
      last_marker="$marker"
      last_change=$(date +%s)
      continue
    fi
    idle=$(( $(date +%s) - last_change ))
    if [ "$idle" -ge "$STALL_SECONDS" ]; then
      log "STALL: no ledger progress for ${idle}s (pid $LOOP_PID alive but wedged) — killing it"
      kill -9 "$LOOP_PID" 2>/dev/null
      sleep 2
      break
    fi
  done

  wait "$LOOP_PID" 2>/dev/null
  rc=$?
  ran=$(( $(date +%s) - started ))
  log "drift_loop exited rc=$rc after ${ran}s"

  # A loop that dies instantly is a configuration problem (no model, server
  # down), not a crash: back off instead of spinning and draining the battery.
  if [ "$ran" -lt 30 ]; then
    consecutive_fast_exits=$(( consecutive_fast_exits + 1 ))
  else
    consecutive_fast_exits=0
  fi
  if [ "$consecutive_fast_exits" -ge 3 ]; then
    log "3 fast exits in a row — backing off 300s (check the model name and the server)"
    sleep 300
    consecutive_fast_exits=0
  else
    sleep 10
  fi
done
