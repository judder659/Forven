<script lang="ts">
	import type { Scorecard, CategoryScore } from '$lib/api';

	export let scorecard: Scorecard;

	const gradeColors: Record<string, string> = {
		A: 'text-emerald-400',
		B: 'text-emerald-400',
		C: 'text-yellow-400',
		D: 'text-red-400',
		F: 'text-red-400'
	};

	const gradeBgColors: Record<string, string> = {
		A: 'bg-emerald-500',
		B: 'bg-emerald-500',
		C: 'bg-yellow-500',
		D: 'bg-red-500',
		F: 'bg-red-500'
	};

	const ratingBarColors: Record<string, string> = {
		excellent: 'bg-emerald-400',
		good: 'bg-emerald-400',
		fair: 'bg-yellow-400',
		poor: 'bg-red-400',
		unknown: 'bg-[#444]'
	};

	const ratingTextColors: Record<string, string> = {
		excellent: 'text-emerald-400',
		good: 'text-emerald-400',
		fair: 'text-yellow-400',
		poor: 'text-red-400',
		unknown: 'text-[#666]'
	};

	const deploymentColors: Record<string, { bg: string; border: string; text: string }> = {
		approved: { bg: 'bg-emerald-500/10', border: 'border-emerald-900', text: 'text-emerald-400' },
		approved_with_conditions: {
			bg: 'bg-emerald-500/10',
			border: 'border-emerald-900',
			text: 'text-emerald-400'
		},
		not_recommended: {
			bg: 'bg-yellow-500/10',
			border: 'border-yellow-900',
			text: 'text-yellow-400'
		},
		rejected: { bg: 'bg-red-500/10', border: 'border-red-900', text: 'text-red-400' }
	};

	const deploymentLabels: Record<string, string> = {
		approved: 'APPROVED',
		approved_with_conditions: 'APPROVED WITH CONDITIONS',
		not_recommended: 'NOT RECOMMENDED',
		rejected: 'REJECTED'
	};

	const sectionMeta: Record<string, { label: string; categoryName: string }> = {
		profitability_analysis: { label: 'Profitability', categoryName: 'Profitability' },
		risk_analysis: { label: 'Risk Management', categoryName: 'Risk Management' },
		robustness_analysis: { label: 'Robustness', categoryName: 'Robustness' },
		statistical_analysis: { label: 'Statistical Validity', categoryName: 'Statistical Validity' }
	};

	let metricsExpanded = false;

	$: percentage = scorecard.max_score > 0
		? Math.round((scorecard.total_score / scorecard.max_score) * 100)
		: 0;
	$: deploymentStyle = deploymentColors[scorecard.deployment_verdict] || deploymentColors.rejected;
	$: writeup = scorecard.analysis_writeup || ({} as Record<string, string>);
	$: categoryMap = buildCategoryMap(scorecard.categories);

	function getWriteupSection(key: string): string {
		return (writeup as Record<string, string>)[key] || '';
	}

	function buildCategoryMap(cats: CategoryScore[]): Record<string, CategoryScore> {
		const map: Record<string, CategoryScore> = {};
		for (const c of cats) {
			map[c.name] = c;
		}
		return map;
	}

	function getCategoryForSection(sectionKey: string): CategoryScore | null {
		const meta = sectionMeta[sectionKey];
		if (!meta) return null;
		return categoryMap[meta.categoryName] || null;
	}

	function formatMetricValue(value: number | null, name: string): string {
		if (value === null || value === undefined) return 'N/A';
		const lower = name.toLowerCase();
		if (lower.includes('rate') || lower.includes('return') || lower.includes('drawdown') || lower.includes('consistency')) {
			return `${value.toFixed(1)}%`;
		}
		if (lower.includes('ratio') || lower.includes('factor') || lower.includes('efficiency') || lower.includes('stability')) {
			return value.toFixed(2);
		}
		if (lower.includes('count') || lower.includes('trades')) {
			return Math.round(value).toString();
		}
		if (lower.includes('years')) {
			return `${value.toFixed(1)} yrs`;
		}
		if (lower.includes('month')) {
			return `${value.toFixed(1)}/mo`;
		}
		if (lower.includes('probability') || lower.includes('prob')) {
			return `${value.toFixed(0)}%`;
		}
		return value.toFixed(2);
	}
</script>

