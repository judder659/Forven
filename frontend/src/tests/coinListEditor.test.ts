import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	setStrategyCoins: vi.fn(),
	getStrategyBreadth: vi.fn(),
}));
vi.mock('$lib/api/multiCoin', async (orig) => ({ ...(await orig<typeof import('$lib/api/multiCoin')>()), setStrategyCoins: api.setStrategyCoins }));
vi.mock('$lib/api/breadth', () => ({ getStrategyBreadth: api.getStrategyBreadth }));

import CoinListEditor from '../lib/components/strategy/container/CoinListEditor.svelte';

const LIST = { home: 'ETH', coins: ['ETH', 'UNI', 'NEAR'], tested_coins: ['ETH', 'UNI', 'NEAR'], edited_at: null, max_coins: 15 };

describe('coin list editor', () => {
	afterEach(() => {
		cleanup();
		vi.clearAllMocks();
	});

	it('adds a typed coin and a breadth suggestion, removes one, and saves the extras', async () => {
		api.getStrategyBreadth.mockResolvedValue({ rows: [{ asset: 'SOL', trades: 9, return_pct: 12, sharpe: 1.4 }, { asset: 'ZEC', error: 'no data' }] });
		api.setStrategyCoins.mockResolvedValue({ ...LIST, coins: ['ETH', 'UNI', 'LINK', 'SOL'] });
		const saved = vi.fn();
		const { container } = render(CoinListEditor, {
			props: { strategyId: 'S1', list: LIST, openCoins: ['UNI'] },
			events: { saved: (e: CustomEvent) => saved(e.detail) },
		} as never);

		await fireEvent.input(screen.getByLabelText('Add a coin'), { target: { value: 'link/usdt' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add' }));
		await waitFor(() => expect(screen.getByText(/\+ SOL/)).toBeTruthy());
		expect(screen.queryByText(/\+ ZEC/)).toBeNull();
		await fireEvent.click(screen.getByText(/\+ SOL/));
		await fireEvent.click(screen.getByLabelText('Remove NEAR'));
		expect((screen.getByLabelText('Remove UNI') as HTMLButtonElement).disabled).toBe(true); // open position
		expect(container.querySelector('[data-coin="LINK"]')?.textContent).toContain('by hand');

		await fireEvent.click(screen.getByRole('button', { name: 'Save 4 coins' }));
		expect(api.setStrategyCoins).toHaveBeenCalledWith('S1', ['UNI', 'LINK', 'SOL']);
		await waitFor(() => expect(saved).toHaveBeenCalled());
	});

	it('shows the server refusal', async () => {
		api.getStrategyBreadth.mockResolvedValue({ rows: [] });
		api.setStrategyCoins.mockRejectedValue(new Error('UNI still has an open paper position.'));
		render(CoinListEditor, { props: { strategyId: 'S1', list: LIST } });
		await fireEvent.click(screen.getByLabelText('Remove UNI'));
		await fireEvent.click(screen.getByRole('button', { name: 'Save 2 coins' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('open paper position'));
	});
});
