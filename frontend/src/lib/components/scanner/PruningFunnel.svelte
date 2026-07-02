<script lang="ts">
	export let stages: { stage: string; count: number }[] = [];

	const LABELS: Record<string, string> = {
		passed: 'Passed',
		no_trades: 'No Trades',
		too_few_trades: 'Few Trades',
		low_sharpe: 'Low Sharpe',
		low_pf: 'Low PF',
		high_dd: 'High DD',
		corr_dedup: 'Dedup',
		timeout: 'Timeout',
		error: 'Error',
		risk_violation: 'Risk',
		other: 'Other',
	};

	const COLORS: Record<string, string> = {
		passed: '#34d399',
		no_trades: '#991b1b',
		too_few_trades: '#b91c1c',
		low_sharpe: '#dc2626',
		low_pf: '#ef4444',
		high_dd: '#f87171',
		corr_dedup: '#facc15',
		timeout: '#999999',
		error: '#666666',
		risk_violation: '#eab308',
		other: '#444444',
	};

	function getLabel(stage: string): string {
		return LABELS[stage] ?? stage;
	}

	function getColor(stage: string): string {
		return COLORS[stage] ?? '#444444';
	}

	$: total = stages.reduce((sum, s) => sum + s.count, 0);
	$: passedCount = stages.find(s => s.stage === 'passed')?.count ?? 0;
	$: sorted = [...stages].sort((a, b) => {
		if (a.stage === 'passed') return -1;
		if (b.stage === 'passed') return 1;
		return b.count - a.count;
	});
</script>

{#if total > 0}
	<div class="space-y-1">
		<div class="flex items-center gap-2 text-[10px] uppercase tracking-wider text-[#666]">
			<span>{total.toLocaleString()} tested</span>
			<span class="text-[#444]">&rarr;</span>
			<span class="text-emerald-400 font-medium">{passedCount.toLocaleString()} passed</span>
			<span class="text-[#444]">({total > 0 ? ((passedCount / total) * 100).toFixed(1) : 0}%)</span>
		</div>
		<div class="flex h-3 overflow-hidden bg-[#222] border border-[#222]">
			{#each sorted as s (s.stage)}
				{@const pct = total > 0 ? (s.count / total) * 100 : 0}
				{#if pct > 0.5}
					<div
						class="h-full transition-all"
						style="width: {pct}%; background-color: {getColor(s.stage)};"
						title="{getLabel(s.stage)}: {s.count.toLocaleString()} ({pct.toFixed(1)}%)"
					></div>
				{/if}
			{/each}
		</div>
		<div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px]">
			{#each sorted as s (s.stage)}
				{#if s.count > 0}
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 inline-block" style="background-color: {getColor(s.stage)};"></span>
						<span class="text-[#666]">{getLabel(s.stage)}</span>
						<span class="text-[#888]">{s.count.toLocaleString()}</span>
					</span>
				{/if}
			{/each}
		</div>
	</div>
{/if}
