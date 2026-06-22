#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
	exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Load local env overrides if present.
if [[ -f "$DIR/.env" ]]; then
	set -a
	# shellcheck disable=SC1091
	source "$DIR/.env"
	set +a
fi

export PYTHONPATH="$DIR${PYTHONPATH:+:${PYTHONPATH}}"
UVICORN_APP_DIR="$DIR"

BACKEND_PORT="${FORVEN_PORT:-8003}"
BACKEND_HOST="${FORVEN_BIND_HOST:-${FORVEN_HOST:-127.0.0.1}}"
BACKEND_WORKERS="${BACKEND_WORKERS:-2}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
START_BOT="${START_BOT:-1}"
FORVEN_ENABLE_REGIME_LAB="${FORVEN_ENABLE_REGIME_LAB:-0}"
case "${FORVEN_ENABLE_REGIME_LAB,,}" in
	1|true|yes|on) FORVEN_ENABLE_REGIME_LAB="1" ;;
	*) FORVEN_ENABLE_REGIME_LAB="0" ;;
esac
export FORVEN_ENABLE_REGIME_LAB
export VITE_ENABLE_REGIME_LAB="${VITE_ENABLE_REGIME_LAB:-$FORVEN_ENABLE_REGIME_LAB}"
START_LAB_WORKER="${START_LAB_WORKER:-$FORVEN_ENABLE_REGIME_LAB}"
START_DAEMON="${START_DAEMON:-0}"
FORCE_RESTART="${FORCE_RESTART:-1}"
DB_PATH="${FORVEN_DB_PATH:-$DIR/data/forven.db}"
TMP_DIR="$DIR/.tmp"
LOG_DIR="$TMP_DIR/logs"

mkdir -p "$TMP_DIR" "$LOG_DIR"

BACKEND_LOG="$LOG_DIR/unified_backend.log"
FRONTEND_LOG="$LOG_DIR/unified_frontend.log"
BOT_LOG="$LOG_DIR/forven_bot.log"
LAB_WORKER_LOG="$LOG_DIR/forven_lab_worker.log"
DAEMON_LOG="$LOG_DIR/forven_daemon.log"

BACKEND_HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/api/health"
FRONTEND_HEALTH_URL="http://127.0.0.1:${FRONTEND_PORT}/api/health"
FRONTEND_ROOT_URL="http://127.0.0.1:${FRONTEND_PORT}/"
FORVEN_HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/api/health"

export FORVEN_CLIENT_BASE="${FORVEN_CLIENT_BASE:-http://127.0.0.1:${BACKEND_PORT}}"

PIDS=()
BOT_PID=""
LAB_WORKER_PID=""
DAEMON_PID=""

info() { echo "[start_all] $*"; }
warn() { echo "[start_all][warn] $*"; }
die() { echo "[start_all][error] $*" >&2; exit 1; }

cleanup() {
	local pid
	for pid in "${PIDS[@]:-}"; do
		if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
			kill "$pid" 2>/dev/null || true
		fi
	done
}
trap cleanup EXIT INT TERM

wait_for_http() {
	local url="${1:?wait_for_http requires a URL}"
	local label="${2:-HTTP check}"
	local attempts="${3:-60}"
	local i
	for ((i = 1; i <= attempts; i++)); do
		if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
			info "$label is healthy ($url)"
			return 0
		fi
		sleep 1
	done
	return 1
}

