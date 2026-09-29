<script lang="ts">
	import type { CurvePoint } from '$lib/utils/strategyContainer/metrics';
	import { underwater } from '$lib/utils/strategyContainer/metrics';
	import { CHART, linePath, linearScale, logGrowthTicks, logScale, niceTicks, stepIndex, timeTickLabel, timeTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { fmtDateUtc, fmtPct, fmtUsd } from '$lib/utils/strategyContainer/format';

	/** Strategy equity (closed-trade or mark-to-market) and a buy & hold benchmark, in dollars. */
	export let strategy: CurvePoint[] = [];
	export let benchmark: CurvePoint[] = [];
	/** Start of the out-of-sample slice; the in-sample span before it is shaded. */
	export let oosStart: number | null = null;
	export let height = 360;

	let width = 640;
	let log = true;
	let showBench = true;
	let showTable = false;
	let cursor: number | null = null;

	const M = { t: 22, r: 70, b: 24, l: 52 };
	const DD_GAP = 22;

	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: topH = Math.round(h * 0.7);
	$: ddTop = M.t + topH + DD_GAP;
	$: ddH = h - topH - DD_GAP;
	$: base = strategy[0]?.v ?? 1;
	$: benchBase = benchmark[0]?.v ?? 1;
	$: strat = strategy.map((p) => ({ t: p.t, m: p.v / base }));
	$: bench = benchmark.map((p) => ({ t: p.t, m: p.v / benchBase }));
	$: dd = underwater(strategy);
	$: times = strat.map((p) => p.t);
	$: t0 = strat[0]?.t ?? 0;
	$: t1 = strat[strat.length - 1]?.t ?? 1;
	$: x = linearScale(t0, t1, M.l, M.l + w);
	$: visible = showBench ? [...strat, ...bench.filter((p) => p.t >= t0 && p.t <= t1)] : strat;
	$: lo = Math.min(...visible.map((p) => p.m)) * 0.97;
	$: hi = Math.max(...visible.map((p) => p.m)) * 1.03;
	$: y = log ? logScale(lo, hi, M.t + topH, M.t) : linearScale(lo, hi, M.t + topH, M.t);
	$: yTicks = log ? logGrowthTicks(lo, hi) : niceTicks(lo, hi, 5);
	$: xt = timeTicks(t0, t1);
	$: minDd = Math.min(-1, ...dd.map((p) => p.v));
	$: yd = linearScale(minDd * 1.1, 0, ddTop + ddH, ddTop);
	$: ddTicks = [0, Math.round(minDd / 2), Math.round(minDd)].filter((v, i, all) => all.indexOf(v) === i);
	$: stratPath = linePath(strat.map((p) => [x(p.t), y(p.m)]));
	$: benchPath = showBench ? linePath(bench.filter((p) => p.t >= t0 && p.t <= t1).map((p) => [x(p.t), y(p.m)])) : '';
	$: ddPath = linePath(dd.map((p) => [x(p.t), yd(p.v)]));
	$: last = strat[strat.length - 1];
	$: lastBench = (() => {
		const inWindow = bench.filter((p) => p.t <= t1);
		return inWindow[inWindow.length - 1];
	})();
	$: oosX = oosStart !== null && oosStart > t0 && oosStart < t1 ? x(oosStart) : null;
	$: readout = cursor === null || strat.length === 0 ? null : (() => {
		const i = stepIndex(times, cursor);
		const s = strat[i];
		const b = bench.length ? bench[stepIndex(bench.map((p) => p.t), cursor)] : null;
		return { t: cursor, x: x(cursor), y: y(s.m), strat: s.m, bench: b?.m ?? null, dd: dd[i]?.v ?? 0, inSample: oosStart !== null && cursor < oosStart };
	})();
	$: tableRows = (() => {
		const out: Array<{ t: number; m: number }> = [];
		let year: number | null = null;
		for (const p of strat) {
			const y2 = new Date(p.t).getUTCFullYear();
			if (y2 !== year) {
				out.push(p);
				year = y2;
			}
		}
		if (last && out[out.length - 1] !== last) out.push(last);
		return out;
	})();

	function move(event: PointerEvent) {
		const svg = (event.currentTarget as SVGElement).ownerSVGElement ?? (event.currentTarget as SVGSVGElement);
		const rect = svg.getBoundingClientRect();
		const px = ((event.clientX - rect.left) / (rect.width || 1)) * (Math.max(200, width));
		cursor = Math.min(t1, Math.max(t0, x.invert(px)));
	}
	function key(event: KeyboardEvent) {
		if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
		event.preventDefault();
		const step = (t1 - t0) / 60;
		cursor = Math.min(t1, Math.max(t0, (cursor ?? t0) + (event.key === 'ArrowRight' ? step : -step)));
	}
	const growthText = (m: number) => fmtPct((m - 1) * 100, m >= 10 ? 0 : 1);
</script>

<div class="grid gap-2" data-testid="growth-chart">
	<div class="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-[#aab1bc]">
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4 bg-[#eef1f5]"></i>Strategy</span>
		{#if showBench}<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4 bg-[#6c7480]"></i>Buy &amp; hold</span>{/if}
		<span class="inline-flex items-center gap-1.5"><i class="inline-block h-0.5 w-4 bg-[#e0663f]"></i>Drawdown</span>
		<span class="ml-auto inline-flex overflow-hidden rounded-full border border-[#2a2f38]" role="group" aria-label="Scale">
			<button type="button" class={`px-2.5 py-0.5 ${log ? 'bg-[#1a1d22] text-white' : 'text-[#777]'}`} aria-pressed={log} on:click={() => (log = true)}>Log</button>
			<button type="button" class={`px-2.5 py-0.5 ${!log ? 'bg-[#1a1d22] text-white' : 'text-[#777]'}`} aria-pressed={!log} on:click={() => (log = false)}>Linear</button>
		</span>
		{#if benchmark.length}
			<button type="button" class={`rounded-full border border-[#2a2f38] px-2.5 py-0.5 ${showBench ? 'bg-[#1a1d22] text-white' : 'text-[#777]'}`} aria-pressed={showBench} on:click={() => (showBench = !showBench)}>Buy &amp; hold</button>
		{/if}
		<button type="button" class={`rounded-full border border-[#2a2f38] px-2.5 py-0.5 ${showTable ? 'bg-[#1a1d22] text-white' : 'text-[#777]'}`} aria-pressed={showTable} on:click={() => (showTable = !showTable)}>Table</button>
	</div>

	{#if strat.length < 2}
		<div class="border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-[12px] text-[#666]">This run stored no equity curve.</div>
	{:else}
		<!-- svelte-ignore a11y-no-noninteractive-tabindex a11y-no-noninteractive-element-interactions -->
		<div class="relative w-full outline-none focus-visible:ring-1 focus-visible:ring-[#8fb0ff]" use:trackWidth={(value) => (width = value || 640)} tabindex="0" role="img" aria-label="Growth and drawdown chart; left and right arrows move the readout" on:keydown={key} on:blur={() => (cursor = null)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible text-[10.5px]">
				{#if oosX !== null}
					<rect x={M.l} y={M.t} width={oosX - M.l} height={topH} fill="rgba(255,255,255,0.028)" />
					<line x1={oosX} x2={oosX} y1={M.t - 14} y2={M.t + h} stroke={CHART.axis} />
					<text x={M.l + 6} y={M.t - 8} fill={CHART.ink3} letter-spacing="0.08em">IN-SAMPLE</text>
					<text x={oosX + 6} y={M.t - 8} fill={CHART.ink3} letter-spacing="0.08em">OUT-OF-SAMPLE</text>
				{/if}
				{#each yTicks as tick (tick)}
					<line x1={M.l} x2={M.l + w} y1={y(tick)} y2={y(tick)} stroke={tick === 1 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
					<text x={M.l - 8} y={y(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{fmtPct((tick - 1) * 100, 0)}</text>
				{/each}
				{#each xt.ticks as tick (tick)}
					<text x={x(tick)} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink3}>{timeTickLabel(tick, xt.monthStep)}</text>
				{/each}
				{#if benchPath}<path d={benchPath} fill="none" stroke={CHART.context} stroke-width="1.5" stroke-linejoin="round" />{/if}
				<path d={`${stratPath}L${x(t1)},${y(lo)}L${x(t0)},${y(lo)}Z`} fill="rgba(238,241,245,0.06)" />
				<path d={stratPath} fill="none" stroke={CHART.ink} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
				{#if last}
					<circle cx={x(last.t)} cy={y(last.m)} r="4" fill={CHART.ink} stroke={CHART.panel} stroke-width="2" />
					<text x={x(last.t) + 8} y={y(last.m) + 4} fill={CHART.ink}>{growthText(last.m)}</text>
				{/if}
				{#if showBench && lastBench}
					<circle cx={x(lastBench.t)} cy={y(lastBench.m)} r="4" fill={CHART.context} stroke={CHART.panel} stroke-width="2" />
					<text x={x(lastBench.t) + 8} y={y(lastBench.m) + 4} fill={CHART.ink3}>{growthText(lastBench.m)}</text>
				{/if}

				<text x={M.l + 6} y={ddTop - 6} fill={CHART.ink3} letter-spacing="0.08em">DRAWDOWN</text>
				{#each ddTicks as tick (tick)}
					<line x1={M.l} x2={M.l + w} y1={yd(tick)} y2={yd(tick)} stroke={tick === 0 ? CHART.axis : CHART.grid} shape-rendering="crispEdges" />
					<text x={M.l - 8} y={yd(tick) + 3.5} text-anchor="end" fill={CHART.ink3}>{fmtPct(tick, 0, false)}</text>
				{/each}
				<path d={`${ddPath}L${x(t1)},${yd(0)}L${x(t0)},${yd(0)}Z`} fill="rgba(224,102,63,0.14)" />
				<path d={ddPath} fill="none" stroke={CHART.loss} stroke-width="1.5" />

				{#if readout}
					<line x1={readout.x} x2={readout.x} y1={M.t} y2={M.t + h} stroke={CHART.ink3} />
					<circle cx={readout.x} cy={readout.y} r="4" fill={CHART.ink} stroke={CHART.panel} stroke-width="2" />
				{/if}
				<rect x={M.l} y={M.t} width={w} height={h} fill="transparent" role="presentation" on:pointermove={move} on:pointerleave={() => (cursor = null)} />
			</svg>
			{#if readout}
				<div class="pointer-events-none absolute top-5 z-10 min-w-[170px] border border-[#2a2f38] bg-[#0b0d11] px-2.5 py-2 text-[11px] shadow-[0_8px_24px_rgba(0,0,0,0.45)]" style={`left:${Math.min(readout.x + 14, Math.max(0, width - 190))}px`} data-testid="growth-readout">
					<div class="mb-1 text-[#777]">{fmtDateUtc(readout.t)} · {readout.inSample ? 'in-sample' : 'out-of-sample'}</div>
					<div class="grid grid-cols-[12px_minmax(0,1fr)_auto] items-center gap-1.5"><i class="h-0.5 w-3 bg-[#eef1f5]"></i><span class="text-[#777]">Strategy</span><span class="text-right text-white">{growthText(readout.strat)} · {fmtUsd(readout.strat * base, 0, false)}</span></div>
					{#if showBench && readout.bench !== null}<div class="grid grid-cols-[12px_minmax(0,1fr)_auto] items-center gap-1.5"><i class="h-0.5 w-3 bg-[#6c7480]"></i><span class="text-[#777]">Buy &amp; hold</span><span class="text-right text-white">{growthText(readout.bench)}</span></div>{/if}
					<div class="grid grid-cols-[12px_minmax(0,1fr)_auto] items-center gap-1.5"><i class="h-0.5 w-3 bg-[#e0663f]"></i><span class="text-[#777]">Drawdown</span><span class="text-right text-white">{fmtPct(readout.dd, 1, false)}</span></div>
				</div>
			{/if}
		</div>
		{#if showTable}
			<div class="overflow-x-auto" data-testid="growth-table">
				<table class="w-full border-collapse text-[12px] tabular-nums">
					<thead><tr class="text-[10px] uppercase tracking-[0.12em] text-[#555]"><th class="px-2 py-1 text-left font-normal">Date</th><th class="px-2 py-1 text-right font-normal">Strategy</th><th class="px-2 py-1 text-right font-normal">Buy &amp; hold</th><th class="px-2 py-1 text-right font-normal">Drawdown</th></tr></thead>
					<tbody>
						{#each tableRows as row (row.t)}
							{@const b = bench.length ? bench[stepIndex(bench.map((p) => p.t), row.t)] : null}
							<tr class="border-t border-[#1d1d1d]"><td class="px-2 py-1 text-left text-[#aab1bc]">{fmtDateUtc(row.t)}</td><td class="px-2 py-1 text-right">{growthText(row.m)}</td><td class="px-2 py-1 text-right">{b ? growthText(b.m) : '—'}</td><td class="px-2 py-1 text-right">{fmtPct(dd[stepIndex(times, row.t)]?.v ?? 0, 1, false)}</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>
