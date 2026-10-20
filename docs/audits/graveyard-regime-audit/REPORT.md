# Graveyard regime resurrection audit

Generated: 2026-07-05T21:08:33.300983+00:00 · runtime 56.6s

## Coverage

- Graveyard (archived + rejected): **5893** strategies
- With persisted backtests: 5127
- With surviving trades artifacts (auditable): **585** (skipped: {'too_few_classified_trades': 182, 'no_candles': 4})

Coverage caveat: artifact compaction deleted trades for most older results; this audit can only judge the strategies whose trade lists survived on disk.

## Regime calendar (share of bars, %)

### BTC-USDT/1h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 50.0 | 19.1 | 27.0 | 4.0 |
| 2021 | 45.1 | 28.2 | 24.2 | 2.5 |
| 2022 | 31.2 | 36.6 | 28.2 | 4.1 |
| 2023 | 41.2 | 25.1 | 25.1 | 8.6 |
| 2024 | 45.5 | 24.1 | 23.9 | 6.5 |
| 2025 | 38.9 | 28.4 | 26.2 | 6.5 |
| 2026 | 32.6 | 34.9 | 26.3 | 6.2 |
| **trailing 365d** | 34.5 | 33.5 | 26.0 | 6.0 |

### BTC-USDT/4h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 49.0 | 15.2 | 31.6 | 4.3 |
| 2021 | 50.4 | 25.9 | 21.5 | 2.2 |
| 2022 | 26.4 | 46.1 | 24.3 | 3.2 |
| 2023 | 50.0 | 20.7 | 23.1 | 6.2 |
| 2024 | 53.2 | 21.4 | 21.3 | 4.1 |
| 2025 | 36.1 | 32.4 | 29.1 | 2.5 |
| 2026 | 34.1 | 45.5 | 19.9 | 0.5 |
| **trailing 365d** | 32.6 | 41.6 | 24.6 | 1.1 |

### ETH-USDT/1h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 50.6 | 18.5 | 27.4 | 3.4 |
| 2021 | 50.4 | 22.7 | 24.7 | 2.2 |
| 2022 | 34.9 | 37.0 | 25.3 | 2.8 |
| 2023 | 42.7 | 26.5 | 25.3 | 5.6 |
| 2024 | 40.4 | 29.0 | 25.7 | 4.9 |
| 2025 | 37.9 | 31.6 | 25.9 | 4.6 |
| 2026 | 29.9 | 36.5 | 28.0 | 5.6 |
| **trailing 365d** | 34.8 | 33.3 | 27.5 | 4.5 |

### ETH-USDT/4h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 48.5 | 10.5 | 36.2 | 4.9 |
| 2021 | 57.2 | 21.7 | 18.7 | 2.4 |
| 2022 | 25.1 | 47.9 | 24.5 | 2.5 |
| 2023 | 48.3 | 23.2 | 25.0 | 3.4 |
| 2024 | 45.3 | 33.5 | 18.5 | 2.8 |
| 2025 | 32.7 | 37.7 | 27.1 | 2.5 |
| 2026 | 31.0 | 37.9 | 29.4 | 1.7 |
| **trailing 365d** | 35.6 | 36.4 | 26.8 | 1.2 |

### SOL-USDT/1h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 25.2 | 39.9 | 31.2 | 3.6 |
| 2021 | 48.7 | 20.3 | 27.0 | 4.1 |
| 2022 | 27.8 | 45.7 | 23.9 | 2.6 |
| 2023 | 42.9 | 27.5 | 25.6 | 4.0 |
| 2024 | 41.6 | 29.5 | 25.3 | 3.6 |
| 2025 | 37.5 | 34.0 | 25.0 | 3.5 |
| 2026 | 37.2 | 33.7 | 25.2 | 3.8 |
| **trailing 365d** | 38.2 | 32.3 | 25.8 | 3.6 |

### SOL-USDT/4h

| period | TREND_UP | TREND_DOWN | RANGE_BOUND | HIGH_VOL |
|---|---|---|---|---|
| 2020 | 9.0 | 36.9 | 49.2 | 4.9 |
| 2021 | 51.1 | 13.7 | 27.0 | 8.1 |
| 2022 | 18.4 | 55.2 | 22.7 | 3.7 |
| 2023 | 46.8 | 24.2 | 23.0 | 6.0 |
| 2024 | 45.3 | 22.8 | 28.7 | 3.2 |
| 2025 | 35.9 | 39.0 | 23.2 | 1.9 |
| 2026 | 27.0 | 43.7 | 27.4 | 1.8 |
| **trailing 365d** | 33.8 | 39.1 | 25.6 | 1.6 |

## Pooled graveyard trades by regime × direction

Net per-trade returns pooled across ALL audited strategies — where the
graveyard's money actually went.

| regime/direction | trades | mean ret % | total ret % | win rate % |
|---|---|---|---|---|
| HIGH_VOL/long | 1606 | -0.2755 | -442.5 | 25.5 |
| HIGH_VOL/short | 168 | 0.2507 | 42.1 | 45.8 |
| RANGE_BOUND/long | 7943 | -0.0671 | -532.6 | 25.9 |
| RANGE_BOUND/short | 799 | 0.0825 | 65.9 | 37.5 |
| TREND_DOWN/long | 8394 | -0.202 | -1695.4 | 26.4 |
| TREND_DOWN/short | 1524 | 0.1514 | 230.7 | 36.3 |
| TREND_UP/long | 12941 | -0.0338 | -437.6 | 29.6 |
| TREND_UP/short | 974 | 0.0636 | 61.9 | 46.2 |

