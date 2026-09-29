/**
 * Wire types for the Data Manager API (the `/data` page).
 *
 * THIS FILE IS THE CONTRACT. Backend routes in forven/routers/data_{acquire,sla,
 * ops,catalog,readiness}.py return exactly these shapes; the UI codes against
 * them. Semantics and ownership: docs/data-manager-next/CONTRACT.md.
 *
 * Conventions
 * - Timestamps are ISO-8601 UTC strings ending in "Z".
 * - `symbol` is the filesystem-canonical pair ("BTC-USDT"); `display_symbol`
 *   is the human form ("BTC/USDT").
 * - Durations are seconds; sizes are bytes.
 * - `venue` is "canonical" for the research lake (Binance USD-M perp family:
 *   binanceusdm / binance-vision, with Binance spot only for bases that have
 *   no perp) or "<source>:<market>" for a separate venue series
 *   ("hyperliquid:perp", "okx:spot", "csv:unknown").
 */

// ---------------------------------------------------------------- shared

export type SlaTier = 'live' | 'paper' | 'pipeline' | 'universe' | 'idle';
export type SlaState = 'fresh' | 'late' | 'breach' | 'frozen' | 'missing';
export type DataStream =
	| 'ohlcv'
	| 'funding'
	| 'oi'
	| 'basis'
	| 'iv'
	| 'ls_ratio'
	| 'taker'
	| 'liquidations';
export type VenueKey = string;

export interface SeriesKey {
	symbol: string;
	/** Bar timeframe; streams without bars use their print cadence ("8h" funding, "1h" basis). */
	timeframe: string;
	stream: DataStream;
	venue: VenueKey;
}

/** forven/dataeng/sla.py::assess — lag is measured from the last bar's OPEN time. */
export interface SlaAssessment {
	tier: SlaTier;
	state: SlaState;
	lag_seconds: number | null;
	allowed_seconds: number;
	/** lag / allowed; >1 means late. null when there is no data. */
	ratio: number | null;
	last_bar_ts: string | null;
	/** Collection priority (ratio x tier weight). 0 for frozen series. */
	priority: number;
}

export interface ConsumerRef {
	kind: 'strategy' | 'bot' | 'workflow';
	id: string;
	name: string;
	/** Strategy stage (live_graduated, paper, gauntlet, ...). */
	stage?: string;
	/** Bot or workflow status. */
	status?: string;
}

export interface ConsumerSummary {
	tier: SlaTier;
	count: number;
	/** Most important consumers first (live, paper, pipeline), at most 5. */
	top: ConsumerRef[];
}

// ---------------------------------------------------------------- jobs (C)

export type DataJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
export type DataJobKind =
	| 'download'
	| 'history_extend'
	| 'universe_seed'
	| 'csv_import'
	| 'gap_repair'
	| 'tail_refresh'
	| 'stream_collect'
	| 'sla_collect'
	| 'reclaim'
	| 'compaction';

/** forven/dataeng/jobs.py row. `origin`: "user" | "sla" | "universe" | "system" | "strategy:<id>". */
export interface DataJob {
	id: string;
	kind: DataJobKind;
	title: string;
	origin: string;
	status: DataJobStatus;
	lane: string;
	/** Automatic background work, rolled up in the Data Log. */
	routine: boolean;
	params: Record<string, unknown>;
	series: Array<Partial<SeriesKey> & { symbol: string }>;
	progress: { done: number; total: number | null; unit: string | null; current?: string | null };
	message: string | null;
	result: unknown;
	/** code: rate_limited | venue_down | unknown_symbol | delisted | venue_refused | shrink_refused |
	 *  corrupt | disk_full | invalid_request | cancelled | backend_restarted | internal */
	error: { code: string; message: string } | null;
	attempts: number;
	parent_id: string | null;
	cancel_requested: boolean;
	retryable: boolean;
	created_at: string;
	started_at: string | null;
	finished_at: string | null;
	updated_at: string;
}

