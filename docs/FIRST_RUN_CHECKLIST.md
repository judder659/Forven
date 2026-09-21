# First-Run Checklist

Use this checklist when cloning Forven onto a new machine.

## 1. Install prerequisites

- [ ] Python 3.11+ is installed
- [ ] Node.js 20+ is installed
- [ ] Git is installed
- [ ] You have repository access and can clone successfully

## 2. Clone the repo

- [ ] `git clone https://github.com/judder659/Forven.git`
- [ ] `cd Forven`

## 3. Choose a bootstrap path

### Windows

- [ ] Set safe startup defaults in the current shell:

```powershell
$env:START_BOT = '0'
$env:START_DAEMON = '0'
```

- [ ] Start the stack:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_all.ps1
```

Notes:

- `start_all.ps1` creates `.venv`, installs missing packages, initializes the DB, and starts backend/frontend services.
- `start_all.ps1` reads environment variables from the current shell. It does not automatically load `.env`.

### macOS / Linux

- [ ] Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

- [ ] Install backend dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

- [ ] Install frontend dependencies:

```bash
cd frontend && pnpm install --frozen-lockfile && cd ..
```

- [ ] Copy the environment template if you want repo-local env settings:

```bash
cp .env.example .env
```

- [ ] Start the stack:

```bash
START_BOT=0 START_DAEMON=0 bash start_all.sh
```

Notes:

- `start_all.sh` loads `.env` automatically if it exists.

## 4. Confirm the stack is healthy

- [ ] Frontend opens at `http://127.0.0.1:5173`
- [ ] Backend health returns `ok` at `http://127.0.0.1:8003/api/health`
- [ ] The dashboard loads at `/`
- [ ] `/settings` loads without API errors

## 5. Configure auth and secrets

- [ ] Review `.env.example`
- [ ] Keep `FORVEN_EXECUTION_MODE=paper`
- [ ] Set `FORVEN_ENCRYPTION_KEY` if you want portable encrypted secrets
- [ ] Set `FORVEN_API_KEY` and `FORVEN_OPERATOR_KEY` if the API will be used beyond localhost
- [ ] Log in to AI providers as needed:

```powershell
python -m forven configure
python -m forven auth login openai
python -m forven auth login minimax
python -m forven auth status
```

- [ ] In the UI, review Settings for provider auth, exchange settings, API keys, notifications, and pipeline settings

## 6. Perform a basic smoke test

- [ ] Open `/data` and confirm datasets or ingestion status load
- [ ] Open `/lab` and `/ai-dropzone`
- [ ] Open `/runs` and confirm the page renders
- [ ] Open `/risk`, `/approval`, `/tasks`, `/ops`, and `/memory`
- [ ] Run one small backtest or lightweight research action in the UI

## 7. Run local quality checks

- [ ] Backend tests:

```powershell
python -m pytest tests -q
```

- [ ] Backend lint:

```powershell
python -m ruff check forven tests
```

- [ ] Frontend tests and checks:

```powershell
cd frontend
pnpm test
pnpm run check
```

## 8. Know where to look if startup fails

- [ ] Windows logs: `.tmp\logs\unified_backend.log` and `.tmp\logs\unified_frontend.log`
- [ ] Unix logs: `.tmp/logs/unified_backend.log` and `.tmp/logs/unified_frontend.log`
- [ ] Runtime state path: `~/.forven` unless `FORVEN_HOME` is set
- [ ] Remember that `python -m forven` opens the CLI; backend-only startup uses `python -m uvicorn --app-dir . forven.api:app`
