# Data Manager — Next-Level Review & Plan (2026-09-28)

Full review of the market-data layer as it stands on `main` @ `034b8a53`: backend (`forven/data.py`,
`forven/data_manager.py`, `forven/dataeng/*`, `forven/api_domains/data.py`, `forven/routers/data.py`,
`forven/data_provenance.py`, scheduler data jobs, health-monitor data checks), the `/data` page and its
nine components, the `lib/api/data.ts` client, Settings → Data, **and the live install** (read-only:
`~/.forven/data` lake census, `forven.db` via `mode=ro`, timed GETs against the running backend).

Every finding below is tagged **[V]** verified (evidence inline) or **[P]** proposed.

---

## 0. Status of the July plan (`docs/data-manager-overhaul.md`) — don't re-propose landed work

| July phase | State today |
|---|---|
| 0 Correctness (dry-run guard, `exclude_streams`, CSV offload, writer locks, fsync) | Shipped |
| 1 Market semantics (perp-canonical fetch, `forven_market` stamp, reconcile script) | Shipped; reconcile applied (106 `*.spotmix.bak` files dated 07-02 remain) |
| 2 Storage engine | **Half**: tail-append sidecar shipped (69 `.tail` files); monthly partitions never built |
| 3 One engine | **Not finished**: live install runs `data_engine.enabled=true`, new installs default `false`; both read paths still live in `load_parquet`/`enrich` |
| 4 Jobs/observability | **Partial**: KV-persisted per-system state (not one job store); dead endpoints now exist; CRITICAL checks page for *running bots* only |
| 5 Completeness | Completeness-aware "gaps" tasks shipped; liquidations WS live (OKX, `connected: true`) |

---

## 1. Executive summary

The Data Manager *works* and has genuinely strong foundations — closed-bar write invariant,
atomic fsync+rename, OHLC quarantine, shrink guard, perp-canonical fetch, point-in-time revision
log, demand-driven coverage, a real quality gate. What keeps it from being industry-leading is
not missing features; it is **trust**:

1. **The lake can be silently corrupted from the Download form** (any non-Binance exchange
   overwrites the canonical Binance-perp series and relabels it) and **CSV imports ignore the
   file's timeframe**. [V]
2. **A third of stored series are stale by the gauntlet's own rule, structurally** — collection
   capacity is ~80 refreshes/h where ~120/h is needed, priority starves intraday, each refresh
   costs several full-series loads, and dead symbols are retried forever. [V]
3. **The UI can't be trusted to say so**: six different freshness definitions, three quality
   scores, a leaderboard blind to 72% of series (including every worst one), an activity log
   drowned 99% in routine lines, and a `/data` nav badge that is never populated. [V]
4. **Operations are fragmented**: five job systems with different state, progress and cancel
   semantics — and single downloads can't actually be cancelled server-side. [V]

The plan: **integrity first** (Phase 0, one session), then **one freshness SLA + one collection
queue** (Phase 1), **one identity/catalog/engine** (Phase 2), **one job model** (Phase 3), then a
**UI rebuilt around three questions** — *Is my data OK? What needs attention and why? How do I
fix it?* (Phase 4), **performance/storage** (Phase 5), and the differentiator: **strategy data
contracts + readiness everywhere** (Phase 6). Almost every phase *deletes* a duplicate
mechanism; net code should shrink.

### Scorecard

| Property | Today (measured) | Target |
|---|---|---|
| Correct | Cross-venue overwrite + CSV timeframe bug reachable from UI; 81% of canonical-backtest data identities empty | Writes venue-scoped with hard refusal; every verdict carries a real identity |
| Fresh | 116/360 series (32%) past the gate's freshness limit, twice measured | LIVE/PAPER 100%, pipeline ≥98%, all ≥95% within one SLA registry |
| Complete | 44/180 planned research-universe series missing; seed dead since 07-06 | Plan-vs-lake diff visible, resumable, ≥99% of plan present |
| Observable | Leaderboard covers 100/360; 14.6 s cold; 6 freshness rules; nav badge empty | One server truth for freshness/quality over all series; p95 < 300 ms; badge on breach |
| Operable | 5 job systems; client-only single-download cancel; 1,985/2,000 log lines routine | One job model with real cancel/retry/origin; activity split user vs routine |

---

## 2. What is already strong (keep; protect with tests)