/** GET /api/data/jobs?status=&kind=&origin=&routine=&symbol=&since=&limit=&offset=
 *  status/kind take several values as repeated params (?status=failed&status=queued) or comma-separated. */
export interface DataJobList {
	total: number;
	jobs: DataJob[];
}

/** POST /api/data/jobs/{id}/cancel and /retry (409 when not retryable) */
export interface DataJobResponse {
	job: DataJob;
}

/** GET /api/data/jobs/summary */
export interface DataJobSummary {
	running: number;
	queued: number;
	failed_24h: number;
	succeeded_24h: number;
	last_routine: DataJob | null;
}

// ---------------------------------------------------------------- catalog (D)

export interface QualitySummary {
	/** 0-100, one documented server-side rubric (CONTRACT.md §D). null = not computed yet. */
	score: number | null;
	/** Plain-language issues, worst first ("3 gaps, largest 14 bars", "12 bars with high < low"). */
	issues: string[];
	computed_at: string | null;
}

export interface CatalogRow {
	/** `${stream}:${venue}:${symbol}:${timeframe}` */
	id: string;
	symbol: string;
	display_symbol: string;
	timeframe: string;
	stream: DataStream;
	venue: VenueKey;
	/** forven_source stamp: binanceusdm | binance-vision | binance | hyperliquid | okx | csv | ... */
	source: string | null;
	/** perp | spot | unknown | unstamped; null for enrichment streams (their files carry no stamp) */
	market: string | null;
	/** crypto | tradfi | stock | etf | forex | index */
	asset_class: string;
	first_ts: string | null;
	last_ts: string | null;
	rows: number;
	/** Bars the [first_ts, last_ts] span implies. */
	expected_rows: number | null;
	/** rows / expected_rows, 0..1 */
	completeness: number | null;
	gap_count: number | null;
	largest_gap_bars: number | null;
	size_bytes: number;
	quality: QualitySummary;
	sla: SlaAssessment;
	consumers: ConsumerSummary;
	frozen: boolean;
	frozen_reason: string | null;
	/** Whether a refresh can do anything for it (false: recorded live from a
	 *  feed, or nothing collects it); null or absent = unknown. */
	refreshable?: boolean | null;
	/** Why a refresh can't help, in plain words. */
	refresh_note?: string | null;
	delisted: boolean;
	/** Forward-filled (fabricated) bars from trades-built venues. */
	synthetic_bars: number;
	/** Bars written by a CSV patch into this series. */
	patched_bars: number;
	/** Last write to the series files. */
	updated_at: string | null;
}

export interface CatalogFacets {
	stream: Record<string, number>;
	venue: Record<string, number>;
	tier: Record<string, number>;
	state: Record<string, number>;
	asset_class: Record<string, number>;
	timeframe: Record<string, number>;
}

/** GET /api/data/catalog?q=&stream=&venue=&tier=&state=&asset_class=&timeframe=&sort=&order=&limit=&offset=
 *  sort: priority (default, most overdue first) | symbol | last_ts | completeness | quality | size | rows | consumers */
export interface CatalogResponse {
	generated_at: string;
	total: number;
	rows: CatalogRow[];
	/** Counts over the whole catalog (ignoring the current filters) for filter chips. */
	facets: CatalogFacets;
}

export interface MonthCell {
	/** "YYYY-MM" */
	month: string;
	expected: number;
	present: number;
	synthetic: number;
	patched: number;
	restated: number;
}

export interface GapSpan {
	/** First missing bar open time. */
	start: string;
	/** Last missing bar open time. */
	end: string;
	bars: number;
	/** missing = fetchable hole; unfillable = the venue has no bars there (memoized); synthetic = forward-filled. */
	kind: 'missing' | 'unfillable' | 'synthetic';
}

