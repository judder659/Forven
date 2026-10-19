# Forven Agent Harness (`forven.agent`)

Drive the running Forven backend from **any AI harness** — Claude Code, Codex, a
Tauri-embedded agent, a sidecar, CI, or a plain script — over Forven's HTTP API.
No MCP required.

## Why this exists

The Forven MCP server (`forven.mcp_server`) is a thin **stdio wrapper** over the
backend REST API at `http://127.0.0.1:8003`. The Svelte/Tauri frontend uses that
same API. MCP only works in MCP-capable hosts (Claude Desktop/Code); it can't run
inside the Tauri app or be reached by Codex. This harness targets the underlying
HTTP API directly, so **anything that can make an HTTP request can use Forven** —
with **zero third-party dependencies** (Python stdlib only).

```
        ┌───────────────────────────────────────────────┐
        │ Backend: forven.api:app  (uvicorn :8003)        │
        │ REST /api/...  +  scheduler / gauntlet / DB      │
        └───────────────┬─────────────────────────────────┘
                        │  HTTP (:8003/api)
   Tauri/Svelte UI ─────┼───── MCP server (stdio) ───── forven.agent (this)
   fetch → :8003/api    │      → :8003/api               urllib → :8003/api
                        └── Claude Code / Codex / sidecar ─┘
```

## Prerequisites

- Backend running on `:8003` (`start_all.ps1` supervises it). Verify: `python -m forven.agent health`.
- Optional auth (only if `:8003` is exposed beyond localhost): set `FORVEN_API_KEY` /
  `FORVEN_OPERATOR_KEY` (env) or pass `--api-key/--operator-key`. Local calls need none.
- Override the origin with `FORVEN_API_URL` or `--base-url`.

## CLI (best for shell-based agents)

Every command prints one JSON document to stdout; errors go to stderr with a
non-zero exit code.

```bash
python -m forven.agent health
python -m forven.agent context --out .tmp/ctx.json     # context is large; save it
python -m forven.agent skills --regime range_bound
python -m forven.agent list --status paper
python -m forven.agent strategy S02545                  # full container
python -m forven.agent gate-report S02545               # why it is/isn't promotable to its next stage
python -m forven.agent readiness --symbol BTC/USDT --timeframe 1h --streams funding,oi  # is the data there?
python -m forven.agent readiness --strategy S02545      # the data contract of a registered strategy
python -m forven.agent status S02545,S02604             # {stage,status} for polling
python -m forven.agent runs --limit 10
python -m forven.agent result <result_id>
python -m forven.agent data-census --stream ohlcv       # freshness SLA census (late/breach series first)
python -m forven.agent paper-pool                       # paper occupancy, queue, door in force
python -m forven.agent readmit-candidates --days 30     # archives the research door would admit (report)
python -m forven.agent live-recheck                     # live strategies vs the new live gate (report)

# write / lifecycle
python -m forven.agent create-session --label hunt --objective "find paper strats"
python -m forven.agent register --file /abs/path/strat.py --session ADZ-0001
python -m forven.agent backtest --strategy S02545 --dataset BTC/USDT-1h --compact
python -m forven.agent backtest --strategy S02545 --dataset ADA/USDT-1h \
    --trade-mode short_only --params '{"base_horizon":48}' --compact
python -m forven.agent optimize --strategy S02545 --dataset BTC/USDT-1h --n-trials 30
python -m forven.agent verdict  --strategy S02545 --dataset BTC/USDT-1h --tests walk_forward,cost_stress
python -m forven.agent promote  --strategy S02550 --to gauntlet --from quick_screen

# one-shot pipeline: register -> 365d backtest -> quick-screen -> promote to gauntlet (force=false)
python -m forven.agent enqueue  --file /abs/path/strat.py --dataset BTC/USDT-1h
# then poll until paper or terminal
python -m forven.agent wait-paper --strategies S02545,S02604 --timeout 1800 --interval 90
# does the edge travel? the frozen rule on 15 coins over sealed research data
python -m forven.agent breadth S02545            # waits; --no-wait returns at once, --refresh reruns
```

## Library (best for sidecars / embedding)

