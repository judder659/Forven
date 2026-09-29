<script lang="ts">
	import { getRegimeSeries, type RegimeSeriesSegment } from '$lib/api/forven';
	import { formatRegimeLabel, regimeSwatchClass } from '$lib/utils/labRegime';

	/** Asset or pair symbol (BTC, BTC/USDT, BTC-USDT all resolve server-side). */
	export let symbol: string;
	export let timeframe = '1h';
	export let bars = 500;

	let segments: RegimeSeriesSegment[] = [];
	let loading = false;
	let error = '';
	let loadedKey = '';

	$: requestKey = `${symbol}|${timeframe}|${bars}`;
	$: if (symbol && requestKey !== loadedKey) {
		void load(requestKey);
	}

	async function load(key: string) {
		loading = true;
		error = '';
		try {
			const response = await getRegimeSeries(symbol, timeframe, bars);
			if (key !== requestKey) return; // stale response
			segments = response.segments ?? [];
			loadedKey = key;
		} catch {
			error = 'regime series unavailable';
			segments = [];
			loadedKey = key;
		} finally {
			loading = false;
		}
	}

	function widthPct(segment: RegimeSeriesSegment, all: RegimeSeriesSegment[]): number {
		const t0 = Date.parse(all[0]?.start ?? '');
		const t1 = Date.parse(all[all.length - 1]?.end ?? '');
		const span = t1 - t0;
		if (!Number.isFinite(span) || span <= 0) return 100 / Math.max(1, all.length);
		const s = Date.parse(segment.start);
		const e = Date.parse(segment.end);
		return Math.max(0.2, ((e - s) / span) * 100);
	}
</script>

<div class="flex w-full items-center gap-2 bg-sc-bg px-2 py-1">
	{#if loading && segments.length === 0}
		<div class="h-2 flex-1 animate-pulse bg-sc-panel2"></div>
	{:else if error}
		<span class="text-[10px] text-sc-ink3">{error}</span>
	{:else if segments.length > 0}
		<div class="flex h-2 flex-1 overflow-hidden" role="img" aria-label="regime timeline">
			{#each segments as segment, i (i)}
				<div
					class={`h-full ${regimeSwatchClass(segment.regime)} opacity-70`}
					style={`width:${widthPct(segment, segments)}%`}
					title={`${formatRegimeLabel(segment.regime)} · ${segment.start.slice(0, 16)} → ${segment.end.slice(0, 16)}`}
				></div>
			{/each}
		</div>
		<span class="text-[10px] text-sc-ink3 whitespace-nowrap">
			now: {formatRegimeLabel(segments[segments.length - 1]?.regime)}
		</span>
	{:else}
		<span class="text-[10px] text-sc-ink3">no data</span>
	{/if}
</div>
