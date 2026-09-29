import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { ExecutionCheck } from '../lib/api/executionCheck';
import { executingEditWarning, explainCheck, settingChanges } from '../lib/utils/executionCheck';

const api = vi.hoisted(() => ({
	getExecutionCheck: vi.fn(),
	acceptExecutionBacktest: vi.fn(),
	restoreValidatedParams: vi.fn(),
}));
vi.mock('$lib/api/executionCheck', () => api);

import ExecutionCheckPanel from '../lib/components/strategy/ExecutionCheckPanel.svelte';

function check(overrides: Partial<ExecutionCheck> = {}): ExecutionCheck {
	return {
		strategy_id: 'S05665',
		stage: 'live_graduated',
		executing: true,
		executable: false,
		kind: 'params_changed',
		reason: 'Execution differs from its promotion backtest: parameters changed; revalidation required',
		accepted: { verified: true, result_id: 'S05665-manual-8dc9', scope: 'existing_live_execution', leverage: 1.3, params: {}, reason: null },
		changes: [{ key: 'execution_profile.leverage', validated: null, current: 2, kind: 'added' }],
		candidates: [
			{ result_id: 'S05665-manual-a5cf', created_at: '2026-09-29T19:53:25Z', start_date: '2024-12-23', end_date: '2025-12-31', trades: 36, total_return_pct: 0.0563, max_drawdown_pct: 0.0856, win_rate: 0.333, profit_factor: 1.25, sharpe: 1.14, leverage: 2 },
			{ result_id: 'S05665-manual-a6d6', created_at: '2026-09-29T19:48:16Z', start_date: '2023-01-01', end_date: '2025-12-31', trades: 172, total_return_pct: 0.21, max_drawdown_pct: 0.12, win_rate: 0.4, profit_factor: 1.3, sharpe: 1.0, leverage: 2 },
		],
		actions: { accept_backtest: true, restore: true, gauntlet: false },
		...overrides,
	};
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('execution check wording', () => {
	it('explains a live settings change and a paper promotion without evidence', () => {
		const live = explainCheck(check());
		expect(live.title).toBe('New live entries are blocked');
		expect(live.fix).toContain('Accept a backtest of the new settings');
		const paper = explainCheck(check({ stage: 'paper', kind: 'unverified' }));
		expect(paper.title).toBe('New paper entries are blocked');
		expect(paper.fix).toContain('gauntlet');
		expect(paper.fix).toContain('no operator shortcut');
	});

	it('lists what a save changes and warns only for trading strategies', () => {
		const current = { leverage: 1.3, sl_mult: 1.8 };
		const next = { leverage: 1.3, sl_mult: 1.8, execution_profile: { leverage: 2, fee_bps: 4.5 } };
		expect(settingChanges(current, next).map((change) => change.key)).toEqual(['execution_profile.fee_bps', 'execution_profile.leverage']);
		const warning = executingEditWarning({ strategyId: 'S05665', stage: 'live_graduated', current, next }) ?? '';
		expect(warning).toContain('S05665 trades LIVE');
		expect(warning).toContain('execution_profile.leverage: not set → 2');
		expect(warning).toContain('accept a backtest of the new settings as the live baseline');
		expect(executingEditWarning({ strategyId: 'S1', stage: 'paper', current, next })).toContain('run the gauntlet');
		expect(executingEditWarning({ strategyId: 'S1', stage: 'quick_screen', current, next })).toBeNull();
		expect(executingEditWarning({ strategyId: 'S1', stage: 'live_graduated', current, next: current })).toBeNull();
	});
});

describe('ExecutionCheckPanel', () => {
	beforeEach(() => {
		api.getExecutionCheck.mockReset();
		api.acceptExecutionBacktest.mockReset();
		api.restoreValidatedParams.mockReset();
	});
	afterEach(() => cleanup());

	it('stays out of the way while the strategy can trade', async () => {
		api.getExecutionCheck.mockResolvedValue(check({ executable: true, kind: 'ok', reason: null, changes: [], candidates: [] }));
		render(ExecutionCheckPanel, { props: { strategyId: 'S05665' } });
		await flush();
		expect(screen.queryByTestId('execution-check')).toBeNull();
	});

	it('shows what changed and restores the validated settings with a recorded reason', async () => {
		api.getExecutionCheck.mockResolvedValue(check());
		api.restoreValidatedParams.mockResolvedValue(check({ executable: true, kind: 'ok', reason: null, changes: [], candidates: [] }));
		const changed = vi.fn();
		render(ExecutionCheckPanel, {
			props: { strategyId: 'S05665' },
			events: { changed: (event: CustomEvent<ExecutionCheck>) => changed(event.detail) },
		});
		await flush();
		expect(screen.getByText('New live entries are blocked')).toBeTruthy();
		expect(screen.getByTestId('execution-check-changes').textContent).toContain('execution_profile.leverage');

		await fireEvent.click(screen.getByTestId('execution-check-restore'));
		expect(screen.getByTestId('execution-check-restore-confirm').textContent).toContain('1.3× leverage');
		await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
		await flush();
		expect(api.restoreValidatedParams).toHaveBeenCalledWith('S05665', 'Undo a settings change');
		expect(changed).toHaveBeenCalledTimes(1);
		expect(screen.queryByTestId('execution-check')).toBeNull();
	});

	it('accepts the backtest the operator picks, never one chosen for them silently', async () => {
		api.getExecutionCheck.mockResolvedValue(check());
		api.acceptExecutionBacktest.mockRejectedValueOnce(new Error('The backtest must contain actual completed trades'));
		render(ExecutionCheckPanel, { props: { strategyId: 'S05665' } });
		await flush();
		await fireEvent.click(screen.getByTestId('execution-check-accept'));
		const confirm = screen.getByTestId('execution-check-accept-confirm');
		expect(confirm.textContent).toContain('does not re-run the research gates');
		expect(confirm.textContent).toContain('36 trades');
		expect(confirm.textContent).toContain('172 trades');
		await fireEvent.click(screen.getByDisplayValue('S05665-manual-a6d6'));
		await fireEvent.click(screen.getByRole('button', { name: 'Accept as live baseline' }));
		await flush();
		expect(api.acceptExecutionBacktest).toHaveBeenCalledWith('S05665', 'S05665-manual-a6d6', 'Accept the new settings for live trading');
		// A refusal from the server is shown, and the panel stays open.
		expect(screen.getByRole('alert').textContent).toContain('actual completed trades');
		expect(screen.getByTestId('execution-check-accept-confirm')).toBeTruthy();
	});

	it('points a paper strategy at the gauntlet with no accept button', async () => {
		api.getExecutionCheck.mockResolvedValue(check({
			stage: 'paper', kind: 'unverified', changes: [], candidates: [],
			accepted: { verified: false, result_id: null, scope: 'promotion', leverage: null, params: {}, reason: 'No workflow confirmation is bound to this promotion' },
			actions: { accept_backtest: false, restore: false, gauntlet: true },
		}));
		render(ExecutionCheckPanel, { props: { strategyId: 'S05665', forgeHref: '/lab/strategy/S05665' } });
		await flush();
		expect(screen.queryByTestId('execution-check-accept')).toBeNull();
		expect(screen.queryByTestId('execution-check-restore')).toBeNull();
		expect(screen.getByText('Open the Forge to run the gauntlet').getAttribute('href')).toBe('/lab/strategy/S05665');
	});
});
