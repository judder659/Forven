<script lang="ts">
	/** Realized net P&L per UTC day over the last 30 days (teal gains, orange losses). */
	import type { DeskFill } from '$lib/api/desk';
	import { fmtDay, fmtUsd, num, parseTs } from '$lib/utils/tradingDesk/format';

	export let fills: DeskFill[] = [];
	export let now = Date.now();
	export let days = 30;

	const W = 600;
	const H = 150;
	const TOP = 12;
	const BOTTOM = 22;
	const LEFT = 44;
	const DAY = 86_400_000;

	$: end = Math.floor(now / DAY) * DAY;
	$: buckets = (() => {
		const out = Array.from({ length: days }, (_, i) => ({ t: end - (days - 1 - i) * DAY, value: 0, n: 0 }));
		for (const fill of fills) {
			if (String(fill.status).toUpperCase() !== 'CLOSED') continue;
			const closed = parseTs(fill.closed_at);
			const net = num(fill.net_pnl_usd);
			if (closed === null || net === null) continue;
			const index = Math.floor((Math.floor(closed / DAY) * DAY - out[0].t) / DAY);
			if (index >= 0 && index < days) {
				out[index].value += net;
				out[index].n += 1;
			}
		}
		return out;
	})();
	$: maxAbs = Math.max(1, ...buckets.map((bucket) => Math.abs(bucket.value)));
	$: mid = TOP + (H - TOP - BOTTOM) / 2;
	$: half = (H - TOP - BOTTOM) / 2;
	$: barWidth = (W - LEFT) / days;
	$: y = (value: number) => mid - (value / maxAbs) * half;
	$: total = buckets.reduce((sum, bucket) => sum + bucket.value, 0);
</script>

<svg class="block h-auto w-full overflow-visible" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Realized P&L per day, last ${days} days, total ${fmtUsd(total, { signed: true })}`}>
	<line x1={LEFT} x2={W} y1={TOP} y2={TOP} stroke="#1c2026" stroke-width="1" shape-rendering="crispEdges" />
	<line x1={LEFT} x2={W} y1={H - BOTTOM} y2={H - BOTTOM} stroke="#1c2026" stroke-width="1" shape-rendering="crispEdges" />
	<line x1={LEFT} x2={W} y1={mid} y2={mid} stroke="#2a2f38" stroke-width="1" shape-rendering="crispEdges" />
	{#each [maxAbs, 0, -maxAbs] as tick}
		<text x={LEFT - 6} y={y(tick) + 3.5} text-anchor="end" class="fill-sc-ink3 font-plex-mono text-[10.5px]">{fmtUsd(tick, { digits: 0 })}</text>
	{/each}
	{#each buckets as bucket, index}
		{#if bucket.n}
			{@const height = Math.max(1.5, Math.abs(y(bucket.value) - mid))}
			<rect x={LEFT + index * barWidth + 1} y={bucket.value >= 0 ? mid - height : mid} width={Math.max(1, barWidth - 2)} {height} rx="1.5" fill={bucket.value >= 0 ? '#139a9f' : '#e0663f'}>
				<title>{fmtDay(bucket.t)}: {fmtUsd(bucket.value, { signed: true })} over {bucket.n} trade{bucket.n === 1 ? '' : 's'}</title>
			</rect>
		{/if}
	{/each}
	{#each [0, Math.floor(days / 3), Math.floor((2 * days) / 3), days - 1] as index}
		<text x={LEFT + index * barWidth + barWidth / 2} y={H - 6} text-anchor="middle" class="fill-sc-ink3 font-plex-mono text-[10.5px]">{fmtDay(buckets[index]?.t)}</text>
	{/each}
</svg>
