<script lang="ts">
	import { CHART, linearScale, niceTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtPct, isNum, toNumber } from '$lib/utils/strategyContainer/format';
	import StatGrid from './StatGrid.svelte';

	/** The Monte Carlo payload: return histogram (percent points) with its percentiles. */
	export let payload: Record<string, unknown> | null = null;
	export let height = 200;

	let width = 420;
	let hover: number | null = null;
	const M = { t: 24, r: 12, b: 26, l: 34 };
	type Bag = Record<string, unknown>;
	const bag = (value: unknown): Bag => (value && typeof value === 'object' && !Array.isArray(value) ? (value as Bag) : {});

	$: hist = bag(payload?.return_histogram);
	$: bins = (Array.isArray(hist.bins) ? hist.bins : []).map(toNumber).filter(isNum);
	$: counts = (Array.isArray(hist.counts) ? hist.counts : []).map((value) => toNumber(value) ?? 0);
	$: step = bins.length > 1 ? bins[1] - bins[0] : 1;
	$: q = bag(payload?.return_distribution);
	$: dd = bag(payload?.drawdown_distribution);
	$: sims = toNumber(payload?.n_simulations);
	$: tradesN = toNumber(payload?.n_trades);
	$: lo = bins[0] ?? 0;
	$: hi = (bins[bins.length - 1] ?? 0) + step;
	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: x = linearScale(lo, hi, M.l, M.l + w);
	$: maxN = Math.max(1, ...counts);
	$: y = linearScale(0, maxN, M.t + h, M.t);
	$: marks = [
		{ name: 'p5', value: toNumber(q.p5), anchor: 'start' },
		{ name: 'median', value: toNumber(q.p50), anchor: 'middle' },
		{ name: 'p95', value: toNumber(q.p95), anchor: 'end' },
	].filter((mark): mark is { name: string; value: number; anchor: string } => isNum(mark.value));
	$: stats = [
		{ label: 'P(profitable)', value: fmtPct(toNumber(payload?.prob_profitable), 1, false) },
		{ label: 'P(loss > 10%)', value: fmtPct(toNumber(payload?.prob_loss_gt_10), 1, false) },
		{ label: 'Median return', value: fmtPct(toNumber(q.p50)) },
		{ label: '95th pct drawdown', value: fmtPct(toNumber(dd.p95), 1, false) },
	];
</script>

{#if bins.length > 1 && counts.length === bins.length}
	<div class="grid gap-3" data-testid="monte-carlo-outcome">
		<div class="relative" use:trackWidth={(value) => (width = value || 420)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible font-plex-mono text-[10.5px]" role="img" aria-label={`Final return of ${sims ?? ''} resampled trade sequences`}>
				{#each niceTicks(0, maxN, 4).filter((tick) => Number.isInteger(tick)) as tick (tick)}
					<line x1={M.l} x2={M.l + w} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
					<text x={M.l - 6} y={y(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{tick}</text>
				{/each}
				{#each niceTicks(lo, hi, 6) as tick (tick)}
					<text x={x(tick)} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink3}>{fmtPct(tick, 0)}</text>
				{/each}
				{#each counts as count, index (index)}
					{#if count > 0}
						{@const bw = Math.max(1, x(bins[index] + step) - x(bins[index]) - 2)}
						<rect
							role="presentation"
							x={x(bins[index]) + 1}
							y={y(count)}
							width={bw}
							height={Math.max(0.5, y(0) - y(count))}
							rx={Math.min(2, bw / 2)}
							fill={bins[index] + step / 2 >= 0 ? CHART.gain : CHART.loss}
							opacity={hover === null || hover === index ? 0.9 : 0.55}
							on:pointerenter={() => (hover = index)}
							on:pointerleave={() => (hover = null)}
						/>
					{/if}
				{/each}
				{#each marks as mark (mark.name)}
					<line x1={x(mark.value)} x2={x(mark.value)} y1={M.t - 4} y2={y(0)} stroke={CHART.ink} />
					<text x={x(mark.value)} y={M.t - 8} text-anchor={mark.anchor} fill={CHART.ink2}>{mark.name} {fmtPct(mark.value, 0)}</text>
				{/each}
			</svg>
			{#if hover !== null}
				<div class="pointer-events-none absolute top-0 z-10 rounded-md border border-sc-line2 bg-[#0b0d11] px-2.5 py-1.5 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(x(bins[hover]) + 8, Math.max(0, width - 160))}px`}>
					<div class="text-sc-ink3">{fmtPct(bins[hover], 1)} to {fmtPct(bins[hover] + step, 1)}</div>
					<div class="flex justify-between gap-3"><span class="text-sc-ink3">Resamples</span><span class="text-sc-ink">{counts[hover]}</span></div>
				</div>
			{/if}
		</div>
		<StatGrid items={stats} testid="monte-carlo-stats" />
		<p class="m-0 text-[12px] leading-relaxed text-sc-ink3">
			{sims?.toLocaleString('en-US') ?? '—'} resamples of the {tradesN ?? '—'} walk-forward trades, drawn with replacement. This bounds luck in the order and mix of trades; it does not test whether the edge itself is real — walk-forward, the held-back test and the regime split do that.
		</p>
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">Monte Carlo has not run.</div>
{/if}
