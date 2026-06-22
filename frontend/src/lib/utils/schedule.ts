// Helpers for displaying and entering scheduler timing in plain minutes.
//
// The backend is unchanged: interval scheduler jobs are still stored as an
// integer-millisecond string, and routine schedules are still 5-field cron
// expressions. These helpers convert only at the UI boundary so the operator
// can think in minutes instead of milliseconds (interval jobs) or cron-step
// syntax (routines).

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

/**
 * Build a cron expression that fires every `minutes` minutes. Handles the
 * cleanly-expressible cases (sub-hour steps, whole hours, whole days). The
 * routines page renders a live "next fire times" preview, so any approximation
 * of an awkward value stays visible to the operator. Returns '' when invalid.
 */
export function minutesToCron(minutes: string | number | null | undefined): string {
	const n = Math.round(Number(minutes));
	if (!Number.isFinite(n) || n < 1) return '';
	if (n < 60) return `*/${n} * * * *`;
	if (n === 60) return '0 * * * *';
	if (n % 1440 === 0) {
		const days = n / 1440;
		return days === 1 ? '0 0 * * *' : `0 0 */${days} * *`;
	}
	if (n % 60 === 0) {
		const hours = n / 60;
		if (hours < 24) return `0 */${hours} * * *`;
		return '0 0 * * *';
	}
	// Not cleanly expressible in cron — approximate to the nearest whole hour.
	const hours = Math.max(1, Math.round(n / 60));
	return hours < 24 ? `0 */${hours} * * *` : '0 0 * * *';
}

/**
 * Inverse of minutesToCron for the patterns it produces, so an existing routine
 * can be re-opened in "every N minutes" mode. Returns null when the expression
 * isn't a simple interval (e.g. "0 14 * * MON"), signalling the caller to fall
 * back to cron mode.
 */
export function cronToMinutes(expr: string | null | undefined): number | null {
	const parts = (expr || '').trim().split(/\s+/);
	if (parts.length !== 5) return null;
	const [min, hour, dom, mon, dow] = parts;
	if (mon !== '*' || dow !== '*') return null;

	const stepMin = /^\*\/(\d+)$/.exec(min);
	// */N * * * *  -> every N minutes (sub-hour)
	if (stepMin && hour === '*' && dom === '*') {
		const n = Number(stepMin[1]);
		return n >= 1 && n < 60 ? n : null;
	}
	// 0 * * * *  -> hourly
	if (min === '0' && hour === '*' && dom === '*') return 60;

	const stepHour = /^\*\/(\d+)$/.exec(hour);
	// 0 */H * * *  -> every H hours
	if (min === '0' && stepHour && dom === '*') {
		const h = Number(stepHour[1]);
		return h >= 1 && h < 24 ? h * 60 : null;
	}
	// 0 0 * * *  -> daily
	if (min === '0' && hour === '0' && dom === '*') return 1440;

	const stepDom = /^\*\/(\d+)$/.exec(dom);
	// 0 0 */D * *  -> every D days
	if (min === '0' && hour === '0' && stepDom) {
		const d = Number(stepDom[1]);
		return d >= 1 ? d * 1440 : null;
	}
	return null;
}
