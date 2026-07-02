<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import MonteCarloChart from './MonteCarloChart.svelte';
	import OptimizationChart from './OptimizationChart.svelte';
	import WalkForwardChart from './WalkForwardChart.svelte';
	import TradeScatterChart from './TradeScatterChart.svelte';
	import StrategyScorecard from './StrategyScorecard.svelte';
	import type { Scorecard, BacktestResult, Trade, RobustnessAnalysis, RegimePerformance, TrialSummary, OHLCVBar } from '$lib/api';
	import { getOHLCV } from '$lib/api';

	export let mode: 'backtest' | 'optimize' | 'walk-forward' | 'robustness' | 'scorecard' = 'backtest';
	export let status: 'idle' | 'running' | 'completed' | 'failed' = 'idle';
	export let logs: string[] = [];
	export let result: BacktestResult | null = null;
	export let robustness: RobustnessAnalysis | null = null;
	export let scorecard: Scorecard | null = null;
	export let progress: string | null = null;

	const dispatch = createEventDispatcher<{
		backtestParams: { params: Record<string, unknown> };
	}>();

	function runBacktestWithParams(params: Record<string, unknown>) {
		dispatch('backtestParams', { params });
	}

	let perfByRegimeEntries: Array<[string, RegimePerformance]> = [];
	$: perfByRegimeEntries =
		robustness?.regimes?.performance_by_regime
			? (Object.entries(robustness.regimes.performance_by_regime) as Array<
					[string, RegimePerformance]
				>)
			: [];

	function formatCurrency(val: number) {
		return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
	}

	function formatPercent(val: number) {
		return (val).toFixed(2) + '%';
	}

	function formatNumber(val: number) {
		return new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 }).format(val);
	}

	function inferMonthsFromBacktest(r: BacktestResult): number | null {
		const metricMonths = r.metrics?.backtest_months;
		if (typeof metricMonths === 'number' && Number.isFinite(metricMonths) && metricMonths > 0) {
			return Math.max(1, Math.round(metricMonths));
		}
		const start = (r.config?.start as string | undefined) ?? r.equity_curve?.[0]?.timestamp;
		const end =
			(r.config?.end as string | undefined) ??
			(r.equity_curve && r.equity_curve.length > 0 ? r.equity_curve[r.equity_curve.length - 1]?.timestamp : undefined);
		if (!start || !end) return null;
		const startMs = new Date(start).getTime();
		const endMs = new Date(end).getTime();
		if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null;
		return Math.max(1, Math.round((endMs - startMs) / (1000 * 60 * 60 * 24 * 30.4375)));
	}

	function normalizedAnnualReturn(r: BacktestResult): number {
		const explicit = r.metrics?.annualized_return_pct;
		if (typeof explicit === 'number' && Number.isFinite(explicit)) return explicit;
		const total = r.metrics?.total_return ?? 0;
		const months = inferMonthsFromBacktest(r);
		if (months && months > 0) {
			const growth = 1 + total / 100;
			if (growth > 0) return (Math.pow(growth, 12 / months) - 1) * 100;
		}
		return total;
	}

	function calculateAvgTrade(trades: Trade[]) {
		if (!trades || trades.length === 0) return '-';
		const sum = trades.reduce((acc: number, t: Trade) => acc + t.return_pct, 0);
		const avg = (sum / trades.length) * 100;
		return formatPercent(avg);
	}

	let showAllTrades = false;
	let candleBars: OHLCVBar[] | null = null;
	let candleFetchedFor: string = '';

	$: if (result && status === 'completed' && result.symbol && result.timeframe) {
		const fetchKey = `${result.symbol}:${result.timeframe}:${result.id}`;
		if (fetchKey !== candleFetchedFor) {
			candleFetchedFor = fetchKey;
			fetchCandleData(result.symbol, result.timeframe);
		}
	}

	async function fetchCandleData(symbol: string, timeframe: string) {
		try {
			const bars = await getOHLCV(symbol, timeframe, 5000);
			candleBars = bars;
		} catch {
			candleBars = null;
		}
	}

	function isOptimizationResult(r: BacktestResult | null): boolean {
		return Boolean(r?.config?.optimization || r?.result_type === 'optimization' || r?.metrics?.best_params);
	}

	function isWalkForwardResult(r: BacktestResult | null): boolean {
		return Boolean(r?.config?.walk_forward || r?.result_type === 'walk_forward' || r?.config?.folds);
	}

	function hasMAEMFEData(trades: Trade[]): boolean {
		if (!trades || trades.length === 0) return false;
		return trades.some((t: Trade) => t.mae !== undefined || t.mfe !== undefined);
	}

	interface ConfigFold {
		fold_index: number;
		train_start: string;
		train_end: string;
		test_start: string;
		test_end: string;
		train_metric: number;
		test_metric: number;
		best_params?: Record<string, unknown>;
	}

	/** Safely extract folds array from result config */
	function getFolds(r: BacktestResult | null): ConfigFold[] {
		const folds = r?.config?.folds;
		if (Array.isArray(folds)) return folds as ConfigFold[];
		return [];
	}

	/** Safely extract trials from metrics */
	function getTrials(r: BacktestResult | null): TrialSummary[] {
		const trials = r?.metrics?.trials_summary;
		if (Array.isArray(trials)) return trials as TrialSummary[];
		return [];
	}

	/** Return a Tailwind color class based on metric quality: green=good, yellow=ok, red=bad */
	function metricColor(metric: string, value: number | null | undefined): string {
		if (value == null || isNaN(value)) return 'text-[#666]';
		switch (metric) {
			case 'total_return':
			case 'cagr':
				return value > 20 ? 'text-green-500' : value > 0 ? 'text-yellow-500' : 'text-red-500';
			case 'sharpe_ratio':
				return value > 1.0 ? 'text-green-500' : value > 0.5 ? 'text-yellow-500' : 'text-red-500';
			case 'sortino_ratio':
				return value > 1.5 ? 'text-green-500' : value > 0.5 ? 'text-yellow-500' : 'text-red-500';
			case 'max_drawdown': {
				const dd = Math.abs(value);
				return dd < 20 ? 'text-green-500' : dd < 40 ? 'text-yellow-500' : 'text-red-500';
			}
			case 'win_rate':
				return value > 50 ? 'text-green-500' : value > 35 ? 'text-yellow-500' : 'text-red-500';
			case 'profit_factor':
				return value > 1.5 ? 'text-green-500' : value > 1.0 ? 'text-yellow-500' : 'text-red-500';
			case 'expectancy':
			case 'avg_trade':
				return value > 0 ? 'text-green-500' : value === 0 ? 'text-yellow-500' : 'text-red-500';
			case 'recovery_factor':
				return value > 3 ? 'text-green-500' : value > 1 ? 'text-yellow-500' : 'text-red-500';
			case 'total_trades':
				return value >= 30 ? 'text-green-500' : value >= 10 ? 'text-yellow-500' : 'text-red-500';
			default:
				return 'text-white';
		}
	}
