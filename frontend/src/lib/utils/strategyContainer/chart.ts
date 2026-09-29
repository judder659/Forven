// Small SVG chart helpers for the strategy container's charts: scales, ticks, and a
// width-tracking action. Charts render as Svelte markup from these numbers.

export interface Scale {
	(value: number): number;
	invert: (pixel: number) => number;
}

export function linearScale(d0: number, d1: number, r0: number, r1: number): Scale {
	const k = (r1 - r0) / (d1 - d0 || 1);
	const scale = ((value: number) => r0 + (value - d0) * k) as Scale;
	scale.invert = (pixel: number) => d0 + (pixel - r0) / k;
	return scale;
}

/** Log scale for positive domains (growth multiples). */
export function logScale(d0: number, d1: number, r0: number, r1: number): Scale {
	const a = Math.log(Math.max(d0, 1e-9));
	const b = Math.log(Math.max(d1, 1e-9));
	const k = (r1 - r0) / (b - a || 1);
	const scale = ((value: number) => r0 + (Math.log(Math.max(value, 1e-9)) - a) * k) as Scale;
	scale.invert = (pixel: number) => Math.exp(a + (pixel - r0) / k);
	return scale;
}

function niceStep(span: number, count: number): number {
	const raw = span / Math.max(count, 1);
	if (!(raw > 0)) return 1;
	const power = 10 ** Math.floor(Math.log10(raw));
	const n = raw / power;
	return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * power;
}

/** Evenly spaced round ticks covering [lo, hi]. */
export function niceTicks(lo: number, hi: number, count = 5): number[] {
	if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
	if (lo === hi) return [lo];
	const step = niceStep(hi - lo, count);
	const out: number[] = [];
	for (let value = Math.ceil(lo / step) * step; value <= hi + step * 1e-6; value += step) out.push(Number(value.toFixed(10)));
	return out;
}

/** Growth-multiple ticks for a log axis (1 = break-even). */
export function logGrowthTicks(lo: number, hi: number): number[] {
	const candidates = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50, 100];
	const inRange = candidates.filter((value) => value >= lo && value <= hi);
	if (inRange.length <= 6) return inRange;
	// Thin to about six, always keeping break-even.
	const stride = Math.ceil(inRange.length / 6);
	return inRange.filter((value, index) => index % stride === 0 || value === 1);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Month-aligned UTC ticks: yearly for long spans, then half-years, quarters, months. */
export function timeTicks(t0: number, t1: number): { ticks: number[]; monthStep: number } {
	const years = (t1 - t0) / (365.25 * 86_400_000);
	const monthStep = years > 3 ? 12 : years > 1.5 ? 6 : years > 0.6 ? 3 : 1;
	const start = new Date(t0);
	let index = start.getUTCFullYear() * 12 + start.getUTCMonth();
	index = Math.ceil(index / monthStep) * monthStep;
	const ticks: number[] = [];
	for (; ; index += monthStep) {
		const t = Date.UTC(Math.floor(index / 12), index % 12, 1);
		if (t > t1) break;
		if (t >= t0) ticks.push(t);
		if (ticks.length > 24) break;
	}
	return { ticks, monthStep };
}

export function timeTickLabel(t: number, monthStep: number): string {
	const d = new Date(t);
	return monthStep >= 12 ? String(d.getUTCFullYear()) : `${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
}

/** SVG path through points already mapped to pixels. */
export function linePath(points: Array<[number, number]>): string {
	return points.map(([x, y], index) => `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
}

/** Index of the point with the largest t that is ≤ t (step lookup). */
export function stepIndex(times: number[], t: number): number {
	if (times.length === 0 || t <= times[0]) return 0;
	let lo = 0;
	let hi = times.length - 1;
	if (t >= times[hi]) return hi;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (times[mid] <= t) lo = mid;
		else hi = mid;
	}
	return lo;
}

/** Svelte action: report the node's width now and on resize (jsdom reports 0). */
export function trackWidth(node: HTMLElement, onWidth: (width: number) => void) {
	let callback = onWidth;
	const report = () => callback(node.clientWidth);
	report();
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => report());
	observer?.observe(node);
	return {
		update(next: (width: number) => void) {
			callback = next;
			report();
		},
		destroy() {
			observer?.disconnect();
		},
	};
}

/** Chart colors (CVD-checked: teal gain vs red-orange loss; strategy in ink, context in grey). */
export const CHART = {
	ink: '#eef1f5',
	ink2: '#aab1bc',
	ink3: '#747c88',
	grid: '#1c2026',
	axis: '#2a2f38',
	context: '#6c7480',
	gain: '#139a9f',
	loss: '#e0663f',
	neutral: '#262a31',
	panel: '#0c0e11',
	ok: '#3cc48f',
	caution: '#e7b24a',
	fail: '#e5574f',
};
