import { afterEach, describe, expect, it } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import HeatmapPanel, { type HeatmapView } from '$lib/components/strategy/HeatmapPanel.svelte';
import type { KnobOption } from '$lib/utils/creatorGrids';

// S07239's kc_period sweep: its own 63 sits on a plateau; the best cell, 66, is a spike.
const xs = [53, 56, 60, 63, 66, 70, 73];
const oos = [0.06, 0.067, 0.081, 0.082, 0.178, 0.085, 0.038];
const knobs: KnobOption[] = [
	{ key: 'param:kc_period', target: 'param', name: 'kc_period', indicator: null, label: 'kc_period', value: 63, integer: true, min: null, max: null },
];
const view = (): HeatmapView => ({
	x: { target: 'param', name: 'kc_period', indicator: null, values: xs, label: 'kc_period', integer: true, min: null },
	y: null,
	cells: Object.fromEntries(xs.map((x, i) => [`${x}|null`,
		{ x, y: null, trades: 60 + (i % 3), oos_trades: 20, oos_return: oos[i], in_return: 0.01 }])),
	done: xs.length,
	total: xs.length,
	status: 'done',
	warnings: [],
});

describe('heatmap panel verdicts', () => {
	let target: HTMLDivElement | null = null;
	let app: ReturnType<typeof mount> | null = null;

	function close(): void {
		if (app) unmount(app);
		app = null;
		target?.remove();
		target = null;
	}
	afterEach(close);

	async function render(current: { x: number | null; y: number | null }) {
		close();
		target = document.createElement('div');
		document.body.appendChild(target);
		app = mount(HeatmapPanel, { target, props: { knobs, view: view(), current } });
		await tick();
		const host = target;
		return (key: string) =>
			host.querySelector(`[data-testid="heatmap-verdict-${key}"]`)?.textContent?.replace(/\s+/g, ' ').trim() ?? null;
	}

	it('judges the current setting apart from the best cell', async () => {
		const line = await render({ x: 63, y: null });
		expect(line('current')).toContain('Current setting');
		expect(line('current')).toContain('plateau');
		expect(line('current')).toContain('kc_period 63 (+8.2% out-of-sample): 2 of 2 neighbours keep at least half its return');
		expect(line('best')).toContain('Best cell');
		expect(line('best')).toContain('spike');
		expect(target?.querySelector('button[title^="kc_period 63"]')?.className).toContain('outline-white');
	});

	it('shows one line when the current setting is the best cell, and notes one off the grid', async () => {
		let line = await render({ x: 66, y: null });
		expect(line('current')).toContain('It is also the best cell here. 7 of 7 settings make money out-of-sample.');
		expect(line('best')).toBeNull();

		line = await render({ x: 61, y: null });
		expect(line('current')).toContain('kc_period 61 is not on this grid, so it is not judged');
		expect(line('best')).toContain('spike');
	});
});