</script>

<div class="h-full flex flex-col bg-black font-mono text-sm overflow-hidden">
	{#if status === 'idle'}
		<div class="flex-1 flex flex-col items-center justify-center text-[#555] space-y-2">
			<svg class="w-12 h-12 opacity-20" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
			</svg>
			<span class="tracking-widest uppercase text-xs">Ready to Simulate</span>
		</div>
	{:else if status === 'running'}
		<div class="flex-1 p-4 overflow-y-auto space-y-1 font-mono text-sm">
			{#each logs as log}
				<div class="text-[#888] border-l-2 border-[#222] pl-2 py-0.5">{log}</div>
			{/each}
			{#if progress}
				<div class="text-emerald-400 pl-2 font-bold">> {progress}</div>
			{/if}
			<div class="animate-pulse text-green-500 pl-2">> PROCESSING...</div>
		</div>
	{:else if status === 'failed'}
		<div class="flex-1 p-6 flex flex-col items-center justify-center">
			<div class="border border-red-900 bg-red-500/5 p-6 max-w-lg text-center">
				<h3 class="text-red-400 font-bold uppercase tracking-widest mb-2">SIMULATION FAILED</h3>
				<div class="text-red-400 space-y-1">
					{#each logs.slice(-3) as log}
						<div>{log}</div>
					{/each}
				</div>
			</div>
		</div>
	{:else if status === 'completed'}
		<div class="flex-1 overflow-y-auto p-6 space-y-6">
			{#if mode === 'scorecard'}
				{#if scorecard}
					<StrategyScorecard {scorecard} />
				{:else}
					<div class="flex h-full items-center justify-center text-[#555]">
						Select a run in History, then generate Scorecard.
					</div>
				{/if}
			{:else if mode === 'robustness'}
				{#if robustness}
					<!-- Monte Carlo -->
					<div class="terminal-card p-4">
						<h3 class="text-sm font-bold text-white uppercase tracking-wider mb-4">Monte Carlo Simulation</h3>
						{#if robustness.monte_carlo}
							<div class="grid grid-cols-3 gap-4 mb-4">
								<div class="terminal-card p-4">
									<div class="text-xs uppercase text-[#666] mb-1">Prob. Profit</div>
									<div class="text-2xl font-bold text-white">
										{formatPercent((robustness.monte_carlo.probability_of_profit || 0) * 100)}
									</div>
								</div>
								<div class="terminal-card p-4">
									<div class="text-xs uppercase text-[#666] mb-1">Prob. Ruin</div>
									<div class="text-2xl font-bold text-white">
										{formatPercent((robustness.monte_carlo.probability_of_ruin || 0) * 100)}
									</div>
								</div>
								<div class="terminal-card p-4">
									<div class="text-xs uppercase text-[#666] mb-1">Avg Max DD</div>
									<div class="text-2xl font-bold text-red-500">
										{formatPercent(robustness.monte_carlo.avg_max_drawdown || 0)}
									</div>
								</div>
							</div>
							
							<!-- Monte Carlo Fan Chart -->
							{#if robustness.monte_carlo.equity_paths_sample && robustness.monte_carlo.equity_paths_sample.length > 0}
								<div class="mt-4">
									<h4 class="text-xs font-bold text-[#666] uppercase tracking-wider mb-2">Equity Path Distribution</h4>
									<MonteCarloChart 
										equityPaths={robustness.monte_carlo.equity_paths_sample}
										height={220}
									/>
								</div>
							{/if}
						{:else if robustness.monte_carlo_note}
							<div class="text-[#888]">{robustness.monte_carlo_note}</div>
						{:else if robustness.monte_carlo_error}
							<div class="text-red-400">{robustness.monte_carlo_error}</div>
						{:else}
							<div class="text-[#555]">No Monte Carlo results.</div>
						{/if}
					</div>

					<!-- Regimes -->
					{#if robustness.regimes}
						<div class="terminal-card p-4">
							<h3 class="text-base font-bold text-white uppercase tracking-wider mb-4">Regimes</h3>
							<div class="text-sm text-[#888]">
								Current: <span class="text-white">{robustness.regimes.current_regime ?? 'unknown'}</span>
							</div>
							{#if robustness.regimes.performance_by_regime}
								<div class="mt-4 overflow-x-auto">
									<table class="w-full text-left border-collapse">
										<thead>
											<tr class="text-xs text-[#666] uppercase border-b border-[#222]">
												<th class="p-2 font-medium">Regime</th>
												<th class="p-2 font-medium text-right">Return</th>
												<th class="p-2 font-medium text-right">Sharpe</th>
												<th class="p-2 font-medium text-right">Max DD</th>
												<th class="p-2 font-medium text-right">Pct Time</th>
											</tr>
										</thead>
										<tbody class="text-sm font-mono">
											{#each perfByRegimeEntries as [regime, perf]}
												<tr class="border-b border-[#111] hover:bg-[#111] transition-colors">
													<td class="p-2 text-[#888]">{regime}</td>
													<td class="p-2 text-right" class:text-green-500={(perf.total_return_pct ?? 0) > 0} class:text-red-500={(perf.total_return_pct ?? 0) < 0}>
														{formatPercent(perf.total_return_pct ?? 0)}
													</td>
													<td class="p-2 text-right text-[#aaa]">{formatNumber(perf.sharpe_ratio ?? 0)}</td>
													<td class="p-2 text-right text-red-500">{formatPercent(perf.max_drawdown_pct ?? 0)}</td>
													<td class="p-2 text-right text-[#888]">{formatPercent(perf.pct_of_total ?? 0)}</td>
												</tr>
											{/each}
										</tbody>
									</table>
								</div>
							{:else}
								<div class="text-[#555] mt-2">No regime performance data.</div>
							{/if}
						</div>
					{:else if robustness.regime_error}
						<div class="terminal-card p-4">
							<h3 class="text-sm font-bold text-white uppercase tracking-wider mb-2">Regimes</h3>
							<div class="text-red-400">{robustness.regime_error}</div>
						</div>
					{/if}
				{:else}
					<div class="flex h-full items-center justify-center text-[#555]">
						Select a run in History, then run Robustness.
					</div>
				{/if}
			{:else if !result}
				<div class="flex h-full items-center justify-center text-[#555]">
					Run a simulation to see results.
				</div>
		{:else if mode === 'optimize' || isOptimizationResult(result)}
			{@const m = result.metrics}
			<div class="terminal-card p-4">
				<div class="flex justify-between items-center mb-4">
					<h3 class="text-base font-bold text-white uppercase tracking-wider">Optimization</h3>
					{#if m.best_params}
						<button 
							class="terminal-button-primary text-sm"
							on:click={() => { if (m.best_params) runBacktestWithParams(m.best_params); }}
						>
							Gauntlet Best Params
						</button>
					{/if}
				</div>
					<div class="grid grid-cols-3 gap-4">
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Objective</div>
							<div class="text-white font-mono text-base">{m.objective ?? '-'}</div>
						</div>
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Best Value</div>
							<div class="text-3xl font-bold text-white">{m.best_value !== undefined ? formatNumber(m.best_value) : '-'}</div>
						</div>
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Trials</div>
							<div class="text-3xl font-bold text-white">{m.n_trials ?? '-'}</div>
						</div>
					</div>

					{#if m.best_params}
						<div class="mt-6">
							<h4 class="text-xs font-bold text-[#666] uppercase tracking-wider mb-2">Best Params</h4>
							<div class="grid grid-cols-2 gap-2">
								{#each Object.entries(m.best_params) as [k, v]}
									<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#222] px-3 py-2">
										<span class="text-xs text-[#666] uppercase">{k}</span>
										<span class="text-white font-mono text-sm">{v}</span>
									</div>
								{/each}
							</div>
						</div>
					{:else}
						<div class="text-[#555] mt-4">No best params returned.</div>
					{/if}

				<!-- Optimization Convergence Chart -->
				{#if getTrials(result).length > 0}
					<div class="mt-6">
						<OptimizationChart 
							trials={getTrials(result)}
							objective={String(m.objective || 'sharpe_ratio')}
							height={180}
						/>
					</div>
				{/if}
			</div>
		{:else if mode === 'walk-forward' || isWalkForwardResult(result)}
			{@const m = result.metrics}
			{@const folds = getFolds(result)}
				<div class="terminal-card p-4">
					<div class="flex justify-between items-center mb-4">
						<h3 class="text-base font-bold text-white uppercase tracking-wider">Walk-Forward</h3>
						{#if m.most_robust_params}
							<button 
								class="terminal-button-primary text-sm"
								on:click={() => { if (m.most_robust_params) runBacktestWithParams(m.most_robust_params); }}
							>
								Gauntlet Robust Params
							</button>
						{/if}
					</div>
					<div class="grid grid-cols-4 gap-4">
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Train Avg</div>
							<div class="text-white font-mono text-base">{m.avg_train_metric !== undefined ? formatNumber(Number(m.avg_train_metric)) : '-'}</div>
						</div>
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Test Avg</div>
							<div class="text-white font-mono text-base">{m.avg_test_metric !== undefined ? formatNumber(Number(m.avg_test_metric)) : '-'}</div>
						</div>
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Overfit Ratio</div>
							<div class="text-white font-mono text-base">{m.overfitting_ratio !== undefined ? formatNumber(Number(m.overfitting_ratio)) : '-'}</div>
						</div>
						<div class="terminal-card p-4">
							<div class="text-xs uppercase text-[#666] mb-1">Folds</div>
							<div class="text-white font-mono text-base">{Array.isArray(m.n_folds) ? m.n_folds.length : folds.length || '-'}</div>
						</div>
					</div>

					{#if m.most_robust_params}
						<div class="mt-6">
							<h4 class="text-xs font-bold text-[#666] uppercase tracking-wider mb-2">Most Robust Params</h4>
							<div class="grid grid-cols-2 gap-2">
								{#each Object.entries(m.most_robust_params) as [k, v]}
									<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#222] px-3 py-2">
										<span class="text-xs text-[#666] uppercase">{k}</span>
										<span class="text-white font-mono text-sm">{v}</span>
									</div>
								{/each}
							</div>
						</div>
					{/if}

					<!-- Walk-Forward Fold Visualization -->
					{#if folds.length > 0}
						<div class="mt-6">
							<h4 class="text-xs font-bold text-[#666] uppercase tracking-wider mb-2">Train vs Test Performance</h4>
							<WalkForwardChart {folds} height={200} />
						</div>
					{/if}

					{#if folds.length > 0}
						<div class="mt-6 overflow-x-auto">
							<h4 class="text-xs font-bold text-[#666] uppercase tracking-wider mb-2">Folds</h4>
							<table class="w-full text-left border-collapse">
								<thead>
									<tr class="text-xs text-[#666] uppercase border-b border-[#222]">
										<th class="p-2 font-medium">Fold</th>
										<th class="p-2 font-medium">Train</th>
										<th class="p-2 font-medium">Test</th>
										<th class="p-2 font-medium text-right">Train Metric</th>
										<th class="p-2 font-medium text-right">Test Metric</th>
									</tr>
								</thead>
								<tbody class="text-sm font-mono">
									{#each folds as fold}
										<tr class="border-b border-[#111] hover:bg-[#111] transition-colors">
											<td class="p-2 text-[#888]">{fold.fold_index + 1}</td>
											<td class="p-2 text-[#888] text-xs">
												{new Date(fold.train_start).toLocaleDateString()} - {new Date(fold.train_end).toLocaleDateString()}
											</td>
											<td class="p-2 text-[#888] text-xs">
												{new Date(fold.test_start).toLocaleDateString()} - {new Date(fold.test_end).toLocaleDateString()}
											</td>
											<td class="p-2 text-right text-[#aaa]">{formatNumber(fold.train_metric ?? 0)}</td>
											<td class="p-2 text-right text-[#aaa]">{formatNumber(fold.test_metric ?? 0)}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
				</div>
			{:else}
				<!-- Backtest Result (default) -->

				<!-- Warnings -->
				{#if result.config?.warnings && result.config.warnings.length > 0}
					<div class="border-l-2 border-yellow-900 bg-yellow-500/5 p-4 mb-4">
						<h3 class="text-[10px] font-bold text-yellow-400 uppercase tracking-widest mb-2">Warnings</h3>
						<ul class="list-disc list-inside text-xs text-yellow-400 space-y-1">
							{#each result.config.warnings as warning}
								<li>{warning}</li>
							{/each}
						</ul>
					</div>
				{/if}

				<!-- Header Metrics -->
				<div class="grid grid-cols-4 gap-4">
					<div class="terminal-card p-4">
						<div class="text-xs uppercase text-[#666] mb-1">Annual Return</div>
						<div class="text-3xl font-bold {metricColor('total_return', normalizedAnnualReturn(result))}">
							{formatPercent(normalizedAnnualReturn(result))}
						</div>
					</div>
					<div class="terminal-card p-4">
						<div class="text-xs uppercase text-[#666] mb-1">Sharpe Ratio</div>
						<div class="text-3xl font-bold {metricColor('sharpe_ratio', result.metrics.sharpe_ratio)}">
							{result.metrics.sharpe_ratio.toFixed(2)}
						</div>
					</div>
					<div class="terminal-card p-4">
						<div class="text-xs uppercase text-[#666] mb-1">Max Drawdown</div>
						<div class="text-3xl font-bold {metricColor('max_drawdown', result.metrics.max_drawdown)}">
							{formatPercent(result.metrics.max_drawdown)}
						</div>
					</div>
					<div class="terminal-card p-4">
						<div class="text-xs uppercase text-[#666] mb-1">Win Rate</div>
						<div class="text-3xl font-bold {metricColor('win_rate', result.metrics.win_rate)}">
							{formatPercent(result.metrics.win_rate)}
						</div>
					</div>
				</div>

				<!-- Equity Curve -->
				<div class="terminal-card p-4">
					<div class="flex justify-between items-center mb-4">
						<h3 class="text-base font-bold text-white uppercase tracking-wider">Equity Curve</h3>
						<div class="flex items-center gap-4 text-xs">
							<span class="flex items-center gap-1"><span class="w-4 h-0.5 bg-cyan-400"></span> Strategy</span>
							<span class="flex items-center gap-1"><span class="w-4 h-0.5 bg-amber-400 opacity-70" style="border-top: 1px dashed"></span> Buy &amp; Hold</span>
							{#if candleBars}<span class="flex items-center gap-1"><span class="w-3 h-3 bg-green-500/50 border border-green-500/60"></span> Price</span>{/if}
						</div>
					</div>
					{#if result.equity_curve}
						<EquityChart
							data={result.equity_curve}
							benchmarkData={result.benchmark_curve || null}
							candleData={candleBars}
							showDrawdown={true}
							height={350}
						/>
					{:else}
						<div class="h-64 flex items-center justify-center text-[#555]">NO CHART DATA</div>
					{/if}
				</div>

				<!-- Trade Quality Scatter (MAE/MFE) -->
				{#if result.trades && hasMAEMFEData(result.trades)}
					<div class="terminal-card p-4">
						<TradeScatterChart trades={result.trades} height={200} />
					</div>
				{/if}

				<!-- Parameters -->
				{#if result.config?.params && Object.keys(result.config.params).length > 0}
					<div class="terminal-card p-4">
						<h3 class="text-base font-bold text-white uppercase tracking-wider mb-4">Parameters</h3>
						<div class="grid grid-cols-2 md:grid-cols-4 gap-2">
							{#each Object.entries(result.config.params) as [k, v]}
								<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#222] px-3 py-2">
									<span class="text-xs text-[#666] uppercase">{k}</span>
									<span class="text-white font-mono text-sm">{v}</span>
								</div>
							{/each}
						</div>
						{#if result.config.initial_capital || result.config.fee_bps !== undefined || result.config.slippage_bps !== undefined}
							<div class="grid grid-cols-2 md:grid-cols-4 gap-2 mt-2">
								{#if result.config.initial_capital}
									<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#181818] px-3 py-2">
										<span class="text-xs text-[#555]">Capital</span>
										<span class="text-[#888] font-mono text-sm">${result.config.initial_capital.toLocaleString()}</span>
									</div>
								{/if}
								{#if result.config.fee_bps !== undefined}
									<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#181818] px-3 py-2">
										<span class="text-xs text-[#555]">Fees</span>
										<span class="text-[#888] font-mono text-sm">{result.config.fee_bps} bps</span>
									</div>
								{/if}
								{#if result.config.slippage_bps !== undefined}
									<div class="flex justify-between items-center bg-[#0a0a0a] border border-[#181818] px-3 py-2">
										<span class="text-xs text-[#555]">Slippage</span>
										<span class="text-[#888] font-mono text-sm">{result.config.slippage_bps} bps</span>
									</div>
								{/if}
							</div>
						{/if}
					</div>
				{/if}

				<!-- Detailed Stats -->
				<div class="terminal-card p-4">
					<h3 class="text-base font-bold text-white uppercase tracking-wider mb-4">Statistics</h3>
					<div class="grid grid-cols-2 md:grid-cols-4 gap-y-4 gap-x-8">
						<div>
							<div class="text-xs text-[#666] uppercase">Trades</div>
							<div class="font-mono text-sm {metricColor('total_trades', result.metrics.total_trades)}">{result.metrics.total_trades}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Profit Factor</div>
							<div class="font-mono text-sm {metricColor('profit_factor', result.metrics.profit_factor ?? null)}">{result.metrics.profit_factor?.toFixed(2) ?? '-'}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Expectancy</div>
							<div class="font-mono text-sm {metricColor('expectancy', result.metrics.expectancy ?? null)}">{result.metrics.expectancy ? formatCurrency(result.metrics.expectancy) : '-'}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Sortino</div>
							<div class="font-mono text-sm {metricColor('sortino_ratio', result.metrics.sortino_ratio ?? null)}">{result.metrics.sortino_ratio?.toFixed(2) ?? '-'}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">CAGR</div>
							<div class="font-mono text-sm {metricColor('cagr', result.metrics.cagr ?? null)}">{result.metrics.cagr ? formatPercent(result.metrics.cagr) : '-'}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Vol (Ann)</div>
							<div class="text-white font-mono text-sm">{(result.metrics.max_drawdown / 2).toFixed(2)}%</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Avg Trade</div>
							<div class="font-mono text-sm {metricColor('avg_trade', result.trades && result.trades.length > 0 ? result.trades.reduce((a, t) => a + t.return_pct, 0) / result.trades.length : null)}">{calculateAvgTrade(result.trades || [])}</div>
						</div>
						<div>
							<div class="text-xs text-[#666] uppercase">Recovery Factor</div>
							<div class="font-mono text-sm {metricColor('recovery_factor', result.metrics.recovery_factor ?? null)}">{result.metrics.recovery_factor?.toFixed(2) ?? '-'}</div>
						</div>
					</div>
				</div>

				<!-- Trades Table -->
				{#if result.trades && result.trades.length > 0}
					{@const feeBps = result.config?.fee_bps ?? 4.5}
					{@const allTradesReversed = [...result.trades].reverse()}
					{@const tradesShown = showAllTrades ? allTradesReversed : allTradesReversed.slice(0, 50)}
					<div class="terminal-card">
						<div class="p-3 border-b border-[#222] flex justify-between items-center">
							<h3 class="text-base font-bold text-white uppercase tracking-wider">Trades</h3>
							<div class="flex items-center gap-3">
								<span class="text-xs text-[#666]">{result.trades.length} total</span>
								{#if result.trades.length > 50}
									<button
										class="text-xs text-[#888] hover:text-white transition-colors"
										on:click={() => showAllTrades = !showAllTrades}
									>
										{showAllTrades ? 'Show Recent' : 'Show All'}
									</button>
								{/if}
							</div>
						</div>
						<div class="overflow-x-auto">
							<table class="w-full text-left border-collapse">
								<thead>
									<tr class="text-[11px] text-[#666] uppercase border-b border-[#222] sticky top-0 bg-[#0a0a0a]">
										<th class="px-2 py-2 font-medium text-center">#</th>
										<th class="px-2 py-2 font-medium">Side</th>
										<th class="px-2 py-2 font-medium">Entry Time</th>
										<th class="px-2 py-2 font-medium text-right">Entry</th>
										<th class="px-2 py-2 font-medium">Exit Time</th>
										<th class="px-2 py-2 font-medium text-right">Exit</th>
										<th class="px-2 py-2 font-medium text-right">Size</th>
										<th class="px-2 py-2 font-medium text-right">Duration</th>
										<th class="px-2 py-2 font-medium text-right">Fees</th>
										<th class="px-2 py-2 font-medium text-right">PnL</th>
										<th class="px-2 py-2 font-medium text-right">Return</th>
										<th class="px-2 py-2 font-medium text-right">Cum. PnL</th>
									</tr>
								</thead>
								<tbody class="text-xs font-mono">
									{#each tradesShown as trade, i}
										{@const tradeIdx = result.trades.length - i}
										{@const notional = (trade.entry_price * trade.size + trade.exit_price * trade.size)}
										{@const estFees = notional * feeBps / 10000}
										{@const cumPnl = result.trades.slice(0, tradeIdx).reduce((sum, t) => sum + t.pnl, 0)}
										{@const duration = (() => {
											if (trade.bars_held != null) return `${trade.bars_held} bars`;
											const ms = new Date(trade.exit_time).getTime() - new Date(trade.entry_time).getTime();
											if (ms < 0) return '-';
											const hours = Math.round(ms / 3600000);
											if (hours < 24) return `${hours}h`;
											const days = Math.round(hours / 24);
											return `${days}d`;
										})()}
										<tr class="border-b border-[#111] hover:bg-[#111] transition-colors">
											<td class="px-2 py-1.5 text-center text-[#555]">{tradeIdx}</td>
											<td class="px-2 py-1.5">
												<span class="px-1.5 py-0.5 text-[10px] font-bold uppercase {(trade.direction || 'long') === 'long' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-900' : 'bg-red-500/10 text-red-400 border border-red-900'}">
													{(trade.direction || 'long') === 'long' ? 'LONG' : 'SHORT'}
												</span>
											</td>
											<td class="px-2 py-1.5 text-[#888] whitespace-nowrap">{new Date(trade.entry_time).toLocaleString()}</td>
											<td class="px-2 py-1.5 text-right text-[#888]">{trade.entry_price.toFixed(2)}</td>
											<td class="px-2 py-1.5 text-[#888] whitespace-nowrap">{new Date(trade.exit_time).toLocaleString()}</td>
											<td class="px-2 py-1.5 text-right text-[#888]">{trade.exit_price.toFixed(2)}</td>
											<td class="px-2 py-1.5 text-right text-[#888]">{trade.size.toFixed(4)}</td>
											<td class="px-2 py-1.5 text-right text-[#666]">{duration}</td>
											<td class="px-2 py-1.5 text-right text-yellow-500/70">{formatCurrency(estFees)}</td>
											<td class="px-2 py-1.5 text-right font-bold" class:text-green-500={trade.pnl > 0} class:text-red-500={trade.pnl < 0}>
												{formatCurrency(trade.pnl)}
											</td>
											<td class="px-2 py-1.5 text-right" class:text-green-500={trade.return_pct > 0} class:text-red-500={trade.return_pct < 0}>
												{formatPercent(trade.return_pct * 100)}
											</td>
											<td class="px-2 py-1.5 text-right" class:text-green-500={cumPnl > 0} class:text-red-500={cumPnl < 0}>
												{formatCurrency(cumPnl)}
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{/if}
			{/if}

		</div>
	{/if}
</div>
