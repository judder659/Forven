import { describe, expect, it } from 'vitest';
import {
	buildExportFilename,
	parseEnvelope,
	serializeEnvelope,
} from '../lib/utils/strategyPortability';

function validEnvelope(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		forven_export: {
			kind: 'strategy_container',
			version: '1.0',
			exported_at: '2026-06-22T00:00:00+00:00',
			source_strategy_id: 'S00123',
			source_display_id: 'S00123',
		},
		strategy: { id: 'S00123', name: 'BTC RSI Momentum', type: 'rsi_momentum' },
		configuration: { type: 'rsi_momentum', symbol: 'BTC/USDT', timeframe: '1h', params: { rsi_period: 14 } },
		history: { backtests: [{ result_id: 'BR-1' }, { result_id: 'BR-2' }] },
		execution: { trades: [{ id: 'T-1' }] },
		events: [{ id: 'E-1' }],
		...overrides,
	};
}

describe('buildExportFilename', () => {
	it('slugifies id + name', () => {
		expect(buildExportFilename('S00123', 'BTC RSI Momentum')).toBe('forven-S00123-btc-rsi-momentum.json');
	});

	it('falls back to id only when name is empty', () => {
		expect(buildExportFilename('S00123')).toBe('forven-S00123.json');
	});

	it('sanitizes unsafe id characters', () => {
		expect(buildExportFilename('S 00/123')).toBe('forven-S_00_123.json');
	});
});

describe('parseEnvelope', () => {
	it('parses a valid envelope and summarizes it', () => {
		const text = serializeEnvelope(validEnvelope());
		const parsed = parseEnvelope(text);
		expect(parsed.meta.kind).toBe('strategy_container');
		expect(parsed.meta.version).toBe('1.0');
		expect(parsed.meta.sourceId).toBe('S00123');
		expect(parsed.summary.type).toBe('rsi_momentum');
		expect(parsed.summary.symbol).toBe('BTC/USDT');
		expect(parsed.summary.backtests).toBe(2);
		expect(parsed.summary.trades).toBe(1);
		expect(parsed.summary.events).toBe(1);
	});

	it('throws on invalid JSON', () => {
		expect(() => parseEnvelope('{not json')).toThrow(/valid JSON/i);
	});

	it('throws when forven_export metadata is missing', () => {
		const text = JSON.stringify({ strategy: {}, configuration: {} });
		expect(() => parseEnvelope(text)).toThrow(/not a Forven strategy export/i);
	});

	it('throws on an unsupported export kind', () => {
		const text = JSON.stringify({ forven_export: { kind: 'dataset', version: '1.0' } });
		expect(() => parseEnvelope(text)).toThrow(/Unsupported export kind/i);
	});

	it('rejects a non-object top level', () => {
		expect(() => parseEnvelope('[1,2,3]')).toThrow(/JSON object/i);
	});
});
