/**
 * Manual Backtest page logic that does not need the DOM: what to show, what to
 * send, and how to describe what the engine will actually do.
 *
 * The page sends only what the operator changes. Everything left blank resolves
 * the way it does for the strategy itself (its params, execution profile, trade
 * mode and leverage) or to the engine defaults (GET /api/backtests/defaults).
 */
import type { BacktestResult, Strategy } from '$lib/api';
import { stableStringify } from '$lib/utils/parameterEditor';

export type TradeMode = 'long_only' | 'short_only' | 'both';
export type SizingMode = 'full' | 'fraction' | 'fixed' | 'atr';

export const TRADE_MODES: TradeMode[] = ['long_only', 'short_only', 'both'];
export const TRADE_MODE_LABELS: Record<TradeMode, string> = {
	long_only: 'Long only',
	short_only: 'Short only',
	both: 'Long and short',
};

/** The request bounds POST /api/backtests enforces (BacktestSubmitBody). */
export const LIMITS = {
	barCap: 100_000,
	maxCapital: 1e12,
	maxCostBps: 1000,
	maxLeverage: 125,
	maxTakeProfitPct: 1000,
	maxTimeStopBars: 1_000_000,
	maxAtrMultiplier: 50,
} as const;

/** Engine constants (forven/strategies/sizing.py) behind a strategy with no execution profile. */
export const ENGINE_DEFAULT_RISK_PCT = 1;
export const ENGINE_DEFAULT_ATR_MULTIPLIER = 2;
export const ENGINE_DEFAULT_STOP_FLOOR_PCT = 3;
/** A configured risk-sized profile that omits its risk uses 2% (DEFAULT_PROFILE_RISK_PER_TRADE). */
export const PROFILE_DEFAULT_RISK_PER_TRADE = 0.02;

/**
 * Params that are not strategy logic knobs: internal contract fields (`_asset`,
 * `_timeframe`, `_data_requirements`, ...), the execution profile, and values the
 * page's own Market and Execution controls set.
 */
export function isHiddenParam(key: string): boolean {
	return key.startsWith('_') || key === 'execution_profile' || key === 'leverage'
		|| key === 'timeframe' || key === 'trade_mode';
}

export function editableParams(raw: Record<string, unknown> | undefined | null): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(raw ?? {})) {
		if (!isHiddenParam(key)) out[key] = value;
	}
	return out;
}

/** Keys whose value in `draft` differs from `base`. */
export function changedParams(draft: Record<string, unknown>, base: Record<string, unknown>): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(draft)) {
		if (stableStringify(value) !== stableStringify(base[key])) out[key] = value;
	}
	return out;
}

/** Built-in templates come from the catalog; everything else is a strategy row. */
export function isBuiltin(strategy: Strategy | null | undefined): boolean {
	return strategy?.source === 'prebuilt' && !strategy?.display_id;
}

export function strategyKey(strategy: Strategy): string {
	return strategy.api_name || strategy.name;
}

function readableName(strategy: Strategy): string {
	// Canonical row names are "{ASSET}-{TYPE}-{ID}"; the type part is the readable bit.
	const raw = strategy.name.replace(/^[A-Z0-9]+-/, '').replace(/-S\d{3,}$/i, '');
	const text = raw.replace(/_/g, ' ').toLowerCase().trim() || strategy.name;
	return text.length > 48 ? `${text.slice(0, 47)}…` : text;
}

export function marketLabel(symbol: string | null | undefined, timeframe: string | null | undefined): string {
	const asset = baseAsset(symbol);
	return [asset, timeframe].filter(Boolean).join(' ');
}

export function strategyOptionLabel(strategy: Strategy): string {
	if (isBuiltin(strategy)) return strategy.name;
	const id = strategy.display_id || strategyKey(strategy);
	const market = marketLabel(strategy.symbol, strategy.timeframe);
	return [id, market, readableName(strategy)].filter(Boolean).join(' · ');
}