export interface StreamSummary {
	stream: DataStream;
	/** The stored series' own symbol: the pair, or the currency for market-wide streams (DVOL: "BTC", "ETH"). */
	symbol?: string;
	timeframe: string;
	venue: VenueKey;
	rows: number;
	first_ts: string | null;
	last_ts: string | null;
	size_bytes: number;
	sla: SlaAssessment;
	/** Columns a strategy sees after enrichment (funding_rate, open_interest, ...). */
	columns: string[];
}

export interface Restatement {
	observed_at: string;
	rows: number;
	first_ts: string | null;
	last_ts: string | null;
}

export interface ProvenanceInfo {
	source: string | null;
	market: string | null;
	venue: VenueKey;
	stamped_symbol: string | null;
	stamped_at: string | null;
	synthetic_ranges: Array<[string, string]>;
	patched_ranges: Array<[string, string]>;
	restatements: Restatement[];
}

export interface ConsumerDetail extends ConsumerRef {
	/** The gauntlet data-gate verdict over this consumer's scoring window (null when not applicable). */
	gate: { ok: boolean; reasons: string[] } | null;
}

/** GET /api/data/series/{symbol}/{timeframe}?stream=ohlcv&venue=canonical */
export interface SeriesDetail extends CatalogRow {
	month_map: MonthCell[];
	/** First 200 gaps, largest first. */
	gaps: GapSpan[];
	gaps_total: number;
	/** The symbol's other streams (funding, OI, basis, ...) with their own SLA. */
	streams: StreamSummary[];
	provenance: ProvenanceInfo;
	consumers_detail: ConsumerDetail[];
	recent_jobs: DataJob[];
	/** Other venues that hold this symbol/timeframe. */
	venues_available: VenueKey[];
}

export interface Bar {
	t: string;
	o: number;
	h: number;
	l: number;
	c: number;
	v: number;
}

/** GET /api/data/series/{symbol}/{timeframe}/bars?venue=&start=&end=&max_points=1500
 *  Server-side OHLC aggregation to at most max_points bars; raw bars when they fit. */
export interface BarsResponse {
	symbol: string;
	timeframe: string;
	venue: VenueKey;
	start: string | null;
	end: string | null;
	/** "raw" or the aggregation bucket ("4h", "1d", "1w"). */
	resolution: string;
	raw: boolean;
	total_in_range: number;
	bars: Bar[];
}

/** GET /api/data/series/{symbol}/{timeframe}/gaps?venue=&limit=&offset= */
export interface GapsResponse {
	total: number;
	gaps: GapSpan[];
}

/** GET /api/data/series/{symbol}/{timeframe}/rows?venue=&start=&end=&limit=&offset=&order=asc|desc (raw stored rows; oldest first unless order=desc) */
export interface RowsResponse {
	total: number;
	columns: string[];
	rows: Array<Record<string, number | string | null>>;
}

/** GET /api/data/streams/{symbol}/{stream}/points?timeframe=&venue=&start=&end=&max_points=1500
 *  Each point carries its time as `t` (like Bar) plus the stream's value columns. */
export interface StreamPointsResponse {
	symbol: string;
	stream: DataStream;
	timeframe: string;
	columns: string[];
	resolution: string;
	raw: boolean;
	points: Array<Record<string, number | string | null>>;
}

export interface SymbolVenue {
	venue: VenueKey;
	market: string;
	listed: boolean;
	history_start: string | null;
}

export interface SymbolCandidate {
	symbol: string;
	display_symbol: string;
	base: string;
	quote: string;
	asset_class: string;
	venues: SymbolVenue[];
	/** What is already stored for it. */
	stored: Array<{ timeframe: string; venue: VenueKey; stream: DataStream; rows: number; last_ts: string | null }>;
	delisted: boolean;
	aliases: string[];
}

/** GET /api/data/identity/resolve?q= */
export interface IdentityResolveResponse {
	query: string;
	candidates: SymbolCandidate[];
}

