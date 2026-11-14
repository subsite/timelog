#!/bin/sh
# Deploys Timelog with Docker Compose.
#
#   ./deploy.sh            production: from committed code only, after the tests pass and a
#                          database backup; the new image is built before the old container
#                          stops, so the site is down only for a few seconds. Every deploy of
#                          new commits is a release (see release.sh): once the app is up, the
#                          CHANGELOG.md entry is committed, tagged v<version>, pushed, and
#                          published as a GitHub release (when gh is signed in)
#   ./deploy.sh staging    recreates the staging container (compose.staging.yml), e.g. after
#                          changing .env.staging; code changes reach staging without this
#
# Works with both `docker compose` and the older `docker-compose`.
set -eu

cd "$(dirname "$0")"
. ./release.sh

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
git fetch --quiet origin 2>/dev/null || echo "Note: could not fetch from origin."
if upstream=$(git rev-parse --verify --quiet '@{upstream}'); then
    git merge-base --is-ancestor "$upstream" HEAD || fail "origin has commits that are not here: pull first"
fi

if [ -x .venv/bin/pytest ]; then
    say "Running the tests"
    .venv/bin/pytest -q || fail "the tests failed"
else
    echo "Note: no .venv/bin/pytest, skipping the tests."
fi

last=$(last_release_tag)
if [ -n "$last" ] && [ "$(git rev-list -n 1 "$last")" = "$(git rev-parse HEAD)" ]; then
    version=${last#v}
    release=
    say "No new commits since $last: deploying $version again"
else
    version=$(next_version)
    release=yes
    say "Releasing $version with these changes:"
    changes_since "$last" | write_changelog "$version" "$(date +%Y-%m-%d)"
    release_notes "$version"
    # Undo the changelog entry if the deploy does not get through.
    trap 'git checkout -q -- CHANGELOG.md' EXIT
fi
export TIMELOG_VERSION="$version"

say "Building the image for $version"
production build

if [ -n "$(production ps -q timelog 2>/dev/null)" ]; then
    backup="/data/backup-before-deploy-$(date +%Y%m%d-%H%M%S)-$version.db"
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

if [ -n "$release" ]; then
    say "Recording release $version"
    git commit -q -m "Release $version" -- CHANGELOG.md
    git tag -a "v$version" -m "Timelog $version"
    trap - EXIT
    # The app is already deployed: from here on, problems are reported but do not stop.
    if git push -q --follow-tags origin HEAD; then
        if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
            release_notes "$version" | gh release create "v$version" --title "Timelog $version" --notes-file - >/dev/null \
                && echo "Published the GitHub release v$version." \
                || echo "Note: could not create the GitHub release; the tag is pushed."
        else
            echo "Note: gh is not installed or not signed in, so there is no GitHub release page."
        fi
    else
        echo "Note: pushing failed; run: git push --follow-tags origin HEAD"
    fi
fi

say "Production runs $version: $(grep -E '^TIMELOG_BASE_URL=' .env | cut -d= -f2-)"