/** "ETH/USDT" → "ETH", "BTCUSDT" → "BTC": the asset the engine backtests. */
export function baseAsset(symbol: string | null | undefined): string {
	let raw = String(symbol ?? '').trim().toUpperCase();
	if (!raw) return '';
	for (const sep of ['/', '-', '_', ':']) {
		if (raw.includes(sep)) {
			raw = raw.split(sep, 1)[0];
			break;
		}
	}
	for (const quote of ['PERP', 'USDT', 'USDC', 'USD']) {
		if (raw.endsWith(quote) && raw.length > quote.length) return raw.slice(0, -quote.length);
	}
	return raw;
}

/** The market a strategy runs on: its row's, or the one a built-in was written for. */
export function nativeMarket(strategy: Strategy): { symbol: string | null; timeframe: string | null } {
	if (isBuiltin(strategy)) {
		return {
			symbol: strategy.asset ? `${strategy.asset}/USDT` : null,
			timeframe: strategy.timeframe ?? null,
		};
	}
	return { symbol: strategy.symbol ?? null, timeframe: strategy.timeframe ?? null };
}

export function sameMarket(
	a: { symbol: string | null; timeframe: string | null },
	symbol: string,
	timeframe: string,
): boolean {
	const symbolMatches = !a.symbol || baseAsset(a.symbol) === baseAsset(symbol);
	const timeframeMatches = !a.timeframe || a.timeframe.toLowerCase() === timeframe.toLowerCase();
	return symbolMatches && timeframeMatches;
}

// --- Execution profile ----------------------------------------------------------

export interface ExecutionProfile {
	sizing_mode?: string | null;
	risk_per_trade?: number | null;
	fixed_size?: number | null;
	atr_stop_multiplier?: number | null;
	kelly_multiplier?: number | null;
	kelly_lookback?: number | null;
	stop_loss_pct?: number | null;
	take_profit_pct?: number | null;
	trailing_stop_pct?: number | null;
	time_stop_bars?: number | null;
}

const PROFILE_FIELDS: (keyof ExecutionProfile)[] = [
	'sizing_mode', 'risk_per_trade', 'fixed_size', 'atr_stop_multiplier', 'kelly_multiplier',
	'kelly_lookback', 'stop_loss_pct', 'take_profit_pct', 'trailing_stop_pct', 'time_stop_bars',
];

function positive(value: unknown): number | null {
	const n = typeof value === 'string' ? Number(value) : value;
	return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : null;
}

/** The strategy's own `params.execution_profile` (the only place the engine reads one). */
export function strategyProfile(raw: Record<string, unknown> | undefined | null): ExecutionProfile | null {
	const source = raw?.execution_profile;
	if (!source || typeof source !== 'object' || Array.isArray(source)) return null;
	const out: ExecutionProfile = {};
	for (const field of PROFILE_FIELDS) {
		const value = (source as Record<string, unknown>)[field];
		if (value !== null && value !== undefined) (out as Record<string, unknown>)[field] = value;
	}
	return Object.keys(out).length ? out : null;
}

function exitParts(profile: ExecutionProfile): string[] {
	const parts: string[] = [];
	const stop = positive(profile.stop_loss_pct);
	const target = positive(profile.take_profit_pct);
	const trailing = positive(profile.trailing_stop_pct);
	const bars = positive(profile.time_stop_bars);
	if (stop !== null) parts.push(`${stop}% stop`);
	if (target !== null) parts.push(`${target}% take-profit`);
	if (trailing !== null) parts.push(`${trailing}% trailing stop`);
	if (bars !== null) parts.push(`exit after ${Math.trunc(bars)} bars`);
	return parts;
}

/**
 * True when the engine treats a profile as "no profile" (sizing.normalize_execution_controls):
 * full-equity sizing with no stop, take-profit, trailing stop or time stop.
 */
export function profileIsInert(profile: ExecutionProfile | null | undefined): boolean {
	if (!profile) return true;
	const mode = String(profile.sizing_mode ?? '').trim().toLowerCase();
	const sized = mode !== '' && mode !== 'none' && mode !== 'full';
	return !sized && exitParts(profile).length === 0;
}

