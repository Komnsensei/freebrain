#!/usr/bin/env bash
# migrate-residence.sh — move the residence (the agent's whole memory) to a new
# box without losing a cycle.  FREE-BRAIN.md: the files are the machine, so
# moving the files moves the run.
#
# Run this FROM THE PHONE (Termux) or from the old box:
#
#   bash deploy/oracle/migrate-residence.sh ubuntu@<vm-ip>
#   bash deploy/oracle/migrate-residence.sh ubuntu@<vm-ip> --dest /opt/builderbro
#   bash deploy/oracle/migrate-residence.sh --drive gdrive:freebrain
#
# What it protects: if the target already has a run that is FURTHER ALONG than
# the source, it refuses to overwrite it unless you pass --force. Losing 30
# cycles to a typo is exactly the kind of thing this project records honestly
# rather than hides.
set -euo pipefail

RESIDENCE_DIR="${DRIVE_RESIDENCE:-freebrain-residence}"
[[ "$RESIDENCE_DIR" = /* ]] || RESIDENCE_DIR="$(pwd)/$RESIDENCE_DIR"

TARGET=""
DEST=""
FORCE=0
DRIVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)  DEST="${2:?--dest needs a path}"; shift 2 ;;
    --drive) DRIVE="${2:?--drive needs an rclone remote}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) TARGET="$1"; shift ;;
  esac
done

log()  { printf '\033[1;36m[migrate]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[migrate] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -d "$RESIDENCE_DIR" ]] || die "no residence at $RESIDENCE_DIR (set DRIVE_RESIDENCE)"
[[ -f "$RESIDENCE_DIR/state.json" ]] || die "$RESIDENCE_DIR/state.json missing — is this a residence?"

# ── what we are about to move ───────────────────────────────────────────────
CYCLES=$(python3 -c "import json;print(json.load(open('$RESIDENCE_DIR/state.json')).get('cycle','?'))" 2>/dev/null || echo '?')
RUN_ID=$(python3 -c "import json;print(json.load(open('$RESIDENCE_DIR/state.json')).get('run','?'))" 2>/dev/null || echo '?')
LEDGER_LINES=$(wc -l < "$RESIDENCE_DIR/ledger.jsonl" 2>/dev/null || echo 0)
SIZE=$(du -sh "$RESIDENCE_DIR" 2>/dev/null | cut -f1)

log "source residence : $RESIDENCE_DIR"
log "run / cycle      : $RUN_ID / $CYCLES   (ledger: $LEDGER_LINES records, $SIZE)"

# A live run should be paused first, or the tar can catch a half-written cycle.
if pgrep -f 'drift_loop\.py' >/dev/null 2>&1; then
  log "NOTE: drift_loop.py is running here — stop it first for a clean snapshot"
  log "      (crash-resume makes this non-fatal; it just avoids a torn cycle)"
fi

# ── Drive target ────────────────────────────────────────────────────────────
if [[ -n "$DRIVE" ]]; then
  command -v rclone >/dev/null || die "rclone not installed here — pkg install rclone (Termux) / curl https://rclone.org/install.sh | bash"
  log "mirroring residence -> $DRIVE"
  rclone sync "$RESIDENCE_DIR" "$DRIVE" --checksum --create-empty-src-dirs --progress
  log "done. On the VM: rclone sync $DRIVE \$DRIVE_RESIDENCE"
  exit 0
fi

[[ -n "$TARGET" ]] || die "usage: $0 user@host [--dest /opt/builderbro] [--drive remote:path]"
DEST="${DEST:-/opt/builderbro}"
REMOTE_RES="${DEST%/}/$(basename "$RESIDENCE_DIR")"

# ── checkpoint safety: never silently rewind a run ─────────────────────────
REMOTE_CYCLES=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" \
  "python3 -c \"import json;print(json.load(open('$REMOTE_RES/state.json')).get('cycle',''))\" 2>/dev/null" \
  2>/dev/null || echo "")

if [[ -n "$REMOTE_CYCLES" && "$REMOTE_CYCLES" =~ ^[0-9]+$ && "$CYCLES" =~ ^[0-9]+$ ]]; then
  if (( REMOTE_CYCLES > CYCLES )) && [[ $FORCE -eq 0 ]]; then
    die "target is AHEAD: remote cycle $REMOTE_CYCLES > local cycle $CYCLES.
     Refusing to rewind the run. Pass --force to overwrite anyway."
  fi
  log "remote checkpoint: cycle $REMOTE_CYCLES (local: $CYCLES) — proceeding"
else
  log "no readable checkpoint on the target — treating this as a fresh box"
fi

# ── pack + ship ─────────────────────────────────────────────────────────────
log "shipping $SIZE to $TARGET:$REMOTE_RES"
ssh -o BatchMode=yes "$TARGET" "mkdir -p '$REMOTE_RES'"

tar czf - -C "$(dirname "$RESIDENCE_DIR")" "$(basename "$RESIDENCE_DIR")" \
  | ssh -o BatchMode=yes "$TARGET" "tar xzf - -C '$DEST'"

REMOTE_NOW=$(ssh -o BatchMode=yes "$TARGET" \
  "python3 -c \"import json;print(json.load(open('$REMOTE_RES/state.json')).get('cycle'))\"" 2>/dev/null || echo '?')

cat <<EOF

────────────────────────────────────────────────────────────────
  Residence migrated.

  target : $TARGET:$REMOTE_RES
  cycle  : $REMOTE_NOW (resume point)

  On the VM:
    grep DRIVE_RESIDENCE /etc/freebrain.env     # must point at $REMOTE_RES
    sudo systemctl start freebrain-loop
    journalctl -fu freebrain-loop
────────────────────────────────────────────────────────────────
EOF