export interface IdentityIssue {
	kind: 'alias_duplicate' | 'unknown_symbol' | 'empty_dir' | 'stray_dir' | 'delisted_collected' | 'unstamped';
	path: string;
	symbol: string;
	detail: string;
	related: string[];
	bytes: number;
	suggestion: string;
}

/** GET /api/data/identity/audit */
export interface IdentityAuditResponse {
	generated_at: string;
	issues: IdentityIssue[];
}

/** GET /api/data/universe/plan-diff */
export interface UniversePlanDiff {
	enabled: boolean;
	size: number;
	asset_classes: string[];
	planned_series: number;
	present_series: number;
	missing: Array<{ symbol: string; rank: number; timeframes: string[]; asset_class: string }>;
	/** Stored canonical candle series outside the plan whose tier is idle (e.g. fell out of the top N). */
	extra: Array<{ symbol: string; timeframes: string[] }>;
	/** Latest universe_seed job, if any. */
	seed_job: DataJob | null;
}

// ---------------------------------------------------------------- freshness (B)

export interface SlaSeriesRow {
	symbol: string;
	display_symbol: string;
	timeframe: string;
	stream: DataStream;
	venue: VenueKey;
	sla: SlaAssessment;
	consumers: ConsumerSummary;
	frozen: boolean;
	frozen_reason: string | null;
	/** Whether a refresh can do anything for it (false: recorded live from a
	 *  feed, or nothing collects it); null or absent = unknown. */
	refreshable?: boolean | null;
	/** Why a refresh can't help, in plain words. */
	refresh_note?: string | null;
}

/** GET /api/data/sla?stream=&limit_worst=50 */
export interface SlaCensus {
	generated_at: string;
	total: number;
	states: Record<SlaState, number>;
	by_tier: Record<SlaTier, Record<SlaState, number>>;
	by_timeframe: Record<string, Record<SlaState, number>>;
	/** Most overdue first (priority desc), frozen excluded. */
	worst: SlaSeriesRow[];
	lag_ratio_p50: number | null;
	lag_ratio_p95: number | null;
	/** The thresholds in force, for "allowed" captions. */
	policy: Record<SlaTier, { missed_bars: number; floor_minutes: number }>;
	breach_multiplier: number;
}

export interface CollectorTick {
	started_at: string;
	finished_at: string;
	refreshed: number;
	bars_added: number;
	failed: number;
	/** Due series left for the next tick (budget or deadline reached). */
	deferred: number;
	bootstrapped?: number;
	frozen?: number;
}

/** GET /api/data/collector */
export interface CollectorStatus {
	enabled: boolean;
	tick_seconds: number;
	last_tick: CollectorTick | null;
	next_tick_at: string | null;
	queue_depth: number;
	refreshed_last_hour: number;
	/** Refreshes/hour needed to keep every non-frozen series within its SLA. */
	demand_per_hour: number;
	/** Refreshes/hour the configured budget allows. */
	capacity_per_hour: number;
	budget: Array<{ venue: string; used_last_minute: number; limit_per_minute: number }>;
}

export interface VenueHealth {
	venue: string;
	label: string;
	/** What the venue supplies, in plain language ("research candles", "execution venue", "deep history", ...). */
	role: string;
	status: 'healthy' | 'degraded' | 'down' | 'unknown';
	last_success_at: string | null;
	last_failure_at: string | null;
	consecutive_failures: number;
	last_error: string | null;
	/** What breaks while it is down, in plain language. */
	affects: string;
}

/** GET /api/data/venues */
export interface VenuesResponse {
	venues: VenueHealth[];
}

/** POST /api/data/sla/refresh  body: SlaRefreshRequest -> { job: DataJob } */
export interface SlaRefreshRequest {
	series?: SeriesKey[];
	/** late = every late/breach series; late_live_paper = only the live and paper tiers. */
	scope?: 'late' | 'late_live_paper';
	/** refresh = bring the tail current (default); repair = also re-fetch interior gaps. */
	mode?: 'refresh' | 'repair';
}

