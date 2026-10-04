# Universe trend blend — pre-registered specification

Written and committed **before** any backtest of this rule was run in this build
(2026-10-04). Amended the same day, still before any run, to state the warm-up,
what the 2.0 cap applies to, cost basis and gap handling. Nothing below may be tuned against results; a change needs a new spec
and a new book name.

## Why a universe strategy

A single-coin strategy makes 30–80 trades in six months, too few to tell skill from
luck: 2 of 11 candidates passed the six-month held-back test in the 10-03/04 hunt,
about the rate a no-edge rule passes by chance. One rule held across many coins
every day produces far more independent evidence per week.

## Rule

| Item | Value |
|---|---|
| Universe (fixed, the 2026-09-25 benchmark list) | BTC, ETH, XRP, LINK, XLM, ADA, ZEC, BNB, DOGE, SOL, UNI, AVAX, NEAR, AAVE, BCH (USDT perps) |
| Bars | Daily UTC closes, built from the 1h lake (`close.groupby(index.floor("D")).last()`, complete days only) |
| Forecast and per-asset position | `forven.baseline_hurdle.trend_baseline_positions` unchanged: Carver EWMAC 8/32, 16/64, 32/128, 64/256 with scalars 5.3/3.75/2.65/1.87, forecasts capped at ±20 and averaged over warm speeds, position = forecast/10 × 20% ÷ annualised vol (36-day EWM), capped ±2 |
| Book weights | equal risk budget: each live asset's position ÷ number of live assets |
| Book vol target | 20% a year: scale = 0.20 ÷ annualised realised vol of the unscaled book's daily price returns over the trailing 60 days up to and including the decision day (at least 40 days; flat until then). The cap of 2.0 is on this scale factor, so gross exposure can reach 2 × the summed \|position\| ÷ live coins (at most 4.0) |
| Rebalance | daily, on the completed daily close; fills at that close |
| Gaps | a coin missing up to 3 daily closes keeps its last position and books the whole move when prices resume; after 3 missing days it leaves the book |
| Costs | 6.5 bps per unit of turnover of the scaled weights (4.5 fee + 2.0 slippage, the backtest defaults) |
| Funding | Binance per-settlement funding converted to per-hour, summed over each UTC day; longs pay positive funding |

Two books, both pre-registered:

- **`trend_blend_long_flat` (primary):** negative forecasts clipped to zero, so the book is long or flat in each coin.
- **`trend_blend_long_short` (secondary):** the same rule with shorts allowed.

## Deviations from the 2026-09-25 benchmark (`tsmom_bench.py`)

- The forecast is the repo's `trend_baseline_positions`, so it is the single source of truth and is the same rule the held-back alpha hurdle uses. The benchmark normalised forecasts by an expanding mean absolute value and capped at ±2.
- The vol-target scale factor is capped at 2.0 instead of 4.0.
- The scale is set from the unscaled book's price returns, and costs are charged on the scaled weights (so leverage changes pay costs). The benchmark scaled its net returns after the fact.
- Costs are 6.5 bps instead of 10 bps. Missing funding is treated as unknown (zero, with coverage reported) rather than a default 0.01%/8h.

## Evidence plan

1. **Research report on sealed data (2021-01-01 to the research cutoff).** It reports per-year returns, Sharpe, max drawdown and turnover. It also reports:
   - per-coin contribution, and the share of coins with a positive contribution;
   - Sharpe with each coin left out in turn;
   - results at 2× costs;
   - a placebo that circularly shifts each coin's forecast in time;
   - alpha versus equal-weight buy-and-hold.
2. **The post-cutoff slice is shown but labelled "pre-viewed, not independent".** The 2026-09-25 review already saw this rule family's Jul–Sep 2026 result (+4% BTC+ETH, +34.5% 15 coins), so the held-back period is not a clean test for it.
3. **The forward paper book is the clean test.** It starts at deployment, records `started_at`, and never backfills days before it.

## Out of scope

Live execution. The books are paper only and place no orders.
