<script lang="ts">
	import { onMount, onDestroy, tick } from 'svelte';
	import { editableSnapshot } from '$lib/utils/creatorRevision';
	import { checkIdeaReadiness, type IdeaReadiness } from '$lib/api/strategyCreator';
	import { goto } from '$app/navigation';
	import {
		getIndicators,
		previewStrategyChart,
		stressTestStrategy,
		heatmapStrategy,
		compareMarkets,
		getDatasets,
		nlToSpec,
		nlEditSpec,
		listStrategyLibrary,
		createLibraryStrategy,
		updateLibraryStrategy,
		deleteLibraryStrategy,
		duplicateLibraryStrategy,
		sendLibraryStrategyToForge,
		getSystemStrategyDetail,
		getPrebuiltStrategies,
		getStrategies,
		submitBacktest,
		registerCustomStrategy,
		getResult,
		getSymbols,
		type IndicatorMeta,
		type PreviewChartContext,
		type PreviewRequest,
		type LibraryStrategy,
		type BacktestResult,
		type SensitivityResult,
		type HeatmapAxisRequest,
		type Strategy,
	} from '$lib/api';
	import { DATE_RANGE_PRESETS, inferDateRangePreset, resolveDateRangePreset, estimateBarCount } from '$lib/utils/dateRange';
	import { ORDERED_TIMEFRAME_OPTIONS } from '$lib/config/timeframes';
	import { addToast } from '$lib/stores/processTracker';
	import { builderIncompatibility } from '$lib/utils/ruleSpec';
	import { specSeriesLabels, specThresholds } from '$lib/utils/ruleLabels';
	import { diffSpecs, type SpecChange } from '$lib/utils/specDiff';
	import {
		chunk,
		hashSpec,
		hasLocalData,
		knobValue,
		marketAvailability,
		resultKey,
		runPool,
		specKnobs,
		withKnobs,
		withoutKnobs,
	} from '$lib/utils/creatorGrids';
	import type { ExecutionRequestFields } from '$lib/api';
	import { portal } from '$lib/actions/portal';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import BacktestResultSummary from '$lib/components/backtest/BacktestResultSummary.svelte';
	import StrategyBuilder from '$lib/components/strategy/StrategyBuilder.svelte';
	import StrategyChart from '$lib/components/strategy/StrategyChart.svelte';
	import StrategyLauncher from '$lib/components/strategy/StrategyLauncher.svelte';
	import TradeInspector from '$lib/components/strategy/TradeInspector.svelte';
	import TradesTable from '$lib/components/strategy/TradesTable.svelte';
	import VitalsPanel from '$lib/components/strategy/VitalsPanel.svelte';
	import StressTestPanel from '$lib/components/strategy/StressTestPanel.svelte';
	import HeatmapPanel, { cellKey, type HeatmapView } from '$lib/components/strategy/HeatmapPanel.svelte';
	import MarketGridPanel, { marketKey, type MarketView } from '$lib/components/strategy/MarketGridPanel.svelte';
	import VariantHistory, { type Variant } from '$lib/components/strategy/VariantHistory.svelte';
	import StrategyImportDialog from '$lib/components/strategy/StrategyImportDialog.svelte';
	import type { StrategyImportResult } from '$lib/api';
	import { STRATEGY_TEMPLATES, type RuleSpec } from '$lib/components/strategy/templates';

	const BAR_CAP = 100_000;
	const RULE_ENGINE_TYPE = 'rule_engine';

	// 'visual' = Rules, 'code' = Python (the saved kinds keep these names).
	type Mode = 'visual' | 'code';
	let mode: Mode = 'visual';

	// Catalog + form
	let indicators: IndicatorMeta[] = [];
	let symbolSuggestions: string[] = [];
	let loadError = '';

	const defaultRange = resolveDateRangePreset('1y');
	let symbol = 'BTC/USDT';
	let timeframe = '1h';
	let startDate = defaultRange.startDate;
	let endDate = defaultRange.endDate;

	let strategyName = 'My Strategy';
	let strategyDescription = '';

	// Visual builder state
	let currentSpec: RuleSpec | null = null; // initialSpec fed into the builder
	let liveSpec: Record<string, unknown> | null = null;
	let liveValid = false;
	let liveErrors: string[] = [];

	$: baseAsset = (symbol.split(/[/\-:]/)[0] || symbol).trim().toUpperCase();
	$: metaByKind = Object.fromEntries(indicators.map((m) => [m.kind, m]));

	function deriveTradeMode(spec: Record<string, unknown> | null): 'long_only' | 'short_only' | 'both' {
		const g = (k: string) => spec?.[k] as { conditions?: unknown[] } | null | undefined;
		const hasLong = !!g('entry_long')?.conditions?.length;
		const hasShort = !!g('entry_short')?.conditions?.length;
		if (hasShort && hasLong) return 'both';
		if (hasShort) return 'short_only';
		return 'long_only';
	}
	$: effectiveTradeMode = mode === 'visual' ? deriveTradeMode(liveSpec) : tradeMode;

	function clone<T>(v: T): T {
		return JSON.parse(JSON.stringify(v));
	}

	function onBuilderChange(
		e: CustomEvent<{ spec: Record<string, unknown>; valid: boolean; errors: string[] }>
	) {
		liveSpec = e.detail.spec;
		liveValid = e.detail.valid;
		liveErrors = e.detail.errors;
		recordHistory(e.detail.spec);
	}

	// Leaving the rules keeps the edits for the way back.
	function setMode(next: Mode) {
		if (mode === 'visual' && next !== 'visual' && liveSpec) currentSpec = clone(liveSpec) as unknown as RuleSpec;
		mode = next;
	}

	// ---- Undo / redo of the rules ------------------------------------------------
	let history: string[] = [];
	let historyIndex = -1;
	let lastRecordAt = 0;
	let lastNumberPath: string | null = null;
	const COALESCE_MS = 800;
	/** The path of the one number that differs between two specs of the same shape, else null. */
	function changedNumber(before: unknown, after: unknown): string | null {
		const paths: string[] = [];
		const same = (x: unknown, y: unknown, path: string): boolean => {
			if (typeof x === 'number' && typeof y === 'number') {
				if (x !== y) paths.push(path);
				return true;
			}
			if (!x || !y || typeof x !== 'object' || typeof y !== 'object') return x === y;
			const xk = Object.keys(x);
			const yk = Object.keys(y);
			if (Array.isArray(x) !== Array.isArray(y) || xk.length !== yk.length || xk.some((k, i) => k !== yk[i])) return false;
			return xk.every((k) => same((x as Record<string, unknown>)[k], (y as Record<string, unknown>)[k], `${path}/${k}`));
		};
		return same(before, after, '') && paths.length === 1 ? paths[0] : null;
	}
	function recordHistory(spec: Record<string, unknown>) {
		const json = JSON.stringify(spec);
		// A restored version echoes back from the builder unchanged.
		if (history[historyIndex] === json) return;
		const now = Date.now();
		// One step per gesture: a burst of edits, or edits to the same number
		// (typing a value, dragging a slider).
		const numberPath = history[historyIndex] ? changedNumber(JSON.parse(history[historyIndex]), spec) : null;
		const sameNumber = numberPath !== null && numberPath === lastNumberPath;
		const coalesce = (sameNumber || now - lastRecordAt < COALESCE_MS) && historyIndex > 0 && historyIndex === history.length - 1;
		lastRecordAt = now;
		lastNumberPath = numberPath;
		history = [...history.slice(0, coalesce ? historyIndex : historyIndex + 1), json].slice(-200);
		historyIndex = history.length - 1;
	}
	function resetHistory() {
		history = [];
		historyIndex = -1;
		lastRecordAt = 0;
		lastNumberPath = null;
	}
	$: canUndo = mode === 'visual' && historyIndex > 0;
	$: canRedo = mode === 'visual' && historyIndex < history.length - 1;
	function stepHistory(delta: number) {
		historyIndex += delta;
		lastRecordAt = 0;
		lastNumberPath = null;
		currentSpec = JSON.parse(history[historyIndex]);
	}
	function undo() {
		if (canUndo) stepHistory(-1);
	}
	function redo() {
		if (canRedo) stepHistory(1);
	}

	// A different strategy starts a fresh line of research: its own undo history
	// and its own count of results seen.
	function resetSession() {
		resetHistory();
		variants = [];
		versionCount = 0;
		seenResults = new Set();
		stressResult = null;
		stressError = '';
		heatmapController?.abort();
		heatmapController = null;
		heatmapView = null;
		marketController?.abort();
		marketController = null;
		marketView = null;
		selectedTrade = null;
		pendingEdit = null;
	}

	// Templates
	function applyTemplate(id: string, { quiet = false } = {}) {
		const t = STRATEGY_TEMPLATES.find((x) => x.id === id);
		if (!t) return;
		resetSession();
		restoreExecution({});
		currentSpec = clone(t.spec);
		symbol = t.symbol;
		timeframe = t.timeframe;
		strategyName = t.name;
		strategyDescription = t.description;
		setMode('visual');
		currentLibraryId = null;
		launcherOpen = false;
		if (!quiet) addToast(`Loaded template “${t.name}”`, 'info');
	}
	function blankCanvas() {
		resetSession();
		restoreExecution({});
		currentSpec = clone({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }],
			params: { oversold: 30 },
			entry_long: { logic: 'and', conditions: [{ left: 'rsi', op: '<', right: { param: 'oversold' } }] },
			exit_long: null,
			entry_short: null,
			exit_short: null,
		});
		strategyName = 'My Strategy';
		strategyDescription = '';
		currentLibraryId = null;
		launcherOpen = false;
		setMode('visual');
	}

	// Code mode. Each new draft gets its own TYPE_NAME: registering code under a
	// name another draft already sent to the Forge is refused.
	function customTemplate(): string {
		const typeName = `my_strategy_${Math.random().toString(36).slice(2, 8)}`;
		return CUSTOM_TEMPLATE.replaceAll('my_strategy', typeName);
	}
	const CUSTOM_TEMPLATE = `import pandas as pd
import numpy as np
from forven.strategies.base import BaseStrategy, Signal


class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "My Strategy"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "my_strategy"

    @property
    def default_params(self) -> dict:
        return {"rsi_length": 14, "oversold": 30, "overbought": 70}

    def _rsi(self, close, n):
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(n).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(n).mean()
        return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    def generate_signals(self, df):
        n = int(self.params["rsi_length"])
        rsi = self._rsi(df["close"], n)
        entries = (rsi < self.params["oversold"]).fillna(False)
        exits = (rsi > self.params["overbought"]).fillna(False)
        return entries, exits

    def generate_signal(self, df):
        n = int(self.params["rsi_length"])
        if len(df) < n + 1:
            return Signal()
        rsi = self._rsi(df["close"], n).iloc[-1]
        price = float(df["close"].iloc[-1])
        if rsi < self.params["oversold"]:
            return Signal(entry_signal=True, direction="long", price=price)
        if rsi > self.params["overbought"]:
            return Signal(exit_signal=True, price=price)
        return Signal()


STRATEGY_CLASS = MyStrategy
TYPE_NAME = "my_strategy"
`;
	let customCode = customTemplate();
	type CustomStatus = 'idle' | 'validating' | 'loaded' | 'failed';
	let customStatus: CustomStatus = 'idle';
	let customErrors: string[] = [];
	let customWarnings: string[] = [];
	let customLoadedName = '';
	let validatedCode = '';
	$: if (customStatus === 'loaded' && customCode !== validatedCode) {
		customStatus = 'idle';
		customLoadedName = '';
	}
	let paramsDraft: Record<string, unknown> = {};

	async function loadCustomStrategy() {
		customStatus = 'validating';
		customErrors = [];
		customWarnings = [];
		try {
			const code = customCode;
			const res = await registerCustomStrategy({ code });
			customErrors = res.errors ?? [];
			customWarnings = res.warnings ?? [];
			if (res.valid && res.registered && res.strategy_name) {
				validatedCode = code;
				customLoadedName = res.strategy_name;
				customStatus = 'loaded';
				// Keep edited values for this strategy's params; drop another strategy's.
				const defaults = res.default_params ?? {};
				paramsDraft = { ...defaults, ...Object.fromEntries(Object.entries(paramsDraft).filter(([key]) => key in defaults)) };
			} else {
				customStatus = 'failed';
				if (customErrors.length === 0) customErrors = ['Strategy failed validation.'];
			}
		} catch (err) {
			customStatus = 'failed';
			customErrors = [err instanceof Error ? err.message : 'Failed to validate strategy'];
		}
	}

	// ---- AI assist: draft a strategy, or change the current one ------------------
	let aiOpen = false;
	let aiPrompt = '';
	let aiLoading = false;
	let editLoading = false;
	let aiError = '';
	let aiProvider: string | null = null;
	let aiReadiness: IdeaReadiness | null = null;
	let aiCheckedKey = '';
	let aiChecking = false;
	let aiInput: HTMLTextAreaElement | undefined;
	let pendingEdit: { spec: RuleSpec; changes: SpecChange[]; errors: string[]; draft: string } | null = null;
	$: aiInputKey = JSON.stringify([aiPrompt, symbol, timeframe]);
	$: if (aiCheckedKey !== aiInputKey) aiReadiness = null;
	$: aiBusy = aiLoading || editLoading || aiChecking;
	$: canEdit = mode === 'visual' && !!liveSpec && liveValid;

	async function toggleAi() {
		aiOpen = !aiOpen;
		if (!aiOpen) return;
		await tick();
		aiInput?.focus();
	}
	function onAiKey(event: KeyboardEvent) {
		if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
			event.preventDefault();
			event.stopPropagation();
			void generateFromNl();
		}
	}
	async function checkInputs() {
		if (!aiPrompt.trim() || aiBusy) return;
		const key = aiInputKey;
		aiChecking = true;
		aiError = '';
		aiProvider = null;
		try {
			const report = await checkIdeaReadiness({ description: aiPrompt, symbol, timeframe });
			if (key !== aiInputKey) return;
			aiCheckedKey = key;
			aiReadiness = report;
		} catch (err) {
			if (key === aiInputKey) aiError = err instanceof Error ? err.message : 'Data check failed';
		} finally {
			aiChecking = false;
		}
	}
	async function generateFromNl() {
		if (!aiPrompt.trim() || aiBusy) return;
		const key = aiInputKey;
		const draft = draftSnapshot;
		const libraryId = currentLibraryId;
		aiLoading = true;
		aiError = '';
		aiProvider = null;
		pendingEdit = null;
		try {
			const res = await nlToSpec({ description: aiPrompt, symbol, timeframe });
			if (key !== aiInputKey || draft !== draftSnapshot || libraryId !== currentLibraryId) {
				addToast('Your draft changed during generation. The returned draft was not applied.', 'info');
				return;
			}
			aiCheckedKey = key;
			aiReadiness = res.readiness ?? null;
			aiProvider = res.provider ?? null;
			const unsupported = res.spec ? builderIncompatibility(res.spec) : null;
			if (unsupported) {
				aiError = `${unsupported} Describe the idea with simpler condition groups and generate again.`;
			} else if (res.spec) {
				currentSpec = clone(res.spec as unknown as RuleSpec);
				currentLibraryId = null;
				setMode('visual');
				if (res.valid) {
					addToast('Generated a strategy from your description — review & tweak it.', 'success');
				} else {
					aiError = (res.errors ?? []).join(' ');
					addToast('Generated a draft, but it needs fixes (see the builder warnings).', 'info');
				}
			} else {
				aiError = (res.errors ?? ['Could not generate a spec.']).join(' ');
			}
		} catch (err) {
			if (key === aiInputKey) aiError = err instanceof Error ? err.message : 'AI generation failed';
		} finally {
			aiLoading = false;
		}
	}
	/** Ask for a change to the current rules; the result is shown as a diff to accept. */
	async function editFromNl() {
		if (!aiPrompt.trim() || aiBusy || !canEdit || !liveSpec) return;
		const key = aiInputKey;
		const draft = draftSnapshot;
		const libraryId = currentLibraryId;
		const before = clone(liveSpec) as unknown as RuleSpec;
		editLoading = true;
		aiError = '';
		aiProvider = null;
		pendingEdit = null;
		try {
			const res = await nlEditSpec({ description: aiPrompt, spec: liveSpec, symbol, timeframe });
			if (key !== aiInputKey || draft !== draftSnapshot || libraryId !== currentLibraryId) {
				addToast('Your draft changed while the AI worked. Its change was not applied.', 'info');
				return;
			}
			aiProvider = res.provider ?? null;
			if (!res.spec) {
				aiError = (res.errors ?? ['The AI could not change the strategy.']).join(' ');
				return;
			}
			const unsupported = builderIncompatibility(res.spec);
			if (unsupported) {
				aiError = `${unsupported} Describe the change more simply and try again.`;
				return;
			}
			const proposed = res.spec as unknown as RuleSpec;
			const changes = diffSpecs(before, proposed);
			if (!changes.length) {
				aiError = 'The AI returned the same rules. Try describing the change differently.';
				return;
			}
			pendingEdit = { spec: proposed, changes, errors: res.valid ? [] : res.errors ?? [], draft };
		} catch (err) {
			if (key === aiInputKey) aiError = err instanceof Error ? err.message : 'AI change failed';
		} finally {
			editLoading = false;
		}
	}
	function applyEdit() {
		if (!pendingEdit) return;
		if (pendingEdit.draft !== draftSnapshot) {
			addToast('Your draft changed since the AI proposed this. Ask again.', 'info');
			pendingEdit = null;
			return;
		}
		currentSpec = clone(pendingEdit.spec);
		pendingEdit = null;
		addToast('Applied the change. Ctrl+Z undoes it.', 'success');
	}

	// Execution settings
	let showAdvanced = false;
	let initialCapital = 10000;
	let feeBps = 10;
	let slippageBps = 5;
	let leverage = 1;
	let tradeMode: 'long_only' | 'short_only' | 'both' = 'long_only';
	type SizingMode = 'full' | 'fraction' | 'fixed' | 'atr' | 'kelly';
	let sizingMode: SizingMode = 'full';
	let riskPerTrade = 0.02;
	let fixedSize = 1000;
	let atrStopMultiplier = 2;
	let kellyMultiplier = 0.5;
	let kellyLookback = 100;
	let stopLossPct: number | null = null;
	let takeProfitPct: number | null = null;
	let trailingStopPct: number | null = null;
	let timeStopBars: number | null = null;
	// Stops are positive; an empty, zero or negative entry means none.
	$: numberOrNull = (v: string) => {
		const n = Number(v);
		return v.trim() === '' || !(n > 0) ? null : n;
	};
	$: estimatedBars = estimateBarCount(startDate, endDate, timeframe);
	$: hasExitControl = [stopLossPct, takeProfitPct, trailingStopPct, timeStopBars].some((v) => v != null);
	// Full equity without an exit control is no active profile, so the engine
	// sizes by its default instead: 1% risk against a 2x ATR stop it places
	// itself (sizing.normalize_execution_controls, sizing.default_controls).
	$: fullFallsBack = sizingMode === 'full' && !hasExitControl;
	$: riskPct = +(Number(riskPerTrade) * 100).toFixed(2);
	$: executionSummary = [
		fullFallsBack ? 'Default sizing: 1% risk, 2× ATR stop'
			: sizingMode === 'full' ? 'Full equity'
			: sizingMode === 'fraction' ? `${riskPct}% risk per trade`
			: sizingMode === 'fixed' ? `${Number(fixedSize).toLocaleString()} per trade`
			: sizingMode === 'atr' ? `${riskPct}% risk, ${atrStopMultiplier}× ATR stop`
			: 'Kelly sizing',
		stopLossPct != null ? `stop ${stopLossPct}%` : '',
		takeProfitPct != null ? `target ${takeProfitPct}%` : '',
		trailingStopPct != null ? `trail ${trailingStopPct}%` : '',
		timeStopBars != null ? `${timeStopBars}-bar time stop` : '',
		`${leverage}× leverage`,
	].filter(Boolean).join(' · ');

	async function showExecution() {
		showAdvanced = true;
		await tick();
		document.getElementById('sc-execution')?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
	}

	// The execution settings a backtest takes; the preview simulates the same.
	let executionRequest: ExecutionRequestFields;
	$: executionRequest = {
		initial_capital: initialCapital,
		fee_bps: feeBps,
		slippage_bps: slippageBps,
		leverage,
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

	// Market window
	const WINDOW_PRESETS = DATE_RANGE_PRESETS.filter((p) => ['3m', '6m', '1y', '2y', '3y', '5y'].includes(p.id));
	$: activePreset = inferDateRangePreset(startDate, endDate);
	function applyWindow(id: (typeof WINDOW_PRESETS)[number]['id']) {
		({ startDate, endDate } = resolveDateRangePreset(id));
	}

	// ---- Live preview --------------------------------------------------------------
	let previewCtx: PreviewChartContext | null = null;
	/** The rules the shown preview ran, which label its chart and trades. */
	let previewSpec: Record<string, unknown> | null = null;
	let previewLoading = false;
	let previewError = '';
	let previewKey = '';
	let previewSeq = 0;
	let previewTimer: ReturnType<typeof setTimeout> | undefined;
	let fitToken = 0;
	let showRuleShading = true;
	onDestroy(() => clearTimeout(previewTimer));

	$: exitReasons = Object.entries(previewCtx?.exit_reasons ?? {}).sort((a, b) => b[1] - a[1]);
	// The chart shows an older draft while the builder has errors.
	$: previewStale = !!previewCtx && !liveValid;
	$: previewTrades = previewCtx?.trades ?? [];
	$: chartLabels = specSeriesLabels(previewSpec, metaByKind);
	$: chartThresholds = specThresholds(previewSpec, metaByKind);
	$: previewKnobs = ((previewSpec?.params ?? {}) as Record<string, number>);
	$: paneCount = new Set((previewCtx?.sub_indicators ?? []).map((line) => String(line.group ?? line.name))).size;

	// Versions of the rules previewed this session, for the Versions tab.
	const MAX_VARIANTS = 60;
	let variants: Variant[] = [];
	let versionCount = 0;
	$: currentVariantKey = liveSpec ? hashSpec(liveSpec) : '';

	// Every result the author has looked at this session: each version of the
	// rules on each market, and every stress-test, heatmap and market-grid cell.
	// Their count is the number of trials the deflated Sharpe charges for.
	let seenResults = new Set<string>();
	$: trials = Math.max(1, seenResults.size);
	let lastPreviewTrials = 0;
	function markSeen(keys: string[]) {
		const before = seenResults.size;
		for (const key of keys) seenResults.add(key);
		if (seenResults.size !== before) seenResults = seenResults;
	}
	/** After a batch of results, refresh the preview so its deflated Sharpe counts them. */
	function recountTrials() {
		if (Math.max(1, seenResults.size) !== lastPreviewTrials) schedulePreview();
	}

	function recordVariant(spec: Record<string, unknown>, ctx: PreviewChartContext) {
		const key = hashSpec(spec);
		const stats = {
			ins: ctx.vitals?.in_sample ?? null,
			oos: ctx.vitals?.out_of_sample ?? null,
			dsr: ctx.vitals?.deflated_sharpe?.probability ?? null,
		};
		const existing = variants.find((v) => v.key === key);
		if (existing) {
			Object.assign(existing, stats, { at: Date.now() });
			variants = variants;
		} else {
			versionCount += 1;
			variants = [...variants, { key, n: versionCount, spec: clone(spec) as unknown as RuleSpec, at: Date.now(), ...stats }].slice(-MAX_VARIANTS);
		}
	}
	function restoreVariant(key: string) {
		const variant = variants.find((v) => v.key === key);
		if (!variant) return;
		currentSpec = clone(variant.spec);
		addToast(`Restored v${variant.n}. Ctrl+Z goes back.`, 'info');
	}

	function previewRequest(spec: Record<string, unknown>, trialCount: number): PreviewRequest {
		return {
			spec,
			symbol: symbol.trim(),
			timeframe,
			start: startDate,
			end: endDate,
			trade_mode: effectiveTradeMode,
			name: strategyName,
			trials: trialCount,
			...previewExecution,
		};
	}

	function schedulePreview() {
		clearTimeout(previewTimer);
		previewTimer = setTimeout(runPreview, 500);
	}
	async function runPreview() {
		if (mode !== 'visual' || !liveValid || !liveSpec) return;
		// Only the latest request may land; an older, slower one would show a stale draft.
		const seq = ++previewSeq;
		const spec = liveSpec;
		const seen = resultKey(spec, symbol, timeframe);
		const trialsSent = Math.max(1, seenResults.size + (seenResults.has(seen) ? 0 : 1));
		previewLoading = true;
		previewError = '';
		try {
			const ctx = await previewStrategyChart(previewRequest(spec, trialsSent));
			if (seq !== previewSeq) return;
			const keepEntry = previewTrades.find((t) => t.n === selectedTrade)?.entry_time;
			previewCtx = ctx;
			previewSpec = spec;
			markSeen([seen]);
			lastPreviewTrials = trialsSent;
			recordVariant(spec, ctx);
			// Keep the inspected trade when it still exists; otherwise show the latest.
			const trades = ctx.trades ?? [];
			selectedTrade = (keepEntry && trades.find((t) => t.entry_time === keepEntry)?.n) || (trades[trades.length - 1]?.n ?? null);
			fitToken += 1;
		} catch (err) {
			if (seq === previewSeq) previewError = err instanceof Error ? err.message : 'Preview failed';
		} finally {
			if (seq === previewSeq) previewLoading = false;
		}
	}
	// A blank or out-of-range number would fail the whole preview request; the
	// preview leaves it out and Run Backtest reports it.
	let previewExecution: ExecutionRequestFields;
	$: previewExecution = Object.fromEntries(
		Object.entries(executionRequest).filter(([key, value]) =>
			typeof value === 'number'
				? Number.isFinite(value) && (value > 0 || (value === 0 && key.endsWith('_bps')))
				: value != null)
	);
	// Auto-refresh the preview when the visual spec, market scope or execution settings change.
	$: if (mode === 'visual' && liveValid && liveSpec) {
		const key = JSON.stringify({ s: liveSpec, symbol, timeframe, startDate, endDate, tm: effectiveTradeMode, x: previewExecution });
		if (key !== previewKey) {
			previewKey = key;
			schedulePreview();
		}
	}

	// ---- Inspecting trades -----------------------------------------------------------
	type Tab = 'trade' | 'trades' | 'stress' | 'heatmap' | 'markets' | 'variants' | 'result';
	let tab: Tab = 'trade';
	let selectedTrade: number | null = null;
	let focusToken = 0;
	$: selected = previewTrades.find((t) => t.n === selectedTrade) ?? null;
	function selectTrade(n: number | null, { focus = false } = {}) {
		selectedTrade = n;
		if (n == null) return;
		tab = 'trade';
		if (focus) focusToken += 1;
	}
	function stepTrade(delta: number) {
		const index = previewTrades.findIndex((t) => t.n === selectedTrade);
		const next = previewTrades[index + delta];
		if (next) selectTrade(next.n, { focus: true });
	}

	// ---- Stress test -------------------------------------------------------------------
	let stressResult: SensitivityResult | null = null;
	let stressLoading = false;
	let stressError = '';
	let stressKey = '';
	$: stressStale = !!stressResult && stressKey !== previewKey;
	async function runStressTest() {
		if (mode !== 'visual' || !liveValid || !liveSpec || stressLoading) return;
		const key = previewKey;
		const spec = liveSpec;
		const market = [symbol, timeframe] as const;
		stressLoading = true;
		stressError = '';
		try {
			stressResult = await stressTestStrategy(previewRequest(spec, trials));
			stressKey = key;
			markSeen(stressResult.knobs.flatMap((knob) => knob.variants.map((variant) =>
				resultKey(withKnobs(spec, [[{ target: knob.target, name: knob.name, indicator: knob.indicator ?? null }, variant.value]]), ...market))));
			recountTrials();
		} catch (err) {
			stressError = err instanceof Error ? err.message : 'Stress test failed';
		} finally {
			stressLoading = false;
		}
	}

	/** What a heatmap or market grid was computed for, apart from what it varies. */
	function toolContext(spec: unknown, market: [string, string] | null, start: string, end: string, tradeMode: string, execution: unknown): string {
		return JSON.stringify({ s: spec, m: market && [market[0].trim().toUpperCase(), market[1]], w: [start, end], tm: tradeMode, x: execution });
	}

	// ---- Parameter heatmap: rows (or, with one axis, values) in three requests ------------
	let heatmapView: HeatmapView | null = null;
	let heatmapContext = '';
	let heatmapController: AbortController | null = null;
	$: knobOptions = specKnobs(liveSpec, metaByKind);
	$: heatmapAxes = heatmapView ? [heatmapView.x, ...(heatmapView.y ? [heatmapView.y] : [])].map(knobRef) : [];
	$: heatmapStale = !!heatmapView && heatmapContext !== toolContext(withoutKnobs(liveSpec, heatmapAxes), [symbol, timeframe],
		startDate, endDate, effectiveTradeMode, previewExecution);
	$: heatmapCurrent = heatmapView
		? { x: knobValue(liveSpec, knobRef(heatmapView.x)), y: heatmapView.y ? knobValue(liveSpec, knobRef(heatmapView.y)) : null }
		: { x: null, y: null };
	// A change the grid does not cover makes the rest of the run moot.
	$: if (heatmapStale && heatmapView?.status === 'running') heatmapController?.abort();

	function knobRef(axis: HeatmapAxisRequest) {
		return { target: axis.target, name: axis.name, indicator: axis.indicator ?? null };
	}
	async function runHeatmap(axes: { x: HeatmapAxisRequest; y: HeatmapAxisRequest | null }) {
		if (mode !== 'visual' || !liveValid || !liveSpec) return;
		heatmapController?.abort();
		const controller = new AbortController();
		heatmapController = controller;
		const spec = liveSpec;
		const market: [string, string] = [symbol, timeframe];
		const base = previewRequest(spec, trials);
		const meta = (axis: HeatmapAxisRequest) => {
			const knob = knobOptions.find((k) => k.target === axis.target && k.name === axis.name && k.indicator === (axis.indicator ?? null));
			return { ...axis, label: knob?.label ?? axis.name, integer: knob?.integer ?? false, min: knob?.min ?? null };
		};
		const units = axes.y
			? chunk(axes.y.values, 3).map((ys) => ({ xs: axes.x.values, ys }))
			: chunk(axes.x.values, 3).map((xs) => ({ xs, ys: null as number[] | null }));
		heatmapView = {
			x: meta(axes.x), y: axes.y ? meta(axes.y) : null, cells: {}, done: 0,
			total: axes.x.values.length * (axes.y ? axes.y.values.length : 1), status: 'running', warnings: [],
		};
		heatmapContext = toolContext(withoutKnobs(spec, [axes.x, ...(axes.y ? [axes.y] : [])].map(knobRef)), market,
			startDate, endDate, effectiveTradeMode, previewExecution);
		await runPool(units, 3, async ({ xs, ys }) => {
			const size = xs.length * (ys ? ys.length : 1);
			try {
				const result = await heatmapStrategy({
					...base, x: { ...axes.x, values: xs }, y: axes.y && ys ? { ...axes.y, values: ys } : null,
				}, controller.signal);
				if (heatmapController !== controller || !heatmapView) return;
				for (const cell of result.cells) heatmapView.cells[cellKey(cell.x, cell.y)] = cell;
				heatmapView.done += size;
				heatmapView.warnings = [...new Set([...heatmapView.warnings, ...result.warnings])];
				heatmapView = heatmapView;
				markSeen(result.cells.filter((cell) => !cell.error).map((cell) => {
					const changes: Array<[ReturnType<typeof knobRef>, number | null]> = [[knobRef(axes.x), cell.x]];
					if (axes.y) changes.push([knobRef(axes.y), cell.y]);
					return resultKey(withKnobs(spec, changes), ...market);
				}));
			} catch (err) {
				if (controller.signal.aborted || heatmapController !== controller || !heatmapView) return;
				heatmapView.done += size;
				heatmapView.warnings = [...new Set([...heatmapView.warnings, err instanceof Error ? err.message : 'A heatmap row failed.'])];
				heatmapView = heatmapView;
			}
		}, controller.signal);
		if (heatmapController !== controller || !heatmapView) return;
		heatmapView.status = controller.signal.aborted ? 'cancelled' : 'done';
		heatmapView = heatmapView;
		heatmapController = null;
		recountTrials();
	}
	function adoptHeatmapCell({ x, y }: { x: number; y: number | null }) {
		if (!heatmapView || !liveSpec) return;
		const changes: Array<[ReturnType<typeof knobRef>, number | null]> = [[knobRef(heatmapView.x), x]];
		if (heatmapView.y) changes.push([knobRef(heatmapView.y), y]);
		currentSpec = clone(withKnobs(liveSpec, changes)) as unknown as RuleSpec;
	}

	// ---- Market grid: the markets in three requests -------------------------------------
	let marketView: MarketView | null = null;
	let marketContext = '';
	let marketController: AbortController | null = null;
	let availability: Map<string, Set<string>> | null = null;
	let marketSymbolOptions: string[] = [];
	let datasetsRequested = false;
	$: marketStale = !!marketView && marketContext !== toolContext(liveSpec, null, startDate, endDate, effectiveTradeMode, previewExecution);
	$: if (marketStale && marketView?.status === 'running') marketController?.abort();
	$: if (tab === 'markets') void loadAvailability();

	async function loadAvailability() {
		if (datasetsRequested) return;
		datasetsRequested = true;
		try {
			const datasets = await getDatasets();
			availability = marketAvailability(datasets);
			marketSymbolOptions = [...new Set(datasets.map((d) => String(d.symbol).toUpperCase())
				.filter((s) => ['USDT', 'USD', 'USDC'].includes(s.split('/')[1] ?? '')))].sort();
		} catch {
			availability = new Map();
		}
	}
	async function runMarkets({ symbols, timeframes }: { symbols: string[]; timeframes: string[] }) {
		if (mode !== 'visual' || !liveValid || !liveSpec) return;
		marketController?.abort();
		const controller = new AbortController();
		marketController = controller;
		const spec = liveSpec;
		const base = previewRequest(spec, trials);
		const cells = symbols.flatMap((s) => timeframes.map((t) => ({ symbol: s, timeframe: t })));
		marketView = { symbols, timeframes, rows: {}, done: 0, total: cells.length, status: 'running', warnings: [] };
		marketContext = toolContext(spec, null, startDate, endDate, effectiveTradeMode, previewExecution);
		// Markets without local data are reported at once and never requested (a request would download them).
		const runnable = cells.filter((cell) => {
			if (!availability || hasLocalData(availability, cell.symbol, cell.timeframe)) return true;
			marketView!.rows[marketKey(cell.symbol, cell.timeframe)] = { ...cell, status: 'no_data',
				message: `No local ${cell.timeframe} data for ${cell.symbol}. Collect it on the Data page.` };
			marketView!.done += 1;
			return false;
		});
		marketView = marketView;
		await runPool(chunk(runnable, 3), 3, async (group) => {
			try {
				const result = await compareMarkets({ ...base, markets: group }, controller.signal);
				if (marketController !== controller || !marketView) return;
				const seen: string[] = [];
				group.forEach((cell, i) => {
					const row = result.rows[i] ?? { ...cell, status: 'error' as const, message: result.warnings[0] ?? 'No result.' };
					marketView!.rows[marketKey(cell.symbol, cell.timeframe)] = row;
					if (row.status === 'ok') seen.push(resultKey(spec, cell.symbol, cell.timeframe));
				});
				marketView.done += group.length;
				marketView.warnings = [...new Set([...marketView.warnings, ...result.warnings])];
				marketView = marketView;
				markSeen(seen);
			} catch (err) {
				if (controller.signal.aborted || marketController !== controller || !marketView) return;
				for (const cell of group) {
					marketView.rows[marketKey(cell.symbol, cell.timeframe)] = { ...cell, status: 'error',
						message: err instanceof Error ? err.message : 'These markets failed.' };
				}
				marketView.done += group.length;
				marketView = marketView;
			}
		}, controller.signal);
		if (marketController !== controller || !marketView) return;
		marketView.status = controller.signal.aborted ? 'cancelled' : 'done';
		marketView = marketView;
		marketController = null;
		recountTrials();
	}
	function pickMarket({ symbol: next, timeframe: nextTimeframe }: { symbol: string; timeframe: string }) {
		symbol = next;
		timeframe = nextTimeframe;
	}

	// Import (creates a new lifecycle container from an export envelope)
	let showImportDialog = false;
	function onStrategyImported(result: StrategyImportResult) {
		showImportDialog = false;
		if (result.ok && result.strategy_id) {
			void goto(`/lab/strategy/${encodeURIComponent(result.strategy_id)}`);
		}
	}

	// Library
	let library: LibraryStrategy[] = [];
	let libraryLoading = false;
	let currentLibraryId: string | null = null;
	let saving = false;

	// Launcher: templates, the library and the system's strategies in one place
	let launcherOpen = false;
	let launcherTab: 'library' | 'templates' | 'prebuilt' | 'app' = 'library';
	let prebuilt: Strategy[] = [];
	let appStrategies: Strategy[] = [];
	let includeAppGenerated = false;
	let appLoading = false;
	let nonEditableNotice = '';

	function openLauncher(tab: typeof launcherTab = 'library') {
		launcherTab = tab;
		launcherOpen = true;
		void loadLibrary();
	}

	async function loadLibrary() {
		libraryLoading = true;
		try {
			library = await listStrategyLibrary();
		} catch {
			library = [];
		} finally {
			libraryLoading = false;
		}
	}

	async function loadPrebuilt() {
		try {
			const res = await getPrebuiltStrategies();
			// rule_engine is the engine itself, not a selectable strategy.
			prebuilt = res.strategies.filter((s) => (s.api_name || s.name) !== 'rule_engine');
		} catch {
			prebuilt = [];
		}
	}

	async function toggleAppGenerated() {
		includeAppGenerated = !includeAppGenerated;
		if (includeAppGenerated && appStrategies.length === 0) {
			appLoading = true;
			try {
				appStrategies = (await getStrategies()).strategies;
			} catch {
				appStrategies = [];
			} finally {
				appLoading = false;
			}
		}
	}

	function findStrategy(list: Strategy[], key: string): Strategy | undefined {
		return list.find((s) => (s.api_name || s.name) === key);
	}

	async function openSystemStrategy(id: string, meta?: Strategy) {
		const displayName = meta?.name || id;
		nonEditableNotice = '';
		launcherOpen = false;
		try {
			const detail = await getSystemStrategyDetail(id);
			const spec = (detail.params && typeof detail.params === 'object' ? (detail.params as Record<string, unknown>).spec : null);
			const unsupported = spec && typeof spec === 'object' ? builderIncompatibility(spec) : null;
			if (unsupported) {
				nonEditableNotice = `“${detail.name || displayName}” can’t be opened in the visual builder. ${unsupported}`;
				return;
			}
			if (spec && typeof spec === 'object') {
				resetSession();
				currentSpec = clone(spec as unknown as RuleSpec);
				symbol = detail.symbol || symbol;
				timeframe = detail.timeframe || timeframe;
				strategyName = `${detail.name || displayName} (copy)`;
				strategyDescription = '';
				restoreExecution(detail.params || {});
				currentLibraryId = null; // editing a system strategy → Save creates a new library entry
				setMode('visual');
				addToast(`Loaded “${detail.name || displayName}” — edits save as a new strategy.`, 'info');
				return;
			}
			nonEditableNotice = `“${detail.name || displayName}” is a built-in ${detail.type || ''} strategy — its logic isn’t an editable rule spec. Run or tune it in Manual Backtest, or build an equivalent here.`;
		} catch {
			nonEditableNotice = `“${displayName}” is a built-in strategy — its logic isn’t an editable rule spec. Run or tune it in Manual Backtest, or build an equivalent here.`;
		}
	}

	$: executionProfile = {
		sizing_mode: sizingMode, risk_per_trade: riskPerTrade, fixed_size: fixedSize,
		atr_stop_multiplier: atrStopMultiplier, kelly_multiplier: kellyMultiplier,
		kelly_lookback: kellyLookback, stop_loss_pct: stopLossPct,
		take_profit_pct: takeProfitPct, trailing_stop_pct: trailingStopPct, time_stop_bars: timeStopBars,
	};
	$: savedParams = {
		...(mode === 'code' ? paramsDraft : {}), execution_profile: executionProfile,
		leverage, trade_mode: effectiveTradeMode,
		_creator_context: { start: startDate, end: endDate, initial_capital: initialCapital, fee_bps: feeBps, slippage_bps: slippageBps },
	};
	$: draftPayload = {
		name: strategyName.trim() || 'My Strategy', kind: mode === 'code' ? 'code' as const : 'visual' as const,
		description: strategyDescription, code: mode === 'code' ? customCode : null,
		spec: mode === 'code' ? null : liveSpec, symbol: symbol.trim(), timeframe, params: savedParams,
	};
	$: savedEntry = library.find((entry) => entry.id === currentLibraryId);
	$: draftSnapshot = editableSnapshot(draftPayload);
	$: dirty = !savedEntry || draftSnapshot !== editableSnapshot(savedEntry);
	function payloadForSave() { return draftPayload; }

	function restoreExecution(params: Record<string, unknown>) {
		const profile = (params.execution_profile || {}) as Record<string, number | string | null>;
		const context = (params._creator_context || {}) as Record<string, number | string>;
		sizingMode = (profile.sizing_mode || 'full') as SizingMode;
		riskPerTrade = Number(profile.risk_per_trade ?? 0.02);
		fixedSize = Number(profile.fixed_size ?? 1000);
		atrStopMultiplier = Number(profile.atr_stop_multiplier ?? 2);
		kellyMultiplier = Number(profile.kelly_multiplier ?? 0.5);
		kellyLookback = Number(profile.kelly_lookback ?? 100);
		stopLossPct = profile.stop_loss_pct == null ? null : Number(profile.stop_loss_pct);
		takeProfitPct = profile.take_profit_pct == null ? null : Number(profile.take_profit_pct);
		trailingStopPct = profile.trailing_stop_pct == null ? null : Number(profile.trailing_stop_pct);
		timeStopBars = profile.time_stop_bars == null ? null : Number(profile.time_stop_bars);
		leverage = Number(params.leverage ?? 1);
		tradeMode = (params.trade_mode || 'long_only') as typeof tradeMode;
		initialCapital = Number(context.initial_capital ?? 10000);
		feeBps = Number(context.fee_bps ?? 10);
		slippageBps = Number(context.slippage_bps ?? 5);
		startDate = String(context.start ?? defaultRange.startDate);
		endDate = String(context.end ?? defaultRange.endDate);
	}

	let savePromptOpen = false;
	let saveAsName = '';

	function requestSave() {
		if (saving) return;
		if (mode === 'visual' && !liveValid) {
			addToast(liveErrors[0] || 'Complete the strategy before saving.', 'error');
			return;
		}
		if (mode === 'code' && !customCode.trim()) {
			addToast('Write some strategy code before saving.', 'error');
			return;
		}
		saveAsName = currentLibraryId ? `${strategyName} (copy)` : strategyName || 'My Strategy';
		savePromptOpen = true;
	}

	async function doSave(overwrite: boolean) {
		saving = true;
		try {
			const payload = payloadForSave();
			let row: LibraryStrategy;
			if (overwrite && currentLibraryId) {
				row = await updateLibraryStrategy(currentLibraryId, { ...payload, expected_version: savedEntry?.version });
				addToast(`Overwrote “${row.name}”`, 'success');
			} else {
				row = await createLibraryStrategy({ ...payload, name: saveAsName.trim() || payload.name });
				currentLibraryId = row.id;
				strategyName = row.name;
				addToast(`Saved “${row.name}” as a new strategy`, 'success');
			}
			savePromptOpen = false;
			await loadLibrary();
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Save failed', 'error');
		} finally {
			saving = false;
		}
	}

	function openLibraryEntry(entry: LibraryStrategy) {
		const unsupported = entry.kind === 'code' ? null : builderIncompatibility(entry.spec);
		if (unsupported) {
			addToast(`“${entry.name}” can’t be opened in the visual builder. ${unsupported}`, 'error');
			return;
		}
		resetSession();
		nonEditableNotice = '';
		strategyName = entry.name;
		strategyDescription = entry.description || '';
		symbol = entry.symbol || symbol;
		timeframe = entry.timeframe || timeframe;
		currentLibraryId = entry.id;
		restoreExecution(entry.params || {});
		if (entry.kind === 'code') {
			mode = 'code';
			customCode = entry.code || customTemplate();
			customStatus = 'idle';
			customLoadedName = '';
			paramsDraft = Object.fromEntries(Object.entries(entry.params || {}).filter(([key]) => !['execution_profile', '_creator_context', 'leverage', 'trade_mode'].includes(key)));
		} else {
			mode = 'visual';
			currentSpec = clone((entry.spec as unknown as RuleSpec) ?? null);
		}
		launcherOpen = false;
		addToast(`Opened “${entry.name}”`, 'info');
	}

	async function duplicateEntry(entry: LibraryStrategy, ev: Event) {
		ev.stopPropagation();
		try {
			await duplicateLibraryStrategy(entry.id);
			await loadLibrary();
			addToast(`Duplicated “${entry.name}”`, 'success');
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Duplicate failed', 'error');
		}
	}

	async function deleteEntry(entry: LibraryStrategy, ev: Event) {
		ev.stopPropagation();
		if (!confirm(`Delete “${entry.name}” from your library?`)) return;
		try {
			await deleteLibraryStrategy(entry.id);
			if (currentLibraryId === entry.id) currentLibraryId = null;
			await loadLibrary();
			addToast(`Deleted “${entry.name}”`, 'info');
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Delete failed', 'error');
		}
	}

	let forging = false;
	async function forgeEntry(entry: LibraryStrategy, ev: Event) {
		ev.stopPropagation();
		if (forging) return;
		if (entry.id === currentLibraryId && dirty) {
			addToast('Save your current changes before sending to Forge.', 'error');
			return;
		}
		forging = true;
		try {
			const res = await sendLibraryStrategyToForge(entry.id, entry.version);
			await loadLibrary();
			const link = `/lab/strategy/${encodeURIComponent(res.forge.strategy_id)}`;
			if (res.already_in_forge) {
				addToast(`“${entry.name}” is already in the Forge as ${res.forge.display_id || res.forge.strategy_id} (${res.forge.stage})`, 'info', link);
			} else {
				addToast(`Sent “${entry.name}” to the Forge (${res.forge.stage})`, 'success', link);
			}
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Send to Forge failed', 'error');
		} finally { forging = false; }
	}

	// Backtest
	type SubmitStatus = 'idle' | 'submitting' | 'failed';
	let submitStatus: SubmitStatus = 'idle';
	let submitError = '';
	let submitWarning = '';
	let resultLoading = false;
	let inlineResult: BacktestResult | null = null;
	let resultSnapshot = '';
	$: if (resultSnapshot && resultSnapshot !== draftSnapshot) { inlineResult = null; lastStrategyId = ''; lastResultId = ''; }
	let lastResultId = '';
	let lastStrategyId = '';
	$: busy = submitStatus === 'submitting';

	function validateRun(): string | null {
		if (mode === 'visual' && !liveValid) return liveErrors[0] || 'Complete the visual strategy first.';
		if (mode === 'code' && customStatus !== 'loaded') return 'Validate & load your custom strategy first.';
		if (!symbol.trim()) return 'Symbol is required.';
		if (startDate && endDate && startDate >= endDate) return 'Start date must be before end date.';
		if (!(initialCapital > 0)) return 'Initial capital must be greater than 0.';
		if (!(feeBps >= 0 && slippageBps >= 0)) return 'Fee and slippage must be 0 or more.';
		if (!(leverage >= 1 && leverage <= 125)) return 'Leverage must be between 1 and 125.';
		if (sizingMode === 'fraction' && stopLossPct == null && trailingStopPct == null)
			return 'Fraction sizing needs a Stop Loss % or Trailing Stop %.';
		if (sizingMode === 'kelly')
			return 'Kelly sizing sizes each trade from the strategy’s own closed trades. A backtest starts with none, so it would open no trades. Choose another sizing mode.';
		if (estimatedBars != null && estimatedBars > BAR_CAP)
			return `This window is ~${estimatedBars.toLocaleString()} bars; the engine caps at ${BAR_CAP.toLocaleString()}.`;
		return null;
	}

	function buildRequest() {
		const isVisual = mode === 'visual';
		const strategyId = isVisual ? `${RULE_ENGINE_TYPE}__${hashSpec(liveSpec)}` : customLoadedName;
		const strategyName_ = isVisual ? RULE_ENGINE_TYPE : customLoadedName;
		const params = isVisual
			? { spec: liveSpec, _asset: baseAsset }
			: Object.keys(paramsDraft).length > 0
				? paramsDraft
				: undefined;
		return {
			strategy_id: strategyId,
			strategy_name: strategyName_,
			strategy_version: 'custom',
			symbol: symbol.trim(),
			timeframe,
			start: startDate,
			end: endDate,
			params,
			preserve_result: true,
			trade_mode: effectiveTradeMode,
			allow_shorting: effectiveTradeMode !== 'long_only',
			...executionRequest,
		};
	}

	// Only attach evidence to the exact saved revision submitted for this run.
	async function persistTestedStatus(libraryId: string, resultId: string, version: number) {
		try {
			const updated = await updateLibraryStrategy(libraryId, { status: 'tested', last_result_id: resultId, expected_version: version });
			library = library.map((entry) => entry.id === libraryId && entry.version === version ? updated : entry);
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Result was not attached to the saved revision.', 'warning');
		}
	}

	async function runBacktest() {
		if (busy || resultLoading) return;
		const error = validateRun();
		if (error) {
			submitError = error;
			return;
		}
		submitStatus = 'submitting';
		submitError = '';
		submitWarning = '';
		inlineResult = null;
		const request = buildRequest();
		const testedEntry = !dirty && savedEntry ? { id: savedEntry.id, version: savedEntry.version } : null;
		const testedSnapshot = draftSnapshot;
		try {
			const job = await submitBacktest(request);
			lastStrategyId = request.strategy_id;
			if (job.warning) submitWarning = job.warning;
			submitStatus = 'idle';
			addToast(`Backtest ${job.status === 'succeeded' ? 'completed' : 'queued'}`, job.status === 'succeeded' ? 'success' : 'info');
			if (job.result_id) {
				lastResultId = job.result_id;
				if (testedEntry) void persistTestedStatus(testedEntry.id, job.result_id, testedEntry.version);
				tab = 'result';
				resultLoading = true;
				try {
					const result = await getResult(job.result_id);
					if (draftSnapshot === testedSnapshot) { inlineResult = result; resultSnapshot = testedSnapshot; }
				} catch {
					inlineResult = null;
				} finally {
					resultLoading = false;
				}
				queueMicrotask(() => document.getElementById('sc-results')?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' }));
			}
		} catch (err) {
			submitStatus = 'failed';
			submitError = err instanceof Error ? err.message : 'Backtest submission failed';
		}
	}

	function openFullReport() {
		if (lastStrategyId) goto(`/lab/strategy/${encodeURIComponent(lastStrategyId)}?returnTo=/strategy-creator`);
	}

	$: tabs = [
		{ key: 'trade' as Tab, label: selected ? `Why · #${selected.n}` : 'Why' },
		{ key: 'trades' as Tab, label: `Trades${previewTrades.length ? ` (${previewTrades.length})` : ''}` },
		{ key: 'stress' as Tab, label: 'Stress test' },
		{ key: 'heatmap' as Tab, label: 'Heatmap' },
		{ key: 'markets' as Tab, label: 'Markets' },
		{ key: 'variants' as Tab, label: `Versions (${variants.length})` },
		{ key: 'result' as Tab, label: 'Backtest' },
	];

	// ---- Keyboard ------------------------------------------------------------------
	function typing(target: EventTarget | null): boolean {
		const el = target as HTMLElement | null;
		return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
	}
	function onKeydown(event: KeyboardEvent) {
		if (launcherOpen || savePromptOpen || showImportDialog) return;
		const mod = event.ctrlKey || event.metaKey;
		const key = event.key.toLowerCase();
		if (mod && key === 'enter') {
			event.preventDefault();
			void runBacktest();
		} else if (mod && key === 's') {
			event.preventDefault();
			requestSave();
		} else if (mod && key === 'o') {
			event.preventDefault();
			openLauncher();
		} else if (mod && (key === 'z' || key === 'y') && !typing(event.target)) {
			event.preventDefault();
			if (key === 'y' || event.shiftKey) redo();
			else undo();
		} else if (!mod && (key === 'arrowleft' || key === 'arrowright') && selectedTrade != null && !typing(event.target)) {
			event.preventDefault();
			stepTrade(key === 'arrowleft' ? -1 : 1);
		}
	}

	onMount(async () => {
		try {
			// Symbol suggestions are non-essential: keep the empty-array fallback so the
			// page still renders, but surface the failure instead of silently showing an
			// empty dropdown with no signal.
			const [inds, syms] = await Promise.all([
				getIndicators(),
				getSymbols().catch((err) => {
					console.warn('Failed to load symbol suggestions', err);
					addToast('Could not load symbol suggestions — you can still type a symbol manually', 'warning');
					return [] as string[];
				}),
			]);
			indicators = inds;
			symbolSuggestions = syms;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load indicator catalog';
		}
		loadLibrary();
		loadPrebuilt();
		// Seed with a template so the page is productive on first load.
		applyTemplate(STRATEGY_TEMPLATES[0].id, { quiet: true });
	});

	const chip = 'border border-[#2a2a2a] px-2 py-1 text-[10px] uppercase tracking-wider transition-colors';
</script>

<svelte:head><title>Strategy Creator | Forven</title></svelte:head>
<svelte:window on:keydown={onKeydown} />

{#snippet resultBlock()}
	<div id="sc-results" class="scroll-mt-6 space-y-3">
		{#if submitWarning}<div class="border border-amber-900 bg-amber-500/5 px-4 py-2.5 text-sm text-amber-400">⚠ {submitWarning}</div>{/if}
		{#if resultLoading}
			<div class="p-8 text-center text-xs uppercase tracking-widest text-[#555]">Loading result…</div>
		{:else if inlineResult}
			<div class="flex items-center justify-between">
				<h2 class="text-[11px] font-bold uppercase tracking-widest text-white">Backtest result</h2>
				<button type="button" on:click={openFullReport} disabled={!lastStrategyId}
					class="terminal-button-primary text-[10px] disabled:opacity-40">Full report →</button>
			</div>
			<BacktestResultSummary result={inlineResult} />
		{:else}
			<div class="border border-dashed border-[#262626] px-3 py-6 text-center text-[12px] text-[#555]">
				Run Backtest scores the out-of-sample part of the window with these execution settings. Its result appears here.
			</div>
		{/if}
	</div>
{/snippet}

<div class="flex min-h-full flex-col lg:h-full">
	<!-- Title bar: identity, history and the main actions -->
	<header class="flex flex-wrap items-center gap-2 border-b border-[#1a1a1a] px-4 py-2">
		<span class="text-[11px] font-bold uppercase tracking-[0.2em] text-[#777]">Creator</span>
		<input bind:value={strategyName} placeholder="Strategy name" aria-label="strategy name"
			class="min-w-[10rem] max-w-[18rem] flex-1 border border-transparent bg-transparent px-1.5 py-1 text-[15px] font-bold text-white outline-none hover:border-[#2a2a2a] focus:border-white" />
		{#if currentLibraryId && dirty}
			<span class="border border-amber-800 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-amber-400" title="Save before sending to Forge">Unsaved changes</span>
		{:else if currentLibraryId && savedEntry}
			<span class="border border-[#333] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[#888]">Saved · v{savedEntry.version} · {savedEntry.status}</span>
		{:else}
			<span class="border border-[#262626] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[#555]">Draft</span>
		{/if}

		<div class="ml-2 inline-flex border border-[#333] text-[10px] uppercase tracking-wide" role="group" aria-label="editor">
			<button type="button" on:click={() => setMode('visual')} aria-pressed={mode === 'visual'}
				class="border-r border-[#333] px-3 py-1 transition-colors {mode === 'visual' ? 'bg-white text-black' : 'text-[#666] hover:text-white'}">Rules</button>
			<button type="button" on:click={() => setMode('code')} aria-pressed={mode === 'code'}
				class="px-3 py-1 transition-colors {mode === 'code' ? 'bg-white text-black' : 'text-[#666] hover:text-white'}">Python</button>
		</div>
		<button type="button" on:click={toggleAi} aria-pressed={aiOpen}
			class="{chip} {aiOpen ? 'border-white bg-white text-black' : 'text-[#aaa] hover:border-white hover:text-white'}">AI assist</button>
		<div class="inline-flex">
			<button type="button" on:click={undo} disabled={!canUndo} aria-label="Undo" title="Undo (Ctrl+Z)"
				class="border border-[#2a2a2a] px-2 py-0.5 text-[13px] text-[#aaa] hover:text-white disabled:opacity-30">↶</button>
			<button type="button" on:click={redo} disabled={!canRedo} aria-label="Redo" title="Redo (Ctrl+Shift+Z)"
				class="-ml-px border border-[#2a2a2a] px-2 py-0.5 text-[13px] text-[#aaa] hover:text-white disabled:opacity-30">↷</button>
		</div>

		<div class="ml-auto flex flex-wrap items-center gap-2">
			<button type="button" on:click={() => openLauncher('templates')} class="{chip} text-[#aaa] hover:border-white hover:text-white">New</button>
			<button type="button" on:click={() => openLauncher('library')} title="Open a saved or system strategy (Ctrl+O)"
				class="{chip} text-[#aaa] hover:border-white hover:text-white">Open…</button>
			<button type="button" on:click={requestSave} disabled={saving} title="Save (Ctrl+S)"
				class="terminal-button text-[10px]">{saving ? 'Saving…' : currentLibraryId ? 'Save' : 'Save to library'}</button>
			{#if currentLibraryId && savedEntry?.forge_strategy_id && !dirty}
				<a href={`/lab/strategy/${encodeURIComponent(savedEntry.forge_strategy_id)}`}
					class="terminal-button text-[10px]" title="This saved revision is already in the Forge">Open in Forge →</a>
			{:else}
				<button type="button" on:click={(e) => { const entry = library.find((l) => l.id === currentLibraryId); if (entry) forgeEntry(entry, e); }}
					disabled={!currentLibraryId || dirty || forging || saving || busy}
					class="terminal-button text-[10px] disabled:opacity-40"
					title={!currentLibraryId ? 'Save to your library to enable Send to Forge' : dirty ? 'Save your changes first' : 'Send this saved revision to Forge'}>Send to Forge →</button>
			{/if}
			<button type="button" on:click={runBacktest} disabled={busy || resultLoading} title="Run Backtest (Ctrl+Enter)"
				class="terminal-button-primary text-[10px] disabled:opacity-40">
				{#if busy || resultLoading}Running…{:else}Run Backtest{/if}
			</button>
		</div>
	</header>

	<!-- Market and window -->
	<div class="flex flex-wrap items-center gap-2 border-b border-[#1a1a1a] px-4 py-1.5 text-[11px]">
		<input list="sc-symbols" value={symbol} on:input={(e) => (symbol = e.currentTarget.value)} on:change={(e) => (symbol = e.currentTarget.value.trim())}
			disabled={busy} aria-label="symbol" placeholder="BTC/USDT"
			class="w-28 border border-[#2a2a2a] bg-black px-2 py-1 font-mono text-[12px] text-white outline-none focus:border-white" />
		<datalist id="sc-symbols">{#each symbolSuggestions as s}<option value={s}></option>{/each}</datalist>
		<select bind:value={timeframe} disabled={busy} aria-label="timeframe"
			class="border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white">
			{#each ORDERED_TIMEFRAME_OPTIONS as option}<option value={option.value}>{option.value}</option>{/each}
		</select>
		<div class="inline-flex" role="group" aria-label="window">
			{#each WINDOW_PRESETS as preset}
				<button type="button" on:click={() => applyWindow(preset.id)} disabled={busy}
					class="-ml-px border px-1.5 py-1 text-[10px] transition-colors first:ml-0 {activePreset === preset.id ? 'relative border-white bg-white text-black' : 'border-[#2a2a2a] text-[#777] hover:text-white'}">{preset.label}</button>
			{/each}
		</div>
		<input type="date" bind:value={startDate} max={endDate} disabled={busy} aria-label="start date"
			class="border border-[#2a2a2a] bg-black px-1.5 py-0.5 font-mono text-[11px] text-[#ccc] outline-none focus:border-white [color-scheme:dark]" />
		<span class="text-[#444]">→</span>
		<input type="date" bind:value={endDate} min={startDate} disabled={busy} aria-label="end date"
			class="border border-[#2a2a2a] bg-black px-1.5 py-0.5 font-mono text-[11px] text-[#ccc] outline-none focus:border-white [color-scheme:dark]" />
		{#if estimatedBars != null}<span class="text-[10px] {estimatedBars > BAR_CAP ? 'text-amber-400' : 'text-[#555]'}">~{estimatedBars.toLocaleString()} bars</span>{/if}
		<button type="button" on:click={showExecution} title="Execution settings"
			class="ml-auto max-w-full truncate border border-[#222] px-2 py-0.5 text-[10px] text-[#888] hover:border-[#555] hover:text-white">{executionSummary}</button>
		{#if mode === 'visual'}
			<span class="text-[10px] text-[#555]" title="Results you have looked at this session: each version of the rules on each market, and every stress-test, heatmap and market-grid cell. The deflated Sharpe charges for each.">
				{trials} result{trials === 1 ? '' : 's'} seen
			</span>
		{/if}
	</div>

	{#if loadError || nonEditableNotice || submitError}
		<div class="space-y-2 px-4 pt-3">
			{#if loadError}<div class="border border-red-900 bg-red-500/5 px-4 py-2 text-sm text-red-400" role="alert">{loadError}</div>{/if}
			{#if nonEditableNotice}
				<div class="border border-amber-900 bg-amber-500/5 px-3 py-2 text-[12px] text-amber-400">
					{nonEditableNotice}
					<a href="/backtest/new" class="ml-1 text-white underline">Open Manual Backtest →</a>
				</div>
			{/if}
			{#if submitError}<div class="border border-red-900 bg-red-500/5 px-4 py-2 text-[12px] text-red-400" role="alert">{submitError}</div>{/if}
		</div>
	{/if}

	<div class="grid min-h-0 flex-1 lg:grid-cols-[minmax(440px,5fr)_minmax(0,7fr)]">
		<!-- LEFT: what the strategy does -->
		<div class="min-h-0 space-y-3 overflow-y-auto border-[#1a1a1a] p-4 pb-24 lg:border-r">
			{#if aiOpen}
				<section class="border border-[#333] bg-[#070707]" aria-label="AI assist">
					<header class="flex items-center gap-2 border-b border-[#161616] px-3 py-1.5">
						<h3 class="text-[11px] font-bold uppercase tracking-wider text-white">AI assist</h3>
						<span class="truncate text-[10px] text-[#555]">describe a new strategy, or a change to this one</span>
						<button type="button" on:click={() => (aiOpen = false)} aria-label="Close AI assist" class="ml-auto px-1 text-[#555] hover:text-white">✕</button>
					</header>
					<div class="space-y-2 p-3">
						<textarea bind:this={aiInput} bind:value={aiPrompt} rows="3" on:keydown={onAiKey}
							placeholder="e.g. Buy when RSI drops below 30 while price is above the 200 EMA; sell above 60.  Or: only take trades when volume is above its 20-bar average."
							class="terminal-input w-full resize-y text-[13px]"></textarea>
						<div class="flex flex-wrap items-center gap-2">
							<button type="button" on:click={checkInputs} disabled={aiBusy || !aiPrompt.trim()} class="terminal-button text-[10px] disabled:opacity-40">{aiChecking ? 'Checking data…' : 'Check data'}</button>
							<button type="button" on:click={generateFromNl} disabled={aiBusy || !aiPrompt.trim()} title="Draft a new strategy (Ctrl+Enter)"
								class="terminal-button-primary text-[10px] disabled:opacity-40">{aiLoading ? 'Generating…' : 'Generate strategy'}</button>
							{#if mode === 'visual'}
								<button type="button" on:click={editFromNl} disabled={aiBusy || !aiPrompt.trim() || !canEdit}
									title={canEdit ? 'Change the current rules and review the difference' : 'Fix the rules first'}
									class="terminal-button text-[10px] disabled:opacity-40">{editLoading ? 'Changing…' : 'Change current rules'}</button>
							{/if}
							{#if aiProvider}<span class="text-[10px] text-[#555]">via {aiProvider}</span>{/if}
						</div>
						{#if aiReadiness}
							<div class="space-y-2 border border-[#333] bg-[#111] p-3 text-[12px]" role="status">
								<p class="text-white">{aiReadiness.can_generate ? 'Basic input check complete' : 'Resolve inputs before generation'}</p>
								<p class="text-[#aaa]">{symbol} / {timeframe} · Named inputs: {aiReadiness.required.join(', ') || 'Price and volume only detected'}</p>
								{#each aiReadiness.issues as issue}<p class="text-amber-400">{issue}</p>{/each}
								{#each aiReadiness.warnings as warning}<p class="text-[#999]">{warning}</p>{/each}
								<a href="/data" class="text-white underline">Review data collection →</a>
							</div>
						{/if}
						{#if aiError}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{aiError}</div>{/if}
						{#if pendingEdit}
							<div class="border border-sky-900 bg-sky-500/5 p-2.5" data-testid="ai-proposed-change">
								<div class="mb-1.5 text-[10px] uppercase tracking-wider text-sky-300">Proposed change</div>
								<div class="space-y-1 text-[11px]">
									{#each pendingEdit.changes as change}
										<div>
											<span class="{change.kind === 'added' ? 'text-emerald-400' : change.kind === 'removed' ? 'text-red-400' : 'text-amber-300'}">{change.kind}</span>
											<span class="text-[#888]">{change.area}</span>
											<span class="text-white">{change.label}</span>
											{#if change.before}<div class="ml-4 font-mono text-[#777] line-through decoration-[#555]">{change.before}</div>{/if}
											{#if change.after}<div class="ml-4 font-mono text-[#ddd]">{change.after}</div>{/if}
										</div>
									{/each}
								</div>
								{#each pendingEdit.errors as error}<div class="mt-1 text-[11px] text-amber-400">{error}</div>{/each}
								<div class="mt-2 flex gap-2">
									<button type="button" on:click={applyEdit} class="terminal-button-primary text-[10px]">Apply change</button>
									<button type="button" on:click={() => (pendingEdit = null)} class="terminal-button text-[10px]">Discard</button>
								</div>
							</div>
						{/if}
					</div>
				</section>
			{/if}

			{#if mode === 'visual'}
				<StrategyBuilder {indicators} initialSpec={currentSpec} disabled={busy} on:change={onBuilderChange}
					signalBars={previewCtx && !previewStale ? previewCtx.signal_bars ?? {} : {}} barCount={previewCtx?.bars.length ?? 0} />
			{:else}
				<section class="space-y-2 border border-[#222] bg-[#050505] p-3" aria-label="Python strategy">
					<p class="text-[11px] text-[#666]">
						Subclass <span class="font-mono text-[#aaa]">BaseStrategy</span>, return entries/exits from
						<span class="font-mono text-[#aaa]">generate_signals(df)</span>. Must export
						<span class="font-mono text-[#aaa]">STRATEGY_CLASS</span> and <span class="font-mono text-[#aaa]">TYPE_NAME</span>.
					</p>
					<textarea bind:value={customCode} spellcheck="false" rows="22" disabled={busy || customStatus === 'validating'} aria-label="strategy code"
						class="terminal-input w-full resize-y font-mono text-[12px] leading-5"></textarea>
					<div class="flex flex-wrap items-center gap-3">
						<button type="button" on:click={loadCustomStrategy} disabled={busy || customStatus === 'validating' || !customCode.trim()}
							class="terminal-button-primary text-[10px] disabled:opacity-40">
							{customStatus === 'validating' ? 'Validating…' : 'Validate & load'}
						</button>
						<button type="button" on:click={() => (customCode = customTemplate())} disabled={busy}
							class="terminal-button text-[10px]">Reset template</button>
						{#if customStatus === 'loaded' && customLoadedName}
							<span class="inline-flex items-center gap-1.5 text-[12px] text-emerald-400">
								<span class="inline-block h-2 w-2 rounded-full bg-emerald-400"></span>
								Loaded <span class="font-mono">{customLoadedName}</span>
							</span>
						{/if}
					</div>
					{#each customErrors as e}<div class="border border-red-900 bg-red-500/5 px-3 py-1.5 font-mono text-[11px] text-red-400">{e}</div>{/each}
					{#each customWarnings as w}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{w}</div>{/each}
					{#if customStatus === 'loaded'}
						<div class="mt-2">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Parameters</div>
							<ParameterEditor params={paramsDraft} saving={busy} on:paramsChange={(e) => (paramsDraft = e.detail)} />
						</div>
					{/if}
				</section>
			{/if}

			<!-- Execution settings -->
			<section id="sc-execution" class="scroll-mt-4 border border-[#222] bg-[#050505] p-3">
				<button type="button" class="flex w-full items-center justify-between gap-3 text-left" on:click={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
					<div class="min-w-0">
						<div class="text-[11px] font-bold uppercase tracking-wider text-white">Execution Settings</div>
						<div class="mt-0.5 truncate text-[11px] text-[#888]" data-testid="execution-summary">{executionSummary}</div>
					</div>
					<span class="text-sm text-[#555]">{showAdvanced ? '−' : '+'}</span>
				</button>
				{#if showAdvanced}
					<div class="mt-3 grid gap-3 border-t border-[#1a1a1a] pt-3 sm:grid-cols-2 xl:grid-cols-3">
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Initial Capital</div>
							<input type="number" bind:value={initialCapital} step="1000" min="100" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Fee (bps)</div>
							<input type="number" bind:value={feeBps} step="1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Slippage (bps)</div>
							<input type="number" bind:value={slippageBps} step="1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Leverage</div>
							<input type="number" bind:value={leverage} step="0.5" min="1" max="125" disabled={busy} class="terminal-input mt-1.5" /></label>
						{#if mode !== 'visual'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Trade Direction</div>
								<select bind:value={tradeMode} disabled={busy} class="terminal-select mt-1.5">
									<option value="long_only">Long only</option><option value="short_only">Short only</option><option value="both">Both</option>
								</select></label>
						{/if}
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Sizing Mode</div>
							<select bind:value={sizingMode} disabled={busy} class="terminal-select mt-1.5">
								<option value="full">Full equity</option><option value="fraction">Fraction (risk)</option><option value="fixed">Fixed notional</option><option value="atr">ATR risk</option>
								<option value="kelly" disabled>Kelly (opens no trades)</option>
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
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">ATR Stop Mult</div>
								<input type="number" bind:value={atrStopMultiplier} step="0.1" min="0" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
						{#if sizingMode === 'kelly'}
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Kelly Mult</div>
								<input type="number" bind:value={kellyMultiplier} step="0.05" min="0" max="5" disabled={busy} class="terminal-input mt-1.5" /></label>
							<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Kelly Lookback</div>
								<input type="number" bind:value={kellyLookback} step="10" min="1" disabled={busy} class="terminal-input mt-1.5" /></label>
						{/if}
					</div>
					{#if fullFallsBack}
						<p class="mt-3 text-[11px] text-[#888]" data-testid="sizing-note">
							With no stop, target, trailing or time stop, the engine does not size at full equity. It risks 1% of equity per trade against a 2× ATR stop that it places itself. Add an exit below to trade full equity.
						</p>
					{:else if sizingMode === 'kelly'}
						<p class="mt-3 text-[11px] text-amber-400" data-testid="sizing-note">
							Kelly sizes each trade from the strategy’s own closed trades. A backtest starts with none, so it opens no trades. Choose another sizing mode.
						</p>
					{/if}
					<div class="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Stop Loss %</div>
							<input type="number" value={stopLossPct ?? ''} on:input={(e) => (stopLossPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Take Profit %</div>
							<input type="number" value={takeProfitPct ?? ''} on:input={(e) => (takeProfitPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Trailing Stop %</div>
							<input type="number" value={trailingStopPct ?? ''} on:input={(e) => (trailingStopPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-wider text-[#666]">Time Stop (bars)</div>
							<input type="number" value={timeStopBars ?? ''} on:input={(e) => (timeStopBars = numberOrNull(e.currentTarget.value))} step="1" min="1" placeholder="None" disabled={busy} class="terminal-input mt-1.5" /></label>
					</div>
					<p class="mt-3 text-[11px] text-[#666]">Stops, sizing and leverage travel with your saved strategy. Forge re-tests it using pipeline data windows and cost assumptions.</p>
				{/if}
			</section>
		</div>

		<!-- RIGHT: what it did -->
		<div class="min-h-0 space-y-3 overflow-y-auto p-4 pb-24">
			{#if mode === 'visual'}
				<section class="border border-[#222] bg-[#050505]" aria-label="Preview">
					<header class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[#141414] px-3 py-1.5 text-[10px] text-[#666]">
						<h3 class="text-[11px] font-bold uppercase tracking-wider text-white">Preview</h3>
						<span title="Entries fill at the open after the signal bar"><span class="text-emerald-500">▲</span> long <span class="text-orange-500">▼</span> short <span class="text-[#999]">●</span> exit</span>
						<span title="Run Backtest scores only this part">░ out-of-sample</span>
						<label class="inline-flex items-center gap-1" title="Shade the bars where the entry rule held">
							<input type="checkbox" bind:checked={showRuleShading} class="accent-white" /> rule held
						</label>
						<span class="ml-auto flex items-center gap-2">
							{#if previewLoading}<span class="text-white">Updating…</span>{/if}
							<button type="button" on:click={runPreview} disabled={!liveValid}
								class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#aaa] hover:border-white hover:text-white disabled:opacity-40">Refresh</button>
						</span>
					</header>
					<div style="height: {380 + paneCount * 92}px">
						{#if previewCtx}
							<StrategyChart bars={previewCtx.bars} mainIndicators={previewCtx.main_indicators} subIndicators={previewCtx.sub_indicators}
								trades={previewTrades} {selectedTrade} ruleSpans={previewCtx.rule_spans ?? {}} oosStart={previewCtx.oos_start ?? null}
								{showRuleShading} labels={chartLabels} thresholds={chartThresholds} {fitToken} {focusToken}
								on:select={(e) => selectTrade(e.detail)} />
						{:else}
							<div class="flex h-full items-center justify-center text-[12px] text-[#555]">
								{previewError ? 'No preview.' : liveValid ? 'Building the preview…' : 'Complete the rules to preview them.'}
							</div>
						{/if}
					</div>
					<div class="space-y-1.5 border-t border-[#141414] px-3 py-2">
						{#if previewStale}
							<div class="border border-[#333] bg-[#111] px-3 py-1.5 text-[11px] text-[#999]" role="status">
								The chart shows the last valid draft. Fix the builder errors to update it.
							</div>
						{/if}
						{#if previewError}
							<div class="border border-red-900 bg-red-500/5 px-3 py-1.5 text-[11px] text-red-400">{previewError}</div>
						{:else if previewCtx}
							<div class="flex flex-wrap gap-x-4 gap-y-1 text-[11px]" data-testid="preview-stats">
								<span class="text-[#555]">Trades: <span class="font-mono text-white">{previewCtx.trade_count ?? previewTrades.length}</span></span>
								{#if exitReasons.length}
									<span class="text-[#555]">Exits: <span class="font-mono text-[#aaa]">{exitReasons.map(([reason, count]) => `${reason.replaceAll('_', ' ')} ${count}`).join(' · ')}</span></span>
								{/if}
								<span class="text-[#555]">Bars: <span class="font-mono text-[#aaa]">{previewCtx.bars.length.toLocaleString()}</span></span>
							</div>
							{#each previewCtx.warnings.slice(0, 4) as w}
								<div class="border border-amber-900 bg-amber-500/5 px-3 py-1 text-[11px] text-amber-400">{w}</div>
							{/each}
						{/if}
					</div>
				</section>

				<VitalsPanel vitals={previewCtx?.vitals ?? null} />

				<section class="border border-[#222] bg-[#050505]" aria-label="Details">
					<div class="flex overflow-x-auto border-b border-[#141414]" role="tablist">
						{#each tabs as t (t.key)}
							<button type="button" role="tab" aria-selected={tab === t.key} on:click={() => (tab = t.key)}
								class="whitespace-nowrap border-r border-[#141414] px-3 py-1.5 text-[10px] uppercase tracking-wider transition-colors {tab === t.key ? 'bg-white text-black' : 'text-[#777] hover:text-white'}">{t.label}</button>
						{/each}
					</div>
					<div class="p-3">
						{#if tab === 'trade'}
							<TradeInspector trade={selected} total={previewTrades.length} labels={chartLabels} knobs={previewKnobs}
								on:step={(e) => stepTrade(e.detail)} on:focus={() => (focusToken += 1)} />
						{:else if tab === 'trades'}
							<TradesTable trades={previewTrades} selected={selectedTrade} on:select={(e) => selectTrade(e.detail, { focus: true })} />
						{:else if tab === 'stress'}
							<StressTestPanel result={stressResult} loading={stressLoading} error={stressError} stale={stressStale}
								canRun={liveValid} on:run={runStressTest} />
						{:else if tab === 'heatmap'}
							<HeatmapPanel knobs={knobOptions} view={heatmapView} stale={heatmapStale} canRun={liveValid} current={heatmapCurrent}
								on:run={(e) => runHeatmap(e.detail)} on:cancel={() => heatmapController?.abort()} on:adopt={(e) => adoptHeatmapCell(e.detail)} />
						{:else if tab === 'markets'}
							<MarketGridPanel view={marketView} {availability} symbolOptions={marketSymbolOptions} currentSymbol={symbol}
								currentTimeframe={timeframe} stale={marketStale} canRun={liveValid}
								on:run={(e) => runMarkets(e.detail)} on:cancel={() => marketController?.abort()} on:pick={(e) => pickMarket(e.detail)} />
						{:else if tab === 'variants'}
							<VariantHistory {variants} resultsSeen={trials} currentKey={currentVariantKey} current={liveSpec as RuleSpec | null} on:restore={(e) => restoreVariant(e.detail)} />
						{:else}
							{@render resultBlock()}
						{/if}
					</div>
				</section>
			{:else}
				<section class="border border-dashed border-[#262626] px-4 py-10 text-center text-[12px] text-[#555]">
					The live preview, trade explanations and stress test work on Rules strategies.
					Validate & load your Python strategy, then Run Backtest to score it.
				</section>
				<section class="border border-[#222] bg-[#050505] p-3">{@render resultBlock()}</section>
			{/if}
		</div>
	</div>
</div>

{#if launcherOpen}
<div use:portal>
	<StrategyLauncher {library} {libraryLoading} templates={STRATEGY_TEMPLATES} {prebuilt} {appStrategies} {includeAppGenerated} {appLoading}
		{currentLibraryId} {forging} tab={launcherTab}
		on:close={() => (launcherOpen = false)}
		on:blank={blankCanvas}
		on:template={(e) => applyTemplate(e.detail)}
		on:openEntry={(e) => openLibraryEntry(e.detail)}
		on:duplicate={(e) => duplicateEntry(e.detail.entry, e.detail.event)}
		on:remove={(e) => deleteEntry(e.detail.entry, e.detail.event)}
		on:forge={(e) => forgeEntry(e.detail.entry, e.detail.event)}
		on:system={(e) => openSystemStrategy(e.detail.id, findStrategy(e.detail.source === 'pre' ? prebuilt : appStrategies, e.detail.id))}
		on:toggleApp={toggleAppGenerated}
		on:import={() => { launcherOpen = false; showImportDialog = true; }} />
</div>
{/if}

<!-- Save prompt: overwrite the opened strategy or create a new one -->
{#if savePromptOpen}
<div use:portal>
	<button type="button" class="fixed inset-0 z-40 bg-black/50" on:click={() => (savePromptOpen = false)} aria-label="Cancel save"></button>
	<div class="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 border border-[#333] bg-[#050505] p-5">
		<h3 class="border-b border-[#222] pb-3 text-sm font-bold uppercase tracking-widest text-white">Save strategy</h3>
		{#if currentLibraryId}
			<p class="mt-3 text-[12px] text-[#666]">
				You're editing <span class="text-white">{strategyName}</span>. Overwrite it, or save your changes as a new strategy?
			</p>
			<button type="button" on:click={() => doSave(true)} disabled={saving}
				class="terminal-button-primary mt-4 w-full text-xs disabled:opacity-40">
				{saving ? 'Saving…' : `Overwrite “${strategyName}”`}
			</button>
			<div class="my-3 flex items-center gap-2 text-[11px] text-[#555]">
				<span class="h-px flex-1 bg-[#222]"></span>or<span class="h-px flex-1 bg-[#222]"></span>
			</div>
		{:else}
			<p class="mt-3 text-[12px] text-[#666]">Name this strategy to save it to your library.</p>
		{/if}
		<label for="sc-saveas-name" class="mt-2 block text-[10px] uppercase tracking-wider text-[#666]">New strategy name</label>
		<input id="sc-saveas-name" bind:value={saveAsName} placeholder="Strategy name"
			class="terminal-input mt-1.5" />
		<div class="mt-4 flex items-center justify-end gap-2">
			<button type="button" on:click={() => (savePromptOpen = false)}
				class="terminal-button text-[10px]">Cancel</button>
			<button type="button" on:click={() => doSave(false)} disabled={saving || !saveAsName.trim()}
				class="terminal-button-primary text-[10px] disabled:opacity-40">
				{saving ? 'Saving…' : 'Save as new'}
			</button>
		</div>
	</div>
</div>
{/if}

{#if showImportDialog}
	<div use:portal>
		<StrategyImportDialog
			on:close={() => (showImportDialog = false)}
			on:imported={(e) => onStrategyImported(e.detail)}
		/>
	</div>
{/if}