Closed-bar-only writes (`_drop_unclosed_bars`), atomic tmp→fsync→`os.replace`, OHLC sanity
quarantine, `_guard_series_shrink` (HARDEN-DATA-OPS), synthetic-range stamping for forward-filled
bars, perp-canonical `_resolve_ohlcv_target`, tail-append fast path for incremental fetches,
footer-only freshness reads, per-exchange candle breaker, append-only revision log + `as_of`,
DATA-PROV-1 semantic fingerprints on validation artifacts, the gauntlet data gate
(`dataeng/quality_gate.py`), incremental catalog `scan_lake` fingerprinting, the data-availability
precheck (`strategies/data_availability.py`), OKX liquidation capture, Kraken trades-built history.

---

## 3. Verified findings (severity-ranked)

### P0 — integrity

**F1 [V] Non-Binance downloads overwrite the canonical Binance-perp series, silently.**
The Download form offers `binance, bybit, okx, coinbase, kraken`
(`DataInspector.svelte:204`). `parquet_path()` has no venue component, so every exchange writes
`ohlcv/{SYM}/{tf}.parquet`. `merge_and_dedup` keeps the *new* bar on timestamp collisions, then
`save_parquet(..., source=exchange_id)` restamps the whole file. `_warn_market_mismatch` returns
early for unmapped exchange ids (`data.py:425`), so no MARKET SPLICE warning fires.
Hermetic repro (scratch `FORVEN_HOME`, only the exchange object faked, **unmodified code paths**):
- OKX (same path as bybit/coinbase): before `binanceusdm/perp close=100` → after `okx/unknown
  close=200`, **199/199 bars replaced**.
- Kraken, recent window (OHLC endpoint): identical outcome — **150/150 seeded bars replaced**,
  file restamped `kraken/unknown`.
- Kraken, "All available" (trades-built): existing bars are **kept** (it fills around the
  snapshot), but Kraken bars are spliced onto the head and/or tail (tail demonstrated; the code
  fills both windows) and the **whole file is relabelled** `kraken/unknown` — a mixed-venue
  series whose stamp misdescribes most of its history.
Blast radius: every backtest/gauntlet run on that series; the promotion gate's source
reconciliation reads the stamp. Cold-file stamps in the live lake today are only `binanceusdm`
(333) and `binance` (27) — no evidence the hole has been hit yet.

**F2 [V] CSV import stores every file as `1d` and never shows its preview.**
`csvTimeframe = '1d'` is never bound to an input (`DataInspector.svelte:201`); the form has only
file + symbol. `csvPreview` is fetched (`:367`) and never rendered. `process_csv_upload`
(`data.py:3073`) never checks the file's cadence against the declared timeframe, so a 1h CSV
merges into an existing `1d` series, and — because the upload keeps the stored provenance — the
result is still stamped `binanceusdm/perp`.

**F3 [V] Two provenance systems collide on one config key (latent).**
`api_core.py:6677` and `:7794` write `config["data_fingerprint"] = dataset_fingerprint(...)` (a
dict). `stamp_data_fingerprint` preserves any existing key (`data_provenance.py:154`), so the
DATA-PROV-1 semantic hash is never stamped on canonical backtests. Live DB: of the last 3,000
`backtest`-type rows, 2,967 carry the dict and **2,395 (81%) are empty** (`checksum: None,
row_count: 0`) because `dataset_fingerprint("BTC","1h")` resolves the bare-asset path
`ohlcv/BTC/`, not `BTC-USDT` (same class as the July funding-dir bug); across the last 3,000 rows
of all types, 2,691 are such rows and none carries `data_fingerprint_detail`. Not active breakage today — `_extract_gauntlet_verdict_payloads` only reads the five
validation types, which carry the string stamp — but `artifact_data_fingerprint` returns
`str(dict)` (`data_provenance.py:176`), so the day anything checks canonical backtests, 2,691
rows read as explicit mismatches.

**F4 [V] Point-in-time reads silently fall back to latest.** On the live (DataHub) path a
`reconstruct_as_of` failure propagates out of `hub.candles`, `load_parquet` logs one WARNING and
falls back to the legacy read, whose reconstruction failure is swallowed at DEBUG
(`data.py:750`) — the caller receives latest values. `gauntlet_as_of_pin` defaults ON.

**F5 [V] Delete is hard, unguarded, and un-undoable.** `delete_dataset` (`data.py:2510`)
unlinks cold+tail with no check for running bots' locked pairs, paper/live strategies or open
gauntlet workflows on the series; the UI guard is a native `confirm()` (`DataPanel.svelte:99`).
Active symbols are then re-bootstrapped by the catch-up planner, so "delete" may not even stick.

