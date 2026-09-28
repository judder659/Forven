# Data Manager rebuild — build contract

The plan is `docs/data-manager-next-level-2026-09-28.md` (read §3 findings and §4 phases first).
This file is how the work is split so several people can build it in parallel without
colliding. **Wire shapes are `frontend/src/lib/api/dataManagerTypes.ts` — that file is the
API contract.** If a shape there is wrong or missing, say so in your report; do not invent a
different shape.

Integration branch: `feat/data-manager-next` (worktree `.claude/worktrees/dm-next`).
Each workstream has its own branch and worktree cut from the foundation commit. The new UI is
a separate test page at **`/data-next`**; the existing `/data` page must keep working
unchanged until the user approves the new one.

---

## 0. Safety rules (non-negotiable)

The live trading backend (port 8003) runs on this machine from the main checkout
`C:\Users\Aaron\Projects\forven`, with real money. Its data lives in `C:\Users\Aaron\.forven`.

1. Work **only** inside your own worktree directory. Run every shell command with that directory
   as cwd (`cd <your worktree> && ...`). Never edit files in the main checkout or in another
   workstream's worktree. Never run `git push`, `git stash`, `git checkout` of other branches,
   or anything that touches another worktree.
2. Any Python process that imports `forven` must run with a scratch home and mainnet unarmed:
   `FORVEN_HOME=<your scratch dir> FORVEN_ALLOW_MAINNET=` (empty). Never let code write under
   `C:\Users\Aaron\.forven`.
3. Against the live backend (`http://127.0.0.1:8003`) you may send **GET requests only**, to look
   at real payloads. Never POST/PUT/DELETE there. Never open the live `forven.db` except with
   `sqlite3.connect("file:...forven.db?mode=ro", uri=True)`.
4. Tests: run **only** your own new test files plus the specific existing test files your change
   affects. Never the full suite (the integrator runs it once). Use
   `-p no:cacheprovider -p noeditable` and at most `-n 2`. The command:
   ```
   cd <worktree> && FORVEN_HOME=<scratch> FORVEN_ALLOW_MAINNET= \
     PYTHONPATH=C:/Users/Aaron/AppData/Local/Temp/claude/C--Users-Aaron-Projects-forven/8bda547a-188e-46e6-a378-d5d3d215a00b/scratchpad/pyplug \
     C:/Users/Aaron/Projects/forven/.venv/Scripts/python.exe -m pytest tests/<files> -q -p no:cacheprovider -p noeditable
   ```
   Verify imports resolve to your worktree once: `python -c "import forven; print(forven.__file__)"`.
5. Frontend: `npx vitest run <your test files>` and `npm run check` are fine; **never `npm run
   build`**. Do not start dev servers on ports 5173, 5174 or 8003.
6. No network calls to exchanges from tests (mock ccxt/HTTP). Small manual probes of public
   market endpoints are allowed but keep them rare — the live system shares this IP's rate limits.
7. Do not delete anything in the real lake, restart anything, or change live settings. Storage
   reclaim, restarting the universe seed, and cleaning stray folders are **the operator's
   decisions**: build the actions, never perform them.
