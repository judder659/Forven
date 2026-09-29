<script lang="ts">
	// The strategy page's Heatmap tab: the strategy's Gauntlet backtest (the
	// request the Gauntlet tab would submit) repeated over a grid of two of its
	// settings. Each cell runs in its own worker, as the optimizer's grid does.
	import { createEventDispatcher, onDestroy } from 'svelte';
	import { strategyParamHeatmap, type HeatmapAxisRequest, type StrategyToolRequest } from '$lib/api';
	import { containerKnobValue, withContainerKnobs, type KnobOption, type KnobRef } from '$lib/utils/creatorGrids';
	import { runHeatmapRequests } from '$lib/utils/gridRuns';
	import HeatmapPanel, { cellKey, type HeatmapView } from './HeatmapPanel.svelte';

	export let request: StrategyToolRequest;
	export let knobs: KnobOption[] = [];

	const dispatch = createEventDispatcher<{ adopt: Array<[KnobRef, number | null]> }>();
	let view: HeatmapView | null = null;
	let controller: AbortController | null = null;
	let context = '';
	onDestroy(() => controller?.abort());

	const ref = (axis: HeatmapAxisRequest): KnobRef => ({ target: axis.target, name: axis.name, indicator: axis.indicator ?? null });
	/** What a run covers apart from the settings it sweeps; any other change makes it stale. */
	function contextOf(req: StrategyToolRequest, axes: HeatmapAxisRequest[]): string {
		return JSON.stringify({ ...req, params: withContainerKnobs(req.params ?? {}, axes.map((axis) => [ref(axis), Number.NaN])) });
	}
	$: axes = view ? [view.x, ...(view.y ? [view.y] : [])] : [];
	$: stale = !!view && context !== contextOf(request, axes);
	$: if (stale && view?.status === 'running') controller?.abort();
	$: current = view
		? { x: containerKnobValue(request.params, ref(view.x)), y: view.y ? containerKnobValue(request.params, ref(view.y)) : null }
		: { x: null, y: null };

	async function run(axesToRun: { x: HeatmapAxisRequest; y: HeatmapAxisRequest | null }) {
		controller?.abort();
		const own = new AbortController();
		controller = own;
		const base = { ...request };
		const meta = (axis: HeatmapAxisRequest) => {
			const knob = knobs.find((k) => k.target === axis.target && k.name === axis.name && k.indicator === (axis.indicator ?? null));
			return { ...axis, label: knob?.label ?? axis.name, integer: knob?.integer ?? false, min: knob?.min ?? null };
		};
		view = {
			x: meta(axesToRun.x), y: axesToRun.y ? meta(axesToRun.y) : null, cells: {}, done: 0,
			total: axesToRun.x.values.length * (axesToRun.y ? axesToRun.y.values.length : 1), status: 'running', warnings: [],
		};
		context = contextOf(base, [axesToRun.x, ...(axesToRun.y ? [axesToRun.y] : [])]);
		await runHeatmapRequests(axesToRun, (x, y, signal) => strategyParamHeatmap({ ...base, x, y }, signal), {
			cells: (cells, warnings, size) => {
				if (!view) return;
				for (const cell of cells) view.cells[cellKey(cell.x, cell.y)] = cell;
				view.done += size;
				view.warnings = [...new Set([...view.warnings, ...warnings])];
				view = view;
			},
			failed: (message, size) => {
				if (!view) return;
				view.done += size;
				view.warnings = [...new Set([...view.warnings, message])];
				view = view;
			},
		}, own.signal);
		if (controller !== own || !view) return;
		view.status = own.signal.aborted ? 'cancelled' : 'done';
		view = view;
		controller = null;
	}

	function adopt({ x, y }: { x: number; y: number | null }) {
		if (!view) return;
		const changes: Array<[KnobRef, number | null]> = [[ref(view.x), x]];
		if (view.y) changes.push([ref(view.y), y]);
		dispatch('adopt', changes);
	}
</script>

<HeatmapPanel {knobs} {view} {stale} {current} canRun={!!request.strategy_id} defaultSteps={5}
	note="Each cell is this strategy's Gauntlet backtest (the parameters, market, window and execution settings on the Gauntlet tab) with those two settings changed. Cells run a few at a time in their own workers, sharing the backtest slots with the pipeline, so a 5×5 grid takes several times as long as one Gauntlet run: under a minute on a short window, several minutes on years of data. Nothing is saved: click a cell to copy its settings into the Gauntlet tab's draft."
	on:run={(e) => run(e.detail)} on:cancel={() => controller?.abort()} on:adopt={(e) => adopt(e.detail)} />
