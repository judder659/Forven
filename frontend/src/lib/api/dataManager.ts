/**
 * Data Manager API client for the `/data-next` page: one function per endpoint
 * in docs/data-manager-next/CONTRACT.md. Wire shapes live in ./dataManagerTypes.
 *
 * Every call goes through fetchApi (auth headers, base discovery, ApiError with
 * the HTTP status). A 404 whose detail is the framework's bare "Not Found" means
 * the route is not deployed yet: see `isRouteMissingError` in ./core.
 */
import { API_BASE, LONG_TIMEOUT_MS, fetchApi, fetchApiStream } from './core';
import type {
	CatalogResponse,
	CollectorStatus,
	DataJob,
	DataJobKind,
	DataJobList,
	DataJobStatus,
	DataJobSummary,
	DataLogEntry,
	DataLogResponse,
	DataStream,
	DeleteCheck,
	DeleteRequest,
	DeleteResult,
	DownloadEstimateResponse,
	DownloadJobsResponse,
	DownloadRequestItem,
	GapsResponse,
	HistoryExtendRequest,
	IdentityAuditResponse,
	IdentityResolveResponse,
	ImportPreview,
	ImportRequestFields,
	ImportResult,
	BarsResponse,
	ReclaimRequest,
	RowsResponse,
	SeriesDetail,
	SeriesKey,
	SlaCensus,
	SlaFreezeRequest,
	SlaRefreshRequest,
	SlaState,
	SlaTier,
	StorageInventory,
	StreamPointsResponse,
	TrashResponse,
	UniversePlanDiff,
	UniverseSeedResponse,
	VenueKey,
	VenueTargetsResponse,
	VenuesResponse,
} from './dataManagerTypes';

export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];

/** `?a=1&b=x` from params. Empty values are dropped; arrays repeat the key
 * (`?tier=live&tier=paper`, FastAPI's `list[str] = Query()` convention). */
export function toQuery(params: Record<string, QueryValue>): string {
	const search = new URLSearchParams();
	for (const [key, value] of Object.entries(params)) {
		if (value === undefined || value === null || value === '') continue;
		if (Array.isArray(value)) {
			for (const item of value) search.append(key, String(item));
			continue;
		}
		search.set(key, String(value));
	}
	const text = search.toString();
	return text ? `?${text}` : '';
}

const seg = (value: string) => encodeURIComponent(value);
const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });

/** Cancel/retry answer with the job row; accept `{ job }` as well. */
function jobOf(payload: DataJob | { job: DataJob }): DataJob {
	return 'job' in payload && payload.job ? payload.job : (payload as DataJob);
}

// ---------------------------------------------------------------- catalog (D)

export interface CatalogQuery {
	q?: string;
	stream?: DataStream[];
	venue?: string[];
	tier?: SlaTier[];
	state?: SlaState[];
	asset_class?: string[];
	timeframe?: string[];
	sort?: 'priority' | 'symbol' | 'last_ts' | 'completeness' | 'quality' | 'size' | 'rows' | 'consumers';
	order?: 'asc' | 'desc';
	limit?: number;
	offset?: number;
}

export function getCatalog(query: CatalogQuery = {}, signal?: AbortSignal): Promise<CatalogResponse> {
	return fetchApi(`/data/catalog${toQuery({ ...query })}`, { signal });
}

export interface SeriesRef {
	symbol: string;
	timeframe: string;
	stream?: DataStream;
	venue?: VenueKey;
}

function seriesPath(ref: Pick<SeriesRef, 'symbol' | 'timeframe'>): string {
	return `/data/series/${seg(ref.symbol)}/${seg(ref.timeframe)}`;
}

/** Stream is sent only when it is not the default (candles). */
function streamParam(stream?: DataStream): DataStream | undefined {
	return stream && stream !== 'ohlcv' ? stream : undefined;
}

export function getSeriesDetail(ref: SeriesRef, signal?: AbortSignal): Promise<SeriesDetail> {
	return fetchApi(`${seriesPath(ref)}${toQuery({ stream: ref.stream ?? 'ohlcv', venue: ref.venue ?? 'canonical' })}`, { signal });
}

export interface WindowQuery {
	start?: string | null;
	end?: string | null;
	max_points?: number;
}

export function getSeriesBars(ref: SeriesRef, window: WindowQuery = {}, signal?: AbortSignal): Promise<BarsResponse> {
	return fetchApi(
		`${seriesPath(ref)}/bars${toQuery({ venue: ref.venue ?? 'canonical', start: window.start, end: window.end, max_points: window.max_points ?? 1500 })}`,
		{ signal },
	);
}

