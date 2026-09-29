<script lang="ts">
	import type { TradeRow } from '$lib/utils/strategyContainer/metrics';
	import { EXIT_LABELS, REGIME_LABELS } from '$lib/utils/strategyContainer/metrics';
	import { fmtFraction, fmtPct, fmtUsd, fmtUtcStamp, signClass } from '$lib/utils/strategyContainer/format';

	/** A run's stored trades: sortable, filterable by side and exit, UTC times. */
	export let rows: TradeRow[] = [];
	export let pageSize = 30;

	type SortKey = 'index' | 'side' | 'entry' | 'exit' | 'bars' | 'exitReason' | 'regime' | 'size' | 'pnl' | 'returnPct';
	let side: 'all' | 'long' | 'short' = 'all';
	let exitFilter = 'all';
	let sortKey: SortKey = 'index';
	let dir: 1 | -1 = 1;
	let showAll = false;

	const COLUMNS: Array<{ key: SortKey; label: string; left?: boolean }> = [
		{ key: 'index', label: '#' },
		{ key: 'side', label: 'Side', left: true },
		{ key: 'entry', label: 'Entry (UTC)', left: true },
		{ key: 'exit', label: 'Exit (UTC)', left: true },
		{ key: 'bars', label: 'Bars' },
		{ key: 'exitReason', label: 'Exit', left: true },
		{ key: 'regime', label: 'Regime at entry', left: true },
		{ key: 'size', label: 'Size' },
		{ key: 'pnl', label: 'PnL' },
		{ key: 'returnPct', label: 'Return' },
	];

	$: exitsPresent = [...new Set(rows.map((row) => row.exitReason))].sort();
	$: filtered = rows.filter((row) => (side === 'all' || row.side === side) && (exitFilter === 'all' || row.exitReason === exitFilter));
	$: sorted = [...filtered].sort((a, b) => {
		const va = a[sortKey];
		const vb = b[sortKey];
		if (va === vb) return (a.index - b.index) * dir;
		if (va === null || va === undefined) return 1;
		if (vb === null || vb === undefined) return -1;
		return (typeof va === 'number' && typeof vb === 'number' ? va - vb : String(va).localeCompare(String(vb))) * dir;
	});
	$: shown = showAll ? sorted : sorted.slice(0, pageSize);
	$: net = filtered.reduce((sum, row) => sum + row.pnl, 0);

	function sortBy(key: SortKey) {
		dir = sortKey === key ? (dir === 1 ? -1 : 1) : key === 'index' ? 1 : -1;
		sortKey = key;
	}
	const pill = (active: boolean) => `px-2.5 py-0.5 text-[11px] ${active ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}`;
</script>

<div class="grid gap-2.5" data-testid="trade-table">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<div class="text-[12px] text-sc-ink3" data-testid="trade-table-summary">{filtered.length} of {rows.length} trades · net <span class={signClass(net)}>{fmtUsd(net)}</span></div>
		<div class="flex flex-wrap gap-2">
			<span class="inline-flex overflow-hidden rounded-full border border-sc-line2" role="group" aria-label="Side filter">
				{#each ['all', 'long', 'short'] as option (option)}
					<button type="button" class={pill(side === option)} aria-pressed={side === option} on:click={() => { side = option as typeof side; showAll = false; }}>{option === 'all' ? 'All' : option === 'long' ? 'Long' : 'Short'}</button>
				{/each}
			</span>
			<span class="inline-flex overflow-hidden rounded-full border border-sc-line2" role="group" aria-label="Exit filter">
				<button type="button" class={pill(exitFilter === 'all')} aria-pressed={exitFilter === 'all'} on:click={() => { exitFilter = 'all'; showAll = false; }}>Any exit</button>
				{#each exitsPresent as reason (reason)}
					<button type="button" class={pill(exitFilter === reason)} aria-pressed={exitFilter === reason} on:click={() => { exitFilter = reason; showAll = false; }}>{EXIT_LABELS[reason] ?? reason.replace(/_/g, ' ')}</button>
				{/each}
			</span>
		</div>
	</div>
	<div class="max-h-[560px] overflow-auto rounded border border-sc-line">
		<table class="w-full min-w-[880px] border-collapse text-[12px] font-plex-mono tabular-nums">
			<thead class="sticky top-0 bg-sc-panel">
				<tr class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3">
					{#each COLUMNS as column (column.key)}
						<th class={`px-2.5 py-1.5 font-normal ${column.left ? 'text-left' : 'text-right'}`} aria-sort={sortKey === column.key ? (dir === 1 ? 'ascending' : 'descending') : 'none'}>
							<button type="button" class="uppercase tracking-[0.06em] hover:text-sc-ink" on:click={() => sortBy(column.key)}>{column.label}{sortKey === column.key ? (dir === 1 ? ' ↑' : ' ↓') : ''}</button>
						</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each shown as row (row.index)}
					<tr class="border-t border-sc-line hover:bg-sc-hover">
						<td class="px-2.5 py-1.5 text-right text-sc-ink3">{row.index}</td>
						<td class="px-2.5 py-1.5 text-left"><span class={`rounded-full border px-1.5 text-[10px] uppercase ${row.side === 'long' ? 'border-[#139a9f]/50 text-[#5ccac4]' : 'border-[#e0663f]/50 text-[#f2956f]'}`}>{row.side}</span></td>
						<td class="px-2.5 py-1.5 text-left text-sc-ink2">{fmtUtcStamp(row.entry)}</td>
						<td class="px-2.5 py-1.5 text-left text-sc-ink2">{fmtUtcStamp(row.exit)}</td>
						<td class="px-2.5 py-1.5 text-right text-sc-ink">{row.bars ?? '—'}</td>
						<td class="px-2.5 py-1.5 text-left text-sc-ink2">{EXIT_LABELS[row.exitReason] ?? row.exitReason.replace(/_/g, ' ')}</td>
						<td class="px-2.5 py-1.5 text-left text-sc-ink3">{row.regime ? REGIME_LABELS[row.regime] ?? row.regime : '—'}</td>
						<td class="px-2.5 py-1.5 text-right text-sc-ink">{row.size === null ? '—' : fmtFraction(row.size, 0, false)}</td>
						<td class={`px-2.5 py-1.5 text-right ${signClass(row.pnl)}`}>{fmtUsd(row.pnl)}</td>
						<td class={`px-2.5 py-1.5 text-right ${signClass(row.returnPct)}`}>{fmtPct(row.returnPct, 2)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if !showAll && sorted.length > pageSize}
		<button type="button" class="justify-self-start rounded-full border border-sc-line2 px-3 py-0.5 text-[11px] text-sc-ink2 hover:text-sc-ink" on:click={() => (showAll = true)}>Show all {sorted.length}</button>
	{/if}
	{#if rows.length === 0}
		<div class="text-[12px] text-sc-ink3">This run stored no trades.</div>
	{/if}
</div>
