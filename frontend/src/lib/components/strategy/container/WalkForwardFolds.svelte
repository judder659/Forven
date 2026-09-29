<script lang="ts">
	import type { WalkForwardEvidence } from '$lib/utils/strategyContainer/evidence';
	import { THIN_FOLD_TRADES } from '$lib/utils/strategyContainer/evidence';
	import { CHART, linearScale, niceTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtDateUtc, fmtNum, isNum } from '$lib/utils/strategyContainer/format';

	/** In-sample against out-of-sample Sharpe per walk-forward fold. */
	export let wf: WalkForwardEvidence | null = null;
	export let height = 230;

	let width = 420;
	let hover: { fold: number; oos: boolean } | null = null;
	const M = { t: 16, r: 10, b: 40, l: 34 };

	$: folds = (wf?.folds ?? []).filter((fold) => isNum(fold.isSharpe) || isNum(fold.oosSharpe));
	$: values = folds.flatMap((fold) => [fold.isSharpe, fold.oosSharpe]).filter(isNum);
	$: lo = Math.min(0, ...values);
	$: hi = Math.max(0.5, ...values);
	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: y = linearScale(lo * 1.15, hi * 1.12, M.t + h, M.t);
	$: ticks = niceTicks(lo, hi, 5);
	$: band = folds.length ? w / folds.length : w;
	$: bw = Math.min(22, band / 3.2);
	$: trades = folds.map((fold) => fold.oosTrades).filter(isNum);
	$: oosValues = folds.map((fold) => fold.oosSharpe).filter(isNum);
	$: thin = trades.length > 0 && Math.max(...trades) < THIN_FOLD_TRADES;
	$: hovered = hover ? folds[hover.fold] ?? null : null;
	const bar = (value: number | null, yScale: (v: number) => number) => {
		const v = isNum(value) ? value : 0;
		return { top: Math.min(yScale(0), yScale(v)), height: Math.max(1, Math.abs(yScale(v) - yScale(0))) };
	};
</script>

{#if folds.length}
	<div class="grid gap-2" data-testid="walk-forward-folds">
		<div class="flex gap-3.5 text-[11px] text-sc-ink2">
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5" style={`background:${CHART.context}`}></i>In-sample</span>
			<span class="inline-flex items-center gap-1.5"><i class="inline-block h-2.5 w-2.5" style={`background:${CHART.ink}`}></i>Out-of-sample</span>
		</div>
		<div class="relative" use:trackWidth={(value) => (width = value || 420)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible font-plex-mono text-[10.5px]" role="img" aria-label={`Sharpe per walk-forward fold, out-of-sample ${oosValues.map((v) => fmtNum(v)).join(', ')}`}>
				{#each ticks as tick (tick)}
					<line x1={M.l} x2={M.l + w} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
					<text x={M.l - 6} y={y(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{fmtNum(tick, Math.abs(hi - lo) < 3 ? 1 : 0)}</text>
				{/each}
				{#each folds as fold, index (fold.fold)}
					{@const cx = M.l + band * (index + 0.5)}
					{@const isBar = bar(fold.isSharpe, y)}
					{@const oosBar = bar(fold.oosSharpe, y)}
					<rect role="presentation" x={cx - bw - 1} y={isBar.top} width={bw} height={isBar.height} rx="2" fill={CHART.context} on:pointerenter={() => (hover = { fold: index, oos: false })} on:pointerleave={() => (hover = null)} />
					<rect role="presentation" x={cx + 1} y={oosBar.top} width={bw} height={oosBar.height} rx="2" fill={CHART.ink} on:pointerenter={() => (hover = { fold: index, oos: true })} on:pointerleave={() => (hover = null)} />
					{#if isNum(fold.oosSharpe)}
						<text x={cx + 1 + bw / 2} y={fold.oosSharpe >= 0 ? y(fold.oosSharpe) - 5 : y(fold.oosSharpe) + 12} text-anchor="middle" fill={CHART.ink2}>{fmtNum(fold.oosSharpe, 1)}</text>
					{/if}
					<text x={cx} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink2}>Fold {fold.fold}</text>
					<text x={cx} y={M.t + h + 30} text-anchor="middle" fill={isNum(fold.oosTrades) && fold.oosTrades < THIN_FOLD_TRADES ? CHART.caution : CHART.ink3}>{fold.oosTrades ?? '—'} trades</text>
				{/each}
			</svg>
			{#if hovered && hover}
				<div class="pointer-events-none absolute top-0 z-10 min-w-[170px] rounded-md border border-sc-line2 bg-[#0b0d11] px-2.5 py-1.5 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(M.l + band * (hover.fold + 0.5) + 16, Math.max(0, width - 190))}px`}>
					<div class="text-sc-ink3">Fold {hovered.fold} · {hover.oos ? 'out-of-sample' : 'in-sample'}</div>
					<div class="flex justify-between gap-3"><span class="text-sc-ink3">Sharpe</span><span class="text-sc-ink">{fmtNum(hover.oos ? hovered.oosSharpe : hovered.isSharpe)}</span></div>
					<div class="flex justify-between gap-3"><span class="text-sc-ink3">Trades</span><span class="text-sc-ink">{(hover.oos ? hovered.oosTrades : hovered.isTrades) ?? '—'}</span></div>
					<div class="flex justify-between gap-3"><span class="text-sc-ink3">Test window</span><span class="text-sc-ink">{fmtDateUtc(hovered.testStart)} – {fmtDateUtc(hovered.testEnd)}</span></div>
				</div>
			{/if}
		</div>
		<p class="m-0 text-[12px] leading-relaxed text-sc-ink3">
			Average out-of-sample Sharpe {fmtNum(wf?.avgOosSharpe)} against {fmtNum(wf?.avgIsSharpe)} in-sample.
			{#if oosValues.length > 1}Folds range from {fmtNum(Math.min(...oosValues))} to {fmtNum(Math.max(...oosValues))}{trades.length ? ` on ${Math.min(...trades)}–${Math.max(...trades)} trades each` : ''}.{/if}
			{#if thin}<span class="text-[#e7b24a]"> Every fold has fewer than {THIN_FOLD_TRADES} trades, so one or two trades move a fold's Sharpe a lot.</span>{/if}
		</p>
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">Walk-forward has not run.</div>
{/if}
