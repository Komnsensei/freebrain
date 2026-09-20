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
# Cascade-only mode. When this device cannot serve local inference, waiting on
# the local provider is pure cost: measured 2026-09-18, a 0.5b model answered a
# 51-token prompt in 85 s on an idle server (~0.1 tok/s) — ~100x slower than the
# same setup earlier the same night, and 317% CPU with zero faults and zero disk
# I/O, so it is not memory, not emulation and not prompt size. With local skipped
# each call fails over to the free hosted open-weight providers immediately,
# which is also the only mode that keeps the phone cool. Either way the ledger
# records the provider that actually served each cycle.
SKIP_LOCAL="${FREE_BRAIN_SKIP_LOCAL:-0}"
# Auto-resume a tripped breaker. The breaker is a safety guard, but a trip is
# also a NORMAL outcome for a 1000-cycle study: a hosted model emits malformed
# JSON occasionally, two in a row trips `consecutive-same-signature`, and without
# this the study stops every few cycles and needs a human — measured 2026-09-18,
# it tripped at cycle 159 after 18 cycles on the hosted cascade. Resuming keeps
# the SAME run id and every trip is written to operator-events.jsonl with the
# failing signature, so the record shows the trip rate instead of hiding it.
# MAX_TRIPS is the guard against a pathological loop: stop and ask for a human.
AUTO_RESUME="${FREE_BRAIN_AUTO_RESUME:-1}"
# Sized from the measured trip rate: trips landed at cycles 159 and 168, i.e.
# ~9 cycles per trip, so the remaining ~830 cycles needs ~95 trips. 200 leaves
# room for the rate to worsen while still stopping a genuinely pathological run.
MAX_TRIPS="${FREE_BRAIN_MAX_TRIPS:-200}"
# A DRIFT ALARM (coherence under the floor for DRIFT_STREAK cycles) is an
# OUTCOME of the study, not corruption: a noisy hosted model reaches it
# occasionally. For an unattended 1000-cycle run the operator's intent is to
# complete the target while recording every alarm, so it is auto-resumed too —
# as a `drift-alarm-reset` event, distinct from a breaker reset, and it never
# touches the coherence floor. MAX_ALARMS is the same kind of pathology guard.
MAX_ALARMS="${FREE_BRAIN_MAX_ALARMS:-200}"
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

trip_count() {
  # Trips are recorded by the loop itself in operator-events.jsonl (same run id),
  # so counting them needs no state of its own and survives a watchdog restart.
  if [ -f "$RESIDENCE/operator-events.jsonl" ]; then
    grep -c 'operator-reset' "$RESIDENCE/operator-events.jsonl" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

alarm_count() {
  # Drift alarms are recorded by the loop in operator-events.jsonl as their own
  # event type, so counting them needs no extra watchdog state.
  if [ -f "$RESIDENCE/operator-events.jsonl" ]; then
    grep -c 'drift-alarm-reset' "$RESIDENCE/operator-events.jsonl" 2>/dev/null || echo 0
  else
    echo 0
  fi
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
EXTRA_ARGS=""
[ "$AUTO_RESUME" = "1" ] && EXTRA_ARGS="--resume-tripped --max-trips $MAX_TRIPS --max-alarms $MAX_ALARMS"
log "watchdog starting — model=$MODEL cycles=$CYCLES stall=${STALL_SECONDS}s budget=${LOCAL_MODEL_STREAM_BUDGET_MS}ms skip_local=$SKIP_LOCAL auto_resume=$AUTO_RESUME (trips so far: $(trip_count)/$MAX_TRIPS, alarms: $(alarm_count)/$MAX_ALARMS)"

while true; do
  if [ "$SKIP_LOCAL" = "1" ]; then
    log "cascade-only mode: local provider skipped (no ollama, no local model loaded)"
  else
    ensure_ollama || { sleep 60; continue; }
  fi

  log "starting drift_loop (cycle $(python3 -c "
import json
try: print(json.load(open('$RESIDENCE/state.json'))['cycle'])
except Exception: print('?')
" 2>/dev/null || echo '?'))"
  started=$(date +%s)
  # With auto-resume on, --resume-tripped is passed on every start: it continues
  # the same run id past a trip (recording it) and has no effect on a healthy run.
  if [ "$AUTO_RESUME" = "1" ] && [ "$(trip_count)" -ge "$MAX_TRIPS" ]; then
    log "TERMINAL: $(trip_count) trips recorded, at the cap of $MAX_TRIPS — stopping for operator review."
    log "  inspect:  $RESIDENCE/operator-events.jsonl"
    break
  fi
  python3 -u drift_loop.py --cycles "$CYCLES" $EXTRA_ARGS >>"$LOG" 2>&1 &
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

  # rc=2 means either (a) the loop REFUSING to start a fresh run over an existing
  # record — a tripped breaker beyond --max-trips, or a different objective — or
  # (b) the auto-resume cap was reached. Restarting cannot fix either: it would
  # spin forever and bury the reason under restart noise. A mid-run trip that is
  # still under the cap no longer reaches here at all, because the loop now
  # auto-resumes it in-process (see --max-trips). Resuming keeps the original run
  # id; starting a new run appends a second run id to the same ledger and forks
  # the study, so refuse loudly and stop.
  if [ "$rc" -eq 2 ]; then
    log "TERMINAL: the loop declined to continue this record — see the lines above."
    log "  continue this run:  cd $APP_DIR && python3 drift_loop.py --cycles $CYCLES --resume-tripped"
    log "  start a new study:  cd $APP_DIR && python3 drift_loop.py --cycles $CYCLES --no-resume"
    log "watchdog stopping — no restart can resolve this."
    break
  fi

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