**F6 [V] Dishonest counters/payloads.** `gaps_filled` increments per fetch call, not per gap
that landed (`data.py:1245`) — the live log shows "MATIC-USDT 4h: +0 bars (3/3 gaps filled, 3
remaining)" every run. Remote-mode fetch returns fabricated numbers (`start_ts
2015-01-01`, 50,000 bars; `api_domains/data.py:684`).

### P1 — freshness & completeness

**F7 [V] 116 of 360 series (32%) fail the gauntlet freshness rule — structurally.** Census of
cold+tail footers at 18:13 and 18:25 UTC: 116 both times (5m 32–36, 15m 29, 1h 29–33, 1m 15,
30m 4, 3 dead symbols). Honest scope: of the (symbol, timeframe) pairs used by the 17 paper and
6 live strategies, only one is failing (SOL-USDT 15m, paper); no gauntlet/quick-screen
strategies or open workflows existed at the moment of measurement (pipeline idle). The rest are
research/intraday series with no current consumer — so this is a **latent gate-blocker for newly
generated strategies** and a **UI trust problem**, not a live-trading incident. Causes:
- *Capacity*: keep-alive 8 pairs/15 min (active set only) + catch-up `auto_catchup_batch=24`/30
  min ≈ **80 refreshes/h**. Minimum demand to hold every series inside the gate limit, from the
  census mix (87×1h every ≤2 h, 50×5m and 37×15m every ≤1.5–1.8 h, 20×1m, 80×4h every ≤8 h, …)
  is **≈120 refreshes/h** even with perfect scheduling — a ~35% structural deficit that matches
  the ~32% failure rate.
- *Priority*: the planner ranks by raw hours behind (`catchup.py:70`), so a 1d series one bar late
  (24 h) outranks a 1m series 180 bars late (3 h) — exactly the intraday-heavy failure mix.
- *Cost*: a "stale" catch-up task runs `backfill_ohlcv_gaps`, i.e. ~6 full-series loads, a full
  gap scan and a re-fetch of every known gap, when all it needs is the keep-alive's O(new bars)
  tail append. One 24-task run at 17:56 added 64 bars.
- *Dead symbols*: MULTI-USDT (last bar 951 days ago), MATIC-USDT 4h and ALT-BTC 1h are retried
  every ~90 min (activity log 13:08, 14:43, …) and MULTI's files were rewritten today; the symbol
  registry knows delistings, the planner doesn't ask.

**F8 [V] Six freshness definitions disagree.** Gate: max(4 bars, 2 h) (`quality_gate.py`).
Quality report: max(6 bars, 1 h) (`data.py:2822`). Coverage matrix: 2/8 bars client-side
(`CoverageMatrix.svelte`). Health monitor: warn max(3 bars, 2 h), crit max(8 bars, 6 h). Stream
health: fixed 30 min regardless of timeframe (`api_domains/data.py:1876`). Settings:
`data_engine_settings.staleness_thresholds` (no consumer) vs top-level
`forven:settings.staleness_thresholds` (read by `health_monitor.py:744`, not exposed in the
manifest, `None` in live KV). A 1h series 5 h old is yellow in the matrix, fresh in the quality
report, and **blocked** by the gate.

**F9 [V] Research universe incomplete, failure invisible.** The seed died on backend restart
07-06; the Maintenance tab has shown "Last run failed: backend restarted mid-seed" for 84 days;
**44 of 180 planned series are missing** (QNT, HBAR, LTC, TAO, ONDO, …). Nothing resumes it.
The liquidity-ranked plan now also pulls TradFi perps (NVDA, SOXS, SKHY, BZ, MARSCOIN) —
probably not intended as default research universe.

**F10 [V] Identity fragmentation in the lake.** BTC lives under four dirs (`BTC-USDT`,
`BTC-USDC`, `BTC-USD`, `BTCUSD` — the last written today 13:19, so some writer bypasses
normalization); stray `RETRY/` dir; `MULTI-USDT` from a fabricated-symbol era; `USDT-TRY`,
`USDT-USD`.

### P1 — observability & operability

**F11 [V] Quality leaderboard is blind to the worst series.** It scores the 100 *most recently
updated* series (`api_domains/data.py:932`) of 360, so 83 of the 116 gate-failing series and all
of the stalest (MULTI, MATIC, ALT-BTC) never appear under "Problems only". Cold build **14.6 s**
(100 full-series loads), rebuilt every 120 s TTL.

**F12 [V] Three quality scores.** Backend `_quality_score` = frontend legacy fallback (ratio
based); `SeriesDrillDown.scoreFrom` (`:40`) penalises absolute gaps ×2 — 10 gaps in 70k bars is
~99.9 on the leaderboard and 80 in the drill-down.

**F13 [V] Five job systems, no real cancel.** Ingestion runs (`THREAD_POOL` of 2, in-memory + KV
snapshot), Binance Vision backfill, universe seed, catch-up, keep-alive (+ manual collect). Each
has its own state, progress and UI. There is no cancel route for ingestion runs; the UI's
AbortController only stops the client poll while the backend thread keeps downloading.

**F14 [V] Activity log drowned.** Last 2,000 events span ~54 h; 1,985 are routine `backfill`
lines, so the Activity tab (limit 200) shows ~4.4 h and user actions (3 downloads) are buried.

**F15 [V] No global signal.** The `/data` nav indicator is `emptyIndicator()` and never
populated (`heartbeat.ts:50`). Candle-freshness paging covers running bots' locked pairs only.

**F16 [V] Dead or misleading settings** in Settings → Data: `onchain_provider` /
`onchain_api_key` (no consumer — it asks for an API key that does nothing), `source_priority.*`
(never read; failover inert), `enabled_exchanges` (status display only), nested
`staleness_thresholds` (F8). "All Timeframes" × "All Available" queues full-history downloads for
1m…1w (`ORDERED_TIMEFRAME_VALUES`) with no size/time estimate; `datetime-local` inputs are local
time with no UTC label; Polygon is listed `available: True` without a key; "CCXT" and "Binance
Direct" are the same adapter; Hyperliquid — the execution venue — isn't offered.

### P2 — storage, performance, hygiene

**F17 [V]** 641 MB of `*.spotmix.bak` (106 files, 07-02) — +51% on a 1.26 GB lake — invisible to
the orphan scan, which only globs `*.tmp`, `*.parquet.tail`, `*.parquet` (`data.py:2570–2609`).
Revision log 357 MB / 162 files with no retention or visibility. ~30 legacy loose files/DBs in
`~/.forven/data/` (Mar–Jul research artifacts).

**F18 [V]** Every read is a full-series read: `load_parquet` never passes a window, although
`hub.candles` already supports `start`/`end`/`columns` pushdown. BTC-USDT 1m is 80 MB cold.
Timings on the live backend: `/data/quality/reports` 14.6 s cold, `/data/coverage` 1.7 s (tree
walk per request), `/datasets` 0.74 s, `/data/active-symbols` 0.89 s.

**F19 [V]** `compute_checksum` hashes file bytes (MD5), and every save restamps
`forven_updated_at` metadata — so a compaction or no-op re-save changes the "identity" of
identical data. `fingerprints_match` has no callers. Any reproducibility feature must hash
values, not bytes.

**F20 [V] Frontend debt.** No stale-response guard on `loadQuality`/`loadStreams` (rapid clicks
can show series A's quality under series B); drill-down is a modal with no focus management;
native `alert`/`confirm`; legacy fallback builders in `data.ts` for endpoints that now exist
(`getDataHealth`, `getIngestionRuns`, `getDatasetVersions`, `getQualityReports`); Inspector and
drill-down show 3 streams of the ~9 the lake holds (basis, IV, LSR, taker, liquidations missing);
drill-down charts only the last 300 bars; Polygon routes carry logic in `routers/data.py`
(convention breach); one data-page test file (`dataCollectionStatus.test.ts`).

---

## 4. The plan

Principles: **one truth per concept** (identity, freshness SLA, quality score, job state);
**integrity fails closed**; **honest UI** (no fabricated numbers, no dead settings); **lean**
(each phase lists what it deletes); **no silent cross-venue fallback** (perp→spot for no-perp
bases stays the only one); **full editability** (new knobs get a manifest entry *and* the
`_apply_settings_section` whitelist); **re-baseline called out per phase**.

### Phase 0 — Integrity (S · 1 session · re-baseline: no)

| # | Change | Tests / acceptance |
|---|---|---|
| 0.1 | **Venue-scoped writes with hard refusal.** Canonical `ohlcv/{SYM}/{tf}` accepts only the canonical family (`binanceusdm`, `binance-vision`, `binance` spot fallback for no-perp bases). Any other venue writes `source={ex}/market={m}/{SYM}/{tf}` via the existing `save_venue_frame`. A venue may own a canonical path only when Binance lists neither perp nor spot for the pair. Replace `_warn_market_mismatch` with `_enforce_series_venue` → `LakeVenueRefused`. Download form labels "canonical research series" vs "separate venue series". | Port the three F1 repros (OKX, Kraken OHLC, Kraken trades-built): canonical file unchanged, venue file created, stamp intact. |
| 0.2 | **CSV correctness.** Bind a timeframe selector; backend infers cadence (median Δ) and alignment and rejects/offers the inferred tf; render the preview (columns, mapping, parsed first/last ts in UTC, inferred tf); **overlap diff** (new / identical / conflicting bars) with policy *add-only* (default) / *overwrite* / *cancel*; patching an existing exchange series requires explicit patch mode and stamps `forven_csv_ranges` (same mechanism as `forven_synthetic_ranges`). | 1h file declared 1d → 400 with inferred tf; conflict diff counts; ranges stamped and preserved across compaction. |
| 0.3 | **Provenance collision.** Rename api_core's dict to `data_identity`, resolve `canonical_market_symbol` first; `artifact_data_fingerprint` treats non-string values as unstamped (grandfathers the 2,691 rows). | Canonical backtests carry both `data_fingerprint` (str) and `data_identity` (non-empty); dict rows never read stale. |
| 0.4 | **Point-in-time fails loud.** `as_of` reconstruction failure raises (or returns with `attrs["as_of_degraded"]` stamped into the result); never silently latest. | Forced reconstruction error → run fails or is visibly degraded. |
| 0.5 | **Delete safety.** Dependency check (bots' locked pairs, paper/live strategies, open gauntlet workflows) → typed confirm listing consumers; move to `data/.trash/` with 7-day retention + restore; warn when the series will be re-bootstrapped. | Delete of a paper strategy's series is blocked without override; restore round-trips bytes. |
| 0.6 | **Honest numbers.** `gaps_filled` = gaps whose bars landed; remote-mode fetch returns the remote's real response or `status: submitted`. | Unit tests on both. |

**Deletes:** `_warn_market_mismatch` soft path; hidden `csvTimeframe` default.

### Phase 1 — One freshness SLA, one collection queue (M · 2 sessions · re-baseline: no)

1. **SLA registry** `forven/dataeng/sla.py`: `allowed_lag(stream, timeframe, tier)` and
   `classify(series) → fresh | due | late | breach | frozen`. Tiers: **LIVE, PAPER, PIPELINE**
   (open gauntlet/QS workflows), **UNIVERSE, IDLE, FROZEN**. The gate keeps its scoring contract
   (max(4 bars, 2 h)) as the PIPELINE threshold; LIVE/PAPER are tighter, UNIVERSE looser. All
   thresholds editable in Settings → Data.
   *Replaces:* `_freshness_for`, CoverageMatrix client thresholds, `_STREAM_CADENCES`,
   health-monitor candle thresholds, both `staleness_thresholds`, `candles_minutes`.
2. **Tier derivation** `series_consumers(symbol, tf)`: strategies by stage + bots' locked pairs +
   open workflows + universe plan + registry status → cached on the catalog row.
3. **One SLA-driven collector** replacing keep-alive (8/15 min) and catch-up (24/30 min): a
   continuous tick drains a priority queue ordered by `(age / allowed_lag) × tier_weight`, bounded
   by a per-venue request-weight budget (not a task count) and a wall-clock deadline.
4. **Cheap tail refresh**: stale tasks = footer cursor → `fetch_ohlcv_chunked(since_ms)` → tail
   append (the keep-alive path). `backfill_ohlcv_gaps` only for *gap* tasks, with a persisted
   **unfillable-gap memo** (venue retention/downtime) so known holes are never re-fetched.
5. **FROZEN state**: registry-delisted/renamed symbols and N consecutive "no newer data" → frozen
   with reason (history kept, never scheduled, grey in UI, user can unfreeze).
6. **Measurement instrument**: productize this review's footer census as `GET /api/data/sla`
   (per tier × tf: counts by state, p50/p95 lag) and `python -m forven.agent data-census`.

**Acceptance:** LIVE/PAPER 100% within SLA over 24 h; PIPELINE ≥98%; all series ≥95% (today 68%);
zero FROZEN series scheduled; catch-up CPU per added bar down ≥10×.
**Deletes:** two scheduler jobs → one; `_catchup_stalled`, `_keepalive_last_checked`, five
threshold definitions, `auto_catchup_batch`, `max_pairs_per_run`.

### Phase 2 — One identity, one catalog, one engine (M–L · 2–3 sessions · re-baseline: **yes** if 2.1 changes a canonical read)

1. **Instrument identity**: extend `dataeng/identity.py` to `venue:market:BASE-QUOTE`
   (`binanceusdm:perp:BTC-USDT`) with an alias table (BTC, BTCUSD, BTC/USD…); every data
   endpoint validates through it (SYMBOL-VALID-1 exists at mint only). Investigate which writer
   produced `BTCUSD` today; decide per stray dir (F10) whether it is a distinct instrument
   (USDC-margined perp is) or an alias to merge/retire — with a dry-run report first.
2. **Catalog as the single index**: DuckDB `series_coverage` gains identity, tier, SLA state,
   consumers, quality score, completeness, provenance, size, last job — **written through** by
   append/save/delete instead of periodic full rescans. `/datasets`, `/data/coverage`, quality
   reports read only the catalog (no tree walks, no series loads).
3. **Universe completeness**: plan-vs-lake diff on the page ("44 planned series missing —
   Download"), the seed becomes a resumable Phase-3 job with opt-in auto-resume (default stays
   manual), and a universe asset-class policy (crypto-only default; TradFi perps opt-in).
   Survivorship: the registry knows 6 delistings of 784 — backfill historical delists from the
   Binance Vision listing.
4. **Single engine**: the live install has read through DataHub for months — make it the only
   path after a CI parity run; delete the legacy read/enrich bodies, the silent hub→legacy
   fallbacks and the `data_engine.enabled` flag. (New installs currently run the *other* path.)
5. **Venue-aware backtests** (deferred since July): per-run data source selection so a venue
   series written under 0.1 (Kraken, OKX, HL) is usable for research on purpose.

### Phase 3 — One job model, honest activity (M · 1–2 sessions · re-baseline: no)

1. **`data_jobs` table + one runner.** Kinds: `tail_refresh, gap_repair, download,
   history_extend, universe_seed, csv_import, stream_collect, compaction, reclaim`. Origin:
   `user | sla | strategy:S#### | universe`. State machine, progress (units/bars/bytes/ETA),
   **cooperative cancel honoured at page boundaries**, retries with an error taxonomy
   (`rate_limited, venue_down, unknown_symbol, delisted, corrupt, disk_full`).
   *Replaces:* `_ingestion_runs` + KV snapshot, `_backfill_state`, `_universe_state`, manual-collect
   debounce, the frontend's per-download polling loop.
