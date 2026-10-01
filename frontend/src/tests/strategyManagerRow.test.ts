import { describe, expect, it } from 'vitest';

import { parseManagerRow, tradesPerMonth } from '../lib/utils/strategy';

describe('parseManagerRow', () => {
	it('preserves AI Drop Zone provenance and backtest readiness flags', () => {
		const row = parseManagerRow({
			id: 'S00999',
			name: 'BTC-AI_DROPZONE_WAVE-S00999',
			symbol: 'BTC',
			timeframe: '1h',
			stage: 'quick_screen',
			source: 'ai_dropzone',
			source_ref: 'btc_ai_dropzone_wave_test.py',
			has_backtest_results: false,
			created_at: '2026-03-15T00:00:00Z'
		});

		expect(row.source).toBe('ai_dropzone');
		expect(row.source_ref).toBe('btc_ai_dropzone_wave_test.py');
		expect(row.has_backtest_results).toBe(false);
	});

	it('treats best backtest ids as evidence of a completed backtest', () => {
		const row = parseManagerRow({
			id: 'S01000',
			name: 'BTC-AI_DROPZONE_WAVE-S01000',
			symbol: 'BTC',
			timeframe: '1h',
			stage: 'quick_screen',
			source: 'ai_dropzone',
			best_backtest_result_id: 'B12345',
			created_at: '2026-03-15T00:00:00Z'
		});

		expect(row.has_backtest_results).toBe(true);
	});

	it('preserves recovery metadata from the backend payload', () => {
		const row = parseManagerRow({
			id: 'S50001',
			name: 'BTC-RSI-S50001',
			symbol: 'BTC',
			timeframe: '1h',
			stage: 'gauntlet',
			has_backtest_results: false,
			recovery_active: true,
			recovery_status: 'repair_pending',
			recovery_attempt_count: 2,
			recovery_last_error: 'backtest worker crashed',
			recovery_cooldown_until: '2026-04-08T12:00:00Z',
			created_at: '2026-04-08T00:00:00Z'
		});

		expect(row.recovery_active).toBe(true);
		expect(row.recovery_status).toBe('repair_pending');
		expect(row.recovery_attempt_count).toBe(2);
		expect(row.recovery_last_error).toBe('backtest worker crashed');
		expect(row.recovery_cooldown_until).toBe('2026-04-08T12:00:00Z');
	});

	it('preserves parent hypothesis linkage from the backend payload', () => {
		const row = parseManagerRow({
			id: 'S70001',
			name: 'BTC-FUNDING-FADE-S70001',
			symbol: 'BTC-PERP',
			timeframe: '15m',
			stage: 'quick_screen',
			hypothesis_id: 'HYP-001',
			hypothesis_display_id: 'H00001',
			created_at: '2026-04-14T00:00:00Z'
		});

		expect(row.hypothesis_id).toBe('HYP-001');
		expect(row.hypothesis_display_id).toBe('H00001');
	});
});

describe('tradesPerMonth', () => {
	it('divides backtest trades by the full test window', () => {
		expect(tradesPerMonth({ total_trades: 120, backtest_months: 24 })).toBe(5);
	});

	it('is null when the count or window is missing or empty', () => {
		expect(tradesPerMonth({ total_trades: null, backtest_months: 12 })).toBeNull();
		expect(tradesPerMonth({ total_trades: 30, backtest_months: null })).toBeNull();
		expect(tradesPerMonth({ total_trades: 30, backtest_months: 0 })).toBeNull();
	});

	it('reads the combined window parsed from the list payload', () => {
		const row = parseManagerRow({
			id: 'S01001',
			name: 'BTC-TREND-S01001',
			stage: 'gauntlet',
			metrics: { total_trades: 90, backtest_months: 18 }
		});
		expect(tradesPerMonth(row)).toBe(5);
	});
});
