# Data Manager Overhaul — Review & Plan (2026-07-01)

Full review of the market-data layer (~9,700 lines: `forven/data.py`, `forven/data_manager.py`,
`forven/dataeng/*`, `forven/binance_vision.py`, `forven/api_domains/data.py`, `forven/routers/data.py`,
the `/data` frontend page, scheduler data jobs, health-monitor data checks, and the consumer side —
scanner, backtest, strategy_validation, sim data pump — plus the ~20 data-layer test files).

Goal: make this an industry-leading data manager for a local-first quant platform. Concretely that
means five properties, in priority order:

1. **Correct** — every bar a strategy sees is causally knowable at that bar's close, from a declared
   venue/market, reproducible point-in-time.
2. **Complete** — no silent gaps, no silent zeros; coverage means "bars present", not "date span".
3. **Fast & non-disruptive** — appends cost O(new bars), never O(series); background collection can
   never starve the live WebSocket.
4. **Reliable** — collectors degrade gracefully, state survives restarts, failures page the operator.
5. **Operable** — one jobs surface with progress + cancel; no dead endpoints; honest health.

## What is already strong (keep, and protect with tests)

- Causality hygiene is genuinely best-in-class for this scale: closed-bar-only write invariant
  (`data.py:542`), bucket-close re-stamping of forward-window aggregates (`data_manager.py:432`,
  `hub.py:399`), macro gated research-only, point-in-time `as_of` reads via the append-only revision
  log (`dataeng/revisions.py`).
- Write safety: atomic tmp→`os.replace`, OHLC sanity quarantine (`data.py:564`), path-traversal
  guards (`data.py:331`), hard no-pickle stance, zip-bomb caps in Binance Vision.
- Cheap reads where it counts: parquet-footer coverage/last-timestamp (`data.py:487`, `data.py:940`),
  keep-alive "is a bar even due?" gate, mtime-keyed caches.
- Ops substrate: per-stream collection telemetry persisted to KV, freshness SLAs with operator
  overrides, demand-driven coverage backfill (`dataeng/coverage.py`), the coverage matrix / quality
  leaderboard / activity log UI.

---

## Findings

### A. Correctness bugs (fix immediately — Phase 0)

- **A1 — Dead dry-run validation guard.** `strategy_validation.py:184` calls
  `DataManager.get_ohlcv(...)`, which does not exist. The `AttributeError` is swallowed at `:189` and
  the function returns "unable to validate → allow". The zero-trade dry-run screen has never run.
  *Verified.*
- **A2 — Spot and futures bars mixed in one series.** Binance Vision backfill is USD-M futures only
  (`binance_vision.py:19` → `data/futures/um`) and merges into the same
  `data/ohlcv/{SYMBOL}/{tf}.parquet` whose tail is fetched from Binance **spot**
  (`data.py:1064-1069`, `defaultType: "spot"`). One series, two markets, a basis discontinuity at the
  splice point — and we trade HL perps. Identity compounds this: `DataHub.candles` discards
  `source`/`market` (`hub.py:208`), so a perp and spot request collide on one path. *Verified.*
- **A3 — `exclude_streams` silently dropped on the DataHub path.** With the data engine enabled,
  `data_manager.enrich(..., exclude_streams=("funding","oi"))` (backtest path, `backtest.py:2493`)
  routes to `DataHub.enrich`, which has no such parameter (`hub.py:60`) and joins funding/OI anyway —
  replacing the Hyperliquid hourly `funding_rate` with Binance per-8h rates, the exact ~8× funding
  mischarge the exclusion prevents. Latent only because the engine defaults off.
- **A4 — CSV upload parses on the event loop.** The only async routes (`routers/data.py:295,301`)
  call synchronous pandas parsing inline; a 50 MiB upload blocks the loop (and the live WS) for the
  whole parse.
- **A5 — Unlocked read-modify-write writers.** `revisions.append_revision` (`revisions.py:127`) and
  `StreamManager.flush_closed_candles` (`stream.py:71`) do load→concat→save with no dataset lock;
  concurrent writers lose rows (last `os.replace` wins).
- **A6 — Missing enrichment file is indistinguishable from zero.** Funding/OI/order-flow joins fill
  `0.0` when the parquet is absent (`data_manager.py:1937`, `hub.py:424`). A strategy reads
  `open_interest=0` as signal instead of "no data". (Root cause of past "trained on funding=0" bugs.)
