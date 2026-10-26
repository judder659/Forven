#!/usr/bin/env bash
# Forven launcher for Linux: the counterpart of launcher.ps1 on Windows.
#
#   ./launcher.sh                  the launcher window (terminal menu without a desktop)
#   ./launcher.sh menu             the terminal menu
#   ./launcher.sh status           what is running
#   ./launcher.sh start            start Forven in the background
#   ./launcher.sh restart          stop everything, then start again
#   ./launcher.sh restart-backend  reload the backend on the current code
#   ./launcher.sh restart-frontend restart the dashboard dev server
#   ./launcher.sh stop             stop the services this checkout started
#   ./launcher.sh open             open the dashboard in the browser
#   ./launcher.sh logs             follow the supervisor log
#   ./launcher.sh install          add Forven to the applications menu
#   ./launcher.sh uninstall        remove it again
#
# Add -y to skip the confirmation before stop/restart. Needs nothing beyond
# what start_all.sh already uses (bash, curl, lsof).

if [ -z "${BASH_VERSION:-}" ]; then
	exec /usr/bin/env bash "$0" "$@"
fi

set -uo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 && pwd -P)"
cd "$DIR" || exit 1

if [[ -f "$DIR/.env" ]]; then
	set -a
	# shellcheck disable=SC1091
	source "$DIR/.env"
	set +a
fi

BACKEND_PORT="${FORVEN_PORT:-8003}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
TMP_DIR="$DIR/.tmp"
LOG_DIR="$TMP_DIR/logs"
SUPERVISOR_LOG="$LOG_DIR/start_all.log"
BACKEND_REQUEST="$TMP_DIR/restart.request"
FRONTEND_REQUEST="$TMP_DIR/restart-frontend.request"
BACKEND_HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/api/health"
FRONTEND_ROOT_URL="http://127.0.0.1:${FRONTEND_PORT}/"
DASHBOARD_URL="http://localhost:${FRONTEND_PORT}"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/forven.desktop"
ASSUME_YES=0

info() { echo "[launcher] $*"; }
warn() { echo "[launcher][warn] $*" >&2; }

# --- Probes -----------------------------------------------------------------

http_ok() { curl -fsS -m 3 "$1" >/dev/null 2>&1; }

proc_cwd() { readlink "/proc/$1/cwd" 2>/dev/null || true; }

