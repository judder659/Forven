<script lang="ts">
	import type { NavMetric, NavPulse } from '$lib/stores/navMetrics';

	/** Heartbeat state indicator (standing facts: pending approvals, HALT, scans running). */
	export let metric: NavMetric | undefined = undefined;
	/** Realtime event pulse (things that happened since the tab was last visited) — wins over metric. */
	export let pulse: NavPulse | undefined = undefined;
	/** No badge while the route is being viewed; visiting is what clears it. */
	export let active = false;

	const COUNT_COLORS: Record<string, string> = {
		danger: 'border border-red-900 bg-red-500/10 text-red-400',
		warn: 'border border-yellow-900 bg-yellow-500/10 text-yellow-400',
		success: 'border border-emerald-900 bg-emerald-500/10 text-emerald-400',
		info: 'border border-[#555] text-white',
		neutral: 'border border-[#333] text-[#888]',
	};

	const COUNT_COLORS_SEEN: Record<string, string> = {
		danger: 'border border-red-900 text-red-400',
		warn: 'border border-yellow-900 text-yellow-500',
		success: 'border border-emerald-900 text-emerald-500',
		info: 'border border-[#333] text-[#999]',
		neutral: 'border border-[#333] text-[#666]',
	};

	const PILL_COLORS: Record<string, string> = {
		danger: 'border-red-900 bg-red-500/10 text-red-400',
		warn: 'border-yellow-900 bg-yellow-500/10 text-yellow-400',
		success: 'border-emerald-900 bg-emerald-500/10 text-emerald-400',
		info: 'border-[#555] text-white',
		neutral: 'border-[#333] text-[#888]',
	};

	const DOT_COLORS: Record<string, string> = {
		danger: 'bg-red-500',
		warn: 'bg-yellow-400',
		success: 'bg-emerald-400',
		info: 'bg-white',
		neutral: 'bg-[#666]',
	};

	function countLabel(count: number): string {
		return count > 99 ? '99+' : String(count);
	}

	$: showPulse = !active && !!pulse && pulse.count > 0;
	// Count/activity badges are NEWS: once the route has been visited they
	// disappear entirely until the underlying seen_key changes (new approval,
	// new trade set, new notifications). Only status pills (HALT, AUTH) persist
	// while their condition holds — they flag standing hazards, not news.
	$: showMetric =
		!active
		&& !showPulse
		&& !!metric
		&& metric.kind !== 'none'
		&& (metric.kind === 'status' || !metric.seen);
	$: metricDimmed = !!metric && metric.seen && metric.severity !== 'danger';
</script>

{#if showPulse && pulse}
	<span class="relative flex shrink-0" title={pulse.summary}>
		<span class="relative min-w-[18px] h-[18px] px-1 text-[10px] font-bold flex items-center justify-center animate-pulse {COUNT_COLORS[pulse.severity] ?? COUNT_COLORS.neutral}">
			{countLabel(pulse.count)}
		</span>
	</span>
{:else if showMetric && metric}
	{#if metric.kind === 'count' && metric.count > 0}
		<span
			class="shrink-0 min-w-[18px] h-[18px] px-1 text-[10px] font-bold flex items-center justify-center {(metricDimmed ? COUNT_COLORS_SEEN : COUNT_COLORS)[metric.severity] ?? COUNT_COLORS.neutral}"
			title={metric.summary}
		>
			{countLabel(metric.count)}
		</span>
	{:else if metric.kind === 'status' && metric.label}
		<span
			class="shrink-0 border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider {PILL_COLORS[metric.severity] ?? PILL_COLORS.neutral} {metricDimmed ? 'opacity-50' : ''}"
			title={metric.summary}
		>
			{metric.label}
		</span>
	{:else if metric.kind === 'activity'}
		<span
			class="shrink-0 w-2 h-2 rounded-full animate-pulse {DOT_COLORS[metric.severity] ?? DOT_COLORS.neutral} {metricDimmed ? 'opacity-50' : ''}"
			title={metric.summary}
		></span>
	{/if}
{/if}
