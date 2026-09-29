<script lang="ts">
	import type { Bullet, EvidenceTone } from '$lib/utils/strategyContainer/evidence';
	import { CHART } from '$lib/utils/strategyContainer/chart';

	/** A result against its threshold: the passing zone, the threshold tick, the result dot. */
	export let bullet: Bullet | null = null;
	export let tone: EvidenceTone = 'ok';

	const clamp = (value: number) => Math.max(0, Math.min(100, value));
	$: pos = (value: number) => (bullet ? clamp(((value - bullet.min) / (bullet.max - bullet.min || 1)) * 100) : 0);
	$: thresholdPos = bullet ? pos(bullet.threshold) : 0;
	$: valuePos = bullet ? pos(bullet.value) : 0;
	$: zoneLeft = bullet?.direction === 'ge' ? thresholdPos : 0;
	$: zoneWidth = bullet?.direction === 'ge' ? 100 - thresholdPos : thresholdPos;
	$: dotColor = tone === 'ok' ? CHART.ok : tone === 'caution' ? CHART.caution : tone === 'fail' ? CHART.fail : CHART.ink3;
</script>

{#if bullet}
	<div class="relative h-[18px] min-w-[96px]" role="img" aria-label={`result ${bullet.value.toFixed(2)} against threshold ${bullet.threshold.toFixed(2)}`}>
		<div class="absolute inset-x-0 top-[7px] h-1 rounded-full bg-sc-line2"></div>
		<div class="absolute top-[7px] h-1 bg-[#3cc48f]/35" style={`left:${zoneLeft}%;width:${zoneWidth}%`}></div>
		<div class="absolute top-[2px] h-[14px] w-[2px] bg-sc-ink2" style={`left:calc(${thresholdPos}% - 1px)`}></div>
		<div class="absolute top-1 h-2.5 w-2.5 -translate-x-1/2 rounded-full border-2 border-sc-panel" style={`left:${valuePos}%;background:${dotColor}`}></div>
	</div>
{:else}
	<span class="text-sc-ink4">—</span>
{/if}
