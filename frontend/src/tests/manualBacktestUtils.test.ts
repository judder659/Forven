import { describe, expect, it } from 'vitest';
import type { BacktestResult, Strategy } from '$lib/api';
import { normalizeStrategyPayload } from '$lib/api/strategies';
import {
	buildBacktestRequest,
	changedParams,
	decayNote,
	describeProfile,
	editableParams,
	isBuiltin,
	nativeMarket,
	overrideLabels,
	profileDraftFrom,
	profileIsInert,
	sameMarket,
	sampleSplit,
	shiftedByHoldout,
	strategyOptionLabel,
	testedWindow,
	validateRun,
	type RunSettings,
} from '$lib/utils/manualBacktest';

// A paper strategy row as GET /api/strategies returns it: params is a JSON blob.
const paperRow = {
	id: 'S07239',
	display_id: 'S07239',
	name: 'BTC-BTC_KC63202_PULLBACK_THRUST-S07239',
	type: 'btc_kc63202_pullback_thrust',
	symbol: 'BTC/USDT',
	timeframe: '4h',
	stage: 'paper',
	status: 'paper',
	source: 'ai_dropzone',
	params: JSON.stringify({
		kc_period: 20,
		thrust: 1.5,
		leverage: 1.3,
		_asset: 'BTC',
		_data_requirements: [{ column: 'iv' }],
		execution_profile: { sizing_mode: 'atr', risk_per_trade: 0.01, atr_stop_multiplier: 2.5, take_profit_pct: 6 },
	}),
};

// A built-in catalog entry as GET /api/strategies/prebuilt returns it.
const catalogEntry = {
	name: 'ETH 15m EMA Trend',
	api_name: 'ema_cross_eth_15m',
	type: 'ema_cross_eth_15m',
	version: '1.0.0',
	description: 'EMA trend on ETH 15m',
	source: 'prebuilt',
	asset: 'ETH',
	timeframe: '15m',
	trade_modes: ['long_only'],
	default_trade_mode: 'long_only',
	parameters: {
		fast: { type: 'number', default: 12, min: 5, max: 30, step: 1 },
		slow: { type: 'number', default: 48 },
		timeframe: { type: 'str', default: '15m' },
		leverage: { type: 'number', default: 1.0 },
		hours: { type: 'list', default: [8, 9, 10] },
	},
};

const [paper] = normalizeStrategyPayload([paperRow]).strategies;
const [builtin] = normalizeStrategyPayload({ strategies: [catalogEntry] }).strategies;

function settings(strategy: Strategy, overrides: Partial<RunSettings> = {}): RunSettings {
	return {
		strategy,
		symbol: 'BTC/USDT',
		timeframe: '4h',
		startDate: '2024-01-01',
		endDate: '2026-01-01',
		params: editableParams(strategy.raw_params),
		initialCapital: null,
		feeBps: null,
		slippageBps: null,
		leverage: null,
		tradeMode: '',
		profile: null,
		...overrides,
	};
}

describe('strategy normalization keeps what the page needs', () => {
	it('keeps a row market, id, stage and nested params intact', () => {
		expect(paper.symbol).toBe('BTC/USDT');
		expect(paper.timeframe).toBe('4h');
		expect(paper.display_id).toBe('S07239');
		expect(paper.stage).toBe('paper');
		expect(paper.raw_params?.execution_profile).toEqual({
			sizing_mode: 'atr', risk_per_trade: 0.01, atr_stop_multiplier: 2.5, take_profit_pct: 6,
		});
		expect(paper.raw_params?._data_requirements).toEqual([{ column: 'iv' }]);
		// The legacy ParamSpec view still stringifies (other pages read it).
		expect(typeof paper.parameters.execution_profile.default).toBe('string');
	});

	it('reads a catalog entry market, trade modes and raw defaults', () => {
		expect(isBuiltin(builtin)).toBe(true);
		expect(isBuiltin(paper)).toBe(false);
		expect(builtin.asset).toBe('ETH');
		expect(builtin.trade_modes).toEqual(['long_only']);
		expect(builtin.raw_params?.hours).toEqual([8, 9, 10]);
		expect(nativeMarket(builtin)).toEqual({ symbol: 'ETH/USDT', timeframe: '15m' });
		expect(nativeMarket(paper)).toEqual({ symbol: 'BTC/USDT', timeframe: '4h' });
	});

	it('labels rows by id, market and readable type', () => {
		expect(strategyOptionLabel(paper)).toBe('S07239 · BTC 4h · btc kc63202 pullback thrust');
		expect(strategyOptionLabel(builtin)).toBe('ETH 15m EMA Trend');
	});
});

