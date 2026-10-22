// Helpers for displaying and entering scheduler timing in plain minutes.
//
// The backend is unchanged: interval scheduler jobs are still stored as an
// integer-millisecond string. These helpers convert only at the UI boundary so
// the operator can think in minutes instead of milliseconds.

/** Convert a millisecond-interval string to whole minutes for display. Returns '' when not a positive interval. */
export function msToMinutes(ms: string | number | null | undefined): string {
	const raw = Number(ms);
	if (!Number.isFinite(raw) || raw <= 0) return '';
	return String(Math.max(1, Math.round(raw / 60000)));
}

/** Convert a minutes value entered in the UI to a millisecond-interval string for the backend. Returns '' when invalid. */
export function minutesToMs(minutes: string | number | null | undefined): string {
	const m = Number(minutes);
	if (!Number.isFinite(m) || m <= 0) return '';
	return String(Math.round(m) * 60000);
}

/** Human-readable interval label from a millisecond value, e.g. "every 30m" / "every 2h" / "every 1d". */
export function formatIntervalMs(ms: string | number | null | undefined): string {
	const raw = Number(ms);
	if (!Number.isFinite(raw) || raw <= 0) return String(ms ?? '').trim() || '--';
	const minutes = Math.max(1, Math.round(raw / 60000));
	if (minutes >= 60 * 24) return `every ${Math.round(minutes / (60 * 24))}d`;
	if (minutes >= 60) return `every ${Math.round(minutes / 60)}h`;
	return `every ${minutes}m`;
}
