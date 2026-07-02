<script lang="ts">
	import { onMount } from 'svelte';

	export let result: any;
	export let objective: string = 'sharpe_ratio';

	// Extract folds from result
	$: folds = result?.config?.folds || [];
	$: metrics = result?.metrics || {};

	// Calculate total time range
	$: totalStart = folds.length > 0 ? folds[0].train_start : '';
	$: totalEnd = folds.length > 0 ? folds[folds.length - 1].test_end : '';

	function getPosition(dateStr: string): number {
		if (!totalStart || !totalEnd) return 0;
		const start = new Date(totalStart).getTime();
		const end = new Date(totalEnd).getTime();
		const current = new Date(dateStr).getTime();
		return Math.max(0, Math.min(100, ((current - start) / (end - start)) * 100));
	}

	function formatDate(dateStr: string): string {
		return new Date(dateStr).toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
	}

	function getTestMetric(fold: any): number {
		return fold?.test_metrics?.[objective] ?? fold?.test_metric ?? 0;
	}

	function getTrainMetric(fold: any): number {
		return fold?.train_metric ?? 0;
	}

	// Get max absolute value for scaling bars
	$: maxMetric = Math.max(
		...folds.map((f: any) => Math.abs(getTrainMetric(f))),
		...folds.map((f: any) => Math.abs(getTestMetric(f))),
		0.1
	);

	// Color based on performance
	function getPerformanceColor(value: number): string {
		if (value > 1) return 'bg-emerald-500';
		if (value > 0.5) return 'bg-emerald-600';
		if (value > 0) return 'bg-yellow-500';
		if (value > -0.5) return 'bg-red-500';
		return 'bg-red-500';
	}

	// Overfitting ratio interpretation
	function getOverfitColor(ratio: number): string {
		if (ratio > 0.7) return '#22c55e'; // green
		if (ratio > 0.3) return '#eab308'; // yellow
		return '#ef4444'; // red
	}

	// Extract unique parameters across folds
	$: allParams = (() => {
		const params = new Set<string>();
		folds.forEach((f: any) => {
			if (f.best_params) {
				Object.keys(f.best_params).forEach(k => params.add(k));
			}
		});
		return Array.from(params);
	})();

	// Get param value range for heatmap
	function getParamRange(param: string): { min: number; max: number } {
		const values = folds
			.map((f: any) => f.best_params?.[param])
			.filter((v: any) => typeof v === 'number');
		if (values.length === 0) return { min: 0, max: 1 };
		return { min: Math.min(...values), max: Math.max(...values) };
	}

	function getParamHeatColor(param: string, value: any): string {
		if (typeof value !== 'number') return 'bg-[#333]';
		const range = getParamRange(param);
		if (range.max === range.min) return 'bg-[#555]';
		const normalized = (value - range.min) / (range.max - range.min);
		// Monochrome lightness ramp (darker = lower, lighter = higher)
		const lightness = 22 + normalized * 45;
		return `hsl(0, 0%, ${lightness}%)`;
	}

	// Helper function to get param values for variance calculation
	function getParamValues(param: string): any[] {
		return folds.map((f: any) => f.best_params?.[param]).filter((v: any) => v !== undefined);
	}

	// Helper function to calculate variance
	function calculateVariance(numValues: any[]): number {
		const nums = numValues.filter((v): v is number => typeof v === 'number');
		if (nums.length <= 1) return 0;
		const mean = nums.reduce((a, b) => a + b, 0) / nums.length;
		return nums.reduce((sum, v) => sum + Math.pow(v - mean, 2), 0) / nums.length;
	}
</script>

