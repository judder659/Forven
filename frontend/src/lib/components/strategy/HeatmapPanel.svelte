<script lang="ts" context="module">
	import type { HeatmapAxis, HeatmapCell } from '$lib/api';

	/** A heatmap as the page assembles it, one row request at a time. */
	export interface HeatmapView {
		x: HeatmapAxis;
		y: HeatmapAxis | null;
		cells: Record<string, HeatmapCell>;
		done: number;
		total: number;
		status: 'running' | 'done' | 'cancelled';
		warnings: string[];
	}

	export const cellKey = (x: number, y: number | null) => `${x}|${y}`;
</script>

<script lang="ts">
	// Parameter heatmap: the rule walked over a grid of two settings. A robust edge
	// sits on a plateau of neighbouring settings that also work; a lone bright
	// cell is usually fitted noise. Click a cell to use its settings.
	import { createEventDispatcher } from 'svelte';
	import type { HeatmapAxisRequest } from '$lib/api';
	import { axisValues, heatmapVerdict, niceRange, type KnobOption } from '$lib/utils/creatorGrids';
	import { formatValue } from '$lib/utils/ruleLabels';

	export let knobs: KnobOption[] = [];
	export let view: HeatmapView | null = null;
	export let stale = false;
	export let canRun = true;
	/** The current values of the swept settings, to mark where the rule is now. */
	export let current: { x: number | null; y: number | null } = { x: null, y: null };

	const dispatch = createEventDispatcher<{
		run: { x: HeatmapAxisRequest; y: HeatmapAxisRequest | null };
		cancel: void;
		adopt: { x: number; y: number | null };
	}>();

	type Metric = 'oos_return' | 'in_return' | 'oos_trades';
	const METRICS: [Metric, string][] = [['oos_return', 'Out-of-sample return'], ['in_return', 'In-sample return'], ['oos_trades', 'Out-of-sample trades']];
	let metric: Metric = 'oos_return';
	let steps = 7;
	let xKey = '';
	let yKey = '';
	let xFrom = 0;
	let xTo = 0;
	let yFrom = 0;
	let yTo = 0;
	// Set once the author picks the y setting, so "none" stays none.
	let yTouched = false;
	// Typed ranges are kept when the step count changes; defaults follow it.
	let xEdited = false;
	let yEdited = false;

	const knobFor = (key: string) => knobs.find((knob) => knob.key === key);
	function resetRange(axis: 'x' | 'y') {
		const knob = knobFor(axis === 'x' ? xKey : yKey);
		if (!knob) return;
		const { from, to } = niceRange(knob.value, steps, knob.integer);
		if (axis === 'x') [xFrom, xTo, xEdited] = [from, to, false];
		else [yFrom, yTo, yEdited] = [from, to, false];
	}
	function onSteps() {
		if (!xEdited) resetRange('x');
		if (!yEdited) resetRange('y');
	}
	// Pick sensible axes when the knob list first arrives or a picked knob disappears.
	$: if (knobs.length && !knobFor(xKey)) {
		xKey = knobs[0].key;
		resetRange('x');
	}
	$: if (yKey && (!knobFor(yKey) || yKey === xKey)) yKey = '';
	$: if (!yKey && knobs.length > 1 && !yTouched) {
		yKey = knobs.find((knob) => knob.key !== xKey)?.key ?? '';
		resetRange('y');
	}

	$: xKnob = knobFor(xKey);
	$: yKnob = yKey ? knobFor(yKey) : undefined;
	$: xPlan = xKnob ? axisValues(xFrom, xTo, steps, xKnob) : [];
	$: yPlan = yKnob ? axisValues(yFrom, yTo, steps, yKnob) : [];
	$: cellCount = xPlan.length * Math.max(1, yPlan.length);

	function axisRequest(knob: KnobOption, values: number[]): HeatmapAxisRequest {
		return { target: knob.target, name: knob.name, indicator: knob.indicator, values };
	}
	function run() {
		if (!xKnob || !xPlan.length) return;
		dispatch('run', { x: axisRequest(xKnob, xPlan), y: yKnob && yPlan.length ? axisRequest(yKnob, yPlan) : null });
	}

	// ---- Grid ----------------------------------------------------------------
	$: xValues = view?.x.values ?? [];
	$: yValues = view?.y ? view.y.values : [null];
	$: displayRows = view?.y ? [...yValues].reverse() : yValues;
	$: cells = Object.values(view?.cells ?? {});
	$: values = cells.map((cell) => cell[metric]).filter((v): v is number => typeof v === 'number');
	$: scale = Math.max(metric === 'oos_trades' ? 1 : 0.01, ...values.map((v) => Math.abs(v)));
	$: best = cells.reduce<HeatmapCell | null>((top, cell) =>
		typeof cell.oos_return === 'number' && !cell.error && (!top || cell.oos_return > (top.oos_return ?? -Infinity)) ? cell : top, null);
	$: verdict = view && view.status !== 'running' ? heatmapVerdict(cells, xValues, yValues) : null;

	const pct = (v: number) => `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
	function shade(cell: HeatmapCell | undefined): string {
		const v = cell?.[metric];
		if (typeof v !== 'number' || cell?.error) return '';
		const strength = Math.min(1, Math.abs(v) / scale);
		const alpha = (0.1 + 0.6 * strength).toFixed(2);
		if (metric === 'oos_trades') return `background-color: rgba(255,255,255,${(0.04 + 0.3 * strength).toFixed(2)})`;
		return v >= 0 ? `background-color: rgba(34,197,94,${alpha})` : `background-color: rgba(239,68,68,${alpha})`;
	}
	function cellText(cell: HeatmapCell | undefined): string {
		const v = cell?.[metric];
		if (cell?.error) return '×';
		if (typeof v !== 'number') return '';
		return metric === 'oos_trades' ? String(v) : pct(v);
	}
	function cellTitle(cell: HeatmapCell | undefined, x: number, y: number | null): string {
		const where = `${view?.x.label} ${formatValue(x)}${view?.y ? ` · ${view.y.label} ${formatValue(y)}` : ''}`;
		if (!cell) return `${where}\nRunning…`;
		if (cell.error) return `${where}\n${cell.error}`;
		return `${where}\nOut-of-sample ${pct(cell.oos_return ?? 0)} on ${cell.oos_trades ?? 0} trades\nIn-sample ${pct(cell.in_return ?? 0)}\nClick to use these settings`;
	}
	const isCurrent = (x: number, y: number | null) => current.x === x && (!view?.y || current.y === y);
</script>

<div class="space-y-3">
	<div class="flex flex-wrap items-end gap-x-3 gap-y-2 text-[11px]">
		<label class="flex flex-col gap-1">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">Across (x)</span>
			<select bind:value={xKey} on:change={() => resetRange('x')} aria-label="heatmap x setting"
				class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[12px] text-white outline-none focus:border-white">
				{#each knobs as knob (knob.key)}<option value={knob.key}>{knob.label}</option>{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">From</span>
			<input type="number" bind:value={xFrom} on:input={() => (xEdited = true)} step="any" aria-label="x from" class="w-20 border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white" />
		</label>
		<label class="flex flex-col gap-1">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">To</span>
			<input type="number" bind:value={xTo} on:input={() => (xEdited = true)} step="any" aria-label="x to" class="w-20 border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white" />
		</label>
		<label class="flex flex-col gap-1">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">Down (y)</span>
			<select bind:value={yKey} on:change={() => { yTouched = true; resetRange('y'); }} aria-label="heatmap y setting"
				class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[12px] text-white outline-none focus:border-white">
				<option value="">— none —</option>
				{#each knobs.filter((knob) => knob.key !== xKey) as knob (knob.key)}<option value={knob.key}>{knob.label}</option>{/each}
			</select>
		</label>
		{#if yKnob}
			<label class="flex flex-col gap-1">
				<span class="text-[9px] uppercase tracking-wider text-[#555]">From</span>
				<input type="number" bind:value={yFrom} on:input={() => (yEdited = true)} step="any" aria-label="y from" class="w-20 border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-[9px] uppercase tracking-wider text-[#555]">To</span>
				<input type="number" bind:value={yTo} on:input={() => (yEdited = true)} step="any" aria-label="y to" class="w-20 border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white" />
			</label>
		{/if}
		<label class="flex flex-col gap-1">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">Steps</span>
			<select bind:value={steps} on:change={onSteps} aria-label="heatmap steps" class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[12px] text-white outline-none focus:border-white">
				{#each [3, 5, 7, 9] as n}<option value={n}>{n}</option>{/each}
			</select>
		</label>
		{#if view?.status === 'running'}
			<button type="button" on:click={() => dispatch('cancel')} class="terminal-button text-[10px]">Cancel</button>
		{:else}
			<button type="button" on:click={run} disabled={!canRun || !xPlan.length}
				class="terminal-button-primary text-[10px] disabled:opacity-40">{view ? 'Run again' : 'Run heatmap'}</button>
		{/if}
		<span class="pb-1 text-[10px] text-[#555]">{cellCount} backtest{cellCount === 1 ? '' : 's'}</span>
	</div>

	{#if !knobs.length}
		<div class="border border-dashed border-[#262626] px-3 py-6 text-center text-[12px] text-[#555]">
			This rule has no numbers to sweep. Add a knob or an indicator.
		</div>
	{:else if !view}
		<div class="border border-dashed border-[#262626] px-3 py-6 text-center text-[12px] text-[#555]">
			Sweep two settings to see whether the rule works across a range of them or only at one exact point.
		</div>
	{:else}
		{#if stale}
			<div class="border border-[#333] bg-[#111] px-3 py-1.5 text-[11px] text-[#999]" role="status">
				The rules, market or settings changed since this heatmap. Run it again to check the current version.
			</div>
		{/if}
		{#each view.warnings as warning}
			<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{warning}</div>
		{/each}
		<div class="flex flex-wrap items-center gap-3 text-[10px] text-[#666]">
			<div class="inline-flex" role="group" aria-label="heatmap metric">
				{#each METRICS as [key, label]}
					<button type="button" on:click={() => (metric = key)} aria-pressed={metric === key}
						class="-ml-px border px-2 py-0.5 first:ml-0 {metric === key ? 'relative border-white bg-white text-black' : 'border-[#2a2a2a] text-[#777] hover:text-white'}">{label}</button>
				{/each}
			</div>
			{#if view.status === 'running'}<span class="text-white">Running… {view.done} of {view.total}</span>{/if}
			{#if view.status === 'cancelled'}<span>Stopped at {view.done} of {view.total}</span>{/if}
			<span class="ml-auto">★ best out-of-sample · <span class="border border-white px-1 text-white">▢</span> current settings</span>
		</div>

		<div class="overflow-x-auto {stale ? 'opacity-60' : ''}" data-testid="heatmap-grid">
			{#if view.y}<div class="pb-1 text-[9px] uppercase tracking-wider text-[#666]">{view.y.label} ↑</div>{/if}
			<div class="grid min-w-[360px] gap-px" style="grid-template-columns: auto repeat({xValues.length}, minmax(44px, 1fr))">
				{#each displayRows as y}
					<div class="flex items-center justify-end pr-2 font-mono text-[10px] text-[#888]">{view.y ? formatValue(y) : ''}</div>
					{#each xValues as x}
						{@const cell = view.cells[cellKey(x, y)]}
						<button type="button" title={cellTitle(cell, x, y)} disabled={!cell || !!cell.error}
							on:click={() => dispatch('adopt', { x, y })}
							style={shade(cell)}
							class="relative flex h-9 items-center justify-center font-mono text-[10px] transition-colors
								{cell ? 'text-white hover:brightness-150' : 'animate-pulse bg-[#0d0d0d] text-[#333]'}
								{cell?.error ? 'bg-[#111] text-[#555]' : ''}
								{isCurrent(x, y) ? 'outline outline-2 -outline-offset-2 outline-white' : ''}">
							{cellText(cell)}
							{#if best && cell === best}<span class="absolute right-0.5 top-0 text-[9px] text-amber-300">★</span>{/if}
						</button>
					{/each}
				{/each}
				<div></div>
				{#each xValues as x}<div class="pt-1 text-center font-mono text-[10px] text-[#888]">{formatValue(x)}</div>{/each}
			</div>
			<div class="pt-1 text-center text-[9px] uppercase tracking-wider text-[#666]">{view.x.label} →</div>
		</div>

		{#if verdict}
			<div class="flex items-baseline gap-2 text-[12px]" data-testid="heatmap-verdict">
				<span class="border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider
					{verdict.status === 'plateau' ? 'border-emerald-700 text-emerald-400' : verdict.status === 'spike' || verdict.status === 'losing' ? 'border-red-800 text-red-400' : 'border-amber-700 text-amber-400'}">{verdict.status}</span>
				<span class="text-[#ccc]">{verdict.text}</span>
			</div>
		{/if}
		<p class="text-[10px] text-[#555]">
			Each cell is a full backtest of those settings with your execution settings. All {view.total} count toward the deflated Sharpe: picking the best cell is itself a selection.
		</p>
	{/if}
</div>
