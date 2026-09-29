<script lang="ts">
	import { CHART, linearScale, niceTicks, trackWidth } from '$lib/utils/strategyContainer/chart';
	import { MIN_JITTER_RERUNS } from '$lib/utils/strategyContainer/evidence';
	import { fmtNum, isNum, toNumber } from '$lib/utils/strategyContainer/format';

	/** Sharpe of each nudged rerun against the pass line, and how many reruns finished. */
	export let payload: Record<string, unknown> | null = null;
	export let height = 96;

	let width = 420;
	const M = { t: 26, r: 16, b: 24, l: 16 };

	const parseList = (value: unknown): number[] => {
		const list = typeof value === 'string' ? (() => { try { return JSON.parse(value); } catch { return []; } })() : value;
		return (Array.isArray(list) ? list : []).map(toNumber).filter(isNum);
	};
	$: values = parseList(payload?.sharpe_values);
	$: reference = toNumber(payload?.reference_sharpe) ?? toNumber(payload?.original_sharpe);
	$: allowed = toNumber(payload?.allowed_degradation);
	$: floor = isNum(reference) && isNum(allowed) ? reference * (1 - allowed) : null;
	$: planned = toNumber(payload?.n_iterations);
	$: completed = toNumber(payload?.iterations_completed) ?? values.length;
	$: jitterPct = toNumber(payload?.jitter_pct);
	$: passRate = toNumber(payload?.pass_rate);
	$: threshold = toNumber(payload?.verdict_threshold);
	$: hi = Math.max(0.5, isNum(reference) ? reference : 0, ...values) * 1.25;
	$: lo = Math.min(0, ...values);
	$: w = Math.max(200, width) - M.l - M.r;
	$: h = height - M.t - M.b;
	$: x = linearScale(lo, hi, M.l, M.l + w);
	$: cy = M.t + h / 2;
	$: thin = completed < MIN_JITTER_RERUNS;
</script>

{#if values.length || isNum(reference)}
	<div class="grid gap-3" data-testid="jitter-strip">
		<div use:trackWidth={(value) => (width = value || 420)}>
			<svg viewBox={`0 0 ${Math.max(200, width)} ${height}`} {height} class="block w-full overflow-visible font-plex-mono text-[10.5px]" role="img" aria-label={`Rerun Sharpes ${values.map((v) => fmtNum(v)).join(', ')} against a pass line of ${fmtNum(floor)}`}>
				<line x1={M.l} x2={M.l + w} y1={cy} y2={cy} stroke={CHART.axis} />
				{#if floor !== null}
					<rect x={x(floor)} y={cy - 10} width={Math.max(0, x(hi) - x(floor))} height="20" fill="rgba(60,196,143,0.08)" />
					<line x1={x(floor)} x2={x(floor)} y1={cy - 14} y2={cy + 14} stroke={CHART.ink3} stroke-width="1.5" />
					<text x={x(floor)} y={M.t - 10} text-anchor="middle" fill={CHART.ink3}>pass line {fmtNum(floor)}</text>
				{/if}
				{#each niceTicks(lo, hi, 6) as tick (tick)}
					<text x={x(tick)} y={M.t + h + 16} text-anchor="middle" fill={CHART.ink3}>{fmtNum(tick, 1)}</text>
				{/each}
				{#if isNum(reference)}
					<line x1={x(reference)} x2={x(reference)} y1={cy - 14} y2={cy + 14} stroke={CHART.ink} stroke-width="2" />
					<text x={x(reference)} y={M.t - 10} text-anchor={floor !== null && Math.abs(x(reference) - x(floor)) < 90 ? 'start' : 'middle'} dx={floor !== null && Math.abs(x(reference) - x(floor)) < 90 ? 8 : 0} fill={CHART.ink2}>baseline {fmtNum(reference)}</text>
				{/if}
				{#each values as value, index (index)}
					<circle cx={x(value)} {cy} r="5" fill={isNum(floor) && value < floor ? CHART.loss : CHART.ink2} stroke={CHART.panel} stroke-width="2"><title>Rerun {index + 1}: Sharpe {fmtNum(value)}</title></circle>
				{/each}
			</svg>
		</div>
		{#if isNum(planned)}
			<div class="grid gap-1.5">
				<div class={`font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] ${thin ? 'text-[#e7b24a]' : 'text-sc-ink4'}`}>Reruns · {completed} of {planned} finished{payload?.deadline_hit === true ? ' before the time limit' : ''}</div>
				<div class="flex flex-wrap gap-1" role="img" aria-label={`${completed} of ${planned} reruns finished`} data-testid="jitter-slots">
					{#each Array.from({ length: planned }, (_, index) => index) as slot (slot)}
						<i class={`block h-2.5 w-4 ${slot < completed ? 'bg-sc-ink2' : 'border border-sc-line2'}`}></i>
					{/each}
				</div>
			</div>
		{/if}
		<p class="m-0 text-[12px] leading-relaxed text-sc-ink3">
			Every parameter nudged {isNum(jitterPct) ? `±${jitterPct}%` : 'slightly'} and rerun. {isNum(passRate) ? `${Math.round(passRate * 100)}% of finished reruns kept at least ${isNum(allowed) ? `${Math.round((1 - allowed) * 100)}%` : 'half'} of the baseline Sharpe` : ''}{isNum(threshold) ? ` (gate ≥ ${Math.round(threshold * 100)}%).` : '.'}
			{#if thin}<span class="text-[#e7b24a]"> {completed} rerun{completed === 1 ? '' : 's'} is too few to trust; rerun the test before relying on it.</span>{/if}
		</p>
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">Parameter jitter has not run.</div>
{/if}
