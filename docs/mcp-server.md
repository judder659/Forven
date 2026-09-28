# Forven MCP Server

Expose the AI Drop Zone to Claude Desktop (or any MCP client) as a set of
tools, so strategy generation becomes a conversation instead of a
copy-paste loop.

## What it is

An MCP server that wraps the running Forven HTTP API. It speaks the
Model Context Protocol over stdio — Claude Desktop spawns the process,
reads the tool list, and calls tools on your behalf as you chat.

## Prerequisites

- Forven backend running (`forven serve` or `python -m uvicorn forven.api:app --port 8003`)
- Python environment where `forven` is importable (the same venv you run the backend from is fine)
- Claude Desktop installed, or any other MCP client

## Run locally

```bash
python -m forven.mcp_server
```

The process reads MCP frames from stdin and writes to stdout, so running
it in a terminal mostly looks like it's hanging. That's expected — it's
waiting for a client.

### Configuration

All via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `FORVEN_API_URL` | `http://127.0.0.1:8003` | Backend base URL |
| `FORVEN_API_KEY` | *(empty)* | Sent as `x-api-key` when set |
| `FORVEN_OPERATOR_KEY` | *(empty)* | Sent as `x-operator-key` when set |
| `FORVEN_MCP_TIMEOUT` | `60` | HTTP timeout in seconds |

If `FORVEN_AUTH_REQUIRED=true` on the backend, you MUST set
`FORVEN_API_KEY` and `FORVEN_OPERATOR_KEY` or every tool call will 401.

## Wire to Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "forven": {
      "command": "python",
      "args": ["-m", "forven.mcp_server"],
      "env": {
        "FORVEN_API_URL": "http://127.0.0.1:8003",
        "FORVEN_API_KEY": "your-api-key",
        "FORVEN_OPERATOR_KEY": "your-operator-key"
      }
    }
  }
}
```

If your `python` isn't on PATH inside Claude Desktop's spawn env, use the
absolute path to the interpreter (e.g. `C:\Users\you\venvs\forven\Scripts\python.exe`).

Restart Claude Desktop. You should see a hammer/tool icon in the chat UI
— click it to confirm the `forven_*` tools are loaded.

## Sessions are automatic

You no longer need to manage sessions by hand. A Drop Zone session
auto-opens on the first register/backtest call, every later call tags to
it, and it closes when the MCP client disconnects. Server-side, sessions
idle beyond the TTL (default 6h, `FORVEN_DROPZONE_IDLE_TTL_HOURS`) are
auto-closed as a backstop, so abandoned sessions never pile up as
"active". `forven_create_session` still exists for setting a meaningful
label/objective up front, and passing an explicit `session_id` to any
tool resumes that session.

## Tool reference

### Read-only

| Tool | Purpose |
|---|---|
| `forven_get_context` | Sectioned Drop Zone context. `section='overview'` (default) first, then `template` / `datasets` / `params` / `gotchas` / `endpoints` / `all`. |
| `forven_list_sessions` | Recent sessions with strategy counts. |
| `forven_get_session` | Strategies + runs tagged to a session (defaults to the active one). |
| `forven_list_strategies` | Registered strategies. |
| `forven_get_recent_runs` | Last N backtest runs. |
| `forven_get_result` | Full metrics + trades + config for one result. |
| `forven_get_robustness_result` | Poll a submitted robustness run: status + verdict scorecard. |
| `forven_get_gate_report` | THE status readout: stage, latest metrics, promotion_ready, failed_gates, next_actions. |
| `forven_get_paper_readiness` | Gate report framed against the paper target. |
| `forven_get_quant_skills` | Curated insights by regime. Check before designing. |
| `forven_get_data_readiness` | The data contract: `strategy_id`, or `symbol` + `timeframe` (+ `streams` such as `['funding','oi']`, `history_days`, `strategy_type`) → verdict ready / needs_data / blocked, a one-sentence summary, and each requirement (candles, history depth, freshness, every feed) with a fix. Check it before writing a strategy that reads anything beyond candles. |

### Write

| Tool | Purpose |
|---|---|
| `forven_create_session` | Optional — open a labeled session (returns id like `ADZ-0007`). |
| `forven_close_session` | Close a session early (idempotent; happens automatically on disconnect). |
| `forven_register_strategy_file` | Register one .py file. Auto-tags the active session. |
| `forven_run_backtest` | Run a backtest (compact gate-relevant metrics by default). Auto-tags. |
| `forven_create_strategy` | Create a container using a built-in execution family. |
| `forven_run_optimization` | Parameter search — persisted optimization evidence is a paper-gate requirement. |
| `forven_run_robustness` | Submit the PERSISTED validation suite (walk_forward, cost_stress, param_jitter) — the artifacts the paper gate reads. |
| `forven_promote_strategy` | Non-forced lifecycle promotion with structured failed_gates. |
| `forven_start_paper_session` | Final hop: gauntlet → paper through the real gate. |
| `forven_run_gauntlet_candidate` | Orchestrated backtest + robustness submission + gauntlet promotion attempt. |

## The loop an agent should run

1. `forven_get_context` (overview) → then `section='template'` and `section='gotchas'` before writing code
2. `forven_get_quant_skills` — priors, to skip known dead ends
   - Reading funding / OI / basis / IV / long-short / taker / liquidations? `forven_get_data_readiness` for the market first. `blocked` means no download can supply it (liquidation history starts at its forward-only capture) — choose another input rather than build a strategy that can never trade.
3. Write the strategy `.py` into the workspace
4. `forven_register_strategy_file` → `strategy_id`
5. `forven_run_backtest` — iterate until OOS metrics are genuinely good
6. `forven_run_optimization` → bake winners into the file's `default_params`
7. `forven_run_robustness` → poll `forven_get_robustness_result` per test
8. `forven_get_gate_report` → all green? `forven_promote_strategy` (`force=false`, always)

The robustness step is what used to be impossible via MCP: those persisted
artifacts are the evidence the paper gate reads, so a strategy no longer
has to wait for the background gauntlet loop to complete the paper hop.

## Troubleshooting

**"Tool call timed out"** — increase `FORVEN_MCP_TIMEOUT`. Full backtests
can take minutes on cold caches.

**"401 Invalid or missing operator key"** — the backend has auth enabled
but the MCP env is missing `FORVEN_OPERATOR_KEY`. Set it in the
Claude Desktop config `env` block.

**"Connection refused"** — backend isn't running or is on a different
port than `FORVEN_API_URL`.

**Tools don't appear in Claude Desktop** — check the Desktop logs
(`%APPDATA%\Claude\logs\mcp-server-forven.log` on Windows). Common
causes: `python` not on PATH, `forven` not importable in the spawned
env, or the config JSON has a syntax error.