2. **Concurrency**: `THREAD_POOL(max_workers=2)` → per-venue slots sharing the Phase-1 budget;
   free-disk check before large jobs.
3. **Activity v2**: "Your actions & incidents" vs "Routine" (one row per job run, expandable);
   filters (level, symbol, origin, date), retention, export.
4. **Alerts**: page on LIVE/PAPER SLA breach (not only running bots), source down > N min, job
   failure after retries, disk low.

### Phase 4 — UI rebuilt around three questions (L · 3–4 sessions · re-baseline: no)

Information architecture: **Health** (default) · **Catalog** · **Coverage** · **Jobs** ·
**Storage**, plus a deep-linkable **series page** `/data/series/[symbol]/[tf]`, a global **Jobs
drawer**, and a first-run **setup** flow. Every colour/score comes from the server (Phases 1–2).

| Component today | Problem (verified) | Replacement | The 3-second question |
|---|---|---|---|
| Overview tiles (`+page.svelte`) | Counts, no statement of health | **Health hero**: "Live & paper data ✅ within SLA" or "⚠ 2 series late → affects S10869"; tier cards (Live/Paper/Pipeline/Universe) with fresh/late/breach; **Fix all**; drives the `/data` nav badge | Is my data OK right now? |
| `SourceHealth` | Reliability = consecutive failures only (it says so itself) | Sources strip in Health: per-venue status, rate budget, last error, *what it affects* | Is a source down, and what breaks? |
| `CoverageMatrix` | Client thresholds contradict the gate; 3 streams; cell click = hidden write; delisted red forever; no search | Server SLA state per cell, tier grouping, search/filter, frozen grey, all streams, cell → series page, explicit "Repair selected / Fix all late" | Where are the holes? |
| `QualityLeaderboard` | 100/360 series, misses the worst; 14.6 s | Catalog-backed, all series, reasons first, one documented score | What's broken and why? |
| `DataPanel` (list) | Only rows+source; native confirm/alert; no sort/filter/bulk | **Catalog table**: virtualized; identity chip, tf, span, completeness, freshness chip, quality, consumers, size, updated; filters + saved views; multi-select bulk (refresh, repair, extend, export, delete); keyboard nav | What do I have, and is it good? |
| `DataInspector` (details + fetch form) | 3 streams; BV buttons duplicate Maintenance; venue-corrupting exchange list; 1m-inclusive "All Timeframes" with no estimate; local-time inputs; CSV hidden `1d` | Details → series page. Fetch → **Get-data wizard**: intent first ("BTC 15m, 3 years" / "a universe" / "import a file"), shows what's already stored, estimate (bars, MB, time), canonical-vs-venue explained, UTC-labelled ranges, progress in the Jobs drawer | How do I get exactly what I need? |
| `SeriesDrillDown` (modal) | Last 300 bars; own score; 3 streams; no provenance | **Series page**: coverage timeline (gap map over full history incl. synthetic / CSV-patched / restated ranges), downsampled full-history chart with zoom to raw bars, all enrichment streams on one axis, provenance + lineage (jobs that wrote it, revisions, identity), consumers with gate verdict per consumer window, actions (refresh / repair / extend / export range / delete with deps) | Can I trust this series for this strategy? |
| CSV path | Timeframe hidden, preview unrendered | **Import wizard**: preview table, column mapping, timezone/format, inferred tf, overlap diff + conflict policy, dry-run then commit | Will this file land where and how I think? |
| Maintenance "Downloads & Coverage" | Seed / BV extend / fill-gaps jargon; red "failed" since 07-06; overlaps automation | **Coverage goals**: universe config + plan diff + one "Bring universe up to plan"; history extension as series/bulk action; gap filling becomes automatic (SLA queue) | Am I collecting what I intend to? |
| Data Engine Status card | Internals + "disabled (optional)" banner | Removed (single engine); sources move to Health | — |
| `StorageMaintenance` | Blind to 641 MB `.bak`, 357 MB revisions, legacy root files | **Storage**: inventory by stream/symbol/tf, reclaimable space with previews + confirm, retention policies, compaction status, trash | Where is my disk going; what can I safely reclaim? |
| `DataActivityLog` | 200 rows ≈ 4 h, 99% routine | Activity v2 (Phase 3) | What happened, and who/what did it? |
| Settings → Data | Dead knobs (F16) | Remove dead knobs; add SLA tiers, collection budget, universe policy, retention — each shows its effect | What does this knob actually change? |
| — | Empty lake on first run | **Setup flow**: presets with real estimates (Starter: BTC/ETH/SOL 1h–1d; Research: top-25 + funding/OI; Pro: top-50 + intraday), one click → jobs | How do I get going? |