# True when PID is "bash start_all.sh" for THIS checkout's script (not an
# editor or tail that merely mentions the file, and not a second checkout).
is_supervisor() {
	local pid="$1" script
	local -a argv=()
	mapfile -d '' -t argv <"/proc/$pid/cmdline" 2>/dev/null || return 1
	((${#argv[@]} >= 2)) || return 1
	case "${argv[0]##*/}" in
		bash | sh) ;;
		*) return 1 ;;
	esac
	script="${argv[1]}"
	[[ "$script" == /* ]] || script="$(proc_cwd "$pid")/$script"
	[[ "$(readlink -f "$script" 2>/dev/null)" == "$DIR/start_all.sh" ]]
}

# PIDs of start_all.sh supervisors running from this checkout. Its subshells
# share the command line; keep only the top-most process.
supervisor_pids() {
	local -a found=()
	local pid
	for pid in $(pgrep -f 'start_all\.sh' 2>/dev/null); do
		[[ "$pid" == "$$" ]] && continue
		is_supervisor "$pid" && found+=("$pid")
	done
	for pid in "${found[@]:-}"; do
		[[ -z "$pid" ]] && continue
		local ppid
		ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
		[[ " ${found[*]} " == *" $ppid "* ]] && continue
		echo "$pid"
	done
}

# PIDs listening on a port that belong to this checkout (working directory
# inside it). A foreign listener is reported, never stopped.
owned_listeners() {
	local port="$1" pid cwd
	for pid in $(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
		cwd="$(proc_cwd "$pid")"
		if [[ "$cwd" == "$DIR" || "$cwd" == "$DIR/"* ]]; then
			echo "$pid"
		fi
	done
}

foreign_listeners() {
	local port="$1" pid cwd
	for pid in $(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
		cwd="$(proc_cwd "$pid")"
		if [[ "$cwd" != "$DIR" && "$cwd" != "$DIR/"* ]]; then
			echo "$pid"
		fi
	done
}

wait_for() {
	local label="$1" seconds="$2"
	shift 2
	local i
	for ((i = 0; i < seconds; i++)); do
		if "$@"; then
			return 0
		fi
		sleep 1
	done
	warn "$label did not happen within ${seconds}s"
	return 1
}

backend_up() { http_ok "$BACKEND_HEALTH_URL"; }
backend_down() { ! http_ok "$BACKEND_HEALTH_URL"; }
frontend_up() { http_ok "$FRONTEND_ROOT_URL"; }
no_supervisor() { [[ -z "$(supervisor_pids)" ]]; }
request_taken() { [[ ! -f "$1" ]]; }

cmd_status() {
	local sup backend frontend
	sup="$(supervisor_pids | tr '\n' ' ')"
	backend="stopped"
	frontend="stopped"
	if backend_up; then
		backend="healthy"
	elif [[ -n "$(lsof -t -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null)" ]]; then
		backend="starting / not answering"
	fi
	if frontend_up; then
		frontend="healthy"
	elif [[ -n "$(lsof -t -nP -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null)" ]]; then
		frontend="starting / not answering"
	fi
	echo "Forven at $DIR"
	echo "  Supervisor: ${sup:-not running}"
	echo "  Backend   (:${BACKEND_PORT}): $backend"
	echo "  Dashboard (:${FRONTEND_PORT}): $frontend"
}

confirm() {
	[[ "$ASSUME_YES" == "1" ]] && return 0
	echo "$1 interrupts Forven workers and may briefly interrupt trade monitoring."
	echo "Open exchange positions are NOT closed. In-flight research recovers on startup."
	local answer
	read -r -p "Continue? [y/N] " answer
	[[ "$answer" =~ ^[Yy] ]]
}

# --- Actions ----------------------------------------------------------------

activate_venv() {
	[[ -n "${VIRTUAL_ENV:-}" ]] && return 0
	local candidate
	for candidate in "$DIR/.venv" "$DIR/venv"; do
		if [[ -f "$candidate/bin/activate" ]]; then
			# shellcheck disable=SC1091
			source "$candidate/bin/activate"
			return 0
		fi
	done
	warn "No .venv found in $DIR; using the system python3."
}

cmd_start() {
	if [[ -n "$(supervisor_pids)" ]]; then
		info "Forven is already running. Use restart, restart-backend or restart-frontend."
		return 0
	fi
	local port pids
	for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
		pids="$(foreign_listeners "$port" | tr '\n' ' ')"
		if [[ -n "$pids" ]]; then
			warn "Port $port is used by a process outside this Forven folder (PID ${pids% }). Nothing was started."
			return 1
		fi
	done
	mkdir -p "$LOG_DIR"
	rm -f "$BACKEND_REQUEST" "$FRONTEND_REQUEST"
	(
		activate_venv
		echo "===== $(date '+%Y-%m-%d %H:%M:%S') started by launcher =====" >>"$SUPERVISOR_LOG"
		# Own session: closing the launcher or its terminal leaves Forven running.
		setsid nohup "$DIR/start_all.sh" >>"$SUPERVISOR_LOG" 2>&1 </dev/null &
	)
	info "Starting Forven in the background (log: $SUPERVISOR_LOG)..."
	local i
	for ((i = 0; i < 300; i++)); do
		if backend_up && frontend_up; then
			info "Forven is ready: $DASHBOARD_URL"
			return 0
		fi
		if ((i > 3)) && no_supervisor; then
			warn "Startup stopped early. Last lines of the log:"
			tail -n 25 "$SUPERVISOR_LOG" >&2
			return 1
		fi
		sleep 1
	done
	warn "Forven is still starting after 5 minutes; see $SUPERVISOR_LOG"
	return 1
}

cmd_stop() {
	local pids pid port
	pids="$(supervisor_pids)"
	if [[ -n "$pids" ]]; then
		info "Stopping the supervisor (PID $(echo $pids))..."
		for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
		wait_for "Supervisor exit" 30 no_supervisor || true
	fi
	# The supervisor stops its children on exit; the backend then needs a few
	# seconds of graceful shutdown. Anything of ours still listening after the
	# grace period is stopped directly.
	local grace="${FORVEN_STOP_GRACE_SECONDS:-30}"
	for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
		pids="$(owned_listeners "$port")"
		[[ -z "$pids" ]] && continue
		for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
		local waited=0
		while ((waited < grace)) && [[ -n "$(owned_listeners "$port")" ]]; do
			sleep 1
			waited=$((waited + 1))
		done
		for pid in $(owned_listeners "$port"); do
			warn "Force-stopping PID $pid on port $port"
			kill -9 "$pid" 2>/dev/null || true
		done
	done
	for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
		pids="$(foreign_listeners "$port" | tr '\n' ' ')"
		[[ -n "$pids" ]] && warn "Port $port is held by a process outside this Forven folder (PID ${pids% }); left alone."
	done
	info "Forven is stopped."
}

cmd_restart() {
	cmd_stop
	cmd_start
}

# The supervisor polls for these request files every couple of seconds, so a
# reload works the same whether start_all.sh runs here or in a terminal window.
cmd_restart_backend() {
	if no_supervisor; then
		warn "Forven is not running, so there is no backend to reload. Use start."
		return 1
	fi
	mkdir -p "$TMP_DIR"
	echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) launcher restart-backend" >"$BACKEND_REQUEST"
	info "Asked the supervisor to reload the backend..."
	if ! wait_for "Supervisor picking up the request" 20 request_taken "$BACKEND_REQUEST"; then
		rm -f "$BACKEND_REQUEST"
		return 1
	fi
	wait_for "Backend health" 180 backend_up || return 1
	info "Backend reloaded and healthy."
}

cmd_restart_frontend() {
	if no_supervisor; then
		warn "Forven is not running. Use start."
		return 1
	fi
	mkdir -p "$TMP_DIR"
	echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) launcher restart-frontend" >"$FRONTEND_REQUEST"
	info "Asked the supervisor to restart the dashboard..."
	if ! wait_for "Supervisor picking up the request" 20 request_taken "$FRONTEND_REQUEST"; then
		rm -f "$FRONTEND_REQUEST"
		warn "This supervisor is older than the launcher; use restart once to load the new one."
		return 1
	fi
	wait_for "Dashboard" 120 frontend_up || return 1
	info "Dashboard restarted: $DASHBOARD_URL"
}

cmd_open() {
	if command -v xdg-open >/dev/null 2>&1; then
		xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 &
	else
		info "Open $DASHBOARD_URL in your browser."
	fi
}

cmd_logs() {
	info "Following the logs (Ctrl+C to stop following; Forven keeps running)."
	local -a files=()
	local f
	for f in "$SUPERVISOR_LOG" "$LOG_DIR/unified_backend.log"; do
		[[ -f "$f" ]] && files+=("$f")
	done
	if ((${#files[@]} == 0)); then
		info "No logs yet in $LOG_DIR"
		return 0
	fi
	# Ctrl+C ends tail only, not the launcher menu.
	trap 'true' INT
	tail -n 40 -F "${files[@]}"
	trap - INT
}

# The window needs PyGObject from the system Python (preinstalled on GNOME
# desktops such as Ubuntu). A project venv does not see it, so try the system
# interpreter first.
gui_python() {
	[[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] || return 1
	local py
	for py in ${FORVEN_GUI_PYTHON:-} /usr/bin/python3 python3; do
		command -v "$py" >/dev/null 2>&1 || continue
		if "$py" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
			echo "$py"
			return 0
		fi
	done
	return 1
}

cmd_gui() {
	local py
	if ! py="$(gui_python)"; then
		warn "No desktop window support (GTK 3 for Python) here; using the terminal menu."
		menu
		return
	fi
	exec "$py" "$DIR/scripts/launcher_gtk.py"
}

cmd_install() {
	mkdir -p "$(dirname "$DESKTOP_FILE")"
	local icon="$DIR/packaging/linux/forven.svg" exec_line terminal
	if gui_python >/dev/null; then
		exec_line="\"$DIR/launcher.sh\" gui"
		terminal=false
	else
		# No GTK: the entry opens the terminal menu instead.
		exec_line="\"$DIR/launcher.sh\" menu"
		terminal=true
	fi
	cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Forven
Comment=Start, stop and restart Forven
Exec=$exec_line
Path=$DIR
Icon=$icon
Terminal=$terminal
StartupWMClass=forven
Categories=Office;Finance;
EOF
	chmod +x "$DESKTOP_FILE" "$DIR/launcher.sh"
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1
	info "Added Forven to your applications menu ($DESKTOP_FILE)."
	[[ "$terminal" == "true" ]] && info "GTK for Python was not found, so it opens the terminal menu."
	info "Search for \"Forven\" in your apps; right-click it to add it to the dock or favorites."
}

cmd_uninstall() {
	rm -f "$DESKTOP_FILE"
	info "Removed $DESKTOP_FILE"
}

menu() {
	local choice
	while true; do
		echo
		echo "==== FORVEN LAUNCHER ===="
		cmd_status
		echo
		echo "  1) Open dashboard        5) Restart dashboard"
		echo "  2) Start Forven          6) Stop Forven"
		echo "  3) Restart Forven        7) Follow logs"
		echo "  4) Reload backend        8) Refresh status"
		echo "  q) Quit (Forven keeps running)"
		read -r -p "> " choice || return 0
		case "$choice" in
			1) cmd_open ;;
			2) cmd_start ;;
			3) confirm "Restarting Forven" && cmd_restart ;;
			4) confirm "Reloading the backend" && cmd_restart_backend ;;
			5) cmd_restart_frontend ;;
			6) confirm "Stopping Forven" && cmd_stop ;;
			7) cmd_logs ;;
			8 | "") ;;
			q | Q) return 0 ;;
			*) echo "Pick 1-8 or q." ;;
		esac
	done
}

usage() { sed -n '2,18p' "$DIR/launcher.sh" | sed 's/^# \{0,1\}//'; }

main() {
	if [[ "$(uname -s)" != "Linux" ]]; then
		warn "launcher.sh is for Linux. On Windows use ForvenLauncher.exe; elsewhere run start_all.sh."
		exit 1
	fi
	local -a args=()
	local a
	for a in "$@"; do
		case "$a" in
			-y | --yes) ASSUME_YES=1 ;;
			*) args+=("$a") ;;
		esac
	done
	local default="menu"
	gui_python >/dev/null && default="gui"
	case "${args[0]:-$default}" in
		menu) menu ;;
		gui) cmd_gui ;;
		status) cmd_status ;;
		start) cmd_start ;;
		stop) confirm "Stopping Forven" && cmd_stop ;;
		restart) confirm "Restarting Forven" && cmd_restart ;;
		restart-backend | reload) confirm "Reloading the backend" && cmd_restart_backend ;;
		restart-frontend) cmd_restart_frontend ;;
		open) cmd_open ;;
		logs) cmd_logs ;;
		install) cmd_install ;;
		uninstall) cmd_uninstall ;;
		help | -h | --help) usage ;;
		*)
			usage
			exit 2
			;;
	esac
}

main "$@"