export const ENGINE_DEFAULT_SIZING =
	`${ENGINE_DEFAULT_RISK_PCT}% of equity at risk per trade against a ${ENGINE_DEFAULT_ATR_MULTIPLIER}× ATR stop `
	+ `(a ${ENGINE_DEFAULT_STOP_FLOOR_PCT}% stop when ATR is unavailable)`;

function pct(fraction: number): string {
	return `${Number((fraction * 100).toFixed(2))}%`;
}

/** One line saying how positions are sized and closed. */
export function describeProfile(profile: ExecutionProfile | null | undefined): string {
	if (profileIsInert(profile)) return `Engine default: ${ENGINE_DEFAULT_SIZING}`;
	const p = profile as ExecutionProfile;
	const mode = String(p.sizing_mode ?? 'full').trim().toLowerCase();
	const risk = positive(p.risk_per_trade) ?? PROFILE_DEFAULT_RISK_PER_TRADE;
	let sizing: string;
	if (mode === 'fraction') sizing = `${pct(risk)} risk per trade`;
	else if (mode === 'fixed') sizing = `${(positive(p.fixed_size) ?? 0).toLocaleString()} per trade`;
	else if (mode === 'atr') sizing = `${pct(risk)} risk per trade, ${positive(p.atr_stop_multiplier) ?? ENGINE_DEFAULT_ATR_MULTIPLIER}× ATR stop`;
	else if (mode === 'kelly') sizing = 'Kelly sizing (opens no trades in a backtest)';
	else sizing = 'Full equity per trade';
	return [sizing, ...exitParts(p)].join(' · ');
}

// --- Request --------------------------------------------------------------------

export interface ProfileDraft {
	sizingMode: SizingMode;
	riskPct: number | null;
	fixedSize: number | null;
	atrMultiplier: number | null;
	stopLossPct: number | null;
	takeProfitPct: number | null;
	trailingStopPct: number | null;
	timeStopBars: number | null;
}

/** An override form seeded from what the strategy runs with today. */
export function profileDraftFrom(profile: ExecutionProfile | null | undefined): ProfileDraft {
	const inert = profileIsInert(profile);
	const p = profile ?? {};
	const mode = String(p.sizing_mode ?? '').trim().toLowerCase();
	const sizingMode: SizingMode = inert
		? 'atr'
		: mode === 'fraction' || mode === 'fixed' || mode === 'atr' ? mode : 'full';
	const risk = positive(p.risk_per_trade);
	return {
		sizingMode,
		riskPct: inert ? ENGINE_DEFAULT_RISK_PCT : Number(((risk ?? PROFILE_DEFAULT_RISK_PER_TRADE) * 100).toFixed(4)),
		fixedSize: positive(p.fixed_size) ?? 1000,
		atrMultiplier: positive(p.atr_stop_multiplier) ?? ENGINE_DEFAULT_ATR_MULTIPLIER,
		stopLossPct: inert ? null : positive(p.stop_loss_pct),
		takeProfitPct: inert ? null : positive(p.take_profit_pct),
		trailingStopPct: inert ? null : positive(p.trailing_stop_pct),
		timeStopBars: inert ? null : positive(p.time_stop_bars),
	};
}

export function profileFromDraft(draft: ProfileDraft): ExecutionProfile {
	const risked = draft.sizingMode === 'fraction' || draft.sizingMode === 'atr';
	return {
		sizing_mode: draft.sizingMode,
		risk_per_trade: risked && draft.riskPct !== null ? draft.riskPct / 100 : null,
		fixed_size: draft.sizingMode === 'fixed' ? draft.fixedSize : null,
		atr_stop_multiplier: draft.sizingMode === 'atr' ? draft.atrMultiplier : null,
		stop_loss_pct: draft.stopLossPct,
		take_profit_pct: draft.takeProfitPct,
		trailing_stop_pct: draft.trailingStopPct,
		time_stop_bars: draft.timeStopBars,
	};
}

