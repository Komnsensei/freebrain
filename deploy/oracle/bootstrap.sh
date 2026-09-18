#!/usr/bin/env bash
# bootstrap.sh — one-shot setup for the Free Brain residence on an
# Oracle Cloud Always Free (Ampere A1, ARM64) VM.  FREE-BRAIN.md P0/P6.
#
# Idempotent: safe to re-run. Every step checks before it acts.
#
#   sudo bash deploy/oracle/bootstrap.sh
#
# Env overrides:
#   FREE_BRAIN_MODEL=qwen2.5-coder:7b   model to pull
#   FREE_BRAIN_DIR=/opt/builderbro      where the repo lives
#   FREE_BRAIN_USER=ubuntu              user that owns the repo + service
#   FREE_BRAIN_INSTALL_RCLONE=0         skip rclone (Drive mirror)
set -euo pipefail

MODEL="${FREE_BRAIN_MODEL:-qwen2.5-coder:7b}"
APP_DIR="${FREE_BRAIN_DIR:-/opt/builderbro}"
APP_USER="${FREE_BRAIN_USER:-ubuntu}"
INSTALL_RCLONE="${FREE_BRAIN_INSTALL_RCLONE:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;36m[freebrain]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[freebrain]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[freebrain] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo (installs packages + systemd units)"

ARCH="$(uname -m)"
log "host: $(uname -s) $ARCH  ($(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs 2>/dev/null || echo unknown))"
log "cores: $(nproc)  ram: $(free -h | awk '/^Mem:/{print $2}')"

case "$ARCH" in
  aarch64|arm64|x86_64|amd64) ;;
  *) warn "untested architecture '$ARCH' — continuing, Ollama may not publish a build" ;;
esac

# ── 1. base packages ────────────────────────────────────────────────────────
log "installing base packages (curl, python3, ca-certificates, rsync)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates python3 rsync tar >/dev/null

command -v python3 >/dev/null || die "python3 missing"
log "python3: $(python3 --version)"

# ── 2. swap (the 1 GB default is not enough headroom for a 7b load) ─────────
if ! swapon --show | grep -q .; then
  log "no swap found — adding a 4G swapfile (prevents OOM during model load)"
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  log "swap already present: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
fi

# ── 3. Ollama ───────────────────────────────────────────────────────────────
if command -v ollama >/dev/null; then
  log "ollama already installed: $(ollama --version 2>/dev/null | head -1)"
else
  log "installing Ollama (official install script)…"
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Keep the model resident: a continuous loop should never pay a reload cost.
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/freebrain.conf <<'EOF'
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF
systemctl daemon-reload
systemctl enable --now ollama
log "waiting for the Ollama API…"
for i in $(seq 1 60); do
  curl -fsS -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  [[ $i -eq 60 ]] && die "Ollama API never came up — check: journalctl -u ollama -n 50"
  sleep 1
done
log "Ollama API is up on 127.0.0.1:11434"

# ── 4. the model ────────────────────────────────────────────────────────────
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  log "model already pulled: $MODEL"
else
  log "pulling $MODEL (this is the slow step — a few minutes)…"
  ollama pull "$MODEL"
fi

# ── 5. the repo ─────────────────────────────────────────────────────────────
if [[ ! -d "$APP_DIR/.git" && ! -f "$APP_DIR/drift_loop.py" ]]; then
  die "$APP_DIR has no drift_loop.py — copy the repo there first:
     rsync -av --exclude node_modules --exclude .git ./ user@<vm-ip>:$APP_DIR/
   then re-run this script."
fi
log "repo found at $APP_DIR"

RESIDENCE="${DRIVE_RESIDENCE:-freebrain-residence}"
[[ "$RESIDENCE" = /* ]] || RESIDENCE="$APP_DIR/$RESIDENCE"
log "residence: $RESIDENCE"

# ── 6. rclone (Drive mirror) ───────────────────────────────────────────────
if [[ "$INSTALL_RCLONE" == "1" ]]; then
  if command -v rclone >/dev/null; then
    log "rclone present: $(rclone version | head -1)"
  else
    log "installing rclone…"
    curl -fsSL https://rclone.org/install.sh | bash >/dev/null 2>&1 || warn "rclone install failed — Drive mirror disabled"
  fi
fi

# ── 7. env file + systemd units ────────────────────────────────────────────
if [[ ! -f /etc/freebrain.env ]]; then
  log "writing /etc/freebrain.env (defaults; edit to taste)"
  cat > /etc/freebrain.env <<EOF
# Free Brain runtime env — read by freebrain-loop.service.
LOCAL_MODEL_URL=http://127.0.0.1:11434/v1
LOCAL_MODEL=$MODEL
DRIVE_RESIDENCE=$RESIDENCE
FREE_BRAIN_OBJECTIVE=Maintain a coherent self-rewriting cognitive loop that preserves its stated objective across 1000 continuous cycles without cognitive drift or infinite recursion.
# DRIVE_REMOTE=gdrive:freebrain
# EVIDENCE_FILE=$RESIDENCE/evidence/q1-evidence.jsonl
EOF
  chmod 644 /etc/freebrain.env
else
  log "/etc/freebrain.env exists — leaving it alone (edit by hand if needed)"
fi

for unit in freebrain-loop.service freebrain-sync.service freebrain-sync.timer; do
  [[ -f "$SCRIPT_DIR/$unit" ]] || die "missing unit file: $SCRIPT_DIR/$unit"
  sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
    "$SCRIPT_DIR/$unit" > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
log "installed units: freebrain-loop.service, freebrain-sync.service, freebrain-sync.timer"

# ── 8. sanity checks before handing over ───────────────────────────────────
log "verifying the harness against the live server…"
( cd "$APP_DIR" && DRIVE_RESIDENCE="$RESIDENCE" LOCAL_MODEL_URL=http://127.0.0.1:11434/v1 \
    LOCAL_MODEL="$MODEL" python3 agent_runtime.py --check ) || warn "agent_runtime.py --check returned non-zero"

if id "$APP_USER" >/dev/null 2>&1; then
  chown -R "$APP_USER" "$APP_DIR" 2>/dev/null || true
else
  warn "user '$APP_USER' does not exist — set FREE_BRAIN_USER and re-run, or the service will not start"
fi

cat <<EOF

────────────────────────────────────────────────────────────────
  Free Brain box is ready.

  model     : $MODEL  (resident, KEEP_ALIVE=-1)
  repo      : $APP_DIR
  residence : $RESIDENCE
  env       : /etc/freebrain.env
  ledger    : $RESIDENCE/ledger.jsonl
  checkpoint: $RESIDENCE/state.json

  Next — migrate the phone's residence (run from the PHONE), then:

    sudo systemctl start freebrain-loop     # the 1,000-cycle wave
    journalctl -fu freebrain-loop           # watch live
    tail -f $RESIDENCE/ledger.jsonl         # the evidence

  One cycle per trigger touch instead of continuous:
    cd $APP_DIR && LOCAL_MODEL_URL=http://127.0.0.1:11434/v1 \\
      python3 drift_loop.py --watch
    (or edit ExecStart in freebrain-loop.service to add --watch)

  Drive mirror (after \`rclone config\`):
    sudo systemctl enable --now freebrain-sync.timer
────────────────────────────────────────────────────────────────
EOF
