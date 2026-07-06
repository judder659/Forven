# Forven

**Forven is a free, open-source, self-hosted autonomous crypto-trading research & operations workspace.** It pairs a team of AI agents with a backtesting engine, a robustness "gauntlet," paper trading, and an operator dashboard — so idea generation, validation, and monitoring all live in one repo you run on your own machine.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
&nbsp;[![Docs](https://img.shields.io/badge/docs-forven.app-0b7285.svg)](https://forven.app)

> ⚠️ **Paper + testnet by default. Not financial advice.**
> Forven ships in **paper-trading mode** with **Hyperliquid testnet** as the supported default. A live/mainnet execution engine exists in the code but is **unsupported, disabled by default, and reachable only via deliberate opt-in** — enabling real-money trading is entirely at your own risk. Trading crypto carries a substantial risk of total loss; backtest and paper results do not predict live performance, and the software's own metrics can be wrong. Use entirely at your own risk. See [`DISCLAIMER.md`](DISCLAIMER.md).

## What it does

- **Autonomous research agents** — a strategy-developer / quant-researcher / risk / execution agent team that generates hypotheses, builds strategies, and runs them through validation.
- **Backtesting engine** — bar-by-bar execution with vectorized signal generation.
- **The Gauntlet** — a robustness battery (walk-forward analysis, Monte-Carlo, parameter-jitter, cost-stress) that gates strategy promotion.
- **Paper trading** on Hyperliquid testnet, with real risk controls: stop-losses, a drawdown kill-switch, fill reconciliation, and liquidation-distance monitoring.
- **Operator dashboard** (SvelteKit) plus an in-process **MCP** server, so you can also drive it from Claude, Codex, and other MCP clients.
- **Bring your own keys** — your LLM provider key and your exchange (testnet) keys stay **local**; nothing is sent to any Forven server. There is no account and no sign-up.

## Stack

- **Backend:** Python 3.11+, FastAPI, uvicorn
- **Frontend:** SvelteKit 2, Svelte 5, Tailwind CSS, Vite
- **Database:** SQLite under `FORVEN_HOME` (defaults to `~/.forven`)
- **Memory:** ChromaDB
- **Backtesting:** built-in bar-by-bar engine with vectorized signal generation
- **Exchange layer:** CCXT adapters under `forven/exchange/`
- **Default local URLs:** frontend `http://127.0.0.1:5173` · backend `http://127.0.0.1:8003` · health `http://127.0.0.1:8003/api/health`

## Quick Start

Requirements: **Python 3.11+**, **Node.js**, and **git**.

```bash
git clone https://github.com/judder659/Forven.git
cd Forven
```

### Windows (recommended)

The PowerShell launcher creates `.venv`, installs backend + frontend dependencies, initializes the database, and starts everything:

```powershell
$env:START_BOT = '0'
$env:START_DAEMON = '0'
powershell -ExecutionPolicy Bypass -File .\start_all.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cd frontend && npm install && cd ..
cp .env.example .env
START_BOT=0 START_DAEMON=0 bash start_all.sh
```

Then open **http://127.0.0.1:5173**.

### Bring your own keys

Forven uses **your** LLM provider and **your** exchange — configured locally and never sent to any Forven server. Two ways to connect an LLM provider:

- **API key (recommended):** add it in the dashboard under **Settings → Agents**, or export the provider's env var before starting (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).
- **OAuth sign-in:** `python -m forven auth login openai` connects your ChatGPT/provider account via OAuth. `python -m forven auth status` shows what's configured.

Add your Hyperliquid **testnet** credentials in the dashboard under `/settings`. No Forven account is required, and credentials never leave your machine.

### ClawStreet adapter (optional, public-leaderboard paper trading)

Forven also ships a [ClawStreet](https://clawstreet.io) exchange adapter at `forven/exchange/clawstreet.py` so promoted strategies can publish their trades and reasoning to a public paper-trading leaderboard. Register a bot via `register_bot()` (or the ClawStreet web UI), claim it once, then set:

```bash
export CLAWSTREET_API_KEY="<your-bot-key>"
```

The adapter mirrors the Hyperliquid module shape (`market_order`, `limit_order`, `cancel_order`, `get_positions`, `get_account_value`, `get_open_orders`), so existing call sites can target it by import. Crypto only by default (31 pairs mapped); stock tickers pass through if you want US equities.

To run the live integration test against your bot:

```bash
export CLAWSTREET_TEST_API_KEY="<your-test-bot-key>"
pytest tests/test_clawstreet_exchange_integration.py
```

The integration suite skips cleanly when the env var is unset, so CI for outside contributors does not require a ClawStreet account.

## Documentation

- **Full docs:** **https://forven.app**
- In-repo: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/FIRST_RUN_CHECKLIST.md`](docs/FIRST_RUN_CHECKLIST.md) · [`docs/USER_MANUAL.md`](docs/USER_MANUAL.md)

## Community

- **GitHub:** https://github.com/judder659/Forven
- **Discord:** https://discord.gg/vzSQTneq6a
- **Reddit:** https://www.reddit.com/r/Forven/
- **X:** https://x.com/forvenapp

## Common commands

```bash
# Run the full stack
powershell -ExecutionPolicy Bypass -File .\start_all.ps1     # Windows
bash start_all.sh                                            # macOS / Linux

# Backend only
python -m uvicorn --app-dir . forven.api:app --host 127.0.0.1 --port 8003 --reload

# Frontend only
cd frontend && npm run dev

# CLI
python -m forven --help

# Tests / checks
python -m pytest tests -q
python -m ruff check forven tests
cd frontend && npm test && npm run check
```

## Configuration & safety

Start from `.env.example` and set only what you need. Useful values:

- `FORVEN_EXECUTION_MODE=paper` — the default and only **supported** mode. `live` exists but is unsupported; a real-money order additionally requires `FORVEN_ALLOW_MAINNET=1` and mainnet credentials, and is entirely at your own risk.
- `START_BOT=0` / `START_DAEMON=0` — leave the Discord bot / autonomous trading daemon off unless you want them.
- `FORVEN_BIND_HOST=127.0.0.1` — localhost only by default. Exposing the API (`0.0.0.0` / a LAN IP) requires setting `FORVEN_API_KEY`, or the app refuses to start.
- `FORVEN_API_KEY` / `FORVEN_OPERATOR_KEY` — set these if the API will be reachable beyond localhost.
- `FORVEN_ENABLE_SHELL_TOOL=0` — the agent's raw shell tool is off by default (prompt-injection risk); enable only at your own risk.
- `FORVEN_ENCRYPTION_KEY` — for portable encrypted credentials.
- `FORVEN_HOME` — to put runtime state outside the default `~/.forven`.

Never commit `.env`, `*.db`, auth tokens, or API keys (they're gitignored). The default configuration is paper + testnet and makes no real-money trades; enabling live/mainnet is unsupported and entirely at your own risk.

## Contributing

Forven is **not yet accepting external code contributions** — a contributor agreement isn't in place yet, so pull requests from outside contributors won't be merged for now. **Issues, ideas, and discussion are very welcome** (GitHub issues, [Discord](https://discord.gg/vzSQTneq6a), [r/Forven](https://www.reddit.com/r/Forven/)). See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

Found a security issue? Please report it privately — see [`SECURITY.md`](SECURITY.md). Don't open a public issue for vulnerabilities.

## License

Copyright (C) 2026 Judder <judder@forven.app>

Forven is free software, licensed under the **GNU Affero General Public License, version 3 or (at your option) any later version**. It is distributed WITHOUT ANY WARRANTY. Because it's AGPL, if you run a modified version as a network service you must make the corresponding source available to its users (AGPL §13). Full text in [`LICENSE`](LICENSE); see also [`NOTICE`](NOTICE) and [`TRADEMARK.md`](TRADEMARK.md).

## Disclaimer

Forven is experimental software provided **AS IS** for educational and research use, and is **not financial advice**. Paper trading + Hyperliquid testnet only; substantial risk of total loss. You are solely responsible for any orders placed and for safeguarding your own credentials. See [`DISCLAIMER.md`](DISCLAIMER.md) for the full terms.