export interface RunSettings {
	strategy: Strategy;
	symbol: string;
	timeframe: string;
	startDate: string;
	endDate: string;
	/** The editable params as the form holds them. */
	params: Record<string, unknown>;
	initialCapital: number | null;
	feeBps: number | null;
	slippageBps: number | null;
	leverage: number | null;
	tradeMode: TradeMode | '';
	/** null = run with the strategy's own execution profile. */
	profile: ProfileDraft | null;
}

/**
 * The params a run sends. A strategy row keeps its stored params (typed, with
 * nested profiles intact), so only the edited keys go. A built-in's scratch row
 * can predate a template change, so the catalog defaults the page shows all go —
 * with a declared `timeframe` param following the chosen timeframe.
 */
export function paramsToSend(settings: RunSettings): Record<string, unknown> {
	const raw = settings.strategy.raw_params ?? {};
	if (!isBuiltin(settings.strategy)) return changedParams(settings.params, editableParams(raw));
	const out: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(raw)) {
		if (!key.startsWith('_')) out[key] = value;
	}
	Object.assign(out, settings.params);
	if ('timeframe' in raw) out.timeframe = settings.timeframe;
	return out;
}

export function buildBacktestRequest(settings: RunSettings) {
	const key = strategyKey(settings.strategy);
	const params = paramsToSend(settings);
	const request: Record<string, unknown> = {
		strategy_id: key,
		strategy_name: key,
		symbol: settings.symbol.trim(),
		timeframe: settings.timeframe,
		start: settings.startDate,
		end: settings.endDate,
		preserve_result: true,
	};
	if (Object.keys(params).length) request.params = params;
	if (settings.initialCapital !== null) request.initial_capital = settings.initialCapital;
	if (settings.feeBps !== null) request.fee_bps = settings.feeBps;
	if (settings.slippageBps !== null) request.slippage_bps = settings.slippageBps;
	if (settings.leverage !== null) request.leverage = settings.leverage;
	if (settings.tradeMode) request.trade_mode = settings.tradeMode;
	if (settings.profile) {
		for (const [field, value] of Object.entries(profileFromDraft(settings.profile))) {
			if (value !== null && value !== undefined) request[field] = value;
		}
	}
	return request as {
		strategy_id: string;
		strategy_name: string;
		symbol: string;
		timeframe: string;
		start: string;
		end: string;
		preserve_result: boolean;
		params?: Record<string, unknown>;
		initial_capital?: number;
		fee_bps?: number;
		slippage_bps?: number;
		leverage?: number;
		trade_mode?: TradeMode;
		sizing_mode?: SizingMode;
		risk_per_trade?: number;
		fixed_size?: number;
		atr_stop_multiplier?: number;
		stop_loss_pct?: number;
		take_profit_pct?: number;
		trailing_stop_pct?: number;
		time_stop_bars?: number;
	};
}

/** Short labels for what a run changed from the strategy as it stands. */
export function overrideLabels(settings: RunSettings): string[] {
	const labels: string[] = [];
	const edited = isBuiltin(settings.strategy)
		? changedParams(settings.params, editableParams(settings.strategy.raw_params))
		: paramsToSend(settings);
	const count = Object.keys(edited).length;
	if (count) labels.push(`${count} param${count === 1 ? '' : 's'} changed`);
	if (settings.tradeMode) labels.push(TRADE_MODE_LABELS[settings.tradeMode]);
	if (settings.leverage !== null) labels.push(`${settings.leverage}× leverage`);
	if (settings.feeBps !== null || settings.slippageBps !== null) labels.push('custom costs');
	if (settings.initialCapital !== null) labels.push(`capital ${settings.initialCapital.toLocaleString()}`);
	if (settings.profile) labels.push(describeProfile(profileFromDraft(settings.profile)));
	return labels;
}

function outOfRange(value: number | null, lo: number, hi: number, { inclusiveLo = false } = {}): boolean {
	if (value === null) return false;
	if (!Number.isFinite(value)) return true;
	return (inclusiveLo ? value < lo : value <= lo) || value > hi;
}

