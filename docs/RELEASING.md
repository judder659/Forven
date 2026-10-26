# Releasing

Installs on the Stable update channel (the default) only see **releases**: annotated
`vX.Y.Z` tags on main. The `Release` workflow (`.github/workflows/release.yml`, script
`scripts/release.py`) cuts them; nobody tags by hand.

## Weekly release (automatic)

Every Monday at 09:00 UTC, after the nightly Windows full suite, the workflow:

1. Picks the newest commit on main that passed both the normal CI and the nightly
   Windows full suite. If nothing new has passed since the last release, it skips the week.
2. Bumps the minor version (0.2.0 → 0.3.0).
3. Builds the notes from the titles of the PRs merged since the last release, sorted into
   New, Fixes and Safety (Safety: titles about live trading, risk, orders, probation or
   security; Fixes: titles starting with Fix, Stop, Keep, Restore and similar).
4. Pushes the tag (its message holds the notes the app shows), publishes a GitHub
   Release, and posts one message to Discord.

PR titles become the release notes, so write them as plain sentences a user would follow.

## Urgent hotfix

Actions → Release → Run workflow → kind `hotfix`, tick `urgent`. It tags the newest main
commit with green CI as a patch version (0.3.0 → 0.3.1). The app shows an amber pill and a
one-line banner, and Remind me tomorrow doesn't hide it.

Tick `dry_run` to see the version and notes without tagging anything.

## Discord

Add the changelog channel's webhook URL as the repository secret
`DISCORD_RELEASE_WEBHOOK` (Settings → Secrets and variables → Actions). Without it the
release still goes out; only the post is skipped.

## What an update does on each install

After the in-app update fast-forwards the checkout, the launcher (start_all or the
Windows watchdog) restarts the backend. Before it does:

- `python -m forven.preflight --install` installs any Python packages the update added or
  whose required versions changed.
- `python -m forven.frontend_deps check` compares `frontend/package-lock.json` with the
  lockfile the dashboard's packages were installed from. If they differ, the launcher stops
  Vite, runs `npm install`, and starts it again.

Source downloads without git history (Download ZIP) get their version from
`forven/_release_version.txt`, which git fills in when it builds the archive.