/** POST /api/data/sla/freeze  body -> { updated: number } */
export interface SlaFreezeRequest {
	series: SeriesKey[];
	frozen: boolean;
	reason?: string;
}

/** POST /api/data/history/extend  -> { job: DataJob }  (Binance Vision deep history) */
export interface HistoryExtendRequest {
	/** Whole symbols (every stored timeframe), or specific series. */
	symbols?: string[];
	series?: SeriesKey[];
	streams?: Array<'ohlcv' | 'funding' | 'oi' | 'basis'>;
}

/** POST /api/data/universe/seed  -> UniverseSeedResponse (the seed runs as a universe_seed job) */
export interface UniverseSeedResponse {
	status: 'started' | 'already_running';
	job: DataJob;
}

// ---------------------------------------------------------------- storage & log (C)

export interface ReclaimItem {
	id: string;
	path: string;
	bytes: number;
	modified_at: string | null;
	note?: string;
}

export interface ReclaimGroup {
	kind: 'backups' | 'legacy_root' | 'empty_dirs' | 'stray_dirs' | 'orphan_tmp' | 'revisions';
	label: string;
	description: string;
	bytes: number;
	count: number;
	/** First 200 items. */
	items: ReclaimItem[];
	/** True when reclaiming cannot lose data a series needs (temp files, backups of reconciled series). */
	safe: boolean;
}

/** GET /api/data/storage */
export interface StorageInventory {
	generated_at: string;
	data_root: string;
	disk: { free_bytes: number; total_bytes: number; min_free_gb: number };
	lake: { bytes: number; files: number; series: number };
	by_stream: Array<{ stream: string; bytes: number; files: number }>;
	top_series: Array<{ symbol: string; timeframe: string; stream: DataStream; venue: VenueKey; bytes: number }>;
	reclaimable: ReclaimGroup[];
	trash: { items: number; bytes: number; oldest: string | null; retention_days: number };
	/** prunable_bytes is proportional to prunable rows (an estimate). */
	revisions: { bytes: number; files: number; oldest: string | null; keep_days: number; prunable_bytes: number | null };
}

/** POST /api/data/storage/reclaim  body -> { job: DataJob }  (moves items to the trash; revisions are pruned) */
export interface ReclaimRequest {
	kind: ReclaimGroup['kind'];
	item_ids: string[] | 'all';
	/** Must equal "reclaim <kind>". */
	confirm: string;
}

export interface TrashItem {
	id: string;
	kind: 'series' | 'backup' | 'legacy' | 'dir' | 'temp';
	label: string;
	original_path: string;
	bytes: number;
	deleted_at: string;
	purge_after: string;
	reason: string;
	series: SeriesKey | null;
}

/** POST /api/data/trash/{id}/restore -> { restored: TrashItem }; 409 detail { message, conflicts } when the
 *  original path has been re-created since. */
export interface TrashRestoreResponse {
	restored: TrashItem;
}

/** POST /api/data/trash/purge — confirm is "empty trash" for 'all', else "purge <n> item(s)"; 'expired' needs none. */
export interface TrashPurgeRequest {
	item_ids?: string[] | 'all' | 'expired';
	confirm?: string;
}

export interface TrashPurgeResult {
	purged: number;
	bytes: number;
}

/** GET /api/data/trash */
export interface TrashResponse {
	items: TrashItem[];
	bytes: number;
	retention_days: number;
}

/** GET /api/data/delete/check?symbol=&timeframe=&stream=ohlcv&venue=canonical */
export interface DeleteCheck {
	series: SeriesKey;
	exists: boolean;
	bytes: number;
	rows: number;
	consumers: ConsumerRef[];
	/** True when a live/paper/pipeline consumer reads the series: deleting needs override_consumers. */
	blocking: boolean;
	/** The collector will download it again (it is in the keep-alive set or the research universe). */
	will_rebootstrap: boolean;
	warnings: string[];
	/** The exact text the user must type to confirm ("delete BTC-USDT 1h"). */
	confirm_phrase: string;
}

