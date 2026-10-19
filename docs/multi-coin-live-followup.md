# Multi-coin containers: the live step (follow-up, needs Jud's explicit OK)

Status: **not built.** Multi-coin containers (`forven/multi_coin.py`, gate plan
phase 5) trade in **paper only**. The live gate holds every container with
`multi_coin_live_review` ("Multi-coin live review pending"), whatever its record.
This note lists what the live step needs, so it can be reviewed and approved on
its own. Going live still needs the operator's typed GO LIVE as well.

## What paper does today

- A paper strategy whose breadth test reads `general` gets a frozen coin list:
  its home coin plus the coins where the unchanged rule made money in the test,
  best Sharpe first, at most `paper_trading.multi_coin_max_coins` (default 6).
  It is written once to KV `forven:multi_coin:<id>` and never rewritten.
- The scanner runs one kernel pass per coin (`scanner.manage_container_via_kernel`).
  Each leg sizes on `1 / N` of the strategy's paper equity and reserves margin
  per coin, so the container risks no more than the strategy on one coin.
- Every leg records under the strategy id. Before any gate reads the record,
  trades are pooled per UTC day (`multi_coin.pool_by_day`, weighted by each
  trade's `container_weight`), so correlated coins count as days of evidence.
- The Propr mirror skips container trades and Track record keeps containers on
  probation.

## What the live step needs

1. **Exchange path review (touches `forven/exchange/`).** The live order path
   assumes one coin per strategy:
   - `scanner._guard_open_trade_execution_intent` refuses an open whose asset
     differs from `strategies.symbol` ("cross-asset open blocked").
   - The live kernel path (`manage_positions_via_kernel(..., execution_type="live")`)
     treats any open row on another coin as a stale-asset position held for review.
   - `exchange/risk.py` `can_open` live pool: slots, per-strategy limits and
     direction books are counted per strategy, so one container could take
     several of the 10 mainnet position slots.
   - The daemon's reconciliation and the unmanaged-position alerts key on the
     strategy's single symbol.
2. **Sizing.** Mainnet runs about $1,000 at 1% risk per trade and 3x leverage.
   The plan's default: cap a live container at 3-4 coins and skip any coin whose
   per-coin size falls under the exchange minimum; the caps themselves do not change.
3. **Execution contract.** `strategies/execution_contract.py` binds one asset per
   accepted promotion (`contract_error`: "market changed"). A container needs the
   coin list in the contract so live provably trades what paper validated.
4. **Propr mirroring.** Define how a container maps onto the challenge account
   (all legs at a share of the slice, or home coin only) before Track record can
   qualify one.
5. **Daemon restart.** Exchange and risk changes only load after the admin
   restart (`scripts\launcher-control.ps1 -Action restart -Service all`).

Until then a container that clears the live evidence test stays in paper and
keeps building its record; nothing moves to live automatically.
