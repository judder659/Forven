<script lang="ts">
	import type { Fold } from '$lib/utils/strategyContainer/evidence';
	import { fmtDateUtc, fmtFraction, fmtMonthYear, isNum, parseTimestamp, signClass } from '$lib/utils/strategyContainer/format';

	/** Each body of evidence on one time axis, from the first in-sample bar to today. */
	export let segments: Array<{ label: string; start: string | null; end: string | null; totalReturn: number | null }> = [];
	export let folds: Fold[] = [];
	export let now: number = Date.now();

	const SHADES = ['rgba(255,255,255,0.05)', 'rgba(255,255,255,0.09)', 'rgba(255,255,255,0.14)', 'rgba(238,241,245,0.30)', 'rgba(143,176,255,0.35)'];

	$: spans = segments
		.map((segment) => ({ ...segment, a: parseTimestamp(segment.start), b: parseTimestamp(segment.end) ?? now }))
		.filter((segment): segment is typeof segment & { a: number; b: number } => segment.a !== null);
	$: t0 = spans.length ? Math.min(...spans.map((s) => s.a)) : now;
	$: t1 = Math.max(now, ...spans.map((s) => s.b));
	$: pos = (t: number) => ((t - t0) / (t1 - t0 || 1)) * 100;
	$: years = (() => {
		const out: number[] = [];
		for (let year = new Date(t0).getUTCFullYear() + 1; year <= new Date(t1).getUTCFullYear(); year += 1) out.push(year);
		return out;
	})();
</script>

{#if spans.length}
	<div class="grid gap-1.5" data-testid="evidence-timeline">
		<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Evidence timeline{folds.length ? ' · walk-forward test folds below' : ''}</div>
		<div class="relative h-[34px] overflow-hidden bg-[#0d0d0d]" role="img" aria-label="Evidence timeline">
			{#each spans as span, index (span.label)}
				{@const left = pos(span.a)}
				{@const width = Math.max(0.6, pos(span.b) - left)}
				<div class="absolute inset-y-0 overflow-hidden border-r-2 border-[#090909] px-1.5 py-0.5" style={`left:${left}%;width:${width}%;background:${SHADES[index % SHADES.length]}`} title={`${span.label}: ${fmtDateUtc(span.a)} – ${fmtDateUtc(span.b)}${isNum(span.totalReturn) ? ` · ${fmtFraction(span.totalReturn)}` : ''}`}>
					{#if width > 8}
						<div class="whitespace-nowrap text-[11px] text-[#aab1bc]">{span.label}</div>
						<div class={`whitespace-nowrap text-[11px] tabular-nums ${signClass(span.totalReturn)}`}>{isNum(span.totalReturn) ? fmtFraction(span.totalReturn) : ''}</div>
					{/if}
				</div>
			{/each}
		</div>
		{#if folds.length}
			<div class="relative h-3" title="Walk-forward test folds">
				{#each folds as fold (fold.fold)}
					{@const a = parseTimestamp(fold.testStart)}
					{@const b = parseTimestamp(fold.testEnd)}
					{#if a !== null && b !== null}
						<i class="absolute top-[3px] h-1.5 rounded-full bg-[#4b525c]" style={`left:${pos(a)}%;width:${Math.max(0.4, pos(b) - pos(a))}%`} title={`Fold ${fold.fold}: ${fmtDateUtc(a)} – ${fmtDateUtc(b)}`}></i>
					{/if}
				{/each}
			</div>
		{/if}
		<div class="flex justify-between text-[10.5px] tabular-nums text-[#666]">
			<span>{fmtMonthYear(t0)}</span>
			{#each years as year (year)}<span>{year}</span>{/each}
			<span>today</span>
		</div>
	</div>
{/if}
