#!/usr/bin/env bash
# setup.sh — wire the Free Brain harness up to GitLab in one command.
#
# Does everything that can be automated, idempotently:
#   1. verifies the token
#   2. creates the project (or finds it if it already exists)
#   3. pushes this repository to it
#   4. sets GITLAB_PUSH_TOKEN as a masked CI variable (so runs persist the residence)
#   5. creates a pipeline schedule (free tier supports scheduled pipelines)
#   6. optionally triggers the first pipeline
#
# The ONE thing it cannot do is create the account — that needs an email
# verification. See deploy/gitlab/GITLAB.md.
#
# Usage:
#   GITLAB_TOKEN=glpat-xxxx bash deploy/gitlab/setup.sh
#   GITLAB_TOKEN=glpat-xxxx bash deploy/gitlab/setup.sh --project freebrain --no-trigger
#
# Token: a Personal Access Token with BOTH `api` and `write_repository` scopes.
# Prefer GITLAB_TOKEN over --token: a command-line argument is visible in `ps`.
#
# No jq dependency (this runs on a phone) — python3 parses the API responses.
set -uo pipefail

API="https://gitlab.com/api/v4"
TOKEN="${GITLAB_TOKEN:-}"
PROJECT="freebrain"
VISIBILITY="private"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEDULE_CRON="0 */6 * * *"
TRIGGER=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)      TOKEN="${2:?}"; shift 2 ;;
    --token-file) TOKEN="$(tr -d ' \n' < "${2:?}")"; shift 2 ;;
    --project)    PROJECT="${2:?}"; shift 2 ;;
    --public)     VISIBILITY="public"; shift ;;
    --source)     SOURCE="${2:?}"; shift 2 ;;
    --cron)       SCHEDULE_CRON="${2:?}"; shift 2 ;;
    --no-trigger) TRIGGER=0; shift ;;
    -h|--help)    sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m[gitlab]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[gitlab]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[gitlab] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$TOKEN" ]] || die "no token. Set GITLAB_TOKEN (or --token-file). Create one at:
     gitlab.com → User settings → Access tokens → scopes: api + write_repository"
[[ -d "$SOURCE/.git" ]] || die "$SOURCE is not a git repository (pass --source DIR)"

# ── tiny JSON helpers (no jq on a phone) ────────────────────────────────────
jget() { python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(''); raise SystemExit
cur=d
for k in sys.argv[1].split('.'):
    if isinstance(cur,list): cur=cur[0] if cur else {}
    cur=cur.get(k) if isinstance(cur,dict) else None
    if cur is None: break
print(cur if cur is not None else '')
" "$1"; }

api() { # api METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -m 60 -X "$method" "$API$path" \
      -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json" -d "$body"
  else
    curl -sS -m 60 -X "$method" "$API$path" -H "PRIVATE-TOKEN: $TOKEN"
  fi
}

# ── 1. who are we? ──────────────────────────────────────────────────────────
log "verifying the token…"
ME="$(api GET /user)"
USERNAME="$(printf '%s' "$ME" | jget username)"
if [[ -z "$USERNAME" ]]; then
  MSG="$(printf '%s' "$ME" | jget message)"
  case "$MSG" in
    *"confirm your email"*|*"confirmation"*)
      die "the account's email is not confirmed yet. GitLab blocks API access until
     you click the verification link it emailed you." ;;
  esac
  die "token rejected (${MSG:-401}). Check the token is valid and has the \`api\` scope."
fi
log "authenticated as ${USERNAME}"

# ── 2. the project ──────────────────────────────────────────────────────────
FULL="$USERNAME/$PROJECT"
PID="$(api GET "/projects/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$FULL")" | jget id)"
if [[ -n "$PID" ]]; then
  log "project already exists: $FULL (id $PID)"
else
  log "creating project $FULL ($VISIBILITY)…"
  CREATED="$(api POST /projects "$(python3 -c "
import json,sys
print(json.dumps({'name': sys.argv[1], 'path': sys.argv[1],
                  'visibility': sys.argv[2], 'initialize_with_readme': False,
                  'description': 'Open-weight self-rewriting agent: the 1000-cycle drift study (Q4). Files are the machine; compute is swappable.'}))
" "$PROJECT" "$VISIBILITY")")"
  PID="$(printf '%s' "$CREATED" | jget id)"
  [[ -n "$PID" ]] || die "could not create the project: $(printf '%s' "$CREATED" | jget message)"
  log "created $FULL (id $PID)"
