# Cross-Sectional Basket Strategies — Design (2026-07-06)

## Why

Every strategy the substrate can express is single-asset time-series: one
symbol in, long/short/flat signals out. The most credible payer-backed edge
class in crypto perps — cross-sectional carry (short the highest-funding
perps, long the lowest, dollar-neutral, collect the spread) — is structurally
inexpressible. Funding exists because levered longs pay for leverage;
harvesting it is selling a service, not predicting price. The 2026-07-05
graveyard audit's "shorts net-positive in every regime" is consistent with a
persistent paid-to-be-short tilt this class would capture systematically.

## Phasing (prove the edge before building plumbing)

- **Phase 0 (this): research engine.** `forven/basket_lab.py` — a
  cross-sectional backtest engine + `FundingCarryBasket` reference strategy,
  run over the deep research universe. Deliverable: net-of-costs evidence the
  edge class does/doesn't exist on OUR data, with a placebo control. No
  lifecycle coupling.
- **Phase 1: gauntlet-grade validation.** WFA folds on the basket equity
  curve, universe perturbation (drop-k symbols), cost stress (2x fees/slip),
  parameter jitter (n_legs, rebalance cadence). Wire verdicts into
  validation_artifacts so baskets face the same honesty bar as strategies.
- **Phase 2: paper.** Multi-leg basket position in the paper ledger, a
  rebalance scheduler job, correlation/budget integration (a dollar-neutral
  basket's effective exposure is its GROSS, not net). MTM prove-it like any
  paper strategy.
- **Phase 3: live.** Per-leg orders through the existing sanctioned gate
  stack (liquidity guard, per-order caps, asset ceilings). Nothing new at the
  order layer — a basket is N small orders.

## Engine semantics (Phase 0)

Honesty conventions mirror the execution kernel wherever they map:

- **No lookahead**: scores computed on bar `t` CLOSE data (enrichment columns
  already bucket-close shifted by `DataManager.enrich`); target weights take
  effect at bar `t+1` OPEN. Per-bar returns are open-to-open.
- **Fills/costs**: turnover cost = Σ|Δw| × (fee_bps + slippage_bps)/1e4 of
  equity at each rebalance (4.5 + 2.0 bps per side — `policy.py` defaults).
- **Funding**: per-bar accrual `−w × funding_rate × bar_hours` per leg, using
  the enriched per-hour `funding_rate` column (Binance 8h rate ÷ 8 — the
  pipeline-wide convention). Positive funding: longs pay, shorts receive.
- **Weights**: equal-weight per leg, `gross_leverage / (2·n_legs)` each;
  short the top-`n_legs` by score, long the bottom-`n_legs`; sides kept equal
  size (dollar-neutral by construction). Constant-mix between rebalances
  (drift ignored — second-order at 8–24h cadence; revisit in Phase 1).
- **Eligibility at t**: non-NaN close and funding, ≥ `min_history_bars` of
  data, price present for the fill bar. Inception/delist handled naturally by
  NaN boundaries (registry-backed series keep dead coins in the lake).
- **Decomposition**: price PnL vs funding PnL reported separately — a carry
  strategy whose PnL is mostly price movement is beta wearing a costume.
- **Placebo control**: same panel, same costs, same cadence, but ranks
  shuffled each rebalance (N seeds). The real strategy must clear the placebo
  distribution — otherwise the "edge" is universe drift + cost luck.

## Strategy contract

```python
class BasketStrategy:
    name: str
    rebalance_hours: int = 8
    n_legs: int = 5
    gross_leverage: float = 1.0
    def score(self, panel: BasketPanel, t: int) -> pd.Series:
        """Per-symbol score at bar index t. Engine SHORTS the top-n_legs
        and LONGS the bottom-n_legs. Use only panel data at/before t."""
```

`BasketPanel` carries aligned matrices (bars × symbols): `open`, `close`,
`funding`, plus any enrichment column the panel was built with (basis, oi,
ls_ratio, iv — same availability caveats as DATA_SCHEMA.md).

## Reference strategy

`FundingCarryBasket.score = current funding_rate` — pure carry, no price
signal. Variants (momentum-filtered carry, basis-ranked, OI-crowding) come
after the pure version's verdict, not before.