export function getSeriesGaps(ref: SeriesRef, page: { limit?: number; offset?: number } = {}, signal?: AbortSignal): Promise<GapsResponse> {
	return fetchApi(
		`${seriesPath(ref)}/gaps${toQuery({ venue: ref.venue ?? 'canonical', stream: streamParam(ref.stream), limit: page.limit, offset: page.offset })}`,
		{ signal },
	);
}

export function getSeriesRows(
	ref: SeriesRef,
	query: { start?: string | null; end?: string | null; limit?: number; offset?: number; order?: 'asc' | 'desc' } = {},
	signal?: AbortSignal,
): Promise<RowsResponse> {
	return fetchApi(
		`${seriesPath(ref)}/rows${toQuery({
			venue: ref.venue ?? 'canonical',
			stream: streamParam(ref.stream),
			start: query.start,
			end: query.end,
			limit: query.limit,
			offset: query.offset,
			order: query.order,
		})}`,
		{ signal },
	);
}

export function getStreamPoints(
	symbol: string,
	stream: DataStream,
	query: WindowQuery & { timeframe?: string; venue?: VenueKey } = {},
	signal?: AbortSignal,
): Promise<StreamPointsResponse> {
	return fetchApi(
		`/data/streams/${seg(symbol)}/${seg(stream)}/points${toQuery({
			timeframe: query.timeframe,
			venue: query.venue,
			start: query.start,
			end: query.end,
			max_points: query.max_points ?? 1500,
		})}`,
		{ signal },
	);
}

export function resolveIdentity(q: string, signal?: AbortSignal): Promise<IdentityResolveResponse> {
	return fetchApi(`/data/identity/resolve${toQuery({ q })}`, { signal });
}

export function getIdentityAudit(): Promise<IdentityAuditResponse> {
	return fetchApi('/data/identity/audit', { timeoutMs: LONG_TIMEOUT_MS });
}

export function getUniversePlanDiff(): Promise<UniversePlanDiff> {
	return fetchApi('/data/universe/plan-diff');
}

// ---------------------------------------------------------------- freshness (B)

export function getSlaCensus(query: { stream?: DataStream; limit_worst?: number } = {}, signal?: AbortSignal): Promise<SlaCensus> {
	return fetchApi(`/data/sla${toQuery({ stream: query.stream, limit_worst: query.limit_worst })}`, { signal });
}

export function getCollectorStatus(): Promise<CollectorStatus> {
	return fetchApi('/data/collector');
}

export function getVenues(): Promise<VenuesResponse> {
	return fetchApi('/data/venues');
}

export async function refreshSeries(request: SlaRefreshRequest): Promise<DataJob> {
	return jobOf(await fetchApi<{ job: DataJob }>('/data/sla/refresh', json(request)));
}

export function freezeSeries(request: SlaFreezeRequest): Promise<{ updated: number }> {
	return fetchApi('/data/sla/freeze', json(request));
}

// ---------------------------------------------------------------- jobs & operations (C)

export interface JobsQuery {
	status?: DataJobStatus[];
	kind?: DataJobKind[];
	/** Exact origin, or a prefix ending in ":" ("strategy:"). */
	origin?: string;
	routine?: boolean;
	symbol?: string;
	since?: string;
	limit?: number;
	offset?: number;
}

export function listJobs(query: JobsQuery = {}, signal?: AbortSignal): Promise<DataJobList> {
	return fetchApi(`/data/jobs${toQuery({ ...query })}`, { signal });
}

export function getJobsSummary(signal?: AbortSignal): Promise<DataJobSummary> {
	return fetchApi('/data/jobs/summary', { signal });
}

export function getJob(id: string): Promise<DataJob> {
	return fetchApi(`/data/jobs/${seg(id)}`);
}

export async function cancelJob(id: string): Promise<DataJob> {
	return jobOf(await fetchApi<DataJob | { job: DataJob }>(`/data/jobs/${seg(id)}/cancel`, { method: 'POST' }));
}

export async function retryJob(id: string): Promise<DataJob> {
	return jobOf(await fetchApi<DataJob | { job: DataJob }>(`/data/jobs/${seg(id)}/retry`, { method: 'POST' }));
}

export async function extendHistory(request: HistoryExtendRequest): Promise<DataJob> {
	return jobOf(await fetchApi<{ job: DataJob }>('/data/history/extend', json(request)));
}

export function seedUniverse(): Promise<UniverseSeedResponse> {
	return fetchApi('/data/universe/seed', { method: 'POST' });
}

