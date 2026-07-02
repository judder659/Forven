<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import {
		getSeriesOHLCV,
		getDataQualityExtended,
		getStreamRows,
		backfillGaps,
		type OHLCVResponse,
		type DataQualityExtended,
		type StreamRowsResponse
	} from '$lib/api/data';
	import CandlestickChart from '$lib/components/CandlestickChart.svelte';
	import DataTableView from '$lib/components/research/DataTableView.svelte';

	export let symbol: string;
	export let timeframe: string;

	const dispatch = createEventDispatcher<{ close: void; changed: void }>();

	let series: OHLCVResponse | null = null;
	let quality: DataQualityExtended | null = null;
	let loading = true;
	let error: string | null = null;
	let filling = false;

	// Viewer state
	type StreamView = 'ohlcv' | 'funding' | 'oi';
	const STREAM_TABS: { id: StreamView; label: string }[] = [
		{ id: 'ohlcv', label: 'OHLCV' },
		{ id: 'funding', label: 'Funding' },
		{ id: 'oi', label: 'OI' }
	];
	let streamView: StreamView = 'ohlcv';
	let ohlcvView: 'chart' | 'table' = 'chart';
	let streamRows: StreamRowsResponse | null = null;
	let streamRowsLoading = false;
	let streamRowsError: string | null = null;
	let loadedStreamKey = '';

	function scoreFrom(q: DataQualityExtended): number {
		// Mirror the backend leaderboard heuristic closely enough for a headline number.
		let score = 100;
		score -= Math.min(40, q.gaps * 2);
		score -= Math.min(20, q.null_values);
		score -= Math.min(20, (q.integrity.invalid_high_low + q.integrity.invalid_close_range) * 5);
		score -= Math.min(10, q.outliers.close + q.outliers.volume);
		if (q.freshness.is_stale) score -= 10;
		return Math.max(0, Math.round(score));
	}

	function scoreClass(score: number): string {
		if (score >= 90) return 'text-emerald-400';
		if (score >= 70) return 'text-yellow-400';
		return 'text-red-400';
	}

	async function load() {
		loading = true;
		error = null;
		try {
			[series, quality] = await Promise.all([
				getSeriesOHLCV(symbol, timeframe, 300),
				getDataQualityExtended(symbol, timeframe)
			]);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load series detail';
		} finally {
			loading = false;
		}
	}

	async function loadStreamRows(s: 'funding' | 'oi') {
		const key = `${symbol}|${timeframe}|${s}`;
		loadedStreamKey = key;
		streamRowsLoading = true;
		streamRows = null;
		streamRowsError = null;
		try {
			const res = await getStreamRows(symbol, s, timeframe, 1000);
			if (loadedStreamKey === key) streamRows = res; // ignore a superseded response
		} catch (e) {
			// Distinguish a load failure (e.g. endpoint unavailable) from genuinely no data.
			if (loadedStreamKey === key) {
				streamRowsError = e instanceof Error ? e.message : 'Failed to load rows';
			}
		} finally {
			if (loadedStreamKey === key) streamRowsLoading = false;
		}
	}

	function selectStream(v: StreamView) {
		streamView = v; // the reactive below loads the rows (guarded against double-fetch)
	}

	async function fix() {
		if (filling) return;
		filling = true;
		error = null;
		try {
			await backfillGaps(symbol, timeframe);
			await load();
			dispatch('changed');
		} catch (e) {
			error = e instanceof Error ? e.message : 'Backfill failed';
		} finally {
			filling = false;
		}
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') dispatch('close');
	}

	function formatCell(v: unknown): string {
		if (v === null || v === undefined) return '—';
		if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : String(v);
		return String(v);
	}

	// OHLCV series + quality depend only on symbol/timeframe — NOT on the active tab,
	// so toggling tabs doesn't trigger a redundant series refetch.
	$: if (symbol && timeframe) load();

	// (Re)load enrichment-stream rows when symbol/timeframe/tab changes (key-guarded
	// so a tab click doesn't double-fetch and unrelated updates don't re-fetch).
	let streamReloadKey = '';
	$: {
		const k = `${symbol}|${timeframe}|${streamView}`;
		if ((streamView === 'funding' || streamView === 'oi') && k !== streamReloadKey) {
			streamReloadKey = k;
			loadStreamRows(streamView);
		}
	}

	// Header suffix reflects the active stream (funding is tf-agnostic; OI may resolve
	// to a different tf than the OHLCV one in the title).
	$: headerSuffix =
		streamView === 'ohlcv'
			? timeframe
			: streamView === 'funding'
				? 'funding'
				: `oi${streamRows?.timeframe ? ' · ' + streamRows.timeframe : ''}`;

	$: metrics = quality
		? [
				{ label: 'Rows', value: quality.row_count.toLocaleString(), bad: false },
				{ label: 'Gaps', value: String(quality.gaps), bad: quality.gaps > 0 },
				{ label: 'Null values', value: String(quality.null_values), bad: quality.null_values > 0 },
				{
					label: 'Invalid OHLC',
					value: String(quality.integrity.invalid_high_low + quality.integrity.invalid_close_range),
					bad: quality.integrity.invalid_high_low + quality.integrity.invalid_close_range > 0
				},
				{
					label: 'Outliers',
					value: String(quality.outliers.close + quality.outliers.volume),
					bad: quality.outliers.close + quality.outliers.volume > 0
				},
				{
					label: 'Freshness',
					value: `${quality.freshness.hours_ago.toFixed(0)}h ago`,
					bad: quality.freshness.is_stale
				}
			]
		: [];
