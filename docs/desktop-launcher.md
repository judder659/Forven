# Desktop launcher (Windows)

Double-click **ForvenLauncher.exe** in the Forven folder. It opens a native,
dark desktop window without a terminal. No new dependencies are required.
The executable is a small host for the adjacent launcher script; keep it in the
Forven directory. Build it from source using Windows PowerShell and
`scripts/build-launcher.ps1`. **Forven Launcher.vbs** is a script-only fallback.
The launcher is independent of the API. Opening or closing it does not change
running services. Create a Windows shortcut to this file for desktop access.

- **Start Forven** starts the configured services through the existing bootstrap.
- **Stop Forven** records persistent stop intent and stops this installation's
  backend, frontend, separate daemon, bot, lab workers, and supervisor.
- **Restart Forven** stops those processes and requests startup again.
- Backend and frontend rows allow individual control. Backend control does not
  stop a separate daemon; use Stop Forven to stop all managed services.
- **Open Forven** opens the normal application. Status refreshes automatically;
  a successful control request is not itself proof of healthy startup.
- Logs remain under `.tmp/logs`; the launcher includes recent supervisor activity.

Stop and restart interrupt in-flight work and can interrupt trade monitoring.
They do **not** close exchange positions. The launcher explains this before
backend/full-stack stop or restart. These are process stops, not a graceful
drain of every task; startup recovery handles interrupted work where supported.

Windows may ask for administrator approval when operating services started by
the elevated scheduled watchdog. Cancelling approval does not run the action.
Read-only status and opening the launcher need no elevation.

Both supervisors honor `.tmp/launcher.*.stopped` intent markers. Start Forven
clears them. Legacy bootstrap also respects a global stop marker; use the
launcher to start again. Do not delete or edit markers while an action is running.
The existing scheduled watchdog remains installed. A ten-minute startup grace
period allows pre-migration database backups to complete before retrying startup.

Process controls verify the installation path and process creation time. A
foreign or unverifiable process occupying a service port blocks the action.
The launcher does not kill arbitrary Python processes or adjacent checkouts.

Validation: `powershell -NoProfile -File tests/test_launcher_services.ps1` tests
process isolation and desired-state rules without stopping live services.
`powershell -NoProfile -STA -File launcher.ps1 -SmokeTest` constructs the window
without starting services.
