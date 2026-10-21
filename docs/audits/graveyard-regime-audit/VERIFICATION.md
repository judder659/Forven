# Verification of the graveyard regime audit (2026-07-05)

Adversarial re-check of FINDINGS.md before any program changes. Script:
`scripts/graveyard_regime_audit_verify.py`. Verdict per attack:

## V1 — Duplication / concentration: PASS (with one refinement)

Threat: pooled buckets dominated by near-duplicate strategies taking the same
market event. Recomputed pooled means three ways — raw trades, deduped by
(series, timeframe, entry_bar), and equal-weight-per-strategy:

| bucket | n_raw | n_uniq | raw %/trade | dedup % | eq-weight % |
|---|---|---|---|---|---|
| TREND_DOWN/long | 8,624 | 5,339 | -0.200 | -0.180 | -0.232 |
| HIGH_VOL/long | 1,628 | 867 | -0.272 | -0.230 | -0.440 |
| RANGE_BOUND/long | 8,100 | 5,246 | -0.066 | -0.118 | 0.000 |
| TREND_UP/long | 13,190 | 6,824 | -0.027 | -0.106 | -0.089 |
| TREND_DOWN/short | 1,553 | 1,325 | +0.167 | +0.128 | +0.494 |
| (all other short buckets) | | | + | + | + |

Every sign survives every weighting. Concentration is real but bounded:
top-10 strategies hold 28% of TREND_DOWN/long losses; 26% of those trades sit
in bars with ≥5 same-bar copies — which is exactly why the dedup/eq-weight
columns matter, and they agree.

**Refinement:** TREND_UP/long is mildly NEGATIVE (-0.09 to -0.11%) under fair
weighting, not breakeven — duplicated winners flattered the raw mean. The
regime gradient (downtrend/high-vol longs ≈ 2-4x worse than uptrend longs)
stands; "longs were fine in uptrends" does not.

## V2 — Direction coverage: PASS

100% of 35,071 trades carry an explicit direction field (31,542 long / 3,529
short). The ~9:1 long bias is real, not a parser default.

## V3 — Return parsing: PASS (units artifact explained)

Per-strategy compound of parsed returns vs persisted totals: corr = 1.0000
across 585 strategies. The nonzero "diffs" are purely a storage-units
convention: `total_return_pct` in metrics_json stores a FRACTION despite the
`_pct` name (worst "mismatches" are exact ×100 ratios, e.g. parsed -99.9975
vs stored -0.99998). Parsing is validated; the field name is a known-style
forven gotcha.

## V4 — Temporal clustering: PASS with a scope caveat

Within buckets, no single episode dominates: TREND_DOWN/long spans 2,124
distinct regime segments (top-3 hold 5% of trades); TREND_DOWN/short spans
599 (top-3: 5%). But calendar coverage is concentrated: ~two-thirds of all
trades fall in Mar–Jun 2026. The findings are a robust description of the
recent market, not yet proven across 2020-2026 — that requires the phase-2
re-backtests.

## V5 — Lookahead sensitivity: PASS (the real-time question)

Threat: the entry bar's regime label incorporates that bar's own close.
Recomputed every bucket using the PREVIOUS bar's regime — information fully
known before the trade existed:

| bucket | prev-bar mean % | entry-bar mean % |
|---|---|---|
| TREND_DOWN/long | -0.187 | -0.200 |
| HIGH_VOL/long | -0.223 | -0.272 |
| TREND_UP/long | -0.047 | -0.027 |
| TREND_DOWN/short | +0.190 | +0.167 |
| HIGH_VOL/short | +0.103 | +0.251 |
| (other shorts) | + | + |

All signs and approximate magnitudes hold on strictly-prior information.
A live gate acting on the classifier's last completed bar would have captured
essentially the measured effect. No hindsight required.

## V6 — Cross-validation vs production: PASS

For 36 strategies where the production pipeline's own regime_split artifact
judged the SAME baseline result, per-regime trade counts agree with a median
disagreement of 3.7% of trades (p90 17.8%; production fetches a shorter
candle context, so small drift is expected). The audit's classification is
consistent with what the pipeline itself computes.

## Net verdict

The two actionable conclusions are confirmed and safe to design against,
scoped to the recent market:
1. Longs taken while the causal classifier already read TREND_DOWN or
   HIGH_VOL were the dominant loss engine — and V5 shows a real-time gate
   would have caught it.
2. Shorts were net-positive in every regime bucket under all weightings, at
   ~9:1 under-generation (direction data 100% explicit).

Still unproven (needs phase-2 full-history re-backtests): that these
conditional edges persist across 2020-2024 markets, and any individual
resurrection candidate.