Cross-cutting: request-id guards on every selection-driven load; drawers/pages instead of modals
(focus management); a shared confirm dialog (typed confirmation for destructive actions) replacing
`alert`/`confirm`; UTC on every timestamp; skeletons and teaching empty states; glossary tooltips
(perp vs spot, canonical vs venue series, SLA tier); delete the legacy fallbacks in `data.ts`;
move Polygon logic out of `routers/data.py`; component tests for Health, Catalog, series page,
wizards.

Suggested build order inside Phase 4: Health + nav badge → Catalog → series page → Get-data and
Import wizards → Coverage v2 → Storage → Setup flow.

### Phase 5 — Performance & storage (M · 1–2 sessions · re-baseline: no, parity-tested)

1. **Windowed reads**: `load_parquet(symbol, tf, start=, end=, columns=)` passes through to the
   pushdown `hub.candles` already implements; the gate, quality and backtests read window +
   warmup only. Parity test: windowed read == full read masked.
2. **Incremental quality**: stats updated on write (appends score only the new slice), stored on
   the catalog row → leaderboard instant (today 14.6 s cold).
3. **Budgets**: every `/data` endpoint p95 < 300 ms; no request performs a full-series load.
4. **Retention**: backups expire after a verified reconcile (operator confirm); revision log keeps
   restatements intersecting any persisted verdict window, compresses/prunes the rest beyond N days.
