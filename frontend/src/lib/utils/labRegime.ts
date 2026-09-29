export interface RegimeLike {
	regime?: string | null;
	display_regime?: string | null;
	core_regime?: string | null;
	uncertain?: boolean | null;
	overlay_regime?: string | null;
	uncertain_share?: number | null;
}

export interface TimelinePriceLike {
	return_pct?: number | null;
}

function resolveRegimeKey(input: RegimeLike | string | null | undefined): string {
	let key: string;
	if (typeof input === 'string') {
		key = String(input || '').trim().toUpperCase();
	} else {
		key =
			String(input?.display_regime || '').trim().toUpperCase() ||
			String(input?.core_regime || '').trim().toUpperCase() ||
			String(input?.regime || '').trim().toUpperCase();
	}
	// Pipeline classifier taxonomy (forven.regime) says RANGE_BOUND; the lab
	// taxonomy says RANGE. One styling vocabulary for both.
	if (key === 'RANGE_BOUND') return 'RANGE';
	return key;
}

export function formatRegimeLabel(input: RegimeLike | string | null | undefined): string {
	const key = resolveRegimeKey(input);
	if (!key) return '-';
	if (key === 'HIGH_VOL') return 'HIGH VOL';
	return key.replaceAll('_', ' ');
}

export function isRegimeUncertain(input: RegimeLike | null | undefined): boolean {
	const overlayRegime = String(input?.overlay_regime || '').trim().toUpperCase();
	const uncertainShare = Number(input?.uncertain_share ?? 0);
	return Boolean(input?.uncertain) || overlayRegime === 'TRANSITION' || uncertainShare > 0;
}

function baseSwatchTone(key: string): string {
	if (key === 'TREND_UP') return 'bg-emerald-400';
	if (key === 'TREND_DOWN') return 'bg-rose-400';
	if (key === 'RANGE') return 'bg-sky-400';
	if (key === 'HIGH_VOL') return 'bg-fuchsia-400';
	if (key === 'TRANSITION') return 'bg-amber-400';
	return 'bg-sc-ink4';
}

export function regimeSwatchClass(input: RegimeLike | string | null | undefined): string {
	const key = resolveRegimeKey(input);
	const uncertain = typeof input === 'string' ? false : isRegimeUncertain(input);
	const tone = baseSwatchTone(key);
	return uncertain ? `${tone} ring-1 ring-amber-300/70` : tone;
}

export function regimeBadgeClass(input: RegimeLike | string | null | undefined): string {
	const key = resolveRegimeKey(input);
	const uncertain = typeof input === 'string' ? false : isRegimeUncertain(input);
	const base =
		key === 'TREND_UP'
			? 'border-emerald-700/70 bg-emerald-500/10 text-emerald-100'
			: key === 'TREND_DOWN'
				? 'border-rose-700/70 bg-rose-500/10 text-rose-100'
				: key === 'RANGE'
					? 'border-sky-700/70 bg-sky-500/10 text-sky-100'
					: key === 'HIGH_VOL'
						? 'border-fuchsia-700/70 bg-fuchsia-500/10 text-fuchsia-100'
						: key === 'TRANSITION'
							? 'border-amber-700/70 bg-amber-500/10 text-amber-100'
							: 'border-sc-line2 bg-sc-ink4/10 text-sc-ink';
	return uncertain ? `${base} shadow-[0_0_0_1px_rgba(251,191,36,0.25)]` : base;
}

export function recordOf(value: unknown): Record<string, unknown> {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
	return value as Record<string, unknown>;
}

export function numberOf(value: unknown): number | null {
	const numeric = Number(value);
	return Number.isFinite(numeric) ? numeric : null;
}

export function stringOf(value: unknown): string {
	return String(value ?? '').trim();
}

export function summarizeTimelineReturn(points: TimelinePriceLike[]): number | null {
	const values = points
		.map((point) => Number(point.return_pct))
		.filter((value) => Number.isFinite(value));
	if (values.length === 0) return null;
	return values[values.length - 1];
}