- **A7 — Sim data pump.** `fetch_candles` sim branch returns any non-empty cached frame with no
  coverage check (`scanner.py:1671`) → silently truncated sim windows; `prefetch_candles` runs
  its SQLite writes on the event loop (`data_pump.py:85-106`); fresh `sqlite3.connect` per read.
- **A8 — `save_parquet` skips fsync.** `data_manager._save_stream_parquet` fsyncs before rename;
  `data.save_parquet` (the OHLCV lake!) does not (`data.py:453-473`). Power loss can leave a
  truncated lake file.

### B. The structural bottleneck: whole-file rewrite storage

Every append — keep-alive, catch-up, backfill, CSV merge — loads the entire series and rewrites the
entire parquet (`save_parquet`, `_combine_and_save`). Downstream symptoms that are all really *this*
bug: the catch-up cadence forced from 10m→30m ("~4 min CPU for ~20 bars", `scheduler.py:3222`), the
keep-alive clamped to 8 pairs / 150 s, the single-worker WebSocket starvation incidents, and the
practical cap on 1m history depth. Fix the storage engine and the whole class of symptoms goes away.

### C. Two engines, half-wired (drift risk)

- Legacy (`data.py` + `data_manager.py`) and the default-off `dataeng` hub coexist with
  silent-fallback-on-exception in both `load_parquet` and `enrich` — a masked-failure and
  divergence machine (`hub.py:75-109` swallows everything).
- A large fraction of `dataeng` is scaffolding with **no production caller**: `validation.py`,
  `microstructure.py` (writes), `stream.py`, `onchain.py`, `derivatives.py`, `registry.py`,
  `errors.StaleData/PartialData/ReadStatus`. Advertised capabilities (cross-source validation,
  tick capture, streaming, multi-venue OI) do not run.
- Circuit breaker covers only the derivatives fetch (`data_manager.py:695`), not the main candle
  path; breakers key on source only, so one symbol's failure can latch every stream "Down"
  (documented incident at `data_manager.py:708`).
- Failover is inert: every stream's priority list is `["binance"]` (`settings.py:12-22`).

### D. Operational / observability gaps

- In-memory job state lost on restart: `_ingestion_runs` (unbounded, `data.py:1194`), backfill
  state (`api_domains/data.py:1880`), catch-up stall cooldowns, quality cache. Frontend literally
  ships a "backend restarted, your download was lost — click again" recovery path (`data.ts:137`).
- Dead endpoints the frontend calls forever: `/data/quality/reports/{symbol}/{tf}` and
  `/data/versions` (always 404 → silent client-side fallback; server-side versioning does not exist).
- Hot-path inefficiencies: `get_data_ingestion_run` linear-scans up to 10k runs per 1.5 s poll
  (`data.py:628`); `get_active_symbols_with_reasons` is N+1 SQLite (`data.py:1856`);
  `/api/data/coverage` walks the entire lake tree per call (`data.py:1947`); `compute_data_quality`
  and `dataset_ohlcv` full-load series; `get_dataset_detail` MD5-hashes the whole file per view.
- Alerting: data-integrity alerts (stale candles for a live bot!) are recorded but never emitted to
  notifications (`health_monitor.py:960-1017`); `check_candle_freshness` queries a SQLite `ohlcv`
  table that is not the parquet lake (`health_monitor.py:1178`) — verify it isn't dead;
  `data_collector` auto-recovery is an explicit stub.
- Scan-frame depth: signal-matrix scans use flat `bars=300` regardless of a strategy's warmup needs
  (`scanner.py:6804,6913`), while the kernel path uses ~1500 — the two paths can disagree.

### E. Completeness gaps

- Coverage = calendar span, not bar completeness (`coverage.py:83`): an old-but-gappy series reads
  as "enough" and skips backfill.
- No orderbook/trades capture (scaffold only); liquidations collector points at a dead/auth-gated
  REST endpoint; BTC dominance is snapshot-only (no history); LSR/taker hardcode 1h period.
- Binance Vision fills pre-history only (keyed on oldest bar) — never interior gaps.

---

## The Plan

### Phase 0 — Correctness fixes (small diffs, do first)

