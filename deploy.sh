#!/bin/sh
# Deploys Timelog with Docker Compose.
#
#   ./deploy.sh            production: from committed code only, after the tests pass and a
#                          database backup; the new image is built before the old container
#                          stops, so the site is down only for a few seconds
#   ./deploy.sh staging    recreates the staging container (compose.staging.yml), e.g. after
#                          changing .env.staging; code changes reach staging without this
#
# Works with both `docker compose` and the older `docker-compose`.
set -eu

cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
    compose() { docker compose "$@"; }
else
    compose() { docker-compose "$@"; }
fi

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nDeploy stopped: %s\n' "$*" >&2; exit 1; }

# Waits until the app answers on the given port, or shows its log and fails.
wait_until_up() {
    port=$1
    shift
    i=0
    until curl -fs -o /dev/null "http://127.0.0.1:$port/login"; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            "$@" logs --tail=40
            fail "the app did not answer on port $port within 30 seconds"
        fi
        sleep 1
    done
}

# Recreates the container: compose v1 cannot always recreate it in place (KeyError: 'ContainerConfig').
recreate() {
    "$@" rm -sf timelog
    "$@" up -d
}

staging() { compose -p timelog-staging -f compose.staging.yml "$@"; }
production() { compose "$@"; }

case "${1:-production}" in
staging)
    [ -f .env.staging ] || fail ".env.staging is missing (see compose.staging.yml)"
    say "Building and recreating staging"
    staging build
    recreate staging
    wait_until_up 8001 staging
    say "Staging is up: $(grep -E '^TIMELOG_BASE_URL=' .env.staging | cut -d= -f2-)"
    exit 0
    ;;
production) ;;
*)
    echo "Usage: $0 [production|staging]" >&2
    exit 2
    ;;
esac

[ -f .env ] || fail ".env is missing (copy .env.example)"

# Production is built from what is committed, so the running code matches a commit.
if [ -n "$(git status --porcelain)" ]; then
    git status --short
    fail "commit or stash the changes above first"
fi
commit=$(git rev-parse --short HEAD)
git fetch --quiet origin 2>/dev/null || true
if upstream=$(git rev-parse --verify --quiet '@{upstream}'); then
    [ "$(git rev-parse HEAD)" = "$upstream" ] || echo "Note: $commit is not the same as $(git rev-parse --abbrev-ref '@{upstream}') (unpushed or behind)."
fi

if [ -x .venv/bin/pytest ]; then
    say "Running the tests"
    .venv/bin/pytest -q || fail "the tests failed"
else
    echo "Note: no .venv/bin/pytest, skipping the tests."
fi

say "Building the image for $commit"
production build

if [ -n "$(production ps -q timelog 2>/dev/null)" ]; then
    backup="/data/backup-before-deploy-$(date +%Y%m%d-%H%M%S)-$commit.db"
    say "Backing up the database to $backup (in the data volume)"
    production exec -T timelog python app.py backup "$backup"
    # Keep the 10 newest pre-deploy backups.
    production exec -T timelog sh -c 'ls -1t /data/backup-before-deploy-*.db 2>/dev/null | tail -n +11 | xargs -r rm --'
else
    echo "Note: production is not running, so there is no backup to take."
fi

say "Replacing the container"
recreate production
wait_until_up 8000 production

say "Production runs $commit: $(grep -E '^TIMELOG_BASE_URL=' .env | cut -d= -f2-)"