wait_for_http_any() {
	local label="${1:-Service}"
	local attempts="${2:-60}"
	shift
	shift || true
	local -a urls=("$@")
	if (( ${#urls[@]} == 0 )); then
		return 1
	fi
	local i
	local url
	for ((i = 1; i <= attempts; i++)); do
		for url in "${urls[@]}"; do
			if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
				info "$label is healthy ($url)"
				return 0
			fi
		done
		sleep 1
	done
	return 1
}

module_available() {
	python3 -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)' "$1"
}

kill_port_listener() {
	local port="$1"
	local pids
	pids="$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
	if [[ -z "$pids" ]]; then
		return 0
	fi
	local pid
	for pid in $pids; do
		warn "Stopping PID ${pid} listening on port ${port}"
		kill "$pid" 2>/dev/null || true
	done
	sleep 1
	pids="$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
	for pid in $pids; do
		warn "Force-stopping PID ${pid} on port ${port}"
		kill -9 "$pid" 2>/dev/null || true
	done
}

clear_stale_db_locks() {
	[[ -f "$DB_PATH" ]] || return 0
	local lock_pids cmd pid remaining
	lock_pids="$(lsof -t "$DB_PATH" 2>/dev/null | sort -u || true)"
	[[ -z "$lock_pids" ]] && return 0

	for pid in $lock_pids; do
		cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
		if [[ "$cmd" == *"uvicorn"* || "$cmd" == *"python"* ]]; then
			warn "Releasing stale DB lock holder PID ${pid}: ${cmd}"
			kill "$pid" 2>/dev/null || true
		else
			warn "DB lock held by non-backend process PID ${pid}: ${cmd}"
		fi
	done

	sleep 1
	remaining="$(lsof -t "$DB_PATH" 2>/dev/null | sort -u || true)"
	for pid in $remaining; do
		cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
		if [[ "$cmd" == *"uvicorn"* || "$cmd" == *"python"* ]]; then
			warn "Force-releasing DB lock holder PID ${pid}"
			kill -9 "$pid" 2>/dev/null || true
		fi
	done
}

kill_stale_bot() {
	local lock_file="${FORVEN_HOME:-$HOME/.forven}/bot.lock"
	[[ -f "$lock_file" ]] || return 0
	local pid
	pid="$(cat "$lock_file" 2>/dev/null | tr -d '[:space:]')"
	[[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 0
	if kill -0 "$pid" 2>/dev/null; then
		warn "Stopping stale bot process PID ${pid}"
		kill "$pid" 2>/dev/null || true
		sleep 1
		if kill -0 "$pid" 2>/dev/null; then
			warn "Force-stopping bot PID ${pid}"
			kill -9 "$pid" 2>/dev/null || true
		fi
	fi
	rm -f "$lock_file"
}

get_lab_worker_status_line() {
	python3 -c 'from forven.lab_worker_service import get_lab_worker_status; s = get_lab_worker_status(); w = s.get("worker") or {}; print(f"{1 if s.get(\"active\") else 0}|{w.get(\"pid\") or \"\"}")' 2>/dev/null || true
}

kill_stale_lab_worker() {
	local status_line active pid
	status_line="$(get_lab_worker_status_line)"
	active="${status_line%%|*}"
	pid="${status_line##*|}"
	if [[ "$active" != "1" && ! "$pid" =~ ^[0-9]+$ ]]; then
		return 0
	fi
	if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
		warn "Stopping stale Regime Lab worker PID ${pid}"
		kill "$pid" 2>/dev/null || true
		sleep 1
		if kill -0 "$pid" 2>/dev/null; then
			warn "Force-stopping Regime Lab worker PID ${pid}"
			kill -9 "$pid" 2>/dev/null || true
		fi
	fi
}

info "Cleaning up stale listeners and lock holders..."
kill_port_listener "$BACKEND_PORT"
kill_port_listener "$FRONTEND_PORT"
kill_stale_bot
if [[ "$FORCE_RESTART" == "1" ]]; then
	kill_stale_lab_worker
fi
clear_stale_db_locks

if [[ -n "$(lsof -t "$DB_PATH" 2>/dev/null || true)" ]]; then
	die "SQLite lock still held at ${DB_PATH}. Run: lsof ${DB_PATH}"
fi

info "Starting backend on port ${BACKEND_PORT}..."
BACKEND_MODULE="forven.api:app"
if ! module_available "forven.api"; then
	die "Required backend module not found: ${BACKEND_MODULE}"
fi

python3 -m uvicorn --app-dir "$UVICORN_APP_DIR" "$BACKEND_MODULE" --host "$BACKEND_HOST" --port "$BACKEND_PORT" --workers "$BACKEND_WORKERS" > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
PIDS+=("$BACKEND_PID")

if ! wait_for_http "$BACKEND_HEALTH_URL" "Backend"; then
	tail -n 80 "$BACKEND_LOG" || true
	die "Backend failed health check"
fi

info "Waiting 2s for backend warm-up..."
sleep 2

if [[ "$START_BOT" == "1" ]]; then
	info "Starting Forven Discord Bot..."
	BOT_MODULE="forven.bot"
	if ! module_available "$BOT_MODULE"; then
		die "Required bot module not found: ${BOT_MODULE}"
	fi
	python3 -c "from forven.bot import run_bot; run_bot()" > "$BOT_LOG" 2>&1 &
	BOT_PID=$!
	PIDS+=("$BOT_PID")
	sleep 1
	if ! kill -0 "$BOT_PID" 2>/dev/null; then
		tail -n 80 "$BOT_LOG" || true
		die "Discord bot exited during startup"
	fi
fi

if [[ "$START_LAB_WORKER" == "1" ]]; then
	status_line="$(get_lab_worker_status_line)"
	active="${status_line%%|*}"
	pid="${status_line##*|}"
	if [[ "$FORCE_RESTART" != "1" && "$active" == "1" && "$pid" =~ ^[0-9]+$ ]]; then
		info "Reusing healthy Regime Lab worker (PID ${pid})..."
	else
		if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
			warn "Stopping existing Regime Lab worker PID ${pid}"
			kill "$pid" 2>/dev/null || true
			sleep 1
		fi
		info "Starting Regime Lab worker..."
		python3 -m forven lab worker > "$LAB_WORKER_LOG" 2>&1 &
		LAB_WORKER_PID=$!
		PIDS+=("$LAB_WORKER_PID")
		sleep 2
		status_line="$(get_lab_worker_status_line)"
		active="${status_line%%|*}"
		if [[ "$active" != "1" ]]; then
			tail -n 80 "$LAB_WORKER_LOG" || true
			die "Regime Lab worker exited during startup"
		fi
	fi
fi

if [[ "$START_DAEMON" == "1" ]]; then
	info "Starting Forven daemon (data/risk loop)..."
	DAEMON_MODULE="forven"
	if ! module_available "$DAEMON_MODULE"; then
		die "Required daemon module not found: ${DAEMON_MODULE}"
	fi
	python3 -m "$DAEMON_MODULE" daemon start > "$DAEMON_LOG" 2>&1 &
	DAEMON_PID=$!
	PIDS+=("$DAEMON_PID")
	sleep 2
	if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
		tail -n 80 "$DAEMON_LOG" || true
		die "Daemon exited during startup"
	fi
fi

info "Starting frontend on port ${FRONTEND_PORT}..."
(
	cd "$DIR/frontend"
  # Bind Vite on the IPv6 unspecified address so both localhost (::1) and 127.0.0.1 work.
  npm run dev -- --host :: --port "$FRONTEND_PORT"
) > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!
PIDS+=("$FRONTEND_PID")

if ! wait_for_http "$FRONTEND_ROOT_URL" "Frontend page"; then
	tail -n 80 "$FRONTEND_LOG" || true
	die "Frontend failed health check"
fi

if ! wait_for_http "$FRONTEND_HEALTH_URL" "Frontend->Backend proxy"; then
	tail -n 80 "$FRONTEND_LOG" || true
	die "Frontend cannot reach backend via /api proxy"
fi

if ! wait_for_http "$FORVEN_HEALTH_URL" "Backend"; then
	tail -n 80 "$BACKEND_LOG" || true
	die "Backend is not healthy"
fi

info "Ready:"
info "  Frontend: http://127.0.0.1:${FRONTEND_PORT}"
info "  Backend:  http://127.0.0.1:${BACKEND_PORT}"
[[ "$START_LAB_WORKER" == "1" ]] && info "  Lab Worker: running (Regime Lab queue processor)"
[[ "$START_DAEMON" == "1" ]] && info "  Daemon:   running (data/risk loop)"
info "Press Ctrl+C to stop all started services."

while true; do
	# In-app self-update: the "Update & restart" action fast-forwards the
	# checkout and drops this sentinel. Bounce the backend so it reloads the
	# pulled code. (The frontend dev server hot-reloads source changes itself.)
	if [[ -f "$DIR/.tmp/restart.request" ]]; then
		info "Self-update restart requested - bouncing backend to load new code..."
		rm -f "$DIR/.tmp/restart.request" 2>/dev/null || true
		kill_port_listener "$BACKEND_PORT"
		python3 -m uvicorn --app-dir "$UVICORN_APP_DIR" "$BACKEND_MODULE" --host "$BACKEND_HOST" --port "$BACKEND_PORT" --workers "$BACKEND_WORKERS" > "$BACKEND_LOG" 2>&1 &
		BACKEND_PID=$!
		PIDS+=("$BACKEND_PID")
		if wait_for_http "$BACKEND_HEALTH_URL" "Backend"; then
			info "Backend restarted (self-update) as PID ${BACKEND_PID}"
		else
			warn "Backend did not pass health check after self-update restart; see ${BACKEND_LOG}"
		fi
		sleep 2
		continue
	fi
	if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
		die "Backend process exited unexpectedly. See ${BACKEND_LOG}"
	fi
	if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
		die "Frontend process exited unexpectedly. See ${FRONTEND_LOG}"
	fi
	if [[ "$START_BOT" == "1" ]] && [[ -n "$BOT_PID" ]] && ! kill -0 "$BOT_PID" 2>/dev/null; then
		die "Discord bot process exited unexpectedly. See ${BOT_LOG}"
	fi
	if [[ "$START_LAB_WORKER" == "1" ]] && [[ -n "$LAB_WORKER_PID" ]] && ! kill -0 "$LAB_WORKER_PID" 2>/dev/null; then
		die "Regime Lab worker process exited unexpectedly. See ${LAB_WORKER_LOG}"
	fi
	if [[ "$START_DAEMON" == "1" ]] && [[ -n "$DAEMON_PID" ]] && ! kill -0 "$DAEMON_PID" 2>/dev/null; then
		die "Daemon process exited unexpectedly. See ${DAEMON_LOG}"
	fi
	sleep 2
done
