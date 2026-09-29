<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		ApiError,
		getJob,
		getManualBacktestDefaults,
		getPrebuiltStrategies,
		getResult,
		getStrategies,
		getSymbols,
		previewSignals,
		submitBacktest,
		type BacktestResult,
		type ManualBacktestDefaults,
		type SignalPreview,
		type Strategy,
	} from '$lib/api';
	import { estimateBarCount, formatBarEstimate, resolveDateRangePreset } from '$lib/utils/dateRange';
	import { addToast } from '$lib/stores/processTracker';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import BacktestResultSummary from '$lib/components/backtest/BacktestResultSummary.svelte';
	import ReadinessChip from '$lib/components/data-readiness/ReadinessChip.svelte';
	import type { ReadinessQuery } from '$lib/api/dataReadiness';
	import {
		TRADE_MODES,
		TRADE_MODE_LABELS,
		buildBacktestRequest,
		changedParams,
		decayNote,
		describeProfile,
		editableParams,
		isBuiltin,
		marketLabel,
		nativeMarket,
		overrideLabels,
		profileDraftFrom,
		profileFromDraft,
		profileIsInert,
		sameMarket,
		sampleSplit,
		shiftedByHoldout,
		strategyKey,
		strategyOptionLabel,
		strategyProfile,
		testedWindow,
		validateRun,
		type ProfileDraft,
		type RunSettings,
		type TradeMode,
	} from '$lib/utils/manualBacktest';

	// --- Strategies ---------------------------------------------------------------

	type Source = 'builtin' | 'mine';
	const MINE_GROUPS = [
		{ status: 'live_graduated', label: 'Live' },
		{ status: 'paper', label: 'Paper' },
		{ status: 'gauntlet', label: 'Gauntlet' },
		{ status: 'quick_screen', label: 'Quick screen' },
	] as const;

	let source: Source = 'builtin';
	let builtins: Strategy[] = [];
	let builtinsLoading = true;
	let builtinsError = '';
	let mineGroups: { label: string; strategies: Strategy[] }[] = [];
	let mineLoading = false;
	let mineLoaded = false;
	let mineError = '';
	let symbolSuggestions: string[] = [];
	let defaults: ManualBacktestDefaults | null = null;

	let selected: Strategy | null = null;
	let selectedKey = '';
	let baseParams: Record<string, unknown> = {};
	let paramsDraft: Record<string, unknown> = {};
	let paramsHaveErrors = false;
	let paramEditorKey = 0;

	$: mineCount = mineGroups.reduce((n, g) => n + g.strategies.length, 0);
	$: builtinSelected = isBuiltin(selected);
	$: hiddenParamCount = selected
		? Object.keys(selected.raw_params ?? {}).length - Object.keys(baseParams).length
		: 0;
	$: editedParamCount = Object.keys(changedParams(paramsDraft, baseParams)).length;

	// --- Market ---------------------------------------------------------------------

	const todayUtc = new Date().toISOString().slice(0, 10);
	let symbol = 'BTC/USDT';
	let timeframe = '1h';
	const initialWindow = resolveDateRangePreset('2y');
	let startDate = initialWindow.startDate;
	let endDate = initialWindow.endDate;

	$: holdoutCutoff = defaults?.holdout_cutoff ?? null;
	$: holdoutDay = holdoutCutoff ? holdoutCutoff.slice(0, 10) : '';
	$: estimatedBars = estimateBarCount(startDate, endDate, timeframe);
	// Data readiness for the market, the window back to its start, and the
	// feeds the selected strategy reads.
	$: readinessDays = Math.ceil((Date.now() - Date.parse(`${startDate}T00:00:00Z`)) / 86_400_000);
	let readinessQuery: ReadinessQuery;
	$: readinessQuery = {
		symbol,
		timeframe,
		...(readinessDays > 0 ? { history_days: readinessDays } : {}),
		...(selected ? { strategy_type: selected.api_name || selected.name } : {}),
	};
	$: native = selected ? nativeMarket(selected) : null;
	$: offMarket = Boolean(native && !sameMarket(native, symbol, timeframe));

	// --- Execution ------------------------------------------------------------------

	let showExecution = false;
	let initialCapital: number | null = null;
	let feeBps: number | null = null;
	let slippageBps: number | null = null;
	let leverage: number | null = null;
	let tradeMode: TradeMode | '' = '';
	let overrideProfile = false;
	let profileDraft: ProfileDraft = profileDraftFrom(null);

	$: ownProfile = selected ? strategyProfile(selected.raw_params) : null;
	$: strategyLeverage = (() => {
		const raw = Number(selected?.raw_params?.leverage);
		return Number.isFinite(raw) && raw > 0 ? raw : null;
	})();
	$: defaultTradeMode = selected ? ownTradeMode(selected) : 'long_only';
	$: allowedTradeModes = selected && builtinSelected && selected.trade_modes?.length
		? (TRADE_MODES.filter((m) => selected?.trade_modes?.includes(m)) as TradeMode[])
		: TRADE_MODES;
	$: sizingSummary = overrideProfile
		? `Override: ${describeProfile(profileFromDraft(profileDraft))}`
		: profileIsInert(ownProfile)
			? describeProfile(null)
			: `Strategy's own profile: ${describeProfile(ownProfile)}`;

	function ownTradeMode(strategy: Strategy): TradeMode {
		if (isBuiltin(strategy) && strategy.default_trade_mode) return strategy.default_trade_mode as TradeMode;
		const raw = strategy.raw_params ?? {};
		const configured = String(raw.trade_mode ?? '').trim().toLowerCase();
		if (configured === 'short_only' || configured === 'both' || configured === 'long_only') return configured;
		const side = String(raw.position ?? raw.direction ?? '').trim().toLowerCase();
		return side === 'short' ? 'short_only' : 'long_only';
	}

	function numberOrNull(value: string): number | null {
		if (value.trim() === '') return null;
		const n = Number(value);
		return Number.isFinite(n) ? n : NaN;
	}

	function resetExecution() {
		initialCapital = null;
		feeBps = null;
		slippageBps = null;
		leverage = null;
		tradeMode = '';
		overrideProfile = false;
		profileDraft = profileDraftFrom(ownProfile);
	}

	function toggleProfileOverride() {
		overrideProfile = !overrideProfile;
		if (overrideProfile) profileDraft = profileDraftFrom(ownProfile);
	}

	// --- The run as configured ------------------------------------------------------

	$: settings = selected
		? ({
			strategy: selected,
			symbol,
			timeframe,
			startDate,
			endDate,
			params: paramsDraft,
			initialCapital,
			feeBps,
			slippageBps,
			leverage,
			tradeMode,
			profile: overrideProfile ? profileDraft : null,
		} satisfies RunSettings)
		: null;
	$: validationError = !settings
		? 'Choose a strategy to backtest.'
		: paramsHaveErrors
			? 'Fix the highlighted parameter before running.'
			: validateRun(settings, { estimatedBars, today: todayUtc });
	$: runOverrides = settings ? overrideLabels(settings) : [];

	// --- Signal preview -------------------------------------------------------------

	let preview: SignalPreview | null = null;
	let previewFor = '';
	let previewLoading = false;
	let previewError = '';

	$: previewKey = settings
		? JSON.stringify([strategyKey(settings.strategy), symbol, timeframe, startDate, endDate, tradeMode, paramsDraft])
		: '';
	$: previewStale = Boolean(preview && previewFor !== previewKey);

	async function handlePreview() {
		if (!settings) return;
		// Signals depend on the strategy, market, window and params only; a
		// half-typed execution override must not block a preview.
		const signalSettings: RunSettings = {
			...settings, initialCapital: null, feeBps: null, slippageBps: null, leverage: null, profile: null,
		};
		const problem = paramsHaveErrors
			? 'Fix the highlighted parameter before previewing.'
			: validateRun(signalSettings, { estimatedBars, today: todayUtc });
		if (problem) {
			previewError = problem;
			return;
		}
		const request = buildBacktestRequest(signalSettings);
		const key = previewKey;
		previewLoading = true;
		previewError = '';
		try {
			preview = await previewSignals({
				strategy_name: request.strategy_name,
				symbol: request.symbol,
				timeframe: request.timeframe,
				start: request.start,
				end: request.end,
				trade_mode: tradeMode || defaultTradeMode,
				params: request.params,
			});
			previewFor = key;
		} catch (err) {
			preview = null;
			previewError = err instanceof Error ? err.message : 'Signal preview failed';
		} finally {
			previewLoading = false;
		}
	}

	// --- Runs -----------------------------------------------------------------------

	type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed';
	interface RunRecord {
		resultId: string;
		jobId: string;
		strategyKey: string;
		label: string;
		market: string;
		requestedStart: string;
		requestedEnd: string;
		overrides: string[];
		submittedAt: number;
		finishedAt?: number;
		status: RunStatus;
		progress?: string;
		error?: string;
		summary?: { returnPct: number | null; sharpe: number | null; maxDrawdownPct: number | null; trades: number | null };
	}

	const RUNS_KEY = 'forven.manualBacktest.runs.v1';
	const VIEWED_KEY = 'forven.manualBacktest.viewed.v1';
	const MAX_RUNS = 12;
	const POLL_LIMIT_MS = 30 * 60 * 1000;

	let runs: RunRecord[] = [];
	let submitting = false;
	let submitError = '';
	let now = Date.now();
	let clock: ReturnType<typeof setInterval> | null = null;
	let destroyed = false;
	const following = new Set<string>();
	const abandoned = new Set<string>();

	let viewedId = '';
	let viewedResult: BacktestResult | null = null;
	let viewedLoading = false;
	let viewedError = '';
	const resultCache = new Map<string, BacktestResult>();

	$: activeRun = runs.find((r) => r.status === 'queued' || r.status === 'running') ?? null;
	$: viewedRun = runs.find((r) => r.resultId === viewedId) ?? null;
	$: window_ = viewedResult ? testedWindow(viewedResult) : null;
	$: split = viewedResult ? sampleSplit(viewedResult) : null;
	$: decay = decayNote(split);
	$: bySide = (() => {
		const sides = (viewedResult?.metrics as Record<string, unknown> | undefined)?.by_side as
			Record<string, Record<string, unknown>> | undefined;
		if (!sides) return [];
		return Object.entries(sides)
			.filter(([, s]) => Number(s?.total_trades ?? 0) > 0)
			.map(([side, s]) => ({
				side,
				trades: Number(s.total_trades ?? 0),
				winRate: Number(s.win_rate ?? 0) * 100,
				returnPct: Number(s.total_return_pct ?? 0) * 100,
			}));
	})();
	$: resultWarnings = viewedResult ? collectWarnings(viewedResult) : [];
	$: holdoutShifted = Boolean(viewedRun && shiftedByHoldout(viewedRun.requestedEnd, holdoutCutoff));
	$: fundingIncomplete = (viewedResult?.metrics as Record<string, unknown> | undefined)?.funding_complete === false;

	function collectWarnings(result: BacktestResult): string[] {
		const out = new Set<string>();
		for (const list of [result.config?.warnings, (result as unknown as Record<string, unknown>).warnings]) {
			if (Array.isArray(list)) for (const w of list) if (typeof w === 'string' && w.trim()) out.add(w.trim());
		}
		return [...out];
	}

	function saveRuns() {
		try {
			sessionStorage.setItem(RUNS_KEY, JSON.stringify(runs));
			sessionStorage.setItem(VIEWED_KEY, viewedId);
		} catch {
			// Private windows and blocked storage: history just won't survive a reload.
		}
	}

	function restoreRuns() {
		try {
			const raw = JSON.parse(sessionStorage.getItem(RUNS_KEY) ?? '[]');
			if (Array.isArray(raw)) runs = raw.filter((r) => r && typeof r.resultId === 'string').slice(0, MAX_RUNS);
			viewedId = sessionStorage.getItem(VIEWED_KEY) ?? '';
		} catch {
			runs = [];
		}
	}

	function updateRun(resultId: string, patch: Partial<RunRecord>) {
		runs = runs.map((r) => (r.resultId === resultId ? { ...r, ...patch } : r));
		saveRuns();
	}

	function summarize(result: BacktestResult): RunRecord['summary'] {
		const m = (result.metrics ?? {}) as Record<string, unknown>;
		const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
		const ret = n(m.total_return);
		const dd = n(m.max_drawdown);
		return {
			returnPct: ret === null ? null : ret * 100,
			sharpe: n(m.sharpe_ratio),
			maxDrawdownPct: dd === null ? null : Math.abs(dd) * 100,
			trades: n(m.total_trades),
		};
	}

	function ensureClock() {
		if (!clock) clock = setInterval(() => (now = Date.now()), 1000);
	}

	async function followJob(record: RunRecord) {
		if (following.has(record.resultId)) return;
		following.add(record.resultId);
		ensureClock();
		try {
			for (let attempt = 0; !destroyed && !abandoned.has(record.resultId); attempt += 1) {
				if (Date.now() - record.submittedAt > POLL_LIMIT_MS) {
					updateRun(record.resultId, { status: 'failed', error: 'Stopped waiting after 30 minutes. The run may still finish; check the strategy report.' });
					return;
				}
				try {
					const job = await getJob(record.jobId);
					if (abandoned.has(record.resultId)) return;
					const status = String(job.status || '').toLowerCase();
					if (status === 'succeeded') {
						updateRun(record.resultId, { status: 'succeeded', progress: undefined, finishedAt: Date.now() });
						await loadResult(record.resultId, { select: viewedId === record.resultId || !viewedId, scroll: false });
						return;
					}
					if (status === 'failed' || status === 'cancelled') {
						updateRun(record.resultId, { status: 'failed', error: job.error || 'The backtest failed.', finishedAt: Date.now() });
						if (viewedId === record.resultId) viewedError = job.error || 'The backtest failed.';
						addToast(`Backtest failed: ${job.error || record.label}`, 'error');
						return;
					}
					updateRun(record.resultId, { status: status === 'queued' ? 'queued' : 'running', progress: job.progress || undefined });
				} catch {
					// A dropped poll is not a failed run; try again.
				}
				await new Promise((resolve) => setTimeout(resolve, attempt < 20 ? 1500 : 4000));
			}
		} finally {
			following.delete(record.resultId);
			if (!following.size && clock) {
				clearInterval(clock);
				clock = null;
			}
		}
	}

	async function loadResult(resultId: string, { select = true, scroll = true } = {}) {
		if (select) {
			viewedId = resultId;
			viewedError = '';
			saveRuns();
		}
		const cached = resultCache.get(resultId);
		if (cached) {
			if (viewedId === resultId) viewedResult = cached;
			return;
		}
		if (viewedId === resultId) {
			viewedLoading = true;
			viewedResult = null;
		}
		try {
			const result = await getResult(resultId);
			resultCache.set(resultId, result);
			const record = runs.find((r) => r.resultId === resultId);
			if (record && result) updateRun(resultId, { summary: summarize(result) });
			if (viewedId === resultId) {
				viewedResult = result;
				if (select && scroll) scrollToId('bt-result-detail');
			}
		} catch (err) {
			if (viewedId === resultId) viewedError = err instanceof Error ? err.message : 'Could not load the result.';
		} finally {
			if (viewedId === resultId) viewedLoading = false;
		}
	}

	function showRun(record: RunRecord) {
		if (record.status === 'succeeded') {
			void loadResult(record.resultId);
			return;
		}
		viewedId = record.resultId;
		viewedResult = null;
		viewedError = record.status === 'failed' ? record.error || 'The backtest failed.' : '';
		saveRuns();
	}

	async function handleSubmit() {
		if (!settings || !selected) return;
		if (validationError) {
			submitError = validationError;
			return;
		}
		if (activeRun) {
			submitError = 'A backtest is already running. Wait for it to finish.';
			return;
		}
		submitError = '';
		submitting = true;
		const request = buildBacktestRequest(settings);
		try {
			const job = await submitBacktest(request, { background: true });
			if (!job.result_id || !job.job_id) throw new Error('The backend did not return a job to follow.');
			const record: RunRecord = {
				resultId: job.result_id,
				jobId: job.job_id,
				strategyKey: request.strategy_id,
				label: strategyOptionLabel(selected),
				market: marketLabel(request.symbol, request.timeframe),
				requestedStart: request.start,
				requestedEnd: request.end,
				overrides: runOverrides,
				submittedAt: Date.now(),
				status: 'queued',
			};
			runs = [record, ...runs.filter((r) => r.resultId !== record.resultId)].slice(0, MAX_RUNS);
			viewedId = record.resultId;
			viewedResult = null;
			viewedError = '';
			saveRuns();
			scrollToId('bt-result-detail');
			void followJob(record);
		} catch (err) {
			submitError = err instanceof ApiError && err.status === 429
				? 'The backtest queue is full. Wait for a running backtest to finish, then try again.'
				: err instanceof Error ? err.message : 'Backtest submission failed.';
		} finally {
			submitting = false;
		}
	}

	/** After the next render, bring a section into view (a no-op where the DOM can't scroll). */
	function scrollToId(id: string) {
		queueMicrotask(() => document.getElementById(id)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }));
	}

	/**
	 * Stop following a run that looks stuck (e.g. the backend restarted under it),
	 * so the page can start another. The backend may still finish and save it.
	 */
	function stopWaiting(record: RunRecord) {
		abandoned.add(record.resultId);
		updateRun(record.resultId, {
			status: 'failed',
			error: 'Stopped waiting. If the backend finishes the run, it is saved to the strategy and shows in its report.',
			finishedAt: Date.now(),
		});
		if (viewedId === record.resultId) viewedError = 'Stopped waiting for this run.';
	}

	function openFullReport() {
		const id = String(viewedResult?.strategy_id || viewedRun?.strategyKey || '').trim();
		if (!id) return;
		goto(`/lab/strategy/${encodeURIComponent(id)}?returnTo=${encodeURIComponent('/backtest/new')}`);
	}

	function clearHistory() {
		runs = runs.filter((r) => r.status === 'queued' || r.status === 'running');
		if (!runs.some((r) => r.resultId === viewedId)) {
			viewedId = '';
			viewedResult = null;
		}
		saveRuns();
	}

	// --- Selecting a strategy -------------------------------------------------------

	function applyStrategy(strategy: Strategy | null) {
		selected = strategy;
		selectedKey = strategy ? strategyKey(strategy) : '';
		baseParams = editableParams(strategy?.raw_params);
		paramsDraft = JSON.parse(JSON.stringify(baseParams));
		paramsHaveErrors = false;
		paramEditorKey += 1;
		preview = null;
		previewError = '';
		submitError = '';
		// Trade mode, leverage and the execution profile belong to a strategy;
		// capital and costs are the operator's and carry over.
		tradeMode = '';
		leverage = null;
		overrideProfile = false;
		profileDraft = profileDraftFrom(strategy ? strategyProfile(strategy.raw_params) : null);
		if (strategy) useStrategyMarket(strategy);
	}

	function useStrategyMarket(strategy: Strategy) {
		const market = nativeMarket(strategy);
		if (market.symbol) symbol = market.symbol;
		if (market.timeframe) timeframe = market.timeframe;
	}

	function allStrategies(): Strategy[] {
		return [...builtins, ...mineGroups.flatMap((g) => g.strategies)];
	}

	function onStrategySelect(event: Event) {
		const key = (event.currentTarget as HTMLSelectElement).value;
		applyStrategy(allStrategies().find((s) => strategyKey(s) === key) ?? null);
	}

	function resetParams() {
		paramsDraft = JSON.parse(JSON.stringify(baseParams));
		paramsHaveErrors = false;
		paramEditorKey += 1;
	}

	function onParamsChange(event: CustomEvent<Record<string, unknown>>) {
		paramsDraft = event.detail;
	}

	async function setSource(next: Source) {
		source = next;
		if (next === 'mine' && !mineLoaded) await loadMine();
	}

	async function loadBuiltins() {
		builtinsLoading = true;
		builtinsError = '';
		try {
			const res = await getPrebuiltStrategies();
			builtins = res.strategies
				.filter((s) => strategyKey(s) !== 'rule_engine')
				.sort((a, b) => a.name.localeCompare(b.name));
		} catch (err) {
			builtinsError = err instanceof Error ? err.message : 'Failed to load strategies';
		} finally {
			builtinsLoading = false;
		}
	}

	async function loadMine() {
		mineLoading = true;
		mineError = '';
		const settled = await Promise.allSettled(
			MINE_GROUPS.map((g) => getStrategies({ status: g.status, limit: 500 })),
		);
		const groups: { label: string; strategies: Strategy[] }[] = [];
		const failures: string[] = [];
		settled.forEach((outcome, i) => {
			if (outcome.status === 'fulfilled') {
				const list = outcome.value.strategies.sort((a, b) =>
					String(b.display_id ?? '').localeCompare(String(a.display_id ?? ''), undefined, { numeric: true }),
				);
				if (list.length) groups.push({ label: MINE_GROUPS[i].label, strategies: list });
			} else {
				failures.push(MINE_GROUPS[i].label);
			}
		});
		mineGroups = groups;
		mineLoaded = failures.length < MINE_GROUPS.length;
		if (failures.length) mineError = `Could not load: ${failures.join(', ')}.`;
		mineLoading = false;
	}

	async function applyDeepLink() {
		const wanted = ($page.url.searchParams.get('strategy') ?? '').trim();
		if (!wanted) return;
		const match = (list: Strategy[]) =>
			list.find((s) => strategyKey(s) === wanted || (s.display_id ?? '') === wanted) ?? null;
		let found = match(builtins);
		if (!found) {
			await setSource('mine');
			found = match(mineGroups.flatMap((g) => g.strategies));
		}
		if (found) {
			source = isBuiltin(found) ? 'builtin' : 'mine';
			applyStrategy(found);
		} else {
			submitError = `Strategy “${wanted}” is not in the built-in, live, paper or Forge lists.`;
		}
	}

	onMount(async () => {
		restoreRuns();
		for (const record of runs) {
			if (record.status === 'queued' || record.status === 'running') void followJob(record);
		}
		if (viewedId && runs.some((r) => r.resultId === viewedId && r.status === 'succeeded')) {
			void loadResult(viewedId, { select: false });
		}
		void getSymbols().then((s) => (symbolSuggestions = s)).catch(() => (symbolSuggestions = []));
		void getManualBacktestDefaults()
			.then((d) => {
				defaults = d;
				// Presets end at the holdout cutoff: research windows past it are
				// shifted back anyway, so the form shows the window that really runs.
				const untouched = startDate === initialWindow.startDate && endDate === initialWindow.endDate;
				if (d.holdout_cutoff && untouched) {
					const w = resolveDateRangePreset('2y', { maxDate: d.holdout_cutoff.slice(0, 10) });
					startDate = w.startDate;
					endDate = w.endDate;
				}
			})
			.catch(() => (defaults = null));
		await loadBuiltins();
		await applyDeepLink();
	});

	onDestroy(() => {
		destroyed = true;
		if (clock) clearInterval(clock);
	});

	// --- Formatting -----------------------------------------------------------------

	function fmtPct(v: number | null | undefined, dp = 2): string {
		return v === null || v === undefined || !Number.isFinite(v) ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(dp)}%`;
	}
	function fmtNum(v: number | null | undefined, dp = 2): string {
		return v === null || v === undefined || !Number.isFinite(v) ? '–' : v.toFixed(dp);
	}
	function fmtDay(value: string | null | undefined): string {
		if (!value) return '–';
		const d = new Date(`${value.slice(0, 10)}T00:00:00Z`);
		return Number.isNaN(d.getTime())
			? value
			: d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
	}
	function fmtElapsed(ms: number): string {
		const s = Math.max(0, Math.round(ms / 1000));
		return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
	}
	function toneFor(v: number | null | undefined): string {
		if (v === null || v === undefined || !Number.isFinite(v)) return 'text-sc-ink2';
		return v >= 0 ? 'text-emerald-400' : 'text-red-400';
	}
</script>

<svelte:head>
	<title>Manual Backtest | Forven</title>
</svelte:head>

<div class="mx-auto max-w-7xl px-4 py-6">
	<!-- Header -->
	<div class="mb-4 border-b border-sc-line pb-4">
		<div class="flex flex-wrap items-end justify-between gap-4">
			<div>
				<h1 class="text-lg font-bold uppercase tracking-widest text-sc-ink">Manual Backtest</h1>
				<p class="mt-1 text-xs text-sc-ink3">
					Backtest a built-in template or one of your strategies on any market. Anything you leave blank runs the way the strategy itself does.
				</p>
			</div>
			<a href="/strategy-creator" class="terminal-button text-xs">Build your own → Strategy Creator</a>
		</div>
	</div>

	<form id="bt-config" on:submit|preventDefault={handleSubmit} novalidate>
		<!-- Strategy -->
		<section class="terminal-card p-4" aria-labelledby="bt-strategy-label">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<label id="bt-strategy-label" for="bt-strategy" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Strategy</label>
				<div class="inline-flex border border-sc-line2" role="group" aria-label="Strategy source">
					<button
						type="button"
						class="px-3 py-1 text-[12px] {source === 'builtin' ? 'bg-sc-ink text-black' : 'text-sc-ink2 hover:text-sc-ink'}"
						aria-pressed={source === 'builtin'}
						on:click={() => setSource('builtin')}
					>
						Built-in <span class="tabular-nums">({builtins.length})</span>
					</button>
					<button
						type="button"
						class="border-l border-sc-line2 px-3 py-1 text-[12px] {source === 'mine' ? 'bg-sc-ink text-black' : 'text-sc-ink2 hover:text-sc-ink'}"
						aria-pressed={source === 'mine'}
						on:click={() => setSource('mine')}
					>
						My strategies{#if mineLoaded}&nbsp;<span class="tabular-nums">({mineCount})</span>{/if}
					</button>
				</div>
			</div>

			<div class="mt-3">
				{#if source === 'builtin' && builtinsLoading}
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" role="status" aria-live="polite">Loading strategies…</div>
				{:else if source === 'builtin' && builtinsError}
					<div class="flex flex-wrap items-center gap-3" role="alert">
						<span class="text-sm text-red-400">{builtinsError}</span>
						<button type="button" on:click={loadBuiltins} class="terminal-button text-[12px]">Retry</button>
					</div>
				{:else if source === 'mine' && mineLoading}
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" role="status" aria-live="polite">Loading your live, paper and Forge strategies…</div>
				{:else}
					<select id="bt-strategy" class="terminal-select" on:change={onStrategySelect} value={selectedKey}>
						<option value="" disabled>Select a strategy…</option>
						{#if source === 'builtin'}
							{#each builtins as strategy (strategyKey(strategy))}
								<option value={strategyKey(strategy)}>{strategyOptionLabel(strategy)}</option>
							{/each}
						{:else}
							{#each mineGroups as group (group.label)}
								<optgroup label={group.label}>
									{#each group.strategies as strategy (strategyKey(strategy))}
										<option value={strategyKey(strategy)}>{strategyOptionLabel(strategy)}</option>
									{/each}
								</optgroup>
							{/each}
						{/if}
					</select>
					{#if source === 'mine' && mineError}
						<div class="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-amber-400" role="alert">
							{mineError}
							<button type="button" on:click={loadMine} class="terminal-button text-[12px]">Retry</button>
						</div>
					{:else if source === 'mine' && mineLoaded && mineCount === 0}
						<p class="mt-2 text-[11px] text-sc-ink3">No live, paper or Forge strategies yet. Archived strategies are not listed.</p>
					{/if}
				{/if}

				{#if selected}
					<div class="rounded-md mt-3 border border-sc-line bg-sc-panel p-3 text-[11px]">
						<div class="flex flex-wrap items-center gap-2">
							<span class="font-mono text-sc-ink">{strategyOptionLabel(selected)}</span>
							{#if selected.stage && !builtinSelected}
								<span class="border border-sc-line2 px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">{selected.stage.replace(/_/g, ' ')}</span>
							{/if}
							{#if builtinSelected}
								<span class="border border-sc-line2 px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">built-in</span>
							{/if}
						</div>
						<!-- A stored strategy's "description" is its notes column: an audit trail, not a summary. -->
						{#if selected.description && builtinSelected}
							<p class="mt-1.5 text-sc-ink3">{selected.description}</p>
						{/if}
						{#if native?.symbol || native?.timeframe}
							<p class="mt-1.5 text-sc-ink3">
								{builtinSelected ? 'Written for' : 'Runs on'}
								<span class="font-mono text-sc-ink2">{marketLabel(native.symbol, native.timeframe)}</span>
							</p>
						{/if}
					</div>
				{/if}
			</div>
		</section>

		<!-- Market -->
		<section class="terminal-card mt-4 p-4">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Market</div>
				<ReadinessChip query={readinessQuery} align="right" />
			</div>
			<div class="mt-3 grid gap-4 md:grid-cols-2">
				<SymbolInput id="bt-symbol" bind:value={symbol} suggestions={symbolSuggestions} helpText="The engine backtests the base asset (ETH/USDT runs ETH)." />
				<TimeframeSelect id="bt-timeframe" bind:value={timeframe} />
			</div>
			{#if selected && native && offMarket}
				<div class="mt-3 flex flex-wrap items-center gap-3 border border-amber-900 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-400" role="status">
					<span>
						{#if builtinSelected}
							This template was written for {marketLabel(native.symbol, native.timeframe)}.
						{:else}
							{selected.display_id || 'This strategy'} trades {marketLabel(native.symbol, native.timeframe)}. A run on another market describes a variant, not the strategy as it trades.
						{/if}
					</span>
					<button type="button" class="terminal-button text-[12px]" on:click={() => selected && useStrategyMarket(selected)}>
						Use {marketLabel(native.symbol, native.timeframe)}
					</button>
				</div>
			{/if}
			<div class="mt-4">
				<DateRangeFieldset idPrefix="bt-date" bind:startDate bind:endDate {timeframe} maxDate={holdoutDay} />
			</div>
			{#if holdoutCutoff}
				<p class="mt-2 text-[11px] text-sc-ink3">
					Research holdout: data from <span class="text-sc-ink2">{fmtDay(holdoutCutoff)}</span> on is held back for each new
					strategy's one-shot test, so backtests end there. A window that reaches past it is shifted back to end at the cutoff.
				</p>
			{/if}
		</section>

		<!-- Parameters -->
		{#if selected}
			<section class="terminal-card mt-4 p-4">
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						Strategy parameters
						{#if editedParamCount}
							<span class="ml-2 border border-sc-line2 px-1.5 py-0.5 text-[9px] text-sc-ink">{editedParamCount} changed</span>
						{/if}
					</div>
					<button type="button" class="terminal-button text-[12px]" on:click={resetParams} disabled={!editedParamCount}>
						Reset to {builtinSelected ? 'template defaults' : 'stored values'}
					</button>
				</div>
				<div class="mt-3">
					{#key paramEditorKey}
						<ParameterEditor params={paramsDraft} bind:hasErrors={paramsHaveErrors} on:paramsChange={onParamsChange} />
					{/key}
				</div>
				{#if hiddenParamCount > 0}
					<p class="mt-2 text-[11px] text-sc-ink3">
						{hiddenParamCount === 1 ? '1 more field is' : `${hiddenParamCount} more fields are`} not listed here: market, leverage,
						trade mode and execution profile are set in Market and Execution, and internal data-contract fields are fixed.
					</p>
				{/if}
			</section>
		{/if}

		<!-- Execution -->
		<section class="terminal-card mt-4 p-4">
			<button type="button" class="flex w-full items-start justify-between gap-3 text-left" on:click={() => (showExecution = !showExecution)} aria-expanded={showExecution}>
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Execution</div>
					<div class="mt-1 text-[11px] text-sc-ink2">{sizingSummary}</div>
				</div>
				<span class="text-sm text-sc-ink3">{showExecution ? '−' : '+'}</span>
			</button>
			{#if showExecution}
				<div class="mt-4 border-t border-sc-line pt-4">
					<p class="text-[11px] text-sc-ink3">Leave a field blank to use the strategy's own setting or the engine default shown in it.</p>
					<div class="mt-3 grid gap-4 md:grid-cols-2 xl:grid-cols-5">
						<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Initial capital</div>
							<input type="number" value={initialCapital ?? ''} on:input={(e) => (initialCapital = numberOrNull(e.currentTarget.value))} step="1000" min="1"
								placeholder={(defaults?.initial_capital ?? 10000).toLocaleString()} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Fees (bps)</div>
							<input type="number" value={feeBps ?? ''} on:input={(e) => (feeBps = numberOrNull(e.currentTarget.value))} step="0.5" min="0"
								placeholder={defaults ? `${defaults.fee_bps} (default)` : 'Default'} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Slippage (bps)</div>
							<input type="number" value={slippageBps ?? ''} on:input={(e) => (slippageBps = numberOrNull(e.currentTarget.value))} step="0.5" min="0"
								placeholder={defaults ? `${defaults.slippage_bps} (default)` : 'Default'} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Leverage</div>
							<input type="number" value={leverage ?? ''} on:input={(e) => (leverage = numberOrNull(e.currentTarget.value))} step="0.5" min="0.1" max="125"
								placeholder={strategyLeverage !== null ? `${strategyLeverage}× (strategy)` : `${defaults?.leverage ?? 1}× (default)`} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Trade direction</div>
							<select bind:value={tradeMode} class="terminal-select mt-1.5">
								<option value="">Strategy default ({TRADE_MODE_LABELS[defaultTradeMode].toLowerCase()})</option>
								{#each allowedTradeModes as mode}
									<option value={mode}>{TRADE_MODE_LABELS[mode]}</option>
								{/each}
							</select></label>
					</div>
					{#if builtinSelected && selected?.trade_modes?.length}
						<p class="mt-1 text-[11px] text-sc-ink3">This template can trade: {selected.trade_modes.map((m) => TRADE_MODE_LABELS[m as TradeMode] ?? m).join(', ').toLowerCase()}.</p>
					{:else if selected && tradeMode && tradeMode !== defaultTradeMode}
						<p class="mt-1 text-[11px] text-sc-ink3">The run fails if the strategy can't trade that side.</p>
					{/if}

					<div class="mt-5 flex flex-wrap items-center justify-between gap-3">
						<label class="flex cursor-pointer items-center gap-2 text-[11px] text-sc-ink2">
							<input type="checkbox" checked={overrideProfile} on:change={toggleProfileOverride} class="h-3.5 w-3.5 accent-white" />
							Override the execution profile (sizing and exits)
						</label>
						<button type="button" class="terminal-button text-[12px]" on:click={resetExecution}>Reset execution</button>
					</div>
					{#if overrideProfile}
						<p class="mt-2 text-[11px] text-sc-ink3">
							An override replaces the strategy's own profile for this run; it is not merged with it.
						</p>
						<div class="mt-3 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
							<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Sizing</div>
								<select bind:value={profileDraft.sizingMode} class="terminal-select mt-1.5">
									<option value="atr">ATR risk (risk % against an ATR stop)</option>
									<option value="fraction">Risk % against your stop</option>
									<option value="fixed">Fixed notional</option>
									<option value="full">Full equity</option>
									<option value="kelly" disabled>Kelly (opens no trades in a backtest)</option>
								</select></label>
							{#if profileDraft.sizingMode === 'fraction' || profileDraft.sizingMode === 'atr'}
								<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Risk per trade (%)</div>
									<input type="number" value={profileDraft.riskPct ?? ''} on:input={(e) => (profileDraft.riskPct = numberOrNull(e.currentTarget.value))} step="0.25" min="0.01" max="100" class="terminal-input mt-1.5" /></label>
							{/if}
							{#if profileDraft.sizingMode === 'fixed'}
								<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Position size (quote)</div>
									<input type="number" value={profileDraft.fixedSize ?? ''} on:input={(e) => (profileDraft.fixedSize = numberOrNull(e.currentTarget.value))} step="100" min="1" class="terminal-input mt-1.5" /></label>
							{/if}
							{#if profileDraft.sizingMode === 'atr'}
								<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">ATR stop (× ATR)</div>
									<input type="number" value={profileDraft.atrMultiplier ?? ''} on:input={(e) => (profileDraft.atrMultiplier = numberOrNull(e.currentTarget.value))} step="0.1" min="0.1" max="50" class="terminal-input mt-1.5" /></label>
							{/if}
						</div>
						<div class="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
							<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Stop loss %</div>
								<input type="number" value={profileDraft.stopLossPct ?? ''} on:input={(e) => (profileDraft.stopLossPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0.1" max="100" placeholder="None" class="terminal-input mt-1.5" /></label>
							<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Take profit %</div>
								<input type="number" value={profileDraft.takeProfitPct ?? ''} on:input={(e) => (profileDraft.takeProfitPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0.1" max="1000" placeholder="None" class="terminal-input mt-1.5" /></label>
							<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Trailing stop %</div>
								<input type="number" value={profileDraft.trailingStopPct ?? ''} on:input={(e) => (profileDraft.trailingStopPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0.1" max="100" placeholder="None" class="terminal-input mt-1.5" /></label>
							<label class="block"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Time stop (bars)</div>
								<input type="number" value={profileDraft.timeStopBars ?? ''} on:input={(e) => (profileDraft.timeStopBars = numberOrNull(e.currentTarget.value))} step="1" min="1" placeholder="None" class="terminal-input mt-1.5" /></label>
						</div>
					{/if}
				</div>
			{/if}
		</section>

		<!-- Preview -->
		<section class="terminal-card mt-4 p-4">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Signal preview</div>
					<p class="mt-1 text-[11px] text-sc-ink3">Counts entry and exit signals over the window, before any sizing or stops. Nothing is saved.</p>
				</div>
				<button type="button" on:click={handlePreview} disabled={previewLoading || !selected} class="terminal-button text-[12px]">
					{previewLoading ? 'Previewing…' : preview && !previewStale ? 'Refresh preview' : 'Preview signals'}
				</button>
			</div>
			{#if previewError}
				<div class="mt-3 border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400" role="alert">{previewError}</div>
			{:else if preview}
				{#if previewStale}
					<p class="mt-3 text-[11px] text-amber-400">The settings changed since this preview. Refresh it to match.</p>
				{/if}
				<div class="mt-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-5 {previewStale ? 'opacity-50' : ''}">
					<div><span class="text-sc-ink3">Bars:</span> <span class="font-mono text-sc-ink2">{preview.total_bars.toLocaleString()}</span></div>
					<div><span class="text-sc-ink3">Entries:</span> <span class="font-mono text-sc-ink">{preview.entry_count}</span></div>
					<div><span class="text-sc-ink3">Exits:</span> <span class="font-mono text-sc-ink2">{preview.exit_count}</span></div>
					<div><span class="text-sc-ink3">Bars between entries:</span> <span class="font-mono text-sc-ink2">{preview.avg_bars_between_entries == null ? '–' : Math.round(preview.avg_bars_between_entries).toLocaleString()}</span></div>
					<div><span class="text-sc-ink3">Density:</span>
						<span class="font-mono {preview.signal_density === 'dense' ? 'text-emerald-400' : preview.signal_density === 'moderate' ? 'text-amber-400' : 'text-sc-ink2'}">{preview.signal_density}</span></div>
				</div>
				{#if preview.sample_entries?.length}
					<div class="mt-2 text-[11px] text-sc-ink3">
						First entries:
						<span class="font-mono text-sc-ink2">{preview.sample_entries.slice(0, 5).map((e) => String(e.timestamp).slice(0, 16).replace('T', ' ')).join(' · ')}</span>
					</div>
				{/if}
				{#if preview.warnings?.length}
					<div class="mt-2 space-y-1">
						{#each preview.warnings as w}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{w}</div>{/each}
					</div>
				{/if}
			{/if}
		</section>

		<!-- Run -->
		<section class="terminal-card mt-4 p-4">
			{#if submitError}
				<div class="mb-3 border border-red-900 bg-red-500/5 px-4 py-3 text-sm text-red-400" role="alert">{submitError}</div>
			{/if}
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div class="min-w-0 text-[11px] text-sc-ink3">
					{#if selected}
						<span class="font-mono text-sc-ink2">{marketLabel(symbol, timeframe)}</span>
						· {fmtDay(startDate)} → {fmtDay(endDate)} · {formatBarEstimate(estimatedBars)}
						· {runOverrides.length ? runOverrides.join(' · ') : "strategy's own settings"}
						{#if validationError}
							<div class="mt-1 text-amber-400">{validationError}</div>
						{/if}
					{:else}
						Choose a strategy to backtest.
					{/if}
				</div>
				<button type="submit" disabled={submitting || Boolean(activeRun) || Boolean(validationError)} aria-busy={submitting || Boolean(activeRun)}
					class="terminal-button-primary text-[12px] disabled:cursor-not-allowed disabled:opacity-40">
					{#if submitting}
						Submitting…
					{:else if activeRun}
						Running… {fmtElapsed(now - activeRun.submittedAt)}
					{:else}
						Run backtest
					{/if}
				</button>
			</div>
		</section>
	</form>

	<!-- Results -->
	{#if runs.length || viewedId}
		<section id="bt-results" class="mt-6 scroll-mt-6">
			{#if runs.length}
				<div class="terminal-card p-4">
					<div class="flex items-center justify-between gap-3">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Runs this session</div>
						<button type="button" class="text-[12px] text-sc-ink3 underline hover:text-sc-ink" on:click={clearHistory}>Clear</button>
					</div>
					<div class="mt-2 overflow-x-auto">
						<table class="w-full text-[11px]">
							<thead class="text-sc-ink3">
								<tr class="border-b border-sc-line">
									<th class="px-2 py-1.5 text-left font-medium">Strategy</th>
									<th class="px-2 py-1.5 text-left font-medium">Market</th>
									<th class="px-2 py-1.5 text-left font-medium">Window</th>
									<th class="px-2 py-1.5 text-left font-medium">Changes</th>
									<th class="px-2 py-1.5 text-right font-medium">Return</th>
									<th class="px-2 py-1.5 text-right font-medium">Sharpe</th>
									<th class="px-2 py-1.5 text-right font-medium">Max DD</th>
									<th class="px-2 py-1.5 text-right font-medium">Trades</th>
								</tr>
							</thead>
							<tbody class="font-mono text-sc-ink2">
								{#each runs as run (run.resultId)}
									<tr class="border-b border-sc-line {run.resultId === viewedId ? 'bg-sc-ink/5 text-sc-ink' : ''}">
										<td class="max-w-[16rem] truncate px-2 py-1.5" title={run.label}>
											<button type="button" class="max-w-full truncate text-left hover:text-sc-ink hover:underline" on:click={() => showRun(run)}
												aria-current={run.resultId === viewedId ? 'true' : undefined}>{run.label}</button>
										</td>
										<td class="px-2 py-1.5">{run.market}</td>
										<td class="whitespace-nowrap px-2 py-1.5 text-sc-ink3">{run.requestedStart} → {run.requestedEnd}</td>
										<td class="max-w-[14rem] truncate px-2 py-1.5 text-sc-ink3" title={run.overrides.join(' · ')}>{run.overrides.length ? run.overrides.join(' · ') : '—'}</td>
										{#if run.status === 'succeeded' && run.summary}
											<td class="px-2 py-1.5 text-right {toneFor(run.summary.returnPct)}">{fmtPct(run.summary.returnPct)}</td>
											<td class="px-2 py-1.5 text-right">{fmtNum(run.summary.sharpe)}</td>
											<td class="px-2 py-1.5 text-right">{run.summary.maxDrawdownPct === null ? '–' : `${run.summary.maxDrawdownPct.toFixed(1)}%`}</td>
											<td class="px-2 py-1.5 text-right">{run.summary.trades ?? '–'}</td>
										{:else if run.status === 'failed'}
											<td class="px-2 py-1.5 text-right text-red-400" colspan="4">failed</td>
										{:else if run.status === 'succeeded'}
											<td class="px-2 py-1.5 text-right text-sc-ink3" colspan="4">done</td>
										{:else}
											<td class="px-2 py-1.5 text-right text-amber-400" colspan="4">{run.status}… {fmtElapsed(now - run.submittedAt)}</td>
										{/if}
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</div>
			{/if}

			{#if viewedRun}
				<div id="bt-result-detail" class="mt-4 scroll-mt-6 border-b border-sc-line pb-4">
					<div class="flex flex-wrap items-start justify-between gap-3">
						<div class="min-w-0">
							<h2 class="text-sm font-bold uppercase tracking-widest text-sc-ink">Result</h2>
							<p class="mt-1 text-xs text-sc-ink2">
								<span class="font-mono text-sc-ink2">{viewedRun.label}</span> on <span class="font-mono text-sc-ink2">{viewedRun.market}</span>
							</p>
							{#if window_}
								<p class="mt-1 text-[11px] text-sc-ink3">
									Tested {fmtDay(window_.start)} → {fmtDay(window_.end)}.
									Metrics and trades below are out-of-sample only: {fmtDay(window_.outOfSampleStart)} → {fmtDay(window_.outOfSampleEnd)}, the last 30% of the window.
								</p>
							{/if}
						</div>
						<div class="flex items-center gap-2">
							<button type="button" on:click={() => scrollToId('bt-config')}
								class="terminal-button text-[12px]">Adjust &amp; re-run</button>
							<button type="button" on:click={openFullReport} disabled={!viewedResult}
								class="terminal-button-primary text-[12px] disabled:opacity-40">Open full report →</button>
						</div>
					</div>
				</div>

				<div class="mt-4 space-y-4">
					{#if viewedRun.status === 'queued' || viewedRun.status === 'running'}
						<div class="terminal-card p-6 text-center text-xs text-sc-ink2" role="status" aria-live="polite">
							<div class="uppercase tracking-widest text-sc-ink2">{viewedRun.status === 'queued' ? 'Queued' : 'Running'} · {fmtElapsed(now - viewedRun.submittedAt)}</div>
							{#if viewedRun.progress}<div class="mt-1 text-sc-ink3">{viewedRun.progress}</div>{/if}
							<div class="mt-2 text-[11px] text-sc-ink3">You can keep editing or leave the page; the run keeps going and is saved to the strategy.</div>
							{#if now - viewedRun.submittedAt > 60_000}
								<button type="button" class="terminal-button mt-3 text-[12px]" on:click={() => viewedRun && stopWaiting(viewedRun)}>Stop waiting</button>
							{/if}
						</div>
					{:else if viewedError}
						<div class="border border-red-900 bg-red-500/5 px-4 py-3 text-sm text-red-400" role="alert">{viewedError}</div>
					{:else if viewedLoading}
						<div class="terminal-card p-8 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" role="status" aria-live="polite">Loading result…</div>
					{:else if viewedResult}
						{#if holdoutShifted}
							<div class="border border-amber-900 bg-amber-500/5 px-4 py-3 text-[11px] text-amber-400" role="status">
								You asked for data up to {fmtDay(viewedRun.requestedEnd)}, but data from {fmtDay(holdoutCutoff)} on is held back, so the engine
								shifted the window back to end at the cutoff (same length). The dates above are the ones it tested.
							</div>
						{/if}
						{#if resultWarnings.length || fundingIncomplete}
							<div class="space-y-1">
								{#each resultWarnings as w}
									<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{w}</div>
								{/each}
								{#if fundingIncomplete}
									<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">
										Funding history is incomplete for this window, so some funding payments are missing from the returns.
									</div>
								{/if}
							</div>
						{/if}

						{#if split}
							<div class="rounded-md border border-sc-line bg-sc-panel p-4">
								<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">In-sample vs out-of-sample</div>
								<div class="mt-2 overflow-x-auto">
									<table class="w-full text-[11px]">
										<thead class="text-sc-ink3">
											<tr class="border-b border-sc-line">
												<th class="px-2 py-1 text-left font-medium">Period</th>
												<th class="px-2 py-1 text-right font-medium">Return</th>
												<th class="px-2 py-1 text-right font-medium">Sharpe</th>
												<th class="px-2 py-1 text-right font-medium">Max DD</th>
												<th class="px-2 py-1 text-right font-medium">Win rate</th>
												<th class="px-2 py-1 text-right font-medium">Trades</th>
											</tr>
										</thead>
										<tbody class="font-mono text-sc-ink2">
											{#each [
												{ name: 'In-sample', dates: window_ ? `${fmtDay(window_.inSampleStart)} → ${fmtDay(window_.inSampleEnd)}` : '', stats: split.inSample },
												{ name: 'Out-of-sample', dates: window_ ? `${fmtDay(window_.outOfSampleStart)} → ${fmtDay(window_.outOfSampleEnd)}` : '', stats: split.outOfSample },
											] as row}
												<tr class="border-b border-sc-line">
													<td class="px-2 py-1.5 text-left">{row.name}<span class="ml-2 text-[10px] text-sc-ink3">{row.dates}</span></td>
													<td class="px-2 py-1.5 text-right {toneFor(row.stats.totalReturnPct)}">{fmtPct(row.stats.totalReturnPct)}</td>
													<td class="px-2 py-1.5 text-right">{fmtNum(row.stats.sharpe)}</td>
													<td class="px-2 py-1.5 text-right">{row.stats.maxDrawdownPct === null ? '–' : `${row.stats.maxDrawdownPct.toFixed(1)}%`}</td>
													<td class="px-2 py-1.5 text-right">{row.stats.winRatePct === null ? '–' : `${row.stats.winRatePct.toFixed(0)}%`}</td>
													<td class="px-2 py-1.5 text-right">{row.stats.trades ?? '–'}</td>
												</tr>
											{/each}
										</tbody>
									</table>
								</div>
								{#if decay}
									<p class="mt-2 text-[11px] text-amber-400">{decay}</p>
								{/if}
								{#if bySide.length > 1}
									<p class="mt-2 text-[11px] text-sc-ink3">
										Out-of-sample by side:
										{#each bySide as s, i}
											{i ? ' · ' : ''}<span class="font-mono text-sc-ink2">{s.side} {s.trades} trades, {s.winRate.toFixed(0)}% wins, <span class={toneFor(s.returnPct)}>{fmtPct(s.returnPct)}</span></span>
										{/each}
									</p>
								{/if}
							</div>
						{/if}

						<BacktestResultSummary result={viewedResult} />
					{:else if viewedRun.status === 'succeeded'}
						<div class="terminal-card p-6 text-sm text-sc-ink2">
							Result saved (<span class="font-mono">{viewedRun.resultId}</span>) but could not be loaded here.
							<button type="button" on:click={() => viewedRun && loadResult(viewedRun.resultId)} class="ml-1 text-sc-ink underline">Try again</button>
						</div>
					{/if}
				</div>
			{/if}
		</section>
	{/if}
</div>
