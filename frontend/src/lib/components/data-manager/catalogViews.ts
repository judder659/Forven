/**
 * Catalog filters, their URL form, saved views and the column chooser.
 * Filtering itself happens on the server; this only carries the choices.
 */
import type { CatalogQuery } from '$lib/api/dataManager';

export type SortKey = NonNullable<CatalogQuery['sort']>;
export type FacetKey = 'stream' | 'venue' | 'tier' | 'state' | 'asset_class' | 'timeframe';

export interface CatalogFilters {
	q: string;
	stream: string[];
	venue: string[];
	tier: string[];
	state: string[];
	asset_class: string[];
	timeframe: string[];
	sort: SortKey;
	order: 'asc' | 'desc';
}

export const FACETS: FacetKey[] = ['tier', 'state', 'stream', 'timeframe', 'venue', 'asset_class'];
export const SORTS: SortKey[] = ['priority', 'symbol', 'last_ts', 'completeness', 'quality', 'size', 'rows', 'consumers'];
export const SORT_LABEL: Record<SortKey, string> = {
	priority: 'Most overdue',
	symbol: 'Symbol',
	last_ts: 'Last bar',
	completeness: 'Completeness',
	quality: 'Quality',
	size: 'Size',
	rows: 'Rows',
	consumers: 'Consumers',
};
/** The natural direction of each sort (symbol A→Z, everything else biggest first). */
export const defaultOrder = (sort: SortKey): 'asc' | 'desc' => (sort === 'symbol' ? 'asc' : 'desc');

export const EMPTY_FILTERS: CatalogFilters = {
	q: '',
	stream: [],
	venue: [],
	tier: [],
	state: [],
	asset_class: [],
	timeframe: [],
	sort: 'priority',
	order: 'desc',
};

export function filtersFromParams(params: URLSearchParams): CatalogFilters {
	const list = (key: FacetKey) => params.getAll(key).flatMap((value) => value.split(',')).map((v) => v.trim()).filter(Boolean);
	const sortParam = params.get('sort') as SortKey | null;
	const sort = sortParam && SORTS.includes(sortParam) ? sortParam : 'priority';
	const orderParam = params.get('order');
	return {
		q: params.get('q')?.trim() ?? '',
		stream: list('stream'),
		venue: list('venue'),
		tier: list('tier'),
		state: list('state'),
		asset_class: list('asset_class'),
		timeframe: list('timeframe'),
		sort,
		order: orderParam === 'asc' || orderParam === 'desc' ? orderParam : defaultOrder(sort),
	};
}

/** URL search params with defaults left out (so a clean catalog has a clean URL). */
export function filtersToParams(filters: CatalogFilters): URLSearchParams {
	const params = new URLSearchParams();
	if (filters.q.trim()) params.set('q', filters.q.trim());
	for (const key of FACETS) for (const value of filters[key]) params.append(key, value);
	if (filters.sort !== 'priority') params.set('sort', filters.sort);
	if (filters.order !== defaultOrder(filters.sort)) params.set('order', filters.order);
	return params;
}

export function filtersToQuery(filters: CatalogFilters, page: { limit: number; offset: number }): CatalogQuery {
	return {
		q: filters.q.trim() || undefined,
		stream: filters.stream as CatalogQuery['stream'],
		venue: filters.venue,
		tier: filters.tier as CatalogQuery['tier'],
		state: filters.state as CatalogQuery['state'],
		asset_class: filters.asset_class,
		timeframe: filters.timeframe,
		sort: filters.sort,
		order: filters.order,
		limit: page.limit,
		offset: page.offset,
	};
}

