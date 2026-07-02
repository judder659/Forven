<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getStrategies,
		getPrebuiltStrategies,
		submitBacktest,
		previewSignals,
		getResult,
		getSymbols,
		type Strategy,
		type SignalPreview,
		type BacktestResult,
	} from '$lib/api';
	import { resolveDateRangePreset, estimateBarCount } from '$lib/utils/dateRange';
	import { addToast } from '$lib/stores/processTracker';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import BacktestResultSummary from '$lib/components/backtest/BacktestResultSummary.svelte';

	const BAR_CAP = 100_000;

	let prebuiltStrategies: Strategy[] = [];
	let appStrategies: Strategy[] = [];
	let strategies: Strategy[] = [];
	let includeAppGenerated = false;
	let selectedStrategy: Strategy | null = null;
	let selectedKey = '';
	let paramsDraft: Record<string, unknown> = {};
	let loadingStrategies = true;
	let loadError = '';
	let symbolSuggestions: string[] = [];

	// Form state
	const defaultRange = resolveDateRangePreset('1y');
	let symbol = 'BTC/USDT';
	let timeframe = '1h';
	let startDate = defaultRange.startDate;
	let endDate = defaultRange.endDate;

	// Advanced execution config
	let showAdvanced = false;
	let initialCapital = 10000;
	let feeBps = 10;
	let slippageBps = 5;
	let leverage = 1;
	let tradeMode: 'long_only' | 'short_only' | 'both' = 'long_only';
	let sizingMode: 'full' | 'fraction' | 'fixed' | 'atr' | 'kelly' = 'full';
	let riskPerTrade = 0.02;
	let fixedSize = 1000;
	let atrStopMultiplier = 2;
	let kellyMultiplier = 0.5;
	let kellyLookback = 100;
	let stopLossPct: number | null = null;
	let takeProfitPct: number | null = null;
	let trailingStopPct: number | null = null;
	let timeStopBars: number | null = null;

	// Preview state
	let previewLoading = false;
	let preview: SignalPreview | null = null;
	let previewError = '';

	// Submission + result state
	type SubmitStatus = 'idle' | 'submitting' | 'failed';
	let submitStatus: SubmitStatus = 'idle';
	let submitError = '';
	let submitWarning = '';
	let resultLoading = false;
	let inlineResult: BacktestResult | null = null;
	let lastResultId = '';
	let lastStrategyId = '';

	$: busy = submitStatus === 'submitting';
	$: estimatedBars = estimateBarCount(startDate, endDate, timeframe);
	$: numberOrNull = (v: string) => (v.trim() === '' ? null : Number(v));

	function rebuildStrategies() {
		const seen = new Set<string>();
		const merged: Strategy[] = [];
		for (const s of prebuiltStrategies) {
			const key = s.api_name || s.name;
			if (!seen.has(key)) {
				seen.add(key);
				merged.push(s);
			}
		}
		if (includeAppGenerated) {
			for (const s of appStrategies) {
				const key = s.api_name || s.name;
				if (!seen.has(key)) {
					seen.add(key);
					merged.push(s);
				}
			}
		}
		strategies = merged.sort((a, b) => a.name.localeCompare(b.name));
	}

	function toggleAppGenerated() {
		includeAppGenerated = !includeAppGenerated;
		rebuildStrategies();
	}

	async function loadStrategies() {
		loadingStrategies = true;
		loadError = '';
		try {
			const [prebuiltRes, appRes] = await Promise.all([getPrebuiltStrategies(), getStrategies()]);
			prebuiltStrategies = prebuiltRes.strategies;
			appStrategies = appRes.strategies;
			rebuildStrategies();
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load strategies';
		} finally {
			loadingStrategies = false;
		}
	}

	async function loadSymbols() {
		try {
			symbolSuggestions = await getSymbols();
		} catch {
			symbolSuggestions = [];
		}
	}

	onMount(() => {
		loadStrategies();
		loadSymbols();
	});

	function applyStrategy(key: string) {
		selectedKey = key;
		selectedStrategy = strategies.find((s) => (s.api_name || s.name) === key) ?? null;
		preview = null;
		previewError = '';
		if (selectedStrategy?.parameters) {
			paramsDraft = Object.fromEntries(
				Object.entries(selectedStrategy.parameters).map(([k, spec]) => [k, spec.default]),
			);
		} else {
			paramsDraft = {};
		}
		const sSym = (selectedStrategy as Record<string, unknown> | null)?.symbol;
		const sTf = (selectedStrategy as Record<string, unknown> | null)?.timeframe;
		if (typeof sSym === 'string' && sSym.trim()) symbol = sSym.trim();
		if (typeof sTf === 'string' && sTf.trim()) timeframe = sTf.trim();
	}

	function onStrategySelect(event: Event) {
		applyStrategy((event.target as HTMLSelectElement).value);
	}

	function onParamsChange(event: CustomEvent<Record<string, unknown>>) {
		paramsDraft = event.detail;
		preview = null;
	}

	function validate(): string | null {
		if (!selectedStrategy) return 'Select a strategy to backtest.';
		if (!symbol.trim()) return 'Symbol is required.';
		if (startDate && endDate && startDate >= endDate) return 'Start date must be before end date.';
		if (!Number.isFinite(initialCapital) || initialCapital <= 0) return 'Initial capital must be greater than 0.';
		if (!Number.isFinite(feeBps) || feeBps < 0) return 'Fee (bps) cannot be negative.';
		if (!Number.isFinite(slippageBps) || slippageBps < 0) return 'Slippage (bps) cannot be negative.';
		if (!Number.isFinite(leverage) || leverage < 1) return 'Leverage must be at least 1.';
		if (leverage > 125) return 'Leverage above 125× is not supported.';
		if (sizingMode === 'fraction') {
			if (!(riskPerTrade > 0 && riskPerTrade <= 1)) return 'Risk per trade must be between 0 and 1.';
			if (stopLossPct == null && trailingStopPct == null)
				return 'Fraction (risk-based) sizing needs a Stop Loss % or Trailing Stop %.';
		}
		if (sizingMode === 'fixed' && !(fixedSize > 0)) return 'Fixed size must be greater than 0.';
		if (sizingMode === 'atr') {
			if (!(atrStopMultiplier > 0)) return 'ATR stop multiplier must be greater than 0.';
			if (!(riskPerTrade > 0 && riskPerTrade <= 1)) return 'Risk per trade must be between 0 and 1.';
		}
		if (sizingMode === 'kelly') {
			if (!(kellyMultiplier > 0 && kellyMultiplier <= 5)) return 'Kelly multiplier must be between 0 and 5.';
			if (!(Number.isInteger(kellyLookback) && kellyLookback >= 1)) return 'Kelly lookback must be a positive whole number.';
		}
		if (stopLossPct != null && !(stopLossPct > 0 && stopLossPct <= 100)) return 'Stop Loss % must be between 0 and 100.';
		if (takeProfitPct != null && !(takeProfitPct > 0)) return 'Take Profit % must be greater than 0.';
		if (trailingStopPct != null && !(trailingStopPct > 0 && trailingStopPct <= 100)) return 'Trailing Stop % must be between 0 and 100.';
		if (timeStopBars != null && !(Number.isInteger(timeStopBars) && timeStopBars >= 1)) return 'Time Stop must be a positive whole number of bars.';
		if (estimatedBars != null && estimatedBars > BAR_CAP)
			return `This window is ~${estimatedBars.toLocaleString()} bars; the engine caps at ${BAR_CAP.toLocaleString()}.`;
		return null;
	}

	function buildRequest() {
		const strategyId = selectedStrategy!.api_name || selectedStrategy!.name;
		return {
			strategy_id: strategyId,
			strategy_name: strategyId,
			strategy_version: selectedStrategy!.version,
			symbol: symbol.trim(),
			timeframe,
			start: startDate,
			end: endDate,
			params: Object.keys(paramsDraft).length > 0 ? paramsDraft : undefined,
			preserve_result: true,
			initial_capital: initialCapital,
			fee_bps: feeBps,
			slippage_bps: slippageBps,
			leverage,
			trade_mode: tradeMode,
			allow_shorting: tradeMode !== 'long_only',
			sizing_mode: sizingMode,
			risk_per_trade: sizingMode === 'fraction' || sizingMode === 'atr' ? riskPerTrade : undefined,
			fixed_size: sizingMode === 'fixed' ? fixedSize : undefined,
			atr_stop_multiplier: sizingMode === 'atr' ? atrStopMultiplier : undefined,
			kelly_multiplier: sizingMode === 'kelly' ? kellyMultiplier : undefined,
			kelly_lookback: sizingMode === 'kelly' ? kellyLookback : undefined,
			stop_loss_pct: stopLossPct,
			take_profit_pct: takeProfitPct,
			trailing_stop_pct: trailingStopPct,
			time_stop_bars: timeStopBars,
		};
	}

	async function handlePreview() {
		if (!selectedStrategy) {
			previewError = 'Select a strategy first.';
			return;
		}
		previewLoading = true;
		previewError = '';
		preview = null;
		try {
			preview = await previewSignals({
				strategy_name: selectedStrategy.api_name || selectedStrategy.name,
				strategy_version: selectedStrategy.version,
				symbol: symbol.trim(),
				timeframe,
				start: startDate,
				end: endDate,
				trade_mode: tradeMode,
				params: Object.keys(paramsDraft).length > 0 ? paramsDraft : undefined,
			});
		} catch (err) {
			previewError = err instanceof Error ? err.message : 'Signal preview failed';
		} finally {
			previewLoading = false;
		}
	}

	async function handleSubmit() {
		const error = validate();
		if (error) {
			submitError = error;
			return;
		}
		submitStatus = 'submitting';
		submitError = '';
		submitWarning = '';
		inlineResult = null;
		const request = buildRequest();
		const strategyId = request.strategy_id;
		try {
			const job = await submitBacktest(request);
			lastStrategyId = strategyId;
			if (job.warning) submitWarning = job.warning;
			if (job.status === 'succeeded') addToast(`Backtest for ${strategyId} completed`, 'success');
			else addToast(`Backtest for ${strategyId} queued (job ${job.job_id})`, 'info');
			submitStatus = 'idle';
			if (job.result_id) {
				lastResultId = job.result_id;
				resultLoading = true;
				try {
					inlineResult = await getResult(job.result_id);
				} catch {
					inlineResult = null;
				} finally {
					resultLoading = false;
				}
				queueMicrotask(() => document.getElementById('bt-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
			}
		} catch (err) {
			submitStatus = 'failed';
			submitError = err instanceof Error ? err.message : 'Backtest submission failed';
		}
	}

	function openFullReport() {
		if (!lastStrategyId) return;
		goto(`/lab/strategy/${encodeURIComponent(lastStrategyId)}?returnTo=/backtest/new`);
	}

	function resetForNextRun() {
		inlineResult = null;
		lastResultId = '';
		submitWarning = '';
		queueMicrotask(() => document.getElementById('bt-config')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
	}
</script>

<svelte:head>
	<title>Manual Backtest | Forven</title>
</svelte:head>

<div class="mx-auto max-w-7xl px-4 py-6">
	<div>
		<!-- Header -->
		<div class="mb-4 border-b border-[#222] pb-4">
			<div class="flex flex-wrap items-end justify-between gap-4">
				<div>
					<h1 class="text-lg font-bold uppercase tracking-widest text-white">Manual Backtest</h1>
					<p class="mt-1 text-xs text-[#666]">
						Pick a strategy, configure execution, preview signals, and run — results appear inline.
					</p>
				</div>
				<a href="/strategy-creator"
					class="terminal-button text-xs">
					Build your own → Strategy Creator
				</a>
			</div>
		</div>

		<form id="bt-config" on:submit|preventDefault={handleSubmit} novalidate>
			<!-- Strategy Selection -->
			<div class="terminal-card p-4">
				<div class="flex items-center justify-between gap-3">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">
						Strategy
						<span class="ml-2 border border-[#333] px-1.5 py-0.5 text-[9px] tabular-nums text-[#888]">{strategies.length}</span>
					</div>
					<button
						type="button"
						on:click={toggleAppGenerated}
						disabled={busy}
						aria-pressed={includeAppGenerated}
						class="inline-flex items-center gap-2 border px-3 py-1 text-[10px] uppercase tracking-wide transition-colors {includeAppGenerated
							? 'border-white bg-white text-black'
							: 'border-[#333] bg-transparent text-[#666] hover:border-[#555] hover:text-white'}"
					>
						<span class="inline-block h-1.5 w-1.5 rounded-full {includeAppGenerated ? 'bg-black' : 'bg-[#555]'}"></span>
						Include app-generated strategies
					</button>
				</div>
				<div class="mt-3">
					{#if loadingStrategies}
						<div class="text-xs uppercase tracking-widest text-[#555]" role="status" aria-live="polite">Loading strategies…</div>
					{:else if loadError}
						<div class="flex flex-wrap items-center gap-3" role="alert">
							<span class="text-sm text-red-400">{loadError}</span>
							<button type="button" on:click={loadStrategies}
								class="terminal-button text-[10px]">Retry</button>
						</div>
					{:else}
						<select
							id="bt-strategy"
							class="terminal-select"
							on:change={onStrategySelect}
							disabled={busy}
							value={selectedKey}
						>
							<option value="" disabled>Select a strategy…</option>
							{#each strategies as strategy}
								<option value={strategy.api_name || strategy.name}>
									{strategy.name}{strategy.api_name && strategy.api_name !== strategy.name ? ` (${strategy.api_name})` : ''}
								</option>
							{/each}
						</select>
						{#if selectedStrategy?.description}
							<div class="mt-2 text-[11px] text-[#666]">{selectedStrategy.description}</div>
						{/if}
					{/if}
				</div>
			</div>

			<!-- Market Scope -->
			<div class="terminal-card mt-4 p-4">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Market Scope</div>
				<div class="mt-3 grid gap-4 md:grid-cols-2">
					<SymbolInput id="bt-symbol" bind:value={symbol} disabled={busy} suggestions={symbolSuggestions} helpText="Backtested on the base asset (e.g. BTC)." />
					<TimeframeSelect id="bt-timeframe" bind:value={timeframe} disabled={busy} />
				</div>
				<div class="mt-4">
					<DateRangeFieldset idPrefix="bt-date" bind:startDate bind:endDate {timeframe} />
				</div>
			</div>

			<!-- Strategy Parameters -->
			{#if selectedStrategy}
				<div class="terminal-card mt-4 p-4">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Strategy Parameters</div>
					<div class="mt-3">
						<ParameterEditor params={paramsDraft} saving={busy} on:paramsChange={onParamsChange} />
					</div>
				</div>
			{/if}

			<!-- Advanced Execution Config -->
			<div class="terminal-card mt-4 p-4">
				<button type="button" class="flex w-full items-center justify-between text-left" on:click={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Execution Settings</div>
					<span class="text-sm text-[#555]">{showAdvanced ? '−' : '+'}</span>
				</button>
				{#if showAdvanced}
					<div class="mt-4 border-t border-[#222] pt-4 text-[10px] uppercase tracking-wider text-[#555]">Capital &amp; Costs</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Initial Capital</div>
							<input type="number" bind:value={initialCapital} step="1000" min="100" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Fee (bps)</div>
							<input type="number" bind:value={feeBps} step="1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Slippage (bps)</div>
							<input type="number" bind:value={slippageBps} step="1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Leverage</div>
							<input type="number" bind:value={leverage} step="0.5" min="1" max="125" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Trade Direction</div>
							<select bind:value={tradeMode} disabled={busy} class="terminal-select mt-1.5">
								<option value="long_only">Long only</option><option value="short_only">Short only</option><option value="both">Both (hedged)</option>
							</select></label>
					</div>
					<div class="mt-5 text-[10px] uppercase tracking-wider text-[#555]">Position Sizing</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Sizing Mode</div>
							<select bind:value={sizingMode} disabled={busy} class="terminal-select mt-1.5">
								<option value="full">Full equity (default)</option><option value="fraction">Fraction (risk-based)</option><option value="fixed">Fixed notional</option><option value="atr">ATR risk</option><option value="kelly">Kelly</option>
							</select></label>
						{#if sizingMode === 'fraction' || sizingMode === 'atr'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Risk Per Trade</div>
								<input type="number" bind:value={riskPerTrade} step="0.005" min="0" max="1" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
						{#if sizingMode === 'fixed'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Fixed Size (quote)</div>
								<input type="number" bind:value={fixedSize} step="100" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
						{#if sizingMode === 'atr'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">ATR Stop Multiplier</div>
								<input type="number" bind:value={atrStopMultiplier} step="0.1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
						{#if sizingMode === 'kelly'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Kelly Multiplier</div>
								<input type="number" bind:value={kellyMultiplier} step="0.05" min="0" max="5" disabled={busy} class="terminal-input mt-1.5" /></label>
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Kelly Lookback (trades)</div>
								<input type="number" bind:value={kellyLookback} step="10" min="1" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
					</div>
					<div class="mt-5 text-[10px] uppercase tracking-wider text-[#555]">Exits &amp; Stops</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Stop Loss %</div>
							<input type="number" value={stopLossPct ?? ''} on:input={(e) => (stopLossPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Take Profit %</div>
							<input type="number" value={takeProfitPct ?? ''} on:input={(e) => (takeProfitPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Trailing Stop %</div>
							<input type="number" value={trailingStopPct ?? ''} on:input={(e) => (trailingStopPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Time Stop (bars)</div>
							<input type="number" value={timeStopBars ?? ''} on:input={(e) => (timeStopBars = numberOrNull(e.currentTarget.value))} step="1" min="1" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
					</div>
				{/if}
			</div>

			<!-- Preview -->
			<div class="terminal-card mt-4 p-4">
				<div class="flex items-center justify-between">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Signal Preview</div>
					<button type="button" on:click={handlePreview} disabled={busy || previewLoading || !selectedStrategy}
						class="terminal-button text-[10px]">
						{previewLoading ? 'Previewing…' : 'Preview signals'}
					</button>
				</div>
				{#if previewError}
					<div class="mt-3 border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400" role="alert">{previewError}</div>
				{:else if preview}
					<div class="mt-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
						<div><span class="text-[#555]">Bars:</span> <span class="font-mono text-[#aaa]">{preview.total_bars.toLocaleString()}</span></div>
						<div><span class="text-[#555]">Entries:</span> <span class="font-mono text-white">{preview.entry_count}</span></div>
						<div><span class="text-[#555]">Exits:</span> <span class="font-mono text-[#aaa]">{preview.exit_count}</span></div>
						<div><span class="text-[#555]">Density:</span>
							<span class="font-mono {preview.signal_density === 'dense' ? 'text-emerald-400' : preview.signal_density === 'moderate' ? 'text-amber-400' : 'text-[#888]'}">{preview.signal_density}</span></div>
					</div>
					{#if preview.warnings.length}
						<div class="mt-2 space-y-1">
							{#each preview.warnings as w}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{w}</div>{/each}
						</div>
					{/if}
				{:else}
					<p class="mt-2 text-[11px] text-[#555]">Check signal density and data coverage for your config before committing to a full run.</p>
				{/if}
			</div>

			<!-- Submit -->
			<div class="terminal-card mt-4 p-4">
				{#if submitError}<div class="mb-4 border border-red-900 bg-red-500/5 px-4 py-3 text-sm text-red-400" role="alert">{submitError}</div>{/if}
				<div class="flex flex-wrap items-center gap-3">
					<button type="submit" disabled={busy || resultLoading || !selectedStrategy} aria-busy={busy}
						class="terminal-button-primary text-xs disabled:cursor-not-allowed disabled:opacity-40">
						{#if busy || resultLoading}Running backtest…{:else}Run Backtest{/if}
					</button>
				</div>
			</div>
		</form>

		<!-- Inline results -->
		{#if resultLoading || inlineResult || submitWarning}
			<div id="bt-results" class="mt-6 scroll-mt-6">
				{#if submitWarning}
					<div class="mb-4 border border-amber-900 bg-amber-500/5 px-4 py-3 text-sm text-amber-400" role="alert">⚠ {submitWarning}</div>
				{/if}
				<div class="border-b border-[#222] pb-4">
					<div class="flex flex-wrap items-center justify-between gap-3">
						<div>
							<h2 class="text-sm font-bold uppercase tracking-widest text-white">Result</h2>
							<p class="mt-1 text-xs text-[#666]">Out-of-sample performance for the run you just submitted.</p>
						</div>
						<div class="flex items-center gap-2">
							<button type="button" on:click={resetForNextRun}
								class="terminal-button text-[10px]">Adjust &amp; re-run</button>
							<button type="button" on:click={openFullReport} disabled={!lastStrategyId}
								class="terminal-button-primary text-[10px] disabled:opacity-40">Open full report →</button>
						</div>
					</div>
				</div>
				<div class="mt-4">
					{#if resultLoading}
						<div class="terminal-card p-8 text-center text-xs uppercase tracking-widest text-[#555]" role="status" aria-live="polite">Loading result…</div>
					{:else if inlineResult}
						<BacktestResultSummary result={inlineResult} />
					{:else if lastResultId}
						<div class="terminal-card p-6 text-sm text-[#888]">
							Result saved (<span class="font-mono">{lastResultId}</span>) but the summary could not be loaded here.
							<button type="button" on:click={openFullReport} class="ml-1 text-white underline">Open the full report</button>.
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>