5. **Partitioned cold storage** (the July Phase 2 remainder) for 1m/5m **only if** 1–3 miss the
   budgets — lean stance.

### Phase 6 — Data contracts & readiness: the differentiator (M · 2 sessions · re-baseline: no)

1. **Strategy data contract**: one function, `data_contract(strategy)` →
   requirements (streams, timeframe, history depth incl. warmup, venue) + status per requirement +
   one-click fix. Unifies `strategies/data_availability.py`, `quality_gate.check_series_quality`
   and the DATA_SCHEMA exposure — the gauntlet gate and the UI read the same contract.
2. **Readiness everywhere**: Strategy Creator, backtest form, series page ("consumers") and
   MCP/`forven.agent` (`get_data_readiness`) show "Needs BTC-USDT 1h ≥ 730d + funding + OI:
   ✅ / ⚠ funding 3 h late [Fetch]". This kills the data-substrate-mismatch class at authoring
   time, for humans and agents alike.
3. **Reproducible snapshots**: value-hash per month of `(ts, OHLCV)` (not file bytes — F19);
   verdicts record `(series, window, month-hashes)`; "Re-run on identical data" and a drift
   explanation when hashes differ.
4. **Research-vs-execution view**: Binance-perp vs Hyperliquid divergence per symbol on the
   series page (the source-reconciliation job already computes it).

