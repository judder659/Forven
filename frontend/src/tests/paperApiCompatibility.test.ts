import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
	adjustPaperStopLoss,
	adjustPaperTakeProfit,
	closePaperPosition,
	createPaperSession,
	deletePaperSession,
	flipPaperPosition,
	getPaperWebSocketUrl,
	openManualPaperPosition,
	partialClosePaperPosition,
	replayPause,
	replayPlay,
	replayReset,
	replaySeek,
	replaySetSpeed,
	replayStep,
	setPaperAutoManagement,
	startPaperSession,
	stopPaperSession,
	updatePaperSession,
} from '../lib/api/paper';

const mockFetch = vi.fn();
globalThis.fetch = mockFetch as unknown as typeof fetch;

describe('paper compatibility API client', () => {
	beforeEach(() => {
		mockFetch.mockReset();
	});

	it('rejects unsupported standalone session mutations before fetch', async () => {
		await expect(createPaperSession('Strategy', 'BTC/USDT')).rejects.toThrow('not supported');
		await expect(updatePaperSession('compat:strategy:S00001', { symbol: 'ETH/USDT' })).rejects.toThrow('not supported');
		await expect(startPaperSession('compat:strategy:S00001')).rejects.toThrow('not supported');
		await expect(stopPaperSession('compat:strategy:S00001')).rejects.toThrow('not supported');
		await expect(deletePaperSession('compat:strategy:S00001')).rejects.toThrow('not supported');

		expect(mockFetch).not.toHaveBeenCalled();
	});

	it('issues POST requests for manual position controls', async () => {
		const okSession = { ok: true, json: () => Promise.resolve({ id: 'compat:strategy:S00001' }) };
		mockFetch.mockResolvedValue(okSession);
		const sessionId = 'compat:strategy:S00001';
		const cases: Array<[Promise<unknown>, string]> = [
			[closePaperPosition(sessionId, 'done'), '/paper/sessions/compat:strategy:S00001/close-position'],
			[partialClosePaperPosition(sessionId, { pct: 50 }), '/paper/sessions/compat:strategy:S00001/partial-close'],
			[openManualPaperPosition(sessionId, { direction: 'long', size: 1 }), '/paper/sessions/compat:strategy:S00001/open-position'],
			[adjustPaperStopLoss(sessionId, 100), '/paper/sessions/compat:strategy:S00001/position/stop-loss'],
			[adjustPaperTakeProfit(sessionId, 200), '/paper/sessions/compat:strategy:S00001/position/take-profit'],
			[flipPaperPosition(sessionId), '/paper/sessions/compat:strategy:S00001/flip'],
			[setPaperAutoManagement(sessionId, true), '/paper/sessions/compat:strategy:S00001/position/auto-management'],
		];

		await Promise.all(cases.map(([promise]) => promise));

		const requested = mockFetch.mock.calls.map((call) => String(call[0]));
		const methods = mockFetch.mock.calls.map((call) => (call[1] as RequestInit)?.method);
		for (const [, path] of cases) {
			expect(requested.some((url) => url.endsWith(path))).toBe(true);
		}
		expect(methods.every((method) => method === 'POST')).toBe(true);
	});

	it('rejects unsupported replay controls before fetch', async () => {
		await expect(replayStep('compat:strategy:S00001')).rejects.toThrow('not supported');
		await expect(replaySeek('compat:strategy:S00001', 4)).rejects.toThrow('not supported');
		await expect(replayPlay('compat:strategy:S00001')).rejects.toThrow('not supported');
		await expect(replayPause('compat:strategy:S00001')).rejects.toThrow('not supported');
		await expect(replaySetSpeed('compat:strategy:S00001', 2)).rejects.toThrow('not supported');
		await expect(replayReset('compat:strategy:S00001')).rejects.toThrow('not supported');

		expect(mockFetch).not.toHaveBeenCalled();
	});

	it('does not advertise a paper websocket URL for compatibility sessions', () => {
		expect(() => getPaperWebSocketUrl('compat:strategy:S00001')).toThrow('not supported');
	});
});
