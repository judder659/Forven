<script lang="ts">
	import { CHART, linearScale, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtFraction, fmtNum, fmtPct, isNum, toNumber } from '$lib/utils/strategyContainer/format';

	/** Baseline against stressed costs for Sharpe, return and profit factor. */
	export let payload: Record<string, unknown> | null = null;
	/** The strict paper → live gate's cap on the share of Sharpe lost (percent points). */
	export let maxDegradationPct: number | null = null;
	export let height = 150;

	let width = 420;
	const M = { t: 18, r: 72, b: 8, l: 96 };
	type Bag = Record<string, unknown>;
	const bag = (value: unknown): Bag => (value && typeof value === 'object' && !Array.isArray(value) ? (value as Bag) : {});

	$: original = bag(payload?.original);
	$: stressed = bag(payload?.stressed);
	$: multiplier = toNumber(payload?.fee_multiplier);
	$: rows = [
		{ name: 'Sharpe', a: toNumber(original.sharpe), b: toNumber(stressed.sharpe), fmt: (v: number) => fmtNum(v) },
		{ name: 'Return', a: toNumber(original.total_return), b: toNumber(stressed.total_return), fmt: (v: number) => fmtFraction(v) },
		{ name: 'Profit factor', a: toNumber(original.profit_factor), b: toNumber(stressed.profit_factor), fmt: (v: number) => fmtNum(v) },
	].filter((row): row is { name: string; a: number; b: number; fmt: (v: number) => string } => isNum(row.a) && isNum(row.b));
	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: band = rows.length ? h / rows.length : h;
	$: degradation = toNumber(payload?.degradation_pct);
	/** The runner's floor on the stressed Sharpe. */
	$: minSharpe = toNumber(payload?.verdict_threshold);
	$: stressedSharpe = toNumber(stressed.sharpe);
	$: tradesA = toNumber(original.total_trades);
	$: tradesB = toNumber(stressed.total_trades);
</script>

{#if rows.length}
	<div class="grid gap-2" data-testid="cost-stress-dumbbell">
		<div class="flex gap-3.5 text-[11px] text-[#aab1bc]">
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5 rounded-full" style={`background:${CHART.ink}`}></i>Baseline costs</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5 rounded-full" style={`background:${CHART.loss}`}></i>{isNum(multiplier) ? `${multiplier}×` : 'Higher'} costs</span>
		</div>
		<div use:trackWidth={(value) => (width = value || 420)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible text-[10.5px]" role="img" aria-label="Metrics at baseline and stressed costs">
				{#each rows as row, index (row.name)}
					{@const cy = M.t + band * (index + 0.5)}
					{@const x = linearScale(Math.min(0, row.a, row.b), Math.max(row.a, row.b) * 1.15 || 1, M.l, M.l + w)}
					<text x={M.l - 10} y={cy + 4} text-anchor="end" fill={CHART.ink2} class="text-[12px]">{row.name}</text>
					<line x1={M.l} x2={M.l + w} y1={cy} y2={cy} stroke={CHART.grid} />
					<line x1={x(row.b)} x2={x(row.a)} y1={cy} y2={cy} stroke={CHART.ink3} stroke-width="2" />
					<circle cx={x(row.a)} {cy} r="5" fill={CHART.ink} stroke={CHART.panel} stroke-width="2" />
					<circle cx={x(row.b)} {cy} r="5" fill={CHART.loss} stroke={CHART.panel} stroke-width="2" />
					<text x={x(row.a) + 9} y={cy - 7} fill={CHART.ink2}>{row.fmt(row.a)}</text>
					<text x={M.l + w + 8} y={cy + 4} fill={CHART.ink}>{row.fmt(row.b)}</text>
				{/each}
			</svg>
		</div>
		<p class="m-0 text-[11px] leading-relaxed text-[#777]">
			{#if isNum(degradation)}Sharpe falls {fmtPct(degradation, 1, false)}{isNum(stressedSharpe) ? ` to ${fmtNum(stressedSharpe)}` : ''} under {isNum(multiplier) ? `${multiplier}×` : 'higher'} fees and slippage.{/if}
			{#if isNum(minSharpe)} The test needs a stressed Sharpe of at least {fmtNum(minSharpe)}{isNum(maxDegradationPct) ? `; the paper → live gate also caps the loss at ${fmtPct(maxDegradationPct, 0, false)}${isNum(degradation) && degradation > maxDegradationPct ? ', which this exceeds' : ''}` : ''}.{/if}
			{#if isNum(tradesA) && isNum(tradesB)}{tradesA === tradesB ? ` Trade count is unchanged (${tradesB}), so costs trim each trade rather than removing marginal ones.` : ` Trades fall from ${tradesA} to ${tradesB}.`}{/if}
		</p>
	</div>
{:else}
	<div class="text-[12px] text-[#666]">Cost stress has not run.</div>
{/if}
