<script lang="ts">
	/** Strategy list: state, a plain reason, the next bar, and a P&L sparkline per strategy. */
	import { createEventDispatcher } from 'svelte';
	import type { DeskMode } from '$lib/api/desk';
	import type { LifecycleStrategy } from '$lib/api';
	import { describeRefusal, tradeModeLabel } from '$lib/utils/tradingDesk/describe';
	import { ago, cap1, dur, fmtDateTime, fmtPct, fmtPx, fmtQty, fmtTime, fmtUsd, lastBarClose, nextBarClose, toneClass } from '$lib/utils/tradingDesk/format';
	import { STATE_LABEL, type DeskRow, type RailFilter } from '$lib/utils/tradingDesk/rows';
	import DeskSparkline from './DeskSparkline.svelte';


	export let mode: DeskMode;
	export let rows: DeskRow[] = [];
	export let selectedId: string | null = null;
	export let filter: RailFilter = 'all';
	export let archived: LifecycleStrategy[] = [];
	export let archivedLoading = false;
	export let selectedArchivedId: string | null = null;
	export let now = Date.now();

	const dispatch = createEventDispatcher<{
		select: string;
		filter: RailFilter;
		selectArchived: LifecycleStrategy;
	}>();

	const CHIP: Record<string, string> = {
		exit_blocked: 'border-[#e5574f]/50 text-[#f3a39d]',
		stale: 'border-[#e5574f]/50 text-[#f3a39d]',
		in_position: 'border-[#8fb0ff]/45 text-[#8fb0ff]',
		blocked: 'border-[#e7b24a]/45 text-[#e7b24a]',
		watching: 'border-sc-line2 text-sc-ink2',
	};

	$: counts = {
		all: rows.length,
		in_position: rows.filter((row) => row.state === 'in_position' || row.state === 'exit_blocked').length,
		blocked: rows.filter((row) => row.state === 'blocked' || row.state === 'stale').length,
		watching: rows.filter((row) => row.state === 'watching').length,
	};
	$: shown = rows.filter((row) =>
		filter === 'all' ? true
			: filter === 'in_position' ? row.state === 'in_position' || row.state === 'exit_blocked'
				: filter === 'blocked' ? row.state === 'blocked' || row.state === 'stale'
					: filter === 'watching' ? row.state === 'watching' : false);
	$: filters = [
		['all', 'All', counts.all],
		['in_position', 'Positions', counts.in_position],
		['blocked', 'Blocked', counts.blocked],
		['watching', 'Watching', counts.watching],
		...(mode === 'paper' ? [['archived', 'Archived', archived.length || null]] : []),
	] as Array<[RailFilter, string, number | null]>;

	function reason(row: DeskRow): { text: string; tone: string } {
		if (row.legs.length) {
			const leg = row.legs[0];
			const math = row.legMath[0];
			const extra = row.legs.length > 1 ? ` · +${row.legs.length - 1} leg` : '';
			if (row.state === 'exit_blocked') return { text: `Exit refused · ${cap1(leg.side)} ${fmtQty(leg.size)} ${row.asset}`, tone: 'text-[#f3a39d]' };
			return { text: `${cap1(leg.side)} ${fmtQty(leg.size)} ${row.asset} · ${fmtUsd(math?.pnl, { signed: true })}${leg.stop !== null ? ` · stop ${fmtPx(leg.stop)}` : ''}${extra}`, tone: 'text-sc-ink2' };
		}
		if (row.state === 'stale') return { text: `Not evaluated since ${ago(row.fleet?.last_scan?.at, now)}`, tone: 'text-[#f3a39d]' };
		if (row.state === 'blocked' && row.fleet) {
			const blocked = row.fleet.blocked_entries;
			return { text: `${blocked.count} entries refused in ${blocked.window_days}d · last ${ago(blocked.last_at, now)}`, tone: 'text-[#e7b24a]' };
		}
		const pending = row.session.pending_signals?.[0];
		if (pending) return { text: `${pending.signal_type === 'exit' ? 'Exit' : 'Entry'} ${fmtPct(pending.distance_pct, 1, false)} away: ${pending.description}`, tone: 'text-sc-ink2' };
		return { text: `No signal at the ${fmtTime(lastBarClose(row.timeframe, now))} close`, tone: 'text-sc-ink2' };
	}
</script>

