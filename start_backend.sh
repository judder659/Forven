#!/usr/bin/env bash
# Start Forven backend with .env vars
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Load .env
if [[ -f "$DIR/.env" ]]; then
	set -a
	source "$DIR/.env"
	set +a
fi

PORT="${FORVEN_PORT:-8004}"
HOST="${FORVEN_BIND_HOST:-0.0.0.0}"

exec .venv/bin/python3.11 -m uvicorn forven.api:app --host "$HOST" --port "$PORT"
