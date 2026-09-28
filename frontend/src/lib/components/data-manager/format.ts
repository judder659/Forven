/**
 * Formatting and labels for the Data Manager page. Pure functions: every state,
 * score and allowance they present comes from the server; nothing here decides
 * whether data is fresh or good.
 */
import type { DataJob, DataJobKind, DataJobStatus, DataStream, SlaAssessment, SlaState, SlaTier } from '$lib/api/dataManagerTypes';

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** Bar width in seconds ("15m" -> 900, "1w" -> 604800); 3600 when unreadable. */
export function timeframeSeconds(timeframe: string): number {
	const match = /^(\d+)([mhdw])$/.exec(timeframe.trim().toLowerCase());
	if (!match) return HOUR;
	return Number(match[1]) * { m: MINUTE, h: HOUR, d: DAY, w: 7 * DAY }[match[2] as 'm' | 'h' | 'd' | 'w'];
}

const pad = (n: number) => String(n).padStart(2, '0');

function parse(iso: string | null | undefined): number | null {
	if (!iso) return null;
	const t = Date.parse(iso);
	return Number.isFinite(t) ? t : null;
}

/** "2026-09-28 18:30 UTC" (or with seconds, or the date alone). */
export function formatUtc(iso: string | null | undefined, options: { seconds?: boolean; date?: boolean; suffix?: boolean } = {}): string {
	const t = parse(iso);
	if (t == null) return '—';
	const d = new Date(t);
	const date = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
	if (options.date) return date;
	const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${options.seconds ? `:${pad(d.getUTCSeconds())}` : ''}`;
	return `${date} ${time}${options.suffix === false ? '' : ' UTC'}`;
}

/** "12 s ago", "5 min ago", "3 h ago", "2 d ago"; "in 18 s" for the future. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
	const t = parse(iso);
	if (t == null) return '—';
	const seconds = Math.round((now - t) / 1000);
	const future = seconds < 0;
	const s = Math.abs(seconds);
	let text: string;
	if (s < 5) return future ? 'in a moment' : 'just now';
	if (s < MINUTE) text = `${s} s`;
	else if (s < HOUR) text = `${Math.floor(s / MINUTE)} min`;
	else if (s < DAY) text = `${Math.floor(s / HOUR)} h`;
	else if (s < 60 * DAY) text = `${Math.floor(s / DAY)} d`;
	else if (s < 730 * DAY) text = `${Math.floor(s / (30.44 * DAY))} mo`;
	else text = `${Math.floor(s / (365.25 * DAY))} y`;
	return future ? `in ${text}` : `${text} ago`;
}

/** Durations in words: "45 s", "52 min", "5 h 12 m", "3 d 4 h", "41 d". */
export function formatDuration(seconds: number | null | undefined): string {
	if (seconds == null || !Number.isFinite(seconds)) return '—';
	const s = Math.max(0, Math.round(seconds));
	if (s < MINUTE) return `${s} s`;
	if (s < HOUR) return `${Math.round(s / MINUTE)} min`;
	if (s < DAY) {
		const h = Math.floor(s / HOUR);
		const m = Math.round((s % HOUR) / MINUTE);
		if (m === 60) return `${h + 1} h`;
		return m ? `${h} h ${m} m` : `${h} h`;
	}
	if (s < 30 * DAY) {
		const d = Math.floor(s / DAY);
		const h = Math.round((s % DAY) / HOUR);
		if (h === 24) return `${d + 1} d`;
		return h ? `${d} d ${h} h` : `${d} d`;
	}
	return `${Math.round(s / DAY)} d`;
}

const oneDecimal = (value: number) => {
	const text = value.toFixed(1);
	return text.endsWith('.0') ? text.slice(0, -2) : text;
};

/** Compact duration for dense cells: "52 min", "5.5 h", "3.2 d". */
export function compactDuration(seconds: number | null | undefined): string {
	if (seconds == null || !Number.isFinite(seconds)) return '—';
	const s = Math.max(0, seconds);
	if (s < HOUR) return `${Math.max(1, Math.round(s / MINUTE))} min`;
	if (s < DAY) return `${oneDecimal(s / HOUR)} h`;
	if (s < 60 * DAY) return `${oneDecimal(s / DAY)} d`;
	return `${Math.round(s / DAY)} d`;
}

/** "5.5 h / 2 h": how far behind, against what the tier allows. */
export function lagCaption(sla: Pick<SlaAssessment, 'lag_seconds' | 'allowed_seconds'>): string {
	if (sla.lag_seconds == null) return `no data / ${compactDuration(sla.allowed_seconds)}`;
	return `${compactDuration(sla.lag_seconds)} / ${compactDuration(sla.allowed_seconds)}`;
}

export function formatBytes(bytes: number | null | undefined): string {
	if (bytes == null || !Number.isFinite(bytes)) return '—';
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let value = Math.max(0, bytes);
	let index = 0;
	while (value >= 1024 && index < units.length - 1) {
		value /= 1024;
		index += 1;
	}
	if (index === 0) return `${Math.round(value)} B`;
	const digits = value < 10 ? 2 : value < 100 ? 1 : 0;
	return `${value.toFixed(digits)} ${units[index]}`;
}

export function formatCount(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '—';
	return Math.round(value).toLocaleString('en-US');
}

/** "3.5 M", "61.8 k", "940". */
export function formatCompact(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '—';
	const abs = Math.abs(value);
	if (abs >= 1e9) return `${oneDecimal(value / 1e9)} B`;
	if (abs >= 1e6) return `${oneDecimal(value / 1e6)} M`;
	if (abs >= 1e4) return `${oneDecimal(value / 1e3)} k`;
	return formatCount(value);
}

/** 0.9985 -> "99.85%"; never shows 100% for less than complete. */
export function formatPercent(fraction: number | null | undefined, digits = 1): string {
	if (fraction == null || !Number.isFinite(fraction)) return '—';
	if (fraction >= 1) return '100%';
	const pct = fraction * 100;
	let text = pct.toFixed(digits);
	if (Number(text) >= 100) text = (100 - 10 ** -Math.max(digits, 2)).toFixed(Math.max(digits, 2));
	return `${text}%`;
}

/** History length between two timestamps: "6.1y", "8mo", "23d", "5h". */
export function historyLength(first: string | null | undefined, last: string | null | undefined): string {
	const a = parse(first);
	const b = parse(last);
	if (a == null || b == null || b < a) return '—';
	const seconds = (b - a) / 1000;
	const years = seconds / (365.25 * DAY);
	if (years >= 1) return `${years.toFixed(1)}y`;
	const months = seconds / (30.44 * DAY);
	if (months >= 1) return `${Math.floor(months)}mo`;
	if (seconds >= DAY) return `${Math.floor(seconds / DAY)}d`;
	return `${Math.max(1, Math.floor(seconds / HOUR))}h`;
}

// ---------------------------------------------------------------- labels

export const STATE_LABEL: Record<SlaState, string> = {
	fresh: 'Fresh',
	late: 'Late',
	breach: 'Breach',
	frozen: 'Frozen',
	missing: 'Missing',
};

export const STATE_HELP: Record<SlaState, string> = {
	fresh: 'Within the lag its tier allows.',
	late: 'Behind by more than its tier allows.',
	breach: 'Behind by more than the breach limit (a multiple of what its tier allows).',
	frozen: 'Deliberately not collected (delisted, renamed, or no new bars after repeated refreshes).',
	missing: 'Needed, but nothing is stored yet.',
};

export const STATES: SlaState[] = ['fresh', 'late', 'breach', 'frozen', 'missing'];
export const TIERS: SlaTier[] = ['live', 'paper', 'pipeline', 'universe', 'idle'];

export const TIER_LABEL: Record<SlaTier, string> = {
	live: 'Live',
	paper: 'Paper',
	pipeline: 'Pipeline',
	universe: 'Research',
	idle: 'Idle',
};

export const TIER_HELP: Record<SlaTier, string> = {
	live: 'Series a live strategy or a running bot trades on.',
	paper: 'Series a paper strategy trades on.',
	pipeline: 'Series strategies in the quick screen or gauntlet are tested on.',
	universe: 'The research universe: planned series kept current for research.',
	idle: 'Stored series nothing uses right now.',
};

const STATE_CHIP: Record<SlaState, string> = {
	fresh: 'border-emerald-900 bg-emerald-500/5 text-emerald-400',
	late: 'border-amber-900 bg-amber-500/5 text-amber-400',
	breach: 'border-red-900 bg-red-500/10 text-red-400',
	frozen: 'border-slate-700 text-slate-400 bg-[repeating-linear-gradient(135deg,rgba(148,163,184,0.16)_0_2px,transparent_2px_5px)]',
	missing: 'border-dashed border-[#444] text-[#888]',
};
export const stateChipClass = (state: SlaState) => STATE_CHIP[state] ?? STATE_CHIP.missing;

const STATE_FILL: Record<SlaState, string> = {
	fresh: 'bg-emerald-500/70',
	late: 'bg-amber-400/80',
	breach: 'bg-red-500/80',
	frozen: 'bg-[repeating-linear-gradient(135deg,rgba(148,163,184,0.55)_0_2px,rgba(71,85,105,0.35)_2px_5px)]',
	missing: 'border border-dashed border-[#555] bg-transparent',
};
export const stateFillClass = (state: SlaState) => STATE_FILL[state] ?? STATE_FILL.missing;

const STATE_TEXT: Record<SlaState, string> = {
	fresh: 'text-emerald-400',
	late: 'text-amber-400',
	breach: 'text-red-400',
	frozen: 'text-slate-400',
	missing: 'text-[#888]',
};
export const stateTextClass = (state: SlaState) => STATE_TEXT[state] ?? 'text-[#888]';

export const STREAM_LABEL: Record<DataStream, string> = {
	ohlcv: 'Candles',
	funding: 'Funding',
	oi: 'Open interest',
	basis: 'Basis',
	iv: 'Implied vol',
	ls_ratio: 'Long/short',
	taker: 'Taker flow',
	liquidations: 'Liquidations',
};
export const STREAMS: DataStream[] = ['ohlcv', 'funding', 'oi', 'basis', 'iv', 'ls_ratio', 'taker', 'liquidations'];
export const streamLabel = (stream: string) => STREAM_LABEL[stream as DataStream] ?? stream;

const EXCHANGE_LABEL: Record<string, string> = {
	binanceusdm: 'Binance USD-M',
	'binance-vision': 'Binance Vision',
	binance: 'Binance',
	hyperliquid: 'Hyperliquid',
	okx: 'OKX',
	bybit: 'Bybit',
	coinbase: 'Coinbase',
	kraken: 'Kraken',
	deribit: 'Deribit',
	polygon: 'Polygon',
	csv: 'CSV import',
};
export const exchangeLabel = (id: string | null | undefined) => (id ? EXCHANGE_LABEL[id] ?? id.charAt(0).toUpperCase() + id.slice(1) : '—');

/** Short chip text for a venue key. */
export function venueShort(venue: string): string {
	if (venue === 'canonical') return 'Research';
	const [source, market] = venue.split(':');
	if (source === 'csv') return 'CSV';
	const name = source === 'hyperliquid' ? 'HL' : exchangeLabel(source);
	return market && market !== 'unknown' ? `${name} ${market}` : name;
}

/** Full venue description: "Binance USD-M perp · research series". */
export function venueLabel(venue: string, source?: string | null, market?: string | null): string {
	if (venue === 'canonical') {
		const who = source === 'binance' ? 'Binance spot' : source && source !== 'binanceusdm' && source !== 'binance-vision' ? exchangeLabel(source) : 'Binance USD-M perp';
		return `${who} · research series`;
	}
	const [id, mkt] = venue.split(':');
	if (id === 'csv') return 'CSV import · separate venue series';
	const m = mkt && mkt !== 'unknown' ? ` ${mkt}` : market && market !== 'unknown' ? ` ${market}` : '';
	return `${exchangeLabel(id)}${m} · separate venue series`;
}

export const VENUE_HELP =
	'The research series (canonical) is the Binance USD-M perp series backtests, the gauntlet and paper trading read. ' +
	'Data from any other venue is stored as a separate venue series and never mixed into it.';

export function originLabel(origin: string | null | undefined): string {
	if (!origin) return '—';
	if (origin === 'user') return 'You';
	if (origin === 'sla') return 'Automatic';
	if (origin === 'system') return 'System';
	if (origin === 'universe') return 'Universe';
	if (origin.startsWith('strategy:')) return `Strategy ${origin.slice('strategy:'.length)}`;
	return origin;
}

export const JOB_KIND_LABEL: Record<DataJobKind, string> = {
	download: 'Download',
	history_extend: 'History extension',
	universe_seed: 'Universe seed',
	csv_import: 'File import',
	gap_repair: 'Gap repair',
	tail_refresh: 'Refresh',
	stream_collect: 'Stream collection',
	sla_collect: 'Collection tick',
	reclaim: 'Storage reclaim',
	compaction: 'Compaction',
};
export const jobKindLabel = (kind: string) => JOB_KIND_LABEL[kind as DataJobKind] ?? kind;

export const JOB_STATUS_LABEL: Record<DataJobStatus, string> = {
	queued: 'Queued',
	running: 'Running',
	succeeded: 'Done',
	failed: 'Failed',
	cancelled: 'Cancelled',
	interrupted: 'Interrupted',
};

const JOB_STATUS_CLASS: Record<DataJobStatus, string> = {
	queued: 'border-[#333] text-[#aaa]',
	running: 'border-sky-900 text-sky-300',
	succeeded: 'border-emerald-900 text-emerald-400',
	failed: 'border-red-900 text-red-400',
	cancelled: 'border-[#333] text-[#777]',
	interrupted: 'border-amber-900 text-amber-400',
};
export const jobStatusClass = (status: DataJobStatus) => JOB_STATUS_CLASS[status] ?? 'border-[#333] text-[#888]';

const ERROR_TEXT: Record<string, string> = {
	rate_limited: 'The venue rate-limited the download',
	venue_down: 'The venue did not respond',
	unknown_symbol: 'The venue does not know this symbol',
	delisted: 'The market is delisted',
	venue_refused: 'Refused: it would mix another venue’s bars into this series',
	shrink_refused: 'Refused: the result would hold fewer bars than stored',
	corrupt: 'A stored file is damaged',
	disk_full: 'Not enough free disk space',
	invalid_request: 'The request was not valid',
	cancelled: 'Cancelled',
	backend_restarted: 'The backend restarted while it ran',
	internal: 'Unexpected error',
};
export const errorText = (code: string | null | undefined) => (code ? ERROR_TEXT[code] ?? code.replaceAll('_', ' ') : '');

/** "61,200 / 105,120 bars · 58%", "23 / 64 months", or the done count alone. */
export function progressText(progress: DataJob['progress']): string {
	const unitName = progress.unit ?? '';
	if (progress.total && progress.total > 0) {
		const pct = Math.min(100, Math.floor((progress.done / progress.total) * 100));
		return `${formatCount(progress.done)} / ${formatCount(progress.total)}${unitName ? ` ${unitName}` : ''} · ${pct}%`;
	}
	return progress.done ? `${formatCount(progress.done)}${unitName ? ` ${unitName}` : ''}` : '';
}

export function progressFraction(progress: DataJob['progress']): number | null {
	if (!progress.total || progress.total <= 0) return null;
	return Math.max(0, Math.min(1, progress.done / progress.total));
}

/** Seconds left at the average rate so far; null until there is a rate. */
export function jobEtaSeconds(job: Pick<DataJob, 'status' | 'started_at' | 'progress'>, now: number = Date.now()): number | null {
	if (job.status !== 'running' || !job.started_at || !job.progress.total || job.progress.done <= 0) return null;
	const elapsed = (now - Date.parse(job.started_at)) / 1000;
	if (!(elapsed > 5)) return null;
	const rate = job.progress.done / elapsed;
	const left = job.progress.total - job.progress.done;
	return left <= 0 ? 0 : left / rate;
}

/** A job's target in words: "BTC-USDT 1h", "BTC-USDT 1h +2 more", or "". */
export function jobSeriesText(job: Pick<DataJob, 'series'>): string {
	const first = job.series[0];
	if (!first) return '';
	const head = [first.symbol, first.timeframe, first.stream && first.stream !== 'ohlcv' ? streamLabel(first.stream).toLowerCase() : '']
		.filter(Boolean)
		.join(' ');
	return job.series.length > 1 ? `${head} +${job.series.length - 1} more` : head;
}

export function plural(count: number, one: string, many = `${one}s`): string {
	return `${formatCount(count)} ${count === 1 ? one : many}`;
}
