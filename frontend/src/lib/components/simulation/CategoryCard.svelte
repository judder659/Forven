<script lang="ts">
	import type { CategoryScore } from '$lib/api';

	export let category: CategoryScore;

	const ratingColors: Record<string, string> = {
		excellent: 'text-emerald-400',
		good: 'text-emerald-400',
		fair: 'text-yellow-400',
		poor: 'text-red-400',
		unknown: 'text-[#666]'
	};

	const ratingBg: Record<string, string> = {
		excellent: 'bg-emerald-400',
		good: 'bg-emerald-400',
		fair: 'bg-yellow-400',
		poor: 'bg-red-400',
		unknown: 'bg-[#555]'
	};

	$: percentage = Math.round((category.score / category.max_score) * 100);

	function getMetricColor(rating: string): string {
		return ratingColors[rating] || ratingColors.unknown;
	}

	function formatValue(value: number | null, name: string): string {
		if (value === null || value === undefined) return 'N/A';

		// Format based on metric type
		const lowerName = name.toLowerCase();
		if (lowerName.includes('rate') || lowerName.includes('return') || lowerName.includes('drawdown')) {
			return `${value.toFixed(1)}%`;
		}
		if (lowerName.includes('ratio') || lowerName.includes('factor') || lowerName.includes('efficiency')) {
			return value.toFixed(2);
		}
		if (lowerName.includes('count') || lowerName.includes('trades')) {
			return Math.round(value).toString();
		}
		if (lowerName.includes('years')) {
			return `${value.toFixed(1)} yrs`;
		}
		if (lowerName.includes('month')) {
			return `${value.toFixed(1)}/mo`;
		}
		return value.toFixed(2);
	}
</script>

<div class="terminal-card">
	<!-- Category Header -->
	<div class="flex items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
		<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">{category.name}</h3>
		<div class="flex items-center gap-2">
			<span class="text-[10px] uppercase tracking-wider {ratingColors[category.rating]}">
				{category.rating}
			</span>
			<span class="text-[10px] text-[#666]">{category.score}/{category.max_score}</span>
		</div>
	</div>

	<div class="p-4">
	<!-- Progress Bar -->
		<div class="h-1.5 bg-[#1a1a1a] mb-4 overflow-hidden">
			<div
				class="h-full {ratingBg[category.rating]} transition-all duration-300"
				style="width: {percentage}%"
			></div>
		</div>

	<!-- Metrics -->
	<div class="space-y-2">
		{#each category.metrics as metric}
			<div class="flex items-center justify-between text-xs">
					<div class="flex items-center gap-2">
						<div
							class="h-1.5 w-1.5 rounded-full {ratingBg[metric.rating]}"
							title={metric.rating}
						></div>
					<span class="text-[#aaa]">{metric.name}</span>
				</div>
				<div class="flex items-center gap-3">
					<span class="{getMetricColor(metric.rating)} font-mono">
						{formatValue(metric.value, metric.name)}
					</span>
					<span class="text-[#666] text-xs w-8 text-right">
						{metric.score}/{metric.max_score}
					</span>
				</div>
			</div>
		{/each}
	</div>
	</div>
</div>
