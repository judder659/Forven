# Graveyard regime resurrection audit — findings (2026-07-05)

Companion to the auto-generated `REPORT.md` (tables) and `audit_results.json`
(full per-strategy detail). Produced by `scripts/graveyard_regime_audit.py`
on branch `audit/graveyard-regime-resurrection`.

## The question

Did we archive strategies that had a real edge inside one market regime and
were only killed by full-window scoring during hostile regimes ("we've been
in a downtrend the past year")?

## Coverage (read this first)

- Graveyard: 5,893 strategies (archived + rejected). Only **585 were
  auditable** (~10%) — artifact compaction deleted the per-trade lists for the
  rest, and 182 more had <10 classifiable trades.
- The audited verdicts were rendered on **~109-day windows** (p10=p50=109,
  p90=120). A 3.6-month window cannot contain enough regime episodes to prove
  or disprove a specialist edge. Every per-strategy conclusion below inherits
  this limit.

## Verdict on the hypothesis: partially confirmed, with a twist

The downtrend DID do the damage — but not by burying provable uptrend
specialists. It did it by bleeding long-biased generalists.

**1. The regime calendar confirms the prior.** Trailing 365d is the most
downtrend-heavy stretch since 2022 (BTC 4h: 42% TREND_DOWN vs 33% TREND_UP;
2026 YTD: 46% vs 34%). Strategies judged recently were judged mostly on
hostile tape.

**2. Where the graveyard's money actually died (pooled net per-trade returns
across all 34,349 audited trades):**

| bucket | trades | mean/trade | sum |
|---|---|---|---|
| TREND_DOWN / long | 8,394 | **-0.202%** | **-1,695%** |
| HIGH_VOL / long | 1,606 | -0.276% | -443% |
| RANGE_BOUND / long | 7,943 | -0.067% | -533% |
| TREND_UP / long | 12,941 | -0.034% | -438% |
| every SHORT bucket | 3,465 total | **positive in all 4 regimes** | +401% |

Two structural facts:
- **Longs-into-downtrend is the single biggest loss engine** (worst sum, and
  half the per-trade toxicity of longs generally).
- **Generation is ~9:1 long-biased** (30,884 long vs 3,465 short trades),
  while shorts were net profitable in every regime, including
  TREND_DOWN/short at +0.151%/trade (n=1,524).

So the graveyard is mostly long-biased strategies whose in-trend performance
was ~breakeven after costs and whose downtrend/high-vol longs killed the
full-window metrics. A direction-aware regime gate (no longs in
TREND_DOWN/HIGH_VOL) would have removed the dominant loss source outright.

**3. Individually revivable strategies: thin.** 0 STRONG, 2 CANDIDATE,
15 WATCH (of which 7 are one near-duplicate BTC-MACD swarm — effectively ~8
distinct). Only 4 have permutation p < 0.05 (regime split unlikely to be
luck):

| strategy | regime | n | mean/trade | t | perm_p | note |
|---|---|---|---|---|---|---|
| S04637 SOL rsi_momentum 1h | RANGE_BOUND | 12 | +0.61% | 3.5 | 0.010 | alpha_t -1.17 → mostly beta |
| S04847 BTC ORB_ADX_SHORT 15m | TREND_UP | 16 | +0.71% | 1.6 | 0.014 | |
| S05305 SOL ATR_BREAKOUT 15m (on BTC) | TREND_DOWN | 23 | +0.18% | 1.95 | 0.021 | |
| S04908 BTC ATR_BREAKOUT 15m | TREND_DOWN | 22 | +0.17% | 1.82 | 0.025 | |

These four (plus CANDIDATE S04675, perm_p 0.09) are worth a regime-gated
re-backtest over 2020→now, but nobody should expect a goldmine: the means are
small and most alpha_t values say the regime itself, not the strategy, earned
the return.

## What this changes about the regime-features plan

1. **Direction × regime gating is the headline win**, bigger than specialist
   revival: block/flip longs when the causal classifier says TREND_DOWN or
   HIGH_VOL. The pooled table is the evidence.
2. **The long bias in generation is a bug-sized opportunity** — shorts are
   under-generated 9:1 and were profitable in every regime bucket. (Likely
   related to the known both-mode→long_only defaulting issue.)
3. **Short windows are the real verdict-quality problem.** 109-day windows
   judge a strategy on one regime draw. Regime-aware judging (or simply longer
   windows with per-regime scorecards) matters more than resurrection.
4. **A definitive resurrection audit needs re-backtests, not artifacts.**
   90% of the graveyard has no surviving trade lists; re-running the top
   graveyard families over 2020→now with per-regime scorecards is the phase-2
   if wanted.

## Method notes / honesty

- Regime = the production causal classifier (forven.regime._classify over
  prefix-causal indicators), vectorized replica parity-checked at runtime
  (0 mismatches / 14,265 bars) — no hindsight labels.
- Returns are net per-trade returns as persisted by the kernel.
- ~4 regime buckets × 585 strategies were searched; at CANDIDATE thresholds
  some survivors by chance are expected — hence the permutation p-values and
  the "input to re-validation, not direct revival" stance.
- alpha_t compares each trade to the asset's own move over the held bars;
  low/negative alpha_t with positive returns = regime beta, which is still
  monetizable but belongs to the regime gate, not the strategy.
