<script lang="ts">
	import { get } from 'svelte/store';
	import { createEventDispatcher, onMount } from 'svelte';
	import {
		getResult,
		getRobustnessResult,
		submitCostStressRobustness,
		submitMonteCarloRobustness,
		submitParamJitterRobustness,
		submitRegimeSplitRobustness,
		submitWalkForwardRobustness,
		type CostStressRobustnessResult,
		type CostStressSnapshot,
		type MonteCarloRobustnessResult,
		type ParamJitterRobustnessResult,
		type PersistedRobustnessResult,
		type RegimeSplitEntry,
		type RegimeSplitRobustnessResult,
		type RobustnessSubmitResponse,
		type WalkForwardRobustnessResult,
	} from '$lib/api/backtesting';
	import { getJob, type BacktestResult, type Job, type StrategyContainerHistoryItem } from '$lib/api';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import NumericInputField from '$lib/components/ui/NumericInputField.svelte';
	import ResultPicker from '$lib/components/ui/ResultPicker.svelte';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import MonteCarloChart from '$lib/components/simulation/MonteCarloChart.svelte';
	import DistributionChart from '$lib/components/charts/DistributionChart.svelte';
	import CostStressComparisonChart from '$lib/components/robustness/CostStressComparisonChart.svelte';
	import RegimePnlChart from '$lib/components/robustness/RegimePnlChart.svelte';
	import { resolveDateRangePreset } from '$lib/utils/dateRange';
	import {
		addToast,
		trackProcess,
		trackedProcesses,
		type TrackedProcess,
	} from '$lib/stores/processTracker';

	type SuiteTestKey = 'walk_forward' | 'monte_carlo' | 'param_jitter' | 'cost_stress' | 'regime_split';
	type TestCompleteEvent = {
		key: SuiteTestKey;
		result_id: string;
		status: string;
		verdict: string | null;
		error?: string | null;
		completed_at?: string | null;
	};

	export let strategyId: string;
	export let backtestHistory: StrategyContainerHistoryItem[] = [];
	export let validationHistory: StrategyContainerHistoryItem[] = [];
	export let symbolSuggestions: string[] = [];
	export let defaultSymbol: string = '';
	export let defaultTimeframe: string = '1h';
	export let activeTestKey: SuiteTestKey = 'walk_forward';
	// The strategy's ACTIVE container backtest (operator-pinned). Robustness runs
	// default to this rather than the most-recent backtest.
	export let pinnedBacktestId: string = '';

	const dispatch = createEventDispatcher<{
		testComplete: TestCompleteEvent;
	}>();

	type SubmitOutcome = {
		key: SuiteTestKey;
		queued: boolean;
		skipped: boolean;
		error?: string;
	};
	type SubmitOptions = {
		quiet?: boolean;
	};
	type PersistedPayload = PersistedRobustnessResult<Record<string, unknown>>;

	const defaultRange = resolveDateRangePreset('1y');
	const suiteKeys: SuiteTestKey[] = ['walk_forward', 'monte_carlo', 'param_jitter', 'cost_stress', 'regime_split'];
	const runningStatuses = new Set(['running', 'queued', 'pending']);
	const terminalStatuses = new Set(['succeeded', 'failed', 'cancelled']);

	// ── Accordion state ──
	let mounted = false;
	let destroyed = false;
	let unsubscribeTrackedProcesses: (() => void) | null = null;
	let lastHistorySignature = '';

	// All five runners render as accordions; only the selected test starts expanded
	// so the suite reads as one scannable list instead of five open panels.
	let expandedSections: Record<SuiteTestKey, boolean> = {
		walk_forward: activeTestKey === 'walk_forward',
		monte_carlo: activeTestKey === 'monte_carlo',
		param_jitter: activeTestKey === 'param_jitter',
		cost_stress: activeTestKey === 'cost_stress',
		regime_split: activeTestKey === 'regime_split',
	};

	function toggleSection(key: SuiteTestKey) {
		expandedSections = { ...expandedSections, [key]: !expandedSections[key] };
	}

	$: if (!expandedSections[activeTestKey]) {
		expandedSections = { ...expandedSections, [activeTestKey]: true };
	}

	// ── Form state ──
	// Window left empty so the reactive below can seed it from the active container
	// backtest (preferred) or fall back to the 1y preset.
	let walkForwardForm = {
		symbol: defaultSymbol,
		timeframe: defaultTimeframe || '1h',
		n_splits: 5,
		train_ratio: 0.7,
		start_date: '',
		end_date: '',
	};

	let monteCarloForm = {
		result_id: '',
		n_simulations: 1000,
		initial_capital: 10000,
	};

	let paramJitterForm = {
		result_id: '',
		jitter_pct: 10,
		n_iterations: 30,
	};

	let costStressForm = {
		symbol: defaultSymbol,
		timeframe: defaultTimeframe || '1h',
		start_date: '',
		end_date: '',
		fee_multiplier: 2.0,
		slippage_multiplier: 2.0,
	};

	let regimeSplitForm = {
		result_id: '',
	};

	// ── Loading / results ──
	let loading: Record<SuiteTestKey, boolean> = {
		walk_forward: false,
		monte_carlo: false,
		param_jitter: false,
		cost_stress: false,
		regime_split: false,
	};
	let errors: Record<SuiteTestKey, string> = {
		walk_forward: '',
		monte_carlo: '',
		param_jitter: '',
		cost_stress: '',
		regime_split: '',
	};
	let skippedPrerequisites: Record<SuiteTestKey, boolean> = {
		walk_forward: false,
		monte_carlo: false,
		param_jitter: false,
		cost_stress: false,
		regime_split: false,
	};

	let walkForwardResult: WalkForwardRobustnessResult | null = null;
	let monteCarloResult: MonteCarloRobustnessResult | null = null;
	let paramJitterResult: ParamJitterRobustnessResult | null = null;
	let costStressResult: CostStressRobustnessResult | null = null;
	let regimeSplitResult: RegimeSplitRobustnessResult | null = null;

	let suiteRunning = false;
	let activeJobIds: Partial<Record<SuiteTestKey, string>> = {};
	let activeResultIds: Partial<Record<SuiteTestKey, string>> = {};
	let hydratedResultIds: Partial<Record<SuiteTestKey, string>> = {};
	let hydratingResultIds: Partial<Record<SuiteTestKey, string>> = {};
	let pollingJobIds: Partial<Record<SuiteTestKey, string>> = {};
	let emittedCompletionSignatures: Partial<Record<SuiteTestKey, string>> = {};
	let backtestDetailCache: Record<string, BacktestResult | null> = {};

	// ── Helpers ──
	$: if (!walkForwardForm.symbol.trim() && defaultSymbol.trim()) {
		walkForwardForm = { ...walkForwardForm, symbol: defaultSymbol.trim() };
	}
	$: if (!costStressForm.symbol.trim() && defaultSymbol.trim()) {
		costStressForm = { ...costStressForm, symbol: defaultSymbol.trim() };
	}
	$: if (!walkForwardForm.timeframe.trim() && defaultTimeframe.trim()) {
		walkForwardForm = { ...walkForwardForm, timeframe: defaultTimeframe.trim() };
	}
	$: if (!costStressForm.timeframe.trim() && defaultTimeframe.trim()) {
		costStressForm = { ...costStressForm, timeframe: defaultTimeframe.trim() };
	}
	// The "active container" baseline: the operator-pinned backtest if present,
	// otherwise the most-recent. ALL robustness runs default to this so they
	// validate the configuration the operator chose — not whatever ran last.
	$: pinnedBaselineItem = pinnedBacktestId.trim()
		? (backtestHistory.find((item) => item.result_id === pinnedBacktestId.trim()) ?? null)
		: null;
	$: activeBaseline = pinnedBaselineItem ?? backtestHistory[0] ?? null;
	$: if (!monteCarloForm.result_id && activeBaseline?.result_id) {
		monteCarloForm = { ...monteCarloForm, result_id: activeBaseline.result_id };
	}
	$: if (!paramJitterForm.result_id && activeBaseline?.result_id) {
		paramJitterForm = { ...paramJitterForm, result_id: activeBaseline.result_id };
	}
	$: if (!regimeSplitForm.result_id && activeBaseline?.result_id) {
		regimeSplitForm = { ...regimeSplitForm, result_id: activeBaseline.result_id };
	}
	// Window-based tests (walk-forward, cost-stress) don't take a result_id — they
	// re-run over a symbol/timeframe/window. Seed that window from the active
	// container's backtest (preferred) so they match the pinned config, falling
	// back to the 1y preset. Only fills empties; the operator can still override.
	$: baselineWindowStart = activeBaseline?.start_date || defaultRange.startDate;
	$: baselineWindowEnd = activeBaseline?.end_date || defaultRange.endDate;
	$: if (!walkForwardForm.start_date && baselineWindowStart) {
		walkForwardForm = {
			...walkForwardForm,
			start_date: baselineWindowStart,
			end_date: baselineWindowEnd,
		};
	}
	$: if (!costStressForm.start_date && baselineWindowStart) {
		costStressForm = {
			...costStressForm,
			start_date: baselineWindowStart,
			end_date: baselineWindowEnd,
		};
	}
	$: anyLoading = suiteRunning || Object.values(loading).some(Boolean);
	// Reactive verdict map — ensures the scorecard re-renders when result
	// variables change.  Using {@const verdictFor(key)} inside {#each} over a
	// static array doesn't re-evaluate because Svelte can't trace the
	// dependency through the function call.
	$: scorecardVerdicts = {
		walk_forward: walkForwardResult?.verdict ?? null,
		monte_carlo: monteCarloResult?.verdict ?? null,
		param_jitter: paramJitterResult?.verdict ?? null,
		cost_stress: costStressResult?.verdict ?? null,
		regime_split: regimeSplitResult?.verdict ?? null,
	} as Record<SuiteTestKey, string | null>;
	$: emitResultCompletion('walk_forward', walkForwardResult);
	$: emitResultCompletion('monte_carlo', monteCarloResult);
	$: emitResultCompletion('param_jitter', paramJitterResult);
	$: emitResultCompletion('cost_stress', costStressResult);
	$: emitResultCompletion('regime_split', regimeSplitResult);
	$: if (mounted) {
		const nextHistorySignature = buildHistorySignature(validationHistory);
		if (nextHistorySignature !== lastHistorySignature) {
			lastHistorySignature = nextHistorySignature;
			void rehydrateFromValidationHistory();
		}
	}

	onMount(() => {
		mounted = true;
		destroyed = false;
		lastHistorySignature = buildHistorySignature(validationHistory);
		syncTrackedProcesses(get(trackedProcesses));
		unsubscribeTrackedProcesses = trackedProcesses.subscribe((processes) => {
			syncTrackedProcesses(processes);
		});
		void rehydrateFromValidationHistory();
		return () => {
			destroyed = true;
			mounted = false;
			unsubscribeTrackedProcesses?.();
			unsubscribeTrackedProcesses = null;
		};
	});

	function strategyHref(): string {
		return `/lab/strategy/${encodeURIComponent(strategyId)}`;
	}

	function normalizeStatus(raw: unknown): string {
		const value = String(raw ?? '').trim().toLowerCase();
		if (runningStatuses.has(value) || terminalStatuses.has(value)) {
			return value;
		}
		if (value === 'done' || value === 'completed' || value === 'complete' || value === 'success') {
			return 'succeeded';
		}
		if (value === 'error' || value === 'errored' || value === 'failed_permanent') {
			return 'failed';
		}
		return 'pending';
	}

	function normalizeSuiteKey(raw: unknown): SuiteTestKey | null {
		const value = String(raw ?? '').trim().toLowerCase();
		return suiteKeys.includes(value as SuiteTestKey) ? (value as SuiteTestKey) : null;
	}

	function parseTimestamp(value: unknown): number {
		if (typeof value !== 'string' || !value.trim()) {
			return 0;
		}
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? parsed : 0;
	}

	function setLoading(key: SuiteTestKey, value: boolean) {
		if (loading[key] === value) return;
		loading = { ...loading, [key]: value };
	}

	function setError(key: SuiteTestKey, value: string | null | undefined) {
		const nextValue = String(value ?? '').trim();
		if (errors[key] === nextValue) return;
		errors = { ...errors, [key]: nextValue };
		if (!nextValue && skippedPrerequisites[key]) {
			skippedPrerequisites = { ...skippedPrerequisites, [key]: false };
		}
	}

	function tradeArtifactPrerequisiteMessage(key: SuiteTestKey): string {
		const label = key === 'regime_split' ? 'Regime split' : key === 'monte_carlo' ? 'Monte Carlo' : jobLabel(key);
		return `${label} needs trade-level artifacts on the selected baseline. Run a fresh baseline backtest for this strategy/window, then rerun ${label}.`;
	}

	function isTradeArtifactPrerequisiteMessage(message: string): boolean {
		const normalized = message.toLowerCase();
		return normalized.includes('persisted trade rows or trade artifacts') || normalized.includes('needs trade-level artifacts');
	}

	function setFailureMessage(key: SuiteTestKey, message: string | null | undefined, skipped = false) {
		const raw = String(message ?? '').trim();
		const isTradeArtifactPrereq = isTradeArtifactPrerequisiteMessage(raw);
		setError(key, isTradeArtifactPrereq ? tradeArtifactPrerequisiteMessage(key) : raw);
		skippedPrerequisites = { ...skippedPrerequisites, [key]: skipped || isTradeArtifactPrereq };
	}

	function setActiveJobId(key: SuiteTestKey, value: string | undefined) {
		const next = { ...activeJobIds };
		if (value?.trim()) {
			next[key] = value.trim();
		} else {
			delete next[key];
		}
		activeJobIds = next;
	}

	function setActiveResultId(key: SuiteTestKey, value: string | undefined) {
		const next = { ...activeResultIds };
		if (value?.trim()) {
			next[key] = value.trim();
		} else {
			delete next[key];
		}
		activeResultIds = next;
	}

	function setHydratedResultId(key: SuiteTestKey, value: string | undefined) {
		const next = { ...hydratedResultIds };
		if (value?.trim()) {
			next[key] = value.trim();
		} else {
			delete next[key];
		}
		hydratedResultIds = next;
	}

	function setHydratingResultId(key: SuiteTestKey, value: string | undefined) {
		const next = { ...hydratingResultIds };
		if (value?.trim()) {
			next[key] = value.trim();
		} else {
			delete next[key];
		}
		hydratingResultIds = next;
	}

	function setPollingJobId(key: SuiteTestKey, value: string | undefined) {
		const next = { ...pollingJobIds };
		if (value?.trim()) {
			next[key] = value.trim();
		} else {
			delete next[key];
		}
		pollingJobIds = next;
	}

	function getSelectedBacktest(resultId: string): StrategyContainerHistoryItem | undefined {
		return backtestHistory.find((item) => item.result_id === resultId);
	}

	async function loadBacktestDetail(resultId: string): Promise<BacktestResult | null> {
		const normalized = resultId.trim();
		if (!normalized) return null;
		if (Object.prototype.hasOwnProperty.call(backtestDetailCache, normalized)) {
			return backtestDetailCache[normalized];
		}
		try {
			const detail = await getResult(normalized);
			backtestDetailCache = { ...backtestDetailCache, [normalized]: detail };
			return detail;
		} catch {
			backtestDetailCache = { ...backtestDetailCache, [normalized]: null };
			return null;
		}
	}

	async function ensureTradeArtifactBaseline(
		key: 'monte_carlo' | 'regime_split',
		resultId: string,
		options: SubmitOptions = {}
	): Promise<SubmitOutcome | null> {
		const detail = await loadBacktestDetail(resultId);
		if (!detail) return null;
		if (Array.isArray(detail.trades) && detail.trades.length > 0) return null;
		const label = key === 'monte_carlo' ? 'Monte Carlo' : 'Regime split';
		const message = `${label} needs trade-level artifacts on the selected baseline. Run a fresh baseline backtest for this strategy/window, then rerun ${label}.`;
		return handleSubmitFailure(key, message, options, true);
	}

	function jobLabel(key: SuiteTestKey): string {
		switch (key) {
			case 'walk_forward':
				return 'Walk-Forward';
			case 'monte_carlo':
				return 'Monte Carlo';
			case 'param_jitter':
				return 'Param Jitter';
			case 'cost_stress':
				return 'Cost Stress';
			case 'regime_split':
				return 'Regime Split';
		}
	}

	function verdictBadge(verdict: string | null): string {
		if (!verdict) return 'border-[#333] text-[#888]';
		return verdict === 'PASS'
			? 'border-emerald-900 bg-emerald-500/10 text-emerald-400'
			: 'border-red-900 bg-red-500/10 text-red-400';
	}

	function verdictLabel(verdict: string | null): string {
		if (!verdict) return 'NOT RUN';
		return verdict;
	}

	function methodLabel(value: unknown): string {
		const raw = typeof value === 'string' ? value.trim() : '';
		if (!raw) return '';
		return raw.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/\b\w/g, (ch) => ch.toUpperCase());
	}

	function toIsoDate(value: string): string | undefined {
		const normalized = value.trim();
		if (!normalized) return undefined;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return undefined;
		return parsed.toISOString();
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

	function buildHistorySignature(items: StrategyContainerHistoryItem[]): string {
		return items
			.filter((item) => !item.deleted_at && normalizeSuiteKey(item.result_type))
			.map((item) => {
				const status = historyItemStatus(item);
				const jobId = historyItemJobId(item) ?? '';
				return `${item.result_id}:${item.result_type}:${item.created_at}:${status}:${jobId}`;
			})
			.sort()
			.join('|');
	}

	function historyItemStatus(item: StrategyContainerHistoryItem): string {
		return normalizeStatus(item.config.status ?? item.metrics.status ?? 'pending');
	}

	function historyItemJobId(item: StrategyContainerHistoryItem): string | undefined {
		const rawValue = item.config.job_id ?? item.metrics.job_id;
		const jobId = typeof rawValue === 'string' ? rawValue.trim() : '';
		return jobId || undefined;
	}

	function historyItemError(item: StrategyContainerHistoryItem): string | undefined {
		const rawValue = item.config.error ?? item.metrics.error;
		const error = typeof rawValue === 'string' ? rawValue.trim() : '';
		return error || undefined;
	}

	function historyItemTimestamp(item: StrategyContainerHistoryItem): number {
		const completedAt = typeof item.config.completed_at === 'string' ? item.config.completed_at : '';
		return Math.max(parseTimestamp(completedAt), parseTimestamp(item.created_at));
	}

	function latestValidationRows(): {
		latestByKey: Partial<Record<SuiteTestKey, StrategyContainerHistoryItem>>;
		latestSucceededByKey: Partial<Record<SuiteTestKey, StrategyContainerHistoryItem>>;
	} {
		const latestByKey: Partial<Record<SuiteTestKey, StrategyContainerHistoryItem>> = {};
		const latestSucceededByKey: Partial<Record<SuiteTestKey, StrategyContainerHistoryItem>> = {};

		for (const item of validationHistory) {
			if (item.deleted_at) continue;
			const key = normalizeSuiteKey(item.result_type);
			if (!key) continue;
			const currentLatest = latestByKey[key];
			if (!currentLatest || historyItemTimestamp(item) >= historyItemTimestamp(currentLatest)) {
				latestByKey[key] = item;
			}
			if (historyItemStatus(item) === 'succeeded') {
				const currentSucceeded = latestSucceededByKey[key];
				if (!currentSucceeded || historyItemTimestamp(item) >= historyItemTimestamp(currentSucceeded)) {
					latestSucceededByKey[key] = item;
				}
			}
		}

		return { latestByKey, latestSucceededByKey };
	}

	function historyItemToJob(key: SuiteTestKey, item: StrategyContainerHistoryItem): Job | null {
		const jobId = historyItemJobId(item);
		if (!jobId) return null;
		const completedAt = typeof item.config.completed_at === 'string' ? item.config.completed_at.trim() : '';
		return {
			id: jobId,
			type: key,
			status: historyItemStatus(item),
			created_at: item.created_at,
			updated_at: completedAt || item.created_at,
			error: historyItemError(item),
			result_id: item.result_id,
			strategy_id: item.strategy_id,
			symbol: item.symbol,
			timeframe: item.timeframe,
		};
	}

	function trackRobustnessJob(key: SuiteTestKey, job: Job) {
		trackProcess(job.id, 'job', jobLabel(key), strategyHref(), job);
	}

	function dispatchTestComplete(
		key: SuiteTestKey,
		resultId: string,
		status: string,
		verdict: string | null,
		error: string | null = null,
		completedAt: string | null = null,
	) {
		const normalizedResultId = resultId.trim();
		if (!normalizedResultId) return;
		const normalizedVerdict = verdict == null ? null : verdict.trim().toUpperCase() || null;
		const signature = `${normalizedResultId}:${status}:${normalizedVerdict ?? ''}:${error ?? ''}`;
		if (emittedCompletionSignatures[key] === signature) return;
		emittedCompletionSignatures = { ...emittedCompletionSignatures, [key]: signature };
		dispatch('testComplete', {
			key,
			result_id: normalizedResultId,
			status,
			verdict: normalizedVerdict,
			error,
			completed_at: completedAt,
		});
	}

	function emitResultCompletion(key: SuiteTestKey, result: { persisted_result_id?: string; verdict?: unknown } | null) {
		if (!mounted || destroyed || !result) return;
		const resultId = String(result.persisted_result_id ?? activeResultIds[key] ?? '').trim();
		if (!resultId) return;
		const verdict = result.verdict == null ? null : String(result.verdict).trim().toUpperCase() || null;
		dispatchTestComplete(key, resultId, 'succeeded', verdict);
	}

	function setResultForKey(key: SuiteTestKey, payload: Record<string, unknown>, resultId: string, jobId?: string) {
		switch (key) {
			case 'walk_forward':
				walkForwardResult = {
					...(payload as unknown as WalkForwardRobustnessResult),
					persisted_result_id: resultId,
					job_id: jobId,
				};
				break;
			case 'monte_carlo':
				monteCarloResult = {
					...(payload as unknown as MonteCarloRobustnessResult),
					persisted_result_id: resultId,
					job_id: jobId,
				};
				break;
			case 'param_jitter':
				paramJitterResult = {
					...(payload as unknown as ParamJitterRobustnessResult),
					persisted_result_id: resultId,
					job_id: jobId,
				};
				break;
			case 'cost_stress':
				costStressResult = {
					...(payload as unknown as CostStressRobustnessResult),
					persisted_result_id: resultId,
					job_id: jobId,
				};
				break;
			case 'regime_split':
				regimeSplitResult = {
					...(payload as unknown as RegimeSplitRobustnessResult),
					persisted_result_id: resultId,
					job_id: jobId,
				};
				break;
		}
	}

	function applyPersistedResult(key: SuiteTestKey, persisted: PersistedPayload) {
		const jobId = typeof persisted.config.job_id === 'string' ? persisted.config.job_id : undefined;
		const payload =
			persisted.payload && typeof persisted.payload === 'object'
				? persisted.payload
				: (persisted.metrics ?? {});
		// Ensure verdict from metrics is always present in the payload so the
		// scorecard can render it even when the artifact payload is incomplete.
		if (!payload.verdict && persisted.metrics?.verdict) {
			payload.verdict = persisted.metrics.verdict;
		}
		setResultForKey(key, payload, persisted.result_id, jobId);
		setHydratedResultId(key, persisted.result_id);
		setActiveResultId(key, persisted.result_id);
		if (jobId) {
			setActiveJobId(key, jobId);
		}
		const status = normalizeStatus(persisted.status);
		setLoading(key, runningStatuses.has(status));
		if (status === 'failed' || status === 'cancelled') {
			setFailureMessage(key, persisted.error ?? `${jobLabel(key)} failed.`);
		} else {
			setError(key, '');
		}
		if (!runningStatuses.has(status)) {
			const rawVerdict = payload.verdict ?? persisted.metrics?.verdict;
			const verdict = rawVerdict == null ? null : String(rawVerdict).trim().toUpperCase() || null;
			const completedAt = typeof persisted.config.completed_at === 'string' ? persisted.config.completed_at : null;
			dispatchTestComplete(key, persisted.result_id, status, verdict, persisted.error ?? null, completedAt);
		}
	}

	async function hydratePersistedResult(key: SuiteTestKey, resultId: string): Promise<void> {
		const normalizedResultId = resultId.trim();
		if (!normalizedResultId) return;
		if (hydratedResultIds[key] === normalizedResultId || hydratingResultIds[key] === normalizedResultId) return;

		setHydratingResultId(key, normalizedResultId);
		try {
			const persisted = await getRobustnessResult<Record<string, unknown>>(normalizedResultId);
			if (destroyed) return;
			applyPersistedResult(key, persisted);
		} catch (error) {
			if (!destroyed) {
				const message = error instanceof Error ? error.message : `Failed to load ${jobLabel(key)} result.`;
				setError(key, message);
			}
		} finally {
			if (hydratingResultIds[key] === normalizedResultId) {
				setHydratingResultId(key, undefined);
			}
		}
	}

	async function tryGetPersistedResult(resultId: string | undefined): Promise<PersistedPayload | null> {
		const normalizedResultId = String(resultId ?? '').trim();
		if (!normalizedResultId) return null;
		try {
			return await getRobustnessResult<Record<string, unknown>>(normalizedResultId);
		} catch {
			return null;
		}
	}

	function isTrackedRobustnessProcess(proc: TrackedProcess): boolean {
		if (proc.type !== 'job') return false;
		const job = proc.data as Job;
		const key = normalizeSuiteKey(job.type);
		if (!key) return false;
		const procStrategyId = typeof job.strategy_id === 'string' ? job.strategy_id.trim() : '';
		return procStrategyId === strategyId || proc.href === strategyHref();
	}

	function jobTimestamp(job: Job): number {
		return Math.max(parseTimestamp(job.updated_at), parseTimestamp(job.created_at));
	}

	function syncTrackedProcesses(processes: TrackedProcess[]) {
		if (!mounted || destroyed) return;
		const latestByKey: Partial<Record<SuiteTestKey, Job>> = {};

		for (const proc of processes) {
			if (!isTrackedRobustnessProcess(proc)) continue;
			const job = proc.data as Job;
			const key = normalizeSuiteKey(job.type);
			if (!key) continue;
			const current = latestByKey[key];
			if (!current || jobTimestamp(job) >= jobTimestamp(current)) {
				latestByKey[key] = job;
			}
		}

		for (const key of suiteKeys) {
			const job = latestByKey[key];
			if (job) {
				handleTrackedJob(key, job);
			}
		}
	}

	function handleTrackedJob(key: SuiteTestKey, job: Job) {
		const status = normalizeStatus(job.status);
		const resultId = typeof job.result_id === 'string' ? job.result_id.trim() : '';
		setActiveJobId(key, job.id);
		if (resultId) {
			setActiveResultId(key, resultId);
		}

		if (runningStatuses.has(status)) {
			setLoading(key, true);
			if (job.error) {
				setError(key, job.error);
			}
			void pollJobForKey(key, job.id);
			return;
		}

		if (status === 'succeeded') {
			setLoading(key, false);
			setError(key, '');
			void hydratePersistedResult(key, resultId || activeResultIds[key] || '');
			return;
		}

		if (status === 'failed' || status === 'cancelled') {
			setLoading(key, false);
			setFailureMessage(key, job.error ?? `${jobLabel(key)} ${status}.`);
		}
	}

	function sleep(ms: number): Promise<void> {
		return new Promise((resolve) => setTimeout(resolve, ms));
	}

	async function pollJobForKey(key: SuiteTestKey, jobId: string): Promise<void> {
		const normalizedJobId = jobId.trim();
		if (!normalizedJobId || pollingJobIds[key] === normalizedJobId || destroyed) {
			return;
		}

		setPollingJobId(key, normalizedJobId);
		setActiveJobId(key, normalizedJobId);
		try {
			while (!destroyed && activeJobIds[key] === normalizedJobId) {
				let terminal = false;
				try {
					const fetchedJob = await getJob(normalizedJobId);
					if (destroyed) return;

					const jobType = normalizeSuiteKey(fetchedJob.type) ?? key;
					const enrichedJob: Job = {
						...fetchedJob,
						type: jobType,
						strategy_id: fetchedJob.strategy_id ?? strategyId,
						result_id: fetchedJob.result_id ?? activeResultIds[key],
						symbol: fetchedJob.symbol ?? walkForwardForm.symbol ?? costStressForm.symbol ?? defaultSymbol,
						timeframe: fetchedJob.timeframe ?? walkForwardForm.timeframe ?? costStressForm.timeframe ?? defaultTimeframe,
					};
					trackRobustnessJob(key, enrichedJob);

					const status = normalizeStatus(enrichedJob.status);
					const resultId = typeof enrichedJob.result_id === 'string' ? enrichedJob.result_id.trim() : '';
					if (resultId) {
						setActiveResultId(key, resultId);
					}

					if (runningStatuses.has(status)) {
						setLoading(key, true);
						if (enrichedJob.error) {
							setError(key, enrichedJob.error);
						}
					} else if (status === 'succeeded') {
						setLoading(key, false);
						setError(key, '');
						const resolvedResultId = resultId || activeResultIds[key] || '';
						if (resolvedResultId) {
							await hydratePersistedResult(key, resolvedResultId);
						}
						terminal = true;
				} else {
					setLoading(key, false);
					setFailureMessage(key, enrichedJob.error ?? `${jobLabel(key)} ${status}.`);
					terminal = true;
				}
				} catch {
					const fallbackResult = await tryGetPersistedResult(activeResultIds[key]);
					if (destroyed) return;
					if (fallbackResult) {
						const status = normalizeStatus(fallbackResult.status);
						if (runningStatuses.has(status)) {
							setLoading(key, true);
						} else {
							applyPersistedResult(key, fallbackResult);
							terminal = true;
						}
					}
				}

				if (terminal) {
					break;
				}
				await sleep(2000);
			}
		} finally {
			if (pollingJobIds[key] === normalizedJobId) {
				setPollingJobId(key, undefined);
			}
		}
	}

	async function rehydrateFromValidationHistory(): Promise<void> {
		if (!mounted || destroyed) return;
		const { latestByKey, latestSucceededByKey } = latestValidationRows();

		for (const key of suiteKeys) {
			const latest = latestByKey[key];
			const latestSucceeded = latestSucceededByKey[key];

			if (latest) {
				const latestStatus = historyItemStatus(latest);
				const latestJobId = historyItemJobId(latest);
				setActiveResultId(key, latest.result_id);

				// A persisted history row can only be treated as "still running"
				// if it has a job_id we can poll. Rows without a job_id are
				// either orphaned fixtures or legacy writes — treating them as
				// running permanently locks the RUN button because no terminal
				// status will ever arrive.
				if (runningStatuses.has(latestStatus) && latestJobId) {
					setLoading(key, true);
					setError(key, '');
					setActiveJobId(key, latestJobId);
					const syntheticJob = historyItemToJob(key, latest);
					if (syntheticJob) {
						trackRobustnessJob(key, syntheticJob);
					}
					void pollJobForKey(key, latestJobId);
				} else if (latestStatus === 'failed' || latestStatus === 'cancelled') {
					setLoading(key, false);
					setFailureMessage(key, historyItemError(latest) ?? `Latest ${jobLabel(key)} run failed.`);
				}
			}

			const resultToHydrate =
				latest && historyItemStatus(latest) === 'succeeded'
					? latest.result_id
					: latestSucceeded?.result_id;
			if (resultToHydrate) {
				void hydratePersistedResult(key, resultToHydrate);
			}
		}
	}

	function validateWalkForwardForm(): string | null {
		if (!walkForwardForm.symbol.trim()) return 'Symbol is required.';
		if (!walkForwardForm.timeframe.trim()) return 'Timeframe is required.';
		if (!Number.isFinite(Number(walkForwardForm.n_splits)) || Number(walkForwardForm.n_splits) < 2) {
			return 'Splits must be at least 2.';
		}
		const trainRatio = Number(walkForwardForm.train_ratio);
		if (!Number.isFinite(trainRatio) || trainRatio <= 0 || trainRatio >= 1) {
			return 'Train ratio must be between 0 and 1.';
		}
		return validateDateRange(walkForwardForm.start_date, walkForwardForm.end_date);
	}

	function validateCostStressForm(): string | null {
		if (!costStressForm.symbol.trim()) return 'Symbol is required.';
		if (!costStressForm.timeframe.trim()) return 'Timeframe is required.';
		if (!Number.isFinite(Number(costStressForm.fee_multiplier)) || Number(costStressForm.fee_multiplier) < 1) {
			return 'Fee multiplier must be at least 1.';
		}
		if (!Number.isFinite(Number(costStressForm.slippage_multiplier)) || Number(costStressForm.slippage_multiplier) < 1) {
			return 'Slippage multiplier must be at least 1.';
		}
		return validateDateRange(costStressForm.start_date, costStressForm.end_date);
	}

	function resolveBaselineResult(
		key: 'monte_carlo' | 'param_jitter' | 'regime_split',
		currentResultId: string
	): { resultId?: string; skipped: boolean; error?: string } {
		const normalized = currentResultId.trim();
		if (normalized) {
			return { resultId: normalized, skipped: false };
		}
		const fallback = backtestHistory[0]?.result_id?.trim();
		if (fallback) {
			if (key === 'monte_carlo') {
				monteCarloForm = { ...monteCarloForm, result_id: fallback };
			} else if (key === 'param_jitter') {
				paramJitterForm = { ...paramJitterForm, result_id: fallback };
			} else {
				regimeSplitForm = { ...regimeSplitForm, result_id: fallback };
			}
			return { resultId: fallback, skipped: false };
		}
		return { skipped: true, error: 'Run a baseline backtest first.' };
	}

	function registerSubmission(
		key: SuiteTestKey,
		response: RobustnessSubmitResponse,
		jobSeed: Partial<Job>,
		options: SubmitOptions = {}
	): SubmitOutcome {
		const now = new Date().toISOString();
		const job: Job = {
			id: response.job_id,
			type: key,
			status: normalizeStatus(response.status),
			created_at: now,
			updated_at: now,
			result_id: response.result_id,
			strategy_id: strategyId,
			symbol: jobSeed.symbol,
			timeframe: jobSeed.timeframe,
		};

		setActiveJobId(key, response.job_id);
		setActiveResultId(key, response.result_id);
		setLoading(key, true);
		setError(key, '');
		expandedSections = { ...expandedSections, [key]: true };
		trackRobustnessJob(key, job);
		void pollJobForKey(key, response.job_id);

		if (!options.quiet) {
			addToast(`${jobLabel(key)} queued`, 'info', strategyHref());
		}

		return { key, queued: true, skipped: false };
	}

	function handleSubmitFailure(
		key: SuiteTestKey,
		message: string,
		options: SubmitOptions = {},
		skipped = false
	): SubmitOutcome {
		setLoading(key, false);
		setFailureMessage(key, message, skipped);
		if (!options.quiet) {
			addToast(message, skipped ? 'info' : 'error', strategyHref());
		}
		return { key, queued: false, skipped, error: message };
	}

	function errorMessageClass(key: SuiteTestKey): string {
		return skippedPrerequisites[key]
			? 'mt-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400'
			: 'mt-3 border border-red-900 bg-red-500/5 px-3 py-2 text-xs text-red-400';
	}

	// ── Scorecard data ──
	const suiteTests: Array<{ key: SuiteTestKey; label: string; accent: string }> = [
		{ key: 'walk_forward', label: 'Walk-Forward', accent: 'violet' },
		{ key: 'monte_carlo', label: 'Monte Carlo', accent: 'amber' },
		{ key: 'param_jitter', label: 'Param Jitter', accent: 'orange' },
		{ key: 'cost_stress', label: 'Cost Stress', accent: 'rose' },
		{ key: 'regime_split', label: 'Regime Split', accent: 'teal' },
	];

	// Reactive metric map — must be a $: binding (not a function call inside
	// {#each}) so Svelte can track the dependency on each result variable.
	$: scorecardMetrics = {
		walk_forward: walkForwardResult ? `${(Number(walkForwardResult.degradation ?? 0) * 100).toFixed(0)}% deg · IS ${Number(walkForwardResult.avg_is_sharpe ?? 0).toFixed(2)} → OOS ${Number(walkForwardResult.avg_oos_sharpe ?? 0).toFixed(2)}` : '--',
		monte_carlo: monteCarloResult ? `${monteCarloResult.prob_profitable ?? 0}% profitable · ${monteCarloResult.n_simulations ?? 0} sims` : '--',
		param_jitter: paramJitterResult ? `${paramJitterResult.pct_positive_sharpe ?? 0}% +sharpe · μ ${Number(paramJitterResult.mean_sharpe ?? 0).toFixed(2)} ± ${Number(paramJitterResult.std_sharpe ?? 0).toFixed(2)}` : '--',
		cost_stress: costStressResult ? `${costStressResult.degradation_pct ?? 0}% deg · ${costStressResult.fee_multiplier ?? '?'}× fee ${costStressResult.slippage_multiplier ?? '?'}× slip` : '--',
		regime_split: regimeSplitResult ? `${regimeSplitResult.n_regimes ?? 0} regimes · best: ${regimeSplitResult.dominant_regime ?? '?'} · weak: ${regimeSplitResult.weakest_regime ?? '?'}` : '--',
	} as Record<SuiteTestKey, string>;

	// ── Submit functions ──
	async function submitWalkForward(options: SubmitOptions = {}): Promise<SubmitOutcome> {
		const validationError = validateWalkForwardForm();
		if (validationError) {
			return handleSubmitFailure('walk_forward', validationError, options);
		}

		setLoading('walk_forward', true);
		setError('walk_forward', '');
		try {
			const response = await submitWalkForwardRobustness({
				strategy_id: strategyId,
				symbol: walkForwardForm.symbol.trim(),
				timeframe: walkForwardForm.timeframe.trim(),
				n_splits: Number(walkForwardForm.n_splits),
				train_ratio: Number(walkForwardForm.train_ratio),
				start_date: walkForwardForm.start_date || undefined,
				end_date: walkForwardForm.end_date || undefined,
			});
			return registerSubmission(
				'walk_forward',
				response,
				{ symbol: walkForwardForm.symbol.trim(), timeframe: walkForwardForm.timeframe.trim() },
				options,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Walk-forward submission failed.';
			return handleSubmitFailure('walk_forward', message, options);
		}
	}

	async function submitMonteCarlo(options: SubmitOptions = {}): Promise<SubmitOutcome> {
		const baseline = resolveBaselineResult('monte_carlo', monteCarloForm.result_id);
		if (!baseline.resultId) {
			return handleSubmitFailure('monte_carlo', baseline.error ?? 'Select a backtest result.', options, baseline.skipped);
		}
		const preflight = await ensureTradeArtifactBaseline('monte_carlo', baseline.resultId, options);
		if (preflight) return preflight;

		setLoading('monte_carlo', true);
		setError('monte_carlo', '');
		try {
			const response = await submitMonteCarloRobustness({
				result_id: baseline.resultId,
				n_simulations: Number(monteCarloForm.n_simulations),
				initial_capital: Number(monteCarloForm.initial_capital),
			});
			const selected = getSelectedBacktest(baseline.resultId);
			return registerSubmission(
				'monte_carlo',
				response,
				{ symbol: selected?.symbol, timeframe: selected?.timeframe },
				options,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Monte Carlo submission failed.';
			return handleSubmitFailure('monte_carlo', message, options);
		}
	}

	async function submitParamJitter(options: SubmitOptions = {}): Promise<SubmitOutcome> {
		const baseline = resolveBaselineResult('param_jitter', paramJitterForm.result_id);
		if (!baseline.resultId) {
			return handleSubmitFailure('param_jitter', baseline.error ?? 'Select a backtest result.', options, baseline.skipped);
		}

		setLoading('param_jitter', true);
		setError('param_jitter', '');
		try {
			const response = await submitParamJitterRobustness({
				strategy_id: strategyId,
				result_id: baseline.resultId,
				jitter_pct: Number(paramJitterForm.jitter_pct),
				n_iterations: Number(paramJitterForm.n_iterations),
			});
			const selected = getSelectedBacktest(baseline.resultId);
			return registerSubmission(
				'param_jitter',
				response,
				{ symbol: selected?.symbol, timeframe: selected?.timeframe },
				options,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Param jitter submission failed.';
			return handleSubmitFailure('param_jitter', message, options);
		}
	}

	async function submitCostStress(options: SubmitOptions = {}): Promise<SubmitOutcome> {
		const validationError = validateCostStressForm();
		if (validationError) {
			return handleSubmitFailure('cost_stress', validationError, options);
		}

		setLoading('cost_stress', true);
		setError('cost_stress', '');
		try {
			const response = await submitCostStressRobustness({
				strategy_id: strategyId,
				symbol: costStressForm.symbol.trim(),
				timeframe: costStressForm.timeframe.trim(),
				start_date: costStressForm.start_date || undefined,
				end_date: costStressForm.end_date || undefined,
				fee_multiplier: Number(costStressForm.fee_multiplier),
				slippage_multiplier: Number(costStressForm.slippage_multiplier),
			});
			return registerSubmission(
				'cost_stress',
				response,
				{ symbol: costStressForm.symbol.trim(), timeframe: costStressForm.timeframe.trim() },
				options,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Cost stress submission failed.';
			return handleSubmitFailure('cost_stress', message, options);
		}
	}

	async function submitRegimeSplit(options: SubmitOptions = {}): Promise<SubmitOutcome> {
		const baseline = resolveBaselineResult('regime_split', regimeSplitForm.result_id);
		if (!baseline.resultId) {
			return handleSubmitFailure('regime_split', baseline.error ?? 'Select a backtest result.', options, baseline.skipped);
		}
		const preflight = await ensureTradeArtifactBaseline('regime_split', baseline.resultId, options);
		if (preflight) return preflight;

		setLoading('regime_split', true);
		setError('regime_split', '');
		try {
			const response = await submitRegimeSplitRobustness({
				result_id: baseline.resultId,
			});
			const selected = getSelectedBacktest(baseline.resultId);
			return registerSubmission(
				'regime_split',
				response,
				{ symbol: selected?.symbol, timeframe: selected?.timeframe },
				options,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : 'Regime split submission failed.';
			return handleSubmitFailure('regime_split', message, options);
		}
	}

	async function runFullSuite() {
		suiteRunning = true;
		expandedSections = {
			walk_forward: true,
			monte_carlo: true,
			param_jitter: true,
			cost_stress: true,
			regime_split: true,
		};

		try {
			const outcomes = await Promise.all([
				submitWalkForward({ quiet: true }),
				submitMonteCarlo({ quiet: true }),
				submitParamJitter({ quiet: true }),
				submitCostStress({ quiet: true }),
				submitRegimeSplit({ quiet: true }),
			]);

			const queued = outcomes.filter((outcome) => outcome.queued).length;
			const skipped = outcomes.filter((outcome) => outcome.skipped).length;
			const failed = outcomes.filter((outcome) => !outcome.queued && !outcome.skipped).length;
			const running = queued;
			const message = `Robustness suite: ${queued} queued, ${running} running, ${failed} failed, ${skipped} skipped.`;
			const toastType =
				queued > 0 && failed === 0 && skipped === 0
					? 'success'
					: queued === 0
						? 'error'
						: 'info';
			addToast(message, toastType, strategyHref());
		} finally {
			suiteRunning = false;
		}
	}

	function handleWalkForwardClick() {
		void submitWalkForward();
	}

	function handleMonteCarloClick() {
		void submitMonteCarlo();
	}

	function handleParamJitterClick() {
		void submitParamJitter();
	}

	function handleCostStressClick() {
		void submitCostStress();
	}

	function handleRegimeSplitClick() {
		void submitRegimeSplit();
	}

	// ── Canvas chart actions ──
	// The cost-stress and regime PnL canvas charts now live in dedicated components
	// (CostStressComparisonChart / RegimePnlChart) matching the MonteCarloChart pattern.
</script>

<div class="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-[#222] pb-4">
	<div>
		<div class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Robustness Runners</div>
	</div>
	<button
		type="button"
		class="terminal-button-primary shrink-0 text-xs"
		on:click={runFullSuite}
		disabled={anyLoading || suiteRunning}
	>
		{suiteRunning ? 'Running Suite...' : 'Run Full Suite'}
	</button>
</div>

<!-- ════════════════════════════════════════════════════════════════════
     RUNNER ACCORDIONS — all five tests, selected one expanded (gauntlet
     status tiles select + expand; headers toggle)
     ════════════════════════════════════════════════════════════════════ -->

<!-- ──── Walk-Forward Analysis ──── -->
<div class="mb-3 terminal-card overflow-hidden">
	<button
		class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#111]"
		on:click={() => toggleSection('walk_forward')}
	>
		<div class="flex items-center gap-3">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Walk-Forward Analysis</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(scorecardVerdicts['walk_forward'])}`}>{verdictLabel(scorecardVerdicts['walk_forward'])}</span>
			{#if loading.walk_forward}<span class="animate-pulse text-[10px] text-yellow-400">running...</span>{/if}
		</div>
		<span class="text-[#555] text-sm">{expandedSections.walk_forward ? '−' : '+'}</span>
	</button>

	{#if expandedSections.walk_forward}
		<div class="border-t border-[#1a1a1a] px-4 py-4" data-testid="runner-body-walk_forward">
			<div class="grid gap-4 lg:grid-cols-2">
				<SymbolInput id="wf-symbol" label="Symbol" bind:value={walkForwardForm.symbol} suggestions={symbolSuggestions} />
				<TimeframeSelect id="wf-timeframe" label="Timeframe" bind:value={walkForwardForm.timeframe} />
			</div>
			<div class="mt-3">
				<DateRangeFieldset
					idPrefix="wf"
					title="Walk-forward window"
					bind:startDate={walkForwardForm.start_date}
					bind:endDate={walkForwardForm.end_date}
					timeframe={walkForwardForm.timeframe}
					accent="violet"
				/>
			</div>
			<div class="mt-3 grid gap-4 lg:grid-cols-3">
				<NumericInputField id="wf-splits" label="Splits" bind:value={walkForwardForm.n_splits} min="2" max="20" helpText="Number of train/test folds." />
				<NumericInputField id="wf-train-ratio" label="Train ratio" bind:value={walkForwardForm.train_ratio} min="0.1" max="0.95" step="0.05" helpText="Fraction of each fold used for training." />
				<div class="flex items-end">
					<button
						type="button"
						class="terminal-button w-full text-xs"
						on:click={handleWalkForwardClick}
						disabled={loading.walk_forward}
					>
						{loading.walk_forward ? 'Running...' : 'Run WFA'}
					</button>
				</div>
			</div>

			{#if errors.walk_forward}
				<div class={errorMessageClass('walk_forward')}>{errors.walk_forward}</div>
			{/if}

			{#if walkForwardResult}
				<div class="mt-4 border border-[#222] bg-[#050505] p-3">
					<div class="mb-3 flex items-center gap-2">
						<span class="text-[10px] uppercase tracking-wide text-[#666]">Results</span>
						<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(String(walkForwardResult.verdict))}`}>{walkForwardResult.verdict}</span>
					</div>
					{#if walkForwardResult.verdict_reasons?.length}
						<div class="mb-3 border border-red-900 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-400" data-testid="wf-verdict-reasons">
							<div class="text-[10px] font-semibold uppercase tracking-wide">Why it failed</div>
							<ul class="mt-1 list-disc space-y-0.5 pl-4">
								{#each walkForwardResult.verdict_reasons as reason}<li>{reason}</li>{/each}
							</ul>
						</div>
					{/if}
					<div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Avg IS Sharpe</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{Number(walkForwardResult.avg_is_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Avg OOS Sharpe</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{Number(walkForwardResult.avg_oos_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Degradation</div>
							<div class="mt-1 font-mono text-sm {Number(walkForwardResult.degradation || 0) > 0.5 ? 'text-red-400' : 'text-emerald-400'}">{(Number(walkForwardResult.degradation || 0) * 100).toFixed(1)}%</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">OOS Trades</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{walkForwardResult.aggregate_oos?.total_trades ?? walkForwardResult.aggregate_oos?.trades ?? '-'}</div>
						</div>
					</div>
					{#if walkForwardResult.splits?.length > 0}
						<table class="mt-3 w-full text-xs">
							<thead class="bg-[#050505] text-[#666]">
								<tr>
									<th class="px-2 py-1 text-left">Split</th>
									<th class="px-2 py-1 text-right">Bars</th>
									<th class="px-2 py-1 text-right">IS Trades</th>
									<th class="px-2 py-1 text-right">IS Sharpe</th>
									<th class="px-2 py-1 text-right">OOS Trades</th>
									<th class="px-2 py-1 text-right">OOS Sharpe</th>
								</tr>
							</thead>
							<tbody>
								{#each walkForwardResult.splits as split}
									<tr class="border-t border-[#111]">
										<td class="px-2 py-1 font-mono text-[#888]">{split.split}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{split.bars}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{split.in_sample?.trades ?? 0}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{Number(split.in_sample?.sharpe ?? 0).toFixed(2)}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{split.out_of_sample?.trades ?? 0}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{Number(split.out_of_sample?.sharpe ?? 0).toFixed(2)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- ──── Monte Carlo ──── -->
<div class="mb-3 terminal-card overflow-hidden">
	<button
		class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#111]"
		on:click={() => toggleSection('monte_carlo')}
	>
		<div class="flex items-center gap-3">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Monte Carlo Simulation</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(scorecardVerdicts['monte_carlo'])}`}>{verdictLabel(scorecardVerdicts['monte_carlo'])}</span>
			{#if loading.monte_carlo}<span class="animate-pulse text-[10px] text-yellow-400">running...</span>{/if}
		</div>
		<span class="text-[#555] text-sm">{expandedSections.monte_carlo ? '−' : '+'}</span>
	</button>

	{#if expandedSections.monte_carlo}
		<div class="border-t border-[#1a1a1a] px-4 py-4" data-testid="runner-body-monte_carlo">
			<div class="grid gap-4 lg:grid-cols-3">
				<ResultPicker id="mc-result" label="Gauntlet result" bind:value={monteCarloForm.result_id} items={backtestHistory} helpText="Source Gauntlet run for trade bootstrap." />
				<NumericInputField id="mc-sims" label="Simulations" bind:value={monteCarloForm.n_simulations} min="100" max="10000" helpText="Number of equity path simulations." />
				<NumericInputField id="mc-capital" label="Initial capital" bind:value={monteCarloForm.initial_capital} min="1000" step="1000" helpText="Starting equity for simulation." />
			</div>
			<div class="mt-3 flex justify-end">
				<button
					type="button"
					class="terminal-button text-xs"
					on:click={handleMonteCarloClick}
					disabled={loading.monte_carlo || !monteCarloForm.result_id}
				>
					{loading.monte_carlo ? 'Running...' : 'Run Monte Carlo'}
				</button>
			</div>

			{#if errors.monte_carlo}
				<div class={errorMessageClass('monte_carlo')}>{errors.monte_carlo}</div>
			{/if}

			{#if monteCarloResult}
				<div class="mt-4 border border-[#222] bg-[#050505] p-3">
					<div class="mb-3 flex items-center gap-2">
						<span class="text-[10px] uppercase tracking-wide text-[#666]">Results</span>
						<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(String(monteCarloResult.verdict))}`}>{monteCarloResult.verdict}</span>
						{#if monteCarloResult.method}
							<span class="border border-[#333] bg-black px-1.5 py-0.5 text-[10px] text-[#888]">{methodLabel(monteCarloResult.method)}</span>
						{/if}
						<span class="text-[10px] text-[#555]">{monteCarloResult.n_simulations} sims / {monteCarloResult.n_trades} trades</span>
					</div>
					{#if monteCarloResult.verdict_reasons?.length}
						<div class="mb-3 border border-red-900 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-400" data-testid="mc-verdict-reasons">
							<div class="text-[10px] font-semibold uppercase tracking-wide">Why it failed</div>
							<ul class="mt-1 list-disc space-y-0.5 pl-4">
								{#each monteCarloResult.verdict_reasons as reason}<li>{reason}</li>{/each}
							</ul>
						</div>
					{/if}
					<!-- Verdict-relevant stats lead: PASS requires prob_profitable above the policy
					     floor AND the P95 bootstrapped drawdown under the cap. -->
					<div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
						<div class="border border-[#1a1a1a] bg-black px-3 py-2" title={`Share of bootstrapped paths ending profitable. Verdict requires ≥ ${monteCarloResult.verdict_thresholds?.min_prob_profitable ?? '—'}%.`}>
							<div class="text-[10px] text-[#666]">Prob Profitable</div>
							<div class="mt-1 font-mono text-sm text-emerald-400">{monteCarloResult.prob_profitable}%</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2" title={`95th-percentile max drawdown across simulations. Verdict requires ≤ ${monteCarloResult.verdict_thresholds?.max_dd_p95 ?? '—'}%.`}>
							<div class="text-[10px] text-[#666]">P95 Max DD</div>
							<div class="mt-1 font-mono text-sm text-red-400">{monteCarloResult.drawdown_distribution?.p95 ?? '--'}%</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Prob Loss &gt;10%</div>
							<div class="mt-1 font-mono text-sm text-red-400">{monteCarloResult.prob_loss_gt_10}%</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Original Return</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{monteCarloResult.original_return}%</div>
						</div>
					</div>

					{#if monteCarloResult.equity_paths?.length > 0}
						<div class="mt-3 bg-[#111] p-2">
							<div class="mb-1 text-[10px] text-[#666] uppercase">Simulated Equity Curves</div>
							<MonteCarloChart equityPaths={monteCarloResult.equity_paths} height={220} />
						</div>
					{:else}
						<div class="mt-3 border border-[#333] bg-[#050505] px-2.5 py-2 text-[11px] text-[#666]">
							Simulated equity curves are unavailable for this stored result (the full simulation
							artifact was pruned). Re-run Monte Carlo to regenerate them.
						</div>
					{/if}

					{#if monteCarloResult.return_histogram || monteCarloResult.drawdown_histogram || monteCarloResult.sharpe_histogram}
						<div class="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
							{#if monteCarloResult.return_histogram}
								<div class="bg-[#111] p-2 flex justify-center">
									<DistributionChart bins={monteCarloResult.return_histogram.bins} counts={monteCarloResult.return_histogram.counts} title="Return Dist (%)" xLabel="Return %" width={320} height={200} stats={{ mean: monteCarloResult.original_return }} />
								</div>
							{/if}
							{#if monteCarloResult.drawdown_histogram}
								<div class="bg-[#111] p-2 flex justify-center">
									<DistributionChart bins={monteCarloResult.drawdown_histogram.bins} counts={monteCarloResult.drawdown_histogram.counts} title="Max DD Dist (%)" xLabel="Max DD %" width={320} height={200} colorBySign={false} />
								</div>
							{/if}
							{#if monteCarloResult.sharpe_histogram}
								<div class="bg-[#111] p-2 flex justify-center">
									<DistributionChart bins={monteCarloResult.sharpe_histogram.bins} counts={monteCarloResult.sharpe_histogram.counts} title="Sharpe Dist" xLabel="Sharpe" width={320} height={200} stats={{ mean: monteCarloResult.original_sharpe }} />
								</div>
							{/if}
						</div>
					{/if}

					<div class="mt-3 grid grid-cols-1 gap-3 text-xs md:grid-cols-3">
						{#if monteCarloResult.return_distribution}
							<div class="bg-[#111] p-2">
								<div class="mb-1 text-[10px] text-[#666] uppercase">Return Percentiles</div>
								<div class="space-y-0.5 font-mono text-[#888]">
									{#each [['P5', monteCarloResult.return_distribution.p5], ['P25', monteCarloResult.return_distribution.p25], ['P50', monteCarloResult.return_distribution.p50], ['P75', monteCarloResult.return_distribution.p75], ['P95', monteCarloResult.return_distribution.p95]] as [label, val]}
										<div class="flex justify-between {label === 'P50' ? 'text-white' : ''}"><span>{label}</span><span>{val}%</span></div>
									{/each}
								</div>
							</div>
						{/if}
						{#if monteCarloResult.drawdown_distribution}
							<div class="bg-[#111] p-2">
								<div class="mb-1 text-[10px] text-[#666] uppercase">Drawdown Percentiles</div>
								<div class="space-y-0.5 font-mono text-[#888]">
									{#each [['P5', monteCarloResult.drawdown_distribution.p5], ['P25', monteCarloResult.drawdown_distribution.p25], ['P50', monteCarloResult.drawdown_distribution.p50], ['P75', monteCarloResult.drawdown_distribution.p75], ['P95', monteCarloResult.drawdown_distribution.p95]] as [label, val]}
										<div class="flex justify-between {label === 'P50' ? 'text-white' : ''}"><span>{label}</span><span>{val}%</span></div>
									{/each}
								</div>
							</div>
						{/if}
						{#if monteCarloResult.sharpe_distribution}
							<div class="bg-[#111] p-2">
								<div class="mb-1 text-[10px] text-[#666] uppercase">Sharpe Percentiles</div>
								<div class="space-y-0.5 font-mono text-[#888]">
									{#each [['P5', monteCarloResult.sharpe_distribution.p5], ['P25', monteCarloResult.sharpe_distribution.p25], ['P50', monteCarloResult.sharpe_distribution.p50], ['P75', monteCarloResult.sharpe_distribution.p75], ['P95', monteCarloResult.sharpe_distribution.p95]] as [label, val]}
										<div class="flex justify-between {label === 'P50' ? 'text-white' : ''}"><span>{label}</span><span>{val}</span></div>
									{/each}
								</div>
							</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- ──── Param Jitter ──── -->
<div class="mb-3 terminal-card overflow-hidden">
	<button
		class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#111]"
		on:click={() => toggleSection('param_jitter')}
	>
		<div class="flex items-center gap-3">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Parameter Jitter</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(scorecardVerdicts['param_jitter'])}`}>{verdictLabel(scorecardVerdicts['param_jitter'])}</span>
			{#if loading.param_jitter}<span class="animate-pulse text-[10px] text-yellow-400">running...</span>{/if}
		</div>
		<span class="text-[#555] text-sm">{expandedSections.param_jitter ? '−' : '+'}</span>
	</button>

	{#if expandedSections.param_jitter}
		<div class="border-t border-[#1a1a1a] px-4 py-4" data-testid="runner-body-param_jitter">
			<div class="grid gap-4 lg:grid-cols-3">
				<ResultPicker id="pj-result" label="Gauntlet result" bind:value={paramJitterForm.result_id} items={backtestHistory} helpText="Baseline for parameter perturbation." />
				<NumericInputField id="pj-pct" label="Jitter %" bind:value={paramJitterForm.jitter_pct} min="1" max="50" helpText="How much to perturb each parameter." />
				<NumericInputField id="pj-iter" label="Iterations" bind:value={paramJitterForm.n_iterations} min="10" max="200" step="5" helpText="Number of perturbation runs." />
			</div>
			<div class="mt-3 flex justify-end">
				<button
					type="button"
					class="terminal-button text-xs"
					on:click={handleParamJitterClick}
					disabled={loading.param_jitter || !paramJitterForm.result_id}
				>
					{loading.param_jitter ? 'Running...' : 'Run Jitter Test'}
				</button>
			</div>

			{#if errors.param_jitter}
				<div class={errorMessageClass('param_jitter')}>{errors.param_jitter}</div>
			{/if}

			{#if paramJitterResult}
				<div class="mt-4 border border-[#222] bg-[#050505] p-3">
					<div class="mb-3 flex items-center gap-2">
						<span class="text-[10px] uppercase tracking-wide text-[#666]">Results</span>
						<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(String(paramJitterResult.verdict))}`}>{paramJitterResult.verdict}</span>
						{#if paramJitterResult.method}
							<span class="border border-[#333] bg-black px-1.5 py-0.5 text-[10px] text-[#888]">{methodLabel(paramJitterResult.method)}</span>
						{/if}
						<span class="text-[10px] text-[#555]">{paramJitterResult.n_iterations} iterations @ {paramJitterResult.jitter_pct}% jitter</span>
					</div>
					<div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Original Sharpe</div>
							<div class="mt-1 font-mono text-sm text-white">{Number(paramJitterResult.original_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Mean Sharpe</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{Number(paramJitterResult.mean_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Std Dev</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{Number(paramJitterResult.std_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Min Sharpe</div>
							<div class="mt-1 font-mono text-sm text-red-400">{Number(paramJitterResult.min_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">Max Sharpe</div>
							<div class="mt-1 font-mono text-sm text-emerald-400">{Number(paramJitterResult.max_sharpe || 0).toFixed(3)}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-black px-3 py-2">
							<div class="text-[10px] text-[#666]">% Positive</div>
							<div class="mt-1 font-mono text-sm text-[#888]">{paramJitterResult.pct_positive_sharpe}%</div>
						</div>
					</div>
					{#if paramJitterResult.sharpe_histogram}
						<div class="mt-3 bg-[#111] p-2 flex justify-center">
							<DistributionChart
								bins={paramJitterResult.sharpe_histogram.bins}
								counts={paramJitterResult.sharpe_histogram.counts}
								title="Sharpe Distribution Under Parameter Jitter"
								xLabel="Sharpe Ratio"
								width={600} height={220}
								stats={{ mean: paramJitterResult.original_sharpe }}
							/>
						</div>
					{:else}
						<div class="mt-3 border border-[#333] bg-[#050505] px-2.5 py-2 text-[11px] text-[#666]">
							The Sharpe distribution chart is unavailable for this stored result (the full
							iteration artifact was pruned). Re-run the jitter test to regenerate it.
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- ──── Cost Stress ──── -->
<div class="mb-3 terminal-card overflow-hidden">
	<button
		class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#111]"
		on:click={() => toggleSection('cost_stress')}
	>
		<div class="flex items-center gap-3">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Cost Stress Test</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(scorecardVerdicts['cost_stress'])}`}>{verdictLabel(scorecardVerdicts['cost_stress'])}</span>
			{#if loading.cost_stress}<span class="animate-pulse text-[10px] text-yellow-400">running...</span>{/if}
		</div>
		<span class="text-[#555] text-sm">{expandedSections.cost_stress ? '−' : '+'}</span>
	</button>

	{#if expandedSections.cost_stress}
		<div class="border-t border-[#1a1a1a] px-4 py-4" data-testid="runner-body-cost_stress">
			<div class="grid gap-4 lg:grid-cols-2">
				<SymbolInput id="cs-symbol" label="Symbol" bind:value={costStressForm.symbol} suggestions={symbolSuggestions} />
				<TimeframeSelect id="cs-timeframe" label="Timeframe" bind:value={costStressForm.timeframe} />
			</div>
			<div class="mt-3">
				<DateRangeFieldset
					idPrefix="cs"
					title="Stress window"
					bind:startDate={costStressForm.start_date}
					bind:endDate={costStressForm.end_date}
					timeframe={costStressForm.timeframe}
					accent="rose"
				/>
			</div>
			<div class="mt-3 grid gap-4 lg:grid-cols-3">
				<NumericInputField id="cs-fee" label="Fee multiplier" bind:value={costStressForm.fee_multiplier} min="1" max="10" step="0.5" helpText="Scale base fee model." />
				<NumericInputField id="cs-slip" label="Slippage multiplier" bind:value={costStressForm.slippage_multiplier} min="1" max="10" step="0.5" helpText="Scale slippage model." />
				<div class="flex items-end">
					<button
						type="button"
						class="terminal-button w-full text-xs"
						on:click={handleCostStressClick}
						disabled={loading.cost_stress}
					>
						{loading.cost_stress ? 'Running...' : 'Run Stress Test'}
					</button>
				</div>
			</div>

			{#if errors.cost_stress}
				<div class={errorMessageClass('cost_stress')}>{errors.cost_stress}</div>
			{/if}

			{#if costStressResult}
				<div class="mt-4 border border-[#222] bg-[#050505] p-3">
					<div class="mb-3 flex items-center gap-2">
						<span class="text-[10px] uppercase tracking-wide text-[#666]">Results</span>
						<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(String(costStressResult.verdict))}`}>{costStressResult.verdict}</span>
						{#if costStressResult.method}
							<span class="border border-[#333] bg-black px-1.5 py-0.5 text-[10px] text-[#888]">{methodLabel(costStressResult.method)}</span>
						{/if}
						<span class="text-[10px] text-[#555]">Fees {costStressResult.fee_multiplier}x / Slippage {costStressResult.slippage_multiplier}x</span>
					</div>
					<div class="grid grid-cols-1 gap-4 md:grid-cols-2">
						<div class="bg-[#111] p-3">
							<div class="mb-2 text-[10px] text-[#666] uppercase">Original</div>
							{#if costStressResult.original}
								<div class="space-y-1 font-mono text-xs">
									<div class="flex justify-between"><span class="text-[#666]">Sharpe</span><span class="text-[#888]">{costStressResult.original.sharpe}</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Return</span><span class="text-emerald-400">{(Number(costStressResult.original.total_return || 0) * 100).toFixed(2)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Max DD</span><span class="text-red-400">{(Number(costStressResult.original.max_drawdown || 0) * 100).toFixed(2)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Win Rate</span><span class="text-[#888]">{(Number(costStressResult.original.win_rate || 0) * 100).toFixed(1)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Trades</span><span class="text-[#888]">{costStressResult.original.total_trades}</span></div>
								</div>
							{/if}
						</div>
						<div class="bg-[#111] p-3">
							<div class="mb-2 text-[10px] text-[#666] uppercase">Stressed</div>
							{#if costStressResult.stressed}
								<div class="space-y-1 font-mono text-xs">
									<div class="flex justify-between"><span class="text-[#666]">Sharpe</span><span class="{Number(costStressResult.stressed.sharpe) < Number(costStressResult.original?.sharpe || 0) ? 'text-red-400' : 'text-[#888]'}">{costStressResult.stressed.sharpe}</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Return</span><span class="text-[#888]">{(Number(costStressResult.stressed.total_return || 0) * 100).toFixed(2)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Max DD</span><span class="text-red-400">{(Number(costStressResult.stressed.max_drawdown || 0) * 100).toFixed(2)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Win Rate</span><span class="text-[#888]">{(Number(costStressResult.stressed.win_rate || 0) * 100).toFixed(1)}%</span></div>
									<div class="flex justify-between"><span class="text-[#666]">Trades</span><span class="text-[#888]">{costStressResult.stressed.total_trades}</span></div>
								</div>
							{/if}
						</div>
					</div>
					<div class="mt-2 text-center text-xs font-mono {Number(costStressResult.degradation_pct || 0) > 50 ? 'text-red-400' : 'text-[#888]'}">
						Sharpe Degradation: {costStressResult.degradation_pct}%
					</div>
					{#if costStressResult.original && costStressResult.stressed}
						<div class="mt-3 bg-[#111] p-2 flex justify-center">
							<CostStressComparisonChart original={costStressResult.original} stressed={costStressResult.stressed} width={500} height={220} />
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- ──── Regime Split ──── -->
<div class="mb-3 terminal-card overflow-hidden">
	<button
		class="flex w-full items-center justify-between px-4 py-3 text-left transition-colors hover:bg-[#111]"
		on:click={() => toggleSection('regime_split')}
	>
		<div class="flex items-center gap-3">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Regime Split</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(scorecardVerdicts['regime_split'])}`}>{verdictLabel(scorecardVerdicts['regime_split'])}</span>
			{#if loading.regime_split}<span class="animate-pulse text-[10px] text-yellow-400">running...</span>{/if}
		</div>
		<span class="text-[#555] text-sm">{expandedSections.regime_split ? '−' : '+'}</span>
	</button>

	{#if expandedSections.regime_split}
		<div class="border-t border-[#1a1a1a] px-4 py-4" data-testid="runner-body-regime_split">
			<div class="grid gap-4 lg:grid-cols-[1fr_auto]">
				<ResultPicker id="rs-result" label="Gauntlet result" bind:value={regimeSplitForm.result_id} items={backtestHistory} helpText="Which run to split by market regime." />
				<div class="flex items-end">
					<button
						type="button"
						class="terminal-button text-xs"
						on:click={handleRegimeSplitClick}
						disabled={loading.regime_split || !regimeSplitForm.result_id}
					>
						{loading.regime_split ? 'Running...' : 'Analyze Regimes'}
					</button>
				</div>
			</div>

			{#if errors.regime_split}
				<div class={errorMessageClass('regime_split')}>{errors.regime_split}</div>
			{/if}

			{#if regimeSplitResult}
				<div class="mt-4 border border-[#222] bg-[#050505] p-3">
					<div class="mb-3 flex items-center gap-2">
						<span class="text-[10px] uppercase tracking-wide text-[#666]">Results</span>
						<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${verdictBadge(String(regimeSplitResult.verdict))}`}>{regimeSplitResult.verdict}</span>
						{#if regimeSplitResult.method}
							<span class="border border-[#333] bg-black px-1.5 py-0.5 text-[10px] text-[#888]">{methodLabel(regimeSplitResult.method)}</span>
						{/if}
						<span class="text-[10px] text-[#555]">
							{regimeSplitResult.n_trades} trades · {regimeSplitResult.n_regimes} qualifying regime{regimeSplitResult.n_regimes === 1 ? '' : 's'}{regimeSplitResult.n_regimes_observed != null && regimeSplitResult.n_regimes_observed !== regimeSplitResult.n_regimes ? ` of ${regimeSplitResult.n_regimes_observed} observed` : ''}
						</span>
					</div>
					{#if regimeSplitResult.verdict_reasons?.length}
						<div class="mb-3 border border-red-900 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-400" data-testid="regime-verdict-reasons">
							<div class="text-[10px] font-semibold uppercase tracking-wide">Why it failed</div>
							<ul class="mt-1 list-disc space-y-0.5 pl-4">
								{#each regimeSplitResult.verdict_reasons as reason}<li>{reason}</li>{/each}
							</ul>
						</div>
					{/if}
					{#if regimeSplitResult.regimes?.length > 0}
						{@const regimesHaveReturns = regimeSplitResult.regimes.some((regime) => regime.total_return_pct != null)}
						<div class="mb-3 bg-[#111] p-2 flex justify-center">
							<RegimePnlChart regimes={regimeSplitResult.regimes} width={600} height={220} />
						</div>
						<!-- The verdict is decided in RETURN space (position-size-invariant); dollar
						     PnL can be synthesized when the baseline lacks real trade PnL. Show the
						     return columns whenever the payload carries them; only legacy persisted
						     results fall back to the $ view. -->
						<table class="w-full text-xs">
							<thead class="bg-[#050505] text-[#666]">
								<tr>
									<th class="px-2 py-1 text-left">Regime</th>
									<th class="px-2 py-1 text-right">Trades</th>
									<th class="px-2 py-1 text-right">Win Rate</th>
									{#if regimesHaveReturns}
										<th class="px-2 py-1 text-right" title="Average per-trade return in this regime (verdict input)">Avg Ret%</th>
										<th class="px-2 py-1 text-right" title="Summed per-trade returns in this regime — profitability here decides the verdict">Total Ret%</th>
										<th class="px-2 py-1 text-right">Best%</th>
										<th class="px-2 py-1 text-right">Worst%</th>
									{:else}
										<th class="px-2 py-1 text-right">Avg PnL</th>
										<th class="px-2 py-1 text-right">Total PnL</th>
										<th class="px-2 py-1 text-right">Best</th>
										<th class="px-2 py-1 text-right">Worst</th>
									{/if}
								</tr>
							</thead>
							<tbody>
								{#each regimeSplitResult.regimes as regime}
									{@const underMinTrades = regimeSplitResult.regime_min_trades != null && regime.trade_count < regimeSplitResult.regime_min_trades}
									<tr class={`border-t border-[#111] ${underMinTrades ? 'opacity-50' : ''}`} title={underMinTrades ? `Fewer than ${regimeSplitResult.regime_min_trades} trades — shown for context but not counted toward the verdict` : undefined}>
										<td class="px-2 py-1 font-mono text-[#888]">{regime.name}{underMinTrades ? ' *' : ''}</td>
										<td class="px-2 py-1 text-right font-mono text-[#888]">{regime.trade_count}</td>
										<td class="px-2 py-1 text-right font-mono {Number(regime.win_rate) >= 50 ? 'text-emerald-400' : 'text-red-400'}">{regime.win_rate}%</td>
										{#if regimesHaveReturns}
											<td class="px-2 py-1 text-right font-mono {Number(regime.avg_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{Number(regime.avg_return_pct ?? 0).toFixed(2)}%</td>
											<td class="px-2 py-1 text-right font-mono {Number(regime.total_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{Number(regime.total_return_pct ?? 0).toFixed(2)}%</td>
											<td class="px-2 py-1 text-right font-mono text-emerald-400">{Number(regime.best_return_pct ?? 0).toFixed(2)}%</td>
											<td class="px-2 py-1 text-right font-mono text-red-400">{Number(regime.worst_return_pct ?? 0).toFixed(2)}%</td>
										{:else}
											<td class="px-2 py-1 text-right font-mono {Number(regime.avg_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${regime.avg_pnl}</td>
											<td class="px-2 py-1 text-right font-mono {Number(regime.total_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${regime.total_pnl}</td>
											<td class="px-2 py-1 text-right font-mono text-emerald-400">${regime.best_trade}</td>
											<td class="px-2 py-1 text-right font-mono text-red-400">${regime.worst_trade}</td>
										{/if}
									</tr>
								{/each}
							</tbody>
						</table>
						{#if regimeSplitResult.profitable_regime_share != null}
							<div class="mt-2 text-center font-mono text-xs text-[#888]">
								Profitable regime share: {(Number(regimeSplitResult.profitable_regime_share) * 100).toFixed(0)}% of qualifying regimes
							</div>
						{/if}
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>
