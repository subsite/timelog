# Release helpers for deploy.sh (sourced, not run). Versions are release dates,
# YYYY.MM.DD, with .2, .3… for further releases on the same day; tags are v<version>.

# The newest release tag, or nothing before the first release.
last_release_tag() {
    git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true
}

# The version for a release made today.
next_version() {
    base=$(date +%Y.%m.%d)
    version=$base
    n=1
    while git rev-parse -q --verify "refs/tags/v$version" >/dev/null; do
        n=$((n + 1))
        version="$base.$n"
    done
    echo "$version"
}

# The subjects of the commits since the last release, oldest first, without release commits.
changes_since() {
    range=${1:+$1..}HEAD
    git log --reverse --no-merges --format=%s "$range" | grep -v '^Release [0-9]' || true
}

# Adds a section for a release at the top of CHANGELOG.md (after its introduction).
# Usage: write_changelog <version> <date>, with the change list on standard input.
write_changelog() {
    section=$(mktemp)
    {
        printf '## %s (%s)\n\n' "$1" "$2"
        sed 's/^/- /'
        printf '\n'
    } >"$section"
    if [ ! -f CHANGELOG.md ]; then
        printf '# Changelog\n\n' >CHANGELOG.md
    fi
    # Insert before the first release heading, or at the end when there is none yet.
    awk -v section="$section" '
        !done && /^## / { while ((getline line < section) > 0) print line; done = 1 }
        { print }
        END { if (!done) { print ""; while ((getline line < section) > 0) print line } }
    ' CHANGELOG.md >CHANGELOG.md.new
    mv CHANGELOG.md.new CHANGELOG.md
    rm -f "$section"
}

# The notes of one release from CHANGELOG.md (its list of changes).
release_notes() {
    awk -v heading="## $1 " '
        index($0, heading) == 1 { found = 1; next }
        found && /^## / { exit }
        found && NF { print }
    ' CHANGELOG.md
}