export function toggleValue(list: string[], value: string): string[] {
	return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export function activeFilterCount(filters: CatalogFilters): number {
	return FACETS.reduce((sum, key) => sum + filters[key].length, 0) + (filters.q.trim() ? 1 : 0);
}

// ---------------------------------------------------------------- saved views

export interface SavedView {
	id: string;
	name: string;
	filters: Partial<CatalogFilters>;
	builtin?: boolean;
}

export const BUILTIN_VIEWS: SavedView[] = [
	{ id: 'live-paper', name: 'Live & paper', filters: { tier: ['live', 'paper'] }, builtin: true },
	{ id: 'problems', name: 'Problems', filters: { state: ['late', 'breach', 'missing'] }, builtin: true },
	{ id: 'research', name: 'Research universe', filters: { tier: ['universe'] }, builtin: true },
	{ id: 'intraday', name: 'Intraday', filters: { timeframe: ['1m', '5m', '15m', '30m'] }, builtin: true },
	{ id: 'everything', name: 'Everything', filters: {}, builtin: true },
];

const VIEWS_KEY = 'forven.dataManager.views_v1';
const COLUMNS_KEY = 'forven.dataManager.columns_v1';

function storage(): Storage | null {
	try {
		return typeof window !== 'undefined' ? window.localStorage : null;
	} catch {
		return null;
	}
}

/** The built-in views plus the user's own, in the order they were saved. */
export function loadViews(store: Storage | null = storage()): SavedView[] {
	let custom: SavedView[] = [];
	try {
		const raw = store?.getItem(VIEWS_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		if (Array.isArray(parsed)) {
			custom = parsed.filter((v): v is SavedView => !!v && typeof v.id === 'string' && typeof v.name === 'string' && typeof v.filters === 'object' && !v.builtin);
		}
	} catch {
		custom = [];
	}
	return [...BUILTIN_VIEWS, ...custom];
}

export function saveCustomViews(views: SavedView[], store: Storage | null = storage()): void {
	try {
		store?.setItem(VIEWS_KEY, JSON.stringify(views.filter((v) => !v.builtin)));
	} catch {
		// Storage full or blocked: views last for this session only.
	}
}

/** A view from the current filters (search text and sort included). */
export function viewFromFilters(name: string, filters: CatalogFilters): SavedView {
	const kept: Partial<CatalogFilters> = {};
	if (filters.q.trim()) kept.q = filters.q.trim();
	for (const key of FACETS) if (filters[key].length) kept[key] = [...filters[key]];
	if (filters.sort !== 'priority') kept.sort = filters.sort;
	if (filters.order !== defaultOrder(filters.sort)) kept.order = filters.order;
	const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'view';
	return { id: `custom-${slug}-${Date.now().toString(36)}`, name: name.trim(), filters: kept };
}

export function applyView(view: SavedView): CatalogFilters {
	const sort = view.filters.sort ?? 'priority';
	return { ...EMPTY_FILTERS, ...structuredCloneSafe(view.filters), sort, order: view.filters.order ?? defaultOrder(sort) };
}

function structuredCloneSafe<T>(value: T): T {
	return JSON.parse(JSON.stringify(value));
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((v) => b.includes(v));

/** True when the filters are exactly what the view selects. */
export function viewMatches(view: SavedView, filters: CatalogFilters): boolean {
	const target = applyView(view);
	return (
		target.q === filters.q.trim() &&
		FACETS.every((key) => sameSet(target[key], filters[key])) &&
		target.sort === filters.sort &&
		target.order === filters.order
	);
}

// ---------------------------------------------------------------- columns

export type ColumnKey = 'tf' | 'stream' | 'history' | 'completeness' | 'freshness' | 'quality' | 'consumers' | 'size' | 'updated';

export interface ColumnDef {
	key: ColumnKey;
	label: string;
	/** CSS grid track. */
	width: string;
	title: string;
	sort?: SortKey;
	align?: 'right';
}

export const COLUMNS: ColumnDef[] = [
	{ key: 'tf', label: 'TF', width: '52px', title: 'Timeframe (bar size)' },
	{ key: 'stream', label: 'Stream', width: '104px', title: 'What the series holds: candles or an enrichment stream' },
	{ key: 'history', label: 'History', width: 'minmax(170px,1.3fr)', title: 'First and last stored bar (UTC) and the span between them', sort: 'last_ts' },
	{ key: 'completeness', label: 'Complete', width: '104px', title: 'Stored bars ÷ the bars the span implies', sort: 'completeness' },
	{ key: 'freshness', label: 'Freshness', width: '150px', title: 'The server’s freshness state, and how far behind the series is against what its tier allows', sort: 'priority' },
	{ key: 'quality', label: 'Quality', width: '70px', title: 'Quality score 0–100 (completeness, gaps, invalid bars, outliers). Freshness is not part of it.', sort: 'quality', align: 'right' },
	{ key: 'consumers', label: 'Used by', width: 'minmax(120px,1fr)', title: 'Strategies, bots and workflows that read the series', sort: 'consumers' },
	{ key: 'size', label: 'Size', width: '76px', title: 'Size on disk', sort: 'size', align: 'right' },
	{ key: 'updated', label: 'Updated', width: '86px', title: 'Last write to the series files', align: 'right' },
];

export const DEFAULT_COLUMNS: ColumnKey[] = COLUMNS.map((c) => c.key);

export function loadColumns(store: Storage | null = storage()): ColumnKey[] {
	try {
		const raw = store?.getItem(COLUMNS_KEY);
		const parsed = raw ? JSON.parse(raw) : null;
		if (Array.isArray(parsed)) {
			const valid = parsed.filter((key): key is ColumnKey => COLUMNS.some((c) => c.key === key));
			if (valid.length) return valid;
		}
	} catch {
		// fall through to defaults
	}
	return [...DEFAULT_COLUMNS];
}

export function saveColumns(columns: ColumnKey[], store: Storage | null = storage()): void {
	try {
		store?.setItem(COLUMNS_KEY, JSON.stringify(columns));
	} catch {
		// Session-only when storage is unavailable.
	}
}

/** The CSS grid template for the visible columns (checkbox and symbol first). */
export function gridTemplate(columns: ColumnKey[]): string {
	return ['28px', 'minmax(180px,1.4fr)', ...COLUMNS.filter((c) => columns.includes(c.key)).map((c) => c.width)].join(' ');
}
