<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { editableSnapshot } from '$lib/utils/creatorRevision';
	import { checkIdeaReadiness, type IdeaReadiness } from '$lib/api/strategyCreator';
	import { goto } from '$app/navigation';
	import {
		getIndicators,
		previewStrategyChart,
		nlToSpec,
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
		type LibraryStrategy,
		type BacktestResult,
		type Strategy,
	} from '$lib/api';
	import { resolveDateRangePreset, estimateBarCount } from '$lib/utils/dateRange';
	import { addToast } from '$lib/stores/processTracker';
	import { chartContextToWorkspaceProps } from '$lib/utils/chartContext';
	import { builderIncompatibility } from '$lib/utils/ruleSpec';
	import type { ExecutionRequestFields } from '$lib/api';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import BacktestResultSummary from '$lib/components/backtest/BacktestResultSummary.svelte';
	import ChartWorkspace from '$lib/components/chart/ChartWorkspace.svelte';
	import StrategyBuilder from '$lib/components/strategy/StrategyBuilder.svelte';
	import StrategyImportDialog from '$lib/components/strategy/StrategyImportDialog.svelte';
	import type { StrategyImportResult } from '$lib/api';
	import { STRATEGY_TEMPLATES, type RuleSpec } from '$lib/components/strategy/templates';

	const BAR_CAP = 100_000;
	const RULE_ENGINE_TYPE = 'rule_engine';

	type Mode = 'visual' | 'code' | 'ai';
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

	function deriveTradeMode(spec: Record<string, unknown> | null): 'long_only' | 'short_only' | 'both' {
		const g = (k: string) => spec?.[k] as { conditions?: unknown[] } | null | undefined;
		const hasLong = !!g('entry_long')?.conditions?.length;
		const hasShort = !!g('entry_short')?.conditions?.length;
		if (hasShort && hasLong) return 'both';
		if (hasShort) return 'short_only';
		return 'long_only';
	}
	$: effectiveTradeMode = mode === 'visual' ? deriveTradeMode(liveSpec) : tradeMode;

	function hashSpec(spec: unknown): string {
		const s = JSON.stringify(spec ?? {});
		let h = 5381;
		for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
		return h.toString(36);
	}

	function clone<T>(v: T): T {
		return JSON.parse(JSON.stringify(v));
	}

	function onBuilderChange(
		e: CustomEvent<{ spec: Record<string, unknown>; valid: boolean; errors: string[] }>
	) {
		liveSpec = e.detail.spec;
		liveValid = e.detail.valid;
		liveErrors = e.detail.errors;
	}

	// Templates
	function applyTemplate(id: string, { quiet = false } = {}) {
		const t = STRATEGY_TEMPLATES.find((x) => x.id === id);
		if (!t) return;
		restoreExecution({});
		currentSpec = clone(t.spec);
		symbol = t.symbol;
		timeframe = t.timeframe;
		strategyName = t.name;
		strategyDescription = t.description;
		mode = 'visual';
		currentLibraryId = null;
		if (!quiet) addToast(`Loaded template “${t.name}”`, 'info');
	}
	function blankCanvas() {
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
		mode = 'visual';
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

	// AI mode
	let aiPrompt = '';
	let aiLoading = false;
	let aiError = '';
	let aiProvider: string | null = null;
	let aiReadiness: IdeaReadiness | null = null;
	let aiCheckedKey = '';
	let aiChecking = false;
	$: aiInputKey = JSON.stringify([aiPrompt, symbol, timeframe]);
	$: if (aiCheckedKey !== aiInputKey) aiReadiness = null;
	async function checkInputs() {
		if (!aiPrompt.trim() || aiChecking || aiLoading) return;
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
		if (!aiPrompt.trim() || aiLoading || aiChecking) return;
		const key = aiInputKey;
		const draft = draftSnapshot;
		const libraryId = currentLibraryId;
		aiLoading = true;
		aiError = '';
		aiProvider = null;
		try {
			const res = await nlToSpec({ description: aiPrompt, symbol, timeframe });
			if (key !== aiInputKey || mode !== 'ai' || draft !== draftSnapshot || libraryId !== currentLibraryId) {
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
				mode = 'visual';
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
	$: executionSummary = [
		fullFallsBack ? 'Default sizing: 1% risk, 2× ATR stop'
			: sizingMode === 'full' ? 'Full equity'
			: sizingMode === 'fraction' ? `${riskPerTrade * 100}% risk per trade`
			: sizingMode === 'fixed' ? `${fixedSize} per trade`
			: sizingMode === 'atr' ? `${riskPerTrade * 100}% risk, ${atrStopMultiplier}× ATR stop`
			: 'Kelly sizing',
		stopLossPct != null ? `stop ${stopLossPct}%` : '',
		takeProfitPct != null ? `target ${takeProfitPct}%` : '',
		trailingStopPct != null ? `trail ${trailingStopPct}%` : '',
		timeStopBars != null ? `${timeStopBars}-bar time stop` : '',
		`${leverage}× leverage`,
	].filter(Boolean).join(' · ');

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

	// Live preview chart
	let previewCtx: PreviewChartContext | null = null;
	let previewLoading = false;
	let previewError = '';
	let previewKey = '';
	let previewSeq = 0;
	let previewTimer: ReturnType<typeof setTimeout> | undefined;
	let fitToken = 0;
	onDestroy(() => clearTimeout(previewTimer));

	$: chartProps = chartContextToWorkspaceProps(previewCtx);
	$: exitReasons = Object.entries(previewCtx?.exit_reasons ?? {}).sort((a, b) => b[1] - a[1]);
	// The chart shows an older draft while the builder has errors.
	$: previewStale = !!previewCtx && !liveValid;

	function schedulePreview() {
		clearTimeout(previewTimer);
		previewTimer = setTimeout(runPreview, 500);
	}
	async function runPreview() {
		if (mode !== 'visual' || !liveValid || !liveSpec) return;
		// Only the latest request may land; an older, slower one would show a stale draft.
		const seq = ++previewSeq;
		previewLoading = true;
		previewError = '';
		try {
			const ctx = await previewStrategyChart({
				spec: liveSpec,
				symbol: symbol.trim(),
				timeframe,
				start: startDate,
				end: endDate,
				trade_mode: effectiveTradeMode,
				name: strategyName,
				...previewExecution,
			});
			if (seq !== previewSeq) return;
			previewCtx = ctx;
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
	let libraryOpen = false;
	let libraryLoading = false;
	let currentLibraryId: string | null = null;
	let saving = false;

	// System strategies (for the unified "Open a strategy" dropdown)
	let prebuilt: Strategy[] = [];
	let appStrategies: Strategy[] = [];
	let includeAppGenerated = false;
	let appLoading = false;
	let openSelectValue = '';
	let nonEditableNotice = '';

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

	async function onOpenSelect() {
		const value = openSelectValue;
		openSelectValue = ''; // reset so re-selecting the same entry fires again
		nonEditableNotice = '';
		if (!value) return;
		const [source, ...rest] = value.split(':');
		const id = rest.join(':');
		if (source === 'blank') {
			blankCanvas();
		} else if (source === 'tpl') {
			applyTemplate(id);
		} else if (source === 'lib') {
			const entry = library.find((l) => l.id === id);
			if (entry) openLibraryEntry(entry);
		} else if (source === 'pre' || source === 'app') {
			await openSystemStrategy(id, source === 'pre' ? findStrategy(prebuilt, id) : findStrategy(appStrategies, id));
		}
	}

	async function openSystemStrategy(id: string, meta?: Strategy) {
		const displayName = meta?.name || id;
		try {
			const detail = await getSystemStrategyDetail(id);
			const spec = (detail.params && typeof detail.params === 'object' ? (detail.params as Record<string, unknown>).spec : null);
			const unsupported = spec && typeof spec === 'object' ? builderIncompatibility(spec) : null;
			if (unsupported) {
				nonEditableNotice = `“${detail.name || displayName}” can’t be opened in the visual builder. ${unsupported}`;
				return;
			}
			if (spec && typeof spec === 'object') {
				currentSpec = clone(spec as unknown as RuleSpec);
				symbol = detail.symbol || symbol;
				timeframe = detail.timeframe || timeframe;
				strategyName = `${detail.name || displayName} (copy)`;
				strategyDescription = '';
				restoreExecution(detail.params || {});
				currentLibraryId = null; // editing a system strategy → Save creates a new library entry
				mode = 'visual';
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
		if (mode === 'ai') {
			addToast('Generate a strategy first, then save it from the Visual tab.', 'error');
			return;
		}
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
		libraryOpen = false;
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
		if (mode === 'ai') return 'Generate a strategy first, then run it from the Visual tab.';
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
				resultLoading = true;
				try {
					const result = await getResult(job.result_id);
					if (draftSnapshot === testedSnapshot) { inlineResult = result; resultSnapshot = testedSnapshot; }
				} catch {
					inlineResult = null;
				} finally {
					resultLoading = false;
				}
				queueMicrotask(() => document.getElementById('sc-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
			}
		} catch (err) {
			submitStatus = 'failed';
			submitError = err instanceof Error ? err.message : 'Backtest submission failed';
		}
	}

	function openFullReport() {
		if (lastStrategyId) goto(`/lab/strategy/${encodeURIComponent(lastStrategyId)}?returnTo=/strategy-creator`);
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
</script>

<svelte:head><title>Strategy Creator | Forven</title></svelte:head>

<div class="mx-auto max-w-7xl px-4 py-6">
	<div>
		<!-- Header -->
		<div class="mb-4 border-b border-[#222] pb-4">
			<div>
				<h1 class="text-lg font-bold uppercase tracking-widest text-white">Strategy Creator</h1>
				<p class="mt-1 text-xs text-[#666]">
					Build your own idea from {indicators.length || '40+'} indicators, watch signals on a live chart, then backtest and send it to the Forge.
				</p>
				<div class="mt-4 flex flex-wrap items-center gap-2">
					<select bind:value={openSelectValue} on:change={onOpenSelect}
						class="terminal-select max-w-[15rem] text-xs"
						title="Open any strategy in the system">
						<option value="">Open a strategy…</option>
						<option value="blank">✦ Blank canvas</option>
						<optgroup label="Templates">
							{#each STRATEGY_TEMPLATES as t}<option value={`tpl:${t.id}`}>{t.name}</option>{/each}
						</optgroup>
						{#if library.length}
							<optgroup label="My Library">
								{#each library as l}<option value={`lib:${l.id}`}>{l.name}</option>{/each}
							</optgroup>
						{/if}
						{#if prebuilt.length}
							<optgroup label="Prebuilt">
								{#each prebuilt as s}<option value={`pre:${s.api_name || s.name}`}>{s.name}</option>{/each}
							</optgroup>
						{/if}
						{#if includeAppGenerated && appStrategies.length}
							<optgroup label="App-generated">
								{#each appStrategies as s}<option value={`app:${s.api_name || s.name}`}>{s.name}</option>{/each}
							</optgroup>
						{/if}
					</select>
					<label class="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-[#666]" title="Include app-generated strategies in the dropdown">
						<input type="checkbox" checked={includeAppGenerated} on:change={toggleAppGenerated} class="accent-white" />
						app-generated{#if appLoading}…{/if}
					</label>
					<button type="button" on:click={() => { libraryOpen = !libraryOpen; if (libraryOpen) loadLibrary(); }}
						class="terminal-button text-[10px]">
						My Strategies ({library.length})
					</button>
					<button type="button" data-testid="creator-import-strategy" on:click={() => (showImportDialog = true)}
						title="Import a strategy export as a new quick_screen container"
						class="terminal-button text-[10px]">
						Import
					</button>
				</div>
			</div>
		</div>

		{#if loadError}
			<div class="mb-4 border border-red-900 bg-red-500/5 px-4 py-3 text-sm text-red-400" role="alert">{loadError}</div>
		{/if}

		<div class="grid gap-4 lg:grid-cols-2">
			<!-- LEFT: builder -->
			<div class="space-y-4">
				<div class="terminal-card p-4">
					<!-- Name + mode tabs -->
					<div class="flex flex-wrap items-center justify-between gap-3">
						<input bind:value={strategyName} placeholder="Strategy name"
							class="terminal-input max-w-xs" />
						<div class="inline-flex border border-[#333] bg-black text-[10px] uppercase tracking-wide">
							<button type="button" on:click={() => (mode = 'visual')} aria-pressed={mode === 'visual'}
								class="border-r border-[#333] px-3 py-1.5 transition-colors {mode === 'visual' ? 'bg-white text-black' : 'text-[#666] hover:text-white'}">Visual</button>
							<button type="button" on:click={() => (mode = 'ai')} aria-pressed={mode === 'ai'}
								class="border-r border-[#333] px-3 py-1.5 transition-colors {mode === 'ai' ? 'bg-white text-black' : 'text-[#666] hover:text-white'}">AI</button>
							<button type="button" on:click={() => (mode = 'code')} aria-pressed={mode === 'code'}
								class="px-3 py-1.5 transition-colors {mode === 'code' ? 'bg-white text-black' : 'text-[#666] hover:text-white'}">Code</button>
						</div>
					</div>

					{#if nonEditableNotice}
						<div class="mt-3 border border-amber-900 bg-amber-500/5 px-3 py-2 text-[12px] text-amber-400">
							{nonEditableNotice}
							<a href="/backtest/new" class="ml-1 text-white underline">Open Manual Backtest →</a>
						</div>
					{/if}

					<div class="mt-4">
						{#if mode === 'visual'}
							<StrategyBuilder {indicators} initialSpec={currentSpec} disabled={busy} on:change={onBuilderChange} />
						{:else if mode === 'ai'}
							<div class="space-y-3">
								<p class="text-[12px] text-[#666]">Describe your idea. Local inputs are checked before the AI drafts editable rules. Missing inputs must be resolved first.</p>
								<textarea bind:value={aiPrompt} rows="4" placeholder="e.g. Buy when RSI drops below 30 and price is above the 200 EMA; sell when RSI goes above 60."
									class="terminal-input w-full resize-y text-[13px]"></textarea>
								<button type="button" on:click={checkInputs} disabled={aiChecking || aiLoading || !aiPrompt.trim()} class="terminal-button text-[10px] disabled:opacity-40">{aiChecking ? 'Checking data…' : 'Check data'}</button>
								<button type="button" on:click={generateFromNl} disabled={aiLoading || aiChecking || !aiPrompt.trim()}
									class="terminal-button-primary text-[10px] disabled:opacity-40">
									{aiLoading ? 'Generating…' : 'Generate strategy'}
								</button>
								{#if aiProvider}<span class="ml-2 text-[10px] text-[#555]">via {aiProvider}</span>{/if}
								{#if aiReadiness}
									<div class="border border-[#333] bg-[#111] p-3 text-[12px] space-y-2" role="status">
										<p class="text-white">{aiReadiness.can_generate ? 'Basic input check complete' : 'Resolve inputs before generation'}</p>
										<p class="text-[#aaa]">{symbol} / {timeframe} · Named inputs: {aiReadiness.required.join(', ') || 'Price and volume only detected'}</p>
										{#each aiReadiness.issues as issue}<p class="text-amber-400">{issue}</p>{/each}
										{#each aiReadiness.warnings as warning}<p class="text-[#999]">{warning}</p>{/each}
										<a href="/data" class="text-white underline">Review data collection →</a>
									</div>
								{/if}
								{#if aiError}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{aiError}</div>{/if}
							</div>
						{:else}
							<div class="space-y-2">
								<p class="text-[11px] text-[#666]">
									Subclass <span class="font-mono text-[#aaa]">BaseStrategy</span>, return entries/exits from
									<span class="font-mono text-[#aaa]">generate_signals(df)</span>. Must export
									<span class="font-mono text-[#aaa]">STRATEGY_CLASS</span> and <span class="font-mono text-[#aaa]">TYPE_NAME</span>.
								</p>
								<textarea bind:value={customCode} spellcheck="false" rows="16" disabled={busy || customStatus === 'validating'}
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
							</div>
						{/if}
					</div>
				</div>

				<!-- Market scope -->
				<div class="terminal-card p-4">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Market Scope</div>
					<div class="mt-3 grid gap-4 md:grid-cols-2">
						<SymbolInput id="sc-symbol" bind:value={symbol} disabled={busy} suggestions={symbolSuggestions} helpText="Base asset is used for the backtest (e.g. BTC)." />
						<TimeframeSelect id="sc-timeframe" bind:value={timeframe} disabled={busy} />
					</div>
					<div class="mt-4"><DateRangeFieldset idPrefix="sc-date" bind:startDate bind:endDate {timeframe} /></div>
				</div>

				<!-- Execution settings -->
				<div class="terminal-card p-4">
					<button type="button" class="flex w-full items-center justify-between gap-3 text-left" on:click={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
						<div class="min-w-0">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Execution Settings</div>
							<div class="mt-1 truncate text-[11px] text-[#888]" data-testid="execution-summary">{executionSummary}</div>
						</div>
						<span class="text-sm text-[#555]">{showAdvanced ? '−' : '+'}</span>
					</button>
					{#if showAdvanced}
						<div class="mt-4 grid gap-4 border-t border-[#222] pt-4 md:grid-cols-2 xl:grid-cols-3">
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
						<div class="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
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
			</div>

			<!-- RIGHT: live preview + actions -->
			<div class="space-y-4">
				<div class="terminal-card p-4">
					<div class="flex items-center justify-between">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Live Preview</div>
						<div class="flex items-center gap-2 text-[11px]">
							{#if previewLoading}<span class="text-white">Updating…</span>{/if}
							<button type="button" on:click={runPreview} disabled={mode !== 'visual' || !liveValid}
								class="terminal-button px-2 py-1 text-[10px]">Refresh</button>
						</div>
					</div>
					{#if mode !== 'visual'}
						<div class="mt-3 border border-dashed border-[#333] px-3 py-10 text-center text-[12px] text-[#555]">
							Live chart preview is available in the Visual builder.
						</div>
					{:else}
						<div class="mt-3 h-[360px] overflow-hidden border border-[#222]">
							<ChartWorkspace
								data={chartProps.data}
								entryMarkers={chartProps.entryMarkers}
								exitMarkers={chartProps.exitMarkers}
								mainIndicators={chartProps.mainIndicators}
								subIndicators={chartProps.subIndicators}
								strategyName={chartProps.strategyName}
								autoScroll={true}
								fitContentToken={fitToken}
							/>
						</div>
						{#if previewStale}
							<div class="mt-2 border border-[#333] bg-[#111] px-3 py-1.5 text-[11px] text-[#999]" role="status">
								The chart shows the last valid draft. Fix the builder errors to update it.
							</div>
						{/if}
						{#if previewError}
							<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-1.5 text-[11px] text-red-400">{previewError}</div>
						{:else if previewCtx}
							<div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]" data-testid="preview-stats">
								<span class="text-[#555]">Trades: <span class="font-mono text-white">{previewCtx.trade_count ?? chartProps.entryMarkers.length}</span></span>
								{#if exitReasons.length}
									<span class="text-[#555]">Exits: <span class="font-mono text-[#aaa]">{exitReasons.map(([reason, count]) => `${reason.replaceAll('_', ' ')} ${count}`).join(' · ')}</span></span>
								{/if}
								{#if previewCtx.signal_bars && Object.keys(previewCtx.signal_bars).length}
									<span class="text-[#555]">Entry rule true on <span class="font-mono text-[#aaa]">{((previewCtx.signal_bars.entry_long ?? 0) + (previewCtx.signal_bars.entry_short ?? 0)).toLocaleString()}</span> bars</span>
								{/if}
								<span class="text-[#555]">Bars: <span class="font-mono text-[#aaa]">{chartProps.data.length.toLocaleString()}</span></span>
							</div>
							<p class="mt-1 text-[10px] text-[#555]">Markers are the trades Run Backtest takes with these execution settings.</p>
							{#each chartProps.warnings.slice(0, 4) as w}
								<div class="mt-1 border border-amber-900 bg-amber-500/5 px-3 py-1 text-[11px] text-amber-400">{w}</div>
							{/each}
						{/if}
					{/if}
				</div>

				<p class="text-[11px] text-[#888]">Stops, sizing and leverage travel with your saved strategy. Forge re-tests it using pipeline data windows and cost assumptions.</p>
				<!-- Actions -->
				<div class="terminal-card p-4">
					{#if submitError}<div class="mb-3 border border-red-900 bg-red-500/5 px-4 py-2.5 text-sm text-red-400" role="alert">{submitError}</div>{/if}
					<div class="flex flex-wrap items-center gap-3">
						<button type="button" on:click={runBacktest} disabled={busy || resultLoading}
							class="terminal-button-primary text-xs disabled:opacity-40">
							{#if busy || resultLoading}Running…{:else}Run Backtest{/if}
						</button>
						<button type="button" on:click={requestSave} disabled={saving}
							class="terminal-button text-xs">
							{saving ? 'Saving…' : currentLibraryId ? 'Save' : 'Save to library'}
						</button>
						{#if currentLibraryId && savedEntry?.forge_strategy_id && !dirty}
							<a href={`/lab/strategy/${encodeURIComponent(savedEntry.forge_strategy_id)}`}
								class="terminal-button text-xs" title="This saved revision is already in the Forge">Open in Forge →</a>
						{:else if currentLibraryId}
							<button type="button" on:click={(e) => { const entry = library.find((l) => l.id === currentLibraryId); if (entry) forgeEntry(entry, e); }}
								disabled={dirty || forging || saving || busy}
								class="terminal-button text-xs disabled:opacity-40"
								title={dirty ? "Save your changes first" : "Send this saved revision to Forge"}>Send to Forge →</button>
						{/if}
					</div>
					{#if currentLibraryId && dirty}<p class="mt-2 text-[11px] text-amber-400">Unsaved changes — save before sending to Forge.</p>{/if}
					{#if !currentLibraryId}
						<p class="mt-2 text-[11px] text-[#555]">Save to your library to enable Send to Forge.</p>
					{/if}
				</div>

				<!-- Inline result -->
				{#if resultLoading || inlineResult || submitWarning}
					<div id="sc-results" class="scroll-mt-6 space-y-3">
						{#if submitWarning}<div class="border border-amber-900 bg-amber-500/5 px-4 py-2.5 text-sm text-amber-400">⚠ {submitWarning}</div>{/if}
						<div class="border-b border-[#222] pb-4">
							<div class="flex items-center justify-between">
								<h2 class="text-sm font-bold uppercase tracking-widest text-white">Backtest Result</h2>
								<button type="button" on:click={openFullReport} disabled={!lastStrategyId}
									class="terminal-button-primary text-[10px] disabled:opacity-40">Full report →</button>
							</div>
						</div>
						{#if resultLoading}
							<div class="terminal-card p-8 text-center text-xs uppercase tracking-widest text-[#555]">Loading result…</div>
						{:else if inlineResult}
							<BacktestResultSummary result={inlineResult} />
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>

	<!-- Library drawer -->
	{#if libraryOpen}
		<button type="button" class="fixed inset-0 z-40 bg-black/50" on:click={() => (libraryOpen = false)} aria-label="Close library"></button>
		<aside class="fixed right-0 top-0 z-50 h-full w-full max-w-md overflow-y-auto border-l border-[#222] bg-[#050505] p-5">
			<div class="flex items-center justify-between border-b border-[#222] pb-4">
				<h2 class="text-sm font-bold uppercase tracking-widest text-white">My Strategies</h2>
				<button type="button" on:click={() => (libraryOpen = false)} class="text-[#555] hover:text-white">✕</button>
			</div>
			{#if libraryLoading}
				<div class="mt-6 text-xs uppercase tracking-widest text-[#555]">Loading…</div>
			{:else if library.length === 0}
				<div class="mt-6 border border-dashed border-[#333] p-6 text-center text-sm text-[#555]">
					No saved strategies yet. Build one and hit “Save to library”.
				</div>
			{:else}
				<div class="mt-4 space-y-2">
					{#each library as entry (entry.id)}
						<div class="border bg-[#050505] p-3 transition-colors hover:border-white {currentLibraryId === entry.id ? 'border-white' : 'border-[#222]'}">
							<button type="button" on:click={() => openLibraryEntry(entry)} class="block w-full text-left">
								<div class="flex items-center justify-between gap-2">
									<span class="truncate text-sm text-white">{entry.name}</span>
									<span class="shrink-0 border border-[#333] px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-[#888]">{entry.status}</span>
								</div>
								<div class="mt-0.5 truncate text-[11px] text-[#555]">
									{entry.kind} · {entry.symbol} {entry.timeframe}{entry.description ? ` · ${entry.description}` : ''}
								</div>
							</button>
							<div class="mt-2 flex items-center gap-3 text-[11px]">
								<button type="button" class="text-white" on:click={() => openLibraryEntry(entry)}>Open</button>
								<button type="button" class="text-[#666] hover:text-white" on:click={(e) => duplicateEntry(entry, e)}>Duplicate</button>
								{#if entry.forge_strategy_id}
									<a class="text-emerald-400 hover:text-white" href={`/lab/strategy/${encodeURIComponent(entry.forge_strategy_id)}`}>In Forge →</a>
								{:else}
									<button type="button" class="text-[#888] hover:text-white disabled:opacity-40" disabled={forging}
										on:click={(e) => forgeEntry(entry, e)}>→ Forge</button>
								{/if}
								<button type="button" class="ml-auto text-[#555] hover:text-red-400" on:click={(e) => deleteEntry(entry, e)}>Delete</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</aside>
	{/if}

	<!-- Save prompt: overwrite the opened strategy or create a new one -->
	{#if savePromptOpen}
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
	{/if}
</div>

{#if showImportDialog}
	<StrategyImportDialog
		on:close={() => (showImportDialog = false)}
		on:imported={(e) => onStrategyImported(e.detail)}
	/>
{/if}
