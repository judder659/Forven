<script lang="ts">
	import { onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import {
		deleteResult,
		getDatasets,
		getJob,
		getPipelineSettings,
		getResult,
		getResultChartContext,
		getStrategyContainer,
		promoteForvenStrategy,
		submitBacktest,
		submitOptimization,
		type BacktestResult,
		type Dataset,
		type PipelineSettings,
		type ResultChartContext,
		type StrategyContainerHistoryItem,
		type StrategyContainerPayload,
	} from '$lib/api';
	import { getPrebuiltStrategies } from '$lib/api/strategies';
	import { updateStrategyDefaultParams } from '$lib/api/backtesting';
	import type { EquityPoint, ParamSpec, Strategy, Trade } from '$lib/api/types';
	import StrategyLink from '$lib/components/ui/StrategyLink.svelte';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import PromotionReadiness from '$lib/components/ui/PromotionReadiness.svelte';
	import ChartWorkspace from '$lib/components/chart/ChartWorkspace.svelte';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import HeatmapChart from '$lib/components/charts/HeatmapChart.svelte';
	import RobustnessPanel from '$lib/components/robustness/RobustnessPanel.svelte';
	import GauntletStatusCard from '$lib/components/robustness/GauntletStatusCard.svelte';
	import {
		getPipelineConfig,
		type GauntletTestEntry,
		type GauntletTestKey,
		type PipelineThresholds,
	} from '$lib/api/lifecycle';
	import type { IndicatorConfig as WorkspaceIndicatorConfig, SignalMarker } from '$lib/stores/chartStore';
	import { addToast } from '$lib/stores/processTracker';
	import { estimateBarCount, formatBarEstimate, formatDateWindowSummary, resolveDateRangePreset } from '$lib/utils/dateRange';
	import {
		analyzeRunRelativePosition,
		type ComparableRunMetrics,
	} from '$lib/utils/runInsights';
	import {
		buildLifecycleStageDescriptors,
		lifecycleActorLabel,
		lifecycleStageLabel,
		normalizeLifecycleStage,
		sortLifecycleEventsDescending,
		summarizeLifecycleEvent,
	} from '$lib/utils/lifecyclePresentation';
	import {
		areParameterRecordsEqual,
		cloneParameterRecord,
		stableStringify,
	} from '$lib/utils/parameterEditor';
	import {
		buildQuickScreenEvidenceRows,
		type QuickScreenEvidenceRow,
	} from '$lib/utils/quickScreenReadiness';
	import { buildTradingViewExport } from '$lib/utils/tradingViewExport';
	import BrainStrategyDecisionsCard from '$lib/components/brain/BrainStrategyDecisionsCard.svelte';
	import TradingViewExportModal from '$lib/components/strategy/TradingViewExportModal.svelte';
	import { openDeepdive } from '$lib/stores/deepdiveStore';

	// TabKey identifiers predate the current UI labels. Mapping (code -> visible label):
	//   'backtests'      -> "Gauntlet History" (the "Run the Gauntlet" button submits a backtest)
	//   'optimizations'  -> "Robustness" (hosts the optimization + robustness sub-tabs)
	// PromotionReadiness on:action maps: 'run_confirmation_backtest' -> 'backtests';
	//   'run_optimization'/'apply_best_params'/'*_validation_suite' -> 'optimizations'.
	type TabKey = 'configuration' | 'backtests' | 'optimizations' | 'execution';
	type SubmitStatus = 'idle' | 'submitting' | 'running' | 'completed' | 'failed';
	type RobustnessSubTab = 'optimization' | 'robustness';
	type RobustnessRunnerTestKey = 'walk_forward' | 'monte_carlo' | 'param_jitter' | 'cost_stress' | 'regime_split';
	type RobustnessRunnerCompleteEvent = {
		key: RobustnessRunnerTestKey;
		result_id: string;
		status: string;
		verdict: string | null;
		error?: string | null;
		completed_at?: string | null;
	};

	const defaultOneYearRange = resolveDateRangePreset('1y');
	const RESULT_CHART_TIMEOUT_MS = 15000;
	const OPTIMIZATION_OBJECTIVES = [
		{ value: 'sharpe_ratio', label: 'Sharpe ratio' },
		{ value: 'total_return_pct', label: 'Total return' },
		{ value: 'profit_factor', label: 'Profit factor' },
		{ value: 'win_rate', label: 'Win rate' },
	];

	type OptimizationParamKind = 'int' | 'float';
	type OptimizationParamDraft = {
		key: string;
		current: number;
		kind: OptimizationParamKind;
		selected: boolean;
		min: string;
		max: string;
		step: string;
		error: string;
	};

	let strategyId = '';
	let returnTo = '/lab';
	let activeTab: TabKey = 'configuration';
	let loading = true;
	let error = '';
	let lastLoadedId = '';
	let container: StrategyContainerPayload | null = null;
	let promoting = false;
	let promoteReason = '';
	let showPromoteConfirm = false;
	// When the promotion gate blocks a capital-stage promotion, hold its reason
	// so the confirm box can show it and offer an informed operator override.
	let promoteBlockReason = '';
	let submitStatus: SubmitStatus = 'idle';
	let submitMessage = '';
	let submitJobId: string | null = null;
	let submitProgress = '';
	let submitPollingStatus = '';
	let submitPollCount = 0;
	let tradingViewExportScript = '';
	let tradingViewExportFilename = '';
	let tradingViewExportWarnings: string[] = [];
	let destroyed = false;
	let selectedResult: BacktestResult | null = null;
	let selectedResultId: string | null = null;
	let selectedResultItem: StrategyContainerHistoryItem | null = null;
	let selectedChartContext: ResultChartContext | null = null;
	let chartContextError = '';
	let chartLoading = false;
	let chartFitContentToken = 0;
	let expandedBacktestParamsId: string | null = null;
	let backtestParamDrafts: Record<string, Record<string, unknown>> = {};
	let backtestParamRunnerId: string | null = null;
	let resultLoading = false;
	let resultError = '';
	let settingDefaultParams = false;
	let backtestingOptParams = false;
	let paramsDraft: Record<string, unknown> = {};
	// Validity surfaced from the ParameterEditor(s). Gating Save/Run on these prevents
	// silently persisting/running the last-valid value while a field shows an error.
	let paramsHasErrors = false;
	let backtestParamDraftErrors: Record<string, boolean> = {};
	let prebuiltStrategies: Strategy[] = [];
	let availableParamSpecs: Record<string, ParamSpec> = {};
	let availableAddParamKeys: string[] = [];
	let selectedAddParamKey = '';
	let addParamHelperText = '';
	let currentStrategyIdentity: string[] = [];
	let matchingPrebuiltStrategy: Strategy | null = null;
	let prebuiltLoadSequence = 0;
	let optimizationParamDrafts: Record<string, OptimizationParamDraft> = {};
	let optimizationParamDraftSource = '';
	let parameterSaveMessage = '';
	let parameterSaveError = '';
	let availableDatasets: Dataset[] = [];
	let pipelineSettings: PipelineSettings | null = null;
	let pipelineThresholds: PipelineThresholds | null = null;
	let quickScreenRows: QuickScreenEvidenceRow[] = [];

	let backtestForm = {
		symbol: '',
		timeframe: '1h',
		start_date: defaultOneYearRange.startDate,
		end_date: defaultOneYearRange.endDate,
	};

	let optimizationForm = {
		symbol: '',
		timeframe: '1h',
		start_date: defaultOneYearRange.startDate,
		end_date: defaultOneYearRange.endDate,
		objective: 'sharpe_ratio',
		n_trials: 100,
	};

	let robustnessSubTab: RobustnessSubTab = 'optimization';
	let selectedRobustnessTest: GauntletTestKey = 'walk_forward';
	let robustnessStatusOverrides: Partial<Record<GauntletTestKey, GauntletTestEntry>> = {};

	type HistorySortField =
		| 'created'
		| 'symbol'
		| 'timeframe'
		| 'start'
		| 'end'
		| 'cagr'
		| 'is_cagr'
		| 'oos_cagr'
		| 'is_sharpe'
		| 'sharpe'
		| 'oos_sharpe'
		| 'robustness'
		| 'total_return'
		| 'max_drawdown'
		| 'win_rate'
		| 'trades'
		| 'profit_factor';
	let historySortBy: HistorySortField = 'created';
	let historySortDir: 'asc' | 'desc' = 'desc';

	function toggleHistorySort(field: HistorySortField): void {
		if (historySortBy === field) {
			historySortDir = historySortDir === 'desc' ? 'asc' : 'desc';
		} else {
			historySortBy = field;
			historySortDir = field === 'created' ? 'desc' : 'desc';
		}
	}

	$: strategyId = $page.params.id ?? '';
	$: returnTo = $page.url.searchParams.get('returnTo') || '/lab';

	$: backtestHistoryRaw = container?.history.backtests ?? [];
	$: backtestHistory = sortBacktestHistory(backtestHistoryRaw, historySortBy, historySortDir);
	$: pinnedBacktestId = (container?.strategy?.pinned_backtest_id ?? '').toString().trim();
	$: historySortIndicator = (field: HistorySortField): string =>
		historySortBy === field ? (historySortDir === 'desc' ? ' \u2193' : ' \u2191') : '';
	$: optimizationHistory = container?.history.optimizations ?? [];
	$: walkForwardHistory = container?.history.walk_forward ?? [];
	$: validationHistory = container?.history.validation ?? [];
	$: quickScreenRows = buildQuickScreenEvidenceRows({
		strategy: container?.strategy ?? null,
		backtests: backtestHistory,
		validationHistory,
		pipelineSettings,
	});
	$: executionTrades = container?.execution.trades ?? [];
	$: executionPositions = container?.execution.positions ?? [];
	$: strategyParams = (container?.configuration.params && typeof container.configuration.params === 'object' && !Array.isArray(container.configuration.params))
		? container.configuration.params as Record<string, unknown>
		: {};
	$: recentEvents = container?.events ?? [];
	$: submitProgressPct = parseProgressPct(submitProgress);
	$: datasetSuggestionSymbols = buildDatasetSymbolSuggestions(availableDatasets, backtestForm.timeframe || optimizationForm.timeframe || '1h');
	$: symbolSuggestions = Array.from(
		new Set(
			[
				container?.configuration.symbol,
				container?.strategy.symbol,
				...backtestHistory.map((item) => item.symbol),
				...optimizationHistory.map((item) => item.symbol),
				...walkForwardHistory.map((item) => item.symbol),
				...datasetSuggestionSymbols,
			]
				.map((value) => String(value ?? '').trim())
				.filter(Boolean),
		),
	);
	$: backtestUniverseSummary = summarizeDatasetBacktestUniverse(availableDatasets);
	$: backtestSymbolHelpText = backtestUniverseSummary
		? `Any local dataset symbol can be backtested here. ${backtestUniverseSummary}.`
		: 'Any symbol with local OHLCV data can be backtested here.';
	$: optimizationSymbolHelpText = backtestUniverseSummary
		? `Optimizations can use the same local dataset universe. ${backtestUniverseSummary}.`
		: 'Optimizations can use any symbol with local OHLCV data.';
	$: selectedResultPeerHistory = selectedResult
		? String(selectedResult.result_type || '').trim().toLowerCase() === 'optimization'
			? optimizationHistory.filter((item) => historyItemHasUsableMetrics(item))
			: String(selectedResult.result_type || '').trim().toLowerCase() === 'walk_forward'
				? walkForwardHistory.filter((item) => historyItemHasUsableMetrics(item))
				: backtestHistory.filter((item) => historyItemHasUsableMetrics(item))
		: [];
	$: selectedResultComparableId = selectedResult ? String(selectedResult.id || selectedResultId || '').trim() : '';
	$: selectedResultStatus = resultStatus(selectedResult);
	$: selectedResultErrorDetail = resultErrorDetail(selectedResult);
	$: selectedResultHasUsableMetrics = resultHasUsableMetrics(selectedResult);
	$: selectedResultComparison = selectedResult && selectedResultHasUsableMetrics
		? analyzeRunRelativePosition(
			toComparableResultMetrics(selectedResult),
			selectedResultPeerHistory
				.filter((item) => String(item.result_id || '').trim() !== selectedResultComparableId)
				.map((item) => toComparableHistoryMetrics(item)),
		)
		: null;
	$: selectedResultEquityCurve = selectedResult?.equity_curve ?? null;
	$: selectedResultBenchmarkCurve = selectedResult?.benchmark_curve ?? null;
	$: selectedResultEquityCurveFull = selectedResult?.equity_curve_full ?? null;
	$: selectedResultBenchmarkCurveFull = selectedResult?.benchmark_curve_full ?? null;
	// Show the entire backtest (IS+OOS) when the full curve is available; older results
	// without it fall back to the OOS-only curve so nothing regresses.
	$: selectedResultUsingFullCurve =
		Array.isArray(selectedResultEquityCurveFull) && selectedResultEquityCurveFull.length > 1;
	$: equityCurveForChart = selectedResultUsingFullCurve
		? selectedResultEquityCurveFull
		: (selectedResultEquityCurve ?? []);
	$: benchmarkCurveForChart = selectedResultUsingFullCurve
		? selectedResultBenchmarkCurveFull
		: selectedResultBenchmarkCurve;
	// OOS divider = first timestamp of the OOS-only curve, only meaningful when the
	// chart shows the longer full curve that extends before the OOS window.
	$: oosStartTimestampForChart =
		selectedResultUsingFullCurve && Array.isArray(selectedResultEquityCurve) && selectedResultEquityCurve.length > 0
			? (selectedResultEquityCurve[0]?.timestamp ?? null)
			: null;
	$: selectedResultHasEquityCurve = Array.isArray(selectedResultEquityCurve) && selectedResultEquityCurve.length > 1;
	$: selectedResultMonthlyHeatmap = buildMonthlyReturnsHeatmap(selectedResultEquityCurve);
	$: selectedResultTradeSummary = computeTradeSummary(selectedResult?.trades);
	$: selectedResultRiskMetrics = buildRiskMetricEntries(selectedResult);
	$: selectedChartBars = (selectedChartContext?.bars ?? []).map((bar) => ({
		...bar,
		volume: typeof bar.volume === 'number' && Number.isFinite(bar.volume) ? bar.volume : 0,
	}));
	$: selectedChartEntryMarkers = toSignalMarkers(selectedChartContext?.entry_markers ?? [], 'entry');
	$: selectedChartExitMarkers = toSignalMarkers(selectedChartContext?.exit_markers ?? [], 'exit');
	$: selectedChartMainIndicators = toWorkspaceIndicators(selectedChartContext?.main_indicators ?? [], 'main');
	$: selectedChartSubIndicators = toWorkspaceIndicators(selectedChartContext?.sub_indicators ?? [], 'sub1');
	$: selectedChartWarnings = selectedChartContext?.warnings ?? [];
	$: selectedChartStart = selectedChartBars.length > 0 ? selectedChartBars[0]?.timestamp ?? null : null;
	$: selectedChartEnd = selectedChartBars.length > 0 ? selectedChartBars[selectedChartBars.length - 1]?.timestamp ?? null : null;
	$: paramsDirty = !areParameterRecordsEqual(strategyParams, paramsDraft);
	$: currentStrategyIdentity = resolveContainerStrategyIdentity(container);
	$: matchingPrebuiltStrategy = resolvePrebuiltStrategy(currentStrategyIdentity, prebuiltStrategies);
	$: availableParamSpecs = resolvePrebuiltParamSpecs(currentStrategyIdentity, prebuiltStrategies);
	$: availableAddParamKeys = Object.keys(availableParamSpecs).filter((key) => !Object.prototype.hasOwnProperty.call(paramsDraft, key));
	$: if (selectedAddParamKey && !availableAddParamKeys.includes(selectedAddParamKey)) {
		selectedAddParamKey = availableAddParamKeys[0] ?? '';
	} else if (!selectedAddParamKey && availableAddParamKeys.length > 0) {
		selectedAddParamKey = availableAddParamKeys[0];
	}
	$: addParamHelperText = buildAddParamHelperText(matchingPrebuiltStrategy, availableAddParamKeys.length);
	$: draftParameterCount = Object.keys(paramsDraft).length;
	$: structuredDraftParameterCount = Object.values(paramsDraft).filter((value) => isStructuredParameterValue(value)).length;
	$: scalarDraftParameterCount = Math.max(draftParameterCount - structuredDraftParameterCount, 0);
	// True while ANY backtest/optimization submit is in flight (main run, optimization,
	// or "Gauntlet With Params"). Used to cross-disable every submit control so two
	// concurrent poll loops can't race the shared progress banner or fire duplicate jobs.
	$: isAnyRunInFlight = submitStatus === 'submitting' || submitStatus === 'running' || backtestingOptParams || settingDefaultParams;
	$: backtestWindowSummary = formatDateWindowSummary(backtestForm.start_date, backtestForm.end_date);
	$: backtestBarEstimateLabel = formatBarEstimate(estimateBarCount(backtestForm.start_date, backtestForm.end_date, backtestForm.timeframe));
	$: backtestRunStateLabel = submitStatus === 'submitting' || submitStatus === 'running'
		? 'Gauntlet in progress'
		: submitStatus === 'completed'
			? 'Latest run completed'
			: submitStatus === 'failed'
				? 'Run needs attention'
				: paramsDirty
					? 'Draft tuned and ready'
					: 'Ready to launch';
	$: backtestRunStateTone = submitStatus === 'failed'
		? 'border-red-900/40 bg-red-950/20 text-red-200'
		: submitStatus === 'completed'
			? 'border-emerald-900/40 bg-emerald-950/20 text-emerald-200'
			: submitStatus === 'submitting' || submitStatus === 'running'
				? 'border-blue-900/40 bg-blue-950/20 text-blue-200'
				: paramsDirty
					? 'border-amber-900/40 bg-amber-950/20 text-amber-200'
					: 'border-cyan-900/40 bg-cyan-950/20 text-cyan-200';
	$: backtestRunSummary = submitStatus === 'submitting' || submitStatus === 'running'
		? submitProgress || submitMessage || 'Submitting the current draft to the backtest engine.'
		: submitStatus === 'completed'
			? submitMessage || 'The latest backtest finished successfully.'
			: submitStatus === 'failed'
				? submitMessage || 'The latest submit failed validation or execution.'
				: paramsDirty
					? 'This draft has local changes and the next backtest will use them immediately.'
					: 'The working draft matches the saved defaults for this strategy.';
	$: syncOptimizationParamDrafts(strategyParams);
	$: syncBacktestParamDrafts(backtestHistory);
	$: orderedRecentEvents = sortLifecycleEventsDescending(recentEvents);
	$: latestLifecycleEvent = orderedRecentEvents[0] ?? null;
	$: currentLifecycleStage = normalizeLifecycleStage(container?.strategy.state);
	$: currentStageDescriptors = buildLifecycleStageDescriptors(currentLifecycleStage, displayStages);
	let selectedReadinessStage: string | null = null;
	$: readinessViewStage = selectedReadinessStage ?? currentLifecycleStage;
	$: nextPipelineStage = (() => {
		if (!currentLifecycleStage || currentLifecycleStage in TERMINAL_STAGES) return null;
		const idx = PIPELINE_STAGES.findIndex(s => s.key === currentLifecycleStage);
		if (idx < 0 || idx >= PIPELINE_STAGES.length - 1) return null;
		return PIPELINE_STAGES[idx + 1];
	})();

	function fmtDate(value: unknown): string {
		if (typeof value !== 'string' || !value.trim()) return '-';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return '-';
		return d.toLocaleString();
	}

	function fmtShortDate(value: string | null | undefined): string {
		if (!value) return '-';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return '-';
		return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}

	function fmtBarCount(value: number): string {
		const count = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
		return `${count.toLocaleString()} bars`;
	}

	function parseTimestamp(value: string | null | undefined): number {
		if (!value) return 0;
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? parsed : 0;
	}

	function datasetMarket(dataset: Dataset): string {
		const marketType = String(dataset.market_type || '').trim().toLowerCase();
		if (marketType) return marketType;
		const assetClass = String(dataset.asset_class || '').trim().toLowerCase();
		if (assetClass === 'stock' || assetClass === 'etf') return 'equity';
		return assetClass || 'unknown';
	}

	function summarizeDatasetBacktestUniverse(datasets: Dataset[]): string {
		const markets = Array.from(
			new Set(
				datasets
					.filter((dataset) => (Number(dataset.row_count) || 0) > 0)
					.map((dataset) => datasetMarket(dataset))
					.filter((market) => market && market !== 'unknown'),
			),
		);
		if (markets.length === 0) return '';
		const labels = markets.map((market) => {
			if (market === 'equity') return 'stocks / ETFs';
			if (market === 'crypto') return 'crypto';
			if (market === 'forex') return 'forex';
			if (market === 'index') return 'indices';
			return market;
		});
		return `Local backtest universe includes ${labels.join(', ')}`;
	}

	function buildDatasetSymbolSuggestions(datasets: Dataset[], preferredTimeframe: string): string[] {
		return [...datasets]
			.filter((dataset) => (Number(dataset.row_count) || 0) > 0)
			.sort((left, right) => {
				const leftPreferred = left.timeframe === preferredTimeframe ? 1 : 0;
				const rightPreferred = right.timeframe === preferredTimeframe ? 1 : 0;
				if (leftPreferred !== rightPreferred) return rightPreferred - leftPreferred;
				const recencyDelta = parseTimestamp(right.end_ts || right.start_ts) - parseTimestamp(left.end_ts || left.start_ts);
				if (recencyDelta !== 0) return recencyDelta;
				return left.symbol.localeCompare(right.symbol);
			})
			.map((dataset) => String(dataset.symbol || '').trim())
			.filter(Boolean)
			.filter((symbol, index, values) => values.indexOf(symbol) === index)
			.slice(0, 250);
	}

	function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
		return new Promise<T>((resolve, reject) => {
			const handle = setTimeout(() => {
				reject(new Error(message));
			}, timeoutMs);

			promise.then(
				(value) => {
					clearTimeout(handle);
					resolve(value);
				},
				(error) => {
					clearTimeout(handle);
					reject(error);
				},
			);
		});
	}

	function toSignalMarkers(
		markers: ResultChartContext['entry_markers'],
		type: SignalMarker['type']
	): SignalMarker[] {
		return markers
			.filter((marker) => typeof marker.timestamp === 'string' && Number.isFinite(marker.price))
			.map((marker) => ({
				timestamp: marker.timestamp,
				price: marker.price,
				type,
				// Carry trade side through so ChartWorkspace draws shorts/covers correctly
				// instead of defaulting every marker to long. (label is intentionally omitted
				// so the component's direction-aware "Short"/"Cover" text wins.)
				direction: marker.direction === 'short' ? 'short' : marker.direction === 'long' ? 'long' : undefined,
			}));
	}

	function toWorkspaceIndicators(
		indicators: ResultChartContext['main_indicators'],
		panel: WorkspaceIndicatorConfig['panel']
	): WorkspaceIndicatorConfig[] {
		return indicators.map((indicator, index) => ({
			id: `${panel}-${indicator.name}-${index}`,
			name: indicator.name,
			params: {},
			color: indicator.color || '#22d3ee',
			panel,
			visible: true,
			data: indicator.data ?? [],
			isStrategyIndicator: true,
		}));
	}

	const PIPELINE_STAGES = [
		{ key: 'quick_screen', label: 'Quick Screen', tooltip: 'Initial filter over a 1yr backtest: positive return, IS Sharpe, max drawdown, plus required validation artifacts.' },
		{ key: 'gauntlet', label: 'Gauntlet', tooltip: 'Robustness testing: walk-forward, Monte Carlo, parameter jitter, cost stress, and regime split.' },
		{ key: 'paper', label: 'Paper Trading', tooltip: 'Live paper trading for 14+ days with 10+ closed trades, positive return, and drawdown <15%.' },
		{ key: 'live_graduated', label: 'Live', tooltip: 'Graduated to real capital. Allocation ramps 25% → 50% → 100% over 5 weeks. Kill switch at 30% drawdown.' },
	] as const;

	$: gauntletMinScore = pipelineThresholds?.gauntlet?.min_robustness_score ?? null;
	// Derive the quick-screen tooltip from the SAME settings the readiness rows use so
	// the badge hover stays in lockstep with what is actually gated (the old static copy
	// advertised return >5% / Sharpe >1.0, which matched neither the rows nor the gate).
	$: quickScreenSharpeThreshold = pipelineSettings?.min_sharpe_ratio ?? 0.5;
	$: quickScreenDrawdownLimit = pipelineSettings?.max_drawdown_pct ?? 40;
	$: displayStages = PIPELINE_STAGES.map((s) => {
		if (s.key === 'gauntlet' && gauntletMinScore != null) {
			return { ...s, tooltip: `${s.tooltip} Score must meet or exceed ${gauntletMinScore}.` };
		}
		if (s.key === 'quick_screen') {
			return { ...s, tooltip: `Initial filter (1yr backtest): IS Sharpe > ${quickScreenSharpeThreshold}, min return > 0%, max drawdown < ${quickScreenDrawdownLimit}%, plus required validation artifacts.` };
		}
		return { ...s };
	});

	const TERMINAL_STAGES: Record<string, string> = {
		archived: 'Strategy archived — removed from active pipeline.',
		rejected: 'Strategy rejected — failed validation criteria.',
	};

	function fmtDuration(start: string | null | undefined, end: string | null | undefined, backtestMonths?: number | null): string {
		// Prefer backtest_months from metrics (OOS period) over date range (full IS+OOS window)
		if (backtestMonths != null && Number.isFinite(backtestMonths) && backtestMonths > 0) {
			const totalDays = Math.round(backtestMonths * 30.4375);
			if (totalDays < 1) return '<1d';
			const years = Math.floor(totalDays / 365);
			const months = Math.floor((totalDays % 365) / 30);
			const days = totalDays % 30;
			const parts: string[] = [];
			if (years > 0) parts.push(`${years}y`);
			if (months > 0) parts.push(`${months}m`);
			if (parts.length === 0) parts.push(`${days}d`);
			return parts.join(' ');
		}
		if (!start || !end) return '-';
		const s = new Date(start);
		const e = new Date(end);
		if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return '-';
		const totalDays = Math.round((e.getTime() - s.getTime()) / 86400000);
		if (totalDays < 1) return '<1d';
		const years = Math.floor(totalDays / 365);
		const months = Math.floor((totalDays % 365) / 30);
		const days = totalDays % 30;
		const parts: string[] = [];
		if (years > 0) parts.push(`${years}y`);
		if (months > 0) parts.push(`${months}m`);
		if (parts.length === 0) parts.push(`${days}d`);
		return parts.join(' ');
	}

	function asNumber(value: unknown, fallback = 0): number {
		if (typeof value === 'number' && Number.isFinite(value)) return value;
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : fallback;
	}

	function parseDateValue(value: unknown): Date | null {
		if (typeof value !== 'string' || !value.trim()) return null;
		const parsed = new Date(value);
		if (Number.isNaN(parsed.getTime())) return null;
		return parsed;
	}

	function resolveRunWindowDays(
		start: unknown,
		end: unknown,
		fallbackDays?: unknown,
		fallbackMonths?: unknown,
	): number | null {
		const startDate = parseDateValue(start);
		const endDate = parseDateValue(end);
		if (startDate && endDate) {
			const durationMs = endDate.getTime() - startDate.getTime();
			if (Number.isFinite(durationMs) && durationMs >= 0) {
				return Math.max(durationMs / 86400000, 1);
			}
		}

		const totalDays = asNumber(fallbackDays, Number.NaN);
		if (Number.isFinite(totalDays) && totalDays > 0) return totalDays;

		const totalMonths = asNumber(fallbackMonths, Number.NaN);
		if (Number.isFinite(totalMonths) && totalMonths > 0) return totalMonths * 30.4375;

		return null;
	}

	function formatRatePerWeek(value: number | null): string {
		if (value === null || !Number.isFinite(value)) return '-';
		if (value >= 100) return value.toFixed(0);
		if (value >= 10) return value.toFixed(1).replace(/\.0$/, '');
		return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
	}

	function formatTradesPerWeek(
		trades: unknown,
		start: unknown,
		end: unknown,
		fallbackDays?: unknown,
		fallbackMonths?: unknown,
	): string {
		const totalTrades = Math.max(0, Math.round(asNumber(trades, 0)));
		const windowDays = resolveRunWindowDays(start, end, fallbackDays, fallbackMonths);
		if (windowDays === null) return '-';
		return formatRatePerWeek((totalTrades / windowDays) * 7);
	}

	function historyTradesPerWeek(item: StrategyContainerHistoryItem): string {
		return formatTradesPerWeek(
			readMetric(item, 'total_trades', 'trades'),
			item.start_date ?? item.config.start,
			item.end_date ?? item.config.end,
			item.metrics.backtest_days,
			item.metrics.backtest_months,
		);
	}

	function resultTradesPerWeek(result: BacktestResult | null): string {
		if (!result) return '-';
		return formatTradesPerWeek(
			readResultMetric(result, 'total_trades', 'trades'),
			result.config?.start,
			result.config?.end,
			result.metrics?.backtest_days,
			result.metrics?.backtest_months,
		);
	}

	function pct(value: unknown, decimals = 2): string {
		const v = asNumber(value, 0);
		return `${v.toFixed(decimals)}%`;
	}

	function pctOrDash(value: number | null, decimals = 2): string {
		if (value === null || !Number.isFinite(value)) return '-';
		return `${value.toFixed(decimals)}%`;
	}

	function numOrDash(value: number | null, decimals = 2): string {
		if (value === null || !Number.isFinite(value)) return '--';
		return value.toFixed(decimals);
	}

	function parseDefinitionRecord(value: unknown): Record<string, unknown> | undefined {
		if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>;
		if (typeof value !== 'string' || !value.trim()) return undefined;
		try {
			const parsed = JSON.parse(value);
			if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			// Ignore malformed definition JSON and fall back to params-only requests.
		}
		return undefined;
	}

	function getContainerDefinitionJson(): Record<string, unknown> | undefined {
		return parseDefinitionRecord(container?.strategy.definition_json);
	}

	function normalizeStrategyLookupValue(value: unknown): string {
		return String(value ?? '').trim().toLowerCase();
	}

	function resolveContainerStrategyIdentity(payload: StrategyContainerPayload | null): string[] {
		if (!payload) return [];
		const candidates = [
			payload.strategy.id,
			payload.strategy.name,
			payload.strategy.type,
			payload.strategy.display_id,
			payload.strategy.hypothesis_id,
			payload.strategy.hypothesis_display_id,
			payload.configuration?.strategy_id,
			payload.configuration?.strategy_name,
			payload.configuration?.type,
			payload.configuration?.strategy_display_id,
			payload.configuration?.hypothesis_id,
			payload.configuration?.hypothesis_display_id,
		];
		return Array.from(
			new Set(
				candidates
					.map((value) => normalizeStrategyLookupValue(value))
					.filter(Boolean),
			),
		);
	}

	function resolvePrebuiltStrategy(identities: string[], strategies: Strategy[]): Strategy | null {
		if (identities.length === 0 || strategies.length === 0) return null;
		for (const strategy of strategies) {
			const strategyIdentities = [
				strategy.api_name,
			]
				.map((value) => normalizeStrategyLookupValue(value))
				.filter(Boolean);
			if (strategyIdentities.some((identity) => identities.includes(identity))) {
				return strategy;
			}
		}
		return null;
	}

	function resolvePrebuiltParamSpecs(identities: string[], strategies: Strategy[]): Record<string, ParamSpec> {
		return resolvePrebuiltStrategy(identities, strategies)?.parameters ?? {};
	}

	async function loadPrebuiltStrategies(): Promise<{ strategies: Strategy[] }> {
		try {
			const payload = await Promise.resolve(getPrebuiltStrategies());
			if (payload && Array.isArray(payload.strategies)) {
				return { strategies: payload.strategies };
			}
		} catch {
			// Some tests and legacy environments do not provide this endpoint.
		}
		return { strategies: [] };
	}

	function buildAddParamHelperText(strategy: Strategy | null, addableCount: number): string {
		if (!strategy) {
			return 'No matching prebuilt strategy metadata was found for this container.';
		}
		if (addableCount <= 0) {
			return `All supported params from ${strategy.name} are already in the draft.`;
		}
		return `${addableCount} addable param${addableCount === 1 ? '' : 's'} available from ${strategy.name}.`;
	}

	function addSelectedParamToDraft(): void {
		const key = selectedAddParamKey.trim();
		if (!key || Object.prototype.hasOwnProperty.call(paramsDraft, key)) return;
		const spec = availableParamSpecs[key];
		if (!spec) return;
		paramsDraft = {
			...paramsDraft,
			[key]: spec.default,
		};
	}

	function getResultDefinitionJson(result: BacktestResult | null): Record<string, unknown> | undefined {
		return parseDefinitionRecord(result?.config?.definition_json) ?? getContainerDefinitionJson();
	}

	function getHistoryDefinitionJson(item: StrategyContainerHistoryItem): Record<string, unknown> | undefined {
		const config = item.config && typeof item.config === 'object' ? item.config : {};
		return parseDefinitionRecord(config.definition_json) ?? getContainerDefinitionJson();
	}

	function signedPercentClass(value: number | null): string {
		if (value === null || !Number.isFinite(value)) return 'text-gray-500';
		return value >= 0 ? 'text-emerald-400' : 'text-red-400';
	}

	function normalizeResultStatus(value: unknown): 'running' | 'succeeded' | 'failed' {
		const normalized = String(value ?? '').trim().toLowerCase();
		if (!normalized) return 'succeeded';
		if (normalized === 'succeeded' || normalized === 'success' || normalized === 'completed' || normalized === 'complete') {
			return 'succeeded';
		}
		if (normalized === 'failed' || normalized === 'error') {
			return 'failed';
		}
		return 'running';
	}

	function statusLabel(status: 'running' | 'succeeded' | 'failed'): string {
		if (status === 'failed') return 'Failed';
		if (status === 'succeeded') return 'Succeeded';
		return 'Running';
	}

	function statusBadgeClass(status: 'running' | 'succeeded' | 'failed'): string {
		if (status === 'failed') return 'border-red-700/70 bg-red-950/30 text-red-200';
		if (status === 'succeeded') return 'border-emerald-700/70 bg-emerald-950/20 text-emerald-200';
		return 'border-blue-700/70 bg-blue-950/30 text-blue-200';
	}

	function toDateInput(value: unknown): string {
		if (typeof value !== 'string' || !value.trim()) return '';
		const direct = /^(\d{4}-\d{2}-\d{2})/.exec(value.trim());
		if (direct) return direct[1];
		const parsed = new Date(value);
		if (Number.isNaN(parsed.getTime())) return '';
		return parsed.toISOString().slice(0, 10);
	}

	function toIsoDate(value: string): string | undefined {
		const normalized = value.trim();
		if (!normalized) return undefined;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return undefined;
		return parsed.toISOString();
	}

	function getString(record: Record<string, unknown>, key: string, fallback = '-'): string {
		const value = record[key];
		if (value == null) return fallback;
		const text = String(value).trim();
		return text || fallback;
	}

	function extractParamRecord(value: unknown): Record<string, unknown> {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
		return cloneParameterRecord(value as Record<string, unknown>);
	}

	function isStructuredParameterValue(value: unknown): boolean {
		return Boolean(value) && typeof value === 'object';
	}

	function getHistoryParams(item: StrategyContainerHistoryItem): Record<string, unknown> {
		const stored = extractParamRecord(item.config?.params);
		if (Object.keys(stored).length > 0) return stored;
		return cloneParameterRecord(strategyParams);
	}

	function getHistoryParamSource(item: StrategyContainerHistoryItem): 'stored' | 'current' | 'none' {
		const stored = extractParamRecord(item.config?.params);
		if (Object.keys(stored).length > 0) return 'stored';
		if (Object.keys(strategyParams).length > 0) return 'current';
		return 'none';
	}

	function buildBacktestParamDraftMap(items: StrategyContainerHistoryItem[]): Record<string, Record<string, unknown>> {
		const next: Record<string, Record<string, unknown>> = {};
		for (const item of items) {
			const resultId = String(item.result_id || '').trim();
			if (!resultId) continue;
			next[resultId] = getHistoryParams(item);
		}
		return next;
	}

	function syncBacktestParamDrafts(items: StrategyContainerHistoryItem[]): void {
		if (!Array.isArray(items) || items.length === 0) {
			if (Object.keys(backtestParamDrafts).length > 0) {
				backtestParamDrafts = {};
			}
			if (expandedBacktestParamsId && !items.some((item) => String(item.result_id || '').trim() === expandedBacktestParamsId)) {
				expandedBacktestParamsId = null;
			}
			return;
		}
		const next: Record<string, Record<string, unknown>> = {};
		for (const item of items) {
			const resultId = String(item.result_id || '').trim();
			if (!resultId) continue;
			next[resultId] = cloneParameterRecord(backtestParamDrafts[resultId] ?? getHistoryParams(item));
		}
		backtestParamDrafts = next;
		if (expandedBacktestParamsId && !(expandedBacktestParamsId in next)) {
			expandedBacktestParamsId = null;
		}
	}

	function getBacktestParamDraft(item: StrategyContainerHistoryItem): Record<string, unknown> {
		const resultId = String(item.result_id || '').trim();
		return cloneParameterRecord(backtestParamDrafts[resultId] ?? getHistoryParams(item));
	}

	function updateBacktestParamDraft(resultId: string, nextParams: Record<string, unknown>): void {
		backtestParamDrafts = {
			...backtestParamDrafts,
			[resultId]: cloneParameterRecord(nextParams),
		};
	}

	function resetBacktestParamDraft(item: StrategyContainerHistoryItem): void {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		updateBacktestParamDraft(resultId, getHistoryParams(item));
	}

	function toggleBacktestParamEditor(item: StrategyContainerHistoryItem): void {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		expandedBacktestParamsId = expandedBacktestParamsId === resultId ? null : resultId;
	}

	function formatBacktestParamChipValue(value: unknown): string {
		if (typeof value === 'number' && Number.isFinite(value)) {
			if (Number.isInteger(value)) return String(value);
			return value.toFixed(Math.abs(value) >= 10 ? 2 : 4).replace(/\.?0+$/, '');
		}
		if (typeof value === 'boolean') return value ? 'true' : 'false';
		if (Array.isArray(value)) return `[${value.length}]`;
		if (value && typeof value === 'object') return '{...}';
		const text = String(value ?? '').trim();
		if (!text) return '""';
		return text.length > 18 ? `${text.slice(0, 15)}...` : text;
	}

	function isBacktestParamDifferentFromDefaults(key: string, value: unknown): boolean {
		if (!(key in strategyParams)) return true;
		return stableStringify(strategyParams[key]) !== stableStringify(value);
	}

	function getBacktestParamSummary(item: StrategyContainerHistoryItem): Array<{ key: string; value: string; changed: boolean }> {
		return Object.entries(getBacktestParamDraft(item))
			.sort(([left], [right]) => left.localeCompare(right))
			.map(([key, value]) => ({
				key,
				value: formatBacktestParamChipValue(value),
				changed: isBacktestParamDifferentFromDefaults(key, value),
			}));
	}

	function getBacktestVisibleParamSummary(item: StrategyContainerHistoryItem): Array<{ key: string; value: string; changed: boolean }> {
		return getBacktestParamSummary(item).slice(0, 4);
	}

	function getBacktestParamOverflowCount(item: StrategyContainerHistoryItem): number {
		return Math.max(getBacktestParamSummary(item).length - 4, 0);
	}

	function getRowId(record: Record<string, unknown>, fallback: string): string {
		for (const key of ['id', 'trade_id', 'position_id']) {
			const value = record[key];
			if (typeof value === 'string' && value.trim()) return value.trim();
		}
		return fallback;
	}


	function selectRobustnessTab(tab: RobustnessSubTab): void {
		robustnessSubTab = tab;
	}

	function selectedRunnerTestKey(key: GauntletTestKey): RobustnessRunnerTestKey {
		return key === 'parameter_jitter' ? 'param_jitter' : key;
	}

	function runnerTestToGauntletKey(key: RobustnessRunnerTestKey): GauntletTestKey {
		return key === 'param_jitter' ? 'parameter_jitter' : key;
	}

	function noteRobustnessTestComplete(detail: RobustnessRunnerCompleteEvent): void {
		const key = runnerTestToGauntletKey(detail.key);
		robustnessStatusOverrides = {
			...robustnessStatusOverrides,
			[key]: {
				result_id: detail.result_id,
				status: detail.status,
				verdict: detail.verdict,
				result_type: detail.key,
				completed_at: detail.completed_at ?? new Date().toISOString(),
				error: detail.error ?? null,
			},
		};
	}

	function readMetricOptional(item: StrategyContainerHistoryItem, ...keys: string[]): number | null {
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		for (const key of keys) {
			if (!(key in metrics)) continue;
			const value = asNumber(metrics[key], Number.NaN);
			if (Number.isFinite(value)) return value;
		}
		return null;
	}

	function readMetric(item: StrategyContainerHistoryItem, ...keys: string[]): number {
		return readMetricOptional(item, ...keys) ?? 0;
	}

	function inferUsesRatioPercentScale(metrics: Record<string, unknown>): boolean | null {
		for (const key of ['win_rate', 'winRate', 'win_rate_pct']) {
			if (!(key in metrics)) continue;
			const value = asNumber(metrics[key], Number.NaN);
			if (!Number.isFinite(value)) continue;
			return Math.abs(value) <= 1;
		}
		return null;
	}

	function normalizePercentMetricValue(
		metrics: Record<string, unknown>,
		key: string,
		value: number,
	): number {
		if (key === 'win_rate' || key === 'winRate' || key === 'win_rate_pct') {
			return Math.abs(value) <= 1 ? value * 100 : value;
		}
		const usesRatioPercentScale = inferUsesRatioPercentScale(metrics);
		const percentLikeKey = key.endsWith('_pct') || key === 'total_return' || key === 'pnl_pct' || key === 'max_drawdown';
		if (percentLikeKey && usesRatioPercentScale === true) return value * 100;
		if (percentLikeKey && usesRatioPercentScale === false) return value;
		if (key.endsWith('_pct') && Math.abs(value) <= 1) return value * 100;
		return value;
	}

	function readPercentMetricOptional(item: StrategyContainerHistoryItem, ...keys: string[]): number | null {
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		for (const key of keys) {
			if (!(key in metrics)) continue;
			const value = asNumber(metrics[key], Number.NaN);
			if (!Number.isFinite(value)) continue;
			return normalizePercentMetricValue(metrics, key, value);
		}
		return null;
	}

	function readPercentMetric(item: StrategyContainerHistoryItem, ...keys: string[]): number {
		return readPercentMetricOptional(item, ...keys) ?? 0;
	}

	function readDrawdownPercentMetricOptional(item: StrategyContainerHistoryItem, ...keys: string[]): number | null {
		const value = readPercentMetricOptional(item, ...keys);
		if (value === null || !Number.isFinite(value)) return null;
		return Math.max(0, Math.min(Math.abs(value), 100));
	}

	function readDrawdownPercentMetric(item: StrategyContainerHistoryItem, ...keys: string[]): number {
		return readDrawdownPercentMetricOptional(item, ...keys) ?? 0;
	}

	function readFlag(item: StrategyContainerHistoryItem, key: string): boolean | null {
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		if (!(key in metrics)) return null;
		const value = metrics[key];
		return typeof value === 'boolean' ? value : null;
	}

	function isCagrReliable(item: StrategyContainerHistoryItem): boolean {
		const flag = readFlag(item, 'annualized_return_reliable');
		if (flag !== null) return flag;
		const months = readMetricOptional(item, 'backtest_months');
		return months === null ? true : months >= 1;
	}

	function isSharpeReliable(item: StrategyContainerHistoryItem): boolean {
		const flag = readFlag(item, 'sharpe_is_reliable');
		if (flag !== null) return flag;
		const trades = readMetricOptional(item, 'total_trades', 'trades');
		return trades === null ? true : trades >= 20;
	}

	function formatProfitFactor(item: StrategyContainerHistoryItem): string {
		if (readFlag(item, 'profit_factor_is_infinite') === true) return '∞';
		const value = readMetricOptional(item, 'profit_factor', 'pf');
		if (value === null || !Number.isFinite(value)) return '∞';
		return value.toFixed(2);
	}

	function formatCagr(item: StrategyContainerHistoryItem): string {
		const value = readPercentMetricOptional(item, 'annualized_return_pct');
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatSharpe(item: StrategyContainerHistoryItem): string {
		const value = readMetricOptional(item, 'sharpe_ratio', 'sharpe');
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function readNestedRecord(item: StrategyContainerHistoryItem, parentKey: string): Record<string, unknown> {
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics as Record<string, unknown> : {};
		const nested = metrics[parentKey];
		if (nested && typeof nested === 'object' && !Array.isArray(nested)) return nested as Record<string, unknown>;
		return {};
	}

	function readInSampleCagr(item: StrategyContainerHistoryItem): number | null {
		const top = readPercentMetricOptional(item, 'in_sample_annualized_return_pct', 'is_annualized_return_pct');
		if (top !== null) return top;
		const nested = readNestedRecord(item, 'in_sample');
		const raw = asNumber(nested['annualized_return_pct'], Number.NaN);
		if (!Number.isFinite(raw)) return null;
		return Math.abs(raw) <= 1 ? raw * 100 : raw;
	}

	function readInSampleSharpe(item: StrategyContainerHistoryItem): number | null {
		const top = readMetricOptional(item, 'in_sample_sharpe', 'is_sharpe', 'is_sharpe_ratio');
		if (top !== null) return top;
		const nested = readNestedRecord(item, 'in_sample');
		const raw = asNumber(nested['sharpe_ratio'] ?? nested['sharpe'], Number.NaN);
		return Number.isFinite(raw) ? raw : null;
	}

	function readOutOfSampleCagr(item: StrategyContainerHistoryItem): number | null {
		const top = readPercentMetricOptional(item, 'out_of_sample_annualized_return_pct', 'oos_annualized_return_pct');
		if (top !== null) return top;
		const nested = readNestedRecord(item, 'out_of_sample');
		const raw = asNumber(nested['annualized_return_pct'], Number.NaN);
		if (!Number.isFinite(raw)) return null;
		return Math.abs(raw) <= 1 ? raw * 100 : raw;
	}

	function readOutOfSampleSharpe(item: StrategyContainerHistoryItem): number | null {
		const top = readMetricOptional(item, 'out_of_sample_sharpe', 'oos_sharpe', 'oos_sharpe_ratio');
		if (top !== null) return top;
		const nested = readNestedRecord(item, 'out_of_sample');
		const raw = asNumber(nested['sharpe_ratio'] ?? nested['sharpe'], Number.NaN);
		return Number.isFinite(raw) ? raw : null;
	}

	function formatOutOfSampleCagr(item: StrategyContainerHistoryItem): string {
		const value = readOutOfSampleCagr(item);
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatOutOfSampleSharpe(item: StrategyContainerHistoryItem): string {
		const value = readOutOfSampleSharpe(item);
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function readRobustness(item: StrategyContainerHistoryItem): number | null {
		const raw = readMetricOptional(item, 'composite_robustness_score', 'robustness_score', 'robustness', 'gauntlet_score');
		if (raw === null) return null;
		return Math.abs(raw) <= 1 ? raw * 100 : raw;
	}

	function formatInSampleCagr(item: StrategyContainerHistoryItem): string {
		const value = readInSampleCagr(item);
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatInSampleSharpe(item: StrategyContainerHistoryItem): string {
		const value = readInSampleSharpe(item);
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function readWalkForwardSharpe(item: StrategyContainerHistoryItem, key: 'avg_is_sharpe' | 'avg_oos_sharpe'): number | null {
		return readMetricOptional(item, key);
	}

	function formatWalkForwardSharpe(item: StrategyContainerHistoryItem, key: 'avg_is_sharpe' | 'avg_oos_sharpe'): string {
		const value = readWalkForwardSharpe(item, key);
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function readWalkForwardDegradationPct(item: StrategyContainerHistoryItem): number | null {
		const value = readMetricOptional(item, 'degradation');
		if (value === null) return null;
		return value * 100;
	}

	function walkForwardDegradationClass(item: StrategyContainerHistoryItem): string {
		const value = readWalkForwardDegradationPct(item);
		if (value === null) return 'text-gray-500';
		return value > 50 ? 'text-red-400' : 'text-emerald-400';
	}

	function formatWalkForwardDegradation(item: StrategyContainerHistoryItem): string {
		const value = readWalkForwardDegradationPct(item);
		if (value === null) return '-';
		return `${value.toFixed(1)}%`;
	}

	function readWalkForwardOosTrades(item: StrategyContainerHistoryItem): number | null {
		const aggregate = readNestedRecord(item, 'aggregate_oos');
		const raw = asNumber(aggregate['total_trades'] ?? aggregate['trades'], Number.NaN);
		return Number.isFinite(raw) ? raw : null;
	}

	function formatWalkForwardOosTrades(item: StrategyContainerHistoryItem): string {
		const value = readWalkForwardOosTrades(item);
		if (value === null) return '-';
		return String(Math.round(value));
	}

	function formatRobustness(item: StrategyContainerHistoryItem): string {
		const value = readRobustness(item);
		if (value === null) return '-';
		return `${value.toFixed(1)}%`;
	}

	function historyTradesCount(item: StrategyContainerHistoryItem): string {
		const trades = readMetricOptional(item, 'total_trades', 'trades', 'trade_count');
		if (trades === null) return '-';
		return Math.round(trades).toString();
	}

	function readHistorySortValue(item: StrategyContainerHistoryItem, field: HistorySortField): number | string | null {
		switch (field) {
			case 'created': {
				const ts = Date.parse(item.created_at || '');
				return Number.isFinite(ts) ? ts : null;
			}
			case 'symbol':
				return (item.symbol || '').toLowerCase();
			case 'timeframe':
				return (item.timeframe || '').toLowerCase();
			case 'start': {
				const ts = Date.parse(item.start_date || '');
				return Number.isFinite(ts) ? ts : null;
			}
			case 'end': {
				const ts = Date.parse(item.end_date || '');
				return Number.isFinite(ts) ? ts : null;
			}
			case 'cagr':
				return readPercentMetricOptional(item, 'annualized_return_pct');
			case 'is_cagr':
				return readInSampleCagr(item);
			case 'oos_cagr':
				return readOutOfSampleCagr(item);
			case 'is_sharpe':
				return readInSampleSharpe(item);
			case 'sharpe':
				return readMetricOptional(item, 'sharpe_ratio', 'sharpe');
			case 'oos_sharpe':
				return readOutOfSampleSharpe(item);
			case 'robustness':
				return readRobustness(item);
			case 'total_return':
				return readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct');
			case 'max_drawdown':
				return readDrawdownPercentMetricOptional(item, 'max_drawdown_pct', 'max_drawdown');
			case 'win_rate':
				return readPercentMetricOptional(item, 'win_rate', 'win_rate_pct');
			case 'trades':
				return readMetricOptional(item, 'total_trades', 'trades', 'trade_count');
			case 'profit_factor': {
				if (readFlag(item, 'profit_factor_is_infinite') === true) return Number.POSITIVE_INFINITY;
				return readMetricOptional(item, 'profit_factor');
			}
			default:
				return null;
		}
	}

	function sortBacktestHistory(
		items: StrategyContainerHistoryItem[],
		field: HistorySortField,
		dir: 'asc' | 'desc'
	): StrategyContainerHistoryItem[] {
		const decorated = items.map((item, index) => ({ item, index, sortValue: readHistorySortValue(item, field) }));
		const sign = dir === 'asc' ? 1 : -1;
		decorated.sort((a, b) => {
			const av = a.sortValue;
			const bv = b.sortValue;
			const aNull = av === null || (typeof av === 'number' && !Number.isFinite(av));
			const bNull = bv === null || (typeof bv === 'number' && !Number.isFinite(bv));
			if (aNull && bNull) return a.index - b.index;
			if (aNull) return 1;
			if (bNull) return -1;
			if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign;
			return String(av).localeCompare(String(bv)) * sign;
		});
		return decorated.map((entry) => entry.item);
	}

	function historyItemStatus(item: StrategyContainerHistoryItem): 'running' | 'succeeded' | 'failed' {
		const config = item.config && typeof item.config === 'object' ? item.config : {};
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		return normalizeResultStatus(config.status ?? metrics.status);
	}

	function historyItemError(item: StrategyContainerHistoryItem): string | undefined {
		const config = item.config && typeof item.config === 'object' ? item.config : {};
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		const error = String(config.error ?? metrics.error ?? '').trim();
		if (error) return error;
		if (historyItemStatus(item) === 'failed') return 'Run failed before an error message was persisted.';
		return undefined;
	}

	function historyItemHasUsableMetrics(item: StrategyContainerHistoryItem): boolean {
		return historyItemStatus(item) === 'succeeded';
	}

	function historyItemTrials(item: StrategyContainerHistoryItem): number | null {
		const metrics = item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
		const config = item.config && typeof item.config === 'object' ? item.config : {};
		for (const value of [metrics.n_trials, metrics.trials, config.n_trials, config.trials]) {
			const parsed = asNumber(value, Number.NaN);
			if (Number.isFinite(parsed) && parsed > 0) return Math.round(parsed);
		}
		return null;
	}

	function readResultMetricOptional(result: BacktestResult | null, ...keys: string[]): number | null {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return null;
		for (const key of keys) {
			if (!(key in result.metrics)) continue;
			const value = asNumber(result.metrics[key], Number.NaN);
			if (Number.isFinite(value)) return value;
		}
		return null;
	}

	function readResultMetric(result: BacktestResult | null, ...keys: string[]): number {
		return readResultMetricOptional(result, ...keys) ?? 0;
	}

	function readResultPercentMetricOptional(result: BacktestResult | null, ...keys: string[]): number | null {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return null;
		for (const key of keys) {
			if (!(key in result.metrics)) continue;
			const value = asNumber(result.metrics[key], Number.NaN);
			if (!Number.isFinite(value)) continue;
			return normalizePercentMetricValue(result.metrics, key, value);
		}
		return null;
	}

	function readResultPercentMetric(result: BacktestResult | null, ...keys: string[]): number {
		return readResultPercentMetricOptional(result, ...keys) ?? 0;
	}

	function readResultDrawdownPercentMetricOptional(result: BacktestResult | null, ...keys: string[]): number | null {
		const value = readResultPercentMetricOptional(result, ...keys);
		if (value === null || !Number.isFinite(value)) return null;
		return Math.max(0, Math.min(Math.abs(value), 100));
	}

	function readResultDrawdownPercentMetric(result: BacktestResult | null, ...keys: string[]): number {
		return readResultDrawdownPercentMetricOptional(result, ...keys) ?? 0;
	}

	function readResultFlag(result: BacktestResult | null, key: string): boolean | null {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return null;
		if (!(key in result.metrics)) return null;
		const value = (result.metrics as Record<string, unknown>)[key];
		return typeof value === 'boolean' ? value : null;
	}

	function readResultDataQualityFlags(result: BacktestResult | null): string[] {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return [];
		const flags = (result.metrics as Record<string, unknown>)['data_quality_flags'];
		return Array.isArray(flags) ? flags.map((flag) => String(flag)) : [];
	}

	function readResultCoverage(result: BacktestResult | null, key: string): number | null {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return null;
		const value = asNumber((result.metrics as Record<string, unknown>)[key], Number.NaN);
		return Number.isFinite(value) ? value : null;
	}

	function coverageToneClass(value: number | null): string {
		if (value === null) return 'text-gray-500';
		if (value >= 95) return 'text-emerald-400';
		if (value >= 50) return 'text-amber-400';
		return 'text-red-400';
	}

	function formatCoveragePct(value: number | null): string {
		return value === null ? '—' : `${value.toFixed(0)}%`;
	}

	// ---------------------------------------------------------------------------
	// Result viewer analytics: equity-derived monthly returns, trade summary,
	// streak detection, and the risk-adjusted metrics grid. All null-safe so the
	// viewer degrades gracefully when equity_curve / trades are absent.
	// ---------------------------------------------------------------------------

	const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

	type MonthlyReturnsHeatmap = {
		data: number[][];
		xLabels: string[];
		yLabels: string[];
	};

	/**
	 * Derive a year x month heatmap of percentage returns from an equity series.
	 * For each calendar month we take the equity at the last point of that month
	 * relative to the equity at the last point of the prior month (or the first
	 * observed equity for the opening month). Months with no data are NaN so the
	 * heatmap renders them as empty cells. Returns null when the series is too
	 * thin to derive anything meaningful.
	 */
	function buildMonthlyReturnsHeatmap(equity: EquityPoint[] | null | undefined): MonthlyReturnsHeatmap | null {
		if (!Array.isArray(equity) || equity.length < 2) return null;

		const points = equity
			.map((point) => {
				const ts = parseDateValue(point.timestamp);
				const value = asNumber(point.equity, Number.NaN);
				if (!ts || !Number.isFinite(value)) return null;
				return { time: ts.getTime(), year: ts.getUTCFullYear(), month: ts.getUTCMonth(), value };
			})
			.filter((p): p is { time: number; year: number; month: number; value: number } => p !== null)
			.sort((a, b) => a.time - b.time);

		if (points.length < 2) return null;

		// Last equity observation per (year, month) bucket.
		const monthEnd = new Map<string, { year: number; month: number; value: number }>();
		for (const point of points) {
			monthEnd.set(`${point.year}-${point.month}`, { year: point.year, month: point.month, value: point.value });
		}

		const buckets = Array.from(monthEnd.values()).sort((a, b) =>
			a.year !== b.year ? a.year - b.year : a.month - b.month,
		);
		if (buckets.length === 0) return null;

		const minYear = buckets[0].year;
		const maxYear = buckets[buckets.length - 1].year;
		const years: number[] = [];
		for (let y = minYear; y <= maxYear; y++) years.push(y);

		// grid[year][month] = month-end-over-prior-month-end percentage return.
		const grid: number[][] = years.map(() => new Array(12).fill(Number.NaN));
		let prevValue = points[0].value;
		for (const bucket of buckets) {
			const ret = prevValue !== 0 ? ((bucket.value - prevValue) / prevValue) * 100 : Number.NaN;
			const rowIndex = bucket.year - minYear;
			grid[rowIndex][bucket.month] = ret;
			prevValue = bucket.value;
		}

		return {
			data: grid,
			xLabels: MONTH_LABELS,
			yLabels: years.map((year) => String(year)),
		};
	}

	type TradeSummary = {
		total: number;
		wins: number;
		losses: number;
		breakeven: number;
		winRatePct: number;
		avgWin: number;
		avgLoss: number;
		payoffRatio: number | null;
		largestWin: number;
		largestLoss: number;
		expectancy: number;
		longestWinStreak: number;
		longestLossStreak: number;
	};

	/**
	 * Compute win/loss aggregates, payoff ratio, expectancy, and the longest
	 * consecutive win/loss streaks from the raw trade list. PnL is the source of
	 * truth for win/loss classification; expectancy is the average pnl per trade.
	 */
	function computeTradeSummary(trades: Trade[] | null | undefined): TradeSummary | null {
		if (!Array.isArray(trades) || trades.length === 0) return null;

		let wins = 0;
		let losses = 0;
		let breakeven = 0;
		let grossWin = 0;
		let grossLoss = 0;
		let pnlSum = 0;
		let largestWin = 0;
		let largestLoss = 0;
		let longestWinStreak = 0;
		let longestLossStreak = 0;
		let currentWinStreak = 0;
		let currentLossStreak = 0;

		for (const trade of trades) {
			const pnl = asNumber(trade?.pnl, 0);
			pnlSum += pnl;
			if (pnl > 0) {
				wins += 1;
				grossWin += pnl;
				if (pnl > largestWin) largestWin = pnl;
				currentWinStreak += 1;
				currentLossStreak = 0;
				if (currentWinStreak > longestWinStreak) longestWinStreak = currentWinStreak;
			} else if (pnl < 0) {
				losses += 1;
				grossLoss += Math.abs(pnl);
				if (pnl < largestLoss) largestLoss = pnl;
				currentLossStreak += 1;
				currentWinStreak = 0;
				if (currentLossStreak > longestLossStreak) longestLossStreak = currentLossStreak;
			} else {
				breakeven += 1;
				currentWinStreak = 0;
				currentLossStreak = 0;
			}
		}

		const total = trades.length;
		const avgWin = wins > 0 ? grossWin / wins : 0;
		const avgLoss = losses > 0 ? grossLoss / losses : 0;
		const payoffRatio = avgLoss > 0 ? avgWin / avgLoss : null;

		return {
			total,
			wins,
			losses,
			breakeven,
			winRatePct: total > 0 ? (wins / total) * 100 : 0,
			avgWin,
			avgLoss,
			payoffRatio,
			largestWin,
			largestLoss,
			expectancy: total > 0 ? pnlSum / total : 0,
			longestWinStreak,
			longestLossStreak,
		};
	}

	type RiskMetricEntry = {
		label: string;
		value: string;
		title: string;
		tone: 'neutral' | 'positive' | 'negative';
	};

	function formatRatioMetric(value: number): string {
		return value.toFixed(2);
	}

	/**
	 * Build the risk-adjusted metrics grid, surfacing every backend metric not
	 * already shown in the headline strip. Undefined metrics are silently omitted.
	 */
	function buildRiskMetricEntries(result: BacktestResult | null): RiskMetricEntry[] {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return [];
		const entries: RiskMetricEntry[] = [];

		const pushRatio = (
			label: string,
			title: string,
			value: number | null,
			tone: 'neutral' | 'positive' = 'neutral',
		) => {
			if (value === null || !Number.isFinite(value)) return;
			entries.push({ label, title, value: formatRatioMetric(value), tone: tone === 'positive' ? (value >= 0 ? 'positive' : 'negative') : 'neutral' });
		};

		const pushPercent = (
			label: string,
			title: string,
			value: number | null,
			signed: boolean,
		) => {
			if (value === null || !Number.isFinite(value)) return;
			const tone: RiskMetricEntry['tone'] = signed ? (value >= 0 ? 'positive' : 'negative') : 'neutral';
			entries.push({ label, title, value: `${value.toFixed(2)}%`, tone });
		};

		pushRatio('Sortino', 'Downside-deviation-adjusted return (higher is better).', readResultMetricOptional(result, 'sortino_ratio'), 'positive');
		pushRatio('Calmar', 'Annualized return / max drawdown (higher is better).', readResultMetricOptional(result, 'calmar_ratio'), 'positive');
		pushRatio('Omega', 'Probability-weighted gains / losses about a threshold (>1 favorable).', readResultMetricOptional(result, 'omega_ratio'), 'positive');
		pushRatio('Tail Ratio', 'Right-tail / left-tail magnitude (>1 favorable).', readResultMetricOptional(result, 'tail_ratio'), 'positive');
		pushRatio('Recovery Factor', 'Net profit / max drawdown (higher is better).', readResultMetricOptional(result, 'recovery_factor'), 'positive');
		pushRatio('Edge Ratio', 'Average MFE / average MAE (>1 favorable).', readResultMetricOptional(result, 'edge_ratio'), 'positive');
		pushPercent('VaR', 'Value at Risk — expected loss at the modeled confidence level.', readResultMetricOptional(result, 'value_at_risk'), false);
		pushPercent('Exp. Shortfall', 'Expected shortfall (average loss beyond VaR).', readResultMetricOptional(result, 'expected_shortfall'), false);
		pushRatio('Expectancy', 'Average expected outcome per trade (return units).', readResultMetricOptional(result, 'expectancy'), 'positive');
		pushPercent('Avg MAE', 'Average maximum adverse excursion across trades.', readResultMetricOptional(result, 'avg_mae'), false);
		pushPercent('Avg MFE', 'Average maximum favorable excursion across trades.', readResultMetricOptional(result, 'avg_mfe'), false);
		pushRatio('Beta', 'Sensitivity to the benchmark (1 = moves with market).', readResultMetricOptional(result, 'beta'));
		pushPercent('Alpha', 'Excess return over the benchmark.', readResultMetricOptional(result, 'alpha'), true);
		pushPercent('Monthly Ret', 'Average monthly return.', readResultMetricOptional(result, 'monthly_return_pct'), true);

		const maxDdDuration = readResultMetricOptional(result, 'max_drawdown_duration');
		if (maxDdDuration !== null && Number.isFinite(maxDdDuration)) {
			entries.push({ label: 'Max DD Days', title: 'Longest drawdown duration (days).', value: Math.round(maxDdDuration).toLocaleString(), tone: 'negative' });
		}
		const avgDdDuration = readResultMetricOptional(result, 'avg_drawdown_duration');
		if (avgDdDuration !== null && Number.isFinite(avgDdDuration)) {
			entries.push({ label: 'Avg DD Days', title: 'Average drawdown duration (days).', value: Math.round(avgDdDuration).toLocaleString(), tone: 'neutral' });
		}
		const avgTradeDuration = readResultMetricOptional(result, 'avg_trade_duration');
		if (avgTradeDuration !== null && Number.isFinite(avgTradeDuration)) {
			entries.push({ label: 'Avg Hold', title: 'Average trade duration (bars).', value: avgTradeDuration.toFixed(1), tone: 'neutral' });
		}

		return entries;
	}

	function riskMetricToneClass(tone: RiskMetricEntry['tone']): string {
		if (tone === 'positive') return 'text-emerald-400';
		if (tone === 'negative') return 'text-red-400';
		return 'text-gray-300';
	}

	function formatSignedCurrency(value: number): string {
		const sign = value >= 0 ? '+' : '-';
		return `${sign}$${Math.abs(value).toFixed(2)}`;
	}

	const EXIT_REASON_LABELS: Record<string, string> = {
		signal: 'Signal',
		stop_loss: 'Stop',
		take_profit: 'Target',
		trailing_stop: 'Trail',
		time_stop: 'Time',
	};

	// exit_reason / size_fraction are emitted by the engine but not yet declared
	// on the Trade type (lib/api is read-only here), so read them defensively.
	function tradeExitReason(trade: Trade): string | null {
		const raw = (trade as unknown as Record<string, unknown>).exit_reason;
		if (typeof raw !== 'string' || !raw.trim()) return null;
		const key = raw.trim().toLowerCase();
		return EXIT_REASON_LABELS[key] ?? raw.trim();
	}

	function tradeSizeFraction(trade: Trade): number | null {
		const raw = (trade as unknown as Record<string, unknown>).size_fraction;
		const value = asNumber(raw, Number.NaN);
		return Number.isFinite(value) ? value : null;
	}

	$: selectedTradesHaveExitReason = (selectedResult?.trades ?? []).some((trade) => tradeExitReason(trade) !== null);
	$: selectedTradesHaveSizeFraction = (selectedResult?.trades ?? []).some((trade) => tradeSizeFraction(trade) !== null);
	$: selectedTradeColumnCount = 11
		+ (selectedTradesHaveExitReason ? 1 : 0)
		+ (selectedTradesHaveSizeFraction ? 1 : 0);

	function isResultCagrReliable(result: BacktestResult | null): boolean {
		const flag = readResultFlag(result, 'annualized_return_reliable');
		if (flag !== null) return flag;
		const months = readResultMetricOptional(result, 'backtest_months');
		return months === null ? true : months >= 1;
	}

	function isResultSharpeReliable(result: BacktestResult | null): boolean {
		const flag = readResultFlag(result, 'sharpe_is_reliable');
		if (flag !== null) return flag;
		const trades = readResultMetricOptional(result, 'total_trades', 'trades');
		return trades === null ? true : trades >= 20;
	}

	function formatResultProfitFactor(result: BacktestResult | null): string {
		if (readResultFlag(result, 'profit_factor_is_infinite') === true) return '∞';
		const value = readResultMetricOptional(result, 'profit_factor', 'pf');
		if (value === null || !Number.isFinite(value)) return '∞';
		return value.toFixed(2);
	}

	function formatResultCagr(result: BacktestResult | null): string {
		const value = readResultPercentMetricOptional(result, 'annualized_return_pct');
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatResultSharpe(result: BacktestResult | null): string {
		const value = readResultMetricOptional(result, 'sharpe_ratio', 'sharpe');
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function readResultNestedRecord(result: BacktestResult | null, parentKey: string): Record<string, unknown> {
		if (!result || !result.metrics || typeof result.metrics !== 'object') return {};
		const nested = (result.metrics as Record<string, unknown>)[parentKey];
		if (nested && typeof nested === 'object' && !Array.isArray(nested)) return nested as Record<string, unknown>;
		return {};
	}

	function matchingHistoryItemForResult(result: BacktestResult | null): StrategyContainerHistoryItem | null {
		if (!result) return null;
		const resultRecord = result as unknown as Record<string, unknown>;
		const id = String(resultRecord.id ?? resultRecord.result_id ?? selectedResultId ?? '').trim();
		if (!id) return null;
		return backtestHistory.find((h) => String(h.result_id) === id) ?? null;
	}

	function readResultInSampleCagr(result: BacktestResult | null): number | null {
		const top = readResultPercentMetricOptional(result, 'in_sample_annualized_return_pct', 'is_annualized_return_pct');
		if (top !== null) return top;
		const nested = readResultNestedRecord(result, 'in_sample');
		const raw = asNumber(nested['annualized_return_pct'], Number.NaN);
		if (Number.isFinite(raw)) return Math.abs(raw) <= 1 ? raw * 100 : raw;
		const overall = readResultPercentMetricOptional(result, 'annualized_return_pct');
		if (overall !== null) return overall;
		const match = matchingHistoryItemForResult(result);
		return match ? (readInSampleCagr(match) ?? readPercentMetricOptional(match, 'annualized_return_pct')) : null;
	}

	function readResultInSampleSharpe(result: BacktestResult | null): number | null {
		const top = readResultMetricOptional(result, 'in_sample_sharpe', 'is_sharpe', 'is_sharpe_ratio');
		if (top !== null) return top;
		const nested = readResultNestedRecord(result, 'in_sample');
		const raw = asNumber(nested['sharpe_ratio'] ?? nested['sharpe'], Number.NaN);
		if (Number.isFinite(raw)) return raw;
		const overall = readResultMetricOptional(result, 'sharpe_ratio', 'sharpe');
		if (overall !== null) return overall;
		const match = matchingHistoryItemForResult(result);
		return match ? (readInSampleSharpe(match) ?? readMetricOptional(match, 'sharpe_ratio', 'sharpe')) : null;
	}

	function readResultOutOfSampleCagr(result: BacktestResult | null): number | null {
		const top = readResultPercentMetricOptional(result, 'out_of_sample_annualized_return_pct', 'oos_annualized_return_pct');
		if (top !== null) return top;
		const nested = readResultNestedRecord(result, 'out_of_sample');
		const raw = asNumber(nested['annualized_return_pct'], Number.NaN);
		if (Number.isFinite(raw)) return Math.abs(raw) <= 1 ? raw * 100 : raw;
		const overall = readResultPercentMetricOptional(result, 'annualized_return_pct');
		if (overall !== null) return overall;
		const match = matchingHistoryItemForResult(result);
		return match ? (readOutOfSampleCagr(match) ?? readPercentMetricOptional(match, 'annualized_return_pct')) : null;
	}

	function readResultOutOfSampleSharpe(result: BacktestResult | null): number | null {
		const top = readResultMetricOptional(result, 'out_of_sample_sharpe', 'oos_sharpe', 'oos_sharpe_ratio');
		if (top !== null) return top;
		const nested = readResultNestedRecord(result, 'out_of_sample');
		const raw = asNumber(nested['sharpe_ratio'] ?? nested['sharpe'], Number.NaN);
		if (Number.isFinite(raw)) return raw;
		const overall = readResultMetricOptional(result, 'sharpe_ratio', 'sharpe');
		if (overall !== null) return overall;
		const match = matchingHistoryItemForResult(result);
		return match ? (readOutOfSampleSharpe(match) ?? readMetricOptional(match, 'sharpe_ratio', 'sharpe')) : null;
	}

	function readResultRobustness(result: BacktestResult | null): number | null {
		const raw = readResultMetricOptional(result, 'composite_robustness_score', 'robustness_score', 'robustness', 'gauntlet_score');
		if (raw !== null) return Math.abs(raw) <= 1 ? raw * 100 : raw;
		const match = matchingHistoryItemForResult(result);
		return match ? readRobustness(match) : null;
	}

	function formatResultInSampleCagr(result: BacktestResult | null): string {
		const value = readResultInSampleCagr(result);
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatResultInSampleSharpe(result: BacktestResult | null): string {
		const value = readResultInSampleSharpe(result);
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function formatResultOutOfSampleCagr(result: BacktestResult | null): string {
		const value = readResultOutOfSampleCagr(result);
		if (value === null) return '-';
		return `${value.toFixed(2)}%`;
	}

	function formatResultOutOfSampleSharpe(result: BacktestResult | null): string {
		const value = readResultOutOfSampleSharpe(result);
		if (value === null) return '-';
		return value.toFixed(2);
	}

	function formatResultRobustness(result: BacktestResult | null): string {
		const value = readResultRobustness(result);
		if (value === null) return '-';
		return `${value.toFixed(1)}%`;
	}

	function formatResultTradesCount(result: BacktestResult | null): string {
		const trades = readResultMetricOptional(result, 'total_trades', 'trades', 'trade_count');
		if (trades === null) return '-';
		return Math.round(trades).toString();
	}

	function resultStatus(result: BacktestResult | null): 'running' | 'succeeded' | 'failed' {
		if (!result) return 'running';
		return normalizeResultStatus(result.status ?? result.config?.status ?? result.metrics?.status);
	}

	function resultErrorDetail(result: BacktestResult | null): string | undefined {
		if (!result) return undefined;
		const error = String(result.error ?? result.config?.error ?? result.metrics?.error ?? '').trim();
		if (error) return error;
		if (resultStatus(result) === 'failed') return 'Run failed before an error message was persisted.';
		return undefined;
	}

	function resultHasUsableMetrics(result: BacktestResult | null): boolean {
		const status = resultStatus(result);
		if (status === 'succeeded') return true;
		if (status === 'failed') return false;
		if (!result || !result.metrics || typeof result.metrics !== 'object') return false;
		const metricKeys = [
			'annualized_return_pct',
			'total_return_pct',
			'total_return',
			'sharpe_ratio',
			'sharpe',
			'max_drawdown_pct',
			'max_drawdown',
			'win_rate',
			'total_trades',
			'trades',
			'profit_factor',
		];
		return metricKeys.some((key) => Number.isFinite(asNumber(result.metrics?.[key], Number.NaN)));
	}

	function toComparableResultMetrics(result: BacktestResult): ComparableRunMetrics {
		return {
			id: String(result.id || ''),
			resultType: String(result.result_type || 'backtest'),
			annualizedReturnPct: readResultPercentMetricOptional(result, 'annualized_return_pct'),
			totalReturnPct: readResultPercentMetric(result, 'total_return_pct', 'total_return'),
			sharpe: readResultMetric(result, 'sharpe_ratio', 'sharpe'),
			maxDrawdownPct: readResultDrawdownPercentMetricOptional(result, 'max_drawdown_pct', 'max_drawdown'),
			winRatePct: readResultPercentMetricOptional(result, 'win_rate', 'win_rate_pct'),
			trades: Math.round(readResultMetric(result, 'total_trades', 'trades')),
			profitFactor: readResultMetric(result, 'profit_factor', 'pf'),
		};
	}

	function toComparableHistoryMetrics(item: StrategyContainerHistoryItem): ComparableRunMetrics {
		return {
			id: String(item.result_id || ''),
			resultType: String(item.result_type || 'backtest'),
			annualizedReturnPct: readPercentMetricOptional(item, 'annualized_return_pct'),
			totalReturnPct: readPercentMetric(item, 'total_return_pct', 'total_return', 'pnl_pct'),
			sharpe: readMetric(item, 'sharpe_ratio', 'sharpe'),
			maxDrawdownPct: readDrawdownPercentMetricOptional(item, 'max_drawdown_pct', 'max_drawdown'),
			winRatePct: readPercentMetricOptional(item, 'win_rate', 'win_rate_pct'),
			trades: Math.round(readMetric(item, 'total_trades', 'trades')),
			profitFactor: readMetric(item, 'profit_factor', 'pf'),
		};
	}

	function resultTypeBadge(type: string): string {
		const normalized = String(type || '').toLowerCase();
		if (normalized === 'optimization') return 'text-blue-300 border-blue-700 bg-blue-900/20';
		if (normalized === 'walk_forward') return 'text-violet-300 border-violet-700 bg-violet-900/20';
		return 'text-emerald-300 border-emerald-700 bg-emerald-900/20';
	}

	function resultTypeLabel(type: string | null | undefined): string {
		const normalized = String(type ?? '').trim().toLowerCase();
		if (normalized === 'optimization') return 'Optimization';
		if (normalized === 'walk_forward') return 'Walk-forward';
		if (normalized === 'grid_search') return 'Grid search';
		return 'Gauntlet';
	}

	function comparisonDeltaLabel(
		value: number | null,
		options: { inverse?: boolean; suffix?: string } = {},
	): string {
		const { inverse = false, suffix = '' } = options;
		if (value === null || !Number.isFinite(value)) return '--';
		const adjusted = inverse ? value * -1 : value;
		const sign = adjusted > 0 ? '+' : '';
		return `${sign}${adjusted.toFixed(2)}${suffix}`;
	}

	function comparisonDeltaClass(
		value: number | null,
		options: { inverse?: boolean } = {},
	): string {
		const { inverse = false } = options;
		if (value === null || !Number.isFinite(value)) return 'text-gray-500';
		const adjusted = inverse ? value * -1 : value;
		if (adjusted > 0) return 'text-emerald-400';
		if (adjusted < 0) return 'text-red-400';
		return 'text-gray-300';
	}

	function historyCardBorder(type: string | null | undefined): string {
		const normalized = String(type ?? '').trim().toLowerCase();
		if (normalized === 'optimization') return 'hover:border-blue-700/60';
		if (normalized === 'walk_forward') return 'hover:border-violet-700/60';
		return 'hover:border-cyan-700/60';
	}


	function parseProgressPct(progress: string): number | null {
		const match = /(\d{1,3}(?:\.\d+)?)\s*%/.exec(progress);
		if (!match) return null;
		const parsed = Number(match[1]);
		if (!Number.isFinite(parsed)) return null;
		return Math.max(0, Math.min(100, parsed));
	}

	function resetSubmitProgress() {
		submitProgress = '';
		submitPollingStatus = '';
		submitPollCount = 0;
	}

	function validateDateRange(start: string, end: string): string | null {
		const startIso = toIsoDate(start);
		const endIso = toIsoDate(end);
		if (start && !startIso) return 'Start date is invalid.';
		if (end && !endIso) return 'End date is invalid.';
		if (startIso && endIso && new Date(startIso) > new Date(endIso)) {
			return 'Start date must be on or before end date.';
		}
		return null;
	}

	function validateBacktestForm(): string | null {
		if (!container) return 'Strategy container is not loaded.';
		if (!container.strategy.id) return 'Container has no strategy_id.';
		if (!backtestForm.symbol.trim()) return 'Symbol is required.';
		if (!backtestForm.timeframe.trim()) return 'Timeframe is required.';
		return validateDateRange(backtestForm.start_date, backtestForm.end_date);
	}

	function validateOptimizationForm(): string | null {
		if (!container) return 'Strategy container is not loaded.';
		if (!container.strategy.id) return 'Container has no strategy_id.';
		if (!optimizationForm.symbol.trim()) return 'Symbol is required.';
		if (!optimizationForm.timeframe.trim()) return 'Timeframe is required.';
		if (!optimizationForm.objective.trim()) return 'Objective is required.';
		if (!Number.isFinite(optimizationForm.n_trials) || optimizationForm.n_trials < 1) {
			return 'Trials must be at least 1.';
		}
		return validateDateRange(optimizationForm.start_date, optimizationForm.end_date);
	}

	function isOptimizableNumericParam(value: unknown): value is number {
		// A param defaulting to exactly 0 (e.g. an off-by-default threshold/stop) is still
		// optimizable — createOptimizationParamDraft already seeds a valid range via its
		// min===max step fallback, so there is no reason to hide it from the sweep list.
		return typeof value === 'number' && Number.isFinite(value);
	}

	function optimizationParamKind(value: number): OptimizationParamKind {
		return Number.isInteger(value) ? 'int' : 'float';
	}

	function roundOptimizationValue(value: number, kind: OptimizationParamKind): number {
		if (kind === 'int') return Math.round(value);
		return Number(value.toFixed(6));
	}

	function formatOptimizationInputValue(value: number, kind: OptimizationParamKind): string {
		if (kind === 'int') return String(Math.round(value));
		return String(Number(value.toFixed(6)));
	}

	function defaultOptimizationStep(value: number, kind: OptimizationParamKind): number {
		const magnitude = Math.abs(value);
		if (kind === 'int') {
			return Math.max(1, Math.round(magnitude >= 20 ? magnitude * 0.1 : 1));
		}
		if (magnitude >= 10) return 0.5;
		if (magnitude >= 1) return 0.1;
		if (magnitude >= 0.1) return 0.01;
		return 0.001;
	}

	function createOptimizationParamDraft(key: string, value: number): OptimizationParamDraft {
		const kind = optimizationParamKind(value);
		const delta = Math.abs(value) * 0.2;
		const step = defaultOptimizationStep(value, kind);
		let min = roundOptimizationValue(value - delta, kind);
		let max = roundOptimizationValue(value + delta, kind);
		if (min === max) {
			min = roundOptimizationValue(value - step, kind);
			max = roundOptimizationValue(value + step, kind);
		}
		// A param defaulting to exactly 0 is usually a non-negative threshold/count; seed
		// the lower bound at 0 rather than going negative (the user can widen it manually).
		if (value === 0 && min < 0) {
			min = 0;
		}
		return {
			key,
			current: value,
			kind,
			selected: false,
			min: formatOptimizationInputValue(min, kind),
			max: formatOptimizationInputValue(max, kind),
			step: formatOptimizationInputValue(step, kind),
			error: '',
		};
	}

	function syncOptimizationParamDrafts(params: Record<string, unknown>) {
		const optimizableEntries = Object.entries(params)
			.filter(([, value]) => isOptimizableNumericParam(value))
			.map(([key, value]) => [key, Number(value)] as const);
		const source = stableStringify(Object.fromEntries(optimizableEntries));
		if (source === optimizationParamDraftSource) return;
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		for (const [key, value] of optimizableEntries) {
			const existing = optimizationParamDrafts[key];
			const kind = optimizationParamKind(value);
			if (existing && existing.current === value && existing.kind === kind) {
				nextDrafts[key] = existing;
				continue;
			}
			nextDrafts[key] = createOptimizationParamDraft(key, value);
		}
		optimizationParamDrafts = nextDrafts;
		optimizationParamDraftSource = source;
	}

	function parseOptimizationParamValue(rawValue: string): number | null {
		if (!rawValue.trim()) return null;
		const parsed = Number(rawValue);
		return Number.isFinite(parsed) ? parsed : null;
	}

	function validateOptimizationParamDraft(draft: OptimizationParamDraft): string {
		const min = parseOptimizationParamValue(draft.min);
		const max = parseOptimizationParamValue(draft.max);
		const step = parseOptimizationParamValue(draft.step);
		if (min === null) return 'Minimum is required.';
		if (max === null) return 'Maximum is required.';
		if (step === null) return 'Step is required.';
		if (min > max) return 'Minimum must be on or before maximum.';
		if (step <= 0) return 'Step must be greater than zero.';
		if (draft.kind === 'int' && (!Number.isInteger(min) || !Number.isInteger(max) || !Number.isInteger(step))) {
			return 'Whole-number params require whole-number min, max, and step.';
		}
		return '';
	}

	function setOptimizationParamSelected(key: string, selected: boolean) {
		const draft = optimizationParamDrafts[key];
		if (!draft) return;
		optimizationParamDrafts = {
			...optimizationParamDrafts,
			[key]: {
				...draft,
				selected,
				error: selected ? validateOptimizationParamDraft(draft) : '',
			},
		};
	}

	function updateOptimizationParamField(key: string, field: 'min' | 'max' | 'step', value: string) {
		const draft = optimizationParamDrafts[key];
		if (!draft) return;
		const nextDraft = {
			...draft,
			[field]: value,
		};
		optimizationParamDrafts = {
			...optimizationParamDrafts,
			[key]: {
				...nextDraft,
				error: nextDraft.selected ? validateOptimizationParamDraft(nextDraft) : '',
			},
		};
	}

	function collectOptimizationParameterRanges(): {
		error: string | null;
		parameterRanges?: Record<string, { min: number; max: number; step: number }>;
	} {
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		const parameterRanges: Record<string, { min: number; max: number; step: number }> = {};
		let firstError: string | null = null;
		for (const [key, draft] of Object.entries(optimizationParamDrafts)) {
			let error = '';
			if (draft.selected) {
				error = validateOptimizationParamDraft(draft);
				if (!error) {
					parameterRanges[key] = {
						min: Number(draft.min),
						max: Number(draft.max),
						step: Number(draft.step),
					};
				}
			}
			if (error && !firstError) firstError = error;
			nextDrafts[key] = {
				...draft,
				error,
			};
		}
		optimizationParamDrafts = nextDrafts;
		return {
			error: firstError,
			parameterRanges: Object.keys(parameterRanges).length > 0 ? parameterRanges : undefined,
		};
	}

	function optimizationParamCurrentLabel(value: number, kind: OptimizationParamKind): string {
		return formatOptimizationInputValue(value, kind);
	}


	function goBack() {
		goto(returnTo);
	}

	function exportToTradingView(): void {
		if (!container) return;
		try {
			const exportBundle = buildTradingViewExport(container);
			tradingViewExportScript = exportBundle.pine;
			tradingViewExportFilename = exportBundle.filename;
			tradingViewExportWarnings = exportBundle.warnings;
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'TradingView export failed', 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		}
	}

	function closeTradingViewExport(): void {
		tradingViewExportScript = '';
		tradingViewExportFilename = '';
		tradingViewExportWarnings = [];
	}

	async function confirmPromotion(override = false): Promise<void> {
		if (!nextPipelineStage) return;
		const target = nextPipelineStage;
		promoting = true;
		try {
			await promoteForvenStrategy(strategyId, target.key, {
				fromStatus: currentLifecycleStage,
				reason: promoteReason.trim() || (override ? 'Operator gate override' : 'Manual promotion from configuration tab'),
				force: true,
				override,
			});
			addToast(`Promoted ${strategyId} to ${target.label}`, 'success');
			showPromoteConfirm = false;
			promoteReason = '';
			promoteBlockReason = '';
			await loadContainer();
		} catch (err) {
			const msg = err instanceof Error ? err.message : 'Promotion failed';
			if (!override) {
				// The promotion gate rejected it. Surface the reason and let the
				// operator make an informed decision to override the gate.
				promoteBlockReason = msg;
			} else {
				addToast(msg, 'error');
			}
		} finally {
			promoting = false;
		}
	}

	async function archiveStrategyFromConfig(): Promise<void> {
		if (typeof window !== 'undefined' && !window.confirm(`Archive strategy ${strategyId}?`)) return;
		promoting = true;
		try {
			await promoteForvenStrategy(strategyId, 'archived', {
				fromStatus: currentLifecycleStage,
				reason: 'Archived from configuration tab',
				force: true,
			});
			addToast(`${strategyId} archived`, 'success');
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Archive failed', 'error');
		} finally {
			promoting = false;
		}
	}

	async function openResult(item: StrategyContainerHistoryItem) {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		selectedResultId = resultId;
		selectedResultItem = item;
		selectedResult = null;
		selectedChartContext = null;
		resultError = '';
		chartContextError = '';
		resultLoading = true;
		chartLoading = false;
		try {
			const resultResponse = await getResult(resultId);
			if (selectedResultId !== resultId) return;
			selectedResult = resultResponse;
		} catch (err) {
			selectedChartContext = null;
			chartLoading = false;
			resultError = err instanceof Error ? err.message : 'Failed to load backtest result';
			addToast(resultError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			if (selectedResultId === resultId) {
				resultLoading = false;
			}
		}

		if (selectedResultId !== resultId || !selectedResult || !resultHasUsableMetrics(selectedResult)) return;

		chartLoading = true;
		try {
			const chartContext = await withTimeout(
				getResultChartContext(resultId),
				RESULT_CHART_TIMEOUT_MS,
				'Chart reconstruction is taking longer than expected. Result details are still available below.',
			);
			if (selectedResultId !== resultId) return;
			selectedChartContext = chartContext;
			chartFitContentToken += 1;
		} catch (err) {
			if (selectedResultId !== resultId) return;
			chartContextError = err instanceof Error ? err.message : 'Failed to load chart context';
		} finally {
			if (selectedResultId === resultId) {
				chartLoading = false;
			}
		}
	}

	async function trashResult(e: Event, item: StrategyContainerHistoryItem) {
		e.stopPropagation();
		const rid = String(item.result_id || '').trim();
		if (!rid) return;
		// Soft-delete (recoverable within the retention window) — but it's still a
		// destructive single-click in a dense row, so confirm like the page's other
		// destructive actions. Warn extra if this run is the pinned/active default.
		const isPinned = pinnedBacktestId && pinnedBacktestId === rid;
		const prompt = isPinned
			? `Move backtest ${rid} to trash? This run is the active default driving paper/live display — consider setting a different default first. You can recover it within the retention window.`
			: `Move backtest ${rid} to trash? You can recover it within the retention window.`;
		if (typeof window !== 'undefined' && !window.confirm(prompt)) return;
		try {
			await deleteResult(rid);
			addToast(`Moved ${rid} to trash`, 'success');
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Delete failed', 'error');
		}
	}

	function getOptBestParams(): Record<string, unknown> | null {
		if (!selectedResult) return null;
		// Check metrics.best_params first (ChromaDB enrichment), then config.params (SQLite)
		const bp = selectedResult.metrics?.best_params;
		if (bp && typeof bp === 'object' && Object.keys(bp).length > 0) return bp as Record<string, unknown>;
		const cp = selectedResult.config?.params;
		if (cp && typeof cp === 'object' && Object.keys(cp).length > 0) return cp as Record<string, unknown>;
		return null;
	}

	function getEffectiveOptimizationParams(): Record<string, unknown> | null {
		if (!selectedResult) return null;
		const bestParams = getOptBestParams();
		if (!bestParams) return null;
		const baseParams = extractParamRecord(selectedResult.config?.base_params);
		return {
			...baseParams,
			...cloneParameterRecord(bestParams),
		};
	}

	function isOptimizationResult(): boolean {
		return selectedResult?.result_type === 'optimization' && getOptBestParams() !== null;
	}

	async function backtestWithOptParams() {
		if (!container || !selectedResult || backtestingOptParams) return;
		const effectiveParams = getEffectiveOptimizationParams();
		if (!effectiveParams) return;
		backtestingOptParams = true;
		try {
			// Re-run over the SAME window the optimization used (stored on the result
			// config) so the "Gauntlet With Params" metrics are comparable to the
			// optimization shown beside it — not a default rolling window. preserve_result
			// keeps an intentional user rerun from being auto-trashed if it scores weakly.
			const optConfig = (selectedResult.config ?? {}) as Record<string, unknown>;
			const optStart = typeof optConfig.start === 'string' ? optConfig.start : '';
			const optEnd = typeof optConfig.end === 'string' ? optConfig.end : '';
			const response = await submitBacktest({
				strategy_id: container.strategy.id,
				strategy_name: container.strategy.name,
				symbol: selectedResult.symbol || backtestForm.symbol,
				timeframe: selectedResult.timeframe || backtestForm.timeframe,
				start: optStart || toIsoDate(backtestForm.start_date),
				end: optEnd || toIsoDate(backtestForm.end_date),
				params: effectiveParams,
				definition_json: getResultDefinitionJson(selectedResult),
				preserve_result: true,
			});
			if (response.status === 'succeeded') {
				addToast('Gauntlet with optimized params completed', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await loadContainer({ autoOpenLatestBacktest: true });
			} else {
				submitJobId = response.job_id;
				addToast('Gauntlet with optimized params queued', 'info', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await pollJobUntilComplete(response.job_id, 'backtest');
			}
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Gauntlet failed', 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			backtestingOptParams = false;
		}
	}

	async function setAsDefaultParams() {
		if (!container || settingDefaultParams) return;
		const effectiveParams = getEffectiveOptimizationParams();
		if (!effectiveParams) return;
		const confirmed = typeof window === 'undefined' || window.confirm(
			'Set these optimized parameters as the strategy defaults?\n\nThis will update the parameters used for paper trading and live execution.'
		);
		if (!confirmed) return;
		settingDefaultParams = true;
		try {
			await updateStrategyDefaultParams(container.strategy.id, effectiveParams, { pinnedBacktestId: null });
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Failed to update params', 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			settingDefaultParams = false;
		}
	}

	async function setBacktestRowAsDefault(item: StrategyContainerHistoryItem): Promise<void> {
		if (!container || settingDefaultParams) return;
		const params = getBacktestParamDraft(item);
		if (Object.keys(params).length === 0) return;
		const confirmed = typeof window === 'undefined' || window.confirm(
			'Set this backtest as the strategy default?\n\nIts parameters will drive paper trading and live execution, and its metrics will display on the Lab manager.'
		);
		if (!confirmed) return;
		settingDefaultParams = true;
		try {
			await updateStrategyDefaultParams(container.strategy.id, params, {
				pinnedBacktestId: item.result_id
			});
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Failed to update params', 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			settingDefaultParams = false;
		}
	}

	function resetParameterDraft(): void {
		paramsDraft = cloneParameterRecord(strategyParams);
		parameterSaveMessage = '';
		parameterSaveError = '';
	}

	function loadBacktestParamsIntoDraft(item: StrategyContainerHistoryItem): void {
		paramsDraft = getBacktestParamDraft(item);
		parameterSaveMessage = '';
		parameterSaveError = '';
		addToast(`Loaded parameters from ${item.result_id} into the working draft.`, 'info', `/lab/strategy/${encodeURIComponent(strategyId)}`);
	}

	async function saveParameterDraft(): Promise<void> {
		if (!container || settingDefaultParams || !paramsDirty) return;
		if (paramsHasErrors) {
			addToast('Fix the highlighted parameter errors before saving.', 'error');
			return;
		}
		settingDefaultParams = true;
		parameterSaveMessage = '';
		parameterSaveError = '';
		try {
			await updateStrategyDefaultParams(container.strategy.id, paramsDraft, { pinnedBacktestId: null });
			parameterSaveMessage = 'Default parameters saved.';
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			await loadContainer();
		} catch (err) {
			parameterSaveError = err instanceof Error ? err.message : 'Failed to save parameter defaults';
			addToast(parameterSaveError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			settingDefaultParams = false;
		}
	}

	async function loadContainer(options: { autoOpenLatestBacktest?: boolean } = {}) {
		if (!strategyId) return;
		const loadSequence = ++prebuiltLoadSequence;
		loading = true;
		error = '';
		selectedResult = null;
		selectedResultId = null;
		selectedResultItem = null;
		selectedChartContext = null;
		resultError = '';
		chartContextError = '';
		resultLoading = false;
		// Clear per-test robustness overrides so a run on the PREVIOUS strategy can't leak
		// PASS/FAIL tiles onto this one (the SPA reuses this component across [id] changes).
		robustnessStatusOverrides = {};
		let nextSelectedBacktest: StrategyContainerHistoryItem | null = null;
		let loadSucceeded = false;
		try {
			const [payload, datasetCatalog, nextPipelineSettings, nextPipelineThresholds] = await Promise.all([
				getStrategyContainer(strategyId, { result_limit: 300, trade_limit: 500 }),
				getDatasets().catch(() => []),
				getPipelineSettings().catch(() => null),
				getPipelineConfig().catch(() => null),
			]);
			container = payload;
			availableDatasets = Array.isArray(datasetCatalog) ? datasetCatalog : [];
			pipelineSettings = nextPipelineSettings;
			pipelineThresholds = nextPipelineThresholds;

			const defaultSymbol = String(payload.configuration.symbol ?? payload.strategy.symbol ?? '').trim();
			const defaultTimeframe = String(payload.configuration.timeframe ?? payload.strategy.timeframe ?? '1h').trim() || '1h';
			const firstBacktest = payload.history.backtests[0];
			const resolvedStartDate = toDateInput(firstBacktest?.start_date) || defaultOneYearRange.startDate;
			const resolvedEndDate = toDateInput(firstBacktest?.end_date) || defaultOneYearRange.endDate;

			backtestForm = {
				symbol: defaultSymbol,
				timeframe: defaultTimeframe,
				start_date: resolvedStartDate,
				end_date: resolvedEndDate,
			};
			optimizationForm = {
				...optimizationForm,
				symbol: defaultSymbol,
				timeframe: defaultTimeframe,
				start_date: resolvedStartDate,
				end_date: resolvedEndDate,
			};
			paramsDraft = cloneParameterRecord(
				(payload.configuration.params && typeof payload.configuration.params === 'object' && !Array.isArray(payload.configuration.params))
					? (payload.configuration.params as Record<string, unknown>)
					: {},
			);
			backtestParamDrafts = buildBacktestParamDraftMap(payload.history.backtests);
			expandedBacktestParamsId = null;
			backtestParamRunnerId = null;
			parameterSaveMessage = '';
			parameterSaveError = '';
			if (options.autoOpenLatestBacktest) {
				nextSelectedBacktest = payload.history.backtests[0] ?? null;
			}
			loadSucceeded = true;
		} catch (err) {
			container = null;
			availableDatasets = [];
			pipelineSettings = null;
			prebuiltStrategies = [];
			availableParamSpecs = {};
			availableAddParamKeys = [];
			selectedAddParamKey = '';
			addParamHelperText = '';
			error = err instanceof Error ? err.message : 'Failed to load container';
		} finally {
			loading = false;
		}
		if (loadSucceeded && loadSequence === prebuiltLoadSequence && !destroyed) {
			void loadPrebuiltStrategies()
				.then((prebuiltResponse) => {
					if (destroyed || loadSequence !== prebuiltLoadSequence) return;
					prebuiltStrategies = Array.isArray(prebuiltResponse.strategies) ? prebuiltResponse.strategies : [];
				})
				.catch(() => {
					if (destroyed || loadSequence !== prebuiltLoadSequence) return;
					prebuiltStrategies = [];
				});
		}
		if (nextSelectedBacktest) {
			activeTab = 'backtests';
			await openResult(nextSelectedBacktest);
		}
	}

	async function pollJobUntilComplete(jobId: string, mode: 'backtest' | 'optimization') {
		// Capture the strategy this poll belongs to. The [id] route reuses this component
		// instance across param changes, so if the user navigates A->B mid-poll we must
		// NOT mutate B's view or toast under B's id when A's job finishes.
		const pollStrategyId = strategyId;
		const label = mode === 'backtest' ? 'Gauntlet' : 'Optimization';
		for (let attempt = 0; attempt < 240; attempt += 1) {
			if (destroyed || strategyId !== pollStrategyId) return;
			try {
				const job = await withTimeout(getJob(jobId), 10_000, 'poll timeout');
				if (destroyed || strategyId !== pollStrategyId) return;
				submitPollingStatus = job.status;
				submitProgress = String(job.progress ?? '').trim();
				submitPollCount = attempt + 1;
				if (job.status === 'succeeded') {
					submitStatus = 'completed';
					submitMessage = `${label} completed.`;
					addToast(`${label} completed`, 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
					await loadContainer({ autoOpenLatestBacktest: mode === 'backtest' });
					return;
				}
				if (job.status === 'failed') {
					submitStatus = 'failed';
					submitMessage = job.error || `${mode} failed`;
					addToast(submitMessage, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
					return;
				}
				if (job.status === 'cancelled') {
					submitStatus = 'failed';
					submitMessage = `${label} was cancelled.`;
					addToast(submitMessage, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
					return;
				}
				submitStatus = 'running';
				submitMessage = `${label} job ${jobId} is ${job.status}${submitProgress ? ` (${submitProgress})` : ''}...`;
			} catch {
				// Ignore transient poll error (timeout, network blip).
			}
			await new Promise((resolve) => setTimeout(resolve, attempt < 10 ? 2000 : 5000));
		}
		if (destroyed || strategyId !== pollStrategyId) return;
		submitStatus = 'failed';
		submitMessage = 'Job polling timed out.';
		addToast(submitMessage, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
	}

	async function executeBacktestSubmission(request: Parameters<typeof submitBacktest>[0]): Promise<void> {
		submitStatus = 'submitting';
		submitMessage = '';
		submitJobId = null;
		resetSubmitProgress();
		try {
			const response = await submitBacktest({ ...request, preserve_result: true });
			submitJobId = response.job_id;
			if (response.status === 'succeeded') {
				submitStatus = 'completed';
				submitMessage = `Gauntlet completed.`;
				addToast('Gauntlet completed', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await loadContainer({ autoOpenLatestBacktest: true });
			} else {
				submitStatus = 'running';
				submitMessage = `Gauntlet queued (${response.job_id}).`;
				addToast('Gauntlet queued', 'info', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await pollJobUntilComplete(response.job_id, 'backtest');
			}
		} catch (err) {
			submitStatus = 'failed';
			submitMessage = err instanceof Error ? err.message : 'Gauntlet submit failed';
			addToast(submitMessage, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		}
	}

	async function submitContainerBacktest() {
		if (!container) return;
		if (paramsHasErrors) {
			const message = 'Fix the highlighted parameter errors before running.';
			submitStatus = 'failed';
			submitMessage = message;
			resetSubmitProgress();
			addToast(message, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		const validationError = validateBacktestForm();
		if (validationError) {
			submitStatus = 'failed';
			submitMessage = validationError;
			resetSubmitProgress();
			addToast(validationError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		await executeBacktestSubmission({
			strategy_id: container.strategy.id,
			strategy_name: container.strategy.name,
			symbol: backtestForm.symbol,
			timeframe: backtestForm.timeframe,
			start: toIsoDate(backtestForm.start_date),
			end: toIsoDate(backtestForm.end_date),
			params: paramsDraft,
			definition_json: getContainerDefinitionJson(),
		});
	}

	async function rerunBacktestFromHistory(item: StrategyContainerHistoryItem): Promise<void> {
		if (!container || backtestParamRunnerId || submitStatus === 'submitting' || submitStatus === 'running') return;
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		backtestParamRunnerId = resultId;
		try {
			await executeBacktestSubmission({
				strategy_id: container.strategy.id,
				strategy_name: container.strategy.name,
				symbol: item.symbol || backtestForm.symbol,
				timeframe: item.timeframe || backtestForm.timeframe,
				start: item.start_date ?? undefined,
				end: item.end_date ?? undefined,
				params: getBacktestParamDraft(item),
				definition_json: getHistoryDefinitionJson(item),
			});
		} finally {
			backtestParamRunnerId = null;
		}
	}

	async function submitContainerOptimization() {
		if (!container) return;
		const validationError = validateOptimizationForm();
		if (validationError) {
			submitStatus = 'failed';
			submitMessage = validationError;
			resetSubmitProgress();
			addToast(validationError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		const { error: parameterRangeError, parameterRanges } = collectOptimizationParameterRanges();
		if (parameterRangeError) {
			submitStatus = 'failed';
			submitMessage = parameterRangeError;
			resetSubmitProgress();
			addToast(parameterRangeError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		submitStatus = 'submitting';
		submitMessage = '';
		submitJobId = null;
		resetSubmitProgress();
		try {
			const response = await submitOptimization({
				strategy_id: container.strategy.id,
				strategy_name: container.strategy.name,
				symbol: optimizationForm.symbol,
				timeframe: optimizationForm.timeframe,
				start: toIsoDate(optimizationForm.start_date),
				end: toIsoDate(optimizationForm.end_date),
				objective: optimizationForm.objective,
				n_trials: optimizationForm.n_trials,
				parameter_ranges: parameterRanges,
			});
			submitJobId = response.job_id;
			if (response.status === 'succeeded') {
				submitStatus = 'completed';
				submitMessage = `Optimization completed.`;
				addToast('Optimization completed', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await loadContainer();
			} else {
				submitStatus = 'running';
				submitMessage = `Optimization queued (${response.job_id}).`;
				addToast('Optimization queued', 'info', `/lab/strategy/${encodeURIComponent(strategyId)}`);
				await pollJobUntilComplete(response.job_id, 'optimization');
			}
		} catch (err) {
			submitStatus = 'failed';
			submitMessage = err instanceof Error ? err.message : 'Optimization submit failed';
			addToast(submitMessage, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		}
	}


	$: if (strategyId && strategyId !== lastLoadedId) {
		lastLoadedId = strategyId;
		void loadContainer();
	}

	function launchDeepdive() {
		if (!container) return;
		openDeepdive(container.strategy.id, container.strategy.name);
	}

	onDestroy(() => {
		destroyed = true;
	});
</script>

<svelte:head>
	<title>{container?.strategy.name ?? strategyId} · Lab</title>
</svelte:head>

<div class="h-full flex flex-col overflow-hidden">
	<div class="flex items-center gap-3 border-b border-[#222] bg-[#0b0b0b] px-4 py-2">
		<button
			type="button"
			class="text-xs text-gray-500 transition-colors hover:text-white"
			on:click={goBack}
		>
			Back
		</button>
		<span class="text-gray-700">|</span>
		{#if container}
			<StrategyLink strategyId={container.strategy.id} label={container.strategy.name} returnTo={returnTo} />
			{#if container.strategy.hypothesis_id}
				{@const hypothesisHrefId = container.strategy.hypothesis_display_id || container.strategy.hypothesis_id}
				{@const hypothesisLabelId = container.strategy.hypothesis_display_id || container.strategy.hypothesis_id}
				<span class="text-gray-700">|</span>
				<a href={`/hypotheses/${encodeURIComponent(hypothesisHrefId)}`} class="text-[11px] uppercase tracking-[0.18em] text-cyan-300 transition hover:text-cyan-200">
					Crucible {hypothesisLabelId}
				</a>
			{/if}
			<span class="text-gray-700">•</span>
			<span class="text-[11px] text-gray-400 uppercase">{String(container.configuration.stage ?? container.strategy.state ?? '-')}</span>
			{#if container.strategy.canonical}
				<span
					data-canonical-badge
					class="ml-1 border border-emerald-500/60 bg-emerald-950/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-100"
					title="Canonical: best-in-cell for this hypothesis; protected from cleanup"
				>
					Canonical
				</span>
			{/if}
			{#if container.strategy.parent_strategy_id}
				<span class="text-gray-700">•</span>
				<a
					href={`/lab/strategy/${encodeURIComponent(container.strategy.parent_strategy_id)}`}
					class="text-[10px] uppercase tracking-[0.18em] text-indigo-300 transition hover:text-indigo-200"
					title={`Iterated from ${container.strategy.parent_strategy_id}`}
				>
					⮐ Parent {container.strategy.parent_strategy_id}
				</a>
			{/if}
			<span class="ml-auto"></span>
			<button
				type="button"
				data-testid="export-tradingview-button"
				class="rounded border border-sky-700/50 bg-sky-950/30 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-200 transition hover:bg-sky-900/40"
				title="Show a Pine v6 strategy for TradingView verification"
				on:click={exportToTradingView}
			>
				Export to TradingView
			</button>
		{:else}
			<span class="text-xs text-gray-400 font-mono">{strategyId}</span>
		{/if}
		{#if submitJobId}
			<span class="ml-auto text-[10px] text-gray-500 font-mono">job: {submitJobId}</span>
		{/if}
	</div>

	{#if loading}
		<div class="flex-1 flex items-center justify-center">
			<div class="text-sm text-gray-500 animate-pulse">Loading container...</div>
		</div>
	{:else if error}
		<div class="flex-1 flex items-center justify-center">
			<div class="rounded border border-red-900 bg-red-950/20 px-4 py-3 text-sm text-red-300">{error}</div>
		</div>
	{:else if container}
		{#if container.strategy.id}
			<div class="border-b border-[#222] bg-[#070707] px-4 py-2">
				<BrainStrategyDecisionsCard strategyId={container.strategy.id} />
			</div>
		{/if}
		<div class="border-b border-[#222] bg-[#0a0a0a] px-4">
			<div role="group" aria-label="Strategy detail sections" class="flex gap-6 text-xs uppercase tracking-wide">
				<button
					type="button"
					aria-pressed={activeTab === 'configuration'}
					class="border-b-2 py-2 transition-colors {activeTab === 'configuration' ? 'border-white text-white' : 'border-transparent text-gray-500 hover:text-gray-300'}"
					on:click={() => (activeTab = 'configuration')}
				>
					Configuration
				</button>
				<button
					type="button"
					aria-pressed={activeTab === 'backtests'}
					class="border-b-2 py-2 transition-colors {activeTab === 'backtests' ? 'border-white text-white' : 'border-transparent text-gray-500 hover:text-gray-300'}"
					on:click={() => (activeTab = 'backtests')}
				>
					Gauntlet History
				</button>
				<button
					type="button"
					aria-pressed={activeTab === 'optimizations'}
					class="border-b-2 py-2 transition-colors {activeTab === 'optimizations' ? 'border-white text-white' : 'border-transparent text-gray-500 hover:text-gray-300'}"
					on:click={() => (activeTab = 'optimizations')}
				>
					Robustness
				</button>
				<button
					type="button"
					aria-pressed={activeTab === 'execution'}
					class="border-b-2 py-2 transition-colors {activeTab === 'execution' ? 'border-white text-white' : 'border-transparent text-gray-500 hover:text-gray-300'}"
					on:click={() => (activeTab = 'execution')}
				>
					Execution
				</button>
			</div>
		</div>

		<div class="flex-1 overflow-auto bg-black p-4">
			{#if submitStatus !== 'idle'}
				<div class="mb-4 rounded border px-3 py-2 text-xs {submitStatus === 'failed' ? 'border-red-900 bg-red-950/20 text-red-300' : submitStatus === 'completed' ? 'border-emerald-900 bg-emerald-950/20 text-emerald-300' : 'border-blue-900 bg-blue-950/20 text-blue-300'}">
					<div>{submitMessage}</div>
					{#if submitStatus === 'running' || submitStatus === 'submitting'}
						<div class="mt-2 border-t border-current/20 pt-2 text-[11px]">
							<div class="flex items-center justify-between gap-2">
								<span class="font-mono uppercase tracking-wide">status: {submitPollingStatus || submitStatus}</span>
								<span class="font-mono text-[10px] text-gray-400">poll #{submitPollCount}</span>
							</div>
							{#if submitProgress}
								<div class="mt-1 text-gray-300">{submitProgress}</div>
							{/if}
							{#if submitProgressPct !== null}
								<div class="mt-2 h-1.5 w-full rounded bg-[#111]">
									<div
										class="h-1.5 rounded bg-cyan-400 transition-all"
										style={`width: ${submitProgressPct}%`}
									></div>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			{#if activeTab === 'configuration'}
				<div>
					<div>
						<div class="mb-3 flex justify-end">
							<button
								type="button"
								data-testid="deepdive-toggle-configuration"
								class="rounded border border-violet-700/50 bg-violet-950/30 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-violet-200 transition hover:bg-violet-900/40"
								on:click={launchDeepdive}
							>
								🔍 Deepdive
							</button>
						</div>
				<div class="grid grid-cols-1 gap-3 xl:grid-cols-[0.85fr_1.15fr]">
					<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="flex items-start justify-between gap-2">
							<div>
								<h3 class="text-sm font-semibold text-white">{container.strategy.name}</h3>
								<div class="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
									<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-cyan-300">{container.strategy.id}</span>
									<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-gray-300">{String(container.configuration.type ?? '-')}</span>
									<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-gray-300">{String(container.configuration.owner ?? '-')}</span>
								</div>
							</div>
							<span class="shrink-0 rounded border border-[#2b2b2b] bg-black px-2 py-0.5 text-[11px] font-mono text-gray-300">{String(container.strategy.state ?? '-')}</span>
						</div>
						<div class="mt-2 grid gap-2 sm:grid-cols-3 text-xs">
							<div class="rounded border border-[#1f1f1f] bg-black px-2 py-1.5">
								<span class="text-[10px] uppercase tracking-wide text-gray-500">Market</span>
								<div class="font-mono text-sm text-white">{String(container.configuration.symbol ?? '-')}</div>
							</div>
							<div class="rounded border border-[#1f1f1f] bg-black px-2 py-1.5">
								<span class="text-[10px] uppercase tracking-wide text-gray-500">Timeframe</span>
								<div class="font-mono text-sm text-white">{String(container.configuration.timeframe ?? '-')}</div>
							</div>
							<div class="rounded border border-[#1f1f1f] bg-black px-2 py-1.5">
								<span class="text-[10px] uppercase tracking-wide text-gray-500">Created</span>
								<div class="text-sm text-gray-400">{fmtDate(container.strategy.created_at)}</div>
							</div>
						</div>
					</div>
					<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="flex items-center justify-between gap-2">
							<div class="flex items-center gap-2">
								<div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Default Parameters</div>
								<span class={`rounded border px-1.5 py-0.5 text-[10px] ${paramsDirty ? 'border-amber-900/70 bg-amber-950/20 text-amber-200' : 'border-[#2b2b2b] text-gray-500'}`}>
									{paramsDirty ? 'Unsaved' : 'Synced'}
								</span>
							</div>
							<div class="flex items-center gap-1.5">
								<button
									type="button"
									class="rounded border border-[#2b2b2b] bg-black px-2 py-1 text-[10px] uppercase tracking-wide text-gray-400 transition hover:text-white disabled:opacity-40"
									on:click={resetParameterDraft}
									disabled={!paramsDirty || settingDefaultParams}
								>Reset</button>
								<button
									type="button"
									class="rounded border border-emerald-700 bg-emerald-950/30 px-2 py-1 text-[10px] uppercase tracking-wide text-emerald-200 transition hover:bg-emerald-900/40 disabled:opacity-40"
									on:click={saveParameterDraft}
									disabled={!paramsDirty || settingDefaultParams || paramsHasErrors}
								>{settingDefaultParams ? 'Saving…' : 'Save'}</button>
							</div>
						</div>
						{#if parameterSaveMessage}
							<div class="mt-2 rounded border border-emerald-900/40 bg-emerald-950/10 px-2 py-1 text-[11px] text-emerald-200">{parameterSaveMessage}</div>
						{/if}
						{#if parameterSaveError}
							<div class="mt-2 rounded border border-red-900/40 bg-red-950/20 px-2 py-1 text-[11px] text-red-300">{parameterSaveError}</div>
						{/if}
						{#if !loading}
							<div class="mt-2 rounded border border-[#1f1f1f] bg-black p-2">
								<div class="flex flex-wrap items-end gap-2">
									<label class="flex min-w-[220px] flex-1 flex-col gap-1 text-[10px] uppercase tracking-wide text-gray-500">
										<span>Add Param</span>
										<select
											class="rounded border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-gray-200 disabled:opacity-40"
											bind:value={selectedAddParamKey}
											data-testid="add-param-select"
											disabled={settingDefaultParams || availableAddParamKeys.length === 0}
										>
											<option value="">Select a supported param</option>
											{#each availableAddParamKeys as key}
												<option value={key}>{key}</option>
											{/each}
										</select>
									</label>
									<button
										type="button"
										class="rounded border border-cyan-700 bg-cyan-950/30 px-3 py-1.5 text-[10px] uppercase tracking-wide text-cyan-200 transition hover:bg-cyan-900/40 disabled:opacity-40"
										on:click={addSelectedParamToDraft}
										data-testid="add-param-button"
										disabled={settingDefaultParams || availableAddParamKeys.length === 0}
									>
										Add Param
									</button>
								</div>
								{#if addParamHelperText}
									<div class="mt-2 text-[11px] text-gray-500">{addParamHelperText}</div>
								{/if}
							</div>
						{/if}
						<div class="mt-2">
							<ParameterEditor bind:params={paramsDraft} bind:hasErrors={paramsHasErrors} saving={settingDefaultParams} />
						</div>
						<details class="mt-2 rounded border border-[#1f1f1f] bg-black">
							<summary class="cursor-pointer px-2 py-1.5 text-[10px] uppercase tracking-wide text-gray-500">Raw JSON</summary>
							<pre class="max-h-[200px] overflow-auto border-t border-[#1a1a1a] p-2 text-[11px] text-gray-300">{stableStringify(paramsDraft)}</pre>
						</details>
					</div>
				</div>

				<div class="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-[0.95fr_1.05fr]">
					<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="flex items-center justify-between gap-2">
							<div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Pipeline</div>
							<div class="flex items-center gap-2 text-[11px]">
								<span class="text-gray-500">{latestLifecycleEvent ? fmtDate(latestLifecycleEvent.created_at) : '--'}</span>
								<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-gray-400">{orderedRecentEvents.length} event{orderedRecentEvents.length === 1 ? '' : 's'}</span>
							</div>
						</div>
						{#if latestLifecycleEvent}
							<div class="mt-2 text-xs text-gray-400">{summarizeLifecycleEvent(latestLifecycleEvent)}</div>
						{/if}
						{#if currentLifecycleStage in TERMINAL_STAGES}
							<div class="mt-2 rounded border border-red-900/40 bg-red-950/15 px-2 py-1.5 text-xs text-red-200">
								<span class="font-medium">{lifecycleStageLabel(currentLifecycleStage)}.</span>
								<span class="ml-1 text-red-300/80">{TERMINAL_STAGES[currentLifecycleStage]}</span>
							</div>
						{:else}
							<div class="mt-2 flex gap-1.5">
								{#each currentStageDescriptors as stage}
									<button
										on:click={() => { selectedReadinessStage = stage.key === currentLifecycleStage ? null : stage.key; }}
										title={stage.tooltip ?? ''}
										class={`flex-1 rounded border px-2 py-1.5 text-center cursor-pointer transition-colors ${
											readinessViewStage === stage.key
												? 'border-cyan-600/60 bg-cyan-950/20 text-cyan-200 ring-1 ring-cyan-500/40'
												: stage.kind === 'current'
													? 'border-cyan-600/40 bg-cyan-950/10 text-cyan-300/70'
													: stage.kind === 'past'
														? 'border-emerald-900/40 bg-emerald-950/10 text-emerald-200'
														: 'border-[#1f1f1f] bg-[#070707] text-gray-500 hover:border-gray-700 hover:text-gray-400'
										}`}
									>
										<div class="text-[10px] uppercase tracking-wide">{stage.label}</div>
									</button>
								{/each}
							</div>
							<div class="mt-2">
								<PromotionReadiness
									{strategyId}
									stage={readinessViewStage}
									quickScreenRows={quickScreenRows}
									on:action={(e) => {
										const action = e.detail?.action;
										if (action === 'run_optimization') {
											activeTab = 'optimizations';
											robustnessSubTab = 'optimization';
										}
										else if (action === 'run_confirmation_backtest') activeTab = 'backtests';
										else if (action === 'apply_best_params') {
											activeTab = 'optimizations';
											robustnessSubTab = 'optimization';
										}
										else if (action === 'run_validation_suite' || action === 're_run_validation_suite') {
											activeTab = 'optimizations';
											robustnessSubTab = 'robustness';
										}
									}}
								/>
							</div>

							<div class="mt-2 flex flex-wrap items-center gap-2">
								{#if nextPipelineStage}
									{#if !showPromoteConfirm}
										<button on:click={() => { showPromoteConfirm = true; promoteReason = ''; promoteBlockReason = ''; }} class="rounded border border-cyan-700/50 bg-cyan-950/30 px-3 py-1.5 text-xs text-cyan-200 hover:bg-cyan-900/40 transition-colors">Promote to {nextPipelineStage.label}</button>
									{:else}
										<div class="flex-1 rounded border border-cyan-800/40 bg-cyan-950/20 p-2 space-y-2">
											<div class="text-xs text-cyan-200">Promote to <span class="font-semibold">{nextPipelineStage.label}</span>?</div>
											<textarea bind:value={promoteReason} placeholder="Reason (optional)" rows="1" class="w-full rounded bg-black border border-[#2b2b2b] text-xs text-gray-300 px-2 py-1 placeholder:text-gray-600 focus:border-cyan-700 focus:outline-none"></textarea>
											{#if promoteBlockReason}
												<div class="rounded border border-amber-700/50 bg-amber-950/30 p-2 text-[11px]">
													<div class="font-semibold text-amber-200">Promotion gate blocked this:</div>
													<div class="mt-0.5 text-amber-100/90">{promoteBlockReason}</div>
													<div class="mt-1 text-amber-300/70">Overriding promotes anyway (logged). The mainnet hard-gate is separate and unaffected.</div>
												</div>
											{/if}
											<div class="flex gap-1.5">
												{#if promoteBlockReason}
													<button disabled={promoting} on:click={() => confirmPromotion(true)} class="rounded bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-500 disabled:opacity-50">{promoting ? 'Overriding...' : 'Override gate & promote'}</button>
												{:else}
													<button disabled={promoting} on:click={() => confirmPromotion(false)} class="rounded bg-cyan-600 px-3 py-1 text-xs text-white hover:bg-cyan-500 disabled:opacity-50">{promoting ? 'Promoting...' : 'Confirm'}</button>
												{/if}
												<button on:click={() => { showPromoteConfirm = false; promoteBlockReason = ''; }} class="rounded border border-[#2b2b2b] bg-black px-3 py-1 text-xs text-gray-400 hover:text-gray-200">Cancel</button>
											</div>
										</div>
									{/if}
								{/if}
								<button on:click={() => goto(`/bot-factory/editor?strategy=${strategyId}`)} class="rounded border border-violet-700/50 bg-violet-950/30 px-3 py-1.5 text-xs text-violet-200 hover:bg-violet-900/40 transition-colors">Deploy as Bot</button>
								<button on:click={archiveStrategyFromConfig} class="rounded border border-red-900/40 bg-red-950/20 px-3 py-1.5 text-xs text-red-300 hover:bg-red-900/30 transition-colors">Archive</button>
							</div>
						{/if}
					</div>

					<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Lifecycle Feed</div>
						{#if orderedRecentEvents.length === 0}
							<div class="mt-2 text-xs text-gray-500">No events recorded.</div>
						{:else}
							<div class="mt-2 max-h-[400px] overflow-auto space-y-1.5">
								{#each orderedRecentEvents as event}
									<div class="rounded border border-[#1f1f1f] bg-[#070707] px-2.5 py-2">
										<div class="flex items-center justify-between gap-2 text-[11px]">
											<div class="flex items-center gap-1.5">
												<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-gray-300">{lifecycleStageLabel(event.from_state)}</span>
												<span class="text-gray-600">-></span>
												<span class="rounded border border-cyan-900/40 bg-cyan-950/20 px-1.5 py-0.5 font-mono text-cyan-200">{lifecycleStageLabel(event.to_state)}</span>
												<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-gray-500">{lifecycleActorLabel(event.actor)}</span>
											</div>
											<span class="shrink-0 text-gray-500">{fmtDate(event.created_at)}</span>
										</div>
										{#if event.reason && event.reason.trim()}
											<div class="mt-1 text-xs text-gray-400">{event.reason.trim()}</div>
										{/if}
									</div>
								{/each}
							</div>
						{/if}
					</div>
				</div>
					</div>
				</div>
			{/if}

			{#if activeTab === 'backtests'}
				<div>
					<div>
						<div class="mb-3 flex justify-end">
							<button
								type="button"
								data-testid="deepdive-toggle-backtests"
								class="rounded border border-violet-700/50 bg-violet-950/30 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-violet-200 transition hover:bg-violet-900/40"
								on:click={launchDeepdive}
							>
								🔍 Deepdive
							</button>
						</div>
				<div class="mb-3 rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
					<div class="grid gap-3 lg:grid-cols-[1fr_1fr_auto]">
						<div class="grid gap-2 sm:grid-cols-2">
							<SymbolInput id="container-backtest-symbol" label="Symbol" bind:value={backtestForm.symbol} suggestions={symbolSuggestions} helpText={backtestSymbolHelpText} />
							<TimeframeSelect id="container-backtest-timeframe" label="Timeframe" bind:value={backtestForm.timeframe} />
						</div>
						<DateRangeFieldset
							idPrefix="container-backtest"
							title="Window"
							bind:startDate={backtestForm.start_date}
							bind:endDate={backtestForm.end_date}
							timeframe={backtestForm.timeframe}
							accent="cyan"
						/>
						<div class="flex items-end">
							<button
								type="button"
								class="w-full rounded-lg border border-cyan-600/60 bg-cyan-950/30 px-5 py-2 text-xs font-semibold uppercase tracking-wider text-cyan-100 transition hover:bg-cyan-900/40 disabled:opacity-40 lg:w-auto"
								on:click={submitContainerBacktest}
								disabled={isAnyRunInFlight}
							>{submitStatus === 'submitting' || submitStatus === 'running' ? 'Running…' : 'Run the Gauntlet'}</button>
						</div>
					</div>
					<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
						<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5">{backtestBarEstimateLabel}</span>
						<span class="rounded border border-[#2b2b2b] bg-black px-1.5 py-0.5">{backtestWindowSummary}</span>
						<span class={`rounded border px-1.5 py-0.5 ${paramsDirty ? 'border-amber-900/60 text-amber-300' : 'border-[#2b2b2b] text-gray-500'}`}>{paramsDirty ? 'Draft has changes' : 'Defaults synced'}</span>
					</div>
				</div>

				<details class="mb-3 rounded-lg border border-[#1d1d1d] bg-[#090909]" data-testid="backtest-parameter-panel">
					<summary class="flex cursor-pointer items-center justify-between px-3 py-2">
						<div class="flex items-center gap-2 text-[10px] uppercase tracking-wide text-gray-500">
							Gauntlet Parameters
							<span class={`rounded border px-1.5 py-0.5 text-[10px] normal-case tracking-normal ${paramsDirty ? 'border-amber-900/60 text-amber-300' : 'border-[#2b2b2b] text-gray-500'}`}>{paramsDirty ? 'Unsaved' : 'Synced'}</span>
						</div>
						<div class="flex items-center gap-1.5">
							<button type="button" class="rounded border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] uppercase text-gray-400 hover:text-white disabled:opacity-40" data-testid="backtest-params-reset" on:click|stopPropagation={resetParameterDraft} disabled={!paramsDirty || settingDefaultParams}>Reset</button>
							<button type="button" class="rounded border border-emerald-700 bg-emerald-950/30 px-2 py-0.5 text-[10px] uppercase text-emerald-200 hover:bg-emerald-900/40 disabled:opacity-40" data-testid="backtest-params-save" on:click|stopPropagation={saveParameterDraft} disabled={!paramsDirty || settingDefaultParams || paramsHasErrors}>{settingDefaultParams ? 'Saving…' : 'Save'}</button>
						</div>
					</summary>
					<div class="border-t border-[#1a1a1a] p-3" data-testid="backtest-parameter-editor">
						<ParameterEditor bind:params={paramsDraft} bind:hasErrors={paramsHasErrors} saving={settingDefaultParams} />
					</div>
				</details>

				<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
					<div class="flex items-center justify-between gap-2">
						<div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Gauntlet history</div>
						<span class="text-[11px] text-gray-500">{backtestHistory.length} run{backtestHistory.length === 1 ? '' : 's'}</span>
					</div>
					<p class="mt-2 text-[11px] leading-relaxed text-gray-500">
						Left columns are <span class="text-gray-400">full-window</span> (IS + OOS combined); the
						<span class="text-gray-400">OOS</span> columns on the right are out-of-sample only.
						<span class="text-gray-400">ⓘ</span>/<span class="text-gray-400">~</span> marks an approximation
						(e.g. Sharpe is a month-weighted average and Max DD is the max of the IS/OOS halves, not recomputed
						from the combined stream). Hover any header for details.
					</p>
					{#if backtestHistory.length === 0}
						<div class="mt-4 rounded border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-sm text-gray-500">No Gauntlet runs yet.</div>
					{:else}
						<div class="mt-4 overflow-hidden rounded border border-[#1f1f1f] bg-[#070707]">
							<div class="max-h-[620px] overflow-auto">
								<table class="min-w-full text-xs">
									<thead class="sticky top-0 z-10 bg-[#0d0d0d] text-[10px] uppercase tracking-[0.18em] text-gray-500">
										<tr>
											<th class="px-3 py-2 text-left">Run</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('created')}>Created{historySortIndicator('created')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('symbol')}>Symbol{historySortIndicator('symbol')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('timeframe')}>TF{historySortIndicator('timeframe')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('start')}>Start{historySortIndicator('start')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('end')}>End{historySortIndicator('end')}</th>
											<th class="px-3 py-2 text-left">Window</th>
											<th class="px-3 py-2 text-left">Params</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('cagr')} title="Full-window CAGR (annualized over IS + OOS). Short windows are shown with muted styling.">CAGR{historySortIndicator('cagr')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('sharpe')} title="Full-window Sharpe (approximate: month-weighted average of IS and OOS Sharpe, not recomputed from the combined return stream). Low-trade samples are shown with muted styling.">Sharpe ⓘ{historySortIndicator('sharpe')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('max_drawdown')} title="Full-window max drawdown (approximate: max of IS and OOS max drawdowns; a drawdown that straddles the IS/OOS boundary is understated).">Max DD ⓘ{historySortIndicator('max_drawdown')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('win_rate')} title="Full-window win rate = combined wins / combined closed trades.">Win%{historySortIndicator('win_rate')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('trades')} title="Total completed trades across IS + OOS.">Trades{historySortIndicator('trades')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('profit_factor')} title="Full-window profit factor = combined gross profit / combined gross loss. ∞ if no losing trades.">PF{historySortIndicator('profit_factor')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('robustness')} title={`Gauntlet robustness score; below ${gauntletMinScore ?? 60} fails the promotion gate`}>Rob%{historySortIndicator('robustness')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300 border-l border-[#222] pl-3" on:click={() => toggleHistorySort('oos_cagr')} title="Out-of-sample CAGR (annualized). Short windows are shown with muted styling.">OOS CAGR{historySortIndicator('oos_cagr')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-gray-300" on:click={() => toggleHistorySort('oos_sharpe')} title="Out-of-sample annualized Sharpe. Low-trade samples are shown with muted styling.">OOS Sharpe{historySortIndicator('oos_sharpe')}</th>
											<th class="px-3 py-2 text-right">Actions</th>
										</tr>
									</thead>
									<tbody>
										{#each backtestHistory as item}
											<tr
												data-testid={`backtest-row-${item.result_id}`}
												class={`border-t border-[#161616] font-mono transition ${
													selectedResultId === item.result_id
														? 'bg-cyan-950/20 shadow-[inset_2px_0_0_0_rgba(34,211,238,0.9)]'
														: pinnedBacktestId && pinnedBacktestId === item.result_id
															? 'bg-emerald-950/15 shadow-[inset_2px_0_0_0_rgba(16,185,129,0.9)] hover:bg-emerald-950/25'
															: 'hover:bg-[#0d0d0d]'
												}`}
												tabindex="0"
												role="button"
												on:click={() => void openResult(item)}
												on:keydown={(event) => {
													if (event.key === 'Enter' || event.key === ' ') {
														event.preventDefault();
														void openResult(item);
													}
												}}
											>
												<td class="px-3 py-2 text-left">
													<div class="flex items-center gap-2">
														<span class="text-cyan-300">{item.result_id}</span>
														<span class={`rounded-full border px-2 py-0.5 text-[9px] ${resultTypeBadge(item.result_type)}`}>{resultTypeLabel(item.result_type)}</span>
														{#if pinnedBacktestId && pinnedBacktestId === item.result_id}
															<span class="rounded-full border border-emerald-600/60 bg-emerald-950/30 px-2 py-0.5 text-[9px] uppercase tracking-wide text-emerald-200" title="This backtest's metrics and params drive the Lab manager display and paper/live trading.">Active</span>
														{/if}
													</div>
												</td>
												<td class="px-3 py-2 text-left text-gray-400">{fmtDate(item.created_at)}</td>
												<td class="px-3 py-2 text-left text-white">{item.symbol || '--'}</td>
												<td class="px-3 py-2 text-left text-gray-300">{item.timeframe || '--'}</td>
												<td class="px-3 py-2 text-left text-gray-400">{fmtShortDate(item.start_date)}</td>
												<td class="px-3 py-2 text-left text-gray-400">{fmtShortDate(item.end_date)}</td>
												<td class="px-3 py-2 text-left text-gray-400">{fmtDuration(item.start_date, item.end_date, readMetricOptional(item, 'backtest_months'))}</td>
												<td class="px-3 py-2 text-left">
													<div data-testid={`backtest-param-summary-${item.result_id}`} class="flex max-w-[320px] flex-wrap gap-1">
														{#if getHistoryParamSource(item) === 'current'}
															<span class="rounded-full border border-amber-900/60 bg-amber-950/15 px-2 py-0.5 text-[10px] text-amber-200">
																Current strategy params
															</span>
														{/if}
														{#if getBacktestVisibleParamSummary(item).length > 0}
															{#each getBacktestVisibleParamSummary(item) as entry}
																<span class={`rounded-full border px-2 py-0.5 text-[10px] ${entry.changed ? 'border-amber-900/60 bg-amber-950/15 text-amber-200' : 'border-[#2b2b2b] bg-black text-gray-300'}`}>
																	{entry.key}={entry.value}
																</span>
															{/each}
															{#if getBacktestParamOverflowCount(item) > 0}
																<span class="rounded-full border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] text-gray-500">
																	+{getBacktestParamOverflowCount(item)} more
																</span>
															{/if}
														{:else}
															<span class="text-[11px] text-gray-600">No stored params</span>
														{/if}
													</div>
												</td>
												<td class={`px-3 py-2 text-right ${isCagrReliable(item) ? signedPercentClass(readPercentMetricOptional(item, 'annualized_return_pct')) : 'text-gray-500'}`}
													title={isCagrReliable(item) ? 'Full-window CAGR (annualized over IS + OOS)' : `Short window (<1 month) — annualized value may be noisy`}>
													{formatCagr(item)}
												</td>
												<td class={`px-3 py-2 text-right ${isSharpeReliable(item) ? 'text-gray-300' : 'text-gray-500'}`}
													title={isSharpeReliable(item) ? 'Full-window Sharpe (approximate: month-weighted avg of IS and OOS)' : `Low trade count (<20) — Sharpe may be noisy`}>
													{formatSharpe(item)}{readFlag(item, 'sharpe_is_approximation') === true ? ' ~' : ''}
												</td>
												<td class="px-3 py-2 text-right text-red-400" title={readFlag(item, 'max_drawdown_is_approximation') === true ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{pct(readDrawdownPercentMetric(item, 'max_drawdown_pct', 'max_drawdown'))}{readFlag(item, 'max_drawdown_is_approximation') === true ? ' ~' : ''}</td>
												<td class="px-3 py-2 text-right text-gray-300">{pct(readPercentMetric(item, 'win_rate', 'win_rate_pct'))}</td>
												<td class="px-3 py-2 text-right text-gray-300">{historyTradesCount(item)}</td>
												<td class="px-3 py-2 text-right text-gray-300"
													title={readFlag(item, 'profit_factor_is_infinite') === true ? 'No losing trades — profit factor is mathematically infinite' : 'Full-window profit factor'}>
													{formatProfitFactor(item)}
												</td>
												<td class="px-3 py-2 text-right text-gray-300" title="Gauntlet robustness score">{formatRobustness(item)}</td>
												<td class={`px-3 py-2 text-right border-l border-[#222] pl-3 ${isCagrReliable(item) ? signedPercentClass(readOutOfSampleCagr(item)) : 'text-gray-500'}`}
													title={isCagrReliable(item) ? 'Out-of-sample CAGR (annualized)' : `Short OOS window (<1 month) — annualized value may be noisy`}>
													{formatOutOfSampleCagr(item)}
												</td>
												<td class={`px-3 py-2 text-right ${isSharpeReliable(item) ? 'text-gray-300' : 'text-gray-500'}`}
													title={isSharpeReliable(item) ? 'Out-of-sample annualized Sharpe' : `Low trade count (<20) — Sharpe may be noisy`}>
													{formatOutOfSampleSharpe(item)}
												</td>
												<td class="px-3 py-2 text-right">
													<div class="flex items-center justify-end gap-2">
														<button
															type="button"
															data-testid={`set-default-backtest-params-${item.result_id}`}
															class={`rounded-xl border px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] transition disabled:opacity-60 ${
																pinnedBacktestId && pinnedBacktestId === item.result_id
																	? 'border-emerald-500 bg-emerald-600/30 text-emerald-100 cursor-default'
																	: 'border-emerald-700 bg-emerald-950/30 text-emerald-200 hover:bg-emerald-900/40'
															}`}
															on:click|stopPropagation={() => void setBacktestRowAsDefault(item)}
															disabled={settingDefaultParams || Boolean(pinnedBacktestId && pinnedBacktestId === item.result_id)}
															title={pinnedBacktestId && pinnedBacktestId === item.result_id ? 'This Gauntlet run is currently active' : 'Make this Gauntlet run the active default'}
														>
															{settingDefaultParams
																? 'Saving…'
																: pinnedBacktestId && pinnedBacktestId === item.result_id
																	? 'Active'
																	: 'Set Default'}
														</button>
														<button
															type="button"
															data-testid={`edit-backtest-params-${item.result_id}`}
															class={`rounded-xl border px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] transition ${
																expandedBacktestParamsId === item.result_id
																	? 'border-cyan-700 bg-cyan-950/30 text-cyan-200'
																	: 'border-[#2b2b2b] bg-black text-gray-400 hover:border-white/20 hover:text-white'
															}`}
															on:click|stopPropagation={() => toggleBacktestParamEditor(item)}
														>
															{expandedBacktestParamsId === item.result_id ? 'Hide' : 'Edit'}
														</button>
														<button
															type="button"
															class="rounded p-1 text-gray-600 transition-colors hover:bg-red-900/30 hover:text-red-400"
															title="Delete result"
															aria-label={`Move backtest result ${item.result_id} to trash`}
															on:click={(e) => trashResult(e, item)}
														>
															<svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
																<path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd" />
															</svg>
														</button>
													</div>
												</td>
											</tr>
											{#if expandedBacktestParamsId === item.result_id}
												<tr class="border-t border-cyan-950/40 bg-[#050505]">
													<td colspan="18" class="px-4 py-4">
														<div class="rounded border border-[#1f1f1f] bg-black p-4">
															<div class="flex flex-wrap items-start justify-between gap-3">
																<div>
																	<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Run Parameter Editor</div>
																	<div class="mt-1 text-sm text-gray-400">
																		Tweak the stored run parameters here, then rerun the exact market window without leaving history.
																	</div>
																</div>
																<div class="flex flex-wrap items-center gap-2">
																	<button
																		type="button"
																		data-testid={`load-backtest-params-${item.result_id}`}
																		class="rounded-xl border border-[#2b2b2b] bg-[#070707] px-3 py-2 text-[11px] font-medium uppercase tracking-[0.16em] text-gray-300 transition hover:border-white/20 hover:text-white"
																		on:click|stopPropagation={() => loadBacktestParamsIntoDraft(item)}
																	>
																		Load Into Draft
																	</button>
																	<button
																		type="button"
																		class="rounded-xl border border-[#2b2b2b] bg-[#070707] px-3 py-2 text-[11px] font-medium uppercase tracking-[0.16em] text-gray-300 transition hover:border-white/20 hover:text-white"
																		on:click|stopPropagation={() => resetBacktestParamDraft(item)}
																	>
																		Reset
																	</button>
																	<button
																		type="button"
																		data-testid={`rerun-backtest-params-${item.result_id}`}
																		class="rounded-xl border border-cyan-700 bg-cyan-950/30 px-3 py-2 text-[11px] font-medium uppercase tracking-[0.16em] text-cyan-200 transition hover:bg-cyan-900/40 disabled:opacity-40"
																		on:click|stopPropagation={() => void rerunBacktestFromHistory(item)}
																					disabled={isAnyRunInFlight || Boolean(backtestParamDraftErrors[item.result_id])}
																	>
																		{backtestParamRunnerId === item.result_id ? 'Running…' : 'Rerun With Changes'}
																	</button>
																</div>
															</div>
															<div class="mt-3 flex flex-wrap gap-2 text-[11px] text-gray-500">
																<span class="rounded-full border border-[#2b2b2b] bg-[#070707] px-2 py-1">
																	{item.symbol || '--'} / {item.timeframe || '--'}
																</span>
																<span class="rounded-full border border-[#2b2b2b] bg-[#070707] px-2 py-1">
																	{fmtShortDate(item.start_date)} -> {fmtShortDate(item.end_date)}
																</span>
															</div>
															<div class="mt-4">
																{#if getHistoryParamSource(item) === 'current'}
																	<div class="mb-3 rounded border border-amber-900/40 bg-amber-950/10 px-3 py-2 text-[11px] text-amber-200">
																		This run did not store its own params. The editor is seeded from the current strategy params as a labeled fallback.
																	</div>
																{/if}
																<div data-testid={`backtest-param-editor-${item.result_id}`}>
																	<ParameterEditor
																		bind:params={backtestParamDrafts[item.result_id]} bind:hasErrors={backtestParamDraftErrors[item.result_id]}
																		saving={backtestParamRunnerId === item.result_id}
																	/>
																</div>
															</div>
														</div>
													</td>
												</tr>
											{/if}
										{/each}
									</tbody>
								</table>
							</div>
						</div>
					{/if}
				</div>
				</div>
			</div>
			{/if}

			{#if activeTab === 'optimizations'}
				<!-- Sub-tab navigation -->
				<div class="mb-4 flex gap-1 rounded border border-[#222] bg-[#090909] p-1">
					{#each [
						{ key: 'optimization', label: 'Optimization' },
						{ key: 'robustness', label: 'Robustness Suite' },
					] as tab}
						<button
							class="flex-1 rounded px-2 py-1.5 text-[11px] uppercase tracking-wide transition-colors
								{robustnessSubTab === tab.key ? 'bg-[#1a1a1a] text-white border border-[#333]' : 'text-gray-500 hover:text-gray-300 border border-transparent'}"
							on:click={() => selectRobustnessTab(tab.key as RobustnessSubTab)}
						>
							{tab.label}
						</button>
					{/each}
				</div>

				{#if robustnessSubTab === 'optimization'}
					<div class="mb-3 rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="grid gap-3 lg:grid-cols-[1fr_1fr_auto]">
							<div class="grid gap-2 sm:grid-cols-2">
								<SymbolInput id="container-opt-symbol" label="Symbol" bind:value={optimizationForm.symbol} suggestions={symbolSuggestions} helpText={optimizationSymbolHelpText} />
								<TimeframeSelect id="container-opt-timeframe" label="Timeframe" bind:value={optimizationForm.timeframe} />
							</div>
							<DateRangeFieldset idPrefix="container-opt" title="Window" bind:startDate={optimizationForm.start_date} bind:endDate={optimizationForm.end_date} timeframe={optimizationForm.timeframe} accent="blue" />
							<div class="flex items-end">
								<button type="button" class="w-full rounded-lg border border-blue-600/60 bg-blue-950/30 px-5 py-2 text-xs font-semibold uppercase tracking-wider text-blue-100 transition hover:bg-blue-900/40 disabled:opacity-40 lg:w-auto" on:click={submitContainerOptimization} disabled={isAnyRunInFlight}>{submitStatus === 'submitting' || submitStatus === 'running' ? 'Running…' : 'Run Optimization'}</button>
							</div>
						</div>
						<div class="mt-2 grid gap-2 sm:grid-cols-2">
							<label class="block" for="container-opt-objective">
								<div class="text-[10px] uppercase tracking-wide text-gray-500">Objective</div>
								<select id="container-opt-objective" bind:value={optimizationForm.objective} class="mt-1 w-full rounded border border-[#2b2b2b] bg-[#050505] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60">
									{#each OPTIMIZATION_OBJECTIVES as option}
										<option value={option.value}>{option.label}</option>
									{/each}
								</select>
							</label>
							<label class="block" for="container-opt-trials">
								<div class="text-[10px] uppercase tracking-wide text-gray-500">Trials</div>
								<input id="container-opt-trials" type="number" min="1" class="mt-1 w-full rounded border border-[#2b2b2b] bg-[#050505] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60" bind:value={optimizationForm.n_trials} />
							</label>
						</div>
						<div data-testid="optimization-params-panel" class="mt-3 rounded-lg border border-[#1d1d1d] bg-black/40 p-3">
							<div class="flex items-center justify-between gap-2">
								<div>
									<div class="text-[10px] uppercase tracking-wide text-gray-500">Optimization Parameters</div>
									<div class="mt-1 text-xs text-gray-400">Select numeric params to optimize. Unchecked params stay fixed at the current strategy defaults.</div>
								</div>
								<div class="rounded-full border border-cyan-900/40 bg-cyan-950/20 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-cyan-200">
									{Object.values(optimizationParamDrafts).filter((draft) => draft.selected).length} selected
								</div>
							</div>
							{#if Object.keys(optimizationParamDrafts).length === 0}
								<div class="mt-3 rounded border border-[#1a1a1a] bg-[#050505] px-3 py-2 text-xs text-gray-500">
									No numeric strategy params are available for optimization on this container.
								</div>
							{:else}
								<div class="mt-3 grid gap-2">
									{#each Object.values(optimizationParamDrafts) as draft (draft.key)}
										<div class="rounded-lg border border-[#1d1d1d] bg-[#050505] p-3">
											<div class="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
												<label class="flex items-center gap-3 text-sm text-white">
													<input
														data-testid={`opt-param-select-${draft.key}`}
														type="checkbox"
														class="h-4 w-4 rounded border border-[#2b2b2b] bg-black text-cyan-400 focus:ring-cyan-500/30"
														checked={draft.selected}
														on:change={(event) => setOptimizationParamSelected(draft.key, (event.currentTarget as HTMLInputElement).checked)}
													/>
													<span class="font-medium uppercase tracking-wide text-gray-200">{draft.key}</span>
													<span class="rounded-full border border-[#243240] bg-[#09111a] px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-cyan-200">Current {optimizationParamCurrentLabel(draft.current, draft.kind)}</span>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-gray-500">Min</div>
													<input
														data-testid={`opt-param-min-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full rounded border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.min}
														on:input={(event) => updateOptimizationParamField(draft.key, 'min', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-gray-500">Max</div>
													<input
														data-testid={`opt-param-max-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full rounded border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.max}
														on:input={(event) => updateOptimizationParamField(draft.key, 'max', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-gray-500">Step</div>
													<input
														data-testid={`opt-param-step-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full rounded border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.step}
														on:input={(event) => updateOptimizationParamField(draft.key, 'step', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<div class="flex items-end">
													<div class="rounded border border-[#1d1d1d] bg-[#090909] px-3 py-2 text-[11px] text-gray-400">
														{draft.kind === 'int' ? 'Whole-number sweep' : 'Decimal sweep'}
													</div>
												</div>
											</div>
											{#if draft.error}
												<div data-testid={`opt-param-error-${draft.key}`} class="mt-2 rounded border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-200">
													{draft.error}
												</div>
											{/if}
										</div>
									{/each}
								</div>
							{/if}
						</div>
					</div>

					<div class="grid grid-cols-1 gap-4">
						<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-4">
							<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-gray-500">Optimization Runs</div>
							{#if optimizationHistory.length === 0}
								<div class="px-3 py-4 text-xs text-gray-600">No optimization runs yet.</div>
							{:else}
								<div class="mt-3 grid gap-3">
									{#each optimizationHistory as item}
										<button
											data-testid={`optimization-row-${item.result_id}`}
											class={`rounded border border-[#222] bg-[#090909] px-4 py-3 text-left transition ${historyCardBorder(item.result_type)} ${selectedResultId === item.result_id ? 'border-blue-500/70 shadow-[0_0_0_1px_rgba(96,165,250,0.08),0_18px_40px_rgba(59,130,246,0.08)]' : ''}`}
											on:click={() => void openResult(item)}
										>
											<div class="flex items-center gap-2 text-xs">
												<span class="font-mono text-cyan-300">{item.result_id}</span>
												<span class={`rounded border px-1 py-0.5 text-[10px] ${resultTypeBadge(item.result_type)}`}>{item.result_type}</span>
												<span class={`rounded border px-1.5 py-0.5 text-[10px] ${statusBadgeClass(historyItemStatus(item))}`}>{statusLabel(historyItemStatus(item))}</span>
												<span class="ml-auto text-gray-500">{fmtDate(item.created_at)}</span>
											</div>
											{#if historyItemError(item)}
												<div class="mt-2 rounded border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-200">
													{historyItemError(item)}
												</div>
											{/if}
											<div class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
												<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
													<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Fitness</div>
													<div class="mt-1 font-mono text-sm text-gray-300">{numOrDash(readMetricOptional(item, 'best_fitness', 'fitness'))}</div>
												</div>
												<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
													<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Sharpe</div>
													<div class={`mt-1 font-mono text-sm ${isSharpeReliable(item) ? 'text-gray-300' : 'text-gray-500'}`} title={isSharpeReliable(item) ? undefined : 'Low trade count (<20) — Sharpe may be noisy'}>{formatSharpe(item)}</div>
												</div>
												<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
													<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">OOS Return</div>
													<div class={`mt-1 font-mono text-sm ${signedPercentClass(readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct'))}`}>{pctOrDash(readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct'))}</div>
												</div>
												<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
													<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Trials</div>
													<div class="mt-1 font-mono text-sm text-gray-300">{historyItemTrials(item) ?? '--'}</div>
												</div>
											</div>
										</button>
									{/each}
								</div>
							{/if}
						</div>
					</div>
				{/if}

				{#if robustnessSubTab === 'robustness'}
					<div class="space-y-3">
						<GauntletStatusCard
							{strategyId}
							stage={currentLifecycleStage}
							selectedTestKey={selectedRobustnessTest}
							testOverrides={robustnessStatusOverrides}
							on:selectTest={(event) => {
								selectedRobustnessTest = event.detail.key;
							}}
							on:promote={() => {
								showPromoteConfirm = true;
								promoteReason = '';
								promoteBlockReason = '';
							}}
						/>
						<RobustnessPanel
							{strategyId}
							{backtestHistory}
							validationHistory={validationHistory}
							{symbolSuggestions}
							defaultSymbol={String(container?.configuration?.symbol ?? '')}
							defaultTimeframe={String(container?.configuration?.timeframe ?? '1h')}
							{pinnedBacktestId}
							activeTestKey={selectedRunnerTestKey(selectedRobustnessTest)}
							on:testComplete={(event) => noteRobustnessTestComplete(event.detail)}
						/>
						{#if selectedRobustnessTest === 'walk_forward'}
							<div class="rounded-lg border border-[#1d1d1d] bg-[#090909] p-4">
								<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-gray-500">Walk Forward Runs</div>
								{#if walkForwardHistory.length === 0}
									<div class="px-3 py-4 text-xs text-gray-600">No walk-forward runs yet.</div>
								{:else}
									<div class="mt-3 grid gap-3">
										{#each walkForwardHistory as item}
											<button class={`rounded border border-[#222] bg-[#090909] px-4 py-3 text-left transition ${historyCardBorder(item.result_type)} ${selectedResultId === item.result_id ? 'border-violet-500/70 shadow-[0_0_0_1px_rgba(167,139,250,0.08),0_18px_40px_rgba(139,92,246,0.08)]' : ''}`} on:click={() => void openResult(item)}>
												<div class="flex items-center gap-2 text-xs">
													<span class="font-mono text-cyan-300">{item.result_id}</span>
													<span class={`rounded border px-1 py-0.5 text-[10px] ${resultTypeBadge(item.result_type)}`}>{item.result_type}</span>
													<span class="ml-auto text-gray-500">{fmtDate(item.created_at)}</span>
												</div>
												<div class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
													<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">IS Sharpe</div>
														<div class="mt-1 font-mono text-sm text-gray-300">{formatWalkForwardSharpe(item, 'avg_is_sharpe')}</div>
													</div>
													<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">OOS Sharpe</div>
														<div class="mt-1 font-mono text-sm text-gray-300">{formatWalkForwardSharpe(item, 'avg_oos_sharpe')}</div>
													</div>
													<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Degradation</div>
														<div class={`mt-1 font-mono text-sm ${walkForwardDegradationClass(item)}`}>{formatWalkForwardDegradation(item)}</div>
													</div>
													<div class="rounded border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">OOS Trades</div>
														<div class="mt-1 font-mono text-sm text-gray-300">{formatWalkForwardOosTrades(item)}</div>
													</div>
												</div>
											</button>
										{/each}
									</div>
								{/if}
							</div>
						{/if}
					</div>
				{/if}

			{/if}

			{#if (activeTab === 'backtests' || activeTab === 'optimizations') && (resultLoading || !!resultError || !!selectedResult)}
				<div class="mt-3 rounded-lg border border-[#1d1d1d] bg-[#090909] p-3">
					{#if resultLoading}
						<div class="py-4 text-center text-sm text-gray-500">Loading result details...</div>
					{:else if resultError}
						<div class="rounded border border-red-900/50 bg-red-950/20 px-3 py-2 text-sm text-red-300">{resultError}</div>
					{:else if selectedResult}
						<div>
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-mono text-sm text-cyan-300">{selectedResultId}</span>
									<span class={`rounded border px-1.5 py-0.5 text-[10px] ${resultTypeBadge(selectedResult.result_type ?? '')}`}>{resultTypeLabel(selectedResult.result_type)}</span>
									<span data-testid="selected-result-status-badge" class={`rounded border px-1.5 py-0.5 text-[10px] ${statusBadgeClass(selectedResultStatus)}`}>{statusLabel(selectedResultStatus)}</span>
									<span class="text-[11px] text-gray-400">{selectedResult.symbol || '--'} / {selectedResult.timeframe || '--'}</span>
									<span class="text-[11px] text-gray-500">{fmtShortDate(selectedResult.config?.start as string | undefined)} -> {fmtShortDate(selectedResult.config?.end as string | undefined)}</span>
									<span class="text-[11px] text-gray-600">{fmtDuration(selectedResult.config?.start as string | null | undefined, selectedResult.config?.end as string | null | undefined)}</span>
								</div>
								{#if isOptimizationResult()}
									<div class="flex items-center gap-1.5">
										<button type="button" class="rounded border border-blue-700 bg-blue-950/30 px-2 py-1 text-[10px] uppercase text-blue-200 hover:bg-blue-900/40 disabled:opacity-40" on:click={backtestWithOptParams} disabled={isAnyRunInFlight}>{backtestingOptParams ? 'Running…' : 'Gauntlet With Params'}</button>
										<button type="button" class="rounded border border-emerald-700 bg-emerald-950/30 px-2 py-1 text-[10px] uppercase text-emerald-200 hover:bg-emerald-900/40 disabled:opacity-40" on:click={setAsDefaultParams} disabled={settingDefaultParams || backtestingOptParams}>{settingDefaultParams ? 'Updating…' : 'Set As Default'}</button>
									</div>
								{/if}
							</div>
							{#if !selectedResultHasUsableMetrics}
								<div data-testid="selected-result-status-banner" class={`mt-3 rounded border px-3 py-2 text-sm ${statusBadgeClass(selectedResultStatus)}`}>
									{#if selectedResultStatus === 'failed'}
										This run failed before producing usable result artifacts.
									{:else}
										This run is still in progress. Refresh the strategy history after it finishes.
									{/if}
									{#if selectedResultErrorDetail}
										<div data-testid="selected-result-error-detail" class="mt-2 font-mono text-xs opacity-90">{selectedResultErrorDetail}</div>
									{/if}
								</div>
							{/if}
							{#if selectedResultHasUsableMetrics}
							<div class="mt-4 rounded border border-[#1f1f1f] bg-[#070707] p-4">
								<div class="flex flex-wrap items-center justify-between gap-3">
									<div>
										<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Trade chart</div>
										<div class="mt-1 text-sm text-gray-400">Candles, trades, decision indicators, and the exact params captured for this run.</div>
										{#if selectedChartBars.length > 0}
											<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
												<span class="rounded-full border border-[#2b2b2b] bg-black px-2.5 py-1" data-testid="selected-chart-bar-count">
													{fmtBarCount(selectedChartBars.length)}
												</span>
												<span class="rounded-full border border-[#2b2b2b] bg-black px-2.5 py-1" data-testid="selected-chart-view-mode">
													Full history
												</span>
												<span class="rounded-full border border-[#2b2b2b] bg-black px-2.5 py-1" data-testid="selected-chart-range">
													{fmtShortDate(selectedChartStart)} -> {fmtShortDate(selectedChartEnd)}
												</span>
											</div>
										{/if}
									</div>
									{#if chartLoading}
										<div class="rounded-full border border-cyan-900/60 bg-cyan-950/20 px-2.5 py-1 text-[11px] text-cyan-200" data-testid="selected-chart-loading-chip">
											Building chart...
										</div>
									{:else if selectedChartContext}
										<div class="rounded-full border border-[#2b2b2b] bg-black px-2.5 py-1 text-[11px] text-gray-400" data-testid="selected-chart-source">
											{selectedChartContext.source === 'artifact' ? 'Stored snapshot' : 'Recomputed'}
										</div>
									{/if}
								</div>
								{#if chartContextError}
									<div class="mt-3 flex items-center justify-between gap-3 rounded-xl border border-amber-900/50 bg-amber-950/20 px-3 py-3 text-sm text-amber-200" data-testid="selected-result-chart-error">
										<span>{chartContextError}</span>
										{#if selectedResultItem}
											<button
												type="button"
												class="shrink-0 rounded border border-amber-700/60 bg-amber-900/30 px-2.5 py-1 text-xs text-amber-100 transition hover:bg-amber-800/40 disabled:opacity-40"
												data-testid="selected-result-chart-retry"
												disabled={chartLoading}
												on:click={() => { if (selectedResultItem) void openResult(selectedResultItem); }}
											>
												Retry chart
											</button>
										{/if}
									</div>
								{/if}
								{#if selectedChartWarnings.length > 0}
									<div class="mt-3 space-y-2">
										{#each selectedChartWarnings as warning}
											<div class="rounded-xl border border-amber-900/50 bg-amber-950/10 px-3 py-2 text-[11px] text-amber-200">
												{warning}
											</div>
										{/each}
									</div>
								{/if}
								{#if chartLoading}
									<div class="mt-3 rounded-xl border border-cyan-900/40 bg-cyan-950/10 px-4 py-6 text-sm text-cyan-100" data-testid="selected-result-chart-loading">
										Loading chart candles, trade markers, and decision overlays. Result details are ready below while this finishes.
									</div>
								{:else if selectedChartContext && selectedChartBars.length > 0}
									<div class="mt-3 h-[420px] overflow-hidden rounded-2xl border border-[#111] bg-black" data-testid="selected-result-chart">
										<ChartWorkspace
											data={selectedChartBars}
											entryMarkers={selectedChartEntryMarkers}
											exitMarkers={selectedChartExitMarkers}
											mainIndicators={selectedChartMainIndicators}
											subIndicators={selectedChartSubIndicators}
											strategyName={selectedChartContext.strategy_name}
											strategyMeta={selectedChartContext.strategy_meta}
											strategyParams={selectedChartContext.strategy_params}
											showStrategyInfo={true}
											windowSize={0}
											fitContentToken={chartFitContentToken}
										/>
									</div>
								{:else if selectedChartContext && !chartContextError}
									<div class="mt-3 rounded border border-[#1f1f1f] bg-black px-4 py-6 text-sm text-gray-500">
										No local OHLCV bars were available to render this run.
									</div>
								{/if}
							</div>
							{#if selectedResultHasEquityCurve}
								<div class="mt-3 rounded border border-[#1f1f1f] bg-black p-3" data-testid="selected-result-equity-curve">
									<div class="flex flex-wrap items-center justify-between gap-2">
										<div>
											<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Equity Curve</div>
											<div class="mt-1 text-xs text-gray-500">
												{#if selectedResultUsingFullCurve}
													Entire backtest — in-sample shaded, out-of-sample bright (OOS divider marked); buy &amp; hold (amber dashed); drawdown subchart. Metrics are OOS-only.
												{:else}
													Strategy equity (cyan), buy &amp; hold benchmark (amber dashed), and the drawdown subchart.
												{/if}
											</div>
										</div>
										<div class="flex items-center gap-3 text-[10px] uppercase tracking-wide text-gray-500">
											{#if selectedResultUsingFullCurve}
												<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-cyan-400/40"></span>In-sample</span>
											{/if}
											<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-cyan-400"></span>{selectedResultUsingFullCurve ? 'Out-of-sample' : 'Strategy'}</span>
											{#if benchmarkCurveForChart && benchmarkCurveForChart.length > 0}
												<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-amber-400"></span>Buy &amp; Hold</span>
											{/if}
											<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-red-500/60"></span>Drawdown</span>
										</div>
									</div>
									<div class="mt-3">
										{#key selectedResultId}
											<EquityChart
												data={equityCurveForChart ?? []}
												benchmarkData={benchmarkCurveForChart}
												oosStartTimestamp={oosStartTimestampForChart}
												showDrawdown={true}
												height={320}
											/>
										{/key}
									</div>
								</div>
							{/if}
							<div class="mt-3 rounded border border-[#1f1f1f] bg-black px-3 py-2" data-testid="selected-result-metrics-strip">
								<div class="flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:items-stretch">
									<div class="rounded border border-cyan-900/30 bg-cyan-950/10 px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-cyan-500/80">In-sample (IS)</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={isResultCagrReliable(selectedResult) ? 'In-sample CAGR (annualized)' : 'Short window (<1 month) — annualized value may be noisy'}><span class="text-[10px] uppercase text-gray-500 mr-1">CAGR</span> <span data-testid="selected-result-in-sample-cagr" class={isResultCagrReliable(selectedResult) ? signedPercentClass(readResultInSampleCagr(selectedResult)) : 'text-gray-500'}>{formatResultInSampleCagr(selectedResult)}</span></div>
											<div title={isResultSharpeReliable(selectedResult) ? 'In-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}><span class="text-[10px] uppercase text-gray-500 mr-1">Sharpe</span> <span data-testid="selected-result-in-sample-sharpe" class={isResultSharpeReliable(selectedResult) ? 'text-gray-300' : 'text-gray-500'}>{formatResultInSampleSharpe(selectedResult)}</span></div>
										</div>
									</div>
									<div class="rounded border border-emerald-900/30 bg-emerald-950/10 px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-emerald-500/80">Out-of-sample (OOS)</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={isResultCagrReliable(selectedResult) ? 'Out-of-sample CAGR (annualized)' : 'Short OOS window (<1 month) — annualized value may be noisy'}><span class="text-[10px] uppercase text-gray-500 mr-1">CAGR</span> <span data-testid="selected-result-cagr" class={isResultCagrReliable(selectedResult) ? signedPercentClass(readResultOutOfSampleCagr(selectedResult)) : 'text-gray-500'}>{formatResultOutOfSampleCagr(selectedResult)}</span></div>
											<div title={isResultSharpeReliable(selectedResult) ? 'Out-of-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}><span class="text-[10px] uppercase text-gray-500 mr-1">Sharpe</span> <span data-testid="selected-result-sharpe" class={isResultSharpeReliable(selectedResult) ? 'text-gray-300' : 'text-gray-500'}>{formatResultOutOfSampleSharpe(selectedResult)}</span></div>
											<div title="Cumulative out-of-sample return (not annualized)"><span class="text-[10px] uppercase text-gray-500 mr-1">Return</span> <span data-testid="selected-result-total-return" class={readResultPercentMetric(selectedResult, 'total_return_pct', 'total_return') >= 0 ? 'text-emerald-400' : 'text-red-400'}>{pct(readResultPercentMetric(selectedResult, 'total_return_pct', 'total_return'))}</span></div>
											<div><span class="text-[10px] uppercase text-gray-500 mr-1">Max DD</span> <span data-testid="selected-result-max-drawdown" class="text-red-400">{pct(readResultDrawdownPercentMetric(selectedResult, 'max_drawdown_pct', 'max_drawdown'))}</span></div>
											<div><span class="text-[10px] uppercase text-gray-500 mr-1">Win%</span> <span data-testid="selected-result-win-rate" class="text-gray-300">{pct(readResultPercentMetric(selectedResult, 'win_rate', 'win_rate_pct'))}</span></div>
											<div><span class="text-[10px] uppercase text-gray-500 mr-1">Trades</span> <span data-testid="selected-result-trades" class="text-gray-300">{formatResultTradesCount(selectedResult)}</span></div>
											<div title={readResultFlag(selectedResult, 'profit_factor_is_infinite') === true ? 'No losing trades — profit factor is mathematically infinite' : 'Gross profit / gross loss'}><span class="text-[10px] uppercase text-gray-500 mr-1">PF</span> <span class="text-gray-300">{formatResultProfitFactor(selectedResult)}</span></div>
										</div>
									</div>
									<div class="rounded border border-[#222] bg-[#070707] px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-gray-500">Gauntlet</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={`Gauntlet robustness score; below ${gauntletMinScore ?? 60} fails the promotion gate`}><span class="text-[10px] uppercase text-gray-500 mr-1">Rob%</span> <span data-testid="selected-result-robustness" class="text-gray-300">{formatResultRobustness(selectedResult)}</span></div>
										</div>
									</div>
									{#if readResultCoverage(selectedResult, 'funding_coverage_pct') !== null || readResultCoverage(selectedResult, 'open_interest_coverage_pct') !== null}
										<div class="rounded border border-[#222] bg-[#070707] px-3 py-2" data-testid="selected-result-data-coverage">
											<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-gray-500">Data</div>
											<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
												<div title="Share of the backtest window with funding-rate data. Low coverage means funding costs are under-measured."><span class="text-[10px] uppercase text-gray-500 mr-1">Funding</span> <span class={coverageToneClass(readResultCoverage(selectedResult, 'funding_coverage_pct'))}>{formatCoveragePct(readResultCoverage(selectedResult, 'funding_coverage_pct'))}</span></div>
												<div title="Share of the backtest window with open-interest data. OI accumulates forward from snapshots and cannot be backfilled."><span class="text-[10px] uppercase text-gray-500 mr-1">OI</span> <span class={coverageToneClass(readResultCoverage(selectedResult, 'open_interest_coverage_pct'))}>{formatCoveragePct(readResultCoverage(selectedResult, 'open_interest_coverage_pct'))}</span></div>
											</div>
										</div>
									{/if}
								</div>
							</div>
							{#if readResultDataQualityFlags(selectedResult).length > 0}
								<div class="mt-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2" role="alert" data-testid="selected-result-data-quality-banner">
									<div class="text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-400">Data quality hold — metrics quarantined</div>
									<ul class="mt-1 list-disc pl-5 text-xs text-amber-200">
										{#each readResultDataQualityFlags(selectedResult) as flag}
											<li>{flag}</li>
										{/each}
									</ul>
									<p class="mt-1 text-[11px] text-amber-300/80">These numbers are implausible (engine/data bug signature) and are excluded from gate decisions. Re-run the backtest once data coverage has converged.</p>
								</div>
							{/if}
							{#if selectedResultRiskMetrics.length > 0}
								<div class="mt-3 rounded border border-[#1f1f1f] bg-black px-3 py-3" data-testid="selected-result-risk-metrics">
									<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Risk-adjusted metrics</div>
									<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
										{#each selectedResultRiskMetrics as metric}
											<div class="rounded border border-[#1a1a1a] bg-[#070707] px-2.5 py-2" title={metric.title}>
												<div class="text-[9px] uppercase tracking-wide text-gray-500">{metric.label}</div>
												<div class={`mt-1 font-mono text-sm ${riskMetricToneClass(metric.tone)}`}>{metric.value}</div>
											</div>
										{/each}
									</div>
								</div>
							{/if}
							{#if selectedResultMonthlyHeatmap}
								<div class="mt-3 overflow-x-auto rounded border border-[#1f1f1f] bg-black px-3 py-3" data-testid="selected-result-monthly-heatmap">
									<div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Monthly returns</div>
									<div class="mt-1 text-xs text-gray-500">Month-over-month equity change derived from the equity curve. Green = gain, red = loss.</div>
									<div class="mt-2">
										<HeatmapChart
											data={selectedResultMonthlyHeatmap.data}
											xLabels={selectedResultMonthlyHeatmap.xLabels}
											yLabels={selectedResultMonthlyHeatmap.yLabels}
											colorScale="diverging"
											width={Math.max(560, selectedResultMonthlyHeatmap.xLabels.length * 46 + 80)}
											height={Math.max(140, selectedResultMonthlyHeatmap.yLabels.length * 30 + 60)}
											valueFormat={(v) => `${v.toFixed(1)}%`}
										/>
									</div>
								</div>
							{/if}
							{#if selectedResultComparison}
								<div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 rounded border border-[#1f1f1f] bg-[#070707] px-3 py-2 text-xs">
									<div><span class="text-[10px] uppercase text-gray-500 mr-1">Sharpe Rank</span> <span class="font-mono text-white">#{selectedResultComparison.sharpeRank}/{selectedResultComparison.sampleSize}</span> <span class="text-gray-500">({selectedResultComparison.sharpePercentile}p)</span></div>
									<div><span class="text-[10px] uppercase text-gray-500 mr-1">Sharpe vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.sharpeDeltaVsMedian)}`}>{comparisonDeltaLabel(selectedResultComparison.sharpeDeltaVsMedian)}</span></div>
									<div><span class="text-[10px] uppercase text-gray-500 mr-1">Return vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.returnDeltaVsMedian)}`}>{comparisonDeltaLabel(selectedResultComparison.returnDeltaVsMedian, { suffix: '%' })}</span></div>
									<div><span class="text-[10px] uppercase text-gray-500 mr-1">DD vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.drawdownDeltaVsMedian, { inverse: true })}`}>{comparisonDeltaLabel(selectedResultComparison.drawdownDeltaVsMedian, { inverse: true, suffix: '%' })}</span></div>
								</div>
							{/if}
								{#if isOptimizationResult()}
								<div class="mt-2 rounded border border-blue-900/40 bg-blue-950/10 px-2 py-1.5 text-[11px] text-blue-200">
									{Object.keys(getOptBestParams() || {}).length} optimized parameters available
								</div>
							{/if}
							{/if}
							</div>
					{/if}

					{#if selectedResult?.trades?.length}
						<div class="mt-4 rounded border border-[#222] bg-[#090909]" data-testid="selected-result-trades">
							<div class="flex flex-wrap items-center gap-2 border-b border-[#1a1a1a] px-3 py-2">
								<span class="text-[10px] uppercase tracking-wide text-gray-500">Out-of-sample trades ({selectedResult.trades.length})</span>
								<span class="rounded-full border border-emerald-900/40 bg-emerald-950/20 px-2 py-0.5 text-[9px] uppercase tracking-wide text-emerald-300/80" title="The trade list reflects out-of-sample execution only.">OOS</span>
							</div>
							<div class="max-h-[480px] overflow-auto">
								<table class="w-full text-xs">
									<thead class="sticky top-0 bg-[#0d0d0d] text-gray-500">
										<tr>
											<th class="px-2 py-2 text-right">#</th>
											<th class="px-2 py-2 text-left">Dir</th>
											<th class="px-2 py-2 text-left">Entry Time</th>
											<th class="px-2 py-2 text-right">Entry</th>
											<th class="px-2 py-2 text-left">Exit Time</th>
											<th class="px-2 py-2 text-right">Exit</th>
											{#if selectedTradesHaveExitReason}
												<th class="px-2 py-2 text-left">Exit Reason</th>
											{/if}
											{#if selectedTradesHaveSizeFraction}
												<th class="px-2 py-2 text-right">Size</th>
											{/if}
											<th class="px-2 py-2 text-right">PnL $</th>
											<th class="px-2 py-2 text-right">PnL%</th>
											<th class="px-2 py-2 text-right">MAE%</th>
											<th class="px-2 py-2 text-right">MFE%</th>
											<th class="px-2 py-2 text-right">Bars</th>
										</tr>
									</thead>
									<tbody>
										{#each selectedResult.trades as trade, i}
											<tr class="border-t border-[#111] hover:bg-[#111]">
												<td class="px-2 py-1.5 text-right font-mono text-gray-600">{i + 1}</td>
												<td class="px-2 py-1.5 {trade.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}">{trade.direction ?? 'long'}</td>
												<td class="px-2 py-1.5 font-mono text-gray-400">{fmtDate(trade.entry_time)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-gray-300">{asNumber(trade.entry_price, 0).toFixed(2)}</td>
												<td class="px-2 py-1.5 font-mono text-gray-400">{fmtDate(trade.exit_time)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-gray-300">{asNumber(trade.exit_price, 0).toFixed(2)}</td>
												{#if selectedTradesHaveExitReason}
													<td class="px-2 py-1.5 text-left font-mono text-gray-400">{tradeExitReason(trade) ?? '-'}</td>
												{/if}
												{#if selectedTradesHaveSizeFraction}
													<td class="px-2 py-1.5 text-right font-mono text-gray-400">{tradeSizeFraction(trade) != null ? `${(tradeSizeFraction(trade)! * 100).toFixed(1)}%` : '-'}</td>
												{/if}
												<td class="px-2 py-1.5 text-right font-mono {asNumber(trade.pnl, 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{asNumber(trade.pnl, 0) >= 0 ? '+' : ''}${asNumber(trade.pnl, 0).toFixed(2)}</td>
												<td class="px-2 py-1.5 text-right font-mono {asNumber(trade.return_pct, 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{pct(trade.return_pct)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-red-400/60">{trade.mae != null ? pct(trade.mae) : '-'}</td>
												<td class="px-2 py-1.5 text-right font-mono text-emerald-400/60">{trade.mfe != null ? pct(trade.mfe) : '-'}</td>
												<td class="px-2 py-1.5 text-right font-mono text-gray-400">{trade.bars_held ?? '-'}</td>
											</tr>
										{/each}
									</tbody>
									{#if selectedResultTradeSummary}
										<tfoot class="sticky bottom-0 border-t-2 border-[#222] bg-[#0d0d0d] text-gray-400">
											<tr>
												<td class="px-2 py-2 text-[10px] uppercase tracking-wide text-gray-500" colspan={selectedTradeColumnCount}>
													<div class="flex flex-wrap gap-x-4 gap-y-1 font-mono normal-case" data-testid="selected-result-trade-summary">
														<span><span class="text-gray-500">Wins</span> <span class="text-emerald-400">{selectedResultTradeSummary.wins}</span> / <span class="text-gray-500">Losses</span> <span class="text-red-400">{selectedResultTradeSummary.losses}</span>{#if selectedResultTradeSummary.breakeven > 0} / <span class="text-gray-500">BE</span> <span class="text-gray-400">{selectedResultTradeSummary.breakeven}</span>{/if}</span>
														<span><span class="text-gray-500">Win%</span> <span class="text-gray-300">{selectedResultTradeSummary.winRatePct.toFixed(1)}%</span></span>
														<span><span class="text-gray-500">Avg win</span> <span class="text-emerald-400">{formatSignedCurrency(selectedResultTradeSummary.avgWin)}</span></span>
														<span><span class="text-gray-500">Avg loss</span> <span class="text-red-400">{formatSignedCurrency(-selectedResultTradeSummary.avgLoss)}</span></span>
														<span><span class="text-gray-500">Payoff</span> <span class="text-gray-300">{selectedResultTradeSummary.payoffRatio != null ? selectedResultTradeSummary.payoffRatio.toFixed(2) : '∞'}</span></span>
														<span><span class="text-gray-500">Largest win</span> <span class="text-emerald-400">{formatSignedCurrency(selectedResultTradeSummary.largestWin)}</span></span>
														<span><span class="text-gray-500">Largest loss</span> <span class="text-red-400">{formatSignedCurrency(selectedResultTradeSummary.largestLoss)}</span></span>
														<span><span class="text-gray-500">Expectancy</span> <span class={selectedResultTradeSummary.expectancy >= 0 ? 'text-emerald-400' : 'text-red-400'}>{formatSignedCurrency(selectedResultTradeSummary.expectancy)}</span></span>
														<span><span class="text-gray-500">Win streak</span> <span class="text-emerald-400">{selectedResultTradeSummary.longestWinStreak}</span></span>
														<span><span class="text-gray-500">Loss streak</span> <span class="text-red-400">{selectedResultTradeSummary.longestLossStreak}</span></span>
													</div>
												</td>
											</tr>
										</tfoot>
									{/if}
								</table>
							</div>
						</div>
					{/if}
				</div>
			{/if}

			{#if activeTab === 'execution'}
				<div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
					<div class="rounded border border-[#222] bg-[#090909]">
						<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-gray-500">Positions</div>
						{#if executionPositions.length === 0}
							<div class="px-3 py-4 text-xs text-gray-600">No positions recorded.</div>
						{:else}
							<div class="max-h-[480px] overflow-auto">
								<table class="w-full text-xs">
									<thead class="bg-[#0d0d0d] text-gray-500">
										<tr>
											<th class="px-3 py-2 text-left">ID</th>
											<th class="px-3 py-2 text-left">Asset</th>
											<th class="px-3 py-2 text-left">Side</th>
											<th class="px-3 py-2 text-right">Size</th>
											<th class="px-3 py-2 text-left">Status</th>
										</tr>
									</thead>
									<tbody>
										{#each executionPositions as row, index}
											<tr class="border-t border-[#111]">
												<td class="px-3 py-2 font-mono text-cyan-300">{getRowId(row, `pos-${index}`)}</td>
												<td class="px-3 py-2 text-gray-300">{getString(row, 'asset')}</td>
												<td class="px-3 py-2 text-gray-300">{getString(row, 'direction')}</td>
												<td class="px-3 py-2 text-right font-mono text-gray-400">{asNumber(row.size, 0).toFixed(4)}</td>
												<td class="px-3 py-2 text-gray-300">{getString(row, 'status')}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</div>

					<div class="rounded border border-[#222] bg-[#090909]">
						<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-gray-500">Trades</div>
						{#if executionTrades.length === 0}
							<div class="px-3 py-4 text-xs text-gray-600">No trades recorded.</div>
						{:else}
							<div class="max-h-[480px] overflow-auto">
								<table class="w-full text-xs">
									<thead class="bg-[#0d0d0d] text-gray-500">
										<tr>
											<th class="px-3 py-2 text-left">ID</th>
											<th class="px-3 py-2 text-left">Asset</th>
											<th class="px-3 py-2 text-left">Side</th>
											<th class="px-3 py-2 text-right">Entry</th>
											<th class="px-3 py-2 text-right">PnL%</th>
										</tr>
									</thead>
									<tbody>
										{#each executionTrades as row, index}
											<tr class="border-t border-[#111]">
												<td class="px-3 py-2 font-mono text-cyan-300">{getRowId(row, `trade-${index}`)}</td>
												<td class="px-3 py-2 text-gray-300">{getString(row, 'asset')}</td>
												<td class="px-3 py-2 text-gray-300">{getString(row, 'direction')}</td>
												<td class="px-3 py-2 text-right font-mono text-gray-400">{asNumber(row.entry_price, 0).toFixed(4)}</td>
												<td class="px-3 py-2 text-right font-mono {(asNumber(row.pnl_pct, 0) >= 0) ? 'text-emerald-400' : 'text-red-400'}">{pct(row.pnl_pct)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{:else}
		<!-- Defensive: container is null but not loading/errored (e.g. a future early-return
		     path). Degrade to an empty state rather than rendering a blank pane. -->
		<div class="flex-1 flex items-center justify-center">
			<div class="rounded border border-[#2b2b2b] bg-[#0a0a0a] px-4 py-3 text-sm text-gray-400">
				Strategy not found.
				<a href={returnTo} class="ml-1 text-cyan-300 underline hover:text-cyan-200">Go back</a>
			</div>
		</div>
	{/if}
</div>

{#if tradingViewExportScript}
	<TradingViewExportModal
		script={tradingViewExportScript}
		filename={tradingViewExportFilename}
		warnings={tradingViewExportWarnings}
		toastLink={`/lab/strategy/${encodeURIComponent(strategyId)}`}
		on:close={closeTradingViewExport}
	/>
{/if}