</script>

<svelte:window on:keydown={onKey} />

<!-- backdrop -->
<div
	class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/80 p-4 sm:p-8"
	on:click={() => dispatch('close')}
	on:keydown={() => {}}
	role="presentation"
>
	<!-- panel -->
	<div
		class="mt-4 w-full max-w-4xl border border-[#222] bg-[#050505]"
		on:click|stopPropagation
		on:keydown|stopPropagation
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="{symbol} {timeframe} detail"
	>
		<div class="flex items-center justify-between border-b border-[#222] p-4">
			<div class="flex items-baseline gap-3">
				<h2 class="font-mono text-lg font-semibold text-white">{symbol}<span class="text-[#666]">/{headerSuffix}</span></h2>
				{#if series}
					<span class="border border-[#333] px-2 py-0.5 text-xs text-[#888]">
						source: <span class="font-mono text-white">{series.source || 'local'}</span>
					</span>
					{#if series.is_fallback}
						<span class="border border-yellow-900 px-2 py-0.5 text-xs text-yellow-400">fallback</span>
					{/if}
					{#if quality}
						{@const sc = scoreFrom(quality)}
						<span class="text-xs text-[#666]">quality</span>
						<span class="font-mono text-sm font-bold {scoreClass(sc)}">{sc}</span>
					{/if}
				{/if}
			</div>
			<button class="p-1 text-[#888] hover:bg-[#111] hover:text-white" on:click={() => dispatch('close')} aria-label="Close">
				✕
			</button>
		</div>

		<div class="p-4">
			{#if error}
				<div class="mb-3 border border-red-900 bg-red-500/5 p-2 text-xs text-red-400">{error}</div>
			{/if}

			<!-- Stream selector + (OHLCV) chart/rows toggle -->
			<div class="mb-3 flex flex-wrap items-center gap-2">
				<div class="flex border border-[#333] p-0.5 text-xs">
					{#each STREAM_TABS as t}
						<button
							class="px-2 py-0.5 {streamView === t.id ? 'bg-[#222] text-white' : 'text-[#888] hover:text-white'}"
							on:click={() => selectStream(t.id)}
						>{t.label}</button>
					{/each}
				</div>
				{#if streamView === 'ohlcv' && series && series.data.length > 0}
					<div class="flex border border-[#333] p-0.5 text-xs">
						<button class="px-2 py-0.5 {ohlcvView === 'chart' ? 'bg-[#222] text-white' : 'text-[#888] hover:text-white'}" on:click={() => (ohlcvView = 'chart')}>Chart</button>
						<button class="px-2 py-0.5 {ohlcvView === 'table' ? 'bg-[#222] text-white' : 'text-[#888] hover:text-white'}" on:click={() => (ohlcvView = 'table')}>Rows</button>
					</div>
				{/if}
			</div>

			{#if streamView === 'ohlcv'}
				{#if loading && !series}
					<div class="py-12 text-center text-sm text-[#666]">Loading series…</div>
				{:else if series && series.data.length > 0}
					{#if ohlcvView === 'chart'}
						<CandlestickChart data={series.data} height={320} showVolume={true} />
					{:else}
						<div class="h-96 overflow-hidden border border-[#222]">
							<DataTableView data={series.data} />
						</div>
					{/if}

					<div class="mt-4 grid grid-cols-3 gap-x-4 gap-y-3 sm:grid-cols-6">
						{#each metrics as m}
							<div>
								<div class="text-[11px] uppercase tracking-wide text-[#666]">{m.label}</div>
								<div class="font-mono text-sm {m.bad ? 'text-red-300' : 'text-white'}">{m.value}</div>
							</div>
						{/each}
					</div>

					{#if quality && quality.gap_details.length > 0}
						<div class="mt-4 border-t border-[#1a1a1a] pt-3">
							<div class="mb-2 flex items-center justify-between">
								<span class="text-xs font-medium text-[#888]">
									Gap detail <span class="text-[#555]">({quality.gap_details.length})</span>
								</span>
								<button class="border border-[#333] px-2 py-0.5 text-[11px] text-[#888] hover:bg-[#111] hover:text-white transition-colors disabled:opacity-50" on:click={fix} disabled={filling}>
									{filling ? 'filling…' : 'Backfill gaps'}
								</button>
							</div>
							<div class="max-h-32 space-y-0.5 overflow-y-auto text-xs">
								{#each quality.gap_details.slice(0, 50) as g}
									<div class="flex justify-between text-[#888]">
										<span class="font-mono">{g.timestamp}</span>
										<span class="text-yellow-400/80">{g.gap_size}</span>
									</div>
								{/each}
							</div>
						</div>
					{/if}
				{:else}
					<div class="py-12 text-center text-sm text-[#666]">No bars stored for this series.</div>
				{/if}
			{:else}
				<!-- Funding / OI: raw enrichment-stream rows -->
				{#if streamRowsLoading}
					<div class="py-12 text-center text-sm text-[#666]">Loading {streamView}…</div>
				{:else if streamRowsError}
					<div class="py-12 text-center text-sm text-red-400">Couldn't load {streamView} rows: {streamRowsError}</div>
				{:else if streamRows && streamRows.rows.length > 0}
					<div class="max-h-96 overflow-auto border border-[#222]">
						<table class="w-full text-left text-xs font-mono">
							<thead class="sticky top-0 bg-[#111]">
								<tr class="border-b border-[#333] text-[#666]">
									{#each streamRows.columns as c}<th class="p-2 uppercase">{c}</th>{/each}
								</tr>
							</thead>
							<tbody>
								{#each [...streamRows.rows].reverse() as r}
									<tr class="border-b border-[#1a1a1a] text-[#888]">
										{#each streamRows.columns as c}<td class="p-2">{formatCell(r[c])}</td>{/each}
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
					<div class="mt-1 text-[11px] text-[#666]">
						{streamRows.rows.length.toLocaleString()} rows{#if streamRows.timeframe} · {streamRows.timeframe}{/if} · newest first
					</div>
				{:else}
					<div class="py-12 text-center text-sm text-[#666]">No {streamView} data stored for this series.</div>
				{/if}
			{/if}
		</div>
	</div>
</div>
