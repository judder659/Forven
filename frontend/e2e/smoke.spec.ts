import { test, expect } from 'playwright/test';

// These run against a REAL `forven.api` boot (isolated FORVEN_HOME temp dir,
// control-plane only) plus the real SvelteKit frontend — see playwright.config.ts.
// They are the release smoke: app boots, the canonical execution status is
// paper, and a live-mode request fails closed end to end.

const api = 'http://127.0.0.1:8017';

test.describe('startup and canonical execution status', () => {
	test('backend health endpoint responds', async ({ request }) => {
		const res = await request.get(`${api}/health`);
		expect(res.ok()).toBeTruthy();
		const body = await res.json();
		// Control-plane-only boots report "degraded" (no scheduler heartbeat by
		// design); anything else present here means the process didn't come up.
		expect(['ok', 'healthy', 'degraded']).toContain(String(body.status));
	});

	test('a fresh install starts in paper mode', async ({ request }) => {
		const res = await request.get(`${api}/api/settings`);
		expect(res.ok()).toBeTruthy();
		const settings = await res.json();
		expect(settings.trading_mode).toBe('paper');
	});

	test('the app shell renders with the risk disclaimer', async ({ page }) => {
		await page.goto('/');
		// A fresh browser context has never acknowledged the disclaimer, so the
		// banner must be visible — its absence on first load means either the
		// shell failed to render or the paper-only disclaimer regressed.
		await expect(page.getByText('Paper + testnet only.')).toBeVisible({ timeout: 15_000 });
	});
});

test.describe('mode mismatch fails closed', () => {
	test('a live-mode request is rejected and never persisted (MODE-SPLIT-1)', async ({ request }) => {
		const put = await request.put(`${api}/api/settings/trading-mode`, {
			data: { trading_mode: 'live' },
		});
		expect(put.status()).toBe(400);
		const body = await put.json();
		expect(String(body.detail)).toContain("'live'");

		// The rejected mode must not leak into the stored settings — this is the
		// split-brain regression (Settings said live while the runtime was paper).
		const settings = await (await request.get(`${api}/api/settings`)).json();
		expect(settings.trading_mode).toBe('paper');
	});

	test('a paper-mode request round-trips', async ({ request }) => {
		const put = await request.put(`${api}/api/settings/trading-mode`, {
			data: { trading_mode: 'paper' },
		});
		expect(put.ok()).toBeTruthy();
		const settings = await (await request.get(`${api}/api/settings`)).json();
		expect(settings.trading_mode).toBe('paper');
	});
});