| # | Fix | Notes |
|---|-----|-------|
| 0.1 | A1: point dry-run validation at a real API (`load_parquet` → tail, fallback `fetch_candles`); make the except-path log at ERROR and count in telemetry | add a test that fails if the method is missing |
| 0.2 | A3: add `exclude_streams` to `DataHub.enrich` + thread through; extend the funding-mischarge parity test to the engine-on path | blocks ever flipping the engine flag |
| 0.3 | A4: offload CSV parse (`run_in_threadpool`) | 2-line fix |
| 0.4 | A5: take the dataset/stream lock in `revisions.append_revision` and stream flush | reuse `_get_dataset_lock` pattern |
| 0.5 | A6: stop filling absent-file joins with 0.0 — leave NaN + emit a `*_available` flag column (or per-frame `attrs`), and log once per series | strategies already advised to guard NaN in DATA_SCHEMA.md |
| 0.6 | A8: fsync in `save_parquet` (match `_save_stream_parquet`) | |
| 0.7 | A7: sim pump — coverage check mirroring the live `cache_covers` gate; move SQLite work off the loop; reuse one connection | |
| 0.8 | Bound + persist `_ingestion_runs` (see 3.3 for the full job store; interim: cap + evict terminal runs) | stops the unbounded dict + stale `max_available` short-circuit |

### Phase 1 — Market semantics: one declared venue per series (A2)

Decision to make first: **canonical market for crypto series = Binance USD-M futures** (recommended —
it matches BV backfill, funding/OI streams, and the HL-perp trading venue; the papertrading-parity
work already locked HL perps as the execution model). Then:

1. Switch the live/keep-alive candle fetch for crypto pairs to `binanceusdm` klines so tail and
   backfill are the same market. Audit every fetcher (`fetch_ohlcv_chunked`, `fetch_market_candles`,
   scanner direct fetch) and stamp `forven_market` in parquet metadata alongside `forven_source`.
2. One-time reconciliation scan: for each existing series, locate the spot/futures splice (BV months
   vs REST tail), quantify close divergence with `reconcile_close_prices`, and re-download the spot
   segment from BV futures where divergence is material. Report before/after; this is a
   **re-baseline trigger** for backtest metrics.
3. Enforce identity: `parquet_path`/hub reads must respect (or at least record) market; reject
   writes whose `forven_market` metadata disagrees with the existing file.

### Phase 2 — Storage engine: kill the whole-file rewrite (B)

Hot/cold split per series, behind the existing `load_parquet`/`save_parquet` chokepoints:

- **Cold**: immutable monthly partition files `data/ohlcv/{SYMBOL}/{tf}/{YYYY-MM}.parquet`
  (zstd, footer stats intact so coverage stays footer-only).
- **Hot**: one small tail file taking all appends. New `append_bars(symbol, tf, frame)` API writes
  only the tail (O(new bars)); collectors stop doing read-merge-write.
- **Compaction**: daily (or tail-size-triggered) roll of hot→cold, under the dataset lock, atomic.
- **Reads**: pyarrow dataset / DuckDB glob over `{dir}/*.parquet` + tail — both already dependencies;
  `load_parquet` signature unchanged. Footer-based `dataset_last_timestamp_ms` reads only the tail.
- **Migration**: `scripts/` one-shot converter with dry-run + row-count/checksum verification;
  dual-read (new layout if directory exists, else legacy file) so migration is per-series and
  reversible. Legacy single-file stays supported for small/equity series if not migrated.
- **Invariants preserved at the new write boundary**: closed-bar-only, OHLC sanity, revision capture
  (revisions only fire on restatements, which now only touch the tail/compaction path), fsync+rename.

Payoff: keep-alive cost per pair drops from O(series) to O(1 bar); catch-up cadence can return to
10 min; the 8-pair cap and 150 s clamps become unnecessary; 1m histories stop being scary.
Apply the same `append+compact` pattern to the stream parquets (funding/OI/derivatives) — they are
smaller but use the identical rewrite pattern.

### Phase 3 — One engine, explicit reliability (C)

1. **Consolidate to a single path.** Keep and finish: `identity`, `catalog` (DuckDB), `coverage`,
   `catchup`, `revisions`, `settings`, `hub` as the read façade (it gains the partitioned-dataset
   reads from Phase 2 naturally). After 0.2 lands and engine-on parity runs in CI, flip
   `data_engine.enabled` default ON, then delete the legacy duplicate read/enrich bodies.