fi
PROJECT_URL="https://gitlab.com/$FULL.git"

# ── 3. push the harness ─────────────────────────────────────────────────────
log "pushing $SOURCE → $FULL"
cd "$SOURCE" || die "cannot cd to $SOURCE"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
# Push with an inline credential so the token is never written to .git/config.
if git push -q "https://oauth2:${TOKEN}@gitlab.com/${FULL}.git" "$BRANCH" 2>/tmp/gitlab-push.err; then
  log "pushed branch $BRANCH"
else
  warn "push failed:"; sed 's/^/     /' /tmp/gitlab-push.err | tail -5
  die "…fix the push before continuing"
fi
# Token-free remote for convenience, so future pulls/pushes use normal git auth.
git remote remove gitlab >/dev/null 2>&1
git remote add gitlab "$PROJECT_URL"

DEFAULT_BRANCH="$(api GET "/projects/$PID" | jget default_branch)"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-$BRANCH}"

# ── 4. the CI variable that lets a run persist the residence ────────────────
log "setting the GITLAB_PUSH_TOKEN CI variable (masked)…"
EXISTING="$(api GET "/projects/$PID/variables/GITLAB_PUSH_TOKEN" | jget key)"
VAR_BODY="$(python3 -c "
import json,sys
print(json.dumps({'key':'GITLAB_PUSH_TOKEN','value':sys.argv[1],
                  'masked':True,'protected':False,'variable_type':'env_var'}))
" "$TOKEN")"
if [[ -n "$EXISTING" ]]; then
  api PUT "/projects/$PID/variables/GITLAB_PUSH_TOKEN" "$VAR_BODY" >/dev/null
  log "updated the existing variable"
else
  OUT="$(api POST "/projects/$PID/variables" "$VAR_BODY")"
  if [[ -n "$(printf '%s' "$OUT" | jget key)" ]]; then
    log "variable created"
  else
    warn "could not set the variable: $(printf '%s' "$OUT" | jget message)"
    warn "runs will still work — they just commit locally and keep artifacts instead"
  fi
fi

# ── 5. the pipeline schedule ───────────────────────────────────────────────
log "creating the pipeline schedule (${SCHEDULE_CRON})…"
SCHEDULES="$(api GET "/projects/$PID/pipeline_schedules")"
ALREADY="$(printf '%s' "$SCHEDULES" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: d=[]
print(next((s['id'] for s in d if 'freebrain' in (s.get('description') or '').lower()), ''))
")"
if [[ -n "$ALREADY" ]]; then
  log "schedule already exists (id $ALREADY)"
else
  SCHED="$(api POST "/projects/$PID/pipeline_schedules" "$(python3 -c "
import json,sys
print(json.dumps({'description':'freebrain drift run every 6h','ref':sys.argv[1],'cron':sys.argv[2],'cron_timezone':'UTC','active':True}))
" "$DEFAULT_BRANCH" "$SCHEDULE_CRON")")"
  if [[ -n "$(printf '%s' "$SCHED" | jget id)" ]]; then
    log "schedule created (id $(printf '%s' "$SCHED" | jget id))"
  else
    warn "could not create the schedule: $(printf '%s' "$SCHED" | jget message)"
    warn "create it by hand: CI/CD → Schedules"
  fi
fi

# ── 6. first run ───────────────────────────────────────────────────────────
if [[ "$TRIGGER" == "1" ]]; then
  log "triggering the first pipeline…"
  RUN="$(api POST "/projects/$PID/pipeline" "$(python3 -c "
import json,sys; print(json.dumps({'ref': sys.argv[1]}))
" "$DEFAULT_BRANCH")")"
  URL="$(printf '%s' "$RUN" | jget web_url)"
  if [[ -n "$URL" ]]; then log "pipeline started: $URL"
  else warn "could not trigger: $(printf '%s' "$RUN" | jget message)"; fi
fi

cat <<EOF

────────────────────────────────────────────────────────────────
  GitLab is wired up.

  project   : https://gitlab.com/$FULL
  branch    : $DEFAULT_BRANCH
  variable  : GITLAB_PUSH_TOKEN (masked) — lets runs persist the residence
  schedule  : $SCHEDULE_CRON UTC
  pipelines : https://gitlab.com/$FULL/-/pipelines

  Raise the job timeout if your plan allows (leave ~10 min of margin):
    Settings → CI/CD → General pipelines → Timeout
    then raise DRIFT_MAX_SECONDS in .gitlab-ci.yml to match.
────────────────────────────────────────────────────────────────
EOF
