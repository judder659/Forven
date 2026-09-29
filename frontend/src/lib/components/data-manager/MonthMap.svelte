<script lang="ts">
	// Coverage timeline: one row per year, one cell per month, shaded by the
	// share of expected bars that are stored. Marks show synthetic (hatched),
	// CSV-patched (dot) and restated (corner) months. Click or Enter zooms the
	// chart to that month. Arrow keys move between months.
	import { createEventDispatcher } from 'svelte';
	import type { MonthCell } from '$lib/api/dataManagerTypes';
	import { formatCount, formatPercent } from './format';

	export let months: MonthCell[] = [];
	export let selected: string | null = null;

	const dispatch = createEventDispatcher<{ select: MonthCell }>();
	const NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

	$: byMonth = new Map(months.map((m) => [m.month, m]));
	$: years = months.length
		? Array.from({ length: Number(months.at(-1)!.month.slice(0, 4)) - Number(months[0].month.slice(0, 4)) + 1 }, (_, i) => Number(months[0].month.slice(0, 4)) + i)
		: [];
	$: order = months.map((m) => m.month);
	let active: string | null = null;
	$: if ((!active || !byMonth.has(active)) && order.length) active = selected && byMonth.has(selected) ? selected : order.at(-1) ?? null;

	const key = (year: number, month: number) => `${year}-${String(month + 1).padStart(2, '0')}`;
	const share = (cell: MonthCell) => (cell.expected > 0 ? cell.present / cell.expected : 0);

	function describe(cell: MonthCell): string {
		const parts = [`${cell.month}: ${formatCount(cell.present)} of ${formatCount(cell.expected)} bars (${formatPercent(share(cell))})`];
		if (cell.synthetic) parts.push(`${formatCount(cell.synthetic)} synthetic (forward-filled)`);
		if (cell.patched) parts.push(`${formatCount(cell.patched)} from a CSV patch`);
		if (cell.restated) parts.push(`${formatCount(cell.restated)} restated by the venue`);
		return parts.join(' · ');
	}

	function focusCell(month: string) {
		active = month;
		document.getElementById(`dm-month-${month}`)?.focus();
	}

	function onKey(event: KeyboardEvent, cell: MonthCell) {
		const i = order.indexOf(cell.month);
		const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowDown' ? 12 : event.key === 'ArrowUp' ? -12 : 0;
		if (step) {
			event.preventDefault();
			const next = order[Math.max(0, Math.min(order.length - 1, i + step))];
			if (next) focusCell(next);
		} else if (event.key === 'Home' || event.key === 'End') {
			event.preventDefault();
			focusCell(event.key === 'Home' ? order[0] : order.at(-1)!);
		}
	}
</script>

{#if !months.length}
	<p class="px-3 py-4 text-[11px] text-sc-ink3">No monthly coverage to show.</p>
{:else}
	<div class="overflow-x-auto px-3 py-2">
		<div role="grid" aria-label="Bars stored per month" class="inline-grid grid-cols-[34px_repeat(12,minmax(22px,1fr))] gap-[3px] text-[9px]">
			<div role="presentation"></div>
			{#each NAMES as name}<div role="columnheader" class="text-center uppercase tracking-wider text-sc-ink3">{name.slice(0, 1)}<span class="sr-only">{name.slice(1)}</span></div>{/each}
			{#each years as year (year)}
				<div role="rowheader" class="self-center pr-1 text-right font-mono text-[10px] text-sc-ink3">{year}</div>
				{#each NAMES as _, m}
					{@const cell = byMonth.get(key(year, m))}
					{#if cell}
						{@const s = share(cell)}
						<button type="button" id="dm-month-{cell.month}" role="gridcell" tabindex={active === cell.month ? 0 : -1}
							aria-label={describe(cell)} title={describe(cell)} aria-selected={selected === cell.month}
							on:click={() => dispatch('select', cell)} on:keydown={(e) => onKey(e, cell)} on:focus={() => (active = cell.month)}
							class="rounded-md relative h-4 border outline-none focus-visible:ring-1 focus-visible:ring-sc-ink {selected === cell.month ? 'border-sc-ink' : s < 1 ? 'border-amber-900/80' : 'border-transparent'}"
							style="background: rgba(209, 213, 219, {cell.expected ? 0.1 + 0.75 * s : 0});">
							{#if cell.synthetic}<span class="absolute inset-0 bg-[repeating-linear-gradient(135deg,rgba(0,0,0,0.55)_0_2px,transparent_2px_4px)]" aria-hidden="true"></span>{/if}
							{#if cell.patched}<span class="absolute right-0.5 top-0.5 h-1 w-1 rounded-full bg-sky-400" aria-hidden="true"></span>{/if}
							{#if cell.restated}<span class="absolute left-0 top-0 h-0 w-0 border-l-[5px] border-t-[5px] border-l-transparent border-t-amber-400" aria-hidden="true"></span>{/if}
						</button>
					{:else}
						<div role="gridcell" aria-label="{key(year, m)}: outside the series" class="h-4 border border-dashed border-sc-line"></div>
					{/if}
				{/each}
			{/each}
		</div>
		<div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9px] text-sc-ink3">
			<span class="flex items-center gap-1"><span class="h-2 w-6 bg-gradient-to-r from-[rgba(209,213,219,0.1)] to-[rgba(209,213,219,0.85)]"></span> share of bars stored</span>
			<span class="flex items-center gap-1"><span class="h-2 w-2 border border-amber-900/80"></span> incomplete month</span>
			<span class="flex items-center gap-1"><span class="h-2 w-2 bg-[repeating-linear-gradient(135deg,#777_0_2px,transparent_2px_4px)]"></span> synthetic</span>
			<span class="flex items-center gap-1"><span class="h-1 w-1 rounded-full bg-sky-400"></span> CSV patch</span>
			<span class="flex items-center gap-1"><span class="h-0 w-0 border-l-[5px] border-t-[5px] border-l-transparent border-t-amber-400"></span> restated</span>
			<span class="ml-auto">click a month to zoom the chart · UTC months</span>
		</div>
	</div>
{/if}