Evaluation-window length (days) p10/p50/p90: 109 / 109 / 120 —
most graveyard verdicts were rendered on windows of roughly this size, i.e. a
single ~3–4 month slice of whatever regime mix happened recently.

## STRONG tier (0)

(none)

## CANDIDATE tier (2)

| strategy | type | series/tf | regime | n | mean% | t | eps | perm_p | alpha_t | full% | in-regime% | bleed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S04637 SOL-RSI_MOMENTUM-S04637 | rsi_momentum | SOL-USDT/1h | RANGE_BOUND | 12 | 0.61 | 3.5 | 12 | 0.01 | -1.17 | 7.69 | 7.51 |  |
| S04675 ETH-ATR_BREAKOUT-S04675 | atr_breakout | ETH-USDT/15m | TREND_DOWN | 19 | 0.65 | 2.71 | 19 | 0.1005 | 1.74 | 18.59 | 12.96 |  |

## WATCH tier (15)

| strategy | type | series/tf | regime | n | mean% | t | eps | perm_p | alpha_t | full% | in-regime% | bleed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S04847 BTC-BTC_ORB_ADX_SHORT-S04847 | btc_orb_adx_short | BTC-USDT/15m | TREND_UP | 16 | 0.71 | 1.6 | 15 | 0.014 | -0.58 | -12.85 | 11.81 | Y |
| S05305 SOL-ATR_BREAKOUT-S05305 | atr_breakout | BTC-USDT/15m | TREND_DOWN | 23 | 0.18 | 1.95 | 22 | 0.021 | -0.3 | 0.72 | 4.16 | Y |
| S04908 BTC-ATR_BREAKOUT-S04908 | atr_breakout | BTC-USDT/15m | TREND_DOWN | 22 | 0.17 | 1.82 | 21 | 0.0245 | -0.15 | 0.12 | 3.85 | Y |
| S05218 SOL-SOL_ADX_VOL_EMA_CROSS_V2 | sol_adx_vol_ema_cr | SOL-USDT/1h | TREND_UP | 12 | 0.85 | 1.8 | 12 | 0.1145 | -1.16 | 10.34 | 10.52 | Y |
| S02120 ETH-ETH_MACD_FAST_W12B_V9-S0 | eth_macd_fast_w12b | ETH-USDT/4h | TREND_UP | 96 | 0.56 | 1.55 | 32 | 0.305 | -0.39 | 69.86 | 62.15 |  |
| S04683 BTC-MACD-S04683 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.23 | 1.62 | 17 | 0.3115 | -0.69 | 7.14 | 7.1 |  |
| S04678 BTC-MACD-S04678 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.23 | 1.62 | 17 | 0.315 | -0.69 | 7.14 | 7.1 |  |
| S03405 ETH-ETH_ATRCHAN_BREAK_R7L1_D | eth_atrchan_break_ | ETH-USDT/1h | TREND_DOWN | 25 | 0.87 | 1.64 | 18 | 0.319 | 0.93 | 29.49 | 23.22 |  |
| S05102 ETH-FUNDING_MOMENTUM-S05102 | funding_momentum | ETH-USDT/1h | RANGE_BOUND | 18 | 0.25 | 1.56 | 18 | 0.32 | -1.78 | 4.42 | 4.59 | Y |
| S02178 SOL-ETH_MACD_FAST_V11B-S0217 | eth_macd_fast_v11b | ETH-USDT/4h | TREND_UP | 70 | 0.20 | 1.73 | 29 | 0.3275 | -1.19 | 22.15 | 14.62 |  |
| S04681 BTC-MACD-S04681 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.23 | 1.62 | 17 | 0.3275 | -0.69 | 7.14 | 7.1 |  |
| S04686 BTC-MACD-S04686 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.23 | 1.62 | 17 | 0.3275 | -0.69 | 7.14 | 7.1 |  |
| S04672 BTC-MACD-S04672 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.22 | 1.55 | 17 | 0.329 | -0.73 | 6.81 | 6.78 |  |
| S04647 BTC-MACD-S04647 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.23 | 1.62 | 17 | 0.337 | -0.69 | 7.14 | 7.1 |  |
| S04679 BTC-MACD-S04679 | macd | BTC-USDT/4h | TREND_UP | 30 | 0.22 | 1.55 | 17 | 0.3375 | -0.73 | 6.81 | 6.78 |  |

## Reading guide & caveats

- `t` is the t-stat of per-trade net returns inside the best regime; `eps` is how many
  distinct historical episodes of that regime the trades span (guards one-lucky-segment).
- `perm_p`: probability of seeing a best-bucket t this large with regime labels shuffled
  across the strategy's own trades (2000 permutations). Small = the regime
  split is real, not luck.
- `alpha_t`: t-stat of per-trade returns MINUS the asset's own move over the held bars.
  Low alpha_t with high t = mostly regime beta (still tradeable gated, but the regime
  calendar is doing the work, not the strategy).
- `bleed` = out-of-regime trades sum negative → full-window scoring buried the in-regime edge.
- Returns are net of the kernel's fees/slippage/funding as persisted per trade.
- Selection caveat: ~4 buckets tried per strategy across the audited set — expect some
  CANDIDATE-tier survivors by chance; perm_p and episode counts are the defenses.
  This shortlist is input to re-validation (regime-gated re-backtest + gauntlet),
  NOT direct revival.