### Considered, deferred (lean)
SQL/notebook console over the lake (DuckDB is there; build only if users ask); tick/orderbook
capture (no consumer); paid vendors (no consumer); Hyperliquid as a download venue in the wizard
(pairs naturally with 2.5 — do it then).

---

## 5. Operator decisions (listed, not done)

1. **Restart the research-universe seed** (dead since 2026-07-06; 44/180 planned series missing)
   — now, or after Phase 3 makes it a resumable job.
2. **Reclaim 641 MB** of `ohlcv/**/*.spotmix.bak` (106 files) once the 07-02 reconcile is
   accepted as final.
3. **Stray lake dirs**: `ohlcv/RETRY/`, `MULTI-USDT/` (delisted; still churned by catch-up),
   `BTCUSD/`, `BTC-USD/`, `USDT-TRY/`, `USDT-USD/` — keep, merge or retire (Phase 2.1 produces the
   report).
4. **~30 legacy files/DBs** in `~/.forven/data/` root (`funding_btc.parquet`,
   `binance_ohlcv.db`, `MATICUSDT_4h.csv`, …) — archive or delete.
5. **Universe composition**: keep TradFi perps (NVDA, SOXS, SKHY, BZ…) in the default research
   universe, or crypto-only?

## 6. Sequencing, effort, risks

