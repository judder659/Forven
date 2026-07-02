import type { Dataset } from './types';
import {
	API_BASE,
	ApiError,
	LONG_TIMEOUT_MS,
	fetchApi,
	fetchWithLimit,
	isNotFoundError,
} from './core';

const INGESTION_POLL_INTERVAL_MS = 1_500;

export interface FetchDataProgress {
	mode: 'direct' | 'ingestion';
	message: string;
	run?: IngestionRun;
}

function createAbortError(signal?: AbortSignal): Error {
	if (signal?.reason instanceof Error) return signal.reason;
	try {
		return new DOMException('The operation was aborted', 'AbortError');
	} catch {
		const error = new Error('The operation was aborted');
		error.name = 'AbortError';
		return error;
	}
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(createAbortError(signal));
			return;
		}
		const timeout = setTimeout(() => {
			signal?.removeEventListener('abort', onAbort);
			resolve();
		}, ms);
		const onAbort = () => {
			clearTimeout(timeout);
			signal?.removeEventListener('abort', onAbort);
			reject(createAbortError(signal));
		};
		signal?.addEventListener('abort', onAbort, { once: true });
	});
}

function formatFetchedBars(value: number | null | undefined): string {
	return Number.isFinite(value) ? Math.max(0, Number(value)).toLocaleString() : '0';
}

// The supervised backend auto-relaunches ~12s after a crash; tolerate that gap
// instead of killing an in-flight download with a bare "Internal Server Error".
const TRANSIENT_POLL_GRACE_MS = 45_000;

function isAbortLike(err: unknown): boolean {
	return err instanceof Error && err.name === 'AbortError';
}

function isTransientServerError(err: unknown): boolean {
	if (isAbortLike(err)) return false;
	if (err instanceof ApiError) return err.status >= 500;
	// Network-level failure (backend down / restarting) surfaces as a plain fetch error.
	return err instanceof Error;
}

export function shouldUseBackgroundIngestion(options: { allAvailable?: boolean }): boolean {
	return Boolean(options.allAvailable);
}

// Data endpoints
export async function getSymbols(): Promise<string[]> {
	return fetchApi('/symbols');
}

export async function getDatasets(): Promise<Dataset[]> {
	return fetchApi('/datasets');
}

export async function fetchData(
	symbol: string,
	timeframe: string,
	exchange: string = 'binance',
	limit: number = 1000,
	signal?: AbortSignal,
	since?: string,
	allAvailable: boolean = false,
	until?: string,
	onProgress?: (progress: FetchDataProgress) => void
): Promise<Dataset> {
	if (shouldUseBackgroundIngestion({ allAvailable })) {
		onProgress?.({
			mode: 'ingestion',
			message: `Queueing ${symbol} ${timeframe}...`,
		});
		let submittedRun: IngestionRun | undefined;
		for (let attempt = 1; ; attempt += 1) {
			try {
				submittedRun = await submitIngestion(
					symbol,
					timeframe,
					exchange,
					limit,
					since,
					until,
					allAvailable,
					signal
				);
				break;
			} catch (err) {
				if (attempt >= 3 || !isTransientServerError(err)) throw err;
				onProgress?.({
					mode: 'ingestion',
					message: `Backend briefly unavailable — retrying ${symbol} ${timeframe} (${attempt}/3)...`,
				});
				await delay(4_000 * attempt, signal);
			}
		}
		const runId = String(submittedRun?.id ?? '').trim();
		if (!runId) {
			throw new ApiError(500, `Ingestion queue did not return a run id for ${symbol} ${timeframe}`);
		}

		let firstPollFailureAt: number | null = null;
		while (true) {
			if (signal?.aborted) {
				throw createAbortError(signal);
			}

			let run: IngestionRun;
			try {
				run = await getIngestionRun(runId, signal);
				firstPollFailureAt = null;
			} catch (err) {
				if (signal?.aborted || isAbortLike(err)) throw err;
				if (isNotFoundError(err)) {
					// Queued runs live in backend memory; a restart loses them.
					throw new ApiError(
						503,
						`The backend restarted while downloading ${symbol} ${timeframe} and the queued run was lost. ` +
							`Already-saved bars are kept — click Fetch Data again to resume incrementally.`
					);
				}
				if (!isTransientServerError(err)) throw err;
				firstPollFailureAt = firstPollFailureAt ?? Date.now();
				if (Date.now() - firstPollFailureAt > TRANSIENT_POLL_GRACE_MS) {
					throw new ApiError(
						503,
						`Lost contact with the backend while downloading ${symbol} ${timeframe} (it may be restarting). ` +
							`Already-saved bars are kept — click Fetch Data again to resume incrementally.`
					);
				}
				onProgress?.({
					mode: 'ingestion',
					message: `Backend briefly unavailable — waiting to resume ${symbol} ${timeframe}...`,
				});
				await delay(INGESTION_POLL_INTERVAL_MS, signal);
				continue;
			}
			if (run.status === 'failed') {
				throw new ApiError(500, run.error || `Fetch failed for ${symbol} ${timeframe}`, run);
			}

			if (run.status === 'completed' || run.status === 'skipped') {
				onProgress?.({
					mode: 'ingestion',
					message: `Finalizing ${symbol} ${timeframe}...`,
					run,
				});
				return getDatasetDetail(symbol, timeframe, signal);
			}

			const statusLabel = run.status === 'pending' ? 'Queued' : 'Downloading';
			onProgress?.({
				mode: 'ingestion',
				message: `${statusLabel} ${symbol} ${timeframe} (${formatFetchedBars(run.bars_fetched)} bars fetched)`,
				run,
			});
			await delay(INGESTION_POLL_INTERVAL_MS, signal);
		}
	}

	const params = new URLSearchParams({
		symbol,
		timeframe,
		exchange
	});
	if (allAvailable) {
		params.set('all_available', 'true');
	} else {
		params.set('limit', String(limit));
		if (since) params.set('since', since);
		if (until) params.set('until', until);
	}
	const url = `/fetch?${params.toString()}`;

	const isWriteConflict = (err: unknown): boolean => {
		if (!(err instanceof ApiError)) return false;
		const msg = String(err.message || '').toLowerCase();
		return msg.includes('write-write conflict') || (
			msg.includes('transactioncontext error') && msg.includes('failed to commit')
		);
	};

	const maxAttempts = 6;
	for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
		try {
			return await fetchApi(url, { method: 'POST', signal: signal ?? AbortSignal.timeout(LONG_TIMEOUT_MS) });
		} catch (err) {
			if (!isWriteConflict(err) || attempt >= maxAttempts) throw err;
			const delayMs = Math.min(2000, 250 * attempt);
			await new Promise((resolve) => setTimeout(resolve, delayMs));
		}
	}

	throw new ApiError(500, 'Failed to fetch data after retries');
}

