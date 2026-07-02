# Edge Data Expansion — Implementation Plan (2026-07-02)

Goal: maximize the data surface available to strategy discovery (backtester +
gauntlet) so more hypotheses can be tested on more evidence. Three levers, in
order of statistical value:

1. **More history** — more trades per hypothesis → tighter confidence, deeper
   WFA folds, regime coverage (2020 crash, 2021 mania, 2022 bear, ...).
2. **More assets** — a cross-sectional research universe → the gauntlet can
   demand an edge generalizes across symbols instead of curve-fitting one.
3. **More orthogonal signals** — new hypothesis classes (basis, implied vol,
   positioning, liquidations) rather than more variations of price.

Discipline requirement threaded through everything: every new stream gets the
same causality treatment as the existing ones (bucket-close shift for
forward-window aggregates, research-only quarantine where lookahead is
unavoidable), and verdicts must become auditable against the data they were
scored on — more data must produce more TRUE edges, not more false ones. No
paid vendors in this plan (see docs note: BV + HL archives cover it free).

---

## Phase A — More history, more assets (raise N)

**A1. Research universe expansion (M).**
Enumerate all listed Binance USD-M perps (exchange markets + BV catalog),
rank by liquidity (quote volume), and deep-backfill a configurable
`research_universe` (default ~50 symbols) on the standard timeframe ladder:
1h/4h/1d full history, 15m/5m for the top tier, 1m for majors only.
- Distinct from the TRADING universe: scanner/keep-alive stays on the curated
  active set; research symbols refresh on the slow catch-up cadence only.
- Machinery already exists end-to-end: BV backfill (headerless parser now
  fixed), coverage self-healing, tail appends, completeness planning. This is
  mostly configuration + an enumeration/seed job.
- Storage ballpark: ~50 symbols on the ladder ≈ a few GB parquet (zstd) —
  fine locally.

**A2. Point-in-time universe registry (S/M) — the survivorship antidote.**
Per-symbol `inception_ts` (BV probe start / first bar) and `delist_ts`
(exchangeInfo status + last-bar heuristics), stored in the DuckDB catalog.
- Backtest/gauntlet windows respect "did this asset exist and trade at T".
- Keep delisted series in the lake (BV retains their archives) — dead coins
  are exactly the data survivorship bias erases.
- Keep-alive skips delisted symbols (no more MATIC-style stale sweeps).

**A3. Deep derivatives history for the research universe (S).**
BV `metrics` files carry OI (+ long/short ratios) far beyond the 30-day REST
window we currently live with. Extend the BV backfill to populate OI/LSR
history for every research-universe symbol, so positioning strategies can be
tested over years, not weeks. Pre-coverage bars stay NaN (not fake zeros).

## Phase B — New orthogonal streams (each causally gated)

**B1. Basis + mark price from BV klines variants (S/M).**
New BV streams (same client, new URLs): `premiumIndexKlines` → `basis_pct`
enrichment column; `markPriceKlines` → mark-price series.
- Basis is a funding predictor and a crowding/regime signal — a genuinely new
  hypothesis class for trend/carry strategies.
- Mark price is what liquidations and PnL actually reference; kernel can use
  it for honest liquidation-adjacent modeling later.
- Causality: bar CLOSE values only (bucket-close shift, same as taker/LSR).

**B2. Implied volatility regime — Deribit DVOL (S).**
Free API; BTC/ETH IV index history. Enrichment columns `iv_btc`/`iv_eth`.
- Crypto-native and 24/7 → eligible for the STRATEGY path (unlike equity
  macro), with bucket-close shift on the hourly series.
- Vol-regime filters are the classic orthogonal overlay: same entry logic,
  different regimes → different verdicts.

**B3. Liquidations, forward capture (M, gated on demand).**
Binance WS `forceOrder` stream → 1h aggregation into the existing
liquidations schema (the REST endpoint is dead; collector currently
env-gated off). History accumulates from day one of capture.
- Decision gate: enable for the research universe once one liquidation-aware
  strategy concept is queued; revisit Tardis only if such strategies PASS the
  gauntlet and need deep history.