```
P0 integrity ............ 1 session   ← start here; small diffs, all testable
P1 SLA + one queue ...... 2 sessions  ← biggest trust win; unblocks Health UI
P2 identity/catalog ..... 2–3         ┐ parallelizable
P3 jobs + activity ...... 1–2         ┘
P4 UI ................... 3–4         ← Health + Catalog can start once P1/P2 APIs exist
P5 perf/storage ......... 1–2
P6 contracts/readiness .. 2
```

Risks: (1) 0.1 changes where non-Binance downloads land — users who relied on "download Kraken
into the main series" need 2.5 (venue-aware backtests) to use that data on purpose; ship the UI
explanation with 0.1. (2) 2.1 identity consolidation can change canonical reads → treat as a
`BACKTEST_ENGINE_VERSION` bump with the usual re-validation sweep. (3) The SLA collector must
respect venue rate limits (Binance futures request weight) and stay off the event loop (single
worker WS starvation history). (4) Single-engine cut-over needs a CI parity run first.
(5) This checkout is shared by concurrent sessions — commit only your own files.

## 7. Evidence appendix (how to re-measure)

- **Lake census** (cold+tail footers, no full loads): per series rows, first bar, age of last
  bar, completeness, `forven_source`/`forven_market` stamps → 360 series, 1.26 GB; 116 past
  max(4 bars, 2 h) at 18:13 and 18:25 UTC. Becomes `GET /api/data/sla` (Phase 1.6).
- **Splice repro**: scratch `FORVEN_HOME`, seed 150 `binanceusdm` bars, fake exchange object
  returning overlapping bars, call `fetch_ohlcv_chunked(..., exchange_id="okx"|"kraken",
  since_ms=...)` and `(..., exchange_id="kraken", all_available=True)` with a fake trades feed.
- **Provenance tally**: `backtest_results.config_json` over the last 3,000 rows (`mode=ro`).
- **Endpoint timings**: `curl -w %{time_total}` against the running backend (GET only).
- **Universe gap**: `GET /api/data/universe` plan vs census.