export async function submitIngestion(
	symbol: string,
	timeframe: string,
	exchange: string = 'binance',
	limit: number = 1000,
	since?: string,
	until?: string,
	allAvailable: boolean = false,
	signal?: AbortSignal
): Promise<IngestionRun> {
	const params = new URLSearchParams({
		symbol,
		timeframe,
		exchange
	});
	if (allAvailable) {
		params.set('all_available', 'true');
	} else {
		params.set('limit', String(limit));
		if (since) params.set('since', since);
		if (until) params.set('until', until);
	}
	const url = `/data/ingestion/submit?${params.toString()}`;
	return fetchApi(url, { method: 'POST', signal });
}

export interface OHLCVBar {
	timestamp: string;
	open: number;
	high: number;
	low: number;
	close: number;
	volume: number;
}

export interface OHLCVResponse {
	symbol: string;
	timeframe: string;
	source: string;
	is_fallback?: boolean;
	start: string;
	end: string;
	row_count: number;
	data: OHLCVBar[];
}

export async function getOHLCV(
	symbol: string,
	timeframe: string,
	limit: number = 100
): Promise<OHLCVBar[]> {
	const safeSymbol = encodeURIComponent(symbol);
	const safeTf = encodeURIComponent(timeframe);
	try {
		const response: OHLCVResponse = await fetchApi(`/datasets/${symbol}/${safeTf}/ohlcv?limit=${limit}`);
		if (Array.isArray(response?.data)) return response.data;
	} catch {
		// Fallback for lightweight backends that expose query-based OHLCV.
	}
	const response: OHLCVResponse = await fetchApi(`/ohlcv?symbol=${safeSymbol}&timeframe=${safeTf}&limit=${limit}`);
	return Array.isArray(response?.data) ? response.data : [];
}

/** Like getOHLCV but returns the full response so callers can read provenance
 * (source / is_fallback), not just the bars. Used by the series drill-down. */
export async function getSeriesOHLCV(
	symbol: string,
	timeframe: string,
	limit: number = 200
): Promise<OHLCVResponse> {
	const safeSymbol = encodeURIComponent(symbol);
	const safeTf = encodeURIComponent(timeframe);
	try {
		const response: OHLCVResponse = await fetchApi(`/datasets/${symbol}/${safeTf}/ohlcv?limit=${limit}`);
		if (Array.isArray(response?.data)) return response;
	} catch {
		// Fallback for lightweight backends that expose query-based OHLCV.
	}
	return fetchApi(`/ohlcv?symbol=${safeSymbol}&timeframe=${safeTf}&limit=${limit}`);
}

