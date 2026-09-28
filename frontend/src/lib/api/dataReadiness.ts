// Client for strategy data contracts (Data Manager workstream E): readiness
// reports, series value fingerprints and research-vs-execution divergence.
// Wire shapes: dataManagerTypes.ts ("readiness (E)").
import { fetchApi } from './core';
import type {
	DataStream,
	DownloadRequestItem,
	ReadinessReport,
	ReadinessRequirement,
	SeriesFingerprint,
	VenueDivergence,
} from './dataManagerTypes';

/** POST /api/data/readiness body: the data contract of a strategy being written. */
export interface ReadinessQuery {
	symbol: string;
	timeframe: string;
	/** Feeds the strategy reads beyond candles. */
	streams?: DataStream[];
	/** Research window in days (the pipeline's quick-screen window when omitted). */
	history_days?: number;
	/** A registered strategy type: its class decides the feeds and warmup. */
	strategy_type?: string;
	/** Strategy source, scanned server-side for the feed columns it reads (never run). */
	code?: string;
}

const seg = (value: string) => encodeURIComponent(value);

export function getStrategyReadiness(strategyId: string): Promise<ReadinessReport> {
	return fetchApi<ReadinessReport>(`/data/readiness/strategy/${seg(strategyId)}`);
}

export function checkReadiness(query: ReadinessQuery): Promise<ReadinessReport> {
	return fetchApi<ReadinessReport>('/data/readiness', { method: 'POST', body: JSON.stringify(query) });
}

export function getSeriesFingerprint(symbol: string, timeframe: string, venue = 'canonical'): Promise<SeriesFingerprint> {
	return fetchApi<SeriesFingerprint>(
		`/data/series/${seg(symbol)}/${seg(timeframe)}/fingerprint?venue=${seg(venue)}`,
	);
}

export function getVenueDivergence(symbol: string, timeframe = '1h'): Promise<VenueDivergence> {
	return fetchApi<VenueDivergence>(`/data/divergence/${seg(symbol)}?timeframe=${seg(timeframe)}`);
}

/** The Get-data page, pre-filled with a fix's download request:
 *  /data-next/get?symbol=&timeframe=&venue=&history=(all | <N>d | <start>..<end>)&streams=a,b */
export function readinessFixHref(fix: ReadinessRequirement['fix']): string | null {
	const item: DownloadRequestItem | undefined = fix?.request;
	if (!item) return null;
	const history =
		item.history.mode === 'all' ? 'all'
		: item.history.mode === 'days' ? `${item.history.days}d`
		: `${item.history.start}..${item.history.end}`;
	const params = new URLSearchParams({ symbol: item.symbol, timeframe: item.timeframe, venue: item.venue, history });
	if (item.streams?.length) params.set('streams', item.streams.join(','));
	return `/data-next/get?${params.toString()}`;
}

// Feed columns a rule spec can read (rule_engine._ENRICHMENT_COLUMNS), and the
// stream each belongs to (data_availability.FEEDS).
const COLUMN_STREAM: Record<string, DataStream> = {
	funding_rate: 'funding',
	open_interest: 'oi',
	ls_ratio: 'ls_ratio',
	taker_buy_sell_ratio: 'taker',
	long_liq_usd: 'liquidations',
	short_liq_usd: 'liquidations',
	liq_imbalance: 'liquidations',
};
// Indicator kinds computed from a feed (forven/strategies/indicators.py `requires`).
const INDICATOR_STREAM: Record<string, DataStream> = { funding_zscore: 'funding', oi_roc: 'oi' };

/** Streams a Strategy Creator rule spec reads: condition operands naming a
 *  feed column, plus indicators computed from one. */
export function specStreams(spec: unknown): DataStream[] {
	const found = new Set<DataStream>();
	const visit = (node: unknown): void => {
		if (Array.isArray(node)) {
			node.forEach(visit);
			return;
		}
		if (typeof node === 'string') {
			if (COLUMN_STREAM[node]) found.add(COLUMN_STREAM[node]);
			return;
		}
		if (!node || typeof node !== 'object') return;
		const record = node as Record<string, unknown>;
		const kind = record.kind;
		if (typeof kind === 'string' && INDICATOR_STREAM[kind]) found.add(INDICATOR_STREAM[kind]);
		for (const key of ['indicators', 'entry_long', 'exit_long', 'entry_short', 'exit_short', 'conditions', 'left', 'right', 'series', 'indicator']) {
			if (key in record) visit(record[key]);
		}
	};
	visit(spec);
	return [...found].sort();
}
