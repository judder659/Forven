#!/usr/bin/env bash

# Forven Hardened Setup
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "=== Forven Pre-flight Checks ==="

# 1. Check Python version
python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "Detected Python $python_version"
if [[ $(echo "$python_version < 3.10" | bc -l) -eq 1 ]]; then
    echo "ERROR: Python >= 3.10 required."
    exit 1
fi

# 2. Check for SQLite
if ! command -v sqlite3 &> /dev/null; then
    echo "WARNING: sqlite3 command not found. Database inspection will require Python tools."
fi

# 3. Install backend dependencies
echo "Installing backend dependencies..."
pip install -e .

# 4. Run database migrations
echo "Running database migrations..."
python3 -c "from forven.db import init_db; init_db()"

# 5. Install frontend dependencies
if [ -d "frontend" ]; then
    echo "Installing Forven frontend dependencies..."
    cd frontend
    if command -v pnpm &> /dev/null; then
        pnpm install --frozen-lockfile
    else
        echo "WARNING: pnpm not found. Skipping frontend dependency installation."
    fi
    cd ..
fi

echo "=== Setup complete. ==="