// Dataset download functions
function exportFailureMessage(status: number, body: string): string {
	const trimmed = (body || '').trim();
	// A bare proxy/ASGI "Internal Server Error" almost always means the supervised
	// backend is mid-restart — say so instead of echoing the unhelpful default.
	if (status >= 500 && (!trimmed || /internal server error/i.test(trimmed))) {
		return `Backend unavailable (HTTP ${status}) — it may be restarting. Try again in ~15 seconds.`;
	}
	return trimmed || 'Download failed';
}

export async function downloadDataset(symbol: string, timeframe: string, format: 'csv' | 'parquet' = 'csv'): Promise<Blob> {
	const safeSymbol = encodeURIComponent(symbol);
	const response = await fetchWithLimit(`${API_BASE}/datasets-export/${safeSymbol}/${timeframe}/download?format=${format}`, {
		method: 'GET',
		timeoutMs: LONG_TIMEOUT_MS,
	});
	if (!response.ok) {
		const error = await response.text();
		throw new Error(exportFailureMessage(response.status, error));
	}
	return response.blob();
}

export async function downloadAllTimeframes(symbol: string, format: 'csv' | 'parquet' = 'csv'): Promise<Blob> {
	const safeSymbol = encodeURIComponent(symbol);
	const response = await fetchWithLimit(`${API_BASE}/datasets-export/${safeSymbol}/download-all?format=${format}`, {
		method: 'GET',
		timeoutMs: LONG_TIMEOUT_MS,
	});
	if (!response.ok) {
		const error = await response.text();
		throw new Error(exportFailureMessage(response.status, error));
	}
	return response.blob();
}

export interface DataQuality {
	symbol: string;
	timeframe: string;
	row_count: number;
	start: string;
	end: string;
	duration_days: number;
	gaps: number;
	null_values: number;
	price_range: {
		min: number;
		max: number;
	};
	volume_stats: {
		min: number;
		max: number;
		avg: number;
	};
}

export async function getDataQuality(symbol: string, timeframe: string): Promise<DataQuality> {
	const params = new URLSearchParams();
	params.set('symbol', symbol);
	params.set('timeframe', timeframe);
	return fetchApi(`/data/quality?${params}`);
}


// ============== Multi-Source Data ==============

export interface DataSource {
	id: string;
	name: string;
	description: string;
	asset_types: string[];
	available: boolean;
	requires_key: boolean;
	requires_tws?: boolean;
}

export interface SourceSymbol {
	symbol: string;
	name?: string;
	type?: string;
	exchange?: string;
	base?: string;
	quote?: string;
	active?: boolean;
}

export interface DataQualityExtended {
	symbol: string;
	timeframe: string;
	row_count: number;
	start: string;
	end: string;
	duration_days: number;
	gaps: number;
	gap_details: Array<{ timestamp: string; gap_size: string }>;
	null_values: number;
	price_range: {
		min: number;
		max: number;
	};
	volume_stats: {
		min: number;
		max: number;
		avg: number;
	};
	outliers: {
		close: number;
		volume: number;
	};
	integrity: {
		invalid_high_low: number;
		invalid_close_range: number;
	};
	freshness: {
		last_update: string;
		hours_ago: number;
		is_stale: boolean;
	};
}

export interface CSVPreview {
	columns: string[];
	row_count: number;
	detected_timestamp_column: string | null;
	has_required_columns: {
		open: boolean;
		high: boolean;
		low: boolean;
		close: boolean;
		volume: boolean;
	};
	suggested_mapping: Record<string, string>;
	sample_data: Array<Record<string, unknown>>;
}

// List available data sources
export async function getDataSources(): Promise<DataSource[]> {
	const normalize = (items: unknown[]): DataSource[] =>
		(items || []).map((item) => {
			const source = (item && typeof item === 'object') ? item as Record<string, unknown> : {};
			return {
				id: String(source.id ?? ''),
				name: String(source.name ?? source.id ?? 'Unknown'),
				description: String(source.description ?? ''),
				asset_types: Array.isArray(source.asset_types) ? source.asset_types.map((x) => String(x)) : [],
				available: Boolean(source.available ?? true),
				requires_key: Boolean(source.requires_key ?? false),
				requires_tws: source.requires_tws === undefined ? undefined : Boolean(source.requires_tws),
			};
		});

	try {
		const legacy = await fetchApi<unknown[]>('/sources');
		return normalize(legacy);
	} catch {
		const current = await fetchApi<unknown[]>('/data/sources');
		return normalize(current);
	}
}