/** POST /api/data/delete  body -> DeleteResult */
export interface DeleteRequest {
	series: SeriesKey[];
	/** One confirm phrase for a single series; "delete <n> series" for a batch. */
	confirm: string;
	override_consumers?: boolean;
}

export interface DeleteResult {
	trashed: TrashItem[];
	skipped: Array<{ series: SeriesKey; reason: string }>;
}

export interface DataLogEntry {
	id: string;
	ts: string;
	level: 'info' | 'warning' | 'error';
	/** user = something a person started; incident = a failure or data problem; routine = automatic work. */
	category: 'user' | 'incident' | 'routine';
	action: string;
	message: string;
	symbol: string | null;
	timeframe: string | null;
	job_id: string | null;
	origin: string | null;
	detail: Record<string, unknown>;
	/** Routine roll-ups: how many underlying entries this row stands for. */
	children?: number;
}

/** GET /api/data/log?category=&level=&symbol=&action=&since=&until=&q=&limit=&offset=
 *  category/level/action take several values as repeated params or comma-separated.
 *  (GET /api/data/log/export with the same params returns text/csv) */
export interface DataLogResponse {
	total: number;
	entries: DataLogEntry[];
}

// ---------------------------------------------------------------- acquisition (A)

export interface VenueTarget {
	venue: VenueKey;
	/** ccxt exchange id used for the download. */
	exchange: string;
	market: 'perp' | 'spot';
	listed: boolean;
	/** True for the research lake's own venue family. */
	canonical: boolean;
	/** Where a download would be stored. */
	destination: 'canonical' | 'venue';
	note: string;
	/** First available bar on the venue when known (listing date). */
	history_start?: string | null;
}

/** GET /api/data/acquire/targets?symbol= */
export interface VenueTargetsResponse {
	symbol: string;
	display_symbol: string;
	targets: VenueTarget[];
}

export type HistoryRequest =
	| { mode: 'all' }
	| { mode: 'days'; days: number }
	| { mode: 'range'; start: string; end: string };

export interface DownloadRequestItem {
	symbol: string;
	timeframe: string;
	/** "canonical" or "<exchange>:<market>" from VenueTarget.venue */
	venue: VenueKey;
	history: HistoryRequest;
	/** Extra perp streams to collect alongside the candles. */
	streams?: DataStream[];
}

export interface DownloadEstimate {
	/** Echoes the request with the symbol normalized ("BTC-USDT"). */
	item: DownloadRequestItem;
	destination: 'canonical' | 'venue';
	/** Where the series lands ("canonical" or "<source>:<market>"). */
	venue?: VenueKey;
	existing_rows: number;
	existing_first: string | null;
	existing_last: string | null;
	new_bars_estimate: number;
	bytes_estimate: number;
	seconds_estimate: number;
	requests_estimate: number;
	warnings: string[];
	/** Why this item cannot be downloaded (unknown symbol, not listed on the venue, ...). */
	blocked: string | null;
}

/** POST /api/data/acquire/estimate  body: { items: DownloadRequestItem[] } */
export interface DownloadEstimateResponse {
	estimates: DownloadEstimate[];
	total_bytes: number;
	total_seconds: number;
	disk_free_bytes: number;
	warnings: string[];
}

/** POST /api/data/acquire/downloads  body: { items: DownloadRequestItem[] } -> { jobs: DataJob[] } */
export interface DownloadJobsResponse {
	jobs: DataJob[];
}

export interface ImportOverlap {
	new_bars: number;
	identical: number;
	conflicting: number;
	conflict_examples: Array<{ t: string; stored_close: number; file_close: number }>;
}

/** POST /api/data/acquire/import/preview  multipart: file (+ optional symbol, timeframe,
 *  timestamp_column, date_format, timezone, mapping_json) */
