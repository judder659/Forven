import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import DeskAttention from '../lib/components/trading/desk/DeskAttention.svelte';
import type { AttentionItem } from '../lib/utils/tradingDesk/attention';

const items: AttentionItem[] = [
	{ id: 'a', severity: 'fail', title: 'S1: exit refused while in position', body: 'The stop still rests on the exchange.', strategyId: 'S1', tab: 'why' },
	{ id: 'b', severity: 'caution', title: 'S2: 124 entries refused in 30 days', body: 'Execution settings unverified.', strategyId: 'S2' },
	{ id: 'c', severity: 'info', title: 'S3: too early to judge', body: 'Seven closed trades.' },
];

describe('DeskAttention collapse', () => {
	beforeEach(() => window.localStorage.clear());
	afterEach(() => cleanup());

	it('starts open and folds the list away with the − button, keeping the counts', async () => {
		render(DeskAttention, { props: { mode: 'paper', items } });
		const toggle = screen.getByTestId('desk-attention-toggle');
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(toggle.textContent).toContain('−');
		expect(screen.getByText('S2: 124 entries refused in 30 days')).toBeTruthy();

		await fireEvent.click(toggle);

		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(toggle.textContent).toContain('+');
		expect(screen.queryByText('S2: 124 entries refused in 30 days')).toBeNull();
		expect(screen.getByText('1 critical')).toBeTruthy();
		expect(screen.getByTestId('desk-attention').textContent).toContain('1 critical · 1 warning · 1 note');
		expect(window.localStorage.getItem('forven.paper.attentionCollapsed')).toBe('1');

		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(screen.getByText('S2: 124 entries refused in 30 days')).toBeTruthy();
		expect(window.localStorage.getItem('forven.paper.attentionCollapsed')).toBeNull();
	});

	it('remembers the choice per desk', async () => {
		window.localStorage.setItem('forven.live.attentionCollapsed', '1');
		render(DeskAttention, { props: { mode: 'live', items } });
		await Promise.resolve();
		expect(screen.getByTestId('desk-attention-toggle').getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByText('S1: exit refused while in position')).toBeNull();
		cleanup();

		render(DeskAttention, { props: { mode: 'paper', items } });
		await Promise.resolve();
		expect(screen.getByTestId('desk-attention-toggle').getAttribute('aria-expanded')).toBe('true');
	});
});