// Get symbols for a specific source. `exchange` scopes a ccxt search to the
// selected exchange (ignored for sources that aren't exchange-specific).
export async function getSourceSymbols(
	source: string,
	query?: string,
	exchange?: string
): Promise<SourceSymbol[]> {
	const params = new URLSearchParams();
	if (query) params.set('query', query);
	if (exchange) params.set('exchange', exchange);
	const queryStr = params.toString();
	try {
		return await fetchApi(`/sources/${source}/symbols${queryStr ? '?' + queryStr : ''}`);
	} catch {
		if (!query) return [];
		const sp = new URLSearchParams({ source, query });
		if (exchange) sp.set('exchange', exchange);
		return fetchApi(`/data/symbols/search?${sp.toString()}`);
	}
}

// Upload CSV data
export async function uploadCSV(
	file: File,
	symbol: string,
	timeframe: string,
	timestampColumn?: string,
	dateFormat?: string
): Promise<Dataset> {
	const formData = new FormData();
	formData.append('file', file);
	formData.append('symbol', symbol);
	formData.append('timeframe', timeframe);
	if (timestampColumn) formData.append('timestamp_column', timestampColumn);
	if (dateFormat) formData.append('date_format', dateFormat);

	const response = await fetchWithLimit(`${API_BASE}/upload/csv`, {
		method: 'POST',
		body: formData,
		timeoutMs: LONG_TIMEOUT_MS,
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
		throw new Error(error.detail || `HTTP ${response.status}`);
	}

	return response.json();
}

// Preview CSV file before upload
export async function previewCSV(file: File): Promise<CSVPreview> {
	const formData = new FormData();
	formData.append('file', file);

	const response = await fetchWithLimit(`${API_BASE}/upload/csv/preview`, {
		method: 'POST',
		body: formData,
		timeoutMs: LONG_TIMEOUT_MS,
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
		throw new Error(error.detail || `HTTP ${response.status}`);
	}

	return response.json();
}

// Get extended data quality metrics
export async function getDataQualityExtended(symbol: string, timeframe: string): Promise<DataQualityExtended> {
	const params = new URLSearchParams();
	params.set('symbol', symbol);
	params.set('timeframe', timeframe);
	return fetchApi(`/data/quality?${params}`);
}

// Delete a dataset
export async function deleteDataset(symbol: string, timeframe: string): Promise<{ status: string; symbol: string; timeframe: string }> {
	return fetchApi(`/datasets/${encodeURIComponent(symbol)}/${timeframe}`, { method: 'DELETE' });
}

// ============== Data Health & Maintenance ==============

export interface DataHealth {
	db_path: string;
	db_size_bytes: number;
	db_exists: boolean;
	wal_present: boolean;
	wal_size_bytes: number;
	dataset_count: number;
	total_parquet_files: number;
	total_parquet_bytes: number;
	last_ingestion_at: string | null;
	last_ingestion_status: string | null;
	orphan_count: number;
	quality_avg_score: number | null;
	checked_at: string;
}

export interface DataEngineStatus {
	enabled?: boolean;
	coverage: Array<Record<string, unknown>>;
	streams: Array<{
		source: string;
		market: string;
		symbol: string;
		stream: string;
		status: string;
		buffered_rows: number;
		updated_at: string;
	}>;
	sources: Array<{
		source: string;
		status: string;
		consecutive_failures: number;
		last_success_at: string | null;
		last_failure_at: string | null;
		message: string;
	}>;
}

export interface DataEngineBackfillPlan {
	task_count: number;
	tasks: Array<{
		source: string;
		market: string;
		symbol: string;
		timeframe: string;
		stream: string;
		start_ts: string;
		end_ts: string;
		permanent: boolean;
	}>;
}

export interface DataEngineBackfillResult {
	planned_total: number;
	candle_total: number;
	executed: number;
	rows_added: number;
	failed: number;
	results: Array<{ symbol: string; timeframe: string; rows_added?: number; stalled?: boolean; error?: string }>;
}

export interface IngestionRun {
	id: string;
	symbol: string;
	timeframe: string;
	source: string;
	status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
	idempotency_key: string | null;
	bars_fetched: number;
	bars_new: number;
	bars_updated: number;
	error: string | null;
	prior_version_id: string | null;
	new_version_id: string | null;
	started_at: string;
	completed_at: string | null;
	duration_ms: number | null;
}

export interface DatasetVersion {
	id: string;
	symbol: string;
	timeframe: string;
	source: string;
	row_count: number;
	start_ts: string;
	end_ts: string;
	checksum: string | null;
	ingestion_run_id: string | null;
	created_at: string;
}

export interface QualityReport {
	id: string;
	symbol: string;
	timeframe: string;
	row_count: number;
	start_ts: string | null;
	end_ts: string | null;
	duration_days: number;
	gaps: number;
	gap_details: Array<{ timestamp: string; gap_size: string }>;
	null_values: number;
	price_range_min: number;
	price_range_max: number;
	volume_min: number;
	volume_max: number;
	volume_avg: number;
	outliers_close: number;
	outliers_volume: number;
	invalid_high_low: number;
	invalid_close_range: number;
	freshness_hours: number;
	is_stale: boolean;
	quality_score: number;
	computed_at: string;
}

export interface OrphanReport {
	orphans: Array<{
		symbol: string;
		timeframe: string;
		path: string;
		size_bytes: number;
		reason?: string;
		safe_delete?: boolean;
	}>;
	cataloged_missing: Array<{ symbol: string; timeframe: string }>;
	scanned_at: string;
}

export interface DatasetDetail extends Dataset {
	updated_at: string | null;
	parquet_exists: boolean;
	checksum: string | null;
}

function parseDateMs(value: string | null | undefined): number {
	if (!value) return 0;
	const t = Date.parse(value);
	return Number.isFinite(t) ? t : 0;
}

function sortDatasetsByRecent(datasets: Dataset[]): Dataset[] {
	return [...datasets].sort((a, b) => parseDateMs(b.end_ts) - parseDateMs(a.end_ts));
}

function toLegacyIngestionRun(ds: Dataset, idx: number): IngestionRun {
	const started = ds.end_ts || ds.start_ts || new Date().toISOString();
	return {
		id: `legacy-run-${idx}-${encodeURIComponent(ds.symbol)}-${ds.timeframe}`,
		symbol: ds.symbol,
		timeframe: ds.timeframe,
		source: ds.source,
		status: 'completed',
		idempotency_key: null,
		bars_fetched: ds.row_count,
		bars_new: ds.row_count,
		bars_updated: 0,
		error: null,
		prior_version_id: null,
		new_version_id: null,
		started_at: started,
		completed_at: started,
		duration_ms: null,
	};
}

function toLegacyDatasetVersion(ds: Dataset, idx: number): DatasetVersion {
	const created = ds.end_ts || ds.start_ts || new Date().toISOString();
	return {
		id: `legacy-ver-${idx}-${encodeURIComponent(ds.symbol)}-${ds.timeframe}`,
		symbol: ds.symbol,
		timeframe: ds.timeframe,
		source: ds.source,
		row_count: ds.row_count,
		start_ts: ds.start_ts,
		end_ts: ds.end_ts,
		checksum: null,
		ingestion_run_id: null,
		created_at: created,
	};
}

function computeFallbackQualityScore(q: DataQualityExtended): number {
	const rowCount = Math.max(1, q.row_count);
	const totalCells = Math.max(1, rowCount * 5);

	let score = 100;
	score -= Math.min(30, (q.gaps / rowCount) * 1000);
	score -= Math.min(20, (q.null_values / totalCells) * 1000);
	score -= Math.min(10, q.integrity.invalid_high_low * 2);
	score -= Math.min(10, q.integrity.invalid_close_range * 2);
	if (q.freshness.is_stale) score -= 10;
	const outlierRatio = (q.outliers.close + q.outliers.volume) / rowCount;
	score -= Math.min(10, outlierRatio * 500);
	return Math.max(0, Math.min(100, Math.round(score * 10) / 10));
}

function toQualityReport(ds: Dataset, q: DataQualityExtended, idx: number): QualityReport {
	const legacyScore = computeFallbackQualityScore(q);
	const payload = q as DataQualityExtended & { quality_score?: number };
	return {
		id: `legacy-quality-${idx}-${encodeURIComponent(ds.symbol)}-${ds.timeframe}`,
		symbol: ds.symbol,
		timeframe: ds.timeframe,
		row_count: q.row_count,
		start_ts: q.start ?? null,
		end_ts: q.end ?? null,
		duration_days: q.duration_days,
		gaps: q.gaps,
		gap_details: q.gap_details ?? [],
		null_values: q.null_values,
		price_range_min: q.price_range.min,
		price_range_max: q.price_range.max,
		volume_min: q.volume_stats.min,
		volume_max: q.volume_stats.max,
		volume_avg: q.volume_stats.avg,
		outliers_close: q.outliers.close,
		outliers_volume: q.outliers.volume,
		invalid_high_low: q.integrity.invalid_high_low,
		invalid_close_range: q.integrity.invalid_close_range,
		freshness_hours: q.freshness.hours_ago,
		is_stale: q.freshness.is_stale,
		quality_score: typeof payload.quality_score === 'number' ? payload.quality_score : legacyScore,
		computed_at: new Date().toISOString(),
	};
}

export async function getDataEngineStatus(): Promise<DataEngineStatus> {
	return fetchApi('/data/engine/status');
}

export async function planDataEngineBackfill(): Promise<DataEngineBackfillPlan> {
	return fetchApi('/data/engine/backfill-plan', { method: 'POST' });
}

// Execute a bounded batch of the catch-up plan (returns `remaining` to paginate).
export async function executeDataEngineBackfill(maxTasks = 10): Promise<DataEngineBackfillResult> {
	return fetchApi(`/data/engine/backfill-execute?max_tasks=${maxTasks}`, { method: 'POST' });
}

export async function getDataHealth(): Promise<DataHealth> {
	try {
		return await fetchApi('/data/health');
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		const datasets = await getDatasets();
		const latest = sortDatasetsByRecent(datasets)[0];
		return {
			db_path: '(legacy backend: health endpoint unavailable)',
			db_size_bytes: 0,
			db_exists: false,
			wal_present: false,
			wal_size_bytes: 0,
			dataset_count: datasets.length,
			total_parquet_files: datasets.length,
			total_parquet_bytes: 0,
			last_ingestion_at: latest?.end_ts ?? null,
			last_ingestion_status: latest ? 'completed' : null,
			orphan_count: 0,
			quality_avg_score: null,
			checked_at: new Date().toISOString(),
		};
	}
}

export async function getIngestionRuns(opts?: {
	symbol?: string;
	status?: string;
	limit?: number;
	offset?: number;
}): Promise<IngestionRun[]> {
	const params = new URLSearchParams();
	if (opts?.symbol) params.set('symbol', opts.symbol);
	if (opts?.status) params.set('status', opts.status);
	if (opts?.limit) params.set('limit', opts.limit.toString());
	if (opts?.offset) params.set('offset', opts.offset.toString());
	const qs = params.toString();
	try {
		return await fetchApi(`/data/ingestion/runs${qs ? '?' + qs : ''}`);
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		let runs = sortDatasetsByRecent(await getDatasets()).map((ds, idx) => toLegacyIngestionRun(ds, idx));
		if (opts?.symbol) runs = runs.filter((r) => r.symbol === opts.symbol);
		if (opts?.status) runs = runs.filter((r) => r.status === opts.status);
		const offset = opts?.offset ?? 0;
		const limit = opts?.limit ?? 50;
		return runs.slice(offset, offset + limit);
	}
}

export async function getIngestionRun(runId: string, signal?: AbortSignal): Promise<IngestionRun> {
	return fetchApi(`/data/ingestion/runs/${runId}`, signal ? { signal } : undefined);
}

export async function getDatasetVersions(opts?: {
	symbol?: string;
	timeframe?: string;
	limit?: number;
}): Promise<DatasetVersion[]> {
	const params = new URLSearchParams();
	if (opts?.symbol) params.set('symbol', opts.symbol);
	if (opts?.timeframe) params.set('timeframe', opts.timeframe);
	if (opts?.limit) params.set('limit', opts.limit.toString());
	const qs = params.toString();
	try {
		return await fetchApi(`/data/versions${qs ? '?' + qs : ''}`);
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		let versions = sortDatasetsByRecent(await getDatasets()).map((ds, idx) => toLegacyDatasetVersion(ds, idx));
		if (opts?.symbol) versions = versions.filter((v) => v.symbol === opts.symbol);
		if (opts?.timeframe) versions = versions.filter((v) => v.timeframe === opts.timeframe);
		return versions.slice(0, opts?.limit ?? 50);
	}
}

// Run an async mapper over `items` with at most `limit` in flight at once. The
// quality-report fallback below MUST NOT fan out one request per dataset — an
// unbounded Promise.all over ~100 datasets saturated the backend threadpool and
// the browser's per-host connection pool, starving the live websocket. Bounding
// concurrency keeps the fallback safe even against a legacy backend.
async function mapWithConcurrency<T, R>(
	items: T[],
	limit: number,
	fn: (item: T, index: number) => Promise<R>
): Promise<R[]> {
	const results = new Array<R>(items.length);
	let cursor = 0;
	const workerCount = Math.max(1, Math.min(limit, items.length));
	async function worker(): Promise<void> {
		while (cursor < items.length) {
			const idx = cursor++;
			results[idx] = await fn(items[idx], idx);
		}
	}
	await Promise.all(Array.from({ length: workerCount }, () => worker()));
	return results;
}

export async function getQualityReports(limit?: number): Promise<QualityReport[]> {
	const params = new URLSearchParams();
	if (limit) params.set('limit', limit.toString());
	const qs = params.toString();
	try {
		return await fetchApi(`/data/quality/reports${qs ? '?' + qs : ''}`);
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		const datasets = sortDatasetsByRecent(await getDatasets()).slice(0, limit ?? 100);
		const reports = await mapWithConcurrency(datasets, 4, async (ds, idx) => {
			try {
				const quality = await getDataQualityExtended(ds.symbol, ds.timeframe);
				return toQualityReport(ds, quality, idx);
			} catch {
				return null;
			}
		});
		return reports.filter((r): r is QualityReport => r !== null);
	}
}

export async function getQualityReport(symbol: string, timeframe: string): Promise<QualityReport> {
	try {
		return await fetchApi(`/data/quality/reports/${encodeURIComponent(symbol)}/${timeframe}`);
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		const quality = await getDataQualityExtended(symbol, timeframe);
		const dataset = (await getDatasets()).find((d) => d.symbol === symbol && d.timeframe === timeframe)
			?? {
			symbol,
			timeframe,
			source: 'unknown',
			start_ts: quality.start,
			end_ts: quality.end,
			row_count: quality.row_count,
		};
		return toQualityReport(dataset, quality, 0);
	}
}

export async function getDatasetDetail(
	symbol: string,
	timeframe: string,
	signal?: AbortSignal
): Promise<DatasetDetail> {
	return fetchApi(
		`/datasets/${encodeURIComponent(symbol)}/${timeframe}`,
		signal ? { signal } : undefined
	);
}

export async function scanOrphans(): Promise<OrphanReport> {
	try {
		return await fetchApi('/data/maintenance/orphans/scan', { method: 'POST' });
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		return {
			orphans: [],
			cataloged_missing: [],
			scanned_at: new Date().toISOString(),
		};
	}
}

export interface OrphanCleanupResult {
	removed: number;
	skipped: number;
	bytes_freed: number;
	scanned: number;
	scanned_at: string;
}

/** Delete the storage-drift artifacts the scan found (leftover temp + empty parquet). */
export async function cleanupOrphans(): Promise<OrphanCleanupResult> {
	return fetchApi('/data/maintenance/orphans/cleanup', { method: 'POST' });
}

// ============== DataManager Stream Health ==============

export interface StreamHealth {
	status: 'live' | 'accumulating' | 'no_data';
	row_count: number;
	last_updated: string | null;
	data_age_hours: number | null;
	timeframe?: string;
}

export interface StreamsResponse {
	symbol: string;
	streams: {
		ohlcv: StreamHealth;
		funding: StreamHealth;
		oi: StreamHealth;
	};
	collection_reason: string | null;
}

export interface ActiveSymbol {
	symbol: string;
	active_strategies: number;
	recent_backtests: number;
}

export async function getStreamHealth(symbol: string): Promise<StreamsResponse> {
	return fetchApi(`/data/streams?symbol=${encodeURIComponent(symbol)}`);
}

export interface StreamRowsResponse {
	symbol: string;
	stream: string;
	timeframe: string | null;
	columns: string[];
	rows: Record<string, unknown>[];
}

// Raw rows for an enrichment stream (funding/oi) — for the data viewer.
export async function getStreamRows(
	symbol: string,
	stream: 'funding' | 'oi',
	timeframe?: string,
	limit = 500
): Promise<StreamRowsResponse> {
	const params = new URLSearchParams({ symbol, stream, limit: String(limit) });
	if (timeframe) params.set('timeframe', timeframe);
	return fetchApi(`/data/stream-rows?${params}`);
}

export async function triggerCollect(symbol: string, stream: string): Promise<{ status: string; rows_added: number }> {
	const params = new URLSearchParams({ symbol, stream });
	return fetchApi(`/data/collect?${params}`, { method: 'POST' });
}

export async function getActiveSymbols(): Promise<ActiveSymbol[]> {
	try {
		return await fetchApi('/data/active-symbols');
	} catch {
		return [];
	}
}

export interface JobProgress {
	done: number;
	total: number;
	current_symbol: string;
}

export interface BackfillStatus {
	running: boolean;
	last_started_at: string | null;
	last_result: Record<string, Record<string, number | string>> | null;
	last_error: string | null;
	progress?: JobProgress | null;
	cancel_requested?: boolean;
}

export async function triggerBackfill(symbol?: string): Promise<{ status: string; symbol: string | null }> {
	const params = symbol ? `?symbol=${encodeURIComponent(symbol)}` : '';
	return fetchApi(`/data/backfill${params}`, { method: 'POST' });
}

export async function getBackfillStatus(): Promise<BackfillStatus> {
	return fetchApi('/data/backfill/status');
}

export async function cancelBackfill(): Promise<{ status: string }> {
	return fetchApi('/data/backfill/cancel', { method: 'POST' });
}

// ---------------------------------------------------------------------------
// Research universe (symbol registry + deep-history seeding)
// ---------------------------------------------------------------------------

export interface SymbolRegistryRow {
	symbol: string;
	market: string;
	status: 'active' | 'delisted' | string;
	inception_ts: string | null;
	delist_ts: string | null;
	quote_volume_24h: number | null;
}

export interface UniversePlanEntry {
	symbol: string;
	rank: number;
	timeframes: string[];
}

export interface UniverseSeedState {
	running: boolean;
	last_started_at: string | null;
	last_result: Record<string, unknown> | null;
	last_error: string | null;
	progress?: JobProgress | null;
}

export interface UniverseConfig {
	enabled?: boolean;
	size?: number;
	base_timeframes?: string[];
	intraday_timeframes?: string[];
	intraday_top?: number;
	minute_top?: number;
	metrics_days?: number;
}

export interface DataUniverse {
	registry_count: number;
	active: number;
	delisted: number;
	registry: SymbolRegistryRow[];
	plan: UniversePlanEntry[];
	seed: UniverseSeedState;
	config?: UniverseConfig;
}

export async function updateUniverseConfig(config: UniverseConfig): Promise<{ config: UniverseConfig }> {
	return fetchApi('/data/universe/config', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(config),
	});
}

export async function getDataUniverse(): Promise<DataUniverse> {
	return fetchApi('/data/universe');
}

export async function refreshUniverseRegistry(): Promise<{ active: number; delisted: number }> {
	return fetchApi('/data/universe/refresh', { method: 'POST' });
}

export async function seedUniverse(): Promise<{ status: string }> {
	return fetchApi('/data/universe/seed', { method: 'POST' });
}

export async function cancelUniverseSeed(): Promise<{ status: string }> {
	return fetchApi('/data/universe/seed/cancel', { method: 'POST' });
}

// ---------------------------------------------------------------------------
// Data-quality gate (the gauntlet's fit-to-score verdict for a series)
// ---------------------------------------------------------------------------

export interface QualityGateVerdict {
	ok: boolean;
	reasons: string[];
	details: Record<string, unknown>;
}

export async function getQualityGate(
	symbol: string,
	timeframe: string,
	windowDays?: number
): Promise<QualityGateVerdict> {
	const params = new URLSearchParams({ symbol, timeframe });
	if (windowDays) params.set('window_days', String(windowDays));
	return fetchApi(`/data/quality-gate?${params}`);
}

export interface DataCoverageEntry {
	rows: number;
	from: string;
	to: string;
	to_ts?: string; // precise ISO timestamp of the last bar (for hour-granular freshness)
}

export type DataCoverage = Record<string, Record<string, DataCoverageEntry>>;

export async function getCoverage(): Promise<DataCoverage> {
	return fetchApi('/data/coverage');
}

export interface BackfillGapsResult {
	symbol: string;
	timeframe: string;
	gaps_found: number;
	gaps_attempted: number;
	gaps_filled: number;
	gaps_remaining: number;
	bars_added: number;
	extended_to_now?: boolean;
	no_recent_data?: boolean;
}

/** Execute a real gap backfill for one stored series (POST /api/data/backfill-gaps). */
export async function backfillGaps(
	symbol: string,
	timeframe: string,
	maxGaps?: number
): Promise<BackfillGapsResult> {
	const params = new URLSearchParams({ symbol, timeframe });
	if (maxGaps != null) params.set('max_gaps', String(maxGaps));
	return fetchApi(`/data/backfill-gaps?${params.toString()}`, { method: 'POST' });
}

export interface CollectionStream {
	stream: string;
	status: 'healthy' | 'recovering' | 'down' | 'never_ran';
	consecutive_failures: number;
	last_success: string | null;
	last_run: string | null;
	last_error: string | null;
	total_rows: number;
}

export interface CollectionHealth {
	score: number;
	streams: CollectionStream[];
}

/** Plain-language per-stream collection health + aggregate score. */
export async function getCollectionHealth(): Promise<CollectionHealth> {
	return fetchApi('/data/collection-health');
}

export interface DataActivityEvent {
	ts: string | null;
	level: 'info' | 'warning' | 'error' | string;
	action: string; // 'download' | 'backfill' | 'source_reconciliation' | 'event'
	message: string;
	detail: Record<string, unknown>;
}

export interface DataActivity {
	events: DataActivityEvent[];
	generated_at: string;
}

/** Unified chronological log of data actions (downloads + backfills + reconciliation). */
export async function getDataActivity(limit = 200): Promise<DataActivity> {
	return fetchApi(`/data/activity?limit=${limit}`);
}