2. **Delete the unwired scaffolding** (lean stance): `validation.py`, `microstructure.py`,
   `stream.py`, `onchain.py`, `derivatives.py`, `registry.py`, unused error types. They live in git
   history; resurrect any of them only when a real caller exists (see Phase 5 decisions).
3. **Fallbacks fail loud.** Replace the silent hub→legacy fallback with: log ERROR + telemetry
   counter + degrade. A causality-relevant divergence between engines must never be silent.
4. **Breakers everywhere, keyed (source, stream).** Wrap the candle path (`fetch_ohlcv_chunked`)
   in the registry/breaker; keep NoData benign. One symbol's OI failure must not latch funding Down.
5. **Failover stays operator-explicit** (matches the no-auto-fallback stance): `source_priority`
   is already in settings — surface it in Settings → Data, default single-source fail-closed.

### Phase 4 — Jobs, observability, operator UX (D)

1. **Persistent jobs store** (one SQLite table): ingestion runs, BV backfill, engine catch-up —
   status/progress/cancel token/started_by; survives restart. Replace `_ingestion_runs`,
   `_backfill_state`, `_catchup_stalled`. Frontend drops the "backend restarted" apology path;
   `get_data_ingestion_run` becomes a keyed lookup.
2. **Progress + cancel** on BV backfill and catch-up (per-symbol progress rows; cooperative cancel
   between tasks). UI: wire batch size to the existing `max_tasks` param; add cancel buttons.
3. **Alerting**: emit notifications for data-integrity alerts (stale candles for live/paper bots =
   page-worthy); fix or delete `check_candle_freshness`'s SQLite `ohlcv` read (point it at the lake
   footer/KV snapshot); implement the `data_collector` auto-recovery stub (trigger a keep-alive run).
4. **Kill dead surface**: implement `/data/quality/reports/{symbol}/{tf}` (cheap: serve from the
   cached leaderboard) and remove `/data/versions` + the client fallback, or back it with the
   revisions log (which IS real versioning — expose restatement counts per series).
5. **Perf touch-ups**: N+1 in active-symbols (one GROUP BY query), `/api/data/coverage` served from
   the DuckDB catalog instead of a tree walk, `dataset_ohlcv` tail-read via DuckDB LIMIT,
   checksum on demand only, remote-mode quality reports (currently `[]` by design — a visibility
   hole).
6. **Scan-depth correctness**: derive per-strategy fetch depth from indicator warmup (the kernel
   path already uses ~1500) so the signal-matrix and kernel paths can't disagree.

### Phase 5 — Completeness (each item is an explicit decision, not default scope)

- **Bar-completeness coverage**: redefine `coverage_days` readiness as bars-present / bars-expected
  (footer row count vs window/timeframe), and let the catch-up planner emit interior-gap tasks
  (BV monthly files can fill interior gaps, not just pre-history).
- **Liquidations**: current REST endpoint is dead. Options: Binance WS `forceOrder` forward-only
  aggregation, or drop the stream. Decide; don't leave a dead collector.
- **BTC dominance**: backfill history once (CoinGecko range endpoint) or accept snapshot-only and
  label it as such in the UI.
- **LSR/taker period**: parameterize the hardcoded 1h if any strategy wants faster buckets.
- **Trades/orderbook (tick capture)**: only if a strategy will consume CVD/depth. If yes: WS feeder,
  idempotent date-partitioned writes (fix the `part-{ns}` duplicate-on-reingest design), dedup on
  read. If no: stays deleted per Phase 3.2.

### Sequencing & effort

```
Phase 0  (S, ~1-2 sessions)   → immediately; independent items, all testable
Phase 1  (M)                  → decision + fetch-path switch + reconciliation scan; re-baseline after
Phase 2  (L, the big one)     → storage engine + migration; unlocks cadence/limits rollbacks
Phase 3  (M)                  → consolidation + breakers; depends on 0.2, benefits from 2
Phase 4  (M)                  → jobs store first (3.3 depends on nothing), then UX/alerts
Phase 5  (S-M each)           → pick per decision
```

Verification gates: engine-on parity suite in CI (candles/enrich/quality frame-equal), causality
tests extended to the partitioned layout, migration dry-run row-count+range equality, and a WS-liveness
smoke (keep-alive + catch-up running while the live WebSocket stays connected). Note the standing
re-baseline obligation: Phase 1's market canonicalization changes historical bars, so paper
re-baseline (already pending from the parity work) should follow it, not precede it.