/** The first problem that would make the backend refuse (or silently reshape) the run. */
export function validateRun(
	settings: RunSettings,
	{ estimatedBars, today }: { estimatedBars: number | null; today: string },
): string | null {
	if (!settings.symbol.trim()) return 'Enter a symbol.';
	if (!settings.startDate || !settings.endDate) return 'Choose a start and end date.';
	if (settings.startDate >= settings.endDate) return 'The start date must be before the end date.';
	if (settings.endDate > today) return 'The end date cannot be in the future.';
	if (estimatedBars !== null && estimatedBars > LIMITS.barCap) {
		return `This window is ~${estimatedBars.toLocaleString()} bars; the engine caps a run at ${LIMITS.barCap.toLocaleString()}. Shorten it or pick a longer timeframe.`;
	}
	if (outOfRange(settings.initialCapital, 0, LIMITS.maxCapital)) return 'Initial capital must be above 0.';
	if (outOfRange(settings.feeBps, 0, LIMITS.maxCostBps, { inclusiveLo: true })) return `Fees must be between 0 and ${LIMITS.maxCostBps} bps.`;
	if (outOfRange(settings.slippageBps, 0, LIMITS.maxCostBps, { inclusiveLo: true })) return `Slippage must be between 0 and ${LIMITS.maxCostBps} bps.`;
	if (outOfRange(settings.leverage, 0, LIMITS.maxLeverage)) return `Leverage must be above 0 and at most ${LIMITS.maxLeverage}×.`;
	const draft = settings.profile;
	if (draft) {
		const risked = draft.sizingMode === 'fraction' || draft.sizingMode === 'atr';
		if (risked && (draft.riskPct === null || outOfRange(draft.riskPct, 0, 100))) return 'Risk per trade must be above 0% and at most 100%.';
		if (draft.sizingMode === 'fixed' && (draft.fixedSize === null || outOfRange(draft.fixedSize, 0, LIMITS.maxCapital))) return 'Enter a fixed position size above 0.';
		if (draft.sizingMode === 'atr' && (draft.atrMultiplier === null || outOfRange(draft.atrMultiplier, 0, LIMITS.maxAtrMultiplier))) return `The ATR stop multiplier must be above 0 and at most ${LIMITS.maxAtrMultiplier}.`;
		if (outOfRange(draft.stopLossPct, 0, 100)) return 'Stop loss must be above 0% and at most 100%.';
		if (outOfRange(draft.takeProfitPct, 0, LIMITS.maxTakeProfitPct)) return `Take-profit must be above 0% and at most ${LIMITS.maxTakeProfitPct}%.`;
		if (outOfRange(draft.trailingStopPct, 0, 100)) return 'Trailing stop must be above 0% and at most 100%.';
		if (draft.timeStopBars !== null && (!Number.isInteger(draft.timeStopBars) || outOfRange(draft.timeStopBars, 0, LIMITS.maxTimeStopBars))) {
			return 'The time stop must be a whole number of bars.';
		}
		if (draft.sizingMode === 'fraction' && draft.stopLossPct === null && draft.trailingStopPct === null) {
			return 'Risk-based sizing needs a stop loss or trailing stop to size against.';
		}
		if (draft.sizingMode === 'full' && profileIsInert(profileFromDraft(draft))) {
			return 'Full-equity sizing needs a stop, take-profit, trailing stop or time stop. Without one the engine treats the profile as empty and uses its default sizing.';
		}
	}
	return null;
}

// --- Result window ----------------------------------------------------------------

function dateOnly(value: unknown): string | null {
	const text = typeof value === 'string' ? value.trim() : '';
	if (!text) return null;
	const parsed = new Date(text.includes('T') || text.includes(' ') ? text.replace(' ', 'T') : `${text}T00:00:00Z`);
	return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString().slice(0, 10);
}

export interface TestedWindow {
	start: string | null;
	end: string | null;
	inSampleStart: string | null;
	inSampleEnd: string | null;
	outOfSampleStart: string | null;
	outOfSampleEnd: string | null;
}