export interface ImportPreview {
	filename: string;
	rows: number;
	columns: string[];
	mapping: { timestamp: string | null; open: string | null; high: string | null; low: string | null; close: string | null; volume: string | null };
	required_ok: boolean;
	/** First rows exactly as in the file. */
	sample: Array<Record<string, unknown>>;
	/** First rows as they will be stored (UTC). */
	parsed_sample: Bar[];
	first_ts: string | null;
	last_ts: string | null;
	inferred_timeframe: string | null;
	/** Share of consecutive bars spaced exactly one inferred timeframe apart, 0..1. */
	timeframe_confidence: number;
	/** Rows whose timestamp is not on a bar boundary of the (declared or inferred) timeframe. */
	misaligned_rows: number;
	invalid_rows: number;
	target: {
		symbol: string;
		timeframe: string;
		exists: boolean;
		destination: 'canonical' | 'venue';
		existing_rows: number;
		existing_source: string | null;
		overlap: ImportOverlap;
	} | null;
	errors: string[];
	warnings: string[];
}

/** POST /api/data/acquire/import  multipart: file, symbol, timeframe, mode, conflict_policy
 *  (+ timestamp_column, date_format, timezone, mapping_json) */
export interface ImportRequestFields {
	symbol: string;
	timeframe: string;
	/** new = a series that does not exist yet; patch = add bars into an existing series. */
	mode: 'new' | 'patch';
	conflict_policy: 'keep_existing' | 'overwrite';
}

export interface ImportResult {
	series: SeriesKey;
	rows_written: number;
	new_bars: number;
	overwritten: number;
	kept: number;
	destination: 'canonical' | 'venue';
	warnings: string[];
}

// ---------------------------------------------------------------- readiness (E)

export interface ReadinessRequirement {
	key: string;
	kind: 'series' | 'stream' | 'history' | 'freshness' | 'venue';
	label: string;
	symbol: string;
	timeframe: string;
	stream: DataStream;
	min_history_days: number | null;
	status: 'ok' | 'warn' | 'missing' | 'blocked';
	detail: string;
	fix: { action: 'download' | 'extend_history' | 'refresh' | 'none'; label: string; request?: DownloadRequestItem } | null;
}

/** POST /api/data/readiness body. `code` is scanned as text, never run; `strategy_type` may be a strategy id. */
export interface ReadinessRequest {
	symbol: string;
	timeframe: string;
	streams?: DataStream[];
	history_days?: number;
	strategy_type?: string;
	code?: string;
}

/** GET /api/data/readiness/strategy/{strategy_id}
 *  POST /api/data/readiness  body: ReadinessRequest */
export interface ReadinessReport {
	subject: { strategy_id?: string; name?: string; symbol: string; timeframe: string };
	verdict: 'ready' | 'needs_data' | 'blocked';
	summary: string;
	requirements: ReadinessRequirement[];
	generated_at: string;
}

/** GET /api/data/series/{symbol}/{timeframe}/fingerprint?venue= */
export interface SeriesFingerprint {
	symbol: string;
	timeframe: string;
	venue: VenueKey;
	version: number;
	months: Array<{ month: string; rows: number; hash: string }>;
	/** Verdicts scored on this series whose recorded month hashes no longer match. */
	drifted_verdicts: Array<{ result_id: string; strategy_id: string; result_type: string; created_at: string; months: string[] }>;
	/** Verdicts compared, and verdicts skipped because they carry no month stamp. */
	checked_verdicts?: number;
	skipped_unstamped?: number;
}

/** GET /api/data/divergence/{symbol}?timeframe=1h */
export interface VenueDivergence {
	symbol: string;
	timeframe: string;
	research_venue: VenueKey;
	execution_venue: VenueKey;
	overlap_bars: number;
	max_close_divergence_pct: number | null;
	mean_abs_divergence_pct: number | null;
	computed_at: string | null;
	status: 'ok' | 'warn' | 'blocked' | 'unknown';
	detail: string;
}
