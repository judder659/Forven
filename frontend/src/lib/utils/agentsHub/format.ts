// Number, money, token and duration formatting for the Agents page. Pure.

export function plural(n: number, one: string, many = `${one}s`): string {
	return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}

/** "$5.78", "$184", "<$0.01", "$0" — spend reads at a glance, never with 4 decimals. */
export function fmtCost(usd: number | null | undefined): string {
	if (typeof usd !== 'number' || !Number.isFinite(usd)) return '—';
	if (usd <= 0) return '$0';
	if (usd < 0.01) return '<$0.01';
	if (usd >= 100) return `$${Math.round(usd).toLocaleString('en-US')}`;
	return `$${usd.toFixed(2)}`;
}

/** 26448610 → "26.4M", 73028 → "73K", 950 → "950". */
export function fmtTokens(tokens: number | null | undefined): string {
	if (typeof tokens !== 'number' || !Number.isFinite(tokens) || tokens <= 0) return '0';
	if (tokens >= 1e9) return `${(tokens / 1e9).toFixed(1)}B`;
	if (tokens >= 1e6) return `${(tokens / 1e6).toFixed(1)}M`;
	if (tokens >= 1e3) return `${Math.round(tokens / 1e3)}K`;
	return String(Math.round(tokens));
}

/** 27 → "27s", 88 → "1m 28s", 3725 → "1h 2m". */
export function fmtSeconds(seconds: number | null | undefined): string {
	if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
	const whole = Math.round(seconds);
	if (whole < 60) return `${whole}s`;
	const minutes = Math.floor(whole / 60);
	if (minutes < 60) return `${minutes}m ${whole % 60}s`;
	const hours = Math.floor(minutes / 60);
	if (hours < 48) return `${hours}h ${minutes % 60}m`;
	return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** 0.9444 → "94%", 0.9996 → "99.9%" (a 100% that isn't), null → "—". */
export function fmtRate(rate: number | null | undefined): string {
	if (typeof rate !== 'number' || !Number.isFinite(rate)) return '—';
	const pct = rate * 100;
	// Floored, so 99.96% reads 99.9% rather than rounding up to a 100% it isn't.
	if (pct >= 99.5 && pct < 100) return `${(Math.floor(pct * 10) / 10).toFixed(1)}%`;
	return `${Math.round(pct)}%`;
}