function nested(metrics: Record<string, unknown> | undefined, key: string): Record<string, unknown> {
	const value = metrics?.[key];
	return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** The window the engine actually tested (it can differ from the one requested). */
export function testedWindow(result: BacktestResult): TestedWindow {
	const metrics = result.metrics as Record<string, unknown> | undefined;
	const inSample = nested(metrics, 'in_sample');
	const outOfSample = nested(metrics, 'out_of_sample');
	const curve = result.equity_curve ?? [];
	const record = result as unknown as Record<string, unknown>;
	return {
		start: dateOnly(record.start) ?? dateOnly(inSample.start_date) ?? dateOnly(curve[0]?.timestamp),
		end: dateOnly(record.end) ?? dateOnly(outOfSample.end_date) ?? dateOnly(curve[curve.length - 1]?.timestamp),
		inSampleStart: dateOnly(inSample.start_date),
		inSampleEnd: dateOnly(inSample.end_date),
		outOfSampleStart: dateOnly(outOfSample.start_date) ?? dateOnly(curve[0]?.timestamp),
		outOfSampleEnd: dateOnly(outOfSample.end_date) ?? dateOnly(curve[curve.length - 1]?.timestamp),
	};
}

/**
 * True when the research holdout moved a requested window: it asked for data at
 * or after the cutoff, and the engine ended the test at the cutoff instead.
 */
export function shiftedByHoldout(requestedEnd: string, cutoff: string | null | undefined): boolean {
	const cutoffDay = dateOnly(cutoff ?? '');
	return Boolean(cutoffDay && requestedEnd && requestedEnd > cutoffDay);
}

// --- Result numbers ---------------------------------------------------------------

export interface PeriodStats {
	totalReturnPct: number | null;
	sharpe: number | null;
	maxDrawdownPct: number | null;
	winRatePct: number | null;
	trades: number | null;
}

function num(value: unknown): number | null {
	const n = typeof value === 'string' ? Number(value) : value;
	return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

/** The engine stores these as fractions even where the key says `_pct`. */
function fractionToPct(value: unknown): number | null {
	const n = num(value);
	return n === null ? null : n * 100;
}

export function periodStats(block: Record<string, unknown>): PeriodStats {
	return {
		totalReturnPct: fractionToPct(block.total_return_pct ?? block.total_return),
		sharpe: num(block.sharpe ?? block.sharpe_ratio),
		maxDrawdownPct: (() => {
			const v = fractionToPct(block.max_drawdown_pct ?? block.max_drawdown);
			return v === null ? null : Math.abs(v);
		})(),
		winRatePct: (() => {
			const v = num(block.win_rate);
			return v === null ? null : v <= 1 ? v * 100 : v;
		})(),
		trades: num(block.total_trades ?? block.trades),
	};
}

/** In-sample next to out-of-sample, when the result carries both. */
export function sampleSplit(result: BacktestResult): { inSample: PeriodStats; outOfSample: PeriodStats } | null {
	const metrics = result.metrics as Record<string, unknown> | undefined;
	const inSample = nested(metrics, 'in_sample');
	if (!Object.keys(inSample).length) return null;
	const outOfSample = nested(metrics, 'out_of_sample');
	return {
		inSample: periodStats(inSample),
		outOfSample: periodStats(Object.keys(outOfSample).length ? outOfSample : (metrics ?? {})),
	};
}

/**
 * A plain-language flag when out-of-sample results fall well short of
 * in-sample: the classic overfitting signature. Null when nothing stands out.
 */
export function decayNote(split: { inSample: PeriodStats; outOfSample: PeriodStats } | null): string | null {
	if (!split) return null;
	const is = split.inSample.sharpe;
	const oos = split.outOfSample.sharpe;
	if (is === null || oos === null || is <= 0.5) return null;
	if (oos <= 0) return `Sharpe was ${is.toFixed(2)} in-sample but ${oos.toFixed(2)} out-of-sample: the edge did not carry over to unseen data.`;
	if (oos < is / 2) return `Out-of-sample Sharpe (${oos.toFixed(2)}) is less than half the in-sample figure (${is.toFixed(2)}), a common sign of overfitting.`;
	return null;
}
