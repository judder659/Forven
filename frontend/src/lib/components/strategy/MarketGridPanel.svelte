<script lang="ts" context="module">
	import type { MarketRow } from '$lib/api';

	/** A market grid as the page assembles it, one market request at a time. */
	export interface MarketView {
		symbols: string[];
		timeframes: string[];
		rows: Record<string, MarketRow>;
		done: number;
		total: number;
		status: 'running' | 'done' | 'cancelled';
		warnings: string[];
	}

	export const marketKey = (symbol: string, timeframe: string) => `${symbol}|${timeframe}`;
</script>

<script lang="ts">
	// The same rules and execution settings on several markets. An edge that only
	// shows up on the market it was built on is more likely fitted to that market.
	import { createEventDispatcher } from 'svelte';
	import { hasLocalData, marketVerdict } from '$lib/utils/creatorGrids';

	export let view: MarketView | null = null;
	export let availability: Map<string, Set<string>> | null = null;
	export let symbolOptions: string[] = [];
	export let currentSymbol = '';
	export let currentTimeframe = '';
	export let stale = false;
	export let canRun = true;
	/** The footnote under the grid. */
	export let note = 'Each market is a full backtest with your execution settings, split in-sample / out-of-sample like Run Backtest. Markets without local data are not downloaded; collect them on the Data page. Every result counts toward the deflated Sharpe.';

	const dispatch = createEventDispatcher<{
		run: { symbols: string[]; timeframes: string[] };
		cancel: void;
		pick: { symbol: string; timeframe: string };
	}>();

	const TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d'];
	const MAJORS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'BNB/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT'];
	const MAX_MARKETS = 24;

	let symbols: string[] = [];
	let timeframes: string[] = [];
	let adding = '';
	let seeded = false;

	// Start from the current market plus a few majors with local data.
	$: if (!seeded && availability) {
		seeded = true;
		const current = currentSymbol.trim().toUpperCase();
		const others = MAJORS.filter((symbol) => symbol !== current && (availability?.has(symbol.split('/')[0]) ?? false));
		symbols = [current, ...others].filter(Boolean).slice(0, 4);
		timeframes = [...new Set([currentTimeframe, '1h', '4h', '1d'])].filter(Boolean).slice(0, 3);
	}

	function addSymbol() {
		const symbol = adding.trim().toUpperCase();
		if (symbol && !symbols.includes(symbol)) symbols = [...symbols, symbol];
		adding = '';
	}
	function toggleTimeframe(tf: string) {
		timeframes = timeframes.includes(tf)
			? timeframes.filter((t) => t !== tf)
			: TIMEFRAMES.filter((t) => t === tf || timeframes.includes(t));
	}
	$: count = symbols.length * timeframes.length;
	$: missing = availability ? symbols.flatMap((s) => timeframes.filter((t) => !hasLocalData(availability!, s, t))).length : 0;

	type Metric = 'oos' | 'in' | 'trades';
	const METRICS: [Metric, string][] = [['oos', 'Out-of-sample return'], ['in', 'In-sample return'], ['trades', 'Out-of-sample trades']];
	let metric: Metric = 'oos';

	$: rows = Object.values(view?.rows ?? {});
	$: verdict = view && view.status !== 'running' ? marketVerdict(rows) : null;
	const metricOf = (row: MarketRow | undefined): number | null => {
		if (!row || row.status !== 'ok') return null;
		if (metric === 'trades') return row.out_of_sample?.trades ?? null;
		return (metric === 'oos' ? row.out_of_sample : row.in_sample)?.net_return ?? null;
	};
	$: scale = Math.max(metric === 'trades' ? 1 : 0.01, ...rows.map(metricOf).filter((v): v is number => v !== null).map(Math.abs));
	const pct = (v: number) => `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
	function shade(row: MarketRow | undefined): string {
		const v = metricOf(row);
		if (v === null) return '';
		const strength = Math.min(1, Math.abs(v) / scale);
		if (metric === 'trades') return `background-color: rgba(255,255,255,${(0.04 + 0.3 * strength).toFixed(2)})`;
		return `background-color: rgba(${v >= 0 ? '34,197,94' : '239,68,68'},${(0.1 + 0.6 * strength).toFixed(2)})`;
	}
	function title(row: MarketRow | undefined, symbol: string, tf: string): string {
		if (!row) return `${symbol} ${tf}\nRunning…`;
		if (row.status !== 'ok') return `${symbol} ${tf}\n${row.message ?? row.status}`;
		const out = row.out_of_sample;
		const ins = row.in_sample;
		return `${symbol} ${tf}\nOut-of-sample ${pct(out?.net_return ?? 0)} on ${out?.trades ?? 0} trades, win rate ${((out?.win_rate ?? 0) * 100).toFixed(0)}%, max drawdown ${((out?.max_drawdown ?? 0) * 100).toFixed(1)}%\nIn-sample ${pct(ins?.net_return ?? 0)} on ${ins?.trades ?? 0} trades\nClick to switch the preview to this market`;
	}
	const isCurrent = (symbol: string, tf: string) => symbol === currentSymbol.trim().toUpperCase() && tf === currentTimeframe;
</script>

<div class="space-y-3">
	<div class="flex flex-wrap items-center gap-2 text-[11px]">
		<span class="text-[9px] uppercase tracking-wider text-[#555]">Markets</span>
		{#each symbols as symbol (symbol)}
			<span class="inline-flex items-center gap-1 border border-[#2a2a2a] px-1.5 py-0.5 font-mono text-[11px] text-[#ddd]">
				{symbol}
				<button type="button" on:click={() => (symbols = symbols.filter((s) => s !== symbol))} aria-label={`remove ${symbol}`} class="text-[#555] hover:text-red-400">×</button>
			</span>
		{/each}
		<form class="inline-flex" on:submit|preventDefault={addSymbol}>
			<input bind:value={adding} list="market-grid-symbols" placeholder="add…" aria-label="add market"
				class="w-24 border border-[#2a2a2a] bg-black px-1.5 py-0.5 font-mono text-[11px] text-white outline-none focus:border-white" />
			<datalist id="market-grid-symbols">{#each symbolOptions as option}<option value={option}></option>{/each}</datalist>
		</form>
	</div>
	<div class="flex flex-wrap items-center gap-2 text-[11px]">
		<span class="text-[9px] uppercase tracking-wider text-[#555]">Timeframes</span>
		<div class="inline-flex" role="group" aria-label="grid timeframes">
			{#each TIMEFRAMES as tf}
				<button type="button" on:click={() => toggleTimeframe(tf)} aria-pressed={timeframes.includes(tf)}
					class="-ml-px border px-2 py-0.5 font-mono text-[11px] first:ml-0 {timeframes.includes(tf) ? 'relative border-white bg-white text-black' : 'border-[#2a2a2a] text-[#777] hover:text-white'}">{tf}</button>
			{/each}
		</div>
		{#if view?.status === 'running'}
			<button type="button" on:click={() => dispatch('cancel')} class="terminal-button text-[10px]">Cancel</button>
		{:else}
			<button type="button" on:click={() => dispatch('run', { symbols, timeframes })} disabled={!canRun || !count || count > MAX_MARKETS || !availability}
				class="terminal-button-primary text-[10px] disabled:opacity-40">{view ? 'Compare again' : 'Compare markets'}</button>
		{/if}
		<span class="text-[10px] {count > MAX_MARKETS ? 'text-amber-400' : 'text-[#555]'}">
			{count} market{count === 1 ? '' : 's'}{count > MAX_MARKETS ? ` (at most ${MAX_MARKETS})` : ''}{missing ? ` · ${missing} without local data` : ''}
		</span>
	</div>

	{#if !availability}
		<div class="px-1 py-4 text-center text-[12px] text-[#555]">Reading which markets have local data…</div>
	{:else if !view}
		<div class="border border-dashed border-[#262626] px-3 py-6 text-center text-[12px] text-[#555]">
			Run the same rules on other markets and timeframes. An edge that only works where it was built is often fitted to that market.
		</div>
	{:else}
		{#if stale}
			<div class="border border-[#333] bg-[#111] px-3 py-1.5 text-[11px] text-[#999]" role="status">
				The rules or settings changed since this comparison. Run it again to check the current version.
			</div>
		{/if}
		{#each view.warnings as warning}
			<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{warning}</div>
		{/each}
		<div class="flex flex-wrap items-center gap-3 text-[10px] text-[#666]">
			<div class="inline-flex" role="group" aria-label="market metric">
				{#each METRICS as [key, label]}
					<button type="button" on:click={() => (metric = key)} aria-pressed={metric === key}
						class="-ml-px border px-2 py-0.5 first:ml-0 {metric === key ? 'relative border-white bg-white text-black' : 'border-[#2a2a2a] text-[#777] hover:text-white'}">{label}</button>
				{/each}
			</div>
			{#if view.status === 'running'}<span class="text-white">Running… {view.done} of {view.total}</span>{/if}
			{#if view.status === 'cancelled'}<span>Stopped at {view.done} of {view.total}</span>{/if}
			<span class="ml-auto"><span class="border border-white px-1 text-white">▢</span> the preview's market</span>
		</div>
		<div class="overflow-x-auto {stale ? 'opacity-60' : ''}" data-testid="market-grid">
			<div class="grid min-w-[320px] gap-px" style="grid-template-columns: auto repeat({view.timeframes.length}, minmax(64px, 1fr))">
				<div></div>
				{#each view.timeframes as tf}<div class="pb-1 text-center font-mono text-[10px] text-[#888]">{tf}</div>{/each}
				{#each view.symbols as symbol}
					<div class="flex items-center justify-end pr-2 font-mono text-[11px] text-[#aaa]">{symbol}</div>
					{#each view.timeframes as tf}
						{@const row = view.rows[marketKey(symbol, tf)]}
						<button type="button" title={title(row, symbol, tf)} disabled={!row || row.status === 'no_data'}
							on:click={() => dispatch('pick', { symbol, timeframe: tf })}
							style={shade(row)}
							class="flex h-11 flex-col items-center justify-center font-mono text-[11px] transition-colors
								{!row ? 'animate-pulse bg-[#0d0d0d]' : row.status === 'ok' ? 'text-white hover:brightness-150' : 'bg-[#0b0b0b] text-[#555]'}
								{isCurrent(symbol, tf) ? 'outline outline-2 -outline-offset-2 outline-white' : ''}">
							{#if row?.status === 'ok'}
								<span>{metric === 'trades' ? row.out_of_sample?.trades ?? 0 : pct(metricOf(row) ?? 0)}</span>
								{#if metric !== 'trades'}<span class="text-[9px] text-white/60">{row.out_of_sample?.trades ?? 0} tr</span>{/if}
							{:else if row?.status === 'no_data'}
								<span class="text-[10px]">no data</span>
							{:else if row}
								<span class="text-[10px]">—</span>
							{/if}
						</button>
					{/each}
				{/each}
			</div>
		</div>
		{#if verdict}
			<div class="flex items-baseline gap-2 text-[12px]" data-testid="market-verdict">
				<span class="border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider
					{verdict.status === 'broad' ? 'border-emerald-700 text-emerald-400' : verdict.status === 'narrow' ? 'border-amber-700 text-amber-400' : verdict.status === 'none' ? 'border-red-800 text-red-400' : 'border-[#333] text-[#888]'}">{verdict.status}</span>
				<span class="text-[#ccc]">{verdict.text}</span>
			</div>
		{/if}
		<p class="text-[10px] text-[#555]">{note}</p>
	{/if}
</div>