<div class="space-y-8">
	<!-- 1. OVERFITTING GAUGE -->
	<div class="terminal-card p-4">
		<h4 class="text-[10px] font-bold text-white uppercase tracking-widest mb-4">Overfitting Analysis</h4>

		<div class="flex items-center gap-8">
			<!-- Gauge -->
			<div class="relative w-32 h-32">
				<svg viewBox="0 0 100 100" class="transform -rotate-90">
					<!-- Background arc -->
					<circle
						cx="50" cy="50" r="40"
						fill="none"
						stroke="#222"
						stroke-width="12"
						stroke-dasharray="188.5"
						stroke-dashoffset="62.8"
					/>
					<!-- Value arc -->
					<circle
						cx="50" cy="50" r="40"
						fill="none"
						stroke={getOverfitColor(metrics.overfitting_ratio || 0)}
						stroke-width="12"
						stroke-dasharray="188.5"
						stroke-dashoffset={188.5 - (Math.max(0, Math.min(1, (metrics.overfitting_ratio || 0))) * 125.7)}
						stroke-linecap="round"
						class="transition-all duration-500"
					/>
				</svg>
				<div class="absolute inset-0 flex items-center justify-center">
					<span class="text-2xl font-bold text-white">
						{((metrics.overfitting_ratio || 0) * 100).toFixed(0)}%
					</span>
				</div>
			</div>

			<!-- Legend -->
			<div class="flex-1 space-y-2 text-sm">
				<div class="flex items-center gap-2">
					<div class="w-3 h-3 bg-emerald-500"></div>
					<span class="text-[#888]">&gt;70%: Low overfitting (robust)</span>
				</div>
				<div class="flex items-center gap-2">
					<div class="w-3 h-3 bg-yellow-500"></div>
					<span class="text-[#888]">30-70%: Moderate overfitting</span>
				</div>
				<div class="flex items-center gap-2">
					<div class="w-3 h-3 bg-red-500"></div>
					<span class="text-[#888]">&lt;30%: High overfitting (avoid)</span>
				</div>
			</div>

			<!-- Stats -->
			<div class="text-right space-y-1">
				<div>
					<span class="text-[#666] text-xs">Avg Train:</span>
					<span class="text-white font-mono ml-2">{(metrics.avg_train_metric || 0).toFixed(3)}</span>
				</div>
				<div>
					<span class="text-[#666] text-xs">Avg Test:</span>
					<span class="{(metrics.avg_test_metric || 0) > 0 ? 'text-green-400' : 'text-red-400'} font-mono ml-2">
						{(metrics.avg_test_metric || 0).toFixed(3)}
					</span>
				</div>
				<div>
					<span class="text-[#666] text-xs">Degradation:</span>
					<span class="text-yellow-400 font-mono ml-2">
						{(((metrics.avg_train_metric || 0) - (metrics.avg_test_metric || 0)) / Math.abs(metrics.avg_train_metric || 1) * 100).toFixed(1)}%
					</span>
				</div>
			</div>
		</div>
	</div>

	<!-- 2. TIMELINE WITH PERFORMANCE -->
	<div class="terminal-card p-4">
		<h4 class="text-[10px] font-bold text-white uppercase tracking-widest mb-4">Fold Timeline & Performance</h4>

		<div class="space-y-3">
			{#each folds as fold, index}
				{@const testMetric = getTestMetric(fold)}
				{@const trainMetric = getTrainMetric(fold)}
				<div class="flex items-center gap-3">
					<span class="text-[#888] text-xs w-14 font-mono">Fold {index + 1}</span>

					<!-- Timeline bar -->
					<div class="flex-1 h-8 bg-[#111] relative overflow-hidden">
						<!-- Train segment -->
						<div
							class="absolute h-full bg-white/20 flex items-center justify-center text-xs text-white/80"
							style="left: {getPosition(fold.train_start)}%; width: {Math.max(getPosition(fold.train_end) - getPosition(fold.train_start), 1)}%;"
						>
							{#if getPosition(fold.train_end) - getPosition(fold.train_start) > 15}
								<span class="font-mono">{trainMetric.toFixed(2)}</span>
							{/if}
						</div>
						<!-- Test segment with performance color -->
						<div
							class="absolute h-full flex items-center justify-center text-xs text-white font-semibold {getPerformanceColor(testMetric)}"
							style="left: {getPosition(fold.test_start)}%; width: {Math.max(getPosition(fold.test_end) - getPosition(fold.test_start), 1)}%;"
						>
							{#if getPosition(fold.test_end) - getPosition(fold.test_start) > 10}
								<span class="font-mono">{testMetric.toFixed(2)}</span>
							{/if}
						</div>
					</div>

					<!-- Performance indicator -->
					<div class="w-16 text-right">
						<span class="text-xs font-mono {testMetric > 0 ? 'text-green-400' : 'text-red-400'}">
							{testMetric > 0 ? '+' : ''}{testMetric.toFixed(2)}
						</span>
					</div>
				</div>
			{/each}
		</div>

		<!-- Timeline legend -->
		<div class="flex justify-between text-xs text-[#666] mt-3 px-14">
			<span>{formatDate(totalStart)}</span>
			<span>{formatDate(totalEnd)}</span>
		</div>

		<div class="flex gap-6 mt-4 text-xs justify-center">
			<div class="flex items-center gap-2">
				<div class="w-4 h-3 bg-white/20"></div>
				<span class="text-[#888]">Training (In-Sample)</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-4 h-3 bg-emerald-500"></div>
				<span class="text-[#888]">Test Profit</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-4 h-3 bg-red-500"></div>
				<span class="text-[#888]">Test Loss</span>
			</div>
		</div>
	</div>

	<!-- 3. TRAIN VS TEST BAR CHART -->
	<div class="terminal-card p-4">
		<h4 class="text-[10px] font-bold text-white uppercase tracking-widest mb-4">Train vs Test Performance by Fold</h4>

		<div class="flex items-end gap-2 h-48">
			{#each folds as fold, index}
				{@const testMetric = getTestMetric(fold)}
				{@const trainMetric = getTrainMetric(fold)}
				<div class="flex-1 flex flex-col items-center gap-1">
					<!-- Bars container -->
					<div class="w-full flex gap-1 items-end h-36">
						<!-- Train bar -->
						<div class="flex-1 flex flex-col justify-end">
							{#if trainMetric >= 0}
								<div
									class="w-full bg-white/40 transition-all"
									style="height: {(trainMetric / maxMetric) * 100}%;"
									title="Train: {trainMetric.toFixed(3)}"
								></div>
							{:else}
								<div class="w-full h-0"></div>
							{/if}
						</div>
						<!-- Test bar -->
						<div class="flex-1 flex flex-col justify-end">
							{#if testMetric >= 0}
								<div
									class="w-full bg-emerald-500 transition-all"
									style="height: {(testMetric / maxMetric) * 100}%;"
									title="Test: {testMetric.toFixed(3)}"
								></div>
							{:else}
								<div class="w-full h-0"></div>
							{/if}
						</div>
					</div>
					<!-- Zero line for negative values -->
					<div class="w-full h-px bg-[#333]"></div>
					<!-- Negative bars -->
					<div class="w-full flex gap-1 h-8">
						<div class="flex-1">
							{#if trainMetric < 0}
								<div
									class="w-full bg-white/20 transition-all"
									style="height: {(Math.abs(trainMetric) / maxMetric) * 100}%;"
									title="Train: {trainMetric.toFixed(3)}"
								></div>
							{/if}
						</div>
						<div class="flex-1">
							{#if testMetric < 0}
								<div
									class="w-full bg-red-500 transition-all"
									style="height: {(Math.abs(testMetric) / maxMetric) * 100}%;"
									title="Test: {testMetric.toFixed(3)}"
								></div>
							{/if}
						</div>
					</div>
					<!-- Label -->
					<span class="text-xs text-[#888] font-mono">F{index + 1}</span>
				</div>
			{/each}
		</div>

		<div class="flex gap-6 mt-4 text-xs justify-center">
			<div class="flex items-center gap-2">
				<div class="w-4 h-3 bg-white/40"></div>
				<span class="text-[#888]">Train (In-Sample)</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-4 h-3 bg-emerald-500"></div>
				<span class="text-[#888]">Test (Out-of-Sample)</span>
			</div>
		</div>
	</div>

	<!-- 4. PARAMETER STABILITY HEATMAP -->
	{#if allParams.length > 0}
		<div class="terminal-card p-4">
			<h4 class="text-[10px] font-bold text-white uppercase tracking-widest mb-4">Parameter Stability Across Folds</h4>
			<p class="text-xs text-[#666] mb-3">Consistent parameters across folds indicate robustness. High variance suggests overfitting.</p>

			<div class="overflow-x-auto">
				<table class="w-full text-xs">
					<thead>
						<tr class="text-[#666]">
							<th class="text-left py-2 pr-4 font-normal">Parameter</th>
							{#each folds as _, index}
								<th class="text-center py-2 px-2 font-normal">F{index + 1}</th>
							{/each}
							<th class="text-center py-2 px-2 font-normal">Variance</th>
						</tr>
					</thead>
					<tbody>
						{#each allParams as param}
							{@const values = getParamValues(param)}
							{@const numValues = values.filter(v => typeof v === 'number')}
							{@const variance = calculateVariance(numValues)}
							{@const range = getParamRange(param)}
							{@const normalizedVariance = range.max !== range.min ? variance / Math.pow(range.max - range.min, 2) : 0}
							<tr class="border-t border-[#1a1a1a]">
								<td class="py-2 pr-4 text-[#888] font-mono">{param}</td>
								{#each folds as fold}
									{@const value = fold.best_params?.[param]}
									<td class="py-2 px-1 text-center">
										<span
											class="inline-block px-2 py-1 text-white font-mono"
											style="background-color: {getParamHeatColor(param, value)};"
										>
											{typeof value === 'number' ? value.toFixed(value % 1 === 0 ? 0 : 2) : value ?? '-'}
										</span>
									</td>
								{/each}
								<td class="py-2 px-2 text-center">
									<span class="inline-block px-2 py-1 text-xs font-mono {normalizedVariance < 0.1 ? 'border border-emerald-900 bg-emerald-500/10 text-emerald-400' : normalizedVariance < 0.3 ? 'border border-yellow-900 bg-yellow-500/10 text-yellow-400' : 'border border-red-900 bg-red-500/10 text-red-400'}">
										{normalizedVariance < 0.1 ? 'Low' : normalizedVariance < 0.3 ? 'Med' : 'High'}
									</span>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<!-- Most robust params -->
			{#if metrics.most_robust_params && Object.keys(metrics.most_robust_params).length > 0}
				<div class="mt-4 p-3 bg-[#111] border border-[#1a1a1a]">
					<p class="text-xs text-[#888] mb-2">Recommended Parameters (most consistent across folds):</p>
					<div class="flex flex-wrap gap-2">
						{#each Object.entries(metrics.most_robust_params) as [key, value]}
							<span class="border border-[#333] bg-[#050505] text-[#888] px-2 py-1 text-sm font-mono">
								{key}={value}
							</span>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- 5. FOLD DETAILS SUMMARY -->
	<div class="terminal-card p-4">
		<h4 class="text-[10px] font-bold text-white uppercase tracking-widest mb-4">Fold Performance Summary</h4>

		<div class="grid grid-cols-5 gap-2 text-center">
			{#each folds as fold, index}
				{@const testMetric = getTestMetric(fold)}
				{@const testReturn = fold.test_metrics?.total_return ?? 0}
				{@const testTrades = fold.test_metrics?.total_trades ?? 0}
				<div class="bg-[#050505] p-3 {testMetric > 0 ? 'border border-emerald-900' : testMetric < 0 ? 'border border-red-900' : 'border border-[#333]'}">
					<div class="text-xs text-[#666] mb-1">Fold {index + 1}</div>
					<div class="text-lg font-bold font-mono {testMetric > 0 ? 'text-emerald-400' : testMetric < 0 ? 'text-red-400' : 'text-[#888]'}">
						{testMetric.toFixed(2)}
					</div>
					<div class="text-xs text-[#666] mt-1">
						{testReturn.toFixed(1)}% | {testTrades} trades
					</div>
				</div>
			{/each}
		</div>
	</div>
</div>
