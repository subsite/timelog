# Changelog

Timelog is versioned by release date (`YYYY.MM.DD`, with `.2`, `.3`… for further releases on the same day). Every production deploy with `./deploy.sh` is a release: it adds a section here listing the commits since the previous release, and tags it `v<version>`.

## 2026.11.14 (2026-11-14)

- Narrower page column and a summary card beside the Timer page on wide screens
- Running timer in the top bar on other pages; STAGING label hidden on phones
- Date-based versions: releases, changelog and version history in Settings

## 2026.11.05 (2026-11-05)

- Timer API with API keys, Settings sections menu, staging environment
- Mark non-production copies with TIMELOG_ENVIRONMENT (red label in staging)
- Live timer updates over server-sent events
- Deploy script: tests, database backup and rebuild for production; recreate staging
- Describe the timer stream in Settings → API

## 2026.11.02 (2026-11-02)

- Optional open sign-up: anyone with GitHub gets their own account

## 2026.11.01 (2026-11-01)

- first commit
- Timelog: single-user time tracker
- Add per-project billing rounding and a currency setting
- Report periods, four grouping levels, per-row rounding, calendar pickers
- Multiple users with separate data, signing in with GitHub
- License under GPL-3.0-or-later; About section in Settings
- Download your own data as a SQLite database
- Import time entries from Clockify
- Stopwatch logo, Show archived in reports, clearer delete dialog

