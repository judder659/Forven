import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';

const api = vi.hoisted(() => ({
	getHoldoutSummary: vi.fn(),
	submitHoldout: vi.fn(),
}));

vi.mock('$lib/api/backtesting', () => ({
	getHoldoutSummary: api.getHoldoutSummary,
	submitHoldout: api.submitHoldout,
}));

import HoldoutTile from '../lib/components/robustness/HoldoutTile.svelte';

async function settle(): Promise<void> {
	for (let i = 0; i < 4; i += 1) {
		await Promise.resolve();
		await tick();
	}
}

describe('HoldoutTile', () => {
	let target: HTMLDivElement;
	let app: ReturnType<typeof mount> | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		api.getHoldoutSummary.mockReset();
		api.submitHoldout.mockReset();
	});

	afterEach(() => {
		if (app) unmount(app);
		app = null;
		target.remove();
	});

	it('renders nothing while the research holdout is off', async () => {
		api.getHoldoutSummary.mockResolvedValue({ enabled: false, cutoff: null, state: 'off' });
		app = mount(HoldoutTile, { target, props: { strategyId: 'S1' } });
		await settle();
		expect(target.querySelector('[data-testid="holdout-tile"]')).toBeNull();
	});

	it('shows a failed one-shot verdict with its reasons', async () => {
		api.getHoldoutSummary.mockResolvedValue({
			enabled: true,
			cutoff: '2026-01-01T00:00:00+00:00',
			paper_mode: 'enforce',
			max_family_shots: 3,
			state: 'fail',
			result_id: 'R1',
			latest: {
				result_id: 'R1',
				status: 'succeeded',
				result: {
					verdict: 'FAIL',
					verdict_reasons: ['lost money on the held-back period (-4.2%)'],
					held_back: { start: '2026-01-01T00:00:00+00:00', end: '2026-09-24T23:00:00+00:00' },
					out_of_sample: { total_trades: 17, total_return_pct: -0.042 },
					baseline_hurdle: { alpha_pct: -6.1 },
					family: 'donchian',
					family_shot: 2,
				},
			},
		});
		app = mount(HoldoutTile, { target, props: { strategyId: 'S1' } });
		await settle();

		const text = target.querySelector('[data-testid="holdout-tile"]')?.textContent ?? '';
		expect(text).toContain('FAILED');
		expect(text).toContain('research data ends 2026-01-01');
		expect(text).toContain('-4.2%');
		expect(text).toContain("'donchian' family shot 2 of 3");
		expect(target.querySelector('[data-testid="holdout-reasons"]')?.textContent).toContain('lost money');
		expect(target.querySelector('[data-testid="holdout-run"]')).toBeNull();
	});

	it('offers to run the test only while it is due', async () => {
		api.getHoldoutSummary
			.mockResolvedValueOnce({ enabled: true, cutoff: '2026-01-01T00:00:00+00:00', paper_mode: 'enforce', state: 'missing' })
			.mockResolvedValueOnce({ enabled: true, cutoff: '2026-01-01T00:00:00+00:00', paper_mode: 'enforce', state: 'running', result_id: 'R2' });
		api.submitHoldout.mockResolvedValue({ state: 'running', result_id: 'R2' });
		app = mount(HoldoutTile, { target, props: { strategyId: 'S2' } });
		await settle();

		const button = target.querySelector('[data-testid="holdout-run"]') as HTMLButtonElement;
		expect(button).not.toBeNull();
		button.click();
		await settle();

		expect(api.submitHoldout).toHaveBeenCalledWith('S2');
		expect(target.textContent).toContain('RUNNING');
		expect(target.querySelector('[data-testid="holdout-run"]')).toBeNull();
	});
});
