# Forven User Manual

This manual reflects the current application surface in this repository.

## 1. What Forven Is

Forven is a local-first workspace for building, validating, supervising, and operating algorithmic trading strategies.

Core pieces:

- A FastAPI backend on `http://127.0.0.1:8003`
- A SvelteKit frontend on `http://127.0.0.1:5173`
- SQLite state stored under `FORVEN_HOME`
- ChromaDB-backed memory and workspace context
- CCXT-based exchange adapters under `forven/exchange/`

Forven keeps research, approvals, runtime monitoring, and operational controls in one place instead of splitting them across notebooks, bots, and dashboards.

## 2. Starting the App

### Full stack

```powershell
powershell -ExecutionPolicy Bypass -File .\start_all.ps1
```

```bash
bash start_all.sh
```

### Backend only

```powershell
python -m uvicorn --app-dir . forven.api:app --host 127.0.0.1 --port 8003 --reload
```

### Frontend only

```powershell
cd frontend
pnpm run dev
```

### CLI

```powershell
python -m forven --help
python -m forven configure
python -m forven auth status
```

## 3. Main Navigation

The left sidebar is the fastest map of the product.

| Route | Purpose | Typical use |
| --- | --- | --- |
| `/` | Dashboard | Overall health, activity, actions, winners, and live signals |
| `/?view=quant_factory` | Quant view | Alternate dashboard slice focused on strategy generation |
| `/?view=beta` | Spec view | Alternate shell/spec dashboard presentation |
| `/data` | Data workspace | Dataset coverage, ingestion runs, and fetch operations |
| `/lab` | Strategy lab | Browse and manage strategy research workflows |
| `/lab/strategy/[id]` | Strategy detail | Inspect one strategy's metrics, state, and lifecycle |
| `/runs` | Run history | Review backtests, jobs, optimizations, and prior outputs |
| `/ai-dropzone` | AI idea intake | Submit or refine prompt-driven strategy ideas |
| `/risk` | Risk oversight | Kill switch state, risk limits, and supervisory controls |
| `/trades` | Trade review | Open positions, recent trades, and execution summaries |
| `/agents` | Agent hub | Agent status, routing, tasks, and model policy |
| `/memory` | Memory workspace | Search and inspect Chroma/workspace memory items |
| `/tasks` | Task queue | Agent tasks, audits, and pipeline task visibility |
| `/approval` | Human gates | Review approvals and sensitive action handoffs |
| `/ops` | Runtime operations | Soak report, notifications, scheduler, logs, and repairs |
| `/settings` | Global configuration | Credentials, provider auth, API keys, pipeline settings, and resets |

## 4. Recommended First Session

1. Start the stack.
2. Open `/settings` and review execution mode, provider auth, and API keys.
3. Open `/data` to make sure datasets and ingestion endpoints are healthy.
4. Open `/lab` or `/ai-dropzone` to create or inspect strategy ideas.
5. Open `/runs` to review backtest and job history.
6. Open `/risk`, `/approval`, and `/ops` before moving a strategy beyond paper workflows.

## 5. Common Workflows

### Strategy research

Use `/ai-dropzone` or `/lab` to create or refine an idea, then drill into `/lab/strategy/[id]` once a strategy exists.

Typical sequence:

1. Create or import an idea.
2. Review parameters and metadata.
3. Run a backtest or robustness workflow.
4. Inspect results in the strategy detail page and `/runs`.

### Strategy lifecycle

The canonical lifecycle is:

- `researching`
- `backtesting`
- `paper`
- `deployed`
- `retired`

These map to the policy gates managed in `forven/policy.py`.

### Operational review

When something needs operator attention:

- `/approval` surfaces gated actions
- `/tasks` shows queued and historical work
- `/ops` shows system health, notifications, scheduler state, logs, and repair actions
- `/risk` shows safety controls and drawdown-related state

### Memory and context

Use `/memory` to inspect saved memories, annotations, and source health for the workspace memory layer.

## 6. Runtime and Safety Notes

- Keep new environments in `paper` mode.
- Do not enable `mainnet` casually.
- Review risk limits before enabling autonomous or exchange-connected behavior.
- If API keys are configured for browser access, pair `FORVEN_API_KEY` with `VITE_FORVEN_API_KEY`, and pair `FORVEN_OPERATOR_KEY` with `VITE_FORVEN_OPERATOR_KEY`.
- The live exchange code lives under `forven/exchange/` and should be treated as sensitive.

## 7. Useful Health Checks

- Backend health: `http://127.0.0.1:8003/api/health`
- Legacy compatibility health: `http://127.0.0.1:8003/health`
- Frontend root: `http://127.0.0.1:5173`
- WebSocket endpoints:
  - `/api/ws/live`
  - `/ws/live`

## 8. Troubleshooting

- Launcher logs live under `.tmp/logs/` on Unix-like systems and `.tmp\logs\` on Windows.
- `python -m forven` is the CLI entrypoint, not the backend server.
- If the frontend is up but API calls fail, verify `http://127.0.0.1:8003/api/health` first.
- If auth or workspace state seems missing, check the active `FORVEN_HOME` path.