8. Commit early and often on your own branch (conventional-commit messages, ending with
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`). Do not amend or rebase shared history.

---

## 1. Foundation (already on the branch — build on it, don't reinvent it)

| Module | What it gives you |
|---|---|
| `forven/dataeng/sla.py` | The one freshness definition. `assess(last_bar_open, timeframe, tier, frozen=, now=, policy=) -> SlaAssessment`, `classify`, `priority`, `allowed_lag_seconds`, `gate_allowed_staleness_hours`, `load_policy()` (30 s cache), `timeframe_seconds`. Tiers `live > paper > pipeline > universe > idle`; lag is measured from the last bar's OPEN time; `allowed = max((missed_bars+1) x tf, floor)`. The pipeline tier equals the gauntlet gate's current rule. |
| `forven/dataeng/consumers.py` | `get_consumer_index()` (60 s cache) → `.for_series(symbol, timeframe) -> SeriesConsumers` (tier, strategies, bots, workflows, universe_rank, keepalive, delisted), `.symbol_tier(symbol)` for timeframe-less streams, `fs_symbol(any spelling)`. |
| `forven/dataeng/jobs.py` | The one job store (`data_jobs` table). `submit(kind, runner, title=, params=, series=, origin=, lane=, routine=, dedupe_key=)`, `register_runner(kind, factory)` + `submit_registered(...)` (retryable kinds), `cancel_job`, `retry_job`, `get_job`, `list_jobs`, `jobs_summary`, `record_routine`, `recover_interrupted` (API startup only), `prune_jobs`, `classify_error`, `check_free_disk`, `JobContext` (`check_cancel()`, `progress(done, total, unit=, message=)`, `set_total`, `note`), `JobCancelled`, `DiskSpaceError`. Lanes: one thread pool per venue (`LANE_WORKERS`). |
| `forven/dataeng/lake.py` | `enumerate_series(streams=None, root=None) -> list[SeriesFile]` — every stored series in every stream from parquet footers (cached per file by size+mtime; ~0.7 s warm over 1,700 files). `SeriesFile.id == "{stream}:{venue}:{symbol}:{timeframe}"`. `find_series(...)`. |
| `forven/dataeng/settings.py` | New settings: `sla_tiers`, `sla_breach_multiplier`, `collector` {enabled, tick_seconds, max_tick_seconds, max_requests_per_minute, strike_out_after}, `storage` {trash_retention_days, min_free_disk_gb, revision_keep_days}, `research_universe.asset_classes`. Removed dead knobs (`source_priority`, `onchain_*`, nested `staleness_thresholds`). Settings page entries already exist in `manifest.ts`. |
| Router stubs | `forven/routers/data_{acquire,sla,ops,catalog,readiness}.py`, already registered in `forven/api.py`. Add routes only to your own file. |

### Shared state keys (write only if you own them; anyone may read)

| Key | Owner | Shape |
|---|---|---|
| KV `data:sla_frozen` | B | `{ "<series_id>": {"reason": str, "since": iso, "strikes": int, "manual": bool} }` |
| KV `data:unfillable_gaps` | B | `{ "<series_id>": [[start_ms, end_ms], ...] }` bar-open ms ranges the venue cannot fill |
| Parquet metadata `forven_patched_ranges` | A | JSON `[[start_ms, end_ms], ...]` — bars written by a CSV patch (same mechanism as the existing `forven_synthetic_ranges`) |
| Trash folder `data_root()/.trash/<id>/` + `manifest.json` | C | see §C |

Series ids everywhere: `{stream}:{venue}:{symbol}:{timeframe}` with `venue` = `canonical` or
`<source>:<market>`.

---

## 2. File ownership

New files are owned by whoever creates them. For **shared existing files**, only the owner edits
the listed functions; everyone else calls them.

| File | Owner → functions |
|---|---|
| `forven/data.py` | **A**: write path — `save_parquet`, `_append_bars_locked`/`append_bars`, `_write_lake_parquet`, `_warn_market_mismatch` (→ hard refusal), `fetch_ohlcv_chunked` routing, `_resolve_ohlcv_target`, `save_venue_frame`, CSV (`preview_csv`, `process_csv_upload`, `_read_uploaded_csv`, `_parse_timestamp_series`), `submit_ingestion` + the `_ingestion_runs*` store and `get_ingestion_run`/`get_active_ingestion_runs`. **B**: `backfill_ohlcv_gaps`, `scan_ohlcv_gaps`, `_freshness_for`. **C**: `delete_dataset`, `_drop_catalog_coverage`, orphan scan/cleanup. **D**: read path — `load_parquet`, `read_lake_frame`, `load_venue_frame`, `scan_datasets`/`_dataset_from_file`, `compute_data_quality`, `dataset_ohlcv`, `coverage_entry`. |
| `forven/api_domains/data.py` | **A**: `post_fetch_data`, `post_data_ingestion_submit`, `get_data_ingestion_runs`, `get_data_ingestion_run`, `post_upload_csv*`. **B**: `execute_data_engine_catchup`, `post_execute_data_engine_backfill`, `post_data_engine_backfill_plan`, `post_backfill_gaps`, stream health (`_STREAM_CADENCES`, `get_stream_health`, `post_collect_stream`). **C**: `get_backfill_status`/`post_trigger_backfill`/`post_cancel_backfill` and their state, universe seed functions and state, `delete_dataset_stub`, orphan endpoints, `get_data_activity`. **D**: datasets/quality/coverage/versions/universe read functions (`get_datasets_stub`, `get_quality_reports`, `get_quality_report`, `get_coverage`, `get_dataset_versions`, `get_data_universe`, `post_universe_config`). |
| `forven/scheduler.py` | **B** (data jobs + their migration). **C** may add one maintenance job (trash purge) in its own block. |
| `forven/health_monitor.py` | **B** (data checks). |
| `forven/data_manager.py` | **B** (collect_* orchestration, keep-alive). **C** (`backfill` progress/cancel hooks only). |
| `forven/dataeng/quality_gate.py` | **B**: `check_series_quality` freshness via `sla`. **A**: `dataset_fingerprint`. |
| `forven/dataeng/hub.py`, `catalog.py`, `identity.py`, `universe.py`, `revisions.py` (read side) | **D**. (C may add a revision-prune helper in `revisions.py` in its own function.) |
| `forven/api_core.py` | **A**: the two `data_fingerprint` lines (→ `data_identity`). **C**: API lifespan startup hook (`recover_interrupted`, prune, trash purge). |
| `forven/data_provenance.py` | **A**. |
| `forven/strategies/backtest.py` | **D**: data loading (windowed reads, venue selection, as_of). |
| `forven/dataeng/settings.py`, `manifest.ts`, `backendDefaults.generated.json` | **B** removes `auto_catchup_*`; **D** flips `enabled` default and removes `enabled_exchanges`. Edit only those entries; regenerate the snapshot with `python -m forven.settings_manifest`. |
| `frontend/**` | **F** — except `frontend/src/lib/settings/*` (above) and the strategy-creator/backtest pages (E, later). |

---

## 3. Workstreams

Every workstream: add routes to its own router file with logic in a module (routers stay
thin), write tests in `tests/test_data_next_<stream>.py`, keep the old `/data` page's endpoints
working (compat), and end with a short report (§5).

### A — Getting data in, safely (write path + acquisition)

1. **Venue-scoped writes with hard refusal** (plan F1, 0.1). Canonical family =
   `{binanceusdm, binance-vision, binance}` (Binance spot only as the fallback for bases with no
   USD-M perp, as `_resolve_ohlcv_target` already does). Add `LakeVenueRefused(RuntimeError)`
   and `resolve_series_target(exchange_id, symbol) -> dict(venue, source, market, fs_symbol,
   canonical: bool, destination: 'canonical'|'venue')` in `forven/data.py`. Any other exchange
   (okx, bybit, coinbase, kraken incl. its trades-built path, hyperliquid, csv for a
   Binance-listed pair) writes a venue series `ohlcv/source=<ex>/market=<spot|perp|unknown>/`
   via `save_venue_frame`. A non-canonical source may own a canonical path only when Binance
   lists neither perp nor spot for the pair (unknown listing → venue path). Every write into an
   existing canonical file whose stamp is from another family raises `LakeVenueRefused`.
   Port the three hermetic repros from the plan (OKX, Kraken OHLC window, Kraken trades-built
   all-available): canonical file byte-identical, venue file created. Update existing tests that
   relied on the old behaviour (e.g. `tests/test_data_remaining_phases.py` Kraken classes) to
   the new destination.
2. **Downloads as jobs.** New module `forven/dataeng/acquire.py`: register job kind `download`
   (runner factory, lane = exchange id; `dedupe_key = "download:<venue>:<symbol>:<tf>"`) that calls
   `fetch_ohlcv_chunked` with a progress callback that reports `ctx.progress(bars, total, unit="bars")`
   and calls `ctx.check_cancel()` per page (cancellation stops the fetch; bars already
   checkpointed stay). Optional `streams` collect funding/OI/basis for perps after the candles.
   `check_free_disk()` before starting. Endpoints: `GET /api/data/acquire/targets`,
   `POST /api/data/acquire/estimate`, `POST /api/data/acquire/downloads` (types §acquisition).
   Estimates: ~23 bytes/bar (measured on the lake's zstd files), 1,000 bars/request, the
   venue's `rateLimit`; Kraken trades-built requests warn they are slow. Migrate `submit_ingestion`
   and the `_ingestion_runs` store onto the job store (a run == a `download` job; keep the old
   `/api/fetch`, `/api/data/ingestion/*` payloads working for the old page, reading from jobs).
3. **File import done right** (plan F2, 0.2): `preview_import` / `commit_import` in `acquire.py`
   and `POST /api/data/acquire/import/preview`, `POST /api/data/acquire/import` (multipart). Infer
   the timeframe from the median spacing, report `timeframe_confidence` and misaligned rows,
   reject a declared timeframe that contradicts the file (400 with the inferred one), timezone
   for naive timestamps (default UTC), explicit `mapping_json`, overlap diff (new / identical /
   conflicting — conflict = any of OHLC differs by more than 1e-9 relative), `mode` new|patch,
   `conflict_policy` keep_existing|overwrite, patches stamp `forven_patched_ranges` and keep the
   stored provenance, new series for a Binance-listed pair go to `csv:unknown`. Route the legacy
   `/api/upload/csv*` endpoints through the same validation (a non-daily file sent with the old
   page's hidden `1d` now fails with a clear message instead of corrupting a series).
4. **Provenance key collision** (plan F3, 0.3): `api_core` writes `config["data_identity"]`
   (renamed) using `dataset_fingerprint(canonical_market_symbol(asset) → fs, timeframe)`;
   `artifact_data_fingerprint` treats a non-string value as unstamped. Tests prove canonical
   backtests carry both a string `data_fingerprint` and a non-empty `data_identity`, and a legacy
   dict row is never stale.
5. **Honest payloads**: remote-mode `post_fetch_data` returns the remote response (or
   `status: "submitted"`), never fabricated bars.

### B — Freshness (SLA engine + one collection queue)

1. `forven/dataeng/collector.py`: build the queue from `lake.enumerate_series()` (canonical and
   Hyperliquid OHLCV plus the enrichment streams) × `consumers` tiers × `sla.assess`; frozen and
   fresh series excluded; order by `sla.priority`. Bootstrap tasks only for live/paper/pipeline
   and keep-alive series that have no file (the research universe never downloads on its own).
2. **Cheap tail refresh** for stale canonical OHLCV: footer cursor
   (`dataset_last_timestamp_ms`) → `fetch_ohlcv_chunked(since_ms=...)` → tail-append fast path.
   HL venue series → `collect_hl_series`. Streams → the existing per-symbol collectors
   (`FundingCollector`, `OICollector`, `BasisCollector`, ...). Gap repair only for "gaps" work,
   skipping `data:unfillable_gaps`; record newly-proven unfillable ranges. Rewrite
   `backfill_ohlcv_gaps` so `gaps_filled` counts gaps whose bars actually landed and it stops
   loading the full series six times (plan F6/F7).
3. **Frozen state**: registry-delisted symbols and series with `collector.strike_out_after`
   consecutive "no newer data" refreshes are frozen (`data:sla_frozen`), never scheduled, and
   `POST /api/data/sla/freeze` lets the user freeze/unfreeze.
4. **Budget**: per-venue token bucket (`collector.max_requests_per_minute`) and
   `collector.max_tick_seconds` deadline; unfinished work is `deferred` to the next tick. Each
   tick is recorded with `jobs.record_routine("sla_collect", ...)`. Register kinds
   `tail_refresh`, `gap_repair`, `stream_collect` for `POST /api/data/sla/refresh` (mode refresh |
   repair, explicit series or scope) — the Health view's "Fix" buttons.
5. **Scheduler**: one job `forven-data-sla-collector` every `collector.tick_seconds`. A named,
   idempotent migration disables `forven-data-ohlcv-keepalive` and `forven-data-engine-catchup`
   (test: run it twice); their dispatch branches become logged no-ops so a half-migrated DB can't
   crash the loop. Fold the fixed-cadence stream jobs (funding/OI/...) in only where the collector
   covers every responsibility they have; otherwise leave them and exclude those streams from the
   collector — document which. Remove `auto_catchup_enabled/batch` (settings + manifest entries,
   regenerate the snapshot).
6. **One freshness everywhere**: `check_series_quality` freshness uses the pipeline tier
   (`sla.gate_allowed_staleness_hours`); the quality report's `_freshness_for`, stream health
   cadences and the health monitor all classify through `sla`. `check_candle_freshness` must cover
   **live and paper strategies** (today it only covers running bots, and `get_running_bots()`
   never selects `locked_pairs`, so even that is dead): CRITICAL page for a live-tier breach,
   WARNING for paper.
7. Endpoints: `GET /api/data/sla`, `GET /api/data/collector`, `GET /api/data/venues`,
   `POST /api/data/sla/refresh`, `POST /api/data/sla/freeze`. CLI:
   `python -m forven.agent data-census` (GETs `/api/data/sla`, prints JSON).
8. Acceptance (hermetic): intraday series are not starved by 1d series (ratio-based order), live
   before idle, frozen never scheduled, budget respected, deadline honoured, gate freshness ==
   pipeline SLA.

### C — Operations (jobs API, deep history, storage, safe delete, Data Log)

1. Jobs API: `GET /api/data/jobs`, `GET /api/data/jobs/summary`, `GET /api/data/jobs/{id}`,
   `POST /api/data/jobs/{id}/cancel`, `POST /api/data/jobs/{id}/retry`. Wire
   `jobs.recover_interrupted()`, `prune_jobs()` and a trash purge into the API startup.
2. Deep history as jobs: kind `history_extend` wrapping `DataManager.backfill` with
   progress/cancel (lane `binance-vision`), `POST /api/data/history/extend`. Universe seed as kind
   `universe_seed` (`POST /api/data/universe/seed` returns `UniverseSeedResponse`). Replace the
   `_backfill_state`/`_universe_state` in-memory+KV machinery with the job store while keeping the
   old endpoints' payloads (`/api/data/backfill/status`, `/api/data/universe` `.seed`) for the old
   page. Fix `backfill()` treating `source=hyperliquid`, `RETRY`, etc. as symbols.
3. `forven/dataeng/storage.py`: `inventory()` → `StorageInventory` (reclaim groups: `*.bak`
   backups, legacy loose files in the data root, empty dirs, stray dirs (no parquet / not a
   symbol), stale `.tmp`, revision-log prune candidates); `reclaim(kind, item_ids)` as a `reclaim`
   job that **moves items to the trash** (revisions are pruned per `storage.revision_keep_days`,
   keeping any restatement inside a persisted verdict's window); trash (`data_root()/.trash/<id>/`
   with a `manifest.json`: original path, bytes, reason, deleted_at, series), restore, purge
   (expired automatically after `storage.trash_retention_days`, or on demand with confirm).
   Endpoints: `GET /api/data/storage`, `POST /api/data/storage/reclaim`, `GET /api/data/trash`,
   `POST /api/data/trash/{id}/restore`, `POST /api/data/trash/purge`.
4. Safe delete (plan F5): `GET /api/data/delete/check`, `POST /api/data/delete` — consumers from
   `consumers`, `blocking` for live/paper/pipeline tiers (needs `override_consumers`), typed
   confirm phrase, moves to trash, warns when the collector will re-download it. The legacy
   `DELETE /api/datasets/...` also moves to trash and refuses (409) when blocking.
5. Data Log: `GET /api/data/log` (+ `/export` CSV) over `activity_log` (source `data`) and
   `data_jobs`: categories user / incident / routine, routine collection rolled up per tick,
   filters, paging. Keep `/api/data/activity` for the old page.

### D — Finding and trusting data (catalog, identity, read path)

1. Catalog: `GET /api/data/catalog` built from `lake.enumerate_series()` + `consumers` +
   `sla.assess` + frozen state + stamps + a quality cache; filters, facets, sort, paging; p95 <
   300 ms warm. Quality: one rubric computed with DuckDB per series, cached by file fingerprint in
   the DuckDB catalog (`series_quality` table), recomputed only when the file changes. Rubric
   (document it in code): start 100; completeness < 0.98 → −min(40, (0.98−c)×200); largest gap
   ≥ 12 bars → −10; invalid OHLC rows → −min(20, 5 each); null OHLC → −min(10, 1 each); outliers
   (|log return| > 8σ) → −min(10, 1 each); freshness is **not** in the score (it is the SLA).
   `issues` lists the reasons in plain language.
2. Series detail, bars (server-side OHLC aggregation to `max_points` via DuckDB), gaps
   (kind from `data:unfillable_gaps` and `forven_synthetic_ranges`), rows, stream points,
   `month_map` (bars expected vs present per month, synthetic/patched/restated counts),
   provenance (stamps, synthetic/patched ranges, restatements from the revision log),
   `consumers_detail` with the data-gate verdict over each consumer's latest backtest window,
   `recent_jobs`, `venues_available`.
3. Identity: `GET /api/data/identity/resolve` (registry + stored series + aliases: BTC, BTCUSDT,
   BTC/USDT, BTC-USD… → candidates with venues and what is stored), `GET /api/data/identity/audit`
   (alias duplicates such as BTC-USD vs BTCUSD vs BTC-USDT, unknown symbols, empty/stray dirs,
   delisted-but-collected, unstamped) — report only, never move files.
4. Universe: `GET /api/data/universe/plan-diff`; classify registry rows crypto vs tradfi (from
   the venue's market info) and honour `research_universe.asset_classes` in
   `plan_research_universe`.
5. Read path: `load_parquet(symbol, tf, *, as_of=None, start=None, end=None, columns=None)`
   windowed (pass through to `hub.candles`, which already supports start/end/columns; pyarrow
   filters on the legacy path); callers that only need a window (quality gate, backtest data
   load) pass it. A failed `as_of` reconstruction raises (`AsOfReconstructionError`) instead of
   silently returning latest data (plan F4). Parity test: windowed == full[mask].
6. Single engine, step one: flip `data_engine.enabled` default to True, remove
   `enabled_exchanges` (derive `hub.status()` sources from the breaker registry), add an
   engine-on vs legacy parity test. **Do not delete the legacy read path** — that is a follow-up
   after the integration suite is green.
7. Venue-aware backtests (plan 2.5): a `data_venue` option ("canonical" default, or a venue key)
   through the backtest data load, the backtest API models and the MCP backtest tool; enrichment
   streams stay canonical.

### E — Readiness (second wave, after B and D land)

Specified when B and D are merged.

### F — The new page (`/data-next`)

Build against `dataManagerTypes.ts`. Client functions in `frontend/src/lib/api/dataManager.ts`
(one per endpoint, using `fetchApi`), shared state in `frontend/src/lib/stores/dataManager.ts`,
components in `frontend/src/lib/components/data-manager/`, routes under
`frontend/src/routes/data-next/`. Realistic fixtures in
`frontend/src/lib/api/dataManagerFixtures.ts` for tests. See §4 for the UX spec.

---

## 4. UX spec for `/data-next`

**The three questions**: *Is my data OK? What needs attention and why? How do I fix it?* —
then *What do I have?* and *How do I get more?* Every view answers its question in three
seconds. Plain language first, jargon behind tooltips. Every colour or score comes from the
server (never recompute thresholds client-side).

**Design language** (match the Strategy Creator): black surfaces (`#000`, `#050505`,
`#0a0a0a`), 1px borders `#1a1a1a`/`#222`/`#333`, square corners, uppercase tracked micro labels
(`text-[9px]`–`text-[11px] uppercase tracking-wider text-[#555]`), mono numerals, white primary
text, `#aaa`/`#888`/`#666` secondary. State colours: fresh = emerald, late = amber, breach =
red, frozen = slate/grey hatched, missing = dark outline. Always pair colour with a word
(accessibility). `terminal-button`, `terminal-button-primary`, `terminal-input` classes. No
native `alert`/`confirm`. UTC on every absolute timestamp, relative time next to it.

**Layout**: `routes/data-next/+layout.svelte` holds the header (title "Data", one-line status
from the SLA census, global symbol search that jumps to a series, "Get data" primary button,
"Import file" button, a jobs indicator "2 running" that opens the Jobs drawer) and the tab bar
(Health · Catalog · Coverage · Jobs · Storage · Log). Tabs are routes. The Jobs drawer slides
from the right (no full-screen backdrop — overlays inside `<main>` draw under the sidebar on the
current main branch).

**Health** (`/data-next`)
- Verdict hero: one sentence, colour-coded — "Live and paper data is current" / "2 series that
  paper strategies trade on are late" / "Live strategy S01566 is trading on data 3 h stale" —
  with "checked 12 s ago · 1,684 series".
- Tier cards Live · Paper · Pipeline · Research · Idle: fresh / late / breach / frozen counts with
  a stacked bar; click → Catalog filtered to that tier.
- Needs attention: the census `worst` list grouped and ordered by tier → each row says what, why
  ("allowed 45 min, 5 h 12 m behind; feeds S04928 (paper)") and offers one button (Refresh now,
  Repair gaps) plus "Details" → series page. A single "Fix all late live & paper" action.
- Sources: venue health strip (Binance USD-M, Binance Vision, Hyperliquid, OKX liquidations,
  Deribit IV) — status, last success, error, what it affects.
- Collector: last run, next run, refreshed in the last hour, demand vs capacity per hour
  ("needs ~123/h · can do 300/h").
- Storage glance: lake size, reclaimable bytes → Storage. Recent incidents (Log category
  incident, last 24 h).
- Empty lake → a friendly setup card linking to `/data-next/setup`.

**Catalog** (`/data-next/catalog`)
- Toolbar: search (symbol or alias), filter chips with counts from `facets` (stream, venue,
  tier, state, asset class, timeframe), sort, saved views (stored in localStorage: "Live &
  paper", "Problems", "Research universe", "Intraday", "Everything"), column chooser.
- Virtualized table (1,700+ rows stays smooth): checkbox · symbol + venue chip · tf · stream ·
  history (first → last, span) · completeness bar · freshness chip (state + "5.5 h / 2 h") ·
  quality score · consumers (count + stage chips) · size · updated. Row → series page.
  Keyboard: ↑/↓ move, Enter open, Space select, `/` focuses search.
- Bulk bar on selection: Refresh, Repair gaps, Extend history, Export, Freeze/Unfreeze, Delete
  (→ inline delete review listing consumers, typed confirmation, "moves to trash for N days").

**Coverage** (`/data-next/coverage`)
- Symbols × timeframes grid from the catalog, one stream at a time (all 8 streams selectable),
  grouped by tier with sticky group headers, search, "show frozen". Cell colour = server SLA
  state; label = history length ("6.1y"); planned-but-missing universe cells (plan-diff) show a
  dashed "planned" outline. A second mode colours by history depth. Cell click → series page;
  shift-click/drag selects cells → "Refresh selected" / "Download missing".

**Series page** (`/data-next/series/[symbol]/[timeframe]?stream=ohlcv&venue=canonical`)
- Header: display symbol, timeframe, venue chip ("Binance USD-M perp · canonical research
  series"), SLA chip, quality score, consumer count; actions Refresh, Repair gaps, Extend
  history, Export, Freeze, Delete.
- Coverage timeline ("gap map"): years as rows × months as columns, cell shade = completeness,
  overlays for synthetic / CSV-patched / restated months; hover shows counts; click zooms the chart
  to that month.
- Chart: full history downsampled from the server, zoom to raw bars (lightweight-charts, reuse
  `CandlestickChart` if it fits), gap markers.
- Streams: the symbol's funding / OI / basis / IV / LSR / taker / liquidation series with their
  own freshness chip and a sparkline.
- Consumers: each strategy/bot with stage and the data-gate verdict for its window.
- Provenance: stamps, identity, synthetic and patched ranges, restatements, recent jobs.
- Rows: raw rows for a chosen window, paged.

**Get data** (`/data-next/get`) — a guided flow, not a form.
1. What: "A market" · "A research universe" (→ universe config + seed) · "A file" (→ import).
2. Market: symbol search (identity resolve, shows where it is listed and what is stored);
   venue choice with the canonical-vs-venue explanation; timeframes (multi-select, with per-row
   estimate); history (All available / last N years / custom UTC range); perp add-ons (funding,
   OI, basis).
3. Review: table of series with existing coverage, new bars, size, time, warnings (1m full
   history ≈ 80 MB, disk free), then Start → jobs appear in the drawer; "you can leave this page".

**Import file** (`/data-next/import`) — drop a CSV → mapping (detected columns, timestamp
format, timezone) → parsed preview in UTC with inferred timeframe and confidence → target
(symbol, timeframe, new vs patch, overlap diff with conflict examples and policy) → Import →
result + link to the series.

**Jobs** (`/data-next/jobs`) — running (progress, ETA, Cancel), queued (Cancel), failed (error
code in words + Retry), finished; filters by origin (You / Automatic / Strategy demand /
Universe), kind, status; routine collection collapsed into one row per run.

**Storage** (`/data-next/storage`) — size by stream and top series; reclaimable groups
(backups, legacy files, stray/empty folders, stale temp files, revision log) each with item
preview and "Move to trash" (typed confirm); trash with Restore / Empty now; identity audit list
(report only).

**Log** (`/data-next/log`) — "Your actions & incidents" (default) and "Automatic" tabs;
filters (level, symbol, action, date range), search, CSV export, paging.

**Setup** (`/data-next/setup`) — presets with real estimates: Starter (BTC, ETH, SOL · 1h/4h/1d ·
full history + funding/OI), Research (top-25 perps + intraday for the top 10), Pro (top-50 +
intraday top-20 + 1m top-10) → one click queues the jobs.

**Also**: the old `/data` header gets a small "Try the new Data Manager →" link to `/data-next`;
the `/data` nav indicator (`stores/heartbeat.ts`) shows live+paper breach counts from
`/api/data/sla`.

---

## 5. Definition of done and report

- New and changed behaviour covered by tests in your own test file(s); the affected existing
  tests updated and passing; ruff clean for Python (`ruff check <files>`), `npm run check` clean
  for frontend files you touched.
- All work committed on your branch.
- Report (as your final message): branch + head commit; what you built (endpoint list); deleted
  code; tests run with results; anything in the contract you could not follow and why; known gaps;
  any shape you believe is wrong in `dataManagerTypes.ts` (with the fix you suggest).