```python
from forven.agent import ForvenAgentClient

fc = ForvenAgentClient()                       # http://127.0.0.1:8003 by default
assert fc.health()["status"] == "ok"

# explore the regime + past survivors before designing
ctx = fc.get_context()                         # datasets, template, param families
skills = fc.get_quant_skills(regime="range_bound")

# write a strategy .py to forven/strategies/custom/, then:
verdict = fc.enqueue_candidate("/abs/path/strat.py", "BTC/USDT-1h")
if verdict["enqueued"]:
    final = fc.wait_for_paper([verdict["strategy_id"]], timeout=2400)
```

## The strategy-discovery loop (what the gates actually want)

0. **Check the data** before designing around anything beyond candles —
   `readiness --symbol … --timeframe … --streams …` (see *Data readiness* below).
1. **Design** a `.py` strategy (extend `forven.strategies.base.BaseStrategy`;
   vectorized `generate_signals` returning a 4-tuple of bool Series; stateless;
   closed-bar only — never `.shift(-1)`; **no `stop_loss_pct` in `default_params`**;
   set `compatible_regimes = ["trending","volatile","range_bound"]` — omitting a
   real regime makes the engine force-exit and shred the strategy); declare a
   small `parameter_space()` (2-3 params x 2-4 values, `{"p": {"values": [...]}}`).
   With none, the optimizer tries x0.8/x1.0/x1.2 of the first three numeric
   `default_params`; the DSR is charged every combination tried.
2. **`enqueue`** — registers, runs a 365-day backtest, pre-screens, and (if it
   passes) promotes to the gauntlet. Never forces a gate.
3. The background **Gauntlet Advancer** drives genuine passers through 12 steps
   to PAPER. Poll with `wait-paper` / `status`.

**Gate reality (so you design things that can pass):**
- `quick_screen` (default fair-sample rule) judges the **whole** window: 20+
  trades, Sharpe above 0, PF ≥ 1.0 (aim ≥ 1.3), MaxDD < 30%, and an OOS slice
  that does not lose badly (Sharpe above −0.5). A window short of trades is
  widened (365 → 730 → 1,460 days, data permitting) before it is judged, so a
  slow strategy is not archived for being slow. The IS/OOS gap and ratio are
  recorded, not enforced; Sharpe ≥ 5 or PF ≥ 8 is still rejected as a likely
  leak. (`ForvenAgentClient.quick_screen(compact)` pre-checks this offline;
  Settings → Quick-screen rule → Strict restores the old split-slice screen.)
- `cost_stress` (2× fees) needs **big per-trade edges** → favors *selective*
  setups or high win-rate reversion. Kills high-volume, low-edge momentum.
- `paper_promotion_gate` is the **door into paper**. Paper risks no capital, so
  the default *research door* admits a strategy that is not broken or clearly
  losing: 20+ confirmation trades, non-negative return, OOS profit factor ≥ 1
  and OOS Sharpe > 0, drawdown and tail-risk floors, and no loss under 2× costs.
  Every robustness test must still have *run*. The **Deflated Sharpe Ratio**,
  walk-forward fold rate, regime split, jitter and robustness score are
  *recorded* there and enforced at paper→live. (Settings → Door into paper →
  *Strict paper* restores the old door where all of them block paper.)
- The **paper pool** holds 40 strategies by default (3 per coin, 10 per
  family). A strategy that passes the door while the pool is full waits in a
  queue ranked by its recorded evidence (`paper_queue_full` is capacity, not a
  rejection). Paper strategies are retired automatically when clearly losing
  (15+ trades, negative mean, t < −1), at a 15% forward drawdown, after 60 idle
  days, or when forward trades contradict the backtest.
- The **research holdout** is on by default: research reads (backtests,
  optimizer, walk-forward) stop at a cutoff 3–6 months back, and each strategy
  gets ONE test on the held-back period when it applies for live. Don't tune
  against it; a FAIL is a merit rejection.
- **Paper→live** needs 20+ forward trades and 30 days, then a sequential test:
  forward must track the backtest, and backtest + forward evidence must clear a
  bar corrected for how many strategies are in paper. The recorded checks, the
  held-back test and DSR on backtest + forward trades are enforced here.
- `gate-report` reports the gate to the strategy's next stage (`target_stage`):
  gauntlet→paper (with `paper_door` and `paper_pool`), or for a paper strategy
  the paper→live checklist ending in the real paper→live gate (`live_gate`,
  plus `live_evidence`). Live promotion also needs the operator's typed GO
  LIVE approval.
- `paper-pool` shows occupancy and the queue; `readmit-candidates` lists recent
  archives the research door would have admitted (report only; the operator
  re-queues with `requeue`); `live-recheck` reports how live strategies fare
  against the new live gate (never demotes anything).