describe('params', () => {
	it('hides contract fields and the controls other sections own', () => {
		expect(Object.keys(editableParams(paper.raw_params)).sort()).toEqual(['kc_period', 'thrust']);
		expect(Object.keys(editableParams(builtin.raw_params)).sort()).toEqual(['fast', 'hours', 'slow']);
	});

	it('diffs structurally', () => {
		expect(changedParams({ a: 1, b: [1, 2], c: { x: 1 } }, { a: 1, b: [1, 2], c: { x: 2 } })).toEqual({ c: { x: 1 } });
	});

	it('sends a row only its edited params, never its stored blobs', () => {
		expect(buildBacktestRequest(settings(paper)).params).toBeUndefined();
		const edited = buildBacktestRequest(settings(paper, { params: { kc_period: 25, thrust: 1.5 } }));
		expect(edited.params).toEqual({ kc_period: 25 });
	});

	it('sends a built-in every template default, with its timeframe following the market', () => {
		const request = buildBacktestRequest(settings(builtin, { timeframe: '1h', params: { fast: 10, slow: 48, hours: [8, 9, 10] } }));
		expect(request.params).toEqual({ fast: 10, slow: 48, hours: [8, 9, 10], leverage: 1.0, timeframe: '1h' });
	});
});

describe('the request carries only what was changed', () => {
	it('leaves every execution setting to the strategy by default', () => {
		const request = buildBacktestRequest(settings(paper));
		expect(request).toEqual({
			strategy_id: 'S07239', strategy_name: 'S07239', symbol: 'BTC/USDT', timeframe: '4h',
			start: '2024-01-01', end: '2026-01-01', preserve_result: true,
		});
		expect(overrideLabels(settings(paper))).toEqual([]);
	});

	it('sends explicit overrides and a complete replacement profile', () => {
		const draft = { ...profileDraftFrom(null), sizingMode: 'fraction' as const, riskPct: 2, stopLossPct: 4 };
		const request = buildBacktestRequest(settings(paper, {
			feeBps: 10, slippageBps: 5, leverage: 2, tradeMode: 'short_only', initialCapital: 25_000, profile: draft,
		}));
		expect(request).toMatchObject({
			fee_bps: 10, slippage_bps: 5, leverage: 2, trade_mode: 'short_only', initial_capital: 25_000,
			sizing_mode: 'fraction', risk_per_trade: 0.02, stop_loss_pct: 4,
		});
		expect(request).not.toHaveProperty('fixed_size');
		expect(request).not.toHaveProperty('atr_stop_multiplier');
	});
});

describe('execution profile', () => {
	it('describes the engine default when there is no profile', () => {
		expect(profileIsInert(null)).toBe(true);
		expect(profileIsInert({ sizing_mode: 'full' })).toBe(true);
		expect(profileIsInert({ sizing_mode: 'full', stop_loss_pct: 5 })).toBe(false);
		expect(describeProfile(null)).toContain('1% of equity at risk per trade against a 2× ATR stop');
	});

	it('describes a stored profile', () => {
		expect(describeProfile({ sizing_mode: 'atr', risk_per_trade: 0.01, atr_stop_multiplier: 2.5, take_profit_pct: 6 }))
			.toBe('1% risk per trade, 2.5× ATR stop · 6% take-profit');
	});

	it('seeds an override from the stored profile, or from the engine default', () => {
		expect(profileDraftFrom({ sizing_mode: 'fraction', risk_per_trade: 0.03, stop_loss_pct: 5 }))
			.toMatchObject({ sizingMode: 'fraction', riskPct: 3, stopLossPct: 5 });
		expect(profileDraftFrom(null)).toMatchObject({ sizingMode: 'atr', riskPct: 1, atrMultiplier: 2, stopLossPct: null });
	});
});