export function getStorage(): Promise<StorageInventory> {
	return fetchApi('/data/storage', { timeoutMs: LONG_TIMEOUT_MS });
}

export async function reclaimStorage(request: ReclaimRequest): Promise<DataJob> {
	return jobOf(await fetchApi<{ job: DataJob }>('/data/storage/reclaim', json(request)));
}

export function getTrash(): Promise<TrashResponse> {
	return fetchApi('/data/trash');
}

export function restoreTrashItem(id: string): Promise<unknown> {
	return fetchApi(`/data/trash/${seg(id)}/restore`, { method: 'POST' });
}

/** Empties the trash now (the whole trash, or the listed items). */
export function purgeTrash(request: { item_ids?: string[] | 'all'; confirm: string }): Promise<unknown> {
	return fetchApi('/data/trash/purge', json(request));
}

export function checkDelete(key: SeriesKey, signal?: AbortSignal): Promise<DeleteCheck> {
	return fetchApi(
		`/data/delete/check${toQuery({ symbol: key.symbol, timeframe: key.timeframe, stream: key.stream, venue: key.venue })}`,
		{ signal },
	);
}

export function deleteSeries(request: DeleteRequest): Promise<DeleteResult> {
	return fetchApi('/data/delete', json(request));
}

export interface DataLogQuery {
	category?: DataLogEntry['category'][];
	level?: DataLogEntry['level'][];
	symbol?: string;
	action?: string;
	since?: string;
	until?: string;
	q?: string;
	limit?: number;
	offset?: number;
}

export function getDataLog(query: DataLogQuery = {}, signal?: AbortSignal): Promise<DataLogResponse> {
	return fetchApi(`/data/log${toQuery({ ...query })}`, { signal });
}

/** The CSV export of the log with the same filters (paging is ignored). */
export function dataLogExportUrl(query: DataLogQuery = {}): string {
	const { limit: _limit, offset: _offset, ...filters } = query;
	return `${API_BASE}/data/log/export${toQuery({ ...filters })}`;
}

/** Download the CSV export with the app's auth headers. */
export async function exportDataLog(query: DataLogQuery = {}): Promise<Blob> {
	const { limit: _limit, offset: _offset, ...filters } = query;
	const response = await fetchApiStream(`/data/log/export${toQuery({ ...filters })}`, { method: 'GET', timeoutMs: LONG_TIMEOUT_MS });
	return response.blob();
}

// ---------------------------------------------------------------- acquisition (A)

export function getAcquireTargets(symbol: string, signal?: AbortSignal): Promise<VenueTargetsResponse> {
	return fetchApi(`/data/acquire/targets${toQuery({ symbol })}`, { signal });
}

export function estimateDownloads(items: DownloadRequestItem[], signal?: AbortSignal): Promise<DownloadEstimateResponse> {
	return fetchApi('/data/acquire/estimate', { ...json({ items }), signal });
}

export function startDownloads(items: DownloadRequestItem[]): Promise<DownloadJobsResponse> {
	return fetchApi('/data/acquire/downloads', json({ items }));
}

/** Optional parsing hints the user set in the import wizard. */
export interface ImportParsing {
	symbol?: string;
	timeframe?: string;
	timestamp_column?: string;
	date_format?: string;
	timezone?: string;
	/** { timestamp, open, high, low, close, volume } -> file column */
	mapping?: Record<string, string | null>;
}

function importForm(file: File, fields: Record<string, string | undefined>, mapping?: Record<string, string | null>): FormData {
	const form = new FormData();
	form.append('file', file);
	for (const [key, value] of Object.entries(fields)) {
		if (value !== undefined && value !== '') form.append(key, value);
	}
	if (mapping && Object.values(mapping).some(Boolean)) form.append('mapping_json', JSON.stringify(mapping));
	return form;
}

// fetchApi leaves the multipart Content-Type to the browser for a FormData body
// and adds the auth headers (the legacy uploadCSV path does not).
export function previewImport(file: File, parsing: ImportParsing = {}): Promise<ImportPreview> {
	const { mapping, ...fields } = parsing;
	return fetchApi('/data/acquire/import/preview', {
		method: 'POST',
		body: importForm(file, fields, mapping),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}

export function commitImport(file: File, request: ImportRequestFields, parsing: Omit<ImportParsing, 'symbol' | 'timeframe'> = {}): Promise<ImportResult> {
	const { mapping, ...fields } = parsing;
	return fetchApi('/data/acquire/import', {
		method: 'POST',
		body: importForm(file, { ...fields, ...request }, mapping),
		timeoutMs: LONG_TIMEOUT_MS,
	});
}
