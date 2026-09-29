<script lang="ts">
	/**
	 * Funding and open interest for the selected market, from the collector's
	 * 15-minute Hyperliquid snapshots. Hyperliquid funds every hour; a positive
	 * rate means longs pay shorts.
	 */
	import type { AssetMarketContext } from '$lib/api/desk';
	import { ago, dur, fmtCompactUsd, fmtNum, fmtPct, fmtRateHourly, parseTs, toneClass } from '$lib/utils/tradingDesk/format';
	import { fundingDirection } from '$lib/utils/tradingDesk/market';

	export let asset: string;
	export let context: AssetMarketContext | null = null;
	export let now = Date.now();

	const label = 'font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3';

	$: funding = context?.funding ?? null;
	$: oi = context?.open_interest ?? null;
	$: nextFunding = parseTs(funding?.next_funding_at);
	$: fundingSeries = (funding?.series ?? []).map((point) => point[1]);
	$: oiSeries = (oi?.series ?? []).map((point) => point[2] ?? point[1]);

	function path(values: number[], width: number, height: number, zero = false): { d: string; zeroY: number | null } {
		if (values.length < 2) return { d: '', zeroY: null };
		const lo = Math.min(...values, ...(zero ? [0] : []));
		const hi = Math.max(...values, ...(zero ? [0] : []));
		const span = hi - lo || 1;
		const x = (i: number) => (i / (values.length - 1)) * (width - 2) + 1;
		const y = (v: number) => height - 2 - ((v - lo) / span) * (height - 4);
		return {
			d: values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' '),
			zeroY: zero ? y(0) : null,
		};
	}

	$: fundingPath = path(fundingSeries, 96, 24, true);
	$: oiPath = path(oiSeries, 96, 24);
</script>

<div class="grid gap-x-6 gap-y-2 border-b border-sc-line px-3 py-2 sm:grid-cols-2 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto]" aria-label={`${asset} funding and open interest`} data-testid="desk-market">
	<div class="grid min-w-0 grid-cols-[minmax(0,1fr)_96px] items-center gap-3">
		<div class="grid min-w-0 gap-0.5">
			<span class={label}>Funding · hourly</span>
			{#if funding}
				<span class="flex flex-wrap items-baseline gap-x-2">
					<b class="font-plex-mono text-[14px] font-medium text-sc-ink">{fmtRateHourly(funding.rate_hourly)}</b>
					<span class="text-[12px] text-sc-ink3">{fmtPct(funding.annualized_pct, 1)} a year</span>
				</span>
				<span class="text-[12px] text-sc-ink2">{fundingDirection(funding.rate_hourly)}{#if nextFunding !== null} · next print in <b class="font-plex-mono font-medium text-sc-ink">{dur(nextFunding - now)}</b>{/if}</span>
				<span class="text-[11.5px] text-sc-ink3">
					24h avg {funding.avg_24h_hourly !== null ? fmtRateHourly(funding.avg_24h_hourly) : '—'} · 7d avg {funding.avg_7d_hourly !== null ? fmtRateHourly(funding.avg_7d_hourly) : '—'}
					{#if funding.stale}<span class="text-[#e7b24a]"> · last update {ago(funding.as_of, now)}</span>{/if}
				</span>
			{:else}
				<span class="text-[12px] text-sc-ink3">No funding data collected for {asset}.</span>
			{/if}
		</div>
		{#if fundingPath.d}
			<svg width="96" height="24" viewBox="0 0 96 24" role="img" aria-label={`${asset} hourly funding, last ${funding?.series.length ?? 0} hours`}>
				{#if fundingPath.zeroY !== null}<line x1="1" x2="95" y1={fundingPath.zeroY} y2={fundingPath.zeroY} stroke="#2a2f38" stroke-width="1" />{/if}
				<path d={fundingPath.d} fill="none" stroke="#aab1bc" stroke-width="1.4" stroke-linejoin="round" />
			</svg>
		{/if}
	</div>
	<div class="grid min-w-0 grid-cols-[minmax(0,1fr)_96px] items-center gap-3">
		<div class="grid min-w-0 gap-0.5">
			<span class={label}>Open interest</span>
			{#if oi}
				<span class="flex flex-wrap items-baseline gap-x-2">
					<b class="font-plex-mono text-[14px] font-medium text-sc-ink">{fmtCompactUsd(oi.usd)}</b>
					<span class="text-[12px] text-sc-ink3">{fmtNum(oi.coins, 0)} {asset}</span>
				</span>
				<span class="text-[12px] text-sc-ink2">24h <b class={`font-plex-mono font-medium ${toneClass(oi.change_24h_pct)}`}>{fmtPct(oi.change_24h_pct, 1)}</b>
					{#if oi.stale}<span class="text-[#e7b24a]"> · last update {ago(oi.as_of, now)}</span>{:else}<span class="text-sc-ink3"> · as of {ago(oi.as_of, now)}</span>{/if}
				</span>
			{:else}
				<span class="text-[12px] text-sc-ink3">Open interest is collected for BTC, ETH and SOL only.</span>
			{/if}
		</div>
		{#if oiPath.d}
			<svg width="96" height="24" viewBox="0 0 96 24" role="img" aria-label={`${asset} open interest, last ${oi?.series.length ?? 0} hours`}>
				<path d={oiPath.d} fill="none" stroke="#aab1bc" stroke-width="1.4" stroke-linejoin="round" />
			</svg>
		{/if}
	</div>
	<div class="grid content-start gap-0.5">
		<span class={label}>Premium</span>
		<b class={`font-plex-mono text-[13px] font-medium ${toneClass(context?.premium)}`}>{context?.premium !== null && context?.premium !== undefined ? fmtPct(context.premium * 100, 3) : '—'}</b>
		<span class="text-[11.5px] text-sc-ink3">mark vs oracle</span>
	</div>
</div>