<aside class="flex min-w-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label={mode === 'live' ? 'Live strategies' : 'Paper strategies'} data-testid="desk-rail">
	<div class="flex flex-wrap items-center gap-2 border-b border-sc-line px-3 py-2">
		<h2 class="text-[13px] font-semibold text-sc-ink">{mode === 'live' ? 'Live strategies' : 'Paper strategies'}</h2>
		<span class="text-[12px] text-sc-ink3">{rows.length} {mode} · {counts.in_position} in position</span>
	</div>
	<div class="flex flex-wrap gap-1.5 border-b border-sc-line px-3 py-2" role="group" aria-label="Filter strategies">
		{#each filters as [key, text, count]}
			<button
				type="button"
				class={`rounded-full border px-2.5 py-px text-[12px] ${filter === key ? 'border-sc-ink4 bg-sc-raise text-sc-ink' : 'border-sc-line2 text-sc-ink3 hover:text-sc-ink2'}`}
				aria-pressed={filter === key}
				on:click={() => dispatch('filter', key)}
			>{text}{#if count !== null}<span class="ml-1 font-plex-mono text-[11px] text-sc-ink4">{count}</span>{/if}</button>
		{/each}
	</div>
	<div class="max-h-[780px] min-h-0 flex-1 overflow-y-auto max-lg:flex max-lg:max-h-none max-lg:overflow-x-auto">
		{#if filter === 'archived'}
			{#if archivedLoading}
				<div class="px-3 py-6 text-center text-[12px] text-sc-ink3">Loading archived strategies…</div>
			{:else if archived.length === 0}
				<div class="px-3 py-6 text-center text-[12px] text-sc-ink3">No archived strategies.</div>
			{:else}
				{#each archived as strategy (strategy.id)}
					<button
						type="button"
						class={`grid w-full gap-0.5 border-b border-l-2 border-sc-line px-3 py-2 text-left hover:bg-sc-hover max-lg:min-w-[250px] ${selectedArchivedId === strategy.id ? 'border-l-sc-ink bg-sc-panel2' : 'border-l-transparent'}`}
						aria-pressed={selectedArchivedId === strategy.id}
						on:click={() => dispatch('selectArchived', strategy)}
					>
						<div class="flex items-center justify-between gap-2">
							<span class="font-plex-mono text-[13px] font-medium text-sc-ink">{strategy.display_id || strategy.name || strategy.id}</span>
							<span class="font-plex-cond text-[10.5px] font-semibold uppercase tracking-[0.08em] text-[#f2956f]">{String(strategy.state || '').replace(/_/g, ' ')}</span>
						</div>
						<span class="text-[11.5px] text-sc-ink3">{strategy.symbol || '—'} · {fmtDateTime(strategy.updated_at)}</span>
						<span class="truncate text-[12px] text-sc-ink2" title={strategy.blocked_reason ?? ''}>{describeRefusal(strategy.blocked_reason).short}</span>
					</button>
				{/each}
			{/if}
		{:else if shown.length === 0}
			<div class="px-3 py-6 text-center text-[12px] text-sc-ink3">
				{rows.length === 0 ? (mode === 'live' ? 'No strategy is trading real money.' : 'No strategy is in paper right now.') : 'No strategy matches this filter.'}
			</div>
		{:else}
			{#each shown as row (row.session.id)}
				{@const why = reason(row)}
				{@const pl = row.legs.length ? row.openPnl : row.stats.net}
				<button
					type="button"
					class={`grid w-full gap-0.5 border-b border-l-2 border-sc-line px-3 py-2.5 text-left hover:bg-sc-hover max-lg:min-w-[250px] max-lg:border-b-0 max-lg:border-r ${selectedId === row.session.id ? 'border-l-sc-ink bg-sc-panel2' : 'border-l-transparent'}`}
					aria-pressed={selectedId === row.session.id}
					data-testid="desk-rail-item"
					on:click={() => dispatch('select', row.session.id)}
				>
					<div class="flex items-center justify-between gap-2">
						<span class="font-plex-mono text-[13px] font-medium text-sc-ink">{row.sid}</span>
						<span class={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 font-plex-cond text-[10.5px] font-semibold uppercase tracking-[0.08em] ${CHIP[row.state]}`}>
							<i class="inline-block h-1.5 w-1.5 rounded-full bg-current"></i>{STATE_LABEL[row.state]}
						</span>
					</div>
					<span class="truncate text-[12px] text-sc-ink2">{row.name}</span>
					<span class="text-[11.5px] text-sc-ink3">{row.asset} · {row.timeframe} · {tradeModeLabel(row.session.trade_mode)} · {row.session.leverage ?? 1}×</span>
					<span class={`text-[12px] ${why.tone}`}>{why.text}</span>
					<div class="mt-0.5 grid grid-cols-[minmax(0,1fr)_76px_auto] items-center gap-2 text-[11.5px] text-sc-ink3">
						<span>Next bar <b class="font-plex-mono font-medium text-sc-ink2">{dur(nextBarClose(row.timeframe, now) - now)}</b></span>
						<DeskSparkline points={row.stats.cumulative} label={`${row.sid} cumulative ${mode} P&L`} />
						<span class={`text-right font-plex-mono text-[12px] ${toneClass(pl)}`} title={row.legs.length ? 'Open P&L' : `Net ${mode} P&L`}>{fmtUsd(pl, { signed: true })}</span>
					</div>
				</button>
			{/each}
		{/if}
	</div>
</aside>
