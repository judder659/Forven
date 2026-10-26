# Desktop launcher

## Windows

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

## Linux

`launcher.sh` in the Forven folder is the Linux launcher. Run it once with
`install` to add **Forven** to your applications menu, then open it from there
(right-click it to pin it to the dock):

```bash
./launcher.sh install
```

On a desktop with GTK for Python (preinstalled on Ubuntu and other GNOME
desktops) it opens a window like the Windows launcher: Start, Restart and Stop
Forven, Reload backend, Restart dashboard, Open Forven, live status and an
activity box. Without GTK it opens the same actions as a numbered terminal
menu (`./launcher.sh menu`). Nothing extra is installed either way; the
launcher only uses bash, curl and lsof, which `start_all.sh` already needs.

Every action is also a command: `status`, `start`, `restart`,
`restart-backend` (alias `reload`), `restart-frontend`, `stop`, `open`, `logs`,
`gui`, `menu`, `uninstall`. Add `-y` to skip the confirmation before stop and
restart. Run `install` again after updating from an older launcher.

- **Start** runs `start_all.sh` in the background in its own session, so closing
  the menu or its terminal leaves Forven running. Output goes to
  `.tmp/logs/start_all.log`. The folder's `.venv` is activated if present, and
  `.env` is read as usual.
- **Reload backend** asks the running supervisor to restart only the backend on
  the current code (the same request the in-app "Update & restart" makes). The
  dashboard stays up. This works whether `start_all.sh` was started by the
  launcher or by hand in a terminal window.
- **Restart dashboard** restarts only the frontend dev server. A supervisor
  started before this launcher existed does not know this request yet; restart
  Forven once to load the new one.
- **Restart / Stop Forven** stop the supervisor of this folder (also one running
  in a terminal window) and everything it started, then wait for the backend's
  graceful shutdown before forcing anything left on the Forven ports.

As on Windows, stop and restart do **not** close exchange positions. Only the
supervisor whose `start_all.sh` lives in this folder, and listeners whose working
folder is inside it, are touched; a port held by anything else is reported and
left alone.

Validation: `pytest tests/test_launcher_sh.py` drives the launcher against a
stand-in supervisor on free ports.
