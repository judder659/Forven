// Timestamps on the Forge come from several tables with different spellings:
// strategy_events stamps ISO-8601 with an offset, while gate_rejections and some
// paper columns store "YYYY-MM-DD HH:MM:SS" with no zone at all. Every one of them
// is UTC, but Date.parse reads a zone-less string as LOCAL time, so a naive parse
// shifts those rows by the browser's offset. parseUtc pins them to UTC.

const HAS_ZONE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;

export function parseUtc(value: unknown): number | null {
	if (typeof value === 'number') return Number.isFinite(value) ? value : null;
	if (typeof value !== 'string') return null;
	const text = value.trim();
	if (!text) return null;
	let iso = text.replace(' ', 'T');
	if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) iso += 'T00:00:00';
	if (!HAS_ZONE.test(iso)) iso += 'Z';
	const parsed = Date.parse(iso);
	return Number.isFinite(parsed) ? parsed : null;
}

/** "just now", "4m ago", "3h ago", "2d ago" — or "—" when the time is unknown. */
export function ago(value: unknown, now: number = Date.now()): string {
	const ts = parseUtc(value);
	if (ts === null) return '—';
	const seconds = Math.max(0, Math.round((now - ts) / 1000));
	if (seconds < 45) return 'just now';
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${minutes}m ago`;
	const hours = Math.round(minutes / 60);
	if (hours < 48) return `${hours}h ago`;
	return `${Math.round(hours / 24)}d ago`;
}

/** Elapsed time without the "ago": "42s", "12m", "3h 5m", "2d 4h". */
export function elapsed(value: unknown, now: number = Date.now()): string {
	const ts = parseUtc(value);
	if (ts === null) return '—';
	const seconds = Math.max(0, Math.floor((now - ts) / 1000));
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours}h ${minutes % 60}m`;
	return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** "Sep 29, 14:05" in the viewer's zone — for "when" columns people read, not bar stamps. */
export function shortDateTime(value: unknown): string {
	const ts = parseUtc(value);
	if (ts === null) return '—';
	const d = new Date(ts);
	return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`;
}

/** "Sep 27" in the viewer's zone. */
export function shortDate(value: unknown): string {
	const ts = parseUtc(value);
	if (ts === null) return '—';
	return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** Whole days in a stage, e.g. 20.8 → "20d", 0.4 → "<1d". */
export function daysLabel(days: number | null | undefined): string {
	if (typeof days !== 'number' || !Number.isFinite(days) || days < 0) return '—';
	if (days < 1) return '<1d';
	return `${Math.floor(days)}d`;
}
