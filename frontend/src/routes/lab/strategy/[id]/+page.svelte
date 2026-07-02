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
		submitBacktest,
		submitOptimization,
		updateStrategyDisplayName,
		type BacktestResult,
		type Dataset,
		type PipelineSettings,
		type ResultChartContext,
		type StrategyContainerHistoryItem,
		type StrategyContainerPayload,
	} from '$lib/api';
	import { getPrebuiltStrategies } from '$lib/api/strategies';
	import {
		getStrategyOpenPosition,
		updateStrategyDefaultParams,
		type OpenPositionUpdate,
		type StrategyOpenPosition,
	} from '$lib/api/backtesting';
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
	import ExecutionSettingsFields from '$lib/components/lab/ExecutionSettingsFields.svelte';
	import type { SizingMode, ExecutionProfileDraft } from '$lib/types/executionProfile';
	import {
		getPipelineConfig,
		getStrategyExecutionGrowth,
		type GauntletTestEntry,
		type GauntletTestKey,
		type PipelineThresholds,
	} from '$lib/api/lifecycle';
	import { closePaperPosition } from '$lib/api/paper';
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
	import StrategyExportMenu from '$lib/components/strategy/StrategyExportMenu.svelte';
	import StrategyImportDialog from '$lib/components/strategy/StrategyImportDialog.svelte';
	import StageControl from '$lib/components/strategy/StageControl.svelte';
	import type { StrategyImportResult } from '$lib/api';
	import { openDeepdive } from '$lib/stores/deepdiveStore';

	let showImportDialog = false;

	function onStrategyImported(result: StrategyImportResult): void {
		showImportDialog = false;
		if (result.ok && result.strategy_id) {
			void goto(`/lab/strategy/${encodeURIComponent(result.strategy_id)}`);
		}
	}

	// ── Display-name editing ─────────────────────────────────────────────────────
	// Friendly name override for the container; falls back to the canonical
	// {ASSET}-{TYPE}-{ID} name when blank.
	let editingName = false;
	let nameDraft = '';
	let savingName = false;

	function startEditName(): void {
		if (!container) return;
		nameDraft = container.strategy.display_name ?? '';
		editingName = true;
	}

	function cancelEditName(): void {
		editingName = false;
		nameDraft = '';
	}

	async function saveName(): Promise<void> {
		if (!container || savingName) return;
		const strategyIdValue = container.strategy.id;
		const next = nameDraft.trim();
		// No-op if unchanged (treat null/'' as equivalent).
		if ((container.strategy.display_name ?? '') === next) {
			cancelEditName();
			return;
		}
		savingName = true;
		try {
			const res = await updateStrategyDisplayName(strategyIdValue, next || null);
			// Reassign container so Svelte picks up the nested change.
			container = {
				...container,
				strategy: { ...container.strategy, display_name: res.display_name },
			};
			editingName = false;
			nameDraft = '';
			addToast(
				res.display_name ? `Renamed to "${res.display_name}"` : 'Display name reset to default',
				'success',
				`/lab/strategy/${encodeURIComponent(strategyIdValue)}`,
			);
		} catch (err) {
			addToast(
				err instanceof Error ? err.message : 'Failed to update display name',
				'error',
				`/lab/strategy/${encodeURIComponent(strategyIdValue)}`,
			);
		} finally {
			savingName = false;
		}
	}

	function focusAndSelect(node: HTMLInputElement): void {
		node.focus();
		node.select();
	}

	function onNameKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			void saveName();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelEditName();
		}
	}

	// TabKey identifiers predate the current UI labels. Mapping (code -> visible label):
	//   'overview'       -> "Overview" (stage pipeline, readiness, gauntlet status, active run)
	//   'backtests'      -> "Gauntlet" (params + run form + history; "Run the Gauntlet" submits a backtest)
	//   'optimizations'  -> "Optimization"
	//   'robustness'     -> "Robustness" (the five validation runners)
	// PromotionReadiness on:action maps: 'run_confirmation_backtest' -> 'backtests';
	//   'run_optimization'/'apply_best_params' -> 'optimizations'; '*_validation_suite' -> 'robustness'.
	type TabKey = 'overview' | 'backtests' | 'optimizations' | 'robustness' | 'execution';
	type SubmitStatus = 'idle' | 'submitting' | 'running' | 'completed' | 'failed';
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
	const DEFAULT_EXECUTION_LEVERAGE = '1';
	const EXECUTION_PARAM_KEYS = new Set([
		'initial_capital',
		'fee_bps',
		'slippage_bps',
		'leverage',
		'sizing_mode',
		'risk_per_trade',
		'fixed_size',
		'atr_stop_multiplier',
		'kelly_multiplier',
		'kelly_lookback',
		'stop_loss_pct',
		'take_profit_pct',
		'trailing_stop_pct',
		'time_stop_bars',
	]);
	const STRICTLY_POSITIVE_EXECUTION_RANGE_KEYS = new Set([
		'initial_capital',
		'leverage',
		'risk_per_trade',
		'fixed_size',
		'atr_stop_multiplier',
		'kelly_multiplier',
		'kelly_lookback',
		'stop_loss_pct',
		'take_profit_pct',
		'trailing_stop_pct',
		'time_stop_bars',
	]);
	// The sizing parameter(s) each mode needs, with their default seed values. When an
	// operator switches Sizing Mode, applySizingModeDefaults() fills any of these that are
	// still blank so a freshly-selected mode is immediately valid/runnable instead of
	// surfacing a "must be greater than 0" error on an empty required field. Values mirror
	// the engine defaults in forven/strategies/backtest.py _resolve_execution_controls
	// (risk_per_trade 0.02, atr_stop_multiplier 2.0, kelly_multiplier 0.5, kelly_lookback
	// 100). Fixed notional has no engine default — an empty value sizes at full equity — so
	// it seeds from the configured Initial Capital (a 1x notional reference) at apply time,
	// falling back to this constant.
	type SizingNumericField = 'risk_per_trade' | 'fixed_size' | 'atr_stop_multiplier' | 'kelly_multiplier' | 'kelly_lookback';
	const SIZING_MODE_FIELD_DEFAULTS: Record<SizingMode, Partial<Record<SizingNumericField, string>>> = {
		full: {},
		fraction: { risk_per_trade: '0.02' },
		fixed: { fixed_size: '10000' },
		atr: { risk_per_trade: '0.02', atr_stop_multiplier: '2' },
		kelly: { kelly_multiplier: '0.5', kelly_lookback: '100' },
	};
	// Fraction risk sizing needs a stop distance (the engine sizes by risk_per_trade / stop),
	// and validateExecutionDraft rejects the mode without a stop loss or trailing stop. Seed
	// this default stop loss when Fraction is selected and neither is set, so it's runnable
	// in one click. ATR risk derives its stop from atr_stop_multiplier, so it needs no seed.
	const FRACTION_DEFAULT_STOP_LOSS_PCT = '2';

	let strategyId = '';
	let returnTo = '/lab';
	let activeTab: TabKey = 'overview';
	let loading = true;
	let error = '';
	let lastLoadedId = '';
	let container: StrategyContainerPayload | null = null;
	// Header-level stage control; owns the promote/demote/archive confirm flow
	// (including the gate-blocked -> informed-override path) from ANY tab.
	let stageControl: StageControl | null = null;
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
	let expandedBacktestParamSummaryIds: Record<string, boolean> = {};
	let backtestParamDrafts: Record<string, Record<string, unknown>> = {};
	let backtestParamRunnerId: string | null = null;
	let resultLoading = false;
	let resultError = '';
	let settingDefaultParams = false;
	let backtestingOptParams = false;
	let paramsDraft: Record<string, unknown> = {};
	let executionDraft: ExecutionProfileDraft = buildFallbackExecutionDraft();
	let containerExecutionDefaults: ExecutionProfileDraft = buildFallbackExecutionDraft();
	let gauntletDraftSource: 'defaults' | 'run' = 'defaults';
	let gauntletDraftSourceId = '';
	let executionDraftError = '';
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
	let optimizationExecutionDrafts: Record<string, OptimizationParamDraft> = {};
	let optimizationExecutionDraftSource = '';
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

	// The container's default backtest context (symbol/timeframe from the configuration +
	// the first/pinned run's window, captured at load). Clicking a history row overrides
	// backtestForm; "Reset to Defaults" must restore THIS, not whatever the last-clicked
	// run happened to use.
	let containerBacktestDefaults = {
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

	let selectedRobustnessTest: GauntletTestKey = 'walk_forward';
	let robustnessStatusOverrides: Partial<Record<GauntletTestKey, GauntletTestEntry>> = {};

	// ── Overview: active-run snapshot ────────────────────────────────────────────
	// The run whose params/metrics drive paper/live (pinned) or, absent a pin, the
	// most recent Gauntlet run. Its full result is fetched lazily for the Overview
	// equity curve.
	let overviewResult: BacktestResult | null = null;
	let overviewResultId = '';
	let overviewResultError = '';
	let overviewResultLoading = false;
	$: overviewEquityOos = overviewResult?.equity_curve ?? null;
	$: overviewEquityFull = overviewResult?.equity_curve_full ?? null;
	$: overviewUsingFullCurve = Array.isArray(overviewEquityFull) && overviewEquityFull.length > 1;
	$: overviewEquityForChart = overviewUsingFullCurve ? (overviewEquityFull ?? []) : (overviewEquityOos ?? []);
	$: overviewBenchmarkForChart = overviewUsingFullCurve
		? (overviewResult?.benchmark_curve_full ?? null)
		: (overviewResult?.benchmark_curve ?? null);
	$: overviewOosStartTimestamp =
		overviewUsingFullCurve && Array.isArray(overviewEquityOos) && overviewEquityOos.length > 0
			? (overviewEquityOos[0]?.timestamp ?? null)
			: null;
	$: overviewHasEquity = Array.isArray(overviewEquityForChart) && overviewEquityForChart.length > 1;

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
	$: pinnedBacktestId = (container?.strategy?.pinned_backtest_id ?? '').toString().trim();
	// The active/default Gauntlet run is pinned to the very top of the list (and
	// rendered green) regardless of the current sort, so the run actually driving
	// paper/live is always the first row the operator sees.
	$: backtestHistory = withPinnedFirst(
		sortBacktestHistory(backtestHistoryRaw, historySortBy, historySortDir),
		pinnedBacktestId
	);
	$: historySortIndicator = (field: HistorySortField): string =>
		historySortBy === field ? (historySortDir === 'desc' ? ' \u2193' : ' \u2191') : '';
	// The run driving execution: the pinned run, else the newest by creation time.
	// Derived from the RAW list so re-sorting the history table can't change it.
	$: activeRunItem =
		(pinnedBacktestId
			? backtestHistoryRaw.find((item) => item.result_id === pinnedBacktestId)
			: null) ??
		[...backtestHistoryRaw].sort(
			(left, right) => parseTimestamp(right.created_at) - parseTimestamp(left.created_at),
		)[0] ??
		null;
	$: if (activeTab === 'overview' && activeRunItem && String(activeRunItem.result_id || '').trim() !== overviewResultId) {
		void loadOverviewResult(activeRunItem);
	}
	$: optimizationHistory = container?.history.optimizations ?? [];
	$: walkForwardHistory = container?.history.walk_forward ?? [];
	$: validationHistory = container?.history.validation ?? [];
	$: quickScreenRows = buildQuickScreenEvidenceRows({
		strategy: container?.strategy ?? null,
		backtests: backtestHistory,
		pipelineSettings,
	});
	$: executionTrades = container?.execution.trades ?? [];
	$: executionPositions = container?.execution.positions ?? [];
	$: executionGrowth = buildExecutionGrowth(growthTrades ?? executionTrades);

	// ── Overview: realized growth from actual paper/live trading ────────────────
	// The paper engine trades an isolated book of $10k + realized PnL, so for a
	// pure-paper strategy `$10k + cumsum(pnl)` IS the real book equity. Live trades
	// are sized off the real wallet, so with any live fills the curve degrades to
	// cumulative realized PnL (anchored at 0) rather than implying a fake book.
	const PAPER_START_EQUITY = 10000;

	type ExecutionGrowth = {
		points: EquityPoint[];
		mode: 'paper' | 'pnl';
		tradeCount: number;
		totalPnl: number;
	};

	function buildExecutionGrowth(trades: Record<string, unknown>[]): ExecutionGrowth | null {
		const closed = trades
			.map((row) => {
				const closedAt = typeof row.closed_at === 'string' && row.closed_at.trim() ? row.closed_at : null;
				const status = String(row.status ?? '').trim().toUpperCase();
				if (!closedAt || status === 'OPEN') return null;
				const closedTs = Date.parse(closedAt);
				if (!Number.isFinite(closedTs)) return null;
				const pnl = [row.pnl_usd, row.pnl]
					.map((value) => Number(value))
					.find((value) => Number.isFinite(value));
				if (pnl === undefined) return null;
				const openedAt = typeof row.opened_at === 'string' && row.opened_at.trim() ? row.opened_at : null;
				const openedTs = openedAt ? Date.parse(openedAt) : Number.NaN;
				return {
					closedAt,
					closedTs,
					openedAt: Number.isFinite(openedTs) ? openedAt : null,
					openedTs: Number.isFinite(openedTs) ? openedTs : null,
					pnl,
					executionType: String(row.execution_type ?? '').trim().toLowerCase(),
				};
			})
			.filter((trade): trade is NonNullable<typeof trade> => trade !== null)
			.sort((left, right) => left.closedTs - right.closedTs);
		if (closed.length === 0) return null;

		const allPaper = closed.every((trade) => trade.executionType.includes('paper'));
		const base = allPaper ? PAPER_START_EQUITY : 0;
		const points: EquityPoint[] = [];
		// Anchor the curve at the earliest open so a single closed trade still draws a
		// segment and the start of trading is visible.
		const anchor = closed.reduce<{ ts: number; at: string } | null>((best, trade) => {
			if (trade.openedTs === null || trade.openedAt === null) return best;
			return !best || trade.openedTs < best.ts ? { ts: trade.openedTs, at: trade.openedAt } : best;
		}, null);
		if (anchor && anchor.ts < closed[0].closedTs) {
			points.push({ timestamp: anchor.at, equity: base });
		}
		let equity = base;
		for (const trade of closed) {
			equity += trade.pnl;
			points.push({ timestamp: trade.closedAt, equity });
		}
		return {
			points,
			mode: allPaper ? 'paper' : 'pnl',
			tradeCount: closed.length,
			totalPnl: equity - base,
		};
	}

	// ── Gauntlet run comparison ──────────────────────────────────────────────────
	// Pick two history runs to diff params, compare headline metrics, and overlay
	// their equity curves (run B rides the chart's benchmark series).
	let compareSelection: string[] = [];
	let compareResults: Record<string, BacktestResult | null> = {};
	let compareLoading = false;
	let compareError = '';

	$: compareItemA = compareSelection.length > 0
		? backtestHistoryRaw.find((item) => item.result_id === compareSelection[0]) ?? null
		: null;
	$: compareItemB = compareSelection.length > 1
		? backtestHistoryRaw.find((item) => item.result_id === compareSelection[1]) ?? null
		: null;
	$: comparePairReady = Boolean(compareItemA && compareItemB);
	$: compareResultA = compareItemA ? compareResults[compareItemA.result_id] ?? null : null;
	$: compareResultB = compareItemB ? compareResults[compareItemB.result_id] ?? null : null;
	$: compareCurveA = compareResultA ? (compareResultA.equity_curve_full ?? compareResultA.equity_curve ?? null) : null;
	$: compareCurveB = compareResultB ? (compareResultB.equity_curve_full ?? compareResultB.equity_curve ?? null) : null;
	$: compareOverlayReady =
		Array.isArray(compareCurveA) && compareCurveA.length > 1 &&
		Array.isArray(compareCurveB) && compareCurveB.length > 1;
	$: compareParamDiff = compareItemA && compareItemB
		? buildCompareParamDiff(compareItemA, compareItemB)
		: [];
	$: compareChangedParamCount = compareParamDiff.filter((row) => !row.same).length;
	$: compareMetricRows = compareItemA && compareItemB
		? buildCompareMetricRows(compareItemA, compareItemB)
		: [];

	type CompareMetricRow = { label: string; a: string; b: string; delta: number | null; higherIsBetter: boolean };

	function toggleCompareSelection(item: StrategyContainerHistoryItem): void {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		if (compareSelection.includes(resultId)) {
			compareSelection = compareSelection.filter((id) => id !== resultId);
			return;
		}
		// Keep at most two: the earliest pick stays as A, a third pick replaces B.
		compareSelection = compareSelection.length >= 2
			? [compareSelection[0], resultId]
			: [...compareSelection, resultId];
		void loadCompareResults();
	}

	function clearCompareSelection(): void {
		compareSelection = [];
		compareError = '';
	}

	async function loadCompareResults(): Promise<void> {
		const targets = compareSelection.filter((id) => !(id in compareResults));
		if (targets.length === 0) return;
		compareLoading = true;
		compareError = '';
		try {
			for (const resultId of targets) {
				const result = await getResult(resultId).catch((err: unknown) => {
					compareError = err instanceof Error ? err.message : 'Failed to load run for comparison';
					return null;
				});
				if (destroyed) return;
				compareResults = { ...compareResults, [resultId]: result ?? null };
			}
		} finally {
			compareLoading = false;
		}
	}

	function buildCompareParamDiff(
		itemA: StrategyContainerHistoryItem,
		itemB: StrategyContainerHistoryItem,
	): Array<{ key: string; a: string; b: string; same: boolean }> {
		const paramsA = getHistoryParams(itemA);
		const paramsB = getHistoryParams(itemB);
		const keys = Array.from(new Set([...Object.keys(paramsA), ...Object.keys(paramsB)])).sort();
		return keys.map((key) => ({
			key,
			a: key in paramsA ? formatBacktestParamChipValue(paramsA[key]) : '—',
			b: key in paramsB ? formatBacktestParamChipValue(paramsB[key]) : '—',
			same: stableStringify({ v: paramsA[key] }) === stableStringify({ v: paramsB[key] }),
		}));
	}

	function buildCompareMetricRows(
		itemA: StrategyContainerHistoryItem,
		itemB: StrategyContainerHistoryItem,
	): CompareMetricRow[] {
		const rows: Array<{
			label: string;
			read: (item: StrategyContainerHistoryItem) => number | null;
			format: (item: StrategyContainerHistoryItem) => string;
			higherIsBetter: boolean;
		}> = [
			{ label: 'OOS CAGR', read: readOutOfSampleCagr, format: formatOutOfSampleCagr, higherIsBetter: true },
			{ label: 'OOS Sharpe', read: readOutOfSampleSharpe, format: formatOutOfSampleSharpe, higherIsBetter: true },
			{
				label: 'Max DD',
				read: (item) => readDrawdownPercentMetricOptional(item, 'max_drawdown_pct', 'max_drawdown'),
				format: (item) => pct(readDrawdownPercentMetric(item, 'max_drawdown_pct', 'max_drawdown')),
				higherIsBetter: false,
			},
			{
				label: 'Win%',
				read: (item) => readPercentMetricOptional(item, 'win_rate', 'win_rate_pct'),
				format: (item) => pct(readPercentMetric(item, 'win_rate', 'win_rate_pct')),
				higherIsBetter: true,
			},
			{
				label: 'Trades',
				read: (item) => readMetricOptional(item, 'total_trades', 'trades'),
				format: (item) => historyTradesCount(item),
				higherIsBetter: true,
			},
			{
				label: 'PF',
				read: (item) => readMetricOptional(item, 'profit_factor', 'pf'),
				format: formatProfitFactor,
				higherIsBetter: true,
			},
			{ label: 'Rob%', read: readRobustness, format: formatRobustness, higherIsBetter: true },
		];
		return rows.map((row) => {
			const valueA = row.read(itemA);
			const valueB = row.read(itemB);
			return {
				label: row.label,
				a: row.format(itemA),
				b: row.format(itemB),
				delta: valueA !== null && valueB !== null ? valueB - valueA : null,
				higherIsBetter: row.higherIsBetter,
			};
		});
	}

	function compareDeltaClass(row: CompareMetricRow): string {
		if (row.delta === null || Math.abs(row.delta) < 1e-9) return 'text-[#555]';
		const improved = row.higherIsBetter ? row.delta > 0 : row.delta < 0;
		return improved ? 'text-emerald-400' : 'text-red-400';
	}

	function compareDeltaLabel(row: CompareMetricRow): string {
		if (row.delta === null) return '—';
		const sign = row.delta > 0 ? '+' : '';
		return `${sign}${row.delta.toFixed(2)}`;
	}

	// Lifecycle stage changes that fall inside the growth window, drawn as markers on
	// the growth curve so "the line changed here" connects to "because the stage changed".
	$: growthAnnotations = buildGrowthAnnotations(orderedRecentEvents, executionGrowth);

	function buildGrowthAnnotations(
		events: typeof orderedRecentEvents,
		growth: ExecutionGrowth | null,
	): Array<{ timestamp: string; label: string; color?: string }> {
		if (!growth || growth.points.length === 0) return [];
		const startTs = Date.parse(growth.points[0].timestamp);
		const endTs = Date.parse(growth.points[growth.points.length - 1].timestamp);
		if (!Number.isFinite(startTs) || !Number.isFinite(endTs)) return [];
		// Allow a day of slack at the end so a just-now stage change still shows.
		const windowEnd = endTs + 86_400_000;
		return events
			.filter((event) => {
				const ts = Date.parse(event.created_at || '');
				return Number.isFinite(ts) && ts >= startTs && ts <= windowEnd;
			})
			.map((event) => ({
				timestamp: event.created_at,
				label: `→ ${lifecycleStageLabel(event.to_state)}`,
				color: '#a78bfa',
			}))
			.slice(0, 20);
	}

	// ── Full growth series (uncapped) ────────────────────────────────────────────
	// The container payload slices the 500 most-recent trades; the dedicated
	// execution-growth endpoint returns EVERY closed trade so long-lived books
	// don't render a silently-truncated curve. Falls back to the container slice.
	let growthTrades: Record<string, unknown>[] | null = null;
	let growthTradesLoadedFor = '';

	$: if (activeTab === 'overview' && container && strategyId && growthTradesLoadedFor !== strategyId) {
		growthTradesLoadedFor = strategyId;
		void loadGrowthTrades(strategyId);
	}

	async function loadGrowthTrades(targetStrategyId: string): Promise<void> {
		try {
			const response = await getStrategyExecutionGrowth(targetStrategyId);
			if (destroyed || strategyId !== targetStrategyId) return;
			growthTrades = Array.isArray(response?.trades)
				? (response.trades as unknown as Record<string, unknown>[])
				: null;
		} catch {
			if (destroyed || strategyId !== targetStrategyId) return;
			growthTrades = null;
		}
	}

	// ── Backtest ↔ reality parity ────────────────────────────────────────────────
	// The trades table stores signed slippage (bps, positive = adverse) and the
	// realized round-trip cost drag (pnl_pct − net_pnl_pct, fractions incl. leverage).
	// Comparing them against the execution profile's modeled costs turns "paper looks
	// worse than the backtest" into a number.
	type ExecutionParity = {
		entrySlipBps: number | null;
		entryCount: number;
		exitSlipBps: number | null;
		exitCount: number;
		costDragPct: number | null;
		costCount: number;
		avgLeverage: number;
	};

	function meanOf(values: number[]): number | null {
		return values.length > 0 ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
	}

	function buildExecutionParity(trades: Record<string, unknown>[]): ExecutionParity | null {
		const entrySlips: number[] = [];
		const exitSlips: number[] = [];
		const costDrags: number[] = [];
		const leverages: number[] = [];
		for (const row of trades) {
			const entrySlip = Number(row.entry_slippage_bps);
			if (Number.isFinite(entrySlip)) entrySlips.push(entrySlip);
			const exitSlip = Number(row.exit_slippage_bps);
			if (Number.isFinite(exitSlip)) exitSlips.push(exitSlip);
			const gross = Number(row.pnl_pct);
			const net = Number(row.net_pnl_pct);
			if (Number.isFinite(gross) && Number.isFinite(net)) costDrags.push((gross - net) * 100);
			const leverage = Number(row.leverage);
			if (Number.isFinite(leverage) && leverage > 0) leverages.push(leverage);
		}
		if (entrySlips.length === 0 && exitSlips.length === 0 && costDrags.length === 0) return null;
		return {
			entrySlipBps: meanOf(entrySlips),
			entryCount: entrySlips.length,
			exitSlipBps: meanOf(exitSlips),
			exitCount: exitSlips.length,
			costDragPct: meanOf(costDrags),
			costCount: costDrags.length,
			avgLeverage: meanOf(leverages) ?? 1,
		};
	}

	$: executionParity = buildExecutionParity(executionTrades);
	$: modeledSlippageBps = optionalNumber(containerExecutionDefaults.slippage_bps) ?? null;
	$: modeledFeeBps = optionalNumber(containerExecutionDefaults.fee_bps) ?? null;
	// Realized fees include leverage (fees_pct = 2 × fee × leverage), so the modeled
	// round-trip budget scales by the average realized leverage for a fair comparison.
	$: modeledCostDragPct =
		modeledFeeBps !== null && executionParity ? (2 * modeledFeeBps * executionParity.avgLeverage) / 100 : null;

	function parityTone(realized: number | null, modeled: number | null): string {
		if (realized === null) return 'text-[#555]';
		if (modeled === null) return 'text-[#aaa]';
		if (modeled <= 0) return realized <= 0 ? 'text-emerald-400' : 'text-yellow-400';
		if (realized <= modeled * 1.25) return 'text-emerald-400';
		if (realized <= modeled * 2) return 'text-yellow-400';
		return 'text-red-400';
	}

	function fmtBps(value: number | null): string {
		return value === null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(1)} bps`;
	}

	// ── Live allocation ramp (configured schedule — advisory) ───────────────────
	type LiveRampInfo = {
		daysLive: number;
		week: number;
		allocationPct: number | null;
		killSwitchPct: number | null;
	};

	$: liveRampInfo =
		currentLifecycleStage === 'live_graduated' ? buildLiveRampInfo(container, pipelineThresholds) : null;

	function buildLiveRampInfo(
		payload: StrategyContainerPayload | null,
		thresholds: PipelineThresholds | null,
	): LiveRampInfo | null {
		if (!payload) return null;
		const strategyRecord = payload.strategy as unknown as Record<string, unknown>;
		const sinceRaw = String(
			strategyRecord.stage_changed_at ?? payload.strategy.state_changed_at ?? payload.strategy.created_at ?? '',
		).trim();
		const since = Date.parse(sinceRaw);
		if (!Number.isFinite(since)) return null;
		const daysLive = Math.max(0, Math.floor((Date.now() - since) / 86_400_000));
		const week = Math.floor(daysLive / 7) + 1;
		const schedule = thresholds?.live_graduated?.allocation_schedule ?? [];
		let allocationPct: number | null = null;
		for (const rung of schedule) {
			if (week >= rung.week_start && week <= rung.week_end) {
				allocationPct = rung.allocation_pct;
				break;
			}
		}
		if (allocationPct === null && schedule.length > 0 && week > schedule[schedule.length - 1].week_end) {
			allocationPct = schedule[schedule.length - 1].allocation_pct;
		}
		return {
			daysLive,
			week,
			allocationPct,
			killSwitchPct: thresholds?.live_graduated?.decay_kill_switch_pct ?? null,
		};
	}

	// ── Execution tab: rich trade/position views ─────────────────────────────────
	$: executionClosedTrades = executionTrades.filter((row) => String(row.status ?? '').trim().toUpperCase() === 'CLOSED');
	$: executionOpenTrades = executionTrades.filter((row) => String(row.status ?? '').trim().toUpperCase() === 'OPEN');
	$: executionRealizedSummary = buildExecutionRealizedSummary(executionClosedTrades);
	$: paperSessionId = String(container?.strategy.paper_session_id ?? '').trim();
	let closingPosition = false;

	type ExecutionRealizedSummary = {
		count: number;
		wins: number;
		losses: number;
		winRatePct: number;
		totalPnlUsd: number;
		profitFactor: number | null;
		avgNetPct: number | null;
	};

	function tradeRowPnlUsd(row: Record<string, unknown>): number | null {
		for (const key of ['pnl_usd', 'pnl'] as const) {
			const value = Number(row[key]);
			if (Number.isFinite(value)) return value;
		}
		return null;
	}

	// pnl_pct / net_pnl_pct are stored as FRACTIONS (0.05 = 5%) — scale for display.
	function tradeRowNetPct(row: Record<string, unknown>): number | null {
		for (const key of ['net_pnl_pct', 'pnl_pct'] as const) {
			const value = Number(row[key]);
			if (Number.isFinite(value)) return value * 100;
		}
		return null;
	}

	function tradeRowFeesPct(row: Record<string, unknown>): number | null {
		const value = Number(row.fees_pct);
		return Number.isFinite(value) ? value * 100 : null;
	}

	function tradeRowSlipBps(row: Record<string, unknown>, key: 'entry_slippage_bps' | 'exit_slippage_bps'): number | null {
		const value = Number(row[key]);
		return Number.isFinite(value) ? value : null;
	}

	function tradeRowSignal(row: Record<string, unknown>): Record<string, unknown> {
		const raw = row.signal_data;
		if (raw && typeof raw === 'object' && !Array.isArray(raw)) return raw as Record<string, unknown>;
		if (typeof raw === 'string' && raw.trim()) {
			try {
				const parsed = JSON.parse(raw);
				if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
			} catch {
				return {};
			}
		}
		return {};
	}

	function tradeRowCloseReason(row: Record<string, unknown>): string {
		const reason = String(tradeRowSignal(row).close_reason ?? '').trim();
		return reason ? reason.replace(/[_-]+/g, ' ') : '-';
	}

	function tradeRowSignalNumber(row: Record<string, unknown>, key: string): number | null {
		const value = Number(tradeRowSignal(row)[key]);
		return Number.isFinite(value) ? value : null;
	}

	function tradeRowPrice(row: Record<string, unknown>, fillKey: string, signalKey: string): string {
		for (const key of [fillKey, signalKey]) {
			const value = Number(row[key]);
			if (Number.isFinite(value) && value > 0) return value.toFixed(4);
		}
		return '-';
	}

	function buildExecutionRealizedSummary(trades: Record<string, unknown>[]): ExecutionRealizedSummary | null {
		if (trades.length === 0) return null;
		let wins = 0;
		let losses = 0;
		let grossWin = 0;
		let grossLoss = 0;
		let totalPnl = 0;
		const netPcts: number[] = [];
		for (const row of trades) {
			const pnl = tradeRowPnlUsd(row) ?? 0;
			totalPnl += pnl;
			if (pnl > 0) {
				wins += 1;
				grossWin += pnl;
			} else if (pnl < 0) {
				losses += 1;
				grossLoss += Math.abs(pnl);
			}
			const netPct = tradeRowNetPct(row);
			if (netPct !== null) netPcts.push(netPct);
		}
		return {
			count: trades.length,
			wins,
			losses,
			winRatePct: trades.length > 0 ? (wins / trades.length) * 100 : 0,
			totalPnlUsd: totalPnl,
			profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
			avgNetPct: meanOf(netPcts),
		};
	}

	async function closeOpenPosition(): Promise<void> {
		if (!paperSessionId || closingPosition) return;
		const prompt =
			"Close this strategy's open position at market?\n\nDispatches on the trade's execution type — paper closes at a fresh mid, live sends a reduce-only market order.";
		if (typeof window !== 'undefined' && !window.confirm(prompt)) return;
		closingPosition = true;
		try {
			await closePaperPosition(paperSessionId, 'Manual close from strategy container');
			addToast('Position close requested', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Failed to close position', 'error');
		} finally {
			closingPosition = false;
		}
	}
	// execution_profile is persisted INSIDE params (the canonical home the
	// optimizer/gauntlet read) but is NOT an alpha param. Strip it everywhere the
	// alpha-param draft is built so (a) it never shows in the ParameterEditor and
	// (b) the strategyParams<->paramsDraft dirty comparison stays symmetric — both
	// sides must omit it, or the draft reads as permanently "Unsaved".
	function stripExecutionProfile(value: unknown): Record<string, unknown> {
		if (!(value && typeof value === 'object' && !Array.isArray(value))) return {};
		const out: Record<string, unknown> = {};
		for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
			if (key !== 'execution_profile' && !EXECUTION_PARAM_KEYS.has(key)) out[key] = entry;
		}
		return out;
	}

	$: strategyParams = stripExecutionProfile(container?.configuration.params);
	// The FULL set of active default params — including execution_profile and the
	// execution param keys that `strategyParams` strips. The Gauntlet-history "changed"
	// chip compares against this so execution params (leverage, execution_profile, …)
	// are not perpetually flagged amber just because the alpha-param view omits them.
	// For the active/pinned run, whose stored params ARE the strategy default, this
	// makes every chip read as unchanged (no yellow).
	$: strategyDefaultsFull = extractParamRecord(container?.configuration.params);
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
	$: containerExecutionDefaults = buildContainerExecutionDefaults(container, pipelineSettings);
	$: executionDirty = !areExecutionDraftsEqual(containerExecutionDefaults, executionDraft);
	$: executionDraftError = validateExecutionDraft(executionDraft) ?? '';
	$: paramsDirty = !areParameterRecordsEqual(strategyParams, paramsDraft);
	$: gauntletDraftDirty = paramsDirty || executionDirty;
	// When NO backtest run is pinned, the manually-saved container defaults are what
	// drive paper/live — i.e. the Gauntlet Parameters themselves are the active params.
	// Surface that with a green treatment on the card (mirrors the green active row in
	// the history table) once the draft is saved/synced rather than mid-edit.
	$: gauntletParamsAreActive =
		!pinnedBacktestId && gauntletDraftSource === 'defaults' && !gauntletDraftDirty;
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
				: gauntletDraftDirty
					? 'Draft tuned and ready'
					: 'Ready to launch';
	$: backtestRunStateTone = submitStatus === 'failed'
		? 'border-red-900/40 bg-red-950/20 text-red-200'
		: submitStatus === 'completed'
			? 'border-emerald-900/40 bg-emerald-950/20 text-emerald-200'
			: submitStatus === 'submitting' || submitStatus === 'running'
				? 'border-[#333] bg-[#0c0c0c] text-white'
				: gauntletDraftDirty
					? 'border-yellow-900 bg-yellow-500/5 text-yellow-400'
					: 'border-[#333] bg-[#0c0c0c] text-white';
	$: backtestRunSummary = submitStatus === 'submitting' || submitStatus === 'running'
		? submitProgress || submitMessage || 'Submitting the current draft to the backtest engine.'
		: submitStatus === 'completed'
			? submitMessage || 'The latest backtest finished successfully.'
			: submitStatus === 'failed'
				? submitMessage || 'The latest submit failed validation or execution.'
				: gauntletDraftDirty
					? 'This draft has local changes and the next backtest will use them immediately.'
					: 'The working draft matches the saved defaults for this strategy.';
	$: syncOptimizationParamDrafts(strategyParams);
	$: syncOptimizationExecutionDrafts(executionDraft);
	$: optimizationParamSelectedCount = Object.values(optimizationParamDrafts).filter((draft) => draft.selected).length;
	$: optimizationExecutionSelectedCount = Object.values(optimizationExecutionDrafts).filter((draft) => draft.selected).length;
	$: allOptimizationParamsSelected =
		Object.keys(optimizationParamDrafts).length > 0 &&
		optimizationParamSelectedCount === Object.keys(optimizationParamDrafts).length;
	$: someOptimizationParamsSelected = optimizationParamSelectedCount > 0 && !allOptimizationParamsSelected;
	$: allOptimizationExecutionSelected =
		Object.keys(optimizationExecutionDrafts).length > 0 &&
		optimizationExecutionSelectedCount === Object.keys(optimizationExecutionDrafts).length;
	$: someOptimizationExecutionSelected = optimizationExecutionSelectedCount > 0 && !allOptimizationExecutionSelected;
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
		if (value === null || !Number.isFinite(value)) return 'text-[#555]';
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
		return 'border-[#333] bg-[#0c0c0c] text-white';
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

	function asPlainRecord(value: unknown): Record<string, unknown> {
		return (!value || typeof value !== 'object' || Array.isArray(value)) ? {} : value as Record<string, unknown>;
	}

	function numberDraftValue(value: unknown, fallback = ''): string {
		if (value === null || value === undefined || value === '') return fallback;
		const parsed = Number(value);
		return Number.isFinite(parsed) ? String(parsed) : fallback;
	}

	// Execution-draft fields are string-typed, but Svelte's bind:value on
	// type="number" inputs writes back NUMBERS (or '' when cleared). Normalize
	// before any string ops — a direct `.trim()` on an edited field throws.
	function draftText(value: unknown): string {
		return value == null ? '' : String(value);
	}

	function optionalNumber(value: string | number): number | undefined {
		const text = String(value ?? '').trim();
		if (!text) return undefined;
		const parsed = Number(text);
		return Number.isFinite(parsed) ? parsed : undefined;
	}

	function leverageDraftValue(value: unknown, fallback = DEFAULT_EXECUTION_LEVERAGE): string {
		const text = String(value ?? '').trim();
		const parsed = text ? Number(text) : Number.NaN;
		if (Number.isFinite(parsed) && parsed > 0 && parsed <= 125) return String(parsed);
		const fallbackParsed = Number(fallback);
		if (Number.isFinite(fallbackParsed) && fallbackParsed > 0 && fallbackParsed <= 125) return String(fallbackParsed);
		return DEFAULT_EXECUTION_LEVERAGE;
	}

	function validSizingMode(value: unknown): SizingMode {
		const normalized = String(value ?? '').trim().toLowerCase();
		if (normalized === 'fraction' || normalized === 'fixed' || normalized === 'atr' || normalized === 'kelly') return normalized;
		return 'full';
	}

	function buildFallbackExecutionDraft(): ExecutionProfileDraft {
		return {
			initial_capital: '10000',
			fee_bps: '4.5',
			slippage_bps: '2',
			leverage: DEFAULT_EXECUTION_LEVERAGE,
			sizing_mode: 'full',
			risk_per_trade: '0.02',
			fixed_size: '10000',
			atr_stop_multiplier: '2',
			kelly_multiplier: '0.5',
			kelly_lookback: '100',
			stop_loss_pct: '',
			take_profit_pct: '',
			trailing_stop_pct: '',
			time_stop_bars: '',
		};
	}

	// Seed the newly-selected sizing mode's parameters with sensible defaults, but only
	// where the operator hasn't already supplied (or loaded from a run) a value — so
	// switching modes never clobbers an explicit entry. Fixed notional prefers the current
	// Initial Capital (a 1x notional reference) over the static fallback.
	function applySizingModeDefaults(mode: SizingMode): void {
		const defaults = SIZING_MODE_FIELD_DEFAULTS[mode];
		const next: ExecutionProfileDraft = { ...executionDraft };
		let changed = false;
		for (const [key, fallback] of Object.entries(defaults) as [SizingNumericField, string][]) {
			if (String(next[key] ?? '').trim()) continue;
			if (key === 'fixed_size') {
				const capital = optionalNumber(next.initial_capital);
				next.fixed_size = capital !== undefined && capital > 0 ? String(capital) : fallback;
			} else {
				next[key] = fallback;
			}
			changed = true;
		}
		// Make Fraction risk runnable in one click: seed a stop loss only when the operator
		// has set neither a stop loss nor a trailing stop, so an existing trailing stop is
		// never overridden.
		if (mode === 'fraction' && !draftText(next.stop_loss_pct).trim() && !draftText(next.trailing_stop_pct).trim()) {
			next.stop_loss_pct = FRACTION_DEFAULT_STOP_LOSS_PCT;
			changed = true;
		}
		if (changed) executionDraft = next;
	}

	function buildExecutionDraftFromRecord(record: Record<string, unknown>, base: ExecutionProfileDraft): ExecutionProfileDraft {
		const nested = asPlainRecord(record.execution_profile);
		const source = { ...record, ...nested };
		return {
			initial_capital: numberDraftValue(source.initial_capital, base.initial_capital),
			fee_bps: numberDraftValue(source.fee_bps, base.fee_bps),
			slippage_bps: numberDraftValue(source.slippage_bps, base.slippage_bps),
			leverage: leverageDraftValue(source.leverage, base.leverage),
			sizing_mode: validSizingMode(source.sizing_mode ?? base.sizing_mode),
			risk_per_trade: numberDraftValue(source.risk_per_trade, base.risk_per_trade),
			fixed_size: numberDraftValue(source.fixed_size, base.fixed_size),
			atr_stop_multiplier: numberDraftValue(source.atr_stop_multiplier, base.atr_stop_multiplier),
			kelly_multiplier: numberDraftValue(source.kelly_multiplier, base.kelly_multiplier),
			kelly_lookback: numberDraftValue(source.kelly_lookback, base.kelly_lookback),
			stop_loss_pct: numberDraftValue(source.stop_loss_pct, base.stop_loss_pct),
			take_profit_pct: numberDraftValue(source.take_profit_pct, base.take_profit_pct),
			trailing_stop_pct: numberDraftValue(source.trailing_stop_pct, base.trailing_stop_pct),
			time_stop_bars: numberDraftValue(source.time_stop_bars, base.time_stop_bars),
		};
	}

	// `containerArg`/`settingsArg` are passed explicitly from the reactive block so
	// Svelte tracks `container` and `pipelineSettings` as dependencies. A bare
	// `buildContainerExecutionDefaults()` call in a `$:` statement names no reactive
	// identifiers, so the compiler computes it ONCE at init (container still null) and
	// never recomputes — which froze `containerExecutionDefaults` at fallback defaults
	// and left `executionDirty` permanently true on every strategy. Imperative callers
	// (loadContainer) rely on the defaults to read the live module-level values.
	function buildContainerExecutionDefaults(
		containerArg: typeof container = container,
		settingsArg: typeof pipelineSettings = pipelineSettings,
	): ExecutionProfileDraft {
		const fallback = buildFallbackExecutionDraft();
		const configuration = asPlainRecord(containerArg?.configuration);
		const params = extractParamRecord(configuration.params);
		const settings = asPlainRecord(settingsArg);
		return buildExecutionDraftFromRecord(
			{
				initial_capital: configuration.initial_capital ?? params.initial_capital,
				fee_bps: configuration.fee_bps ?? settings.backtest_fee_bps,
				slippage_bps: configuration.slippage_bps ?? settings.backtest_slippage_bps,
				leverage: configuration.leverage ?? params.leverage,
				sizing_mode: configuration.sizing_mode ?? params.sizing_mode,
				risk_per_trade: configuration.risk_per_trade ?? params.risk_per_trade,
				fixed_size: configuration.fixed_size ?? params.fixed_size,
				atr_stop_multiplier: configuration.atr_stop_multiplier ?? params.atr_stop_multiplier,
				kelly_multiplier: configuration.kelly_multiplier ?? params.kelly_multiplier,
				kelly_lookback: configuration.kelly_lookback ?? params.kelly_lookback,
				stop_loss_pct: configuration.stop_loss_pct ?? params.stop_loss_pct,
				take_profit_pct: configuration.take_profit_pct ?? params.take_profit_pct,
				trailing_stop_pct: configuration.trailing_stop_pct ?? params.trailing_stop_pct,
				time_stop_bars: configuration.time_stop_bars ?? params.time_stop_bars,
				execution_profile: configuration.execution_profile ?? params.execution_profile,
			},
			fallback,
		);
	}

	function getHistoryExecutionDraft(item: StrategyContainerHistoryItem): ExecutionProfileDraft {
		return buildExecutionDraftFromRecord(asPlainRecord(item.config), containerExecutionDefaults);
	}

	function areExecutionDraftsEqual(left: ExecutionProfileDraft, right: ExecutionProfileDraft): boolean {
		return stableStringify(left) === stableStringify(right);
	}

	function executionDraftToPayload(draft: ExecutionProfileDraft): Record<string, unknown> {
		const payload: Record<string, unknown> = {
			sizing_mode: draft.sizing_mode,
		};
		for (const key of ['initial_capital', 'fee_bps', 'slippage_bps'] as const) {
			const value = optionalNumber(draft[key]);
			if (value !== undefined) payload[key] = value;
		}
		const leverage = optionalNumber(draft.leverage);
		if (leverage !== undefined && leverage > 0 && leverage <= 125) payload.leverage = leverage;
		if (draft.sizing_mode === 'fraction' || draft.sizing_mode === 'atr') {
			const risk = optionalNumber(draft.risk_per_trade);
			if (risk !== undefined) payload.risk_per_trade = risk;
		}
		if (draft.sizing_mode === 'fixed') {
			const fixed = optionalNumber(draft.fixed_size);
			if (fixed !== undefined) payload.fixed_size = fixed;
		}
		if (draft.sizing_mode === 'atr') {
			const atr = optionalNumber(draft.atr_stop_multiplier);
			if (atr !== undefined) payload.atr_stop_multiplier = atr;
		}
		if (draft.sizing_mode === 'kelly') {
			const multiplier = optionalNumber(draft.kelly_multiplier);
			const lookback = optionalNumber(draft.kelly_lookback);
			if (multiplier !== undefined) payload.kelly_multiplier = multiplier;
			if (lookback !== undefined) payload.kelly_lookback = Math.trunc(lookback);
		}
		for (const key of ['stop_loss_pct', 'take_profit_pct', 'trailing_stop_pct', 'time_stop_bars'] as const) {
			const value = optionalNumber(draft[key]);
			if (value !== undefined) payload[key] = key === 'time_stop_bars' ? Math.trunc(value) : value;
		}
		return payload;
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
			if (Object.keys(expandedBacktestParamSummaryIds).length > 0) {
				expandedBacktestParamSummaryIds = {};
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
		const retainedSummaryIds: Record<string, boolean> = {};
		for (const [resultId, expanded] of Object.entries(expandedBacktestParamSummaryIds)) {
			if (expanded && resultId in next) retainedSummaryIds[resultId] = true;
		}
		if (Object.keys(retainedSummaryIds).length !== Object.keys(expandedBacktestParamSummaryIds).length) {
			expandedBacktestParamSummaryIds = retainedSummaryIds;
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

	function toggleBacktestParamSummary(item: StrategyContainerHistoryItem): void {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		const next = { ...expandedBacktestParamSummaryIds };
		if (next[resultId]) {
			delete next[resultId];
		} else {
			next[resultId] = true;
		}
		expandedBacktestParamSummaryIds = next;
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
		if (!(key in strategyDefaultsFull)) return true;
		return stableStringify(strategyDefaultsFull[key]) !== stableStringify(value);
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

	function getBacktestVisibleParamSummary(item: StrategyContainerHistoryItem, expanded = false): Array<{ key: string; value: string; changed: boolean }> {
		const summary = getBacktestParamSummary(item);
		return expanded ? summary : summary.slice(0, 4);
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
		if (value === null) return 'text-[#555]';
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

	// Hoist the pinned/active run to the front of the (already sorted) list so it is
	// always the first row, no matter which column the operator sorts by.
	function withPinnedFirst(
		items: StrategyContainerHistoryItem[],
		pinnedId: string
	): StrategyContainerHistoryItem[] {
		if (!pinnedId) return items;
		const index = items.findIndex((item) => item.result_id === pinnedId);
		if (index <= 0) return items;
		const next = items.slice();
		const [pinned] = next.splice(index, 1);
		next.unshift(pinned);
		return next;
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
		if (value === null) return 'text-[#555]';
		if (value >= 95) return 'text-emerald-400';
		if (value >= 50) return 'text-yellow-400';
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
		return 'text-[#aaa]';
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
		if (normalized === 'optimization') return 'text-[#888] border-[#333]';
		if (normalized === 'walk_forward') return 'text-[#888] border-[#333]';
		return 'text-[#888] border-[#333]';
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
		if (value === null || !Number.isFinite(value)) return 'text-[#555]';
		const adjusted = inverse ? value * -1 : value;
		if (adjusted > 0) return 'text-emerald-400';
		if (adjusted < 0) return 'text-red-400';
		return 'text-[#aaa]';
	}

	function historyCardBorder(type: string | null | undefined): string {
		const normalized = String(type ?? '').trim().toLowerCase();
		if (normalized === 'optimization') return 'hover:border-[#555]';
		if (normalized === 'walk_forward') return 'hover:border-[#555]';
		return 'hover:border-[#555]';
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
		return validateDateRange(backtestForm.start_date, backtestForm.end_date) ?? validateExecutionDraft(executionDraft);
	}

	function validateExecutionDraft(draft: ExecutionProfileDraft): string | null {
		const initialCapital = optionalNumber(draft.initial_capital);
		if (draftText(draft.initial_capital).trim() && (initialCapital === undefined || initialCapital <= 0)) return 'Initial capital must be greater than 0.';
		const fee = optionalNumber(draft.fee_bps);
		if (draftText(draft.fee_bps).trim() && (fee === undefined || fee < 0)) return 'Fee bps cannot be negative.';
		const slippage = optionalNumber(draft.slippage_bps);
		if (draftText(draft.slippage_bps).trim() && (slippage === undefined || slippage < 0)) return 'Slippage bps cannot be negative.';
		const leverage = optionalNumber(draft.leverage);
		if (draftText(draft.leverage).trim() && (leverage === undefined || leverage <= 0 || leverage > 125)) return 'Leverage must be greater than 0 and no more than 125.';
		if (draft.sizing_mode === 'fraction' || draft.sizing_mode === 'atr') {
			const risk = optionalNumber(draft.risk_per_trade);
			if (risk === undefined || risk <= 0 || risk > 1) return 'Risk per trade must be between 0 and 1.';
		}
		if (draft.sizing_mode === 'fraction' && !draftText(draft.stop_loss_pct).trim() && !draftText(draft.trailing_stop_pct).trim()) {
			return 'Fraction sizing needs a stop loss or trailing stop.';
		}
		if (draft.sizing_mode === 'fixed') {
			const fixed = optionalNumber(draft.fixed_size);
			if (fixed === undefined || fixed <= 0) return 'Fixed notional must be greater than 0.';
		}
		if (draft.sizing_mode === 'atr') {
			const atr = optionalNumber(draft.atr_stop_multiplier);
			if (atr === undefined || atr <= 0) return 'ATR stop multiplier must be greater than 0.';
		}
		if (draft.sizing_mode === 'kelly') {
			const multiplier = optionalNumber(draft.kelly_multiplier);
			if (multiplier === undefined || multiplier <= 0 || multiplier > 5) return 'Kelly multiplier must be between 0 and 5.';
			const lookback = optionalNumber(draft.kelly_lookback);
			if (lookback === undefined || lookback < 1 || !Number.isInteger(lookback)) return 'Kelly lookback must be a positive whole number.';
		}
		for (const [key, label, max] of [
			['stop_loss_pct', 'Stop loss %', 100],
			['take_profit_pct', 'Take profit %', 1000],
			['trailing_stop_pct', 'Trailing stop %', 100],
		] as const) {
			const value = optionalNumber(draft[key]);
			if (draftText(draft[key]).trim() && (value === undefined || value <= 0 || value > max)) return `${label} must be greater than 0 and no more than ${max}.`;
		}
		const timeStop = optionalNumber(draft.time_stop_bars);
		if (draftText(draft.time_stop_bars).trim() && (timeStop === undefined || timeStop < 1 || !Number.isInteger(timeStop))) return 'Time stop must be a positive whole number of bars.';
		return null;
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

	function createOptimizationExecutionParamDraft(key: string, value: number): OptimizationParamDraft {
		const draft = createOptimizationParamDraft(key, value);
		if (STRICTLY_POSITIVE_EXECUTION_RANGE_KEYS.has(key) && Number(draft.min) <= 0) {
			draft.min = draft.kind === 'int' ? '1' : formatOptimizationInputValue(Math.max(Number(draft.step), 0.000001), draft.kind);
		}
		return draft;
	}

	function syncOptimizationParamDrafts(params: Record<string, unknown>) {
		const optimizableEntries = Object.entries(params)
			.filter(([key, value]) => !EXECUTION_PARAM_KEYS.has(key) && isOptimizableNumericParam(value))
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

	function optimizationExecutionValues(draft: ExecutionProfileDraft): Record<string, number> {
		const values: Record<string, number> = {};
		const alwaysAvailable = ['leverage', 'stop_loss_pct', 'take_profit_pct', 'trailing_stop_pct', 'time_stop_bars'] as const;
		for (const key of alwaysAvailable) {
			const value = optionalNumber(draft[key]);
			if (value !== undefined) values[key] = value;
		}
		if (draft.sizing_mode === 'fraction' || draft.sizing_mode === 'atr') {
			const value = optionalNumber(draft.risk_per_trade);
			if (value !== undefined) values.risk_per_trade = value;
		}
		if (draft.sizing_mode === 'fixed') {
			const value = optionalNumber(draft.fixed_size);
			if (value !== undefined) values.fixed_size = value;
		}
		if (draft.sizing_mode === 'atr') {
			const value = optionalNumber(draft.atr_stop_multiplier);
			if (value !== undefined) values.atr_stop_multiplier = value;
		}
		if (draft.sizing_mode === 'kelly') {
			const multiplier = optionalNumber(draft.kelly_multiplier);
			const lookback = optionalNumber(draft.kelly_lookback);
			if (multiplier !== undefined) values.kelly_multiplier = multiplier;
			if (lookback !== undefined) values.kelly_lookback = lookback;
		}
		return values;
	}

	function syncOptimizationExecutionDrafts(draft: ExecutionProfileDraft): void {
		const entries = Object.entries(optimizationExecutionValues(draft));
		const source = stableStringify(Object.fromEntries(entries));
		if (source === optimizationExecutionDraftSource) return;
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		for (const [key, value] of entries) {
			const existing = optimizationExecutionDrafts[key];
			const kind = optimizationParamKind(value);
			if (existing && existing.current === value && existing.kind === kind) {
				nextDrafts[key] = existing;
				continue;
			}
			nextDrafts[key] = createOptimizationExecutionParamDraft(key, value);
		}
		optimizationExecutionDrafts = nextDrafts;
		optimizationExecutionDraftSource = source;
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

	function validateOptimizationExecutionDraft(draft: OptimizationParamDraft): string {
		const baseError = validateOptimizationParamDraft(draft);
		if (baseError) return baseError;
		const min = Number(draft.min);
		const max = Number(draft.max);
		if (STRICTLY_POSITIVE_EXECUTION_RANGE_KEYS.has(draft.key) && min <= 0) {
			return `${draft.key} minimum must be greater than zero.`;
		}
		if (draft.key === 'leverage' && max > 125) return 'Leverage maximum cannot exceed 125.';
		if (draft.key === 'risk_per_trade' && max > 1) return 'Risk per trade maximum cannot exceed 1.';
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

	function setAllOptimizationParamsSelected(selected: boolean) {
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		for (const [key, draft] of Object.entries(optimizationParamDrafts)) {
			nextDrafts[key] = {
				...draft,
				selected,
				error: selected ? validateOptimizationParamDraft(draft) : '',
			};
		}
		optimizationParamDrafts = nextDrafts;
	}

	function setOptimizationExecutionSelected(key: string, selected: boolean): void {
		const draft = optimizationExecutionDrafts[key];
		if (!draft) return;
		optimizationExecutionDrafts = {
			...optimizationExecutionDrafts,
			[key]: {
				...draft,
				selected,
				error: selected ? validateOptimizationExecutionDraft(draft) : '',
			},
		};
	}

	function setAllOptimizationExecutionSelected(selected: boolean): void {
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		for (const [key, draft] of Object.entries(optimizationExecutionDrafts)) {
			nextDrafts[key] = {
				...draft,
				selected,
				error: selected ? validateOptimizationExecutionDraft(draft) : '',
			};
		}
		optimizationExecutionDrafts = nextDrafts;
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

	function updateOptimizationExecutionField(key: string, field: 'min' | 'max' | 'step', value: string): void {
		const draft = optimizationExecutionDrafts[key];
		if (!draft) return;
		const nextDraft = {
			...draft,
			[field]: value,
		};
		optimizationExecutionDrafts = {
			...optimizationExecutionDrafts,
			[key]: {
				...nextDraft,
				error: nextDraft.selected ? validateOptimizationExecutionDraft(nextDraft) : '',
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

	function collectOptimizationExecutionRanges(): {
		error: string | null;
		executionRanges?: Record<string, { min: number; max: number; step: number }>;
	} {
		const nextDrafts: Record<string, OptimizationParamDraft> = {};
		const executionRanges: Record<string, { min: number; max: number; step: number }> = {};
		let firstError: string | null = null;
		for (const [key, draft] of Object.entries(optimizationExecutionDrafts)) {
			let error = '';
			if (draft.selected) {
				error = validateOptimizationExecutionDraft(draft);
				if (!error) {
					executionRanges[key] = {
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
		optimizationExecutionDrafts = nextDrafts;
		return {
			error: firstError,
			executionRanges: Object.keys(executionRanges).length > 0 ? executionRanges : undefined,
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

	function openStageChange(targetStage?: string): void {
		if (!stageControl) return;
		if (targetStage) {
			stageControl.openFor(targetStage);
		} else if (nextPipelineStage) {
			stageControl.openFor(nextPipelineStage.key);
		}
	}

	async function handleStageChanged(): Promise<void> {
		await loadContainer();
	}

	async function loadOverviewResult(item: StrategyContainerHistoryItem): Promise<void> {
		const resultId = String(item.result_id || '').trim();
		if (!resultId || overviewResultId === resultId) return;
		overviewResultId = resultId;
		overviewResult = null;
		overviewResultError = '';
		overviewResultLoading = true;
		try {
			const result = await getResult(resultId);
			if (destroyed || overviewResultId !== resultId) return;
			overviewResult = result ?? null;
		} catch (err) {
			if (destroyed || overviewResultId !== resultId) return;
			overviewResultError = err instanceof Error ? err.message : 'Failed to load run details';
		} finally {
			if (overviewResultId === resultId) {
				overviewResultLoading = false;
			}
		}
	}

	async function openResult(item: StrategyContainerHistoryItem) {
		const resultId = String(item.result_id || '').trim();
		if (!resultId) return;
		selectedResultId = resultId;
		selectedResultItem = item;
		loadGauntletDraftFromHistory(item);
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

	function getOptimizationHistoryBestParams(item: StrategyContainerHistoryItem): Record<string, unknown> {
		const metricsParams = asPlainRecord(item.metrics?.best_params);
		if (Object.keys(metricsParams).length > 0) return metricsParams;
		const configBest = asPlainRecord(item.config?.best_params);
		if (Object.keys(configBest).length > 0) return configBest;
		return extractParamRecord(item.config?.params);
	}

	function getOptimizationHistoryExecutionProfile(item: StrategyContainerHistoryItem): Record<string, unknown> {
		const profile = asPlainRecord(item.config?.execution_profile);
		if (Object.keys(profile).length > 0) return profile;
		const bestProfile = asPlainRecord(item.config?.best_execution_profile);
		if (Object.keys(bestProfile).length > 0) return bestProfile;
		return asPlainRecord(item.config?.best_execution_controls);
	}

	function optimizationObjectiveLabel(value: unknown): string {
		const normalized = String(value ?? '').trim();
		const match = OPTIMIZATION_OBJECTIVES.find((option) => option.value === normalized);
		return match?.label ?? (normalized || 'Fitness');
	}

	function optimizationObjectiveValue(item: StrategyContainerHistoryItem): number | null {
		const objective = String(item.config?.objective || item.metrics?.objective || '').trim();
		const explicit = readMetricOptional(item, 'best_objective_value', 'objective_value');
		if (explicit !== null) return explicit;
		if (objective === 'total_return_pct') return readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct');
		if (objective === 'profit_factor') return readMetricOptional(item, 'profit_factor', 'pf');
		if (objective === 'win_rate') return readPercentMetricOptional(item, 'win_rate', 'win_rate_pct');
		if (objective === 'sharpe_ratio' || objective === 'sharpe') return readMetricOptional(item, 'sharpe_ratio', 'sharpe');
		return readMetricOptional(item, 'best_fitness', 'fitness');
	}

	function formatOptimizationObjectiveValue(item: StrategyContainerHistoryItem): string {
		const value = optimizationObjectiveValue(item);
		if (value === null || !Number.isFinite(value)) return '--';
		const objective = String(item.config?.objective || item.metrics?.objective || '').trim();
		if (objective === 'total_return_pct' || objective === 'win_rate') return `${value.toFixed(2)}%`;
		return value.toFixed(2);
	}

	function formatOptimizationChipRecord(record: Record<string, unknown>, limit = 6): string[] {
		return Object.entries(record)
			.sort(([left], [right]) => left.localeCompare(right))
			.slice(0, limit)
			.map(([key, value]) => `${key}=${formatBacktestParamChipValue(value)}`);
	}

	function optimizationTopResults(item: StrategyContainerHistoryItem): Array<Record<string, unknown>> {
		const top = item.config?.top_results;
		return Array.isArray(top) ? top.filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === 'object').slice(0, 3) : [];
	}

	function optimizationRunMarketLabel(item: StrategyContainerHistoryItem): string {
		const config = asPlainRecord(item.config);
		const symbol = String(config.symbol ?? item.symbol ?? '').trim() || 'Symbol';
		const timeframe = String(config.timeframe ?? item.timeframe ?? '').trim() || 'TF';
		return `${symbol} / ${timeframe}`;
	}

	function optimizationRunWindowLabel(item: StrategyContainerHistoryItem): string {
		const config = asPlainRecord(item.config);
		const start = String(config.start ?? item.start_date ?? '').trim();
		const end = String(config.end ?? item.end_date ?? '').trim();
		return `${fmtShortDate(start)} -> ${fmtShortDate(end)}`;
	}

	function optimizationWfaLabel(item: StrategyContainerHistoryItem): string {
		const config = asPlainRecord(item.config);
		const metrics = asPlainRecord(item.metrics);
		const verdict = String(config.wfa_verdict ?? metrics.wfa_verdict ?? '').trim();
		if (verdict) return verdict.toUpperCase();
		const validated = config.validated ?? metrics.validated;
		if (validated === true) return 'VALIDATED';
		if (validated === false) return 'NOT VALIDATED';
		return '--';
	}

	function optimizationWfaClass(item: StrategyContainerHistoryItem): string {
		const label = optimizationWfaLabel(item).toLowerCase();
		if (label.includes('fail') || label.includes('not')) return 'text-red-300';
		if (label.includes('pass') || label.includes('valid')) return 'text-emerald-300';
		return 'text-[#aaa]';
	}

	function formatOptimizationObjectiveByName(objective: string, value: number | null): string {
		if (value === null || !Number.isFinite(value)) return '--';
		if (objective === 'total_return_pct' || objective === 'win_rate') return `${value.toFixed(2)}%`;
		return value.toFixed(2);
	}

	function optimizationTopObjectiveLabel(entry: Record<string, unknown>, item: StrategyContainerHistoryItem): string {
		const objective = String(entry.objective ?? item.config?.objective ?? item.metrics?.objective ?? '').trim();
		const value = asNumber(entry.objective_value, Number.NaN);
		const fallback = asNumber(entry.fitness, Number.NaN);
		const resolved = Number.isFinite(value) ? value : Number.isFinite(fallback) ? fallback : null;
		return formatOptimizationObjectiveByName(objective, resolved);
	}

	function optimizationTopFitnessLabel(entry: Record<string, unknown>): string {
		const fitness = asNumber(entry.fitness, Number.NaN);
		return Number.isFinite(fitness) ? fitness.toFixed(2) : '--';
	}

	function optimizationTopParamChips(entry: Record<string, unknown>, limit = 4): string[] {
		const params = asPlainRecord(entry.full_params);
		const fallback = asPlainRecord(entry.params);
		return formatOptimizationChipRecord(Object.keys(params).length > 0 ? params : fallback, limit);
	}

	function optimizationTopExecutionChips(entry: Record<string, unknown>, limit = 4): string[] {
		const controls = asPlainRecord(entry.full_execution_controls);
		const fallback = asPlainRecord(entry.execution_controls);
		return formatOptimizationChipRecord(Object.keys(controls).length > 0 ? controls : fallback, limit);
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

	function getEffectiveOptimizationExecutionDraft(): ExecutionProfileDraft | null {
		if (!selectedResult) return null;
		const config = asPlainRecord(selectedResult.config);
		const executionProfile = asPlainRecord(config.execution_profile);
		const bestExecutionProfile = asPlainRecord(config.best_execution_profile);
		const bestExecutionControls = asPlainRecord(config.best_execution_controls);
		const source = Object.keys(executionProfile).length > 0
			? executionProfile
			: Object.keys(bestExecutionProfile).length > 0
				? bestExecutionProfile
				: bestExecutionControls;
		if (Object.keys(source).length === 0) return null;
		return buildExecutionDraftFromRecord(source, containerExecutionDefaults);
	}

	function isOptimizationResult(): boolean {
		return selectedResult?.result_type === 'optimization' && getOptBestParams() !== null;
	}

	async function backtestWithOptParams() {
		if (!container || !selectedResult || backtestingOptParams) return;
		const effectiveParams = getEffectiveOptimizationParams();
		if (!effectiveParams) return;
		const optimizedExecutionDraft = getEffectiveOptimizationExecutionDraft();
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
				...executionDraftToPayload(optimizedExecutionDraft ?? executionDraft),
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

	function formatPositionPrice(value: number | null | undefined): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '—';
		return Number(value).toLocaleString(undefined, { maximumFractionDigits: 8 });
	}

	// Defensive: a failed pre-edit check must never block saving — log and proceed.
	async function fetchOpenPositionSummary(strategyId: string): Promise<StrategyOpenPosition | null> {
		try {
			return await getStrategyOpenPosition(strategyId);
		} catch (err) {
			console.warn('Open-position pre-save check failed; proceeding with save.', err);
			return null;
		}
	}

	// Build the warning shown before saving execution settings onto a live/paper
	// trade. Returns null when there is no open position to warn about.
	function describeOpenPositionWarning(summary: StrategyOpenPosition | null): string | null {
		if (!summary?.has_open_position || !summary.positions?.length) return null;
		const pos = summary.positions[0];
		const entry = formatPositionPrice(pos.entry_price);
		const extra = summary.count > 1 ? ` (plus ${summary.count - 1} other open position${summary.count - 1 === 1 ? '' : 's'})` : '';
		return `⚠️ This strategy has an open ${pos.direction.toUpperCase()} ${pos.asset} position (entry ${entry})${extra}. Saving these execution settings will UPDATE its stop-loss / take-profit on the live position. Apply anyway?`;
	}

	// Surface what the backend recomputed on the open position after a save.
	function notifyOpenPositionUpdate(update: OpenPositionUpdate | null | undefined): void {
		if (!update?.affected) return;
		for (const p of update.positions ?? []) {
			// The backend records a per-trade apply FAILURE as { trade_id, error } (no
			// direction/asset) and never raises — the param save itself still succeeded.
			// Surface that as its own error toast instead of dereferencing p.direction on the
			// error variant, which would throw and turn a successful save into a false
			// "failed" toast (and skip the post-save container refresh).
			if (!p.direction) {
				addToast(
					`Open position not updated${p.asset ? ` (${p.asset})` : ''}: ${p.error ?? 'apply failed'}`,
					'error',
					`/lab/strategy/${encodeURIComponent(strategyId)}`,
				);
				continue;
			}
			addToast(
				`Updated open ${p.direction.toUpperCase()} ${p.asset}: SL ${formatPositionPrice(p.stop_loss?.old)} → ${formatPositionPrice(p.stop_loss?.new)}, TP ${formatPositionPrice(p.take_profit?.old)} → ${formatPositionPrice(p.take_profit?.new)}`,
				'info',
				`/lab/strategy/${encodeURIComponent(strategyId)}`,
			);
		}
	}

	async function setAsDefaultParams() {
		if (!container || settingDefaultParams) return;
		const effectiveParams = getEffectiveOptimizationParams();
		if (!effectiveParams) return;
		const optimizedExecutionDraft = getEffectiveOptimizationExecutionDraft();
		const paramsToSave = optimizedExecutionDraft
			? { ...effectiveParams, execution_profile: executionDraftToPayload(optimizedExecutionDraft) }
			: effectiveParams;
		const openPositionWarning = describeOpenPositionWarning(
			await fetchOpenPositionSummary(container.strategy.id),
		);
		const confirmMessage = openPositionWarning
			? `Set these optimized parameters as the strategy defaults?\n\nThis will update the parameters used for paper trading and live execution.\n\n${openPositionWarning}`
			: 'Set these optimized parameters as the strategy defaults?\n\nThis will update the parameters used for paper trading and live execution.';
		const confirmed = typeof window === 'undefined' || window.confirm(confirmMessage);
		if (!confirmed) return;
		settingDefaultParams = true;
		try {
			const res = await updateStrategyDefaultParams(container.strategy.id, paramsToSave, { pinnedBacktestId: null });
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			notifyOpenPositionUpdate(res?.open_position_update);
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
		const openPositionWarning = describeOpenPositionWarning(
			await fetchOpenPositionSummary(container.strategy.id),
		);
		const confirmMessage = openPositionWarning
			? `Set this backtest as the strategy default?\n\nIts parameters will drive paper trading and live execution, and its metrics will display on the Lab manager.\n\n${openPositionWarning}`
			: 'Set this backtest as the strategy default?\n\nIts parameters will drive paper trading and live execution, and its metrics will display on the Lab manager.';
		const confirmed = typeof window === 'undefined' || window.confirm(confirmMessage);
		if (!confirmed) return;
		settingDefaultParams = true;
		try {
			const res = await updateStrategyDefaultParams(container.strategy.id, params, {
				pinnedBacktestId: item.result_id
			});
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			notifyOpenPositionUpdate(res?.open_position_update);
			await loadContainer();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Failed to update params', 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		} finally {
			settingDefaultParams = false;
		}
	}

	function resetParameterDraft(): void {
		paramsDraft = cloneParameterRecord(strategyParams);
		executionDraft = { ...containerExecutionDefaults };
		// A row click can move the backtest symbol/timeframe/window away from the container
		// default; a "Reset to Defaults" that left them pointed at the last-clicked run
		// would be a half-reset. Restore the captured default context alongside the draft.
		backtestForm = { ...backtestForm, ...containerBacktestDefaults };
		gauntletDraftSource = 'defaults';
		gauntletDraftSourceId = '';
		parameterSaveMessage = '';
		parameterSaveError = '';
	}

	function resetGauntletDraftToDefaults(): void {
		resetParameterDraft();
	}

	function loadGauntletDraftFromHistory(item: StrategyContainerHistoryItem, notify = false): void {
		paramsDraft = getBacktestParamDraft(item);
		executionDraft = getHistoryExecutionDraft(item);
		// Mirror the selected run's full backtest context into the form — symbol,
		// timeframe AND the date span — so the loaded draft reflects the SAME inputs it
		// was produced over and re-running ("Run the Gauntlet") reproduces the run instead
		// of pairing a stale symbol/timeframe with a new window. Symbol/timeframe fall back
		// to the current form when the run omits them; the date span is applied only when
		// BOTH endpoints parse (atomic) so we never blend a new start with a stale end.
		const windowStart = toDateInput(item.start_date ?? item.config.start);
		const windowEnd = toDateInput(item.end_date ?? item.config.end);
		const runSymbol = (item.symbol ?? '').trim();
		const runTimeframe = (item.timeframe ?? '').trim();
		backtestForm = {
			...backtestForm,
			symbol: runSymbol || backtestForm.symbol,
			timeframe: runTimeframe || backtestForm.timeframe,
			...(windowStart && windowEnd ? { start_date: windowStart, end_date: windowEnd } : {}),
		};
		gauntletDraftSource = 'run';
		gauntletDraftSourceId = String(item.result_id || '').trim();
		parameterSaveMessage = '';
		parameterSaveError = '';
		if (notify && gauntletDraftSourceId) {
			addToast(`Loaded Gauntlet settings from ${gauntletDraftSourceId}.`, 'info', `/lab/strategy/${encodeURIComponent(strategyId)}`);
		}
	}

	function loadBacktestParamsIntoDraft(item: StrategyContainerHistoryItem): void {
		loadGauntletDraftFromHistory(item, true);
	}

	async function saveParameterDraft(): Promise<void> {
		if (!container || settingDefaultParams || !gauntletDraftDirty) return;
		if (paramsHasErrors || executionDraftError) {
			addToast('Fix the highlighted parameter errors before saving.', 'error');
			return;
		}
		// This pane has no confirm by default — only prompt when the save would
		// mutate an open paper/live position.
		const openPositionWarning = describeOpenPositionWarning(
			await fetchOpenPositionSummary(container.strategy.id),
		);
		if (openPositionWarning && typeof window !== 'undefined' && !window.confirm(openPositionWarning)) return;
		settingDefaultParams = true;
		parameterSaveMessage = '';
		parameterSaveError = '';
		try {
			// Persist the execution profile under params.execution_profile — the
			// canonical home the optimizer / gauntlet / acceptance gate read — alongside
			// the alpha params, so a tuned profile survives reload and drives backtests
			// instead of being ephemeral.
			const paramsToSave = { ...paramsDraft, execution_profile: executionDraftToPayload(executionDraft) };
			const res = await updateStrategyDefaultParams(container.strategy.id, paramsToSave, { pinnedBacktestId: null });
			parameterSaveMessage = 'Default parameters saved.';
			addToast('Default parameters updated', 'success', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			notifyOpenPositionUpdate(res?.open_position_update);
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
		// Force the Overview snapshot to refetch — the active run (or its stored
		// metrics) may have changed as part of whatever triggered this reload.
		overviewResult = null;
		overviewResultId = '';
		overviewResultError = '';
		overviewResultLoading = false;
		// Comparison selections belong to the previous container state.
		compareSelection = [];
		compareResults = {};
		compareError = '';
		// Refetch the full growth series alongside the container.
		growthTrades = null;
		growthTradesLoadedFor = '';
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
			containerBacktestDefaults = { ...backtestForm };
			optimizationForm = {
				...optimizationForm,
				symbol: defaultSymbol,
				timeframe: defaultTimeframe,
				start_date: resolvedStartDate,
				end_date: resolvedEndDate,
			};
			paramsDraft = cloneParameterRecord(stripExecutionProfile(payload.configuration.params));
			executionDraft = buildContainerExecutionDefaults();
			gauntletDraftSource = 'defaults';
			gauntletDraftSourceId = '';
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
			...executionDraftToPayload(executionDraft),
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
				...executionDraftToPayload(gauntletDraftSourceId === resultId ? executionDraft : getHistoryExecutionDraft(item)),
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
		const { error: executionRangeError, executionRanges } = collectOptimizationExecutionRanges();
		if (executionRangeError) {
			submitStatus = 'failed';
			submitMessage = executionRangeError;
			resetSubmitProgress();
			addToast(executionRangeError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		const executionValidationError = validateExecutionDraft(executionDraft);
		if (executionValidationError) {
			submitStatus = 'failed';
			submitMessage = executionValidationError;
			resetSubmitProgress();
			addToast(executionValidationError, 'error', `/lab/strategy/${encodeURIComponent(strategyId)}`);
			return;
		}
		submitStatus = 'submitting';
		submitMessage = '';
		submitJobId = null;
		resetSubmitProgress();
		try {
			const executionPayload = executionDraftToPayload(executionDraft);
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
				execution_parameter_ranges: executionRanges,
				execution_profile: executionPayload,
				...executionPayload,
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
	<title>{container?.strategy.display_name || container?.strategy.name || strategyId} · Lab</title>
</svelte:head>

<div class="h-full flex flex-col overflow-hidden">
	<div class="flex items-center gap-3 border-b border-[#222] bg-[#0b0b0b] px-4 py-2">
		<button
			type="button"
			class="text-xs text-[#555] transition-colors hover:text-white"
			on:click={goBack}
		>
			Back
		</button>
		<span class="text-gray-700">|</span>
		{#if container}
			{#if editingName}
				<input
					use:focusAndSelect
					class="border border-[#333] bg-black px-2 py-0.5 font-mono text-[11px] text-white focus:border-white focus:outline-none disabled:opacity-50"
					style="min-width: 220px"
					maxlength="140"
					bind:value={nameDraft}
					placeholder={container.strategy.name}
					disabled={savingName}
					on:keydown={onNameKeydown}
					aria-label="Display name"
				/>
				<button
					type="button"
					class="border border-emerald-700/60 bg-emerald-950/30 px-2 py-0.5 text-[11px] text-emerald-200 transition hover:bg-emerald-900/40 disabled:opacity-50"
					title="Save display name (Enter)"
					disabled={savingName}
					on:click={saveName}
				>
					{savingName ? '…' : 'Save'}
				</button>
				<button
					type="button"
					class="border border-[#2b2b2b] px-2 py-0.5 text-[11px] text-[#888] transition hover:text-white disabled:opacity-50"
					title="Cancel (Esc)"
					disabled={savingName}
					on:click={cancelEditName}
				>
					Cancel
				</button>
			{:else}
				<StrategyLink strategyId={container.strategy.id} label={container.strategy.display_name || container.strategy.name} returnTo={returnTo} />
				<button
					type="button"
					data-testid="edit-display-name-button"
					class="text-[#555] transition-colors hover:text-white"
					title="Edit display name"
					aria-label="Edit display name"
					on:click={startEditName}
				>
					<svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
						<path d="M12 20h9" />
						<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
					</svg>
				</button>
				{#if container.strategy.display_name}
					<span class="font-mono text-[10px] text-[#555]" title="Canonical name">({container.strategy.name})</span>
				{/if}
			{/if}
			{#if container.strategy.hypothesis_id}
				{@const hypothesisHrefId = container.strategy.hypothesis_display_id || container.strategy.hypothesis_id}
				{@const hypothesisLabelId = container.strategy.hypothesis_display_id || container.strategy.hypothesis_id}
				<span class="text-gray-700">|</span>
				<a href={`/hypotheses/${encodeURIComponent(hypothesisHrefId)}`} class="text-[11px] uppercase tracking-widest text-[#888] transition-colors hover:text-white">
					Crucible {hypothesisLabelId}
				</a>
			{/if}
			<span class="text-gray-700">•</span>
			<StageControl
				bind:this={stageControl}
				strategyId={container.strategy.id}
				currentStage={currentLifecycleStage}
				pipelineStages={PIPELINE_STAGES}
				on:changed={() => void handleStageChanged()}
			/>
			<button
				type="button"
				data-testid="active-driver-chip"
				class={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] transition ${
					pinnedBacktestId
						? 'border-emerald-600/60 bg-emerald-950/30 text-emerald-200 hover:bg-emerald-900/40'
						: 'border-[#2b2b2b] bg-black text-[#888] hover:text-gray-200'
				}`}
				title={pinnedBacktestId
					? `Gauntlet run ${pinnedBacktestId} is pinned — its params and metrics drive paper/live execution. Click to view.`
					: 'No run is pinned — the manually-saved container defaults drive paper/live execution. Click to view the Gauntlet history.'}
				on:click={() => (activeTab = 'backtests')}
			>
				<span class={`h-1.5 w-1.5 rounded-full ${pinnedBacktestId ? 'bg-emerald-400' : 'bg-gray-500'}`}></span>
				<span class="uppercase tracking-[0.12em]">Driver</span>
				<span class="font-mono normal-case tracking-normal">{pinnedBacktestId || 'Container defaults'}</span>
			</button>
			{#if container.strategy.canonical}
				<span
					data-canonical-badge
					class="ml-1 border border-emerald-500/60 bg-emerald-950/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-emerald-100"
					title="Canonical: best-in-cell for this hypothesis; protected from cleanup"
				>
					Canonical
				</span>
			{/if}
			{#if container.strategy.parent_strategy_id}
				<span class="text-gray-700">•</span>
				<a
					href={`/lab/strategy/${encodeURIComponent(container.strategy.parent_strategy_id)}`}
					class="text-[10px] uppercase tracking-widest text-[#888] transition-colors hover:text-white"
					title={`Iterated from ${container.strategy.parent_strategy_id}`}
				>
					⮐ Parent {container.strategy.parent_strategy_id}
				</a>
			{/if}
			<span class="ml-auto"></span>
			<StrategyExportMenu strategyId={container.strategy.id} displayId={container.strategy.display_id || container.strategy.id} name={container.strategy.name} />
			<button
				type="button"
				data-testid="import-strategy-button"
				class="border border-[#2b2b2b] bg-black px-3 py-1 text-[10px] font-semibold uppercase tracking-widest text-[#aaa] transition hover:text-white"
				title="Import a strategy export as a new quick_screen container"
				on:click={() => (showImportDialog = true)}
			>
				⤒ Import
			</button>
			<button
				type="button"
				data-testid="export-tradingview-button"
				class="terminal-button px-3 py-1 text-[10px] tracking-widest"
				title="Show a Pine v6 strategy for TradingView verification"
				on:click={exportToTradingView}
			>
				Export to TradingView
			</button>
		{:else}
			<span class="text-xs text-[#888] font-mono">{strategyId}</span>
		{/if}
		{#if submitJobId}
			<span class="ml-auto text-[10px] text-[#555] font-mono">job: {submitJobId}</span>
		{/if}
	</div>

	{#if loading}
		<div class="flex-1 flex items-center justify-center">
			<div class="text-xs uppercase tracking-widest text-[#555]">Loading container...</div>
		</div>
	{:else if error}
		<div class="flex-1 flex items-center justify-center">
			<div class="border border-red-900 bg-red-950/20 px-4 py-3 text-sm text-red-300">{error}</div>
		</div>
	{:else if container}
		{#if container.strategy.id}
			<div class="border-b border-[#222] bg-[#070707] px-4 py-2">
				<BrainStrategyDecisionsCard strategyId={container.strategy.id} />
			</div>
		{/if}
		<div class="border-b border-[#222] bg-[#0a0a0a] px-4">
			<div role="group" aria-label="Strategy detail sections" class="flex gap-6 text-xs uppercase tracking-wide">
				{#each [
					{ key: 'overview', label: 'Overview' },
					{ key: 'backtests', label: 'Gauntlet' },
					{ key: 'optimizations', label: 'Optimization' },
					{ key: 'robustness', label: 'Robustness' },
					{ key: 'execution', label: 'Execution' },
				] as tab (tab.key)}
					<button
						type="button"
						data-testid={`strategy-tab-${tab.key}`}
						aria-pressed={activeTab === tab.key}
						class="border-b-2 py-2 transition-colors {activeTab === tab.key ? 'border-white text-white' : 'border-transparent text-[#555] hover:text-[#aaa]'}"
						on:click={() => (activeTab = tab.key as TabKey)}
					>
						{tab.label}
					</button>
				{/each}
			</div>
		</div>

		<div class="flex-1 overflow-auto bg-black p-4">
			{#if submitStatus !== 'idle'}
				<div class="mb-4 border px-3 py-2 text-xs {submitStatus === 'failed' ? 'border-red-900 bg-red-950/20 text-red-300' : submitStatus === 'completed' ? 'border-emerald-900 bg-emerald-950/20 text-emerald-300' : 'border-[#333] bg-[#050505] text-[#888]'}">
					<div>{submitMessage}</div>
					{#if submitStatus === 'running' || submitStatus === 'submitting'}
						<div class="mt-2 border-t border-current/20 pt-2 text-[11px]">
							<div class="flex items-center justify-between gap-2">
								<span class="font-mono uppercase tracking-wide">status: {submitPollingStatus || submitStatus}</span>
								<span class="font-mono text-[10px] text-[#888]">poll #{submitPollCount}</span>
							</div>
							{#if submitProgress}
								<div class="mt-1 text-[#aaa]">{submitProgress}</div>
							{/if}
							{#if submitProgressPct !== null}
								<div class="mt-2 h-1.5 w-full bg-[#111]">
									<div
										class="h-1.5 bg-white transition-all"
										style={`width: ${submitProgressPct}%`}
									></div>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			{#if activeTab === 'overview'}
				<!-- Identity strip — carries the strategy facts the removed Configuration tab used to show. -->
				<div class="mb-3 flex flex-wrap items-center gap-1.5 text-[11px]" data-testid="overview-identity-strip">
					<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-[#aaa]" title="Strategy type">{String(container.configuration.type ?? '-')}</span>
					<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-[#aaa]" title="Owner">{String(container.configuration.owner ?? '-')}</span>
					<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-white" title="Configured market">{String(container.configuration.symbol ?? '-')}</span>
					<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-white" title="Configured timeframe">{String(container.configuration.timeframe ?? '-')}</span>
					<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-[#888]" title="Created">{fmtDate(container.strategy.created_at)}</span>
					<span class="ml-auto"></span>
					<button
						type="button"
						data-testid="deepdive-toggle-overview"
						class="border border-[#333] bg-[#0c0c0c] px-3 py-1 text-[10px] font-semibold uppercase tracking-widest text-white transition hover:bg-[#111]"
						on:click={launchDeepdive}
					>
						🔍 Deepdive
					</button>
				</div>
				<div class="grid grid-cols-1 gap-3 xl:grid-cols-[0.95fr_1.05fr]">
					<div class="space-y-3">
						<div class="border border-[#1d1d1d] bg-[#090909] p-3">
							<div class="flex items-center justify-between gap-2">
								<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Pipeline</div>
								<div class="flex items-center gap-2 text-[11px]">
									<span class="text-[#555]">{latestLifecycleEvent ? fmtDate(latestLifecycleEvent.created_at) : '--'}</span>
									<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-[#888]">{orderedRecentEvents.length} event{orderedRecentEvents.length === 1 ? '' : 's'}</span>
								</div>
							</div>
							{#if latestLifecycleEvent}
								<div class="mt-2 text-xs text-[#888]">{summarizeLifecycleEvent(latestLifecycleEvent)}</div>
							{/if}
							{#if currentLifecycleStage in TERMINAL_STAGES}
								<div class="mt-2 border border-red-900/40 bg-red-950/15 px-2 py-1.5 text-xs text-red-200">
									<span class="font-medium">{lifecycleStageLabel(currentLifecycleStage)}.</span>
									<span class="ml-1 text-red-300/80">{TERMINAL_STAGES[currentLifecycleStage]}</span>
								</div>
								<div class="mt-2">
									<button
										type="button"
										data-testid="overview-revive-button"
										on:click={() => openStageChange('quick_screen')}
										class="border border-emerald-700/50 bg-emerald-950/30 px-3 py-1.5 text-xs text-emerald-200 transition-colors hover:bg-emerald-900/40"
									>Revive to Quick Screen</button>
								</div>
							{:else}
								<div class="mt-2 flex gap-1.5">
									{#each currentStageDescriptors as stage}
										<button
											on:click={() => { selectedReadinessStage = stage.key === currentLifecycleStage ? null : stage.key; }}
											title={stage.tooltip ?? ''}
											class={`flex-1 border px-2 py-1.5 text-center cursor-pointer transition-colors ${
												readinessViewStage === stage.key
													? 'border-[#333] bg-[#0c0c0c] text-white '
													: stage.kind === 'current'
														? 'border-[#333] bg-[#0c0c0c] text-white'
														: stage.kind === 'past'
															? 'border-emerald-900/40 bg-emerald-950/10 text-emerald-200'
															: 'border-[#1f1f1f] bg-[#070707] text-[#555] hover:border-gray-700 hover:text-[#888]'
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
											if (action === 'run_optimization' || action === 'apply_best_params') activeTab = 'optimizations';
											else if (action === 'run_confirmation_backtest') activeTab = 'backtests';
											else if (action === 'run_validation_suite' || action === 're_run_validation_suite') activeTab = 'robustness';
										}}
									/>
								</div>

								<div class="mt-2 flex flex-wrap items-center gap-2">
									{#if nextPipelineStage}
										<button
											data-testid="overview-promote-button"
											on:click={() => openStageChange(nextPipelineStage?.key)}
											class="border border-[#333] bg-[#0c0c0c] px-3 py-1.5 text-xs text-white hover:bg-[#111] transition-colors"
										>Promote to {nextPipelineStage.label}</button>
									{/if}
									<button on:click={() => goto(`/bot-factory/editor?strategy=${strategyId}`)} class="border border-[#333] bg-[#0c0c0c] px-3 py-1.5 text-xs text-white hover:bg-[#111] transition-colors">Deploy as Bot</button>
									<button
										data-testid="overview-archive-button"
										on:click={() => openStageChange('archived')}
										class="border border-red-900/40 bg-red-950/20 px-3 py-1.5 text-xs text-red-300 hover:bg-red-900/30 transition-colors"
									>Archive</button>
								</div>
							{/if}
						</div>

						{#if liveRampInfo}
							<div class="border border-emerald-900/40 bg-[#090909] p-3" data-testid="overview-live-ramp">
								<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Live Status</div>
								<div class="mt-2 grid grid-cols-3 gap-2 text-xs">
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Days Live</div>
										<div class="mt-1 font-mono text-sm text-emerald-300">{liveRampInfo.daysLive}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Week</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{liveRampInfo.week}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="From the configured allocation schedule. Advisory only — the live sizer does not currently enforce this ramp.">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Ramp Alloc</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{liveRampInfo.allocationPct !== null ? `${liveRampInfo.allocationPct}%` : '—'}</div>
									</div>
								</div>
								<div class="mt-2 text-[11px] text-[#555]">
									{#if liveRampInfo.killSwitchPct !== null}
										Decay kill switch at <span class="text-red-300">{liveRampInfo.killSwitchPct}%</span> drawdown.
									{/if}
									The allocation ramp is the configured schedule — advisory, not enforced by the sizer.
								</div>
							</div>
						{/if}

						<GauntletStatusCard
							{strategyId}
							stage={currentLifecycleStage}
							selectedTestKey={selectedRobustnessTest}
							testOverrides={robustnessStatusOverrides}
							on:selectTest={(event) => {
								selectedRobustnessTest = event.detail.key;
								activeTab = 'robustness';
							}}
							on:promote={() => openStageChange()}
						/>
					</div>

					<div class="space-y-3">
						<div
							class={` border p-3 ${activeRunItem && pinnedBacktestId && pinnedBacktestId === activeRunItem.result_id ? 'border-emerald-700/60 shadow-[inset_2px_0_0_0_rgba(16,185,129,0.9)] bg-[#090909]' : 'border-[#1d1d1d] bg-[#090909]'}`}
							data-testid="overview-active-run-card"
						>
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div class="flex items-center gap-2">
									<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Execution Driver</div>
									{#if activeRunItem}
										{#if pinnedBacktestId && pinnedBacktestId === activeRunItem.result_id}
											<span class="rounded-full border border-emerald-600/60 bg-emerald-950/30 px-2 py-0.5 text-[9px] uppercase tracking-wide text-emerald-200" title="This pinned run's params and metrics drive paper/live execution.">Pinned · Active</span>
										{:else}
											<span class="rounded-full border border-yellow-900 bg-yellow-500/5 px-2 py-0.5 text-[9px] uppercase tracking-wide text-yellow-400" title="No run is pinned — the manually-saved container defaults drive paper/live. Showing the latest Gauntlet run.">Latest run · defaults drive execution</span>
										{/if}
									{/if}
								</div>
								{#if activeRunItem}
									<button
										type="button"
										data-testid="overview-open-active-run"
										class="border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] uppercase tracking-wide text-[#888] transition hover:text-white"
										on:click={() => { activeTab = 'backtests'; if (activeRunItem) void openResult(activeRunItem); }}
									>Open Run</button>
								{/if}
							</div>
							{#if !activeRunItem}
								<div class="mt-3 border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-sm text-[#555]">
									No Gauntlet runs yet — run one from the Gauntlet tab to see how this strategy performs.
								</div>
							{:else}
								<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-[#555]">
									<span class="font-mono text-white">{activeRunItem.result_id}</span>
									<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-[#aaa]">{activeRunItem.symbol || '--'} / {activeRunItem.timeframe || '--'}</span>
									<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5">{fmtShortDate(activeRunItem.start_date)} → {fmtShortDate(activeRunItem.end_date)}</span>
									<span>{fmtDate(activeRunItem.created_at)}</span>
								</div>
								<div class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 xl:grid-cols-7" data-testid="overview-active-run-metrics">
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Out-of-sample CAGR (annualized)">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">OOS CAGR</div>
										<div class={`mt-1 font-mono text-sm ${isCagrReliable(activeRunItem) ? signedPercentClass(readOutOfSampleCagr(activeRunItem)) : 'text-[#555]'}`}>{formatOutOfSampleCagr(activeRunItem)}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Out-of-sample annualized Sharpe">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">OOS Sharpe</div>
										<div class={`mt-1 font-mono text-sm ${isSharpeReliable(activeRunItem) ? 'text-[#aaa]' : 'text-[#555]'}`}>{formatOutOfSampleSharpe(activeRunItem)}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Maximum peak-to-trough drawdown">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Max DD</div>
										<div class="mt-1 font-mono text-sm text-red-400">{pct(readDrawdownPercentMetric(activeRunItem, 'max_drawdown_pct', 'max_drawdown'))}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Combined win rate">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Win%</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{pct(readPercentMetric(activeRunItem, 'win_rate', 'win_rate_pct'))}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Total completed trades">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Trades</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{historyTradesCount(activeRunItem)}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Gross profit / gross loss">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">PF</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{formatProfitFactor(activeRunItem)}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title={`Gauntlet robustness score; below ${gauntletMinScore ?? 60} fails the promotion gate`}>
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Rob%</div>
										<div class="mt-1 font-mono text-sm text-[#aaa]">{formatRobustness(activeRunItem)}</div>
									</div>
								</div>
								{#if overviewResultLoading}
									<div class="mt-3 border border-[#333] bg-[#0c0c0c] px-3 py-4 text-xs text-white">Loading equity curve…</div>
								{:else if overviewResultError}
									<div class="mt-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400">{overviewResultError}</div>
								{:else if overviewHasEquity}
									<div class="mt-3" data-testid="overview-equity-curve">
										{#key overviewResultId}
											<EquityChart
												data={overviewEquityForChart}
												benchmarkData={overviewBenchmarkForChart}
												oosStartTimestamp={overviewOosStartTimestamp}
												showDrawdown={true}
												height={240}
											/>
										{/key}
									</div>
								{/if}
							{/if}
						</div>

						<div class="border border-[#1d1d1d] bg-[#090909] p-3" data-testid="overview-growth-card">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Strategy Growth</div>
								{#if executionGrowth}
									<div class="flex items-center gap-2 text-[11px]">
										<span class={` border px-1.5 py-0.5 font-mono ${executionGrowth.totalPnl >= 0 ? 'border-emerald-900/50 bg-emerald-950/15 text-emerald-300' : 'border-red-900/50 bg-red-950/15 text-red-300'}`}>
											{formatSignedCurrency(executionGrowth.totalPnl)}
										</span>
										<span class="text-[#555]">{executionGrowth.tradeCount} closed trade{executionGrowth.tradeCount === 1 ? '' : 's'}</span>
									</div>
								{/if}
							</div>
							{#if executionGrowth}
								<div class="mt-1 text-xs text-[#555]">
									{#if executionGrowth.mode === 'paper'}
										Paper book equity — the strategy's isolated ${PAPER_START_EQUITY.toLocaleString()} book plus realized PnL from each closed paper trade.
									{:else}
										Cumulative realized PnL from closed paper/live trades.
									{/if}
								</div>
								<div class="mt-3">
									<EquityChart
										data={executionGrowth.points}
										showDrawdown={executionGrowth.mode === 'paper'}
										annotations={growthAnnotations}
										height={220}
									/>
								</div>
							{:else}
								<div class="mt-3 border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-sm text-[#555]">
									No closed paper/live trades yet — real trading growth appears here once the strategy starts closing trades.
								</div>
							{/if}
						</div>

						<div class="border border-[#1d1d1d] bg-[#090909] p-3" data-testid="overview-parity-card">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Backtest ↔ Reality</div>
								{#if executionParity}
									<span class="text-[11px] text-[#555]">avg leverage {executionParity.avgLeverage.toFixed(1)}×</span>
								{/if}
							</div>
							{#if executionParity}
								<div class="mt-1 text-xs text-[#555]">
									Realized execution costs vs the execution profile's model. Slippage is signed — positive = filled worse than the signal price.
								</div>
								<div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3" data-testid="overview-parity-metrics">
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Average signed entry slippage vs the signal price across fills. Modeled budget: the execution profile's per-side slippage.">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Entry Slippage</div>
										<div class={`mt-1 font-mono text-sm ${parityTone(executionParity.entrySlipBps, modeledSlippageBps)}`}>{fmtBps(executionParity.entrySlipBps)}</div>
										<div class="mt-0.5 text-[10px] text-[#555]">modeled {modeledSlippageBps !== null ? `${modeledSlippageBps} bps` : '—'} · n={executionParity.entryCount}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Average signed exit slippage vs the signal price across closes.">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Exit Slippage</div>
										<div class={`mt-1 font-mono text-sm ${parityTone(executionParity.exitSlipBps, modeledSlippageBps)}`}>{fmtBps(executionParity.exitSlipBps)}</div>
										<div class="mt-0.5 text-[10px] text-[#555]">modeled {modeledSlippageBps !== null ? `${modeledSlippageBps} bps` : '—'} · n={executionParity.exitCount}</div>
									</div>
									<div class="border border-[#1f1f1f] bg-black px-2.5 py-2" title="Average realized round-trip cost drag per closed trade (gross − net PnL, includes leverage). Modeled: 2 × fee × avg leverage.">
										<div class="text-[9px] uppercase tracking-wide text-[#555]">Cost / Trade</div>
										<div class={`mt-1 font-mono text-sm ${parityTone(executionParity.costDragPct, modeledCostDragPct)}`}>{executionParity.costDragPct !== null ? `${executionParity.costDragPct.toFixed(3)}%` : '—'}</div>
										<div class="mt-0.5 text-[10px] text-[#555]">modeled {modeledCostDragPct !== null ? `${modeledCostDragPct.toFixed(3)}%` : '—'} · n={executionParity.costCount}</div>
									</div>
								</div>
							{:else}
								<div class="mt-3 border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-sm text-[#555]">
									No fills with recorded slippage/cost data yet — parity metrics appear once the strategy starts trading.
								</div>
							{/if}
						</div>

						<div class="border border-[#1d1d1d] bg-[#090909] p-3">
							<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Lifecycle Feed</div>
							{#if orderedRecentEvents.length === 0}
								<div class="mt-2 text-xs text-[#555]">No events recorded.</div>
							{:else}
								<div class="mt-2 max-h-[320px] overflow-auto space-y-1.5">
									{#each orderedRecentEvents as event}
										<div class="border border-[#1f1f1f] bg-[#070707] px-2.5 py-2">
											<div class="flex items-center justify-between gap-2 text-[11px]">
												<div class="flex items-center gap-1.5">
													<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono text-[#aaa]">{lifecycleStageLabel(event.from_state)}</span>
													<span class="text-[#555]">-></span>
													<span class="border border-[#333] bg-[#0c0c0c] px-1.5 py-0.5 font-mono text-white">{lifecycleStageLabel(event.to_state)}</span>
													<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5 text-[#555]">{lifecycleActorLabel(event.actor)}</span>
												</div>
												<span class="shrink-0 text-[#555]">{fmtDate(event.created_at)}</span>
											</div>
											{#if event.reason && event.reason.trim()}
												<div class="mt-1 text-xs text-[#888]">{event.reason.trim()}</div>
											{/if}
										</div>
									{/each}
								</div>
							{/if}
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
								class="border border-[#333] bg-[#0c0c0c] px-3 py-1 text-[10px] font-semibold uppercase tracking-widest text-white transition hover:bg-[#111]"
								on:click={launchDeepdive}
							>
								🔍 Deepdive
							</button>
						</div>
				<div class="mb-3 border border-[#1d1d1d] bg-[#090909] p-3">
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
								class="w-full border border-[#333] bg-[#0c0c0c] px-5 py-2 text-xs font-semibold uppercase tracking-wider text-white transition hover:bg-[#111] disabled:opacity-40 lg:w-auto"
								on:click={submitContainerBacktest}
								disabled={isAnyRunInFlight}
							>{submitStatus === 'submitting' || submitStatus === 'running' ? 'Running…' : 'Run the Gauntlet'}</button>
						</div>
					</div>
					<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-[#555]">
						<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5">{backtestBarEstimateLabel}</span>
						<span class="border border-[#2b2b2b] bg-black px-1.5 py-0.5">{backtestWindowSummary}</span>
						<span class={` border px-1.5 py-0.5 ${gauntletDraftDirty ? 'border-yellow-900 text-yellow-400' : 'border-[#2b2b2b] text-[#555]'}`}>{gauntletDraftDirty ? 'Draft has changes' : 'Defaults synced'}</span>
					</div>
				</div>

				<details class={`mb-3 border bg-[#090909] ${gauntletParamsAreActive ? 'border-emerald-700/60 shadow-[inset_2px_0_0_0_rgba(16,185,129,0.9)]' : 'border-[#1d1d1d]'}`} data-testid="backtest-parameter-panel">
					<summary class="flex cursor-pointer items-center justify-between px-3 py-2">
						<div class="flex items-center gap-2 text-[10px] uppercase tracking-wide text-[#555]">
							<span class={gauntletParamsAreActive ? 'text-emerald-300' : ''}>Gauntlet Parameters</span>
							<span class={` border px-1.5 py-0.5 text-[10px] normal-case tracking-normal ${gauntletDraftSource === 'run' ? 'border-[#333] text-white' : 'border-[#2b2b2b] text-[#555]'}`}>
								{gauntletDraftSource === 'run' && gauntletDraftSourceId ? `Run ${gauntletDraftSourceId}` : 'Container defaults'}
							</span>
							{#if gauntletParamsAreActive}
								<span class="rounded-full border border-emerald-600/60 bg-emerald-950/30 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-200" title="No backtest run is pinned — these manually-saved parameters are the active default driving paper/live.">Active</span>
							{:else}
								<span class={` border px-1.5 py-0.5 text-[10px] normal-case tracking-normal ${gauntletDraftDirty ? 'border-yellow-900 text-yellow-400' : 'border-[#2b2b2b] text-[#555]'}`}>{gauntletDraftDirty ? 'Unsaved' : 'Synced'}</span>
							{/if}
						</div>
						<div class="flex items-center gap-1.5">
							<button type="button" class="border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] uppercase text-[#888] hover:text-white disabled:opacity-40" data-testid="backtest-params-reset" on:click|stopPropagation={resetGauntletDraftToDefaults} disabled={(gauntletDraftSource === 'defaults' && !gauntletDraftDirty) || settingDefaultParams}>Reset to Defaults</button>
							<button type="button" class="border border-emerald-700 bg-emerald-950/30 px-2 py-0.5 text-[10px] uppercase text-emerald-200 hover:bg-emerald-900/40 disabled:opacity-40" data-testid="backtest-params-save" on:click|stopPropagation={saveParameterDraft} disabled={!gauntletDraftDirty || settingDefaultParams || paramsHasErrors || Boolean(executionDraftError)}>{settingDefaultParams ? 'Saving…' : 'Save'}</button>
						</div>
					</summary>
					<div class="border-t border-[#1a1a1a] p-3" data-testid="backtest-parameter-editor">
						{#if parameterSaveMessage}
							<div class="mb-2 border border-emerald-900/40 bg-emerald-950/10 px-2 py-1 text-[11px] text-emerald-200">{parameterSaveMessage}</div>
						{/if}
						{#if parameterSaveError}
							<div class="mb-2 border border-red-900/40 bg-red-950/20 px-2 py-1 text-[11px] text-red-300">{parameterSaveError}</div>
						{/if}
						<div class="mb-2 text-[10px] uppercase tracking-[0.2em] text-[#555]">Execution Settings</div>
						<ExecutionSettingsFields
							bind:draft={executionDraft}
							error={executionDraftError}
							disabled={settingDefaultParams}
							onSizingModeChange={() => applySizingModeDefaults(executionDraft.sizing_mode)}
						/>
						<div class="mt-3 border-t border-[#1a1a1a] pt-3">
							<div class="mb-2 text-[10px] uppercase tracking-[0.2em] text-[#555]">Strategy Parameters</div>
							{#if !loading}
								<div class="mb-2 border border-[#1f1f1f] bg-black p-2">
									<div class="flex flex-wrap items-end gap-2">
										<label class="flex min-w-[220px] flex-1 flex-col gap-1 text-[10px] uppercase tracking-wide text-[#555]">
											<span>Add Param</span>
											<select
												class="border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-gray-200 disabled:opacity-40"
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
											class="border border-[#333] bg-[#0c0c0c] px-3 py-1.5 text-[10px] uppercase tracking-wide text-white transition hover:bg-[#111] disabled:opacity-40"
											on:click={addSelectedParamToDraft}
											data-testid="add-param-button"
											disabled={settingDefaultParams || availableAddParamKeys.length === 0}
										>
											Add Param
										</button>
									</div>
									{#if addParamHelperText}
										<div class="mt-2 text-[11px] text-[#555]">{addParamHelperText}</div>
									{/if}
								</div>
							{/if}
							<ParameterEditor bind:params={paramsDraft} bind:hasErrors={paramsHasErrors} saving={settingDefaultParams} />
						</div>
						<details class="mt-2 border border-[#1f1f1f] bg-black">
							<summary class="cursor-pointer px-2 py-1.5 text-[10px] uppercase tracking-wide text-[#555]">Raw JSON</summary>
							<pre class="max-h-[200px] overflow-auto border-t border-[#1a1a1a] p-2 text-[11px] text-[#aaa]">{stableStringify(paramsDraft)}</pre>
						</details>
					</div>
				</details>

				{#if compareSelection.length === 1}
					<div class="mb-3 border border-[#333] bg-[#0c0c0c] px-3 py-2 text-[11px] text-white" data-testid="compare-hint">
						<span class="font-mono">{compareSelection[0]}</span> selected — tick a second run's Cmp box to compare.
						<button type="button" class="ml-2 border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] uppercase text-[#888] hover:text-white" on:click={clearCompareSelection}>Clear</button>
					</div>
				{/if}

				{#if comparePairReady && compareItemA && compareItemB}
					<div class="mb-3 border border-[#1d1d1d] bg-[#090909] p-3" data-testid="run-compare-panel">
						<div class="flex flex-wrap items-center gap-2">
							<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Run Comparison</div>
							<span class="border border-[#333] bg-[#0c0c0c] px-1.5 py-0.5 font-mono text-[11px] text-white">A · {compareItemA.result_id}</span>
							<span class="text-[#555]">vs</span>
							<span class="border border-yellow-900 bg-yellow-500/5 px-1.5 py-0.5 font-mono text-[11px] text-yellow-400">B · {compareItemB.result_id}</span>
							<span class="text-[11px] text-[#555]">Δ = B − A</span>
							<button
								type="button"
								data-testid="compare-clear"
								class="ml-auto border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] uppercase text-[#888] transition hover:text-white"
								on:click={clearCompareSelection}
							>Clear</button>
						</div>

						<div class="mt-3 grid gap-3 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
							<div>
								<table class="w-full text-xs" data-testid="compare-metrics-table">
									<thead class="bg-[#0d0d0d] text-[10px] uppercase tracking-widest text-[#555]">
										<tr>
											<th class="px-2 py-1.5 text-left">Metric</th>
											<th class="px-2 py-1.5 text-right text-white">A</th>
											<th class="px-2 py-1.5 text-right text-yellow-400">B</th>
											<th class="px-2 py-1.5 text-right">Δ</th>
										</tr>
									</thead>
									<tbody>
										{#each compareMetricRows as row (row.label)}
											<tr class="border-t border-[#161616] font-mono">
												<td class="px-2 py-1.5 text-left text-[#888]">{row.label}</td>
												<td class="px-2 py-1.5 text-right text-[#aaa]">{row.a}</td>
												<td class="px-2 py-1.5 text-right text-[#aaa]">{row.b}</td>
												<td class={`px-2 py-1.5 text-right ${compareDeltaClass(row)}`}>{compareDeltaLabel(row)}</td>
											</tr>
										{/each}
									</tbody>
								</table>

								<div class="mt-3 border-t border-[#1a1a1a] pt-3">
									<div class="flex items-center gap-2 text-[10px] uppercase tracking-widest text-[#555]">
										<span>Parameter Diff</span>
										<span class="rounded-full border border-[#2b2b2b] bg-black px-1.5 py-0.5 font-mono normal-case tracking-normal text-[#888]">{compareChangedParamCount} changed</span>
									</div>
									{#if compareParamDiff.length === 0}
										<div class="mt-2 text-[11px] text-[#555]">No stored params on either run.</div>
									{:else}
										<div class="mt-2 max-h-[260px] overflow-auto">
											<table class="w-full text-xs" data-testid="compare-param-diff">
												<tbody>
													{#each compareParamDiff.filter((row) => !row.same) as row (row.key)}
														<tr class="border-t border-[#161616] font-mono">
															<td class="px-2 py-1 text-left text-[#888]">{row.key}</td>
															<td class="px-2 py-1 text-right text-white">{row.a}</td>
															<td class="px-2 py-1 text-right text-yellow-400">{row.b}</td>
														</tr>
													{/each}
													{#if compareChangedParamCount === 0}
														<tr><td class="px-2 py-2 text-[11px] text-[#555]" colspan="3">All {compareParamDiff.length} stored params are identical.</td></tr>
													{:else if compareParamDiff.length - compareChangedParamCount > 0}
														<tr><td class="px-2 py-2 text-[11px] text-[#555]" colspan="3">{compareParamDiff.length - compareChangedParamCount} identical param{compareParamDiff.length - compareChangedParamCount === 1 ? '' : 's'} hidden.</td></tr>
													{/if}
												</tbody>
											</table>
										</div>
									{/if}
								</div>
							</div>

							<div>
								<div class="flex items-center gap-3 text-[10px] uppercase tracking-wide text-[#555]">
									<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-white"></span>A · {compareItemA.result_id}</span>
									<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-yellow-500/10"></span>B · {compareItemB.result_id}</span>
								</div>
								{#if compareError}
									<div class="mt-2 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400">{compareError}</div>
								{:else if compareLoading}
									<div class="mt-2 border border-[#333] bg-[#0c0c0c] px-3 py-4 text-xs text-white">Loading equity curves…</div>
								{:else if compareOverlayReady}
									<div class="mt-2" data-testid="compare-equity-overlay">
										{#key `${compareItemA.result_id}:${compareItemB.result_id}`}
											<EquityChart
												data={compareCurveA ?? []}
												benchmarkData={compareCurveB}
												benchmarkTitle={`B · ${compareItemB.result_id}`}
												showDrawdown={false}
												height={280}
											/>
										{/key}
									</div>
								{:else}
									<div class="mt-2 border border-[#1f1f1f] bg-black px-3 py-4 text-xs text-[#555]">
										Equity curves are unavailable for one or both runs (older results may not have stored them).
									</div>
								{/if}
							</div>
						</div>
					</div>
				{/if}

				<div class="border border-[#1d1d1d] bg-[#090909] p-3">
					<div class="flex items-center justify-between gap-2">
						<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Gauntlet history</div>
						<span class="text-[11px] text-[#555]">{backtestHistory.length} run{backtestHistory.length === 1 ? '' : 's'}</span>
					</div>
					<p class="mt-2 text-[11px] leading-relaxed text-[#555]">
						Left columns are <span class="text-[#888]">full-window</span> (IS + OOS combined); the
						<span class="text-[#888]">OOS</span> columns on the right are out-of-sample only.
						<span class="text-[#888]">ⓘ</span>/<span class="text-[#888]">~</span> marks an approximation
						(e.g. Sharpe is a month-weighted average and Max DD is the max of the IS/OOS halves, not recomputed
						from the combined stream). Hover any header for details.
					</p>
					{#if backtestHistory.length === 0}
						<div class="mt-4 border border-[#1f1f1f] bg-[#070707] px-4 py-6 text-sm text-[#555]">No Gauntlet runs yet.</div>
					{:else}
						<div class="mt-4 overflow-hidden border border-[#1f1f1f] bg-[#070707]">
							<div class="max-h-[620px] overflow-auto">
								<table class="min-w-full text-xs">
									<thead class="sticky top-0 z-10 bg-[#0d0d0d] text-[10px] uppercase tracking-widest text-[#555]">
										<tr>
											<th class="px-3 py-2 text-left">Run</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('created')}>Created{historySortIndicator('created')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('symbol')}>Symbol{historySortIndicator('symbol')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('timeframe')}>TF{historySortIndicator('timeframe')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('start')}>Start{historySortIndicator('start')}</th>
											<th class="px-3 py-2 text-left cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('end')}>End{historySortIndicator('end')}</th>
											<th class="px-3 py-2 text-left">Window</th>
											<th class="px-3 py-2 text-left">Params</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('cagr')} title="Full-window CAGR (annualized over IS + OOS). Short windows are shown with muted styling.">CAGR{historySortIndicator('cagr')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('sharpe')} title="Full-window Sharpe (approximate: month-weighted average of IS and OOS Sharpe, not recomputed from the combined return stream). Low-trade samples are shown with muted styling.">Sharpe ⓘ{historySortIndicator('sharpe')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('max_drawdown')} title="Full-window max drawdown (approximate: max of IS and OOS max drawdowns; a drawdown that straddles the IS/OOS boundary is understated).">Max DD ⓘ{historySortIndicator('max_drawdown')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('win_rate')} title="Full-window win rate = combined wins / combined closed trades.">Win%{historySortIndicator('win_rate')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('trades')} title="Total completed trades across IS + OOS.">Trades{historySortIndicator('trades')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('profit_factor')} title="Full-window profit factor = combined gross profit / combined gross loss. ∞ if no losing trades.">PF{historySortIndicator('profit_factor')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('robustness')} title={`Gauntlet robustness score; below ${gauntletMinScore ?? 60} fails the promotion gate`}>Rob%{historySortIndicator('robustness')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa] border-l border-[#222] pl-3" on:click={() => toggleHistorySort('oos_cagr')} title="Out-of-sample CAGR (annualized). Short windows are shown with muted styling.">OOS CAGR{historySortIndicator('oos_cagr')}</th>
											<th class="px-3 py-2 text-right cursor-pointer select-none hover:text-[#aaa]" on:click={() => toggleHistorySort('oos_sharpe')} title="Out-of-sample annualized Sharpe. Low-trade samples are shown with muted styling.">OOS Sharpe{historySortIndicator('oos_sharpe')}</th>
											<th class="px-3 py-2 text-right">Actions</th>
										</tr>
									</thead>
									<tbody>
										{#each backtestHistory as item}
											<tr
												data-testid={`backtest-row-${item.result_id}`}
												class={`border-t border-[#161616] font-mono transition ${
													pinnedBacktestId && pinnedBacktestId === item.result_id
														? selectedResultId === item.result_id
															? 'bg-emerald-950/30 shadow-[inset_2px_0_0_0_rgba(16,185,129,1),inset_-2px_0_0_0_rgba(34,211,238,0.9)]'
															: 'bg-emerald-950/15 shadow-[inset_2px_0_0_0_rgba(16,185,129,0.9)] hover:bg-emerald-950/25'
														: selectedResultId === item.result_id
															? 'bg-[#0c0c0c] shadow-[inset_2px_0_0_0_rgba(34,211,238,0.9)]'
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
														<input
															type="checkbox"
															data-testid={`compare-select-${item.result_id}`}
															class="h-3.5 w-3.5 border border-[#2b2b2b] bg-black text-white"
															title="Select for run comparison (pick two)"
															checked={compareSelection.includes(item.result_id)}
															on:click|stopPropagation={() => toggleCompareSelection(item)}
														/>
														<span class="text-white">{item.result_id}</span>
														<span class={`rounded-full border px-2 py-0.5 text-[9px] ${resultTypeBadge(item.result_type)}`}>{resultTypeLabel(item.result_type)}</span>
														{#if pinnedBacktestId && pinnedBacktestId === item.result_id}
															<span class="rounded-full border border-emerald-600/60 bg-emerald-950/30 px-2 py-0.5 text-[9px] uppercase tracking-wide text-emerald-200" title="This backtest's metrics and params drive the Lab manager display and paper/live trading.">Active</span>
														{/if}
													</div>
												</td>
												<td class="px-3 py-2 text-left text-[#888]">{fmtDate(item.created_at)}</td>
												<td class="px-3 py-2 text-left text-white">{item.symbol || '--'}</td>
												<td class="px-3 py-2 text-left text-[#aaa]">{item.timeframe || '--'}</td>
												<td class="px-3 py-2 text-left text-[#888]">{fmtShortDate(item.start_date)}</td>
												<td class="px-3 py-2 text-left text-[#888]">{fmtShortDate(item.end_date)}</td>
												<td class="px-3 py-2 text-left text-[#888]">{fmtDuration(item.start_date, item.end_date, readMetricOptional(item, 'backtest_months'))}</td>
												<td class="px-3 py-2 text-left">
													<div data-testid={`backtest-param-summary-${item.result_id}`} class="flex max-w-[320px] flex-wrap gap-1">
														{#if getHistoryParamSource(item) === 'current'}
															<span class="rounded-full border border-yellow-900 bg-yellow-500/5 px-2 py-0.5 text-[10px] text-yellow-400">
																Current strategy params
															</span>
														{/if}
														{#if getBacktestVisibleParamSummary(item, Boolean(expandedBacktestParamSummaryIds[String(item.result_id || '').trim()])).length > 0}
															{#each getBacktestVisibleParamSummary(item, Boolean(expandedBacktestParamSummaryIds[String(item.result_id || '').trim()])) as entry}
																<span class={`rounded-full border px-2 py-0.5 text-[10px] ${entry.changed ? 'border-yellow-900 bg-yellow-500/5 text-yellow-400' : 'border-[#2b2b2b] bg-black text-[#aaa]'}`}>
																	{entry.key}={entry.value}
																</span>
															{/each}
															{#if getBacktestParamOverflowCount(item) > 0}
																<button
																	type="button"
																	data-testid={`backtest-param-overflow-${item.result_id}`}
																	class="rounded-full border border-[#2b2b2b] bg-black px-2 py-0.5 text-[10px] text-[#555] transition hover:border-[#555] hover:text-white"
																	aria-expanded={Boolean(expandedBacktestParamSummaryIds[String(item.result_id || '').trim()])}
																	aria-label={`${Boolean(expandedBacktestParamSummaryIds[String(item.result_id || '').trim()]) ? 'Hide extra parameters for' : 'Show all parameters for'} ${item.result_id}`}
																	on:click|stopPropagation={() => toggleBacktestParamSummary(item)}
																>
																	{Boolean(expandedBacktestParamSummaryIds[String(item.result_id || '').trim()]) ? 'Show less' : `+${getBacktestParamOverflowCount(item)} more`}
																</button>
															{/if}
														{:else}
															<span class="text-[11px] text-[#555]">No stored params</span>
														{/if}
													</div>
												</td>
												<td class={`px-3 py-2 text-right ${isCagrReliable(item) ? signedPercentClass(readPercentMetricOptional(item, 'annualized_return_pct')) : 'text-[#555]'}`}
													title={isCagrReliable(item) ? 'Full-window CAGR (annualized over IS + OOS)' : `Short window (<1 month) — annualized value may be noisy`}>
													{formatCagr(item)}
												</td>
												<td class={`px-3 py-2 text-right ${isSharpeReliable(item) ? 'text-[#aaa]' : 'text-[#555]'}`}
													title={isSharpeReliable(item) ? 'Full-window Sharpe (approximate: month-weighted avg of IS and OOS)' : `Low trade count (<20) — Sharpe may be noisy`}>
													{formatSharpe(item)}{readFlag(item, 'sharpe_is_approximation') === true ? ' ~' : ''}
												</td>
												<td class="px-3 py-2 text-right text-red-400" title={readFlag(item, 'max_drawdown_is_approximation') === true ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{pct(readDrawdownPercentMetric(item, 'max_drawdown_pct', 'max_drawdown'))}{readFlag(item, 'max_drawdown_is_approximation') === true ? ' ~' : ''}</td>
												<td class="px-3 py-2 text-right text-[#aaa]">{pct(readPercentMetric(item, 'win_rate', 'win_rate_pct'))}</td>
												<td class="px-3 py-2 text-right text-[#aaa]">{historyTradesCount(item)}</td>
												<td class="px-3 py-2 text-right text-[#aaa]"
													title={readFlag(item, 'profit_factor_is_infinite') === true ? 'No losing trades — profit factor is mathematically infinite' : 'Full-window profit factor'}>
													{formatProfitFactor(item)}
												</td>
												<td class="px-3 py-2 text-right text-[#aaa]" title="Gauntlet robustness score">{formatRobustness(item)}</td>
												<td class={`px-3 py-2 text-right border-l border-[#222] pl-3 ${isCagrReliable(item) ? signedPercentClass(readOutOfSampleCagr(item)) : 'text-[#555]'}`}
													title={isCagrReliable(item) ? 'Out-of-sample CAGR (annualized)' : `Short OOS window (<1 month) — annualized value may be noisy`}>
													{formatOutOfSampleCagr(item)}
												</td>
												<td class={`px-3 py-2 text-right ${isSharpeReliable(item) ? 'text-[#aaa]' : 'text-[#555]'}`}
													title={isSharpeReliable(item) ? 'Out-of-sample annualized Sharpe' : `Low trade count (<20) — Sharpe may be noisy`}>
													{formatOutOfSampleSharpe(item)}
												</td>
												<td class="px-3 py-2 text-right">
													<div class="flex items-center justify-end gap-2">
														<button
															type="button"
															data-testid={`set-default-backtest-params-${item.result_id}`}
															class={` border px-2.5 py-1 text-[10px] uppercase tracking-widest transition disabled:opacity-60 ${
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
															class={` border px-2.5 py-1 text-[10px] uppercase tracking-widest transition ${
																expandedBacktestParamsId === item.result_id
																	? 'border-[#333] bg-[#0c0c0c] text-white'
																	: 'border-[#2b2b2b] bg-black text-[#888] hover:border-white/20 hover:text-white'
															}`}
															on:click|stopPropagation={() => toggleBacktestParamEditor(item)}
														>
															{expandedBacktestParamsId === item.result_id ? 'Hide' : 'Edit'}
														</button>
														<button
															type="button"
															class="p-1 text-[#555] transition-colors hover:bg-red-900/30 hover:text-red-400"
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
														<div class="border border-[#1f1f1f] bg-black p-4">
															<div class="flex flex-wrap items-start justify-between gap-3">
																<div>
																	<div class="text-[10px] uppercase tracking-widest text-[#555]">Run Parameter Editor</div>
																	<div class="mt-1 text-sm text-[#888]">
																		Tweak the stored run parameters here, then rerun the exact market window without leaving history.
																	</div>
																</div>
																<div class="flex flex-wrap items-center gap-2">
																	<button
																		type="button"
																		data-testid={`load-backtest-params-${item.result_id}`}
																		class="border border-[#2b2b2b] bg-[#070707] px-3 py-2 text-[11px] font-medium uppercase tracking-widest text-[#aaa] transition hover:border-white/20 hover:text-white"
																		on:click|stopPropagation={() => loadBacktestParamsIntoDraft(item)}
																	>
																		Load Into Draft
																	</button>
																	<button
																		type="button"
																		class="border border-[#2b2b2b] bg-[#070707] px-3 py-2 text-[11px] font-medium uppercase tracking-widest text-[#aaa] transition hover:border-white/20 hover:text-white"
																		on:click|stopPropagation={() => resetBacktestParamDraft(item)}
																	>
																		Reset
																	</button>
																	<button
																		type="button"
																		data-testid={`rerun-backtest-params-${item.result_id}`}
																		class="border border-[#333] bg-[#0c0c0c] px-3 py-2 text-[11px] font-medium uppercase tracking-widest text-white transition hover:bg-[#111] disabled:opacity-40"
																		on:click|stopPropagation={() => void rerunBacktestFromHistory(item)}
																					disabled={isAnyRunInFlight || Boolean(backtestParamDraftErrors[item.result_id])}
																	>
																		{backtestParamRunnerId === item.result_id ? 'Running…' : 'Rerun With Changes'}
																	</button>
																</div>
															</div>
															<div class="mt-3 flex flex-wrap gap-2 text-[11px] text-[#555]">
																<span class="rounded-full border border-[#2b2b2b] bg-[#070707] px-2 py-1">
																	{item.symbol || '--'} / {item.timeframe || '--'}
																</span>
																<span class="rounded-full border border-[#2b2b2b] bg-[#070707] px-2 py-1">
																	{fmtShortDate(item.start_date)} -> {fmtShortDate(item.end_date)}
																</span>
															</div>
															<div class="mt-4">
																{#if getHistoryParamSource(item) === 'current'}
																	<div class="mb-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-400">
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
					<div class="mb-3 border border-[#1d1d1d] bg-[#090909] p-3">
						<div class="grid gap-3 lg:grid-cols-[1fr_1fr_auto]">
							<div class="grid gap-2 sm:grid-cols-2">
								<SymbolInput id="container-opt-symbol" label="Symbol" bind:value={optimizationForm.symbol} suggestions={symbolSuggestions} helpText={optimizationSymbolHelpText} />
								<TimeframeSelect id="container-opt-timeframe" label="Timeframe" bind:value={optimizationForm.timeframe} />
							</div>
							<DateRangeFieldset idPrefix="container-opt" title="Window" bind:startDate={optimizationForm.start_date} bind:endDate={optimizationForm.end_date} timeframe={optimizationForm.timeframe} accent="blue" />
							<div class="flex items-end">
								<button type="button" class="w-full border border-[#333] bg-[#0c0c0c] px-5 py-2 text-xs font-semibold uppercase tracking-wider text-white transition hover:bg-[#111] disabled:opacity-40 lg:w-auto" on:click={submitContainerOptimization} disabled={isAnyRunInFlight}>{submitStatus === 'submitting' || submitStatus === 'running' ? 'Running…' : 'Run Optimization'}</button>
							</div>
						</div>
						<div class="mt-2 grid gap-2 sm:grid-cols-2">
							<label class="block" for="container-opt-objective">
								<div class="text-[10px] uppercase tracking-wide text-[#555]">Objective</div>
								<select id="container-opt-objective" bind:value={optimizationForm.objective} class="mt-1 w-full border border-[#2b2b2b] bg-[#050505] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60">
									{#each OPTIMIZATION_OBJECTIVES as option}
										<option value={option.value}>{option.label}</option>
									{/each}
								</select>
							</label>
							<label class="block" for="container-opt-trials">
								<div class="text-[10px] uppercase tracking-wide text-[#555]">Trials</div>
								<input id="container-opt-trials" type="number" min="1" class="mt-1 w-full border border-[#2b2b2b] bg-[#050505] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60" bind:value={optimizationForm.n_trials} />
							</label>
						</div>
						<div data-testid="optimization-params-panel" class="mt-3 border border-[#1d1d1d] bg-black/40 p-3">
							<div class="flex items-center justify-between gap-2">
								<div>
									<div class="text-[10px] uppercase tracking-wide text-[#555]">Optimization Parameters</div>
									<div class="mt-1 text-xs text-[#888]">Select numeric params to optimize. Unchecked params stay fixed at the current strategy defaults.</div>
								</div>
								<div class="rounded-full border border-[#333] bg-[#0c0c0c] px-2 py-1 text-[10px] uppercase tracking-widest text-white">
									{optimizationParamSelectedCount} selected
								</div>
							</div>
							{#if Object.keys(optimizationParamDrafts).length === 0}
								<div class="mt-3 border border-[#1a1a1a] bg-[#050505] px-3 py-2 text-xs text-[#555]">
									No numeric strategy params are available for optimization on this container.
								</div>
							{:else}
								<label class="mt-3 flex w-fit items-center gap-3 text-xs">
									<input
										data-testid="opt-param-select-all"
										type="checkbox"
										class="h-4 w-4 border border-[#2b2b2b] bg-black text-white"
										checked={allOptimizationParamsSelected}
										indeterminate={someOptimizationParamsSelected}
										on:change={(event) => setAllOptimizationParamsSelected((event.currentTarget as HTMLInputElement).checked)}
									/>
									<span class="font-medium uppercase tracking-wide text-[#aaa]">Select all</span>
								</label>
								<div class="mt-2 grid gap-2">
									{#each Object.values(optimizationParamDrafts) as draft (draft.key)}
										<div class="border border-[#1d1d1d] bg-[#050505] p-3">
											<div class="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
												<label class="flex items-center gap-3 text-sm text-white">
													<input
														data-testid={`opt-param-select-${draft.key}`}
														type="checkbox"
														class="h-4 w-4 border border-[#2b2b2b] bg-black text-white"
														checked={draft.selected}
														on:change={(event) => setOptimizationParamSelected(draft.key, (event.currentTarget as HTMLInputElement).checked)}
													/>
													<span class="font-medium uppercase tracking-wide text-gray-200">{draft.key}</span>
													<span class="rounded-full border border-[#2b2b2b] bg-[#0c0c0c] px-2 py-0.5 text-[10px] uppercase tracking-widest text-white">Current {optimizationParamCurrentLabel(draft.current, draft.kind)}</span>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-[#555]">Min</div>
													<input
														data-testid={`opt-param-min-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.min}
														on:input={(event) => updateOptimizationParamField(draft.key, 'min', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-[#555]">Max</div>
													<input
														data-testid={`opt-param-max-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.max}
														on:input={(event) => updateOptimizationParamField(draft.key, 'max', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<label class="block">
													<div class="text-[10px] uppercase tracking-wide text-[#555]">Step</div>
													<input
														data-testid={`opt-param-step-${draft.key}`}
														type="number"
														step={draft.kind === 'int' ? '1' : 'any'}
														class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
														value={draft.step}
														on:input={(event) => updateOptimizationParamField(draft.key, 'step', (event.currentTarget as HTMLInputElement).value)}
													/>
												</label>
												<div class="flex items-end">
													<div class="border border-[#1d1d1d] bg-[#090909] px-3 py-2 text-[11px] text-[#888]">
														{draft.kind === 'int' ? 'Whole-number sweep' : 'Decimal sweep'}
													</div>
												</div>
											</div>
											{#if draft.error}
												<div data-testid={`opt-param-error-${draft.key}`} class="mt-2 border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-200">
													{draft.error}
												</div>
											{/if}
										</div>
									{/each}
								</div>
							{/if}
							<div class="mt-4 border-t border-[#1a1a1a] pt-4">
								<div class="flex items-center justify-between gap-2">
									<div class="text-[10px] uppercase tracking-wide text-[#555]">Execution Settings</div>
									<div class="rounded-full border border-[#333] bg-[#0c0c0c] px-2 py-1 text-[10px] uppercase tracking-widest text-white">
										{optimizationExecutionSelectedCount} selected
									</div>
								</div>
								{#if Object.keys(optimizationExecutionDrafts).length === 0}
									<div class="mt-3 border border-[#1a1a1a] bg-[#050505] px-3 py-2 text-xs text-[#555]">
										No numeric execution settings are active for the current sizing mode.
									</div>
								{:else}
									<label class="mt-3 flex w-fit items-center gap-3 text-xs">
										<input
											data-testid="opt-exec-select-all"
											type="checkbox"
											class="h-4 w-4 border border-[#2b2b2b] bg-black text-white"
											checked={allOptimizationExecutionSelected}
											indeterminate={someOptimizationExecutionSelected}
											on:change={(event) => setAllOptimizationExecutionSelected((event.currentTarget as HTMLInputElement).checked)}
										/>
										<span class="font-medium uppercase tracking-wide text-[#aaa]">Select all execution</span>
									</label>
									<div class="mt-2 grid gap-2">
										{#each Object.values(optimizationExecutionDrafts) as draft (draft.key)}
											<div class="border border-[#1d1d1d] bg-[#050505] p-3">
												<div class="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
													<label class="flex items-center gap-3 text-sm text-white">
														<input
															data-testid={`opt-exec-select-${draft.key}`}
															type="checkbox"
															class="h-4 w-4 border border-[#2b2b2b] bg-black text-white"
															checked={draft.selected}
															on:change={(event) => setOptimizationExecutionSelected(draft.key, (event.currentTarget as HTMLInputElement).checked)}
														/>
														<span class="font-medium uppercase tracking-wide text-gray-200">{draft.key}</span>
														<span class="rounded-full border border-[#2b2b2b] bg-[#0c0c0c] px-2 py-0.5 text-[10px] uppercase tracking-widest text-white">Current {optimizationParamCurrentLabel(draft.current, draft.kind)}</span>
													</label>
													<label class="block">
														<div class="text-[10px] uppercase tracking-wide text-[#555]">Min</div>
														<input
															data-testid={`opt-exec-min-${draft.key}`}
															type="number"
															step={draft.kind === 'int' ? '1' : 'any'}
															class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
															value={draft.min}
															on:input={(event) => updateOptimizationExecutionField(draft.key, 'min', (event.currentTarget as HTMLInputElement).value)}
														/>
													</label>
													<label class="block">
														<div class="text-[10px] uppercase tracking-wide text-[#555]">Max</div>
														<input
															data-testid={`opt-exec-max-${draft.key}`}
															type="number"
															step={draft.kind === 'int' ? '1' : 'any'}
															class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
															value={draft.max}
															on:input={(event) => updateOptimizationExecutionField(draft.key, 'max', (event.currentTarget as HTMLInputElement).value)}
														/>
													</label>
													<label class="block">
														<div class="text-[10px] uppercase tracking-wide text-[#555]">Step</div>
														<input
															data-testid={`opt-exec-step-${draft.key}`}
															type="number"
															step={draft.kind === 'int' ? '1' : 'any'}
															class="mt-1 w-full border border-[#2b2b2b] bg-[#090909] px-2 py-1.5 text-sm text-white outline-none focus:border-white/60"
															value={draft.step}
															on:input={(event) => updateOptimizationExecutionField(draft.key, 'step', (event.currentTarget as HTMLInputElement).value)}
														/>
													</label>
													<div class="flex items-end">
														<div class="border border-[#1d1d1d] bg-[#090909] px-3 py-2 text-[11px] text-[#888]">
															{draft.kind === 'int' ? 'Whole-number sweep' : 'Decimal sweep'}
														</div>
													</div>
												</div>
												{#if draft.error}
													<div data-testid={`opt-exec-error-${draft.key}`} class="mt-2 border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-200">
														{draft.error}
													</div>
												{/if}
											</div>
										{/each}
									</div>
								{/if}
							</div>
						</div>
					</div>

					<div class="grid grid-cols-1 gap-4">
						<div class="border border-[#1d1d1d] bg-[#090909] p-4">
							<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-[#555]">Optimization Runs</div>
							{#if optimizationHistory.length === 0}
								<div class="px-3 py-4 text-xs text-[#555]">No optimization runs yet.</div>
							{:else}
								<div class="mt-3 grid gap-3">
									{#each optimizationHistory as item}
										{@const objectiveName = String(item.config?.objective || item.metrics?.objective || '').trim()}
										{@const bestParamChips = formatOptimizationChipRecord(getOptimizationHistoryBestParams(item), 8)}
										{@const executionChips = formatOptimizationChipRecord(getOptimizationHistoryExecutionProfile(item), 8)}
										{@const topResults = optimizationTopResults(item)}
										<button
											data-testid={`optimization-row-${item.result_id}`}
											class={` border border-[#222] bg-[#090909] px-4 py-3 text-left transition ${historyCardBorder(item.result_type)} ${selectedResultId === item.result_id ? 'border-[#333] shadow-[0_0_0_1px_rgba(96,165,250,0.08),0_18px_40px_rgba(59,130,246,0.08)]' : ''}`}
											on:click={() => void openResult(item)}
										>
											<div class="flex flex-wrap items-center gap-2 text-xs">
												<span class="break-all font-mono text-white">{item.result_id}</span>
												<span class={` border px-1 py-0.5 text-[10px] ${resultTypeBadge(item.result_type)}`}>{item.result_type}</span>
												<span class={` border px-1.5 py-0.5 text-[10px] ${statusBadgeClass(historyItemStatus(item))}`}>{statusLabel(historyItemStatus(item))}</span>
												<span class="border border-[#252525] bg-black px-2 py-0.5 font-mono text-[10px] text-[#aaa]">{optimizationRunMarketLabel(item)}</span>
												<span class="border border-[#252525] bg-black px-2 py-0.5 font-mono text-[10px] text-[#888]">{optimizationRunWindowLabel(item)}</span>
												<span class="ml-auto text-[#555]">{fmtDate(item.created_at)}</span>
											</div>
											{#if historyItemError(item)}
												<div class="mt-2 border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-200">
													{historyItemError(item)}
												</div>
											{/if}
											<div class="mt-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-3 xl:grid-cols-6">
												<div class="border-l border-[#333] bg-black/50 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">{optimizationObjectiveLabel(objectiveName)}</div>
													<div class="mt-1 font-mono text-sm text-white">{formatOptimizationObjectiveValue(item)}</div>
												</div>
												<div class="border-l border-[#252525] bg-black/40 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Fitness</div>
													<div class="mt-1 font-mono text-sm text-[#aaa]">{numOrDash(readMetricOptional(item, 'best_fitness', 'fitness'))}</div>
												</div>
												<div class="border-l border-[#252525] bg-black/40 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Sharpe</div>
													<div class={`mt-1 font-mono text-sm ${isSharpeReliable(item) ? 'text-[#aaa]' : 'text-[#555]'}`} title={isSharpeReliable(item) ? undefined : 'Low trade count (<20) — Sharpe may be noisy'}>{formatSharpe(item)}</div>
												</div>
												<div class="border-l border-[#252525] bg-black/40 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Return</div>
													<div class={`mt-1 font-mono text-sm ${signedPercentClass(readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct'))}`}>{pctOrDash(readPercentMetricOptional(item, 'total_return_pct', 'total_return', 'pnl_pct'))}</div>
												</div>
												<div class="border-l border-[#252525] bg-black/40 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">WFA</div>
													<div class={`mt-1 font-mono text-sm ${optimizationWfaClass(item)}`}>{optimizationWfaLabel(item)}</div>
												</div>
												<div class="border-l border-[#252525] bg-black/40 px-3 py-2">
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Trials</div>
													<div class="mt-1 font-mono text-sm text-[#aaa]">{historyItemTrials(item) ?? '--'}</div>
												</div>
											</div>
											<div class="mt-3 grid gap-3 border-t border-[#1a1a1a] pt-3 lg:grid-cols-2">
												<div>
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Best Params</div>
													{#if bestParamChips.length}
														<div class="mt-2 flex flex-wrap gap-1.5">
															{#each bestParamChips as chip}
																<span class="border border-[#333] bg-[#0c0c0c] px-2 py-1 font-mono text-[10px] text-white">{chip}</span>
															{/each}
														</div>
													{:else}
														<div class="mt-2 text-[11px] text-[#555]">No optimized signal parameters stored.</div>
													{/if}
												</div>
												<div>
													<div class="text-[10px] uppercase tracking-widest text-[#555]">Execution Profile</div>
													{#if executionChips.length}
														<div class="mt-2 flex flex-wrap gap-1.5">
															{#each executionChips as chip}
																<span class="border border-emerald-900/50 bg-emerald-950/10 px-2 py-1 font-mono text-[10px] text-emerald-100">{chip}</span>
															{/each}
														</div>
													{:else}
														<div class="mt-2 text-[11px] text-[#555]">No execution overrides stored.</div>
													{/if}
												</div>
											</div>
											{#if topResults.length}
												<div class="mt-3 border-t border-[#1a1a1a] pt-3">
													<div class="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-widest text-[#555]">
														<span>Top Candidates</span>
														<span class="font-mono text-gray-700">{topResults.length}</span>
													</div>
													<div class="grid gap-2 xl:grid-cols-3">
														{#each topResults as candidate, index}
															{@const candidateParams = optimizationTopParamChips(candidate, 4)}
															{@const candidateExecution = optimizationTopExecutionChips(candidate, 4)}
															<div class="border border-[#1f1f1f] bg-black/40 px-3 py-2">
																<div class="flex items-center gap-2 text-[11px]">
																	<span class="font-mono text-[#555]">#{index + 1}</span>
																	<span class="font-mono text-white">{optimizationTopObjectiveLabel(candidate, item)}</span>
																	<span class="ml-auto font-mono text-[#555]">fit {optimizationTopFitnessLabel(candidate)}</span>
																</div>
																<div class="mt-2 flex flex-wrap gap-1">
																	{#each candidateParams as chip}
																		<span class="border border-[#282828] px-1.5 py-0.5 font-mono text-[9px] text-[#aaa]">{chip}</span>
																	{/each}
																	{#each candidateExecution as chip}
																		<span class="border border-emerald-900/40 px-1.5 py-0.5 font-mono text-[9px] text-emerald-200">{chip}</span>
																	{/each}
																</div>
															</div>
														{/each}
													</div>
												</div>
											{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>
					</div>
			{/if}

			{#if activeTab === 'robustness'}
					<div class="space-y-3">
						<GauntletStatusCard
							{strategyId}
							stage={currentLifecycleStage}
							selectedTestKey={selectedRobustnessTest}
							testOverrides={robustnessStatusOverrides}
							on:selectTest={(event) => {
								selectedRobustnessTest = event.detail.key;
							}}
							on:promote={() => openStageChange()}
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
							<div class="border border-[#1d1d1d] bg-[#090909] p-4">
								<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-[#555]">Walk Forward Runs</div>
								{#if walkForwardHistory.length === 0}
									<div class="px-3 py-4 text-xs text-[#555]">No walk-forward runs yet.</div>
								{:else}
									<div class="mt-3 grid gap-3">
										{#each walkForwardHistory as item}
											<button class={` border border-[#222] bg-[#090909] px-4 py-3 text-left transition ${historyCardBorder(item.result_type)} ${selectedResultId === item.result_id ? 'border-[#333] shadow-[0_0_0_1px_rgba(167,139,250,0.08),0_18px_40px_rgba(139,92,246,0.08)]' : ''}`} on:click={() => void openResult(item)}>
												<div class="flex items-center gap-2 text-xs">
													<span class="font-mono text-white">{item.result_id}</span>
													<span class={` border px-1 py-0.5 text-[10px] ${resultTypeBadge(item.result_type)}`}>{item.result_type}</span>
													<span class="ml-auto text-[#555]">{fmtDate(item.created_at)}</span>
												</div>
												<div class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
													<div class="border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-widest text-[#555]">IS Sharpe</div>
														<div class="mt-1 font-mono text-sm text-[#aaa]">{formatWalkForwardSharpe(item, 'avg_is_sharpe')}</div>
													</div>
													<div class="border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-widest text-[#555]">OOS Sharpe</div>
														<div class="mt-1 font-mono text-sm text-[#aaa]">{formatWalkForwardSharpe(item, 'avg_oos_sharpe')}</div>
													</div>
													<div class="border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-widest text-[#555]">Degradation</div>
														<div class={`mt-1 font-mono text-sm ${walkForwardDegradationClass(item)}`}>{formatWalkForwardDegradation(item)}</div>
													</div>
													<div class="border border-[#1f1f1f] bg-black px-3 py-2">
														<div class="text-[10px] uppercase tracking-widest text-[#555]">OOS Trades</div>
														<div class="mt-1 font-mono text-sm text-[#aaa]">{formatWalkForwardOosTrades(item)}</div>
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


			{#if (activeTab === 'backtests' || activeTab === 'optimizations' || activeTab === 'robustness') && (resultLoading || !!resultError || !!selectedResult)}
				<div class="mt-3 border border-[#1d1d1d] bg-[#090909] p-3">
					{#if resultLoading}
						<div class="py-4 text-center text-sm text-[#555]">Loading result details...</div>
					{:else if resultError}
						<div class="border border-red-900/50 bg-red-950/20 px-3 py-2 text-sm text-red-300">{resultError}</div>
					{:else if selectedResult}
						<div>
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-mono text-sm text-white">{selectedResultId}</span>
									<span class={` border px-1.5 py-0.5 text-[10px] ${resultTypeBadge(selectedResult.result_type ?? '')}`}>{resultTypeLabel(selectedResult.result_type)}</span>
									<span data-testid="selected-result-status-badge" class={` border px-1.5 py-0.5 text-[10px] ${statusBadgeClass(selectedResultStatus)}`}>{statusLabel(selectedResultStatus)}</span>
									<span class="text-[11px] text-[#888]">{selectedResult.symbol || '--'} / {selectedResult.timeframe || '--'}</span>
									<span class="text-[11px] text-[#555]">{fmtShortDate(selectedResult.config?.start as string | undefined)} -> {fmtShortDate(selectedResult.config?.end as string | undefined)}</span>
									<span class="text-[11px] text-[#555]">{fmtDuration(selectedResult.config?.start as string | null | undefined, selectedResult.config?.end as string | null | undefined)}</span>
								</div>
								{#if isOptimizationResult()}
									<div class="flex items-center gap-1.5">
										<button type="button" class="border border-[#333] bg-[#0c0c0c] px-2 py-1 text-[10px] uppercase text-white hover:bg-[#111] disabled:opacity-40" on:click={backtestWithOptParams} disabled={isAnyRunInFlight}>{backtestingOptParams ? 'Running…' : 'Gauntlet With Params'}</button>
										<button type="button" class="border border-emerald-700 bg-emerald-950/30 px-2 py-1 text-[10px] uppercase text-emerald-200 hover:bg-emerald-900/40 disabled:opacity-40" on:click={setAsDefaultParams} disabled={settingDefaultParams || backtestingOptParams}>{settingDefaultParams ? 'Updating…' : 'Set As Default'}</button>
									</div>
								{/if}
							</div>
							{#if !selectedResultHasUsableMetrics}
								<div data-testid="selected-result-status-banner" class={`mt-3 border px-3 py-2 text-sm ${statusBadgeClass(selectedResultStatus)}`}>
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
							<div class="mt-4 border border-[#1f1f1f] bg-[#070707] p-4">
								<div class="flex flex-wrap items-center justify-between gap-3">
									<div>
										<div class="text-[10px] uppercase tracking-widest text-[#555]">Trade chart</div>
										<div class="mt-1 text-sm text-[#888]">Candles, trades, decision indicators, and the exact params captured for this run.</div>
										{#if selectedChartBars.length > 0}
											<div class="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-[#555]">
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
										<div class="rounded-full border border-[#333] bg-[#0c0c0c] px-2.5 py-1 text-[11px] text-white" data-testid="selected-chart-loading-chip">
											Building chart...
										</div>
									{:else if selectedChartContext}
										<div class="rounded-full border border-[#2b2b2b] bg-black px-2.5 py-1 text-[11px] text-[#888]" data-testid="selected-chart-source">
											{selectedChartContext.source === 'artifact' ? 'Stored snapshot' : 'Recomputed'}
										</div>
									{/if}
								</div>
								{#if chartContextError}
									<div class="mt-3 flex items-center justify-between gap-3 border border-yellow-900 bg-yellow-500/5 px-3 py-3 text-sm text-yellow-400" data-testid="selected-result-chart-error">
										<span>{chartContextError}</span>
										{#if selectedResultItem}
											<button
												type="button"
												class="shrink-0 border border-yellow-900 bg-yellow-500/10 px-2.5 py-1 text-xs text-yellow-400 transition hover:bg-yellow-500/10 disabled:opacity-40"
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
											<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-400">
												{warning}
											</div>
										{/each}
									</div>
								{/if}
								{#if chartLoading}
									<div class="mt-3 border border-[#333] bg-[#0c0c0c] px-4 py-6 text-sm text-white" data-testid="selected-result-chart-loading">
										Loading chart candles, trade markers, and decision overlays. Result details are ready below while this finishes.
									</div>
								{:else if selectedChartContext && selectedChartBars.length > 0}
									<div class="mt-3 h-[420px] overflow-hidden border border-[#111] bg-black" data-testid="selected-result-chart">
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
									<div class="mt-3 border border-[#1f1f1f] bg-black px-4 py-6 text-sm text-[#555]">
										No local OHLCV bars were available to render this run.
									</div>
								{/if}
							</div>
							{#if selectedResultHasEquityCurve}
								<div class="mt-3 border border-[#1f1f1f] bg-black p-3" data-testid="selected-result-equity-curve">
									<div class="flex flex-wrap items-center justify-between gap-2">
										<div>
											<div class="text-[10px] uppercase tracking-widest text-[#555]">Equity Curve</div>
											<div class="mt-1 text-xs text-[#555]">
												{#if selectedResultUsingFullCurve}
													Entire backtest — in-sample shaded, out-of-sample bright (OOS divider marked); buy &amp; hold (amber dashed); drawdown subchart. Metrics are OOS-only.
												{:else}
													Strategy equity (cyan), buy &amp; hold benchmark (amber dashed), and the drawdown subchart.
												{/if}
											</div>
										</div>
										<div class="flex items-center gap-3 text-[10px] uppercase tracking-wide text-[#555]">
											{#if selectedResultUsingFullCurve}
												<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-white"></span>In-sample</span>
											{/if}
											<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-white"></span>{selectedResultUsingFullCurve ? 'Out-of-sample' : 'Strategy'}</span>
											{#if benchmarkCurveForChart && benchmarkCurveForChart.length > 0}
												<span class="flex items-center gap-1.5"><span class="h-0.5 w-4 rounded-full bg-yellow-500/10"></span>Buy &amp; Hold</span>
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
							<div class="mt-3 border border-[#1f1f1f] bg-black px-3 py-2" data-testid="selected-result-metrics-strip">
								<div class="flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:items-stretch">
									<div class="border border-[#333] bg-[#0c0c0c] px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-widest text-white">In-sample (IS)</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={isResultCagrReliable(selectedResult) ? 'In-sample CAGR (annualized)' : 'Short window (<1 month) — annualized value may be noisy'}><span class="text-[10px] uppercase text-[#555] mr-1">CAGR</span> <span data-testid="selected-result-in-sample-cagr" class={isResultCagrReliable(selectedResult) ? signedPercentClass(readResultInSampleCagr(selectedResult)) : 'text-[#555]'}>{formatResultInSampleCagr(selectedResult)}</span></div>
											<div title={isResultSharpeReliable(selectedResult) ? 'In-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}><span class="text-[10px] uppercase text-[#555] mr-1">Sharpe</span> <span data-testid="selected-result-in-sample-sharpe" class={isResultSharpeReliable(selectedResult) ? 'text-[#aaa]' : 'text-[#555]'}>{formatResultInSampleSharpe(selectedResult)}</span></div>
										</div>
									</div>
									<div class="border border-emerald-900/30 bg-emerald-950/10 px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-widest text-emerald-500/80">Out-of-sample (OOS)</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={isResultCagrReliable(selectedResult) ? 'Out-of-sample CAGR (annualized)' : 'Short OOS window (<1 month) — annualized value may be noisy'}><span class="text-[10px] uppercase text-[#555] mr-1">CAGR</span> <span data-testid="selected-result-cagr" class={isResultCagrReliable(selectedResult) ? signedPercentClass(readResultOutOfSampleCagr(selectedResult)) : 'text-[#555]'}>{formatResultOutOfSampleCagr(selectedResult)}</span></div>
											<div title={isResultSharpeReliable(selectedResult) ? 'Out-of-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}><span class="text-[10px] uppercase text-[#555] mr-1">Sharpe</span> <span data-testid="selected-result-sharpe" class={isResultSharpeReliable(selectedResult) ? 'text-[#aaa]' : 'text-[#555]'}>{formatResultOutOfSampleSharpe(selectedResult)}</span></div>
											<div title="Cumulative out-of-sample return (not annualized)"><span class="text-[10px] uppercase text-[#555] mr-1">Return</span> <span data-testid="selected-result-total-return" class={readResultPercentMetric(selectedResult, 'total_return_pct', 'total_return') >= 0 ? 'text-emerald-400' : 'text-red-400'}>{pct(readResultPercentMetric(selectedResult, 'total_return_pct', 'total_return'))}</span></div>
											<div><span class="text-[10px] uppercase text-[#555] mr-1">Max DD</span> <span data-testid="selected-result-max-drawdown" class="text-red-400">{pct(readResultDrawdownPercentMetric(selectedResult, 'max_drawdown_pct', 'max_drawdown'))}</span></div>
											<div><span class="text-[10px] uppercase text-[#555] mr-1">Win%</span> <span data-testid="selected-result-win-rate" class="text-[#aaa]">{pct(readResultPercentMetric(selectedResult, 'win_rate', 'win_rate_pct'))}</span></div>
											<div><span class="text-[10px] uppercase text-[#555] mr-1">Trades</span> <span data-testid="selected-result-trades" class="text-[#aaa]">{formatResultTradesCount(selectedResult)}</span></div>
											<div title={readResultFlag(selectedResult, 'profit_factor_is_infinite') === true ? 'No losing trades — profit factor is mathematically infinite' : 'Gross profit / gross loss'}><span class="text-[10px] uppercase text-[#555] mr-1">PF</span> <span class="text-[#aaa]">{formatResultProfitFactor(selectedResult)}</span></div>
										</div>
									</div>
									<div class="border border-[#222] bg-[#070707] px-3 py-2">
										<div class="text-[9px] font-semibold uppercase tracking-widest text-[#555]">Gauntlet</div>
										<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
											<div title={`Gauntlet robustness score; below ${gauntletMinScore ?? 60} fails the promotion gate`}><span class="text-[10px] uppercase text-[#555] mr-1">Rob%</span> <span data-testid="selected-result-robustness" class="text-[#aaa]">{formatResultRobustness(selectedResult)}</span></div>
										</div>
									</div>
									{#if readResultCoverage(selectedResult, 'funding_coverage_pct') !== null || readResultCoverage(selectedResult, 'open_interest_coverage_pct') !== null}
										<div class="border border-[#222] bg-[#070707] px-3 py-2" data-testid="selected-result-data-coverage">
											<div class="text-[9px] font-semibold uppercase tracking-widest text-[#555]">Data</div>
											<div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs">
												<div title="Share of the backtest window with funding-rate data. Low coverage means funding costs are under-measured."><span class="text-[10px] uppercase text-[#555] mr-1">Funding</span> <span class={coverageToneClass(readResultCoverage(selectedResult, 'funding_coverage_pct'))}>{formatCoveragePct(readResultCoverage(selectedResult, 'funding_coverage_pct'))}</span></div>
												<div title="Share of the backtest window with open-interest data. OI accumulates forward from snapshots and cannot be backfilled."><span class="text-[10px] uppercase text-[#555] mr-1">OI</span> <span class={coverageToneClass(readResultCoverage(selectedResult, 'open_interest_coverage_pct'))}>{formatCoveragePct(readResultCoverage(selectedResult, 'open_interest_coverage_pct'))}</span></div>
											</div>
										</div>
									{/if}
								</div>
							</div>
							{#if readResultDataQualityFlags(selectedResult).length > 0}
								<div class="mt-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2" role="alert" data-testid="selected-result-data-quality-banner">
									<div class="text-[10px] font-semibold uppercase tracking-widest text-yellow-400">Data quality hold — metrics quarantined</div>
									<ul class="mt-1 list-disc pl-5 text-xs text-yellow-400">
										{#each readResultDataQualityFlags(selectedResult) as flag}
											<li>{flag}</li>
										{/each}
									</ul>
									<p class="mt-1 text-[11px] text-yellow-400">These numbers are implausible (engine/data bug signature) and are excluded from gate decisions. Re-run the backtest once data coverage has converged.</p>
								</div>
							{/if}
							{#if selectedResultRiskMetrics.length > 0}
								<div class="mt-3 border border-[#1f1f1f] bg-black px-3 py-3" data-testid="selected-result-risk-metrics">
									<div class="text-[10px] uppercase tracking-widest text-[#555]">Risk-adjusted metrics</div>
									<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
										{#each selectedResultRiskMetrics as metric}
											<div class="border border-[#1a1a1a] bg-[#070707] px-2.5 py-2" title={metric.title}>
												<div class="text-[9px] uppercase tracking-wide text-[#555]">{metric.label}</div>
												<div class={`mt-1 font-mono text-sm ${riskMetricToneClass(metric.tone)}`}>{metric.value}</div>
											</div>
										{/each}
									</div>
								</div>
							{/if}
							{#if selectedResultMonthlyHeatmap}
								<div class="mt-3 overflow-x-auto border border-[#1f1f1f] bg-black px-3 py-3" data-testid="selected-result-monthly-heatmap">
									<div class="text-[10px] uppercase tracking-widest text-[#555]">Monthly returns</div>
									<div class="mt-1 text-xs text-[#555]">Month-over-month equity change derived from the equity curve. Green = gain, red = loss.</div>
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
								<div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 border border-[#1f1f1f] bg-[#070707] px-3 py-2 text-xs">
									<div><span class="text-[10px] uppercase text-[#555] mr-1">Sharpe Rank</span> <span class="font-mono text-white">#{selectedResultComparison.sharpeRank}/{selectedResultComparison.sampleSize}</span> <span class="text-[#555]">({selectedResultComparison.sharpePercentile}p)</span></div>
									<div><span class="text-[10px] uppercase text-[#555] mr-1">Sharpe vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.sharpeDeltaVsMedian)}`}>{comparisonDeltaLabel(selectedResultComparison.sharpeDeltaVsMedian)}</span></div>
									<div><span class="text-[10px] uppercase text-[#555] mr-1">Return vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.returnDeltaVsMedian)}`}>{comparisonDeltaLabel(selectedResultComparison.returnDeltaVsMedian, { suffix: '%' })}</span></div>
									<div><span class="text-[10px] uppercase text-[#555] mr-1">DD vs Med</span> <span class={`font-mono ${comparisonDeltaClass(selectedResultComparison.drawdownDeltaVsMedian, { inverse: true })}`}>{comparisonDeltaLabel(selectedResultComparison.drawdownDeltaVsMedian, { inverse: true, suffix: '%' })}</span></div>
								</div>
							{/if}
								{#if isOptimizationResult()}
								<div class="mt-2 border border-[#333] bg-[#0c0c0c] px-2 py-1.5 text-[11px] text-white">
									{Object.keys(getOptBestParams() || {}).length} optimized parameters available
								</div>
							{/if}
							{/if}
							</div>
					{/if}

					{#if selectedResult?.trades?.length}
						<div class="mt-4 border border-[#222] bg-[#090909]" data-testid="selected-result-trades">
							<div class="flex flex-wrap items-center gap-2 border-b border-[#1a1a1a] px-3 py-2">
								<span class="text-[10px] uppercase tracking-wide text-[#555]">Out-of-sample trades ({selectedResult.trades.length})</span>
								<span class="rounded-full border border-emerald-900/40 bg-emerald-950/20 px-2 py-0.5 text-[9px] uppercase tracking-wide text-emerald-300/80" title="The trade list reflects out-of-sample execution only.">OOS</span>
							</div>
							<div class="max-h-[480px] overflow-auto">
								<table class="w-full text-xs">
									<thead class="sticky top-0 bg-[#0d0d0d] text-[#555]">
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
												<td class="px-2 py-1.5 text-right font-mono text-[#555]">{i + 1}</td>
												<td class="px-2 py-1.5 {trade.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}">{trade.direction ?? 'long'}</td>
												<td class="px-2 py-1.5 font-mono text-[#888]">{fmtDate(trade.entry_time)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-[#aaa]">{asNumber(trade.entry_price, 0).toFixed(2)}</td>
												<td class="px-2 py-1.5 font-mono text-[#888]">{fmtDate(trade.exit_time)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-[#aaa]">{asNumber(trade.exit_price, 0).toFixed(2)}</td>
												{#if selectedTradesHaveExitReason}
													<td class="px-2 py-1.5 text-left font-mono text-[#888]">{tradeExitReason(trade) ?? '-'}</td>
												{/if}
												{#if selectedTradesHaveSizeFraction}
													<td class="px-2 py-1.5 text-right font-mono text-[#888]">{tradeSizeFraction(trade) != null ? `${(tradeSizeFraction(trade)! * 100).toFixed(1)}%` : '-'}</td>
												{/if}
												<td class="px-2 py-1.5 text-right font-mono {asNumber(trade.pnl, 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{asNumber(trade.pnl, 0) >= 0 ? '+' : ''}${asNumber(trade.pnl, 0).toFixed(2)}</td>
												<td class="px-2 py-1.5 text-right font-mono {asNumber(trade.return_pct, 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{pct(trade.return_pct)}</td>
												<td class="px-2 py-1.5 text-right font-mono text-red-400/60">{trade.mae != null ? pct(trade.mae) : '-'}</td>
												<td class="px-2 py-1.5 text-right font-mono text-emerald-400/60">{trade.mfe != null ? pct(trade.mfe) : '-'}</td>
												<td class="px-2 py-1.5 text-right font-mono text-[#888]">{trade.bars_held ?? '-'}</td>
											</tr>
										{/each}
									</tbody>
									{#if selectedResultTradeSummary}
										<tfoot class="sticky bottom-0 border-t-2 border-[#222] bg-[#0d0d0d] text-[#888]">
											<tr>
												<td class="px-2 py-2 text-[10px] uppercase tracking-wide text-[#555]" colspan={selectedTradeColumnCount}>
													<div class="flex flex-wrap gap-x-4 gap-y-1 font-mono normal-case" data-testid="selected-result-trade-summary">
														<span><span class="text-[#555]">Wins</span> <span class="text-emerald-400">{selectedResultTradeSummary.wins}</span> / <span class="text-[#555]">Losses</span> <span class="text-red-400">{selectedResultTradeSummary.losses}</span>{#if selectedResultTradeSummary.breakeven > 0} / <span class="text-[#555]">BE</span> <span class="text-[#888]">{selectedResultTradeSummary.breakeven}</span>{/if}</span>
														<span><span class="text-[#555]">Win%</span> <span class="text-[#aaa]">{selectedResultTradeSummary.winRatePct.toFixed(1)}%</span></span>
														<span><span class="text-[#555]">Avg win</span> <span class="text-emerald-400">{formatSignedCurrency(selectedResultTradeSummary.avgWin)}</span></span>
														<span><span class="text-[#555]">Avg loss</span> <span class="text-red-400">{formatSignedCurrency(-selectedResultTradeSummary.avgLoss)}</span></span>
														<span><span class="text-[#555]">Payoff</span> <span class="text-[#aaa]">{selectedResultTradeSummary.payoffRatio != null ? selectedResultTradeSummary.payoffRatio.toFixed(2) : '∞'}</span></span>
														<span><span class="text-[#555]">Largest win</span> <span class="text-emerald-400">{formatSignedCurrency(selectedResultTradeSummary.largestWin)}</span></span>
														<span><span class="text-[#555]">Largest loss</span> <span class="text-red-400">{formatSignedCurrency(selectedResultTradeSummary.largestLoss)}</span></span>
														<span><span class="text-[#555]">Expectancy</span> <span class={selectedResultTradeSummary.expectancy >= 0 ? 'text-emerald-400' : 'text-red-400'}>{formatSignedCurrency(selectedResultTradeSummary.expectancy)}</span></span>
														<span><span class="text-[#555]">Win streak</span> <span class="text-emerald-400">{selectedResultTradeSummary.longestWinStreak}</span></span>
														<span><span class="text-[#555]">Loss streak</span> <span class="text-red-400">{selectedResultTradeSummary.longestLossStreak}</span></span>
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
				<div class="space-y-4">
					{#if executionRealizedSummary}
						<div class="border border-[#1d1d1d] bg-[#090909] p-3" data-testid="execution-summary-strip">
							<div class="flex flex-wrap items-center gap-x-5 gap-y-1.5 font-mono text-xs">
								<span class="text-[10px] uppercase tracking-[0.2em] text-[#555]">Realized</span>
								<span><span class="text-[#555]">Closed</span> <span class="text-[#aaa]">{executionRealizedSummary.count}</span></span>
								<span><span class="text-[#555]">Win%</span> <span class="text-[#aaa]">{executionRealizedSummary.winRatePct.toFixed(1)}%</span></span>
								<span><span class="text-[#555]">Total PnL</span> <span class={executionRealizedSummary.totalPnlUsd >= 0 ? 'text-emerald-400' : 'text-red-400'}>{formatSignedCurrency(executionRealizedSummary.totalPnlUsd)}</span></span>
								<span><span class="text-[#555]">PF</span> <span class="text-[#aaa]">{executionRealizedSummary.profitFactor !== null ? executionRealizedSummary.profitFactor.toFixed(2) : '∞'}</span></span>
								<span title="Average net PnL per closed trade (fees included)"><span class="text-[#555]">Avg Net</span> <span class={(executionRealizedSummary.avgNetPct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{executionRealizedSummary.avgNetPct !== null ? `${executionRealizedSummary.avgNetPct.toFixed(3)}%` : '—'}</span></span>
							</div>
						</div>
					{/if}

					<div class="border border-[#222] bg-[#090909]" data-testid="execution-open-trades">
						<div class="flex items-center justify-between gap-2 border-b border-[#1a1a1a] px-3 py-2">
							<span class="text-[10px] uppercase tracking-wide text-[#555]">Open Positions ({executionOpenTrades.length})</span>
							{#if executionOpenTrades.length > 0 && paperSessionId}
								<button
									type="button"
									data-testid="execution-close-position"
									class="border border-red-900/50 bg-red-950/20 px-2.5 py-1 text-[10px] uppercase tracking-wide text-red-300 transition hover:bg-red-900/30 disabled:opacity-50"
									disabled={closingPosition}
									on:click={() => void closeOpenPosition()}
									title="Close the session's open position at market — paper closes at a fresh mid, live sends a reduce-only market order."
								>{closingPosition ? 'Closing…' : 'Close Position'}</button>
							{/if}
						</div>
						{#if executionOpenTrades.length === 0}
							<div class="px-3 py-4 text-xs text-[#555]">No open positions.</div>
						{:else}
							<div class="max-h-[320px] overflow-auto">
								<table class="min-w-full text-xs">
									<thead class="sticky top-0 bg-[#0d0d0d] text-[#555]">
										<tr>
											<th class="px-3 py-2 text-left">ID</th>
											<th class="px-3 py-2 text-left">Asset</th>
											<th class="px-3 py-2 text-left">Side</th>
											<th class="px-3 py-2 text-left">Type</th>
											<th class="px-3 py-2 text-right">Entry</th>
											<th class="px-3 py-2 text-right">Size</th>
											<th class="px-3 py-2 text-right">Lev</th>
											<th class="px-3 py-2 text-right">Stop</th>
											<th class="px-3 py-2 text-right">Target</th>
											<th class="px-3 py-2 text-right" title="Signed entry slippage vs the signal price (positive = adverse)">Slip In</th>
											<th class="px-3 py-2 text-left">Opened</th>
										</tr>
									</thead>
									<tbody>
										{#each executionOpenTrades as row, index}
											{@const stop = tradeRowSignalNumber(row, 'stop_loss')}
											{@const target = tradeRowSignalNumber(row, 'take_profit')}
											<tr class="border-t border-[#111] font-mono">
												<td class="px-3 py-2 text-white">{getRowId(row, `open-${index}`)}</td>
												<td class="px-3 py-2 text-white">{getString(row, 'asset')}</td>
												<td class={`px-3 py-2 ${getString(row, 'direction') === 'short' ? 'text-red-400' : 'text-emerald-400'}`}>{getString(row, 'direction')}</td>
												<td class="px-3 py-2 text-[#888]">{getString(row, 'execution_type')}</td>
												<td class="px-3 py-2 text-right text-[#aaa]">{tradeRowPrice(row, 'fill_entry_price', 'entry_price')}</td>
												<td class="px-3 py-2 text-right text-[#888]">{asNumber(row.size, 0) ? asNumber(row.size, 0).toFixed(4) : '-'}</td>
												<td class="px-3 py-2 text-right text-[#888]">{asNumber(row.leverage, 0) ? `${asNumber(row.leverage, 0).toFixed(1)}×` : '-'}</td>
												<td class="px-3 py-2 text-right text-red-300">{stop !== null ? stop.toFixed(4) : '-'}</td>
												<td class="px-3 py-2 text-right text-emerald-300">{target !== null ? target.toFixed(4) : '-'}</td>
												<td class="px-3 py-2 text-right text-[#888]">{fmtBps(tradeRowSlipBps(row, 'entry_slippage_bps'))}</td>
												<td class="px-3 py-2 text-left text-[#888]">{fmtDate(row.opened_at)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</div>

					<div class="border border-[#222] bg-[#090909]" data-testid="execution-closed-trades">
						<div class="border-b border-[#1a1a1a] px-3 py-2 text-[10px] uppercase tracking-wide text-[#555]">Closed Trades ({executionClosedTrades.length})</div>
						{#if executionClosedTrades.length === 0}
							<div class="px-3 py-4 text-xs text-[#555]">No closed trades recorded.</div>
						{:else}
							<div class="max-h-[520px] overflow-auto">
								<table class="min-w-full text-xs">
									<thead class="sticky top-0 bg-[#0d0d0d] text-[#555]">
										<tr>
											<th class="px-3 py-2 text-left">ID</th>
											<th class="px-3 py-2 text-left">Asset</th>
											<th class="px-3 py-2 text-left">Side</th>
											<th class="px-3 py-2 text-left">Type</th>
											<th class="px-3 py-2 text-right">Entry</th>
											<th class="px-3 py-2 text-right">Exit</th>
											<th class="px-3 py-2 text-right">PnL $</th>
											<th class="px-3 py-2 text-right" title="Net PnL per trade (fees included) as % of margin">Net %</th>
											<th class="px-3 py-2 text-right" title="Realized round-trip fees (includes leverage)">Fees %</th>
											<th class="px-3 py-2 text-right" title="Signed entry slippage (positive = adverse)">Slip In</th>
											<th class="px-3 py-2 text-right" title="Signed exit slippage (positive = adverse)">Slip Out</th>
											<th class="px-3 py-2 text-left">Reason</th>
											<th class="px-3 py-2 text-left">Opened</th>
											<th class="px-3 py-2 text-left">Closed</th>
										</tr>
									</thead>
									<tbody>
										{#each executionClosedTrades as row, index}
											{@const pnlUsd = tradeRowPnlUsd(row)}
											{@const netPct = tradeRowNetPct(row)}
											{@const feesPct = tradeRowFeesPct(row)}
											<tr class="border-t border-[#111] font-mono hover:bg-[#0d0d0d]">
												<td class="px-3 py-2 text-white">{getRowId(row, `trade-${index}`)}</td>
												<td class="px-3 py-2 text-white">{getString(row, 'asset')}</td>
												<td class={`px-3 py-2 ${getString(row, 'direction') === 'short' ? 'text-red-400' : 'text-emerald-400'}`}>{getString(row, 'direction')}</td>
												<td class="px-3 py-2 text-[#888]">{getString(row, 'execution_type')}</td>
												<td class="px-3 py-2 text-right text-[#aaa]">{tradeRowPrice(row, 'fill_entry_price', 'entry_price')}</td>
												<td class="px-3 py-2 text-right text-[#aaa]">{tradeRowPrice(row, 'fill_exit_price', 'exit_price')}</td>
												<td class={`px-3 py-2 text-right ${(pnlUsd ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{pnlUsd !== null ? formatSignedCurrency(pnlUsd) : '-'}</td>
												<td class={`px-3 py-2 text-right ${(netPct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{netPct !== null ? `${netPct.toFixed(2)}%` : '-'}</td>
												<td class="px-3 py-2 text-right text-[#888]">{feesPct !== null ? `${feesPct.toFixed(3)}%` : '-'}</td>
												<td class="px-3 py-2 text-right text-[#888]">{fmtBps(tradeRowSlipBps(row, 'entry_slippage_bps'))}</td>
												<td class="px-3 py-2 text-right text-[#888]">{fmtBps(tradeRowSlipBps(row, 'exit_slippage_bps'))}</td>
												<td class="px-3 py-2 text-left text-[#888]">{tradeRowCloseReason(row)}</td>
												<td class="px-3 py-2 text-left text-[#555]">{fmtDate(row.opened_at)}</td>
												<td class="px-3 py-2 text-left text-[#555]">{fmtDate(row.closed_at)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</div>

					<details class="border border-[#222] bg-[#090909]">
						<summary class="cursor-pointer px-3 py-2 text-[10px] uppercase tracking-wide text-[#555]">Risk Slots ({executionPositions.length})</summary>
						{#if executionPositions.length === 0}
							<div class="border-t border-[#1a1a1a] px-3 py-4 text-xs text-[#555]">No live risk-slot reservations.</div>
						{:else}
							<div class="max-h-[280px] overflow-auto border-t border-[#1a1a1a]">
								<table class="min-w-full text-xs">
									<thead class="bg-[#0d0d0d] text-[#555]">
										<tr>
											<th class="px-3 py-2 text-left">Trade</th>
											<th class="px-3 py-2 text-left">Asset</th>
											<th class="px-3 py-2 text-left">Side</th>
											<th class="px-3 py-2 text-right">Risk %</th>
											<th class="px-3 py-2 text-right">Entry</th>
											<th class="px-3 py-2 text-left">Type</th>
											<th class="px-3 py-2 text-left">Book</th>
											<th class="px-3 py-2 text-left">Opened</th>
										</tr>
									</thead>
									<tbody>
										{#each executionPositions as row, index}
											<tr class="border-t border-[#111] font-mono">
												<td class="px-3 py-2 text-white">{getString(row, 'trade_id', getRowId(row, `pos-${index}`))}</td>
												<td class="px-3 py-2 text-[#aaa]">{getString(row, 'asset')}</td>
												<td class="px-3 py-2 text-[#aaa]">{getString(row, 'direction')}</td>
												<td class="px-3 py-2 text-right text-[#888]">{asNumber(row.risk_pct, 0) ? `${(asNumber(row.risk_pct, 0) * 100).toFixed(2)}%` : '-'}</td>
												<td class="px-3 py-2 text-right text-[#888]">{asNumber(row.entry_price, 0) ? asNumber(row.entry_price, 0).toFixed(4) : '-'}</td>
												<td class="px-3 py-2 text-left text-[#888]">{getString(row, 'execution_type')}</td>
												<td class="px-3 py-2 text-left text-[#888]">{getString(row, 'book')}</td>
												<td class="px-3 py-2 text-left text-[#555]">{fmtDate(row.opened_at)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</details>
				</div>
			{/if}
		</div>
	{:else}
		<!-- Defensive: container is null but not loading/errored (e.g. a future early-return
		     path). Degrade to an empty state rather than rendering a blank pane. -->
		<div class="flex-1 flex items-center justify-center">
			<div class="border border-[#2b2b2b] bg-[#0a0a0a] px-4 py-3 text-sm text-[#888]">
				Strategy not found.
				<a href={returnTo} class="ml-1 text-white underline hover:text-white">Go back</a>
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

{#if showImportDialog}
	<StrategyImportDialog
		on:close={() => (showImportDialog = false)}
		on:imported={(e) => onStrategyImported(e.detail)}
	/>
{/if}