- Never set `force=true` to skip a gate.

## Data readiness (check before you build)

A strategy that reads a feed the lake cannot supply runs to a silent 0-trade
result (the "data-substrate mismatch"). Ask first:

```bash
python -m forven.agent readiness --symbol ETH/USDT --timeframe 4h --streams funding,oi
python -m forven.agent readiness --symbol BTC/USDT --timeframe 1h --code-file /abs/draft.py   # scans the draft's columns
python -m forven.agent readiness --strategy S02545
```

```python
report = fc.get_data_readiness(symbol="ETH/USDT", timeframe="4h", streams=["funding", "oi"])
report["verdict"]   # "ready" | "needs_data" | "blocked"
report["summary"]   # one sentence
for req in report["requirements"]:   # candles, history, freshness, each feed (+ venue divergence when on the capital path)
    print(req["status"], req["label"], req["detail"], (req["fix"] or {}).get("label"))
```

- `ready` — build it. Warnings (a late feed, a young symbol) don't block.
- `needs_data` — each `missing` row carries a `fix` with a download request; the
  operator (or the Data page) can fetch it. The backtest also auto-downloads
  fetchable feeds.
- `blocked` — no download can supply it: liquidation history starts at its
  forward-only capture (OKX), a second asset's columns, an unknown symbol.
  Choose another input; do not substitute a proxy.

The history requirement is the pipeline's research window (the quick screen's,
730 days by default) plus the strategy's warmup; streams must cover the window.

## Endpoint reference (for non-Python harnesses: Node/Tauri, curl, Codex)

All under `http://127.0.0.1:8003`. JSON bodies. The CLI/library are just sugar
over these — call them directly from any language.

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | liveness |
| GET  | `/api/ai-dropzone/context` | datasets, template, param families |
| GET  | `/api/quant-skills?regime=` | priors from past survivors |
| GET  | `/api/strategies?status=` | list strategies |
| GET  | `/api/strategies/{id}/container` | full strategy (status under `configuration`) |
| GET  | `/api/lifecycle/strategies/{id}/readiness` | paper-readiness detail |
| GET  | `/api/data/readiness/strategy/{id}` | data contract of a strategy |
| POST | `/api/data/readiness` `{symbol,timeframe,streams?,history_days?,strategy_type?,code?}` | data contract of an idea |
| GET  | `/api/backtesting/runs?limit=` | recent runs |
| GET  | `/api/results/{id}` | one backtest result |
| GET  | `/api/data/sla?stream=&limit_worst=` | freshness SLA census of every stored series |
| GET  | `/api/lifecycle/paper-pool` | paper occupancy, limits, queue, door in force |
| GET  | `/api/lifecycle/paper-pool/readmit-candidates?days=&limit=` | archives the research door would admit |
| POST | `/api/lifecycle/paper-pool/requeue` `{strategy_ids}` | operator: re-run those archives through the door |
| GET/POST | `/api/lifecycle/paper-pool/live-recheck` | report-only live re-check (POST recomputes) |
| POST | `/api/ai-dropzone/sessions` `{label,actor,objective}` | open session |
| POST | `/api/ai-dropzone/sessions/{id}/close` | close session |
| POST | `/api/strategies/intake/register-file` `{file_path,source,session_id?}` | register a `.py` |
| POST | `/api/backtesting/run` `{strategy_id,dataset_id,trade_mode?,parameters?,...}` | backtest |
| POST | `/api/backtesting/optimize` `{strategy_id,dataset_id,n_trials?,...}` | optimize |
| POST | `/api/backtesting/verdict/run` `{strategy_id,dataset_id,tests?}` | robustness tests |
| POST | `/api/strategies/{id}/promote` `{to_status,from_status?,reason,force}` | advance lifecycle |

Auth headers (optional, when enabled): `x-api-key`, `x-operator-key`.

### Minimal Node/TypeScript example (for the Tauri sidecar)

```ts
const BASE = "http://127.0.0.1:8003";
async function call(path: string, body?: unknown) {
  const r = await fetch(BASE + path, {
    method: body ? "POST" : "GET",
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}: ${await r.text()}`);
  return r.json();
}
await call("/api/health");
await call("/api/backtesting/run", { strategy_id: "S02545", dataset_id: "BTC/USDT-1h", request_source: "tauri" });
```
