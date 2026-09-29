import { describe, expect, it } from 'vitest';

import type { LifecycleStrategy, StrategyContainerHistoryItem } from '../lib/api/lifecycle';
import { buildQuickScreenEvidenceRows } from '../lib/utils/quickScreenReadiness';

function buildBacktest(metrics: Record<string, unknown>): StrategyContainerHistoryItem {
	return {
		result_id: 'B1001',
		strategy_id: 'S0001',
		result_type: 'backtest',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		start_date: '2025-01-01T00:00:00Z',
		end_date: '2025-12-31T00:00:00Z',
		metrics,
		config: {},
		created_at: '2026-04-01T00:00:00Z',
		deleted_at: null,
	};
}

// The live install's pipeline thresholds (quick_screen section): what
// policy._evaluate_quick_screen_gate reads.
const gateThresholds = {
	min_total_return_pct: 0.0,
	max_drawdown_pct: 0.3,
	min_sharpe: 0.0,
	min_trades: 20,
	min_profit_factor: 1.05,
};

function buildStrategy(metrics: LifecycleStrategy['metrics'] = null): LifecycleStrategy {
	return {
		id: 'S0001',
		display_id: 'S0001',
		hypothesis_id: null,
		hypothesis_display_id: null,
		name: 'BTC Trend Candidate',
		type: 'rsi_momentum',
		state: 'quick_screen',
		source: 'manual',
		source_ref: null,
		owner: 'brain',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		definition_json: null,
		dataset_hash: null,
		policy_version: 1,
		build_version: null,
		metrics_json: null,
		metrics,
		paper_session_id: null,
		paper_started_at: null,
		last_policy_result_json: null,
		blocked_reason: null,
		model: null,
		model_id: null,
		created_at: '2026-04-01T00:00:00Z',
		updated_at: '2026-04-01T00:00:00Z',
		state_changed_at: null,
		failed_at: null,
		retention_expires_at: null,
	};
}

// Engine units: fractions for returns/drawdowns/win rate. The top-level scalars of a
// container payload are a full-window overlay, so they deliberately disagree with the
// nested slices here — the rows must read the slices, as the gate does.
function engineMetrics(
	overrides: { in_sample?: Record<string, unknown>; out_of_sample?: Record<string, unknown> } = {},
): LifecycleStrategy['metrics'] {
	const metrics = {
		total_trades: 165,
		total_return_pct: 0.9,
		max_drawdown_pct: 0.05,
		win_rate: 0.4,
		in_sample: { total_trades: 120, sharpe: 0.82, profit_factor: 1.4, win_rate: 0.41, ...overrides.in_sample },
		out_of_sample: {
			total_trades: 45,
			sharpe: 0.6,
			profit_factor: 1.2,
			total_return_pct: 0.124,
			max_drawdown_pct: 0.187,
			win_rate: 0.38,
			...overrides.out_of_sample,
		},
	};
	// Partial on purpose: the slices are what the gate reads.
	return metrics as unknown as LifecycleStrategy['metrics'];
}

describe('buildQuickScreenEvidenceRows', () => {
	it('mirrors the quick-screen gate checks from the nested slices, in engine units', () => {
		const rows = buildQuickScreenEvidenceRows({
			strategy: buildStrategy(engineMetrics()),
			backtests: [],
			thresholds: gateThresholds,
		});

		expect(rows.map((row) => [row.key, row.status, row.actual, row.required])).toEqual([
			['trade_count', 'passed', '120', '≥ 20'],
			['is_sharpe_ratio', 'passed', '0.82', '> 0.10'],
			['profit_factor', 'passed', '1.20', '≥ 1.05'],
			['minimum_return', 'passed', '12.4%', '≥ 0%'],
			['max_drawdown', 'passed', '18.7%', '≤ 30%'],
			['oos_sharpe_ratio', 'passed', '0.60', '≥ 0.00'],
		]);
	});

	it('judges against the gate thresholds, not the Settings page values', () => {
		// Settings carries min_sharpe_ratio 0.5 / max_drawdown_pct 40; the gate reads the
		// thresholds. An IS Sharpe of 0.3 passes the gate's 0.1 floor, and a 35% OOS
		// drawdown fails its 30% limit.
		const rows = buildQuickScreenEvidenceRows({
			strategy: buildStrategy(
				engineMetrics({ in_sample: { sharpe: 0.3 }, out_of_sample: { max_drawdown_pct: 0.35, profit_factor: 1.02 } }),
			),
			backtests: [],
			thresholds: gateThresholds,
		});
		const status = Object.fromEntries(rows.map((row) => [row.key, row.status]));
		expect(status.is_sharpe_ratio).toBe('passed');
		expect(status.max_drawdown).toBe('failed');
		// The weaker slice's profit factor decides (IS 1.4, OOS 1.02 < 1.05).
		expect(status.profit_factor).toBe('failed');
	});

	it('falls back to the gate defaults when the thresholds did not load', () => {
		const rows = buildQuickScreenEvidenceRows({ strategy: buildStrategy(engineMetrics()), backtests: [], thresholds: null });
		expect(Object.fromEntries(rows.map((row) => [row.key, row.required]))).toMatchObject({
			trade_count: '≥ 20',
			profit_factor: '≥ 1.00',
			max_drawdown: '≤ 30%',
		});
	});

	it('reads legacy percent-point slices without rescaling them', () => {
		const rows = buildQuickScreenEvidenceRows({
			strategy: null,
			backtests: [
				buildBacktest({
					in_sample: { sharpe: 0.9, profit_factor: 1.3, win_rate: 52 },
					out_of_sample: { sharpe: 0.7, profit_factor: 1.2, total_return_pct: 12.4, max_drawdown_pct: 18.7, win_rate: 48 },
				}),
			],
			thresholds: gateThresholds,
		});
		const actual = Object.fromEntries(rows.map((row) => [row.key, row.actual]));
		expect(actual.minimum_return).toBe('12.4%');
		expect(actual.max_drawdown).toBe('18.7%');
	});

	it('does NOT gate on the gauntlet validation suite at quick-screen', () => {
		// Regression: a 'Validation Coverage' row requiring 5 robustness artifacts used to be
		// emitted here, false-blocking every candidate at 0/5 since that suite only runs INSIDE
		// the gauntlet. The requirement belongs to gauntlet -> paper readiness, not quick-screen.
		const rows = buildQuickScreenEvidenceRows({ strategy: buildStrategy(engineMetrics()), backtests: [], thresholds: gateThresholds });
		expect(rows.some((row) => row.key === 'validation_coverage')).toBe(false);
	});

	it('marks missing evidence as warning rows', () => {
		const rows = buildQuickScreenEvidenceRows({ strategy: null, backtests: [], thresholds: gateThresholds });

		expect(rows.every((row) => row.status === 'warning')).toBe(true);
		expect(rows.find((row) => row.key === 'is_sharpe_ratio')).toEqual({
			key: 'is_sharpe_ratio',
			label: 'IS Sharpe Ratio',
			status: 'warning',
			actual: 'Unavailable',
			required: '> 0.10',
			detail: 'Unavailable | Required > 0.10',
		});
	});
});