<div class="space-y-4">
	<!-- Header: Grade Circle + Verdict + Deployment Badge -->
	<div class="terminal-card p-5">
		<div class="flex items-center gap-6">
			<!-- Grade Circle -->
			<div class="relative flex-shrink-0" style="width: 80px; height: 80px;">
				<svg viewBox="0 0 100 100" class="transform -rotate-90">
					<circle
						cx="50" cy="50" r="40" fill="none" stroke="#222" stroke-width="6"
					/>
					<circle
						cx="50" cy="50" r="40" fill="none"
						stroke={scorecard.grade === 'A' || scorecard.grade === 'B' ? '#34d399'
							: scorecard.grade === 'C' ? '#facc15'
							: scorecard.grade === 'D' ? '#f87171' : '#f87171'}
						stroke-width="6" stroke-linecap="round"
						stroke-dasharray={2 * Math.PI * 40}
						stroke-dashoffset={2 * Math.PI * 40 - (percentage / 100) * 2 * Math.PI * 40}
						class="transition-all duration-500"
					/>
				</svg>
				<div class="absolute inset-0 flex items-center justify-center">
					<span class="text-3xl font-bold {gradeColors[scorecard.grade]}">{scorecard.grade}</span>
				</div>
			</div>

			<!-- Verdict Text -->
			<div class="flex-1 min-w-0">
				<h2 class="text-xl font-bold uppercase tracking-widest text-white">Strategy Analysis</h2>
				<p class="text-[#888] text-sm mt-0.5">
					{scorecard.strategy_name || 'Unknown Strategy'}
					{#if scorecard.symbol}
						<span class="text-[#666]">on {scorecard.symbol}</span>
					{/if}
					{#if scorecard.timeframe}
						<span class="text-[#666]">({scorecard.timeframe})</span>
					{/if}
				</p>
				<p class="text-sm mt-1">
					<span class="{gradeColors[scorecard.grade]} font-semibold">{scorecard.verdict}</span>
					<span class="text-[#666] ml-1">— {scorecard.total_score}/{scorecard.max_score} pts</span>
				</p>
			</div>

			<!-- Deployment Badge -->
			<div class="flex-shrink-0">
				<div class="px-3 py-1.5 border {deploymentStyle.bg} {deploymentStyle.border}">
					<span class="text-xs font-bold uppercase tracking-widest {deploymentStyle.text}">
						{deploymentLabels[scorecard.deployment_verdict]}
					</span>
				</div>
				<div class="text-[10px] text-[#666] mt-1 text-right">
					Tests: {scorecard.tests_included.join(', ')}
				</div>
			</div>
		</div>
	</div>

	<!-- Executive Summary -->
	{#if writeup.executive_summary}
		<div class="terminal-card p-5">
			<h3 class="text-[10px] font-bold text-[#888] uppercase tracking-widest mb-3">Executive Summary</h3>
			<p class="text-sm text-[#888] leading-relaxed">{writeup.executive_summary}</p>
		</div>
	{/if}

	<!-- Analysis Sections -->
	{#each ['profitability_analysis', 'risk_analysis', 'robustness_analysis', 'statistical_analysis'] as sectionKey}
		{@const cat = getCategoryForSection(sectionKey)}
		{@const meta = sectionMeta[sectionKey]}
		{@const text = getWriteupSection(sectionKey)}
		{#if text || cat}
			<div class="terminal-card p-5">
				<!-- Section Header with progress bar -->
				<div class="flex items-center justify-between mb-3">
					<h3 class="text-[10px] font-bold text-white uppercase tracking-widest">{meta.label}</h3>
					{#if cat}
						<div class="flex items-center gap-3">
							<!-- Mini progress bar -->
								<div class="w-24 h-1.5 bg-[#1a1a1a] overflow-hidden">
									<div
										class="h-full {ratingBarColors[cat.rating]}"
										style="width: {cat.max_score > 0 ? Math.round((cat.score / cat.max_score) * 100) : 0}%"
									></div>
								</div>
							<span class="text-xs {ratingTextColors[cat.rating]} capitalize font-medium">
								{cat.rating}
							</span>
							<span class="text-xs text-[#666]">{cat.score}/{cat.max_score}</span>
						</div>
					{/if}
				</div>

				<!-- Analysis paragraph -->
				{#if text}
					<p class="text-sm text-[#888] leading-relaxed">{text}</p>
				{:else}
					<p class="text-sm text-[#666] italic">No analysis data available for this category.</p>
				{/if}
			</div>
		{/if}
	{/each}

	<!-- Bottom Line -->
	{#if writeup.bottom_line}
		<div class="terminal-card p-5">
			<h3 class="text-[10px] font-bold text-[#888] uppercase tracking-widest mb-3">Bottom Line</h3>
			<p class="text-sm text-[#888] leading-relaxed">{writeup.bottom_line}</p>
		</div>
	{/if}

	<!-- Red Flags Banner -->
	{#if scorecard.red_flags.length > 0}
		<div class="border border-red-900 bg-red-500/5 p-5">
			<div class="flex items-center gap-2 mb-3">
				<svg class="w-5 h-5 text-red-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
					<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
				</svg>
				<h3 class="text-[10px] font-bold text-red-400 uppercase tracking-widest">
					{scorecard.red_flags.length} Red Flag{scorecard.red_flags.length > 1 ? 's' : ''}
				</h3>
			</div>
			<div class="space-y-2">
				{#each scorecard.red_flags as flag}
					<p class="text-sm text-red-400 pl-7">{flag}</p>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Collapsible Metrics Reference -->
	<div class="terminal-card">
		<button
			class="w-full flex items-center justify-between p-4 text-left hover:bg-[#111] transition-colors"
			on:click={() => (metricsExpanded = !metricsExpanded)}
		>
			<span class="text-[10px] uppercase tracking-widest text-[#888] font-bold">Key Metrics Reference</span>
			<svg
				class="w-4 h-4 text-[#666] transition-transform {metricsExpanded ? 'rotate-180' : ''}"
				fill="none" stroke="currentColor" viewBox="0 0 24 24"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
			</svg>
		</button>

		{#if metricsExpanded}
			<div class="px-4 pb-4 space-y-4">
				{#each scorecard.categories as category}
					<div>
						<div class="flex items-center justify-between mb-2">
							<h4 class="text-[10px] font-bold text-[#666] uppercase tracking-wider">{category.name}</h4>
							<span class="text-xs text-[#666]">{category.score}/{category.max_score}</span>
						</div>
						<div class="space-y-1">
							{#each category.metrics as metric}
								<div class="flex items-center justify-between text-xs">
									<div class="flex items-center gap-2">
										<div class="w-1.5 h-1.5 rounded-full {ratingBarColors[metric.rating]}"></div>
										<span class="text-[#888]">{metric.name}</span>
									</div>
									<div class="flex items-center gap-2">
										<span class="{ratingTextColors[metric.rating]} font-mono">
											{formatMetricValue(metric.value, metric.name)}
										</span>
										<span class="text-[#555] w-6 text-right">{metric.score}/{metric.max_score}</span>
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
