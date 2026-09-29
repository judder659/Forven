<script lang="ts">
	import type { SkillOutcomeEvent } from '$lib/api';

	/** Outcome events ordered most-recent first (the API default). The component
	 * reverses them internally to plot cumulative confidence chronologically. */
	export let outcomes: SkillOutcomeEvent[] = [];
	/** Current confidence (the right-most point of the trajectory). */
	export let currentConfidence: number;
	export let width = 80;
	export let height = 24;

	function buildSeries(events: SkillOutcomeEvent[], current: number): number[] {
		if (!events.length) return [current, current];
		// Walk back from the current value applying inverse deltas to recover
		// the historical trajectory. API returns events newest-first.
		let conf = current;
		const reversed = [conf];
		for (const ev of events) {
			conf = Math.max(0, Math.min(1, conf - (ev.confidence_delta ?? 0)));
			reversed.push(conf);
		}
		return reversed.reverse();
	}

	function buildPath(values: number[], w: number, h: number): string {
		if (values.length < 2) return '';
		const stepX = w / (values.length - 1);
		const pad = 2;
		const usableH = h - pad * 2;
		return values
			.map((v, i) => {
				const x = i * stepX;
				const clamped = Math.max(0, Math.min(1, v));
				const y = pad + usableH - clamped * usableH;
				return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}

	$: series = buildSeries(outcomes, currentConfidence);
	$: trend = series.length >= 2 ? series[series.length - 1] - series[0] : 0;
	$: stroke = trend > 0.001 ? '#4ade80' : trend < -0.001 ? '#f87171' : '#9ca3af';
</script>

{#if series.length >= 2}
	<svg
		{width}
		{height}
		viewBox="0 0 {width} {height}"
		class="inline-block align-middle"
		role="img"
		aria-label="Confidence trajectory sparkline"
	>
		<path d={buildPath(series, width, height)} fill="none" stroke={stroke} stroke-width="1.5" />
	</svg>
{:else}
	<span class="text-[10px] text-sc-ink4">--</span>
{/if}