describe('validation mirrors what the backend accepts', () => {
	const ok = { estimatedBars: 8000, today: '2026-09-28' };

	it('accepts the defaults', () => {
		expect(validateRun(settings(paper), ok)).toBeNull();
	});

	it.each([
		[{ startDate: '2026-02-01', endDate: '2026-01-01' }, 'start date must be before'],
		[{ endDate: '2026-12-01' }, 'cannot be in the future'],
		[{ feeBps: 1200 }, 'Fees must be between'],
		[{ leverage: 0 }, 'Leverage must be above 0'],
		[{ leverage: Number.NaN }, 'Leverage must be above 0'],
		[{ initialCapital: -5 }, 'Initial capital'],
		[{ profile: { ...profileDraftFrom(null), sizingMode: 'fraction' as const, riskPct: 2 } }, 'needs a stop loss or trailing stop'],
		[{ profile: { ...profileDraftFrom(null), sizingMode: 'full' as const } }, 'Full-equity sizing needs'],
		[{ profile: { ...profileDraftFrom(null), timeStopBars: 2.5 } }, 'whole number of bars'],
		[{ profile: { ...profileDraftFrom(null), takeProfitPct: 5000 } }, 'Take-profit must be'],
	])('rejects %o', (overrides, message) => {
		expect(validateRun(settings(paper, overrides as Partial<RunSettings>), ok)).toContain(message);
	});

	it('rejects a window over the engine bar cap', () => {
		expect(validateRun(settings(paper), { estimatedBars: 150_000, today: '2026-09-28' })).toContain('caps a run');
	});
});

describe('market', () => {
	it('compares on the base asset', () => {
		expect(sameMarket({ symbol: 'BTC/USDT', timeframe: '4h' }, 'BTCUSDT', '4h')).toBe(true);
		expect(sameMarket({ symbol: 'BTC/USDT', timeframe: '4h' }, 'BTC/USDT', '1h')).toBe(false);
		expect(sameMarket({ symbol: 'ETH/USDT', timeframe: null }, 'ETH', '1d')).toBe(true);
	});
});

describe('result window', () => {
	const result = {
		id: 'R1', job_id: 'j', strategy_name: 's', strategy_version: '1', symbol: 'BTC', timeframe: '1h', created_at: '',
		start: '2024-12-23T06:00:00+00:00', end: '2025-12-31T23:00:00+00:00',
		config: {},
		metrics: {
			total_return: -0.1, sharpe_ratio: 0.4, max_drawdown: 0.2, win_rate: 0.4, total_trades: 50,
			in_sample: { start_date: '2024-12-23T06:00:00+00:00', end_date: '2025-09-10T21:00:00+00:00', sharpe: 2.1, total_return_pct: 0.25, total_trades: 90, win_rate: 0.5, max_drawdown_pct: 0.125 },
			out_of_sample: { start_date: '2025-09-10T21:00:00+00:00', end_date: '2025-12-31T23:00:00+00:00', sharpe: 0.4, total_return_pct: -0.1, total_trades: 50, win_rate: 0.4, max_drawdown_pct: 0.2 },
		},
	} as unknown as BacktestResult;

	it('reports the tested and out-of-sample dates', () => {
		expect(testedWindow(result)).toEqual({
			start: '2024-12-23', end: '2025-12-31',
			inSampleStart: '2024-12-23', inSampleEnd: '2025-09-10',
			outOfSampleStart: '2025-09-10', outOfSampleEnd: '2025-12-31',
		});
	});

	it('knows when the holdout moved the window', () => {
		expect(shiftedByHoldout('2026-09-28', '2026-01-01T00:00:00+00:00')).toBe(true);
		expect(shiftedByHoldout('2026-01-01', '2026-01-01T00:00:00+00:00')).toBe(false);
		expect(shiftedByHoldout('2026-09-28', null)).toBe(false);
	});

	it('puts in-sample next to out-of-sample and flags the decay', () => {
		const split = sampleSplit(result);
		expect(split?.inSample).toEqual({ totalReturnPct: 25, sharpe: 2.1, maxDrawdownPct: 12.5, winRatePct: 50, trades: 90 });
		expect(split?.outOfSample.sharpe).toBe(0.4);
		expect(decayNote(split)).toContain('less than half');
		expect(decayNote(null)).toBeNull();
	});
});