**B4. Hyperliquid venue series (M).**
HL public archive + live API → `source=hyperliquid, market=perp` candles +
funding for the traded subset.
- Enables the venue-fidelity gauntlet stage: final validation on the venue we
  execute on; continuous Binance↔HL divergence feeds the promotion gate.

**B5. Historical spread/depth from BV `bookDepth`/`bookTicker` (M/L).**
Sampled top-of-book + depth history for the research universe.
- Primary consumer: empirically calibrated slippage/spread curves in the
  kernel (edges measured NET of realistic costs kill fake edges early).
- Also closes the deferred liquidity-floor guard gap (no entries into depth
  that can't absorb the size).
- Secondary: depth-imbalance enrichment columns, only if a consumer emerges.

## Phase C — Make the data usable by the discovery loop

**C1. Schema exposure to strategy generation (S).**
Every new column documented in `templates/workspace/DATA_SCHEMA.md` with
availability windows and NaN semantics. The AI strategy generators only
hypothesize over columns they know exist — undocumented data finds no edges.

**C2. Data-quality gates on verdicts (M) — fail closed.**
Preconditions before quick_screen/gauntlet stages score: completeness ≥
threshold over the eval window, zero unfilled gaps inside it, freshness,
history ≥ strategy warmup, venue divergence in bounds. Violations return the
existing `blocked_data` status and defer to the self-healing backfill.
A strategy must never be promoted or archived on defective data.

**C3. Dataset fingerprints + as_of pinning (M) — verdict auditability.**
Stamp every backtest/gauntlet verdict with per-series fingerprints (checksum,
span, row count, market) and pin `as_of` at gauntlet-candidacy creation so
all stages score identical data. Automatic re-baseline flagging when a
verdict's underlying data drifts (today's perp rebuild would have been
auto-detected instead of remembered).

**C4. Fast multi-asset loads (S/M).**
Cross-sectional testing over ~50 symbols needs windowed reads: wire backtest
loads through the DuckDB hub's start/end predicate pushdown instead of
full-series loads + tail(n). Matters most for 1m/5m work.

**C5. Intra-bar fill resolution (M/L, kernel + data).**
Serve aligned 1m sub-bars under each higher-TF bar (`load_subbars`) so the
kernel resolves stop-vs-target ordering inside a bar from the actual path
instead of an assumption. Changes what every backtest number means; the
single biggest fidelity upgrade available.

## Phase D — Overfitting counterweights (small, mostly bookkeeping)

**D1.** Record the enrichment columns each strategy consumes into its verdict
(feature-count context for DSR/trials accounting — more columns tested must
raise the bar, not lower it).
**D2.** Anomaly/outage ledger: known exchange-downtime windows (Binance/HL
maintenance) recorded once, maskable in backtests, so "edges" concentrated in
outage artifacts get caught.

---

## Sequencing

```
Run 1  A1 + A2 + A3     universe + survivorship + deep OI    (config-heavy, machinery exists)
Run 2  C2 + C3 + C1     gates + fingerprints + schema        (trust layer BEFORE the flood)
Run 3  B1 + B2          basis + IV enrichment                (two cheap orthogonal signals)
Run 4  C5 + B5          intra-bar + calibrated slippage      (fidelity pair, kernel-adjacent)
Run 5  B4               HL venue series + fidelity stage     (pre-live-capital)
Gated  B3               liquidations WS                      (when a consumer strategy exists)
```

Rationale for Run 2 before Run 3: gates and fingerprints BEFORE the data
flood — a 5x larger universe with new columns multiplies the ways a defective
series or drifted dataset can mint a false PASS. Trust infrastructure first,
then volume.

Non-goals (revisit only with a concrete consumer): paid vendors (Tardis,
Kaiko, on-chain), tick/L2 live capture, equities expansion, more macro
streams.

Re-baseline note: A1/A3 extend history for existing symbols → backtest
metrics for existing strategies WILL move. Fold into the already-pending
re-baseline rather than doing two.